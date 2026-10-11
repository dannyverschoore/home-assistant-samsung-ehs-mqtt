import binascii
import inspect
import json
import os
import re
import time
from queue import Empty, Queue

import paho.mqtt.client as mqtt
import serial
from pysamsungnasa.protocol.factory.messages import basic, indoor, network, outdoor
from usb_controls import USBControls
from pathlib import Path
DHW_REMOVED = set(json.loads(Path(__file__).with_name("removed_dhw.json").read_text(encoding="utf-8"))["message_ids"])


PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
MQTT_HOST = os.getenv("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

BASE = "samsung_ehs"
MODULES = [outdoor, indoor, basic, network]
DATA_TIMEOUT_SECONDS = int(os.getenv("DATA_TIMEOUT_SECONDS", "30"))
REPUBLISH_SECONDS = int(os.getenv("REPUBLISH_SECONDS", "60"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "60"))
PRINT_VALUES = os.getenv("PRINT_VALUES", "false").lower() == "true"

ADDRESS_CLASSES = {
  0x10: "Outdoor",
  0x11: "HTU",
  0x20: "Indoor/EHS",
  0x30: "ERV",
  0x38: "MCU",
  0x40: "Remote controller",
  0x50: "Wired remote",
  0x58: "PIM",
  0x59: "SIM",
  0x62: "WiFi kit",
  0x65: "Central controller",
  0x80: "JIG/client",
  0xB0: "Broadcast",
  0xB1: "Broadcast",
  0xB2: "Broadcast",
  0xB3: "Broadcast",
  0xB4: "Broadcast",
  0xB7: "Broadcast",
  0xB8: "Broadcast",
  0xB9: "Broadcast",
}

KEEP_KEYWORDS = [
  "outdoor", "ambient", "indoor", "room", "water", "inlet", "outlet",
  "target", "compressor", "frequency", "current", "voltage", "power",
  "pump", "flow", "dhw", "tank", "defrost", "mode", "operation", "error",
]


def slugify(text):
  return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def build_message_map():
  messages = {}
  for module in MODULES:
    for _, cls in inspect.getmembers(module, inspect.isclass):
      msg_id = getattr(cls, "MESSAGE_ID", None)
      name = getattr(cls, "MESSAGE_NAME", None)
      if msg_id is None or name is None or msg_id in DHW_REMOVED:
        continue
      if not any(word in name.lower() for word in KEEP_KEYWORDS):
        continue
      unit = getattr(cls, "UNIT_OF_MEASUREMENT", None)
      scale = getattr(cls, "ARITHMETIC", None)
      if scale is None:
        if unit in ("°C", "A") or "temp" in name.lower():
          scale = 0.1
          unit = unit or "°C"
        elif "pressure" in name.lower():
          scale = 0.1
        else:
          scale = 1
      messages[msg_id] = {
        "key": slugify(f"{msg_id:04x}_{name}"),
        "name": "Temperatuurregeling (kamer of aanvoerwater)" if msg_id == 0x406F else name,
        "unit": unit,
        "scale": scale,
      }
  return messages


MESSAGES = build_message_map()
published_discovery = set()
last_values = {}
last_publish_time = {}
detected_addresses = {}
last_connection_state = None
last_heartbeat = 0
zero_published = False
command_queue = Queue()
packet_number = 0
last_bus_activity = 0
read_packet_number = None
read_sent_time = 0
pending_command = None
pending_expected = {}
confirmed_controls = {}
pending_packet_number = None
pending_command_time = 0

client = mqtt.Client()
if MQTT_USER:
  client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
client.will_set(f"{BASE}/availability", "offline", retain=True)
usb = USBControls(client, BASE)


def mqtt_connect():
  while True:
    try:
      client.connect(MQTT_HOST, MQTT_PORT, 60)
      client.loop_start()
      return
    except Exception as error:
      print(f"MQTT fout: {error}; opnieuw over 10 seconden", flush=True)
      time.sleep(10)


def publish_diagnostic_discovery():
  device = {
    "identifiers": [BASE],
    "name": "Samsung EHS",
    "manufacturer": "Samsung",
    "model": "EHS Mono R290",
  }
  client.publish(
    f"homeassistant/binary_sensor/{BASE}_verbinding/config",
    json.dumps({
      "name": "Samsung EHS verbinding",
      "unique_id": f"{BASE}_verbinding",
      "state_topic": f"{BASE}/availability",
      "payload_on": "online",
      "payload_off": "offline",
      "device_class": "connectivity",
      "device": device,
    }),
    retain=True,
  )
  client.publish(
    f"homeassistant/switch/{BASE}_power/config",
    json.dumps({
      "name": "Samsung EHS zone 1 ingeschakeld",
      "unique_id": f"{BASE}_power",
      "command_topic": f"{BASE}/control/power/set",
      "state_topic": f"{BASE}/control/power/state",
      "availability_topic": f"{BASE}/availability",
      "payload_on": "ON",
      "payload_off": "OFF",
      "payload_available": "online",
      "payload_not_available": "offline",
      "optimistic": False,
      "device": device,
    }),
    retain=True,
  )
  # Empty retained discovery messages remove the three former buttons.
  for key in ("start_heating", "start_cooling", "stop_zone"):
    client.publish(f"homeassistant/button/{BASE}_{key}/config", "", retain=True)
  for key, name, command, icon in (
    ("heating", "Verwarmen", "HEAT", "mdi:radiator"),
    ("cooling", "Koelen", "COOL", "mdi:snowflake"),
  ):
    client.publish(f"homeassistant/switch/{BASE}_{key}/config", json.dumps({
      "name": name, "unique_id": f"{BASE}_{key}", "device": device,
      "command_topic": f"{BASE}/control/power/set",
      "state_topic": f"{BASE}/control/{key}/state",
      "payload_on": command, "payload_off": command + "_OFF",
      "state_on": "ON", "state_off": "OFF", "optimistic": False,
      "availability_topic": f"{BASE}/availability", "icon": icon,
    }), retain=True)
  client.publish(f"homeassistant/sensor/{BASE}_confirmed_mode/config", json.dumps({
    "name": "Bedrijfsmodus", "unique_id": f"{BASE}_confirmed_mode", "device": device,
    "state_topic": f"{BASE}/control/mode/state",
    "availability_topic": f"{BASE}/availability", "icon": "mdi:heat-pump",
  }), retain=True)
  client.publish(
    f"homeassistant/sensor/{BASE}_nasa_adressen/config",
    json.dumps({
      "name": "Samsung EHS NASA adressen",
      "unique_id": f"{BASE}_nasa_adressen",
      "state_topic": f"{BASE}/diagnose/address_count",
      "json_attributes_topic": f"{BASE}/diagnose/addresses",
      "icon": "mdi:serial-port",
      "device": device,
    }),
    retain=True,
  )


def on_mqtt_message(_client, _userdata, message):
  if usb.message(message):
    return
  if message.topic == f"{BASE}/diagnose/read_power" and message.payload in (b"READ", b"READ_SETTINGS", b"READ_B20020", b"READ_B0FF20") and not getattr(message, "retain", False):
    command_queue.put(message.payload.decode("ascii"))
    return
  if message.topic != f"{BASE}/control/power/set":
    return
  if getattr(message, "retain", False):
    print("Opgeslagen MQTT-bedieningsopdracht genegeerd", flush=True)
    return
  value = message.payload.decode("utf-8", errors="ignore").strip().upper()
  if value not in ("ON", "OFF", "HEAT", "COOL", "HEAT_OFF", "COOL_OFF"):
    print(f"Ongeldig aan/uit-commando genegeerd: {value}", flush=True)
    return
  command_queue.put(value)


def on_mqtt_connect(client, _userdata, _flags, rc):
  global last_connection_state
  if rc == 0:
    last_connection_state = None  # Republish availability after a broker reconnect.
    client.subscribe(f"{BASE}/control/power/set")
    client.subscribe(f"{BASE}/diagnose/read_power")
    client.subscribe(f"{BASE}/control/usb/+/set")
    client.subscribe(f"{BASE}/control/installer_unlock/set")
    client.subscribe(f"{BASE}/control/refresh")


client.on_connect = on_mqtt_connect
client.on_message = on_mqtt_message


def publish_connection(state):
  global last_connection_state
  if state == last_connection_state:
    return
  client.publish(f"{BASE}/availability", state, retain=True)
  last_connection_state = state
  if state == "online":
    command_queue.put("READ")
    usb.refresh()
  print(f"Samsung EHS verbinding: {state}", flush=True)


def publish_addresses():
  items = sorted(detected_addresses.values(), key=lambda item: item["address"])
  client.publish(f"{BASE}/diagnose/address_count", len(items), retain=True)
  client.publish(
    f"{BASE}/diagnose/addresses",
    json.dumps({"addresses": items}),
    retain=True,
  )


def register_address(raw_address, direction):
  address = raw_address.hex().upper()
  address_class = raw_address[0]
  if address_class not in ADDRESS_CLASSES:
    return
  is_new = address not in detected_addresses
  item = detected_addresses.setdefault(address, {
    "address": address,
    "class": ADDRESS_CLASSES[address_class],
    "seen_as_source": 0,
    "seen_as_destination": 0,
    "last_seen": "",
  })
  item[direction] += 1
  item["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
  if is_new:
    print(f"NASA-adres gevonden: {address} ({item['class']})", flush=True)
  publish_addresses()


def extract_nasa_frames(buffer):
  """Use NASA length and CRC; payload bytes may contain 0x32/0x34."""
  frames = []
  while buffer:
    start = buffer.find(b"\x32")
    if start < 0:
      return frames, b""
    buffer = buffer[start:]
    if len(buffer) < 3:
      break
    size = int.from_bytes(buffer[1:3], "big") + 2
    if not 16 <= size <= 1500:
      buffer = buffer[1:]
      continue
    if len(buffer) < size:
      break
    frame = buffer[:size]
    if frame[-1] != 0x34 or binascii.crc_hqx(frame[3:-3], 0) != int.from_bytes(frame[-3:-1], "big"):
      buffer = buffer[1:]
      continue
    frames.append(frame)
    buffer = buffer[size:]
  return frames, buffer


def decode_datasets(frame):
  payload = frame[13:-3]
  values = []
  index = 0
  for _ in range(frame[12]):
    if index + 2 > len(payload):
      return None
    msg_id = int.from_bytes(payload[index:index + 2], "big")
    kind = (msg_id & 0x0600) >> 8
    index += 2
    if kind == 6:
      # NASA structures use the remaining payload, not a length byte.
      return [] if frame[12] == 1 else None
    else:
      size = {0: 1, 2: 2, 4: 4}[kind]
    if index + size > len(payload):
      return None
    if kind != 6:
      values.append((msg_id, int.from_bytes(payload[index:index + size], "big", signed=kind != 0)))
    index += size
  return values if index == len(payload) else None


def expire_power_command():
  global pending_command, pending_packet_number
  if pending_command is not None and time.time() - pending_command_time >= 15:
    print(f"EHS opdracht {pending_command}: geen bevestigde status binnen 15 seconden", flush=True)
    pending_command = None
    pending_packet_number = None


def observe_nasa_frame(frame):
  global pending_command, pending_packet_number, pending_command_time, read_packet_number
  packet = frame
  source = packet[3:6]
  destination = packet[6:9]
  if source[0] not in ADDRESS_CLASSES or destination[0] not in ADDRESS_CLASSES:
    return
  register_address(source, "seen_as_source")
  register_address(destination, "seen_as_destination")
  data_type = packet[10] & 0x0F
  received_packet_number = packet[11]
  source_address = source.hex().upper()
  destination_address = destination.hex().upper()
  if b"\x40\x00" in packet[13:]:
    type_name = {
      1: "READ",
      2: "WRITE",
      3: "REQUEST",
      4: "NOTIFICATION",
      5: "RESPONSE",
      6: "ACK",
      7: "NACK",
    }.get(data_type, f"TYPE_{data_type}")
    print(
      f"POWER FRAME 0x4000 | bron={source_address} | "
      f"doel={destination_address} | type={type_name} | "
      f"packet={received_packet_number:02X} | hex={packet.hex().upper()}",
      flush=True,
    )
  if source_address == "200000" and destination_address == "80FF00" and received_packet_number == read_packet_number and data_type in (5, 7):
    print(f"NASA leestest antwoord ontvangen: type={data_type} | packet={received_packet_number:02X}", flush=True)
    read_packet_number = None
  if source_address == "200000" and data_type in (5, 6, 7):
    type_name = {5: "RESPONSE", 6: "ACK", 7: "NACK"}[data_type]
    print(
      f"NASA {type_name} ontvangen | bron={source_address} | "
      f"doel={destination_address} | packet={received_packet_number:02X} | "
      f"hex={packet.hex().upper()}",
      flush=True,
    )
    if (pending_command is not None and destination_address == "80FF00"
        and received_packet_number == pending_packet_number):
      print(
        f"Terugmelding voor opdracht {pending_command} na "
        f"{time.time() - pending_command_time:.1f} seconden",
        flush=True,
      )
      if data_type == 7:
        print("EHS heeft de opdracht afgewezen (NACK)", flush=True)
        pending_command = None
        pending_packet_number = None
      elif data_type == 6:
        usb.queue.appendleft({"type": 1, "target": "200000", "values": [(msg_id, 0) for msg_id in pending_expected], "time": time.monotonic(), "legacy_confirm": True})


def publish_sensor_discovery(msg_id, info):
  if msg_id in published_discovery:
    return
  payload = {
    "name": f"Samsung EHS {info['name']}",
    "state_topic": f"{BASE}/{info['key']}/state",
    "unique_id": f"{BASE}_{info['key']}",
    "state_class": "measurement",
    "availability_topic": f"{BASE}/availability",
    "payload_available": "online",
    "payload_not_available": "offline",
    "device": {
      "identifiers": [BASE],
      "name": "Samsung EHS",
      "manufacturer": "Samsung",
      "model": "EHS Mono R290",
    },
  }
  if info["unit"]:
    payload["unit_of_measurement"] = info["unit"]
  client.publish(
    f"homeassistant/sensor/{BASE}_{info['key']}/config",
    json.dumps(payload),
    retain=True,
  )
  published_discovery.add(msg_id)


def read_value(payload, index):
  prefix = payload[index]
  if prefix in (0x80, 0x40, 0x02) and index + 2 < len(payload):
    return ((payload[index] << 8) | payload[index + 1], payload[index + 2]), 3
  if prefix in (0x82, 0x42, 0x22) and index + 3 < len(payload):
    raw = int.from_bytes(payload[index + 2:index + 4], "big", signed=True)
    return ((payload[index] << 8) | payload[index + 1], raw), 4
  if prefix in (0x84, 0x44, 0x24) and index + 5 < len(payload):
    raw = int.from_bytes(payload[index + 2:index + 6], "big", signed=True)
    return ((payload[index] << 8) | payload[index + 1], raw), 6
  return None, 1


def handle_payload(frame):
  global pending_command, pending_packet_number
  values = decode_datasets(frame)
  if values is None:
    print("NASA bericht heeft ongeldige datasetlengte; metingen genegeerd", flush=True)
    return
  source_address = frame[3:6].hex().upper()
  data_type = frame[10] & 0x0F
  if source_address == "200000" and data_type == 5:
    print("EHS uitgelezen waarden: " + ", ".join(f"{msg_id:04X}={raw}" for msg_id, raw in values), flush=True)
  # An echoed REQUEST/WRITE is not a measurement or a confirmed state.
  if data_type not in (4, 5):
    return
  if source_address == "200000":
    for msg_id, raw in values:
      if msg_id == 0x4001 or (msg_id == 0x4000 and raw in (0, 1)):
        confirmed_controls[msg_id] = raw
    power = confirmed_controls.get(0x4000)
    mode = confirmed_controls.get(0x4001)
    if power == 0 or (power == 1 and mode is not None):
      client.publish(f"{BASE}/control/heating/state", "ON" if power == 1 and mode == 4 else "OFF", retain=True)
      client.publish(f"{BASE}/control/cooling/state", "ON" if power == 1 and mode == 1 else "OFF", retain=True)
  for msg_id, raw in values:
    if msg_id == 0x4000 and source_address == "200000" and raw in (0, 1):
      reported_state = "ON" if raw == 1 else "OFF"
      client.publish(f"{BASE}/control/power/state", reported_state, retain=True)
    if msg_id == 0x4001 and source_address == "200000":
      modes = {0: "Auto", 1: "Koelen", 2: "Drogen", 3: "Ventileren", 4: "Verwarmen"}
      client.publish(f"{BASE}/control/mode/state", modes.get(raw, f"Onbekend ({raw})"), retain=True)
    if source_address == "200000" and pending_command is not None and pending_expected.get(msg_id) == raw:
      del pending_expected[msg_id]
      if not pending_expected:
        print(f"EHS bevestigt gewenste bediening: {pending_command}", flush=True)
        pending_command = None
        pending_packet_number = None
    if msg_id in MESSAGES:
      info = MESSAGES[msg_id]
      value = round(raw * info["scale"], 2)
      now = time.time()
      if last_values.get(msg_id) != value or now - last_publish_time.get(msg_id, 0) >= REPUBLISH_SECONDS:
        publish_sensor_discovery(msg_id, info)
        client.publish(f"{BASE}/{info['key']}/state", value, retain=True)
        last_values[msg_id] = value
        last_publish_time[msg_id] = now
        if PRINT_VALUES:
          print(f"{msg_id:04X} | {info['name']} = {value}", flush=True)


def build_power_packet(turn_on):
  global packet_number
  packet_number = (packet_number + 1) % 256
  # JIG/client 80FF00 -> binnenunit 200000, NORMAL + REQUEST, bericht 0x4000.
  data = bytes.fromhex(
    f"80FF00200000C013{packet_number:02X}014000{'01' if turn_on else '00'}"
  )
  crc = binascii.crc_hqx(data, 0)
  size = len(data) + 4
  return b"\x32" + size.to_bytes(2, "big") + data + crc.to_bytes(2, "big") + b"\x34"


def build_mode_packet(command):
  global packet_number
  packet_number = (packet_number + 1) % 256
  mode = {"HEAT": 4, "COOL": 1}[command]
  # Set operating mode and enable zone 1 together; leave temperatures unchanged.
  data = bytes.fromhex(f"80FF00200000C013{packet_number:02X}024001{mode:02X}400001")
  return b"\x32" + (len(data) + 4).to_bytes(2, "big") + data + binascii.crc_hqx(data, 0).to_bytes(2, "big") + b"\x34"


def process_power_command(ser):
  global pending_command, pending_packet_number, pending_command_time
  global read_packet_number, read_sent_time
  expire_power_command()
  if read_packet_number is not None and time.time() - read_sent_time >= 15:
    print("NASA leestest: geen passend antwoord binnen 15 seconden", flush=True)
    read_packet_number = None
  if read_packet_number is not None:
    return
  if time.monotonic() - last_bus_activity < 0.1 or ser.in_waiting:
    return
  try:
    command = command_queue.get_nowait()
  except Empty:
    return
  if last_connection_state != "online":
    print(f"Commando {command} geblokkeerd: EHS is offline", flush=True)
    return
  if pending_command is not None and time.time() - pending_command_time < 15:
    print(
      f"Commando {command} geblokkeerd: opdracht {pending_command} wacht nog op antwoord",
      flush=True,
    )
    return
  if command in ("READ", "READ_SETTINGS", "READ_B20020", "READ_B0FF20"):
    global packet_number
    packet_number = (packet_number + 1) % 256
    target = {"READ": "200000", "READ_SETTINGS": "200000", "READ_B20020": "B20020", "READ_B0FF20": "B0FF20"}[command]
    datasets = "02400000400100" if command == "READ" else "01400000"
    if command == "READ_SETTINGS":
      ids = (0x4069, 0x406A, 0x406F, 0x4095, 0x4028, 0x4201, 0x4247, 0x423A)
      datasets = f"{len(ids):02X}" + "".join(f"{msg_id:04X}" + ("0000" if msg_id & 0x0600 == 0x0200 else "00") for msg_id in ids)
    data = bytes.fromhex(f"80FF00{target}C011{packet_number:02X}{datasets}")
    packet = b"\x32" + (len(data) + 4).to_bytes(2, "big") + data + binascii.crc_hqx(data, 0).to_bytes(2, "big") + b"\x34"
    ser.write(packet)
    ser.flush()
    read_packet_number = packet_number
    read_sent_time = time.time()
    print(f"NASA leestest verstuurd | bron=80FF00 | doel={target} | packet={packet_number:02X}", flush=True)
    return
  if command in ("HEAT_OFF", "COOL_OFF"):
    print(f"{command}: zone 1 blijft behouden; stoppen via warmtevraag is nog niet ingericht", flush=True)
    return
  packet = build_mode_packet(command) if command in ("HEAT", "COOL") else build_power_packet(command == "ON")
  pending_expected.clear()
  pending_expected.update({0x4000: 0 if command == "OFF" else 1})
  if command in ("HEAT", "COOL"):
    pending_expected[0x4001] = 4 if command == "HEAT" else 1
  ser.write(packet)
  ser.flush()
  pending_command = command
  pending_packet_number = packet_number
  pending_command_time = time.time()
  print(
    f"EHS aan/uit REQUEST verzonden: {command} naar 200000 | "
    f"packet={packet_number:02X} | hex={packet.hex().upper()}",
    flush=True,
  )


def publish_unavailable():
  global zero_published
  publish_connection("offline")
  if zero_published:
    return
  zero_published = True
  print("Geen geldige EHS-data: entiteiten unavailable; laatste waarden behouden", flush=True)


mqtt_connect()
publish_diagnostic_discovery()
usb.discovery()
publish_connection("offline")
print(f"Diagnose actief; {len(MESSAGES)} bruikbare NASA messages geladen", flush=True)

while True:
  ser = None
  try:
    ser = serial.Serial(
      PORT, 9600, bytesize=serial.EIGHTBITS,
      parity=serial.PARITY_EVEN, stopbits=serial.STOPBITS_ONE, timeout=0.05, exclusive=True,
    )
    print(f"Seriële poort geopend: {ser.name} | 9600 8E1", flush=True)
    buffer = b""
    last_valid_frame = time.time()
    while True:
      now = time.time()
      if now - last_heartbeat >= HEARTBEAT_SECONDS:
        print(
          f"Diagnose heartbeat | adressen={len(detected_addresses)} | "
          f"sensors={len(last_values)} | mqtt={client.is_connected()}",
          flush=True,
        )
        last_heartbeat = now
      data = ser.read(512)
      if data:
        last_bus_activity = time.monotonic()
        buffer += data
        frames, buffer = extract_nasa_frames(buffer)
        for frame in frames:
          usb.observe(frame, decode_datasets(frame))
          observe_nasa_frame(frame)
          # Only actual indoor/outdoor traffic proves the EHS is available.
          if frame[3] in (0x10, 0x20) and (frame[10] & 0x0F) in (4, 5):
            last_valid_frame = time.time()
            zero_published = False
            publish_connection("online")
          handle_payload(frame)
      if not usb.busy:
        process_power_command(ser)
      legacy_busy = read_packet_number is not None or (pending_command is not None and not (usb.queue and usb.queue[0].get("legacy_confirm")))
      usb.tick(ser, last_connection_state, last_bus_activity, legacy_busy)
      if time.time() - last_valid_frame >= DATA_TIMEOUT_SECONDS:
        publish_unavailable()
  except KeyboardInterrupt:
    publish_unavailable()
    break
  except Exception as error:
    print(f"Fout: {error}", flush=True)
    publish_unavailable()
    time.sleep(10)
  finally:
    try:
      if ser:
        ser.close()
    except Exception:
      pass

