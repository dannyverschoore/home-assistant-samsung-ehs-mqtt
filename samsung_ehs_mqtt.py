import os
import serial
import json
import time
import re
import inspect
import paho.mqtt.client as mqtt

from pysamsungnasa.protocol.factory.messages import outdoor, indoor, basic, network

PORT = os.getenv("SERIAL_PORT", "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")

MQTT_HOST = os.getenv("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "mqtt_samsung")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "samsung12345")

BASE = "samsung_ehs"
MODULES = [outdoor, indoor, basic, network]

PRINT_VALUES = False
HEARTBEAT_SECONDS = 60
REPUBLISH_SECONDS = 60

KEEP_KEYWORDS = [
  "outdoor", "ambient", "indoor", "room",
  "water", "inlet", "outlet", "target",
  "compressor", "frequency", "current", "voltage",
  "power", "pump", "flow", "dhw", "tank",
  "defrost", "mode", "operation", "error",
]

KEEP_IDS = set()


def slugify(text):
  text = text.lower()
  text = re.sub(r"[^a-z0-9]+", "_", text)
  return text.strip("_")


def is_useful_sensor(msg_id, msg_name):
  name = msg_name.lower()
  return msg_id in KEEP_IDS or any(word in name for word in KEEP_KEYWORDS)


def unit_to_device_class(unit):
  return {
    "°C": "temperature",
    "A": "current",
    "V": "voltage",
    "Hz": "frequency",
    "W": "power",
  }.get(unit)


def build_message_map():
  messages = {}

  for module in MODULES:
    for _, cls in inspect.getmembers(module, inspect.isclass):
      msg_id = getattr(cls, "MESSAGE_ID", None)
      msg_name = getattr(cls, "MESSAGE_NAME", None)

      if msg_id is None or msg_name is None:
        continue

      if not is_useful_sensor(msg_id, msg_name):
        continue

      unit = getattr(cls, "UNIT_OF_MEASUREMENT", None)
      scale = getattr(cls, "ARITHMETIC", None)

      if scale is None:
        if unit in ("°C", "A"):
          scale = 0.1
        elif "temperature" in msg_name.lower() or "temp" in msg_name.lower():
          scale = 0.1
          unit = unit or "°C"
        elif "pressure" in msg_name.lower():
          scale = 0.1
        else:
          scale = 1

      key = slugify(f"{msg_id:04x}_{msg_name}")

      messages[msg_id] = {
        "key": key,
        "name": msg_name,
        "unit": unit,
        "scale": scale,
      }

  return messages


MESSAGES = build_message_map()
published_discovery = set()
last_values = {}
last_publish_time = {}
last_heartbeat = 0


client = mqtt.Client()

if MQTT_USER:
  client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_start()


def publish_discovery(msg_id, info):
  if msg_id in published_discovery:
    return

  key = info["key"]
  unit = info["unit"]

  payload = {
    "name": f"Samsung EHS {info['name']}",
    "state_topic": f"{BASE}/{key}/state",
    "unique_id": f"{BASE}_{key}",
    "state_class": "measurement",
    "device": {
      "identifiers": [BASE],
      "name": "Samsung EHS",
      "manufacturer": "Samsung",
      "model": "EHS Mono R290",
    },
  }

  if unit:
    payload["unit_of_measurement"] = unit
    device_class = unit_to_device_class(unit)
    if device_class:
      payload["device_class"] = device_class

  client.publish(
    f"homeassistant/sensor/{BASE}_{key}/config",
    json.dumps(payload),
    retain=True,
  )

  published_discovery.add(msg_id)


def read_value(payload, i):
  prefix = payload[i]

  if prefix in (0x80, 0x40, 0x02):
    if i + 2 >= len(payload):
      return None, 1
    msg_id = (payload[i] << 8) | payload[i + 1]
    raw = payload[i + 2]
    return (msg_id, raw), 3

  if prefix in (0x82, 0x42, 0x22):
    if i + 3 >= len(payload):
      return None, 1
    msg_id = (payload[i] << 8) | payload[i + 1]
    raw = int.from_bytes(payload[i + 2:i + 4], "big", signed=True)
    return (msg_id, raw), 4

  if prefix in (0x84, 0x44, 0x24):
    if i + 5 >= len(payload):
      return None, 1
    msg_id = (payload[i] << 8) | payload[i + 1]
    raw = int.from_bytes(payload[i + 2:i + 6], "big", signed=True)
    return (msg_id, raw), 6

  return None, 1


def publish_value(msg_id, value, info):
  now = time.time()
  key = info["key"]

  old_value = last_values.get(msg_id)
  old_time = last_publish_time.get(msg_id, 0)

  if old_value == value and now - old_time < REPUBLISH_SECONDS:
    return

  publish_discovery(msg_id, info)

  client.publish(
    f"{BASE}/{key}/state",
    value,
    retain=True,
  )

  last_values[msg_id] = value
  last_publish_time[msg_id] = now

  if PRINT_VALUES:
    print(f"{msg_id:04X} | {info['name']} = {value} {info['unit'] or ''}", flush=True)


def handle_payload(payload):
  i = 0

  while i < len(payload) - 2:
    result, step = read_value(payload, i)

    if result:
      msg_id, raw = result

      if msg_id in MESSAGES:
        info = MESSAGES[msg_id]
        value = round(raw * info["scale"], 2)
        publish_value(msg_id, value, info)

    i += step


def heartbeat():
  global last_heartbeat

  now = time.time()
  if now - last_heartbeat >= HEARTBEAT_SECONDS:
    print(
      f"Samsung EHS actief | sensors={len(last_values)} | mqtt={client.is_connected()}",
      flush=True,
    )
    last_heartbeat = now


print(f"MQTT verbonden, {len(MESSAGES)} bruikbare NASA messages geladen", flush=True)

while True:
  ser = None

  try:
    ser = serial.Serial(
      PORT,
      9600,
      bytesize=serial.EIGHTBITS,
      parity=serial.PARITY_NONE,
      stopbits=serial.STOPBITS_ONE,
      timeout=1,
    )

    print(f"Seriële poort geopend: {ser.name}", flush=True)
    buffer = b""

    while True:
      heartbeat()

      data = ser.read(512)

      if not data:
        continue

      buffer += data

      while True:
        start = buffer.find(b"\x34")

        if start < 0:
          if len(buffer) > 4096:
            buffer = b""
          break

        if start > 0:
          buffer = buffer[start:]

        next_start = buffer.find(b"\x34", 1)

        if next_start < 0:
          if len(buffer) > 4096:
            buffer = buffer[-512:]
          break

        frame = buffer[:next_start]
        buffer = buffer[next_start:]

        if len(frame) < 22:
          continue

        payload = frame[19:-2]
        handle_payload(payload)

  except KeyboardInterrupt:
    print("Gestopt", flush=True)
    break

  except Exception as e:
    print(f"Fout: {e}", flush=True)
    print("Opnieuw proberen over 10 seconden...", flush=True)
    time.sleep(10)

  finally:
    try:
      if ser:
        ser.close()
    except Exception:
      pass
