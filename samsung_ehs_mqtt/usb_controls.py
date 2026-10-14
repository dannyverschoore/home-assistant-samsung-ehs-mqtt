"""Validated NASA controls. No physical setting is changed at startup."""
import binascii
import json
import math
import time
from collections import deque
from pathlib import Path


class USBControls:
    def __init__(self, client, base):
        self.client, self.base = client, base
        self.specs = {item['id']: item for item in json.loads(Path(__file__).with_name('controls.json').read_text(encoding='utf-8'))}
        self.queue = deque()
        self.values = {}
        self.valid = set()
        self.pending = None
        self.counter = 128
        self.unlock_until = 0
        self.last_refresh = 0

    @property
    def busy(self):
        return self.pending is not None

    def publish(self, suffix, value):
        self.client.publish(f'{self.base}/{suffix}', value, retain=True)

    def status(self, status, detail):
        self.publish('control/result/state', status)
        self.publish('control/result/attributes', json.dumps({'tijd': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'uitleg': detail}, ensure_ascii=False))
        print(f'USB bediening | {status} | {detail}', flush=True)

    def discovery(self):
        removed = json.loads(Path(__file__).with_name('removed_dhw.json').read_text(encoding='utf-8'))
        for spec in removed['controls']:
            self.client.publish(f"homeassistant/{spec['domain']}/{self.base}_usb_{spec['id']:04x}/config", '', retain=True)
        for key in removed['sensor_keys']:
            self.client.publish(f'homeassistant/sensor/{self.base}_{key}/config', '', retain=True)
        device = {'identifiers': [self.base], 'name': 'Samsung EHS', 'manufacturer': 'Samsung'}
        for msg_id, spec in self.specs.items():
            if msg_id == 0x4000:
                continue  # Keep the existing master switch and its entity ID.
            domain = spec['domain']
            key = f'{self.base}_usb_{msg_id:04x}'
            prefix = f'control/usb/{msg_id:04x}'
            payload = {'name': spec['name'], 'unique_id': key, 'device': device,
                       'command_topic': f'{self.base}/{prefix}/set',
                       'state_topic': f'{self.base}/{prefix}/state',
                       'availability': [{'topic': f'{self.base}/availability'}, {'topic': f'{self.base}/{prefix}/availability'}],
                       'availability_mode': 'all', 'optimistic': False}
            if spec['advanced']:
                payload['entity_category'] = 'config'
            if domain == 'number':
                payload.update(min=spec['min'], max=spec['max'], step=spec['step'], mode='box')
                if spec.get('unit'):
                    payload['unit_of_measurement'] = spec['unit']
            elif domain == 'select':
                payload['options'] = list(spec['options'])
            else:
                payload.update(payload_on='ON', payload_off='OFF')
            self.client.publish(f'homeassistant/{domain}/{key}/config', json.dumps(payload, ensure_ascii=False), retain=True)
            self.publish(f'{prefix}/availability', 'offline')
        self.client.publish(f'homeassistant/switch/{self.base}_installer_unlock/config', json.dumps({
            'name': 'Installateursinstellingen ontgrendelen (10 min)', 'unique_id': f'{self.base}_installer_unlock',
            'device': device, 'command_topic': f'{self.base}/control/installer_unlock/set',
            'state_topic': f'{self.base}/control/installer_unlock/state', 'entity_category': 'config',
            'availability_topic': f'{self.base}/availability', 'optimistic': False,
            'payload_on': 'ON', 'payload_off': 'OFF'}), retain=True)
        self.publish('control/installer_unlock/state', 'OFF')
        self.client.publish(f'homeassistant/button/{self.base}_refresh_controls/config', json.dumps({
            'name': 'USB instellingen opnieuw uitlezen', 'unique_id': f'{self.base}_refresh_controls',
            'device': device, 'command_topic': f'{self.base}/control/refresh', 'payload_press': 'READ',
            'entity_category': 'diagnostic', 'availability_topic': f'{self.base}/availability'}), retain=True)
        self.client.publish(f'homeassistant/sensor/{self.base}_usb_result/config', json.dumps({
            'name': 'Laatste USB opdracht', 'unique_id': f'{self.base}_usb_result', 'device': device,
            'state_topic': f'{self.base}/control/result/state', 'json_attributes_topic': f'{self.base}/control/result/attributes',
            'entity_category': 'diagnostic', 'availability_topic': f'{self.base}/availability'}), retain=True)
        for key, name in [('compressor_running', 'Compressor draait'), ('heat_demand', 'Warmte of koelvraag aanwezig')]:
            self.client.publish(f'homeassistant/binary_sensor/{self.base}_{key}/config', json.dumps({
                'name': name, 'unique_id': f'{self.base}_{key}', 'device': device,
                'state_topic': f'{self.base}/control/{key}/state', 'availability_topic': f'{self.base}/availability',
                'payload_on': 'ON', 'payload_off': 'OFF'}), retain=True)

    def refresh(self, all_settings=True):
        if any(item['type'] == 1 and not item.get('confirm') for item in self.queue):
            return
        for target in ('200000', '100000'):
            ids = [msg_id for msg_id, spec in self.specs.items() if spec['target'] == target and (all_settings or not spec['advanced'])]
            if target == '200000':
                ids += [0x4069, 0x406A, 0x4028]
            else:
                ids += [0x8238]
            for index in range(0, len(ids), 8):
                self.queue.append({'type': 1, 'target': target, 'values': [(i, 0) for i in ids[index:index+8]], 'time': time.monotonic()})
        self.last_refresh = time.monotonic()

    def message(self, message):
        topic = message.topic
        if not topic.startswith(f'{self.base}/control/'):
            return False
        recognized = topic in (f'{self.base}/control/installer_unlock/set', f'{self.base}/control/refresh') or topic.startswith(f'{self.base}/control/usb/')
        if not recognized:
            return False
        if getattr(message, 'retain', False):
            self.status('Genegeerd', 'Een opgeslagen MQTT opdracht wordt niet opnieuw uitgevoerd')
            return True
        value = message.payload.decode('utf-8', errors='replace').strip()
        if topic.endswith('/installer_unlock/set'):
            self.unlock_until = time.monotonic() + 600 if value == 'ON' else 0
            self.publish('control/installer_unlock/state', 'ON' if self.unlock_until else 'OFF')
            return True
        if topic.endswith('/refresh'):
            self.refresh()
            return True
        try:
            parts = topic.split('/')
            if len(parts) != 5 or parts[-1] != 'set':
                raise ValueError('Onbekend bedieningstopic')
            msg_id = int(parts[-2], 16)
            spec = self.specs[msg_id]
            if spec['domain'] == 'number':
                number = float(value)
                minimum, maximum = self.allowed_range(spec)
                if not math.isfinite(number) or not minimum <= number <= maximum:
                    raise ValueError(f'Waarde buiten het actuele bereik {minimum:g} tot {maximum:g}')
                units = (number - spec['min']) / spec['step']
                if abs(units - round(units)) > 1e-6:
                    raise ValueError('Waarde past niet bij de toegestane stapgrootte')
                raw = round(number / spec['scale'])
            else:
                raw = spec['options'][value]
            if (spec['target'], msg_id) not in self.valid:
                raise ValueError('Deze instelling is nog niet geldig uitgelezen bij jouw regelkast')
            if spec['advanced'] and time.monotonic() >= self.unlock_until:
                raise ValueError('Ontgrendel eerst de installateursinstellingen')
            # FSV/reference settings use WRITE, not a temporary control REQUEST.
            kind = 2 if spec['advanced'] else 3
            values = [(msg_id, raw)]
            self.queue.appendleft({'type': kind, 'target': spec['target'], 'values': values, 'confirm_values': [(msg_id, raw)], 'name': spec['name'], 'time': time.monotonic(), 'advanced': spec['advanced']})
            self.status('In wachtrij', f"{spec['name']}: {value}")
        except (ValueError, KeyError, OverflowError) as error:
            self.status('Geblokkeerd', str(error))
        return True

    def allowed_range(self, spec):
        minimum, maximum = spec['min'], spec['max']
        mode = self.values.get(('200000', 0x4001))
        pairs = {}
        if mode == 1:
            pairs = {0x4247: (0x424B, 0x424A), 0x42D7: (0x424B, 0x424A), 0x4201: (0x424D, 0x424C), 0x42D6: (0x424D, 0x424C)}
        elif mode == 4:
            pairs = {0x4247: (0x424F, 0x424E), 0x42D7: (0x424F, 0x424E), 0x4201: (0x4251, 0x4250), 0x42D6: (0x4251, 0x4250)}
        pairs[0x4235] = (0x4253, 0x4252)
        if spec['id'] in pairs:
            lower, upper = pairs[spec['id']]
            if ('200000', lower) in self.valid:
                minimum = max(minimum, self.values[('200000', lower)] * 0.1)
            if ('200000', upper) in self.valid:
                maximum = min(maximum, self.values[('200000', upper)] * 0.1)
        if mode == 1 and spec['id'] in (0x4247, 0x42D7) and self.values.get(('200000', 0x4094)) == 1:
            minimum = max(minimum, 16)  # Samsung's minimum for floor cooling.
        return minimum, maximum

    def packet(self, target, kind, values):
        self.counter = (self.counter + 1) % 256
        payload = b''
        for msg_id, raw in values:
            size = {0: 1, 2: 2, 4: 4}[(msg_id & 0x0600) >> 8]
            payload += msg_id.to_bytes(2, 'big') + (int(raw) & ((1 << (size*8))-1)).to_bytes(size, 'big')
        data = bytes.fromhex('80FF00' + target) + bytes([0xC0, 0x10 | kind, self.counter, len(values)]) + payload
        return b'\x32' + (len(data)+4).to_bytes(2, 'big') + data + binascii.crc_hqx(data, 0).to_bytes(2, 'big') + b'\x34'

    def tick(self, ser, online, bus_time, legacy_busy):
        now = time.monotonic()
        if self.unlock_until and now >= self.unlock_until:
            self.unlock_until = 0
            self.publish('control/installer_unlock/state', 'OFF')
        if self.pending and now >= self.pending['deadline']:
            old = self.pending
            self.pending = None
            if old['type'] == 2:
                expected = old.get('confirm_values', old['values'])
                self.queue.appendleft({'type': 1, 'target': old['target'], 'values': [(i, 0) for i, _ in expected], 'confirm': expected, 'write_retry': True, 'time': now})
            elif old['type'] == 3:
                self.status('Geen bevestiging', f"Instelling {old['values'][0][0]:04X} is niet bevestigd; laatste echte waarde blijft staan")
            elif len(old['values']) > 1 and not old.get('confirm'):
                self.split_read(old)
            elif old.get('confirm'):
                self.status('Geen bevestiging', 'Het teruglezen na de opdracht kreeg geen antwoord')
        if online != 'online':
            self.queue.clear()
            self.pending = None
            return
        if now - self.last_refresh > 60:
            self.refresh(False)
        if self.pending or legacy_busy or now - bus_time < 0.1 or ser.in_waiting or not self.queue:
            return
        item = self.queue.popleft()
        if item['type'] in (2, 3) and (now - item['time'] > 20 or item['advanced'] and now >= self.unlock_until):
            self.status('Geblokkeerd', 'Opdracht verlopen of installateursbediening vergrendeld')
            return
        packet = self.packet(item['target'], item['type'], item['values'])
        ser.write(packet)
        ser.flush()
        item.update(number=self.counter, deadline=now + (15 if item['type'] == 3 else 4))
        self.pending = item
        if item['type'] in (2, 3):
            self.status('Verstuurd', f"{item.get('name', self.specs[item['values'][0][0]]['name'])}; wachten op echte terugmelding")

    def split_read(self, item):
        for value in item['values']:
            self.queue.append({'type': 1, 'target': item['target'], 'values': [value], 'time': time.monotonic()})

    def observe(self, frame, values):
        source, destination = frame[3:6].hex().upper(), frame[6:9].hex().upper()
        kind, number = frame[10] & 15, frame[11]
        item = self.pending
        if item and source == item['target'] and destination == '80FF00' and number == item['number']:
            if kind == 7:
                self.pending = None
                if item['type'] in (2, 3):
                    self.status('Afgewezen', f"Samsung wijst instelling {item['values'][0][0]:04X} af")
                elif len(item['values']) > 1:
                    self.split_read(item)
            elif (kind == 6 and item['type'] in (2, 3)) or (kind == 5 and item['type'] == 2):
                expected = item.get('confirm_values', item['values'])
                self.queue.appendleft({'type': 1, 'target': source, 'values': [(msg_id, 0) for msg_id, _ in expected], 'confirm': expected, 'write_retry': item['type'] == 2, 'time': time.monotonic()})
                self.pending = None
            elif kind == 5 and item['type'] == 1:
                self.pending = None
                if item.get('confirm'):
                    got = dict(values or [])
                    success = all(got.get(msg_id) == raw for msg_id, raw in item['confirm'])
                    msg_id, raw = item['confirm'][0]
                    spec = self.specs[msg_id]
                    # Some EHS controllers acknowledge a water REQUEST without
                    # applying it. One WRITE retry, still within current limits.
                    retry = False
                    if not success and msg_id == 0x4247 and not item.get('write_retry'):
                        lower, upper = self.allowed_range(spec)
                        retry = lower <= raw * spec['scale'] <= upper
                    if retry:
                        self.queue.appendleft({'type': 2, 'target': source, 'values': item['confirm'], 'advanced': False, 'time': time.monotonic()})
                        self.status('Herstelpoging', 'Waterdoel niet toegepast via REQUEST; eenmalige WRITE met terugleescontrole')
                    else:
                        self.status('Bevestigd' if success else 'Niet overgenomen', f"{spec['name']}; gewenste waarde {'teruggemeld' if success else 'niet teruggemeld'}")
                else:
                    missing = [pair for pair in item['values'] if pair[0] not in dict(values or [])]
                    if len(item['values']) > 1 and missing:
                        self.split_read({**item, 'values': missing})
        if kind not in (4, 5) or values is None:
            return
        for msg_id, raw in values:
            self.values[(source, msg_id)] = raw
            if source == '100000' and msg_id == 0x8238 and 0 <= raw <= 200:
                self.publish('control/compressor_running/state', 'ON' if raw > 0 else 'OFF')
            if source == '200000' and msg_id == 0x4028 and raw in (0, 1):
                self.publish('control/heat_demand/state', 'ON' if raw else 'OFF')
            spec = self.specs.get(msg_id)
            if not spec or spec['target'] != source:
                continue
            prefix = f'control/usb/{msg_id:04x}'
            value = raw * spec['scale']
            if spec['domain'] == 'number':
                valid = spec['min'] <= value <= spec['max']
                state = round(value, 4)
            else:
                reverse = {v: k for k, v in spec['options'].items()}
                valid = raw in reverse
                state = reverse.get(raw)
            if valid:
                self.valid.add((source, msg_id))
                self.publish(f'{prefix}/state', state)
                self.publish(f'{prefix}/availability', 'online')
            else:
                self.valid.discard((source, msg_id))
                self.publish(f'{prefix}/availability', 'offline')
