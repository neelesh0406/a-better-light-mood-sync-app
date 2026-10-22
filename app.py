#!/usr/bin/env python3
"""Luma: a local, dependency-free WiZ control app."""
import argparse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from urllib.parse import urlparse, parse_qs

import wiz
from live_sync import LiveSync, capture_info

ROOT = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get('LUMA_STATE_FILE', str(ROOT / '.luma.json')))
SCENES = {1: 'Ocean', 2: 'Romance', 3: 'Sunset', 4: 'Party', 5: 'Fireplace',
          6: 'Cozy', 7: 'Forest', 8: 'Pastel colors', 9: 'Wake-up', 10: 'Bedtime',
          11: 'Warm white', 12: 'Daylight', 13: 'Cool white', 14: 'Night light',
          15: 'Focus', 16: 'Relax', 17: 'True colors', 18: 'TV time',
          19: 'Plantgrowth', 20: 'Spring', 21: 'Summer', 22: 'Fall',
          23: 'Deep dive', 24: 'Jungle', 25: 'Mojito', 26: 'Club',
          27: 'Christmas', 28: 'Halloween', 29: 'Candlelight', 30: 'Golden white',
          31: 'Pulse', 32: 'Steampunk', 33: 'Diwali', 34: 'White',
          35: 'Alarm', 36: 'Snowy sky', 40: 'Dim-to-warm'}


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Expected a whole number from %s to %s.' % (low, high))
    return value


def pilot_params(raw):
    if not isinstance(raw, dict) or not raw:
        raise ValueError('Choose a light setting.')
    allowed = {'state', 'dimming', 'r', 'g', 'b', 'temp', 'sceneId', 'speed'}
    if set(raw) - allowed:
        raise ValueError('Unknown light setting.')
    result = dict(raw)
    if 'state' in raw and type(raw['state']) is not bool:
        raise ValueError('Power must be true or false.')
    for key, low, high in [('dimming', 10, 100), ('r', 0, 255), ('g', 0, 255),
                           ('b', 0, 255), ('temp', 2200, 6500), ('speed', 10, 200)]:
        if key in raw:
            integer(raw[key], low, high)
    if set(raw) & {'r', 'g', 'b'} and not {'r', 'g', 'b'} <= set(raw):
        raise ValueError('Supply all three RGB channels.')
    if sum([bool(set(raw) & {'r', 'g', 'b'}), 'temp' in raw, 'sceneId' in raw]) > 1:
        raise ValueError('Choose color, white temperature, or a scene.')
    if 'sceneId' in raw and (type(raw['sceneId']) is not int or raw['sceneId'] not in SCENES):
        raise ValueError('Choose a supported scene.')
    return result


def snapshot(pilot):
    result = {key: pilot[key] for key in ('state', 'dimming') if key in pilot}
    if pilot.get('sceneId') in SCENES:
        result['sceneId'] = pilot['sceneId']
        if 'speed' in pilot:
            result['speed'] = pilot['speed']
    elif all(key in pilot for key in ('r', 'g', 'b')):
        result.update({key: pilot[key] for key in ('r', 'g', 'b')})
    elif 'temp' in pilot:
        result['temp'] = pilot['temp']
    return result


class Controller:
    def __init__(self, state_file=STATE_FILE):
        self.file = Path(state_file)
        self.lock = threading.RLock()
        self.device_locks = {}
        self.undo = {}
        self.logs = []
        self.stop_event = threading.Event()
        self.sync = LiveSync(self)
        self.data = {'lights': [{'ip': '192.168.1.8', 'name': 'Philips A60', 'room': 'My room'}],
                     'favorites': [], 'jobs': []}
        if self.file.exists():
            self.data = json.loads(self.file.read_text())

    def save(self):
        with self.lock:
            tmp = self.file.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.chmod(0o600)
            tmp.replace(self.file)

    def log(self, message, ok=True):
        with self.lock:
            self.logs.insert(0, {'time': time.time(), 'message': message, 'ok': ok})
            del self.logs[60:]

    def known(self, ip):
        with self.lock:
            if not any(light['ip'] == ip for light in self.data['lights']):
                raise ValueError('Add this light first.')
            return self.device_locks.setdefault(ip, threading.RLock())

    def status(self, ip, info=False):
        with self.known(ip):
            pilot = wiz.request(ip, 'getPilot', timeout=1)['result']
            result = {'ip': ip, 'online': True, 'pilot': pilot}
            if info:
                raw = wiz.request(ip, 'getSystemConfig', timeout=1)['result']
                result['info'] = {key: raw[key] for key in ('mac', 'fwVersion', 'moduleName') if key in raw}
            return result

    def cancel_for(self, ip):
        with self.lock:
            self.data['jobs'] = [job for job in self.data['jobs']
                                 if not (job['ip'] == ip and job['kind'] == 'fade')]
            self.save()

    def apply(self, ip, params, label='Light updated', remember=True, manual=True):
        params = pilot_params(params)
        # A manual command or due routine takes over cleanly from live sync.
        self.sync.stop(restore=False, ip=ip)
        with self.known(ip):
            if manual:
                self.cancel_for(ip)
            if remember:
                previous = snapshot(wiz.request(ip, 'getPilot', timeout=1)['result'])
            reply = wiz.request(ip, 'setPilot', params, timeout=1)
            if reply['result'].get('success') is not True:
                raise RuntimeError('The bulb did not acknowledge this change.')
            if remember:
                self.undo[ip] = previous
            self.log(label)
            try:
                return self.status(ip)
            except OSError:
                return {'ip': ip, 'online': False, 'acknowledged': True,
                        'warning': 'Command acknowledged; state refresh timed out.'}

    def add_light(self, ip, name, room):
        address = ipaddress.IPv4Address(ip)
        network = wiz.local_network()
        if address not in network or address in (network.network_address, network.broadcast_address):
            raise ValueError('Use a light IP on this Mac’s current network: %s' % network)
        pilot = wiz.request(str(address), 'getPilot', timeout=1)['result']
        if not pilot.get('mac'):
            raise ValueError('This device did not identify as a WiZ light.')
        with self.lock:
            light = {'ip': str(address), 'name': str(name or 'WiZ light')[:50], 'room': str(room or 'My room')[:40]}
            self.data['lights'] = [item for item in self.data['lights'] if item['ip'] != light['ip']]
            self.data['lights'].append(light)
            self.save()
        return light

    def public(self):
        with self.lock:
            return json.loads(json.dumps({**self.data, 'logs': self.logs, 'scenes': SCENES,
                                          'sync': self.sync.public(),
                                          'serverTime': time.time(),
                                          'timezone': datetime.now().astimezone().tzname()}))

    def add_job(self, body):
        ip = body['ip']
        self.known(ip)
        kind = body.get('kind')
        if kind not in ('timer', 'daily', 'fade'):
            raise ValueError('Unknown routine type.')
        params = pilot_params(body.get('params', {'state': False}))
        now = time.time()
        job = {'id': secrets.token_hex(6), 'ip': ip, 'kind': kind,
               'name': str(body.get('name', 'Routine'))[:60], 'params': params}
        if kind == 'daily':
            clock = datetime.strptime(body.get('time', ''), '%H:%M')
            target = datetime.now().replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
            if target.timestamp() <= now:
                target += timedelta(days=1)
            job.update(due=target.timestamp(), clock=body['time'])
        else:
            minutes = integer(body.get('minutes'), 1, 1440)
            job['due'] = now + minutes * 60
            if kind == 'fade':
                with self.known(ip):
                    current = self.status(ip)['pilot']
                    if not current.get('state'):
                        raise ValueError('Turn the light on before starting a sleep fade.')
                    self.cancel_for(ip)
                    job.update(start=now, initial=current.get('dimming', 100), next=now)
        with self.lock:
            if len(self.data['jobs']) >= 50:
                raise ValueError('Remove a routine before adding more (limit 50).')
            self.data['jobs'].append(job)
            self.save()
        self.log('Scheduled: ' + job['name'])
        return job

    def tick(self):
        now = time.time()
        with self.lock:
            jobs = [dict(job) for job in self.data['jobs']]
        for job in jobs:
            fade = job['kind'] == 'fade'
            if (job.get('next', job['due']) if fade else job['due']) > now:
                continue
            # Serialize with manual commands so cancellation cannot leave a late fade write.
            with self.known(job['ip']):
                with self.lock:
                    live = next((j for j in self.data['jobs'] if j['id'] == job['id']), None)
                    if live is None:
                        continue
                    if not fade or now >= job['due']:
                        if job['kind'] == 'daily':
                            clock = datetime.strptime(job['clock'], '%H:%M')
                            tomorrow = datetime.fromtimestamp(now).replace(hour=clock.hour, minute=clock.minute,
                                                                           second=0, microsecond=0) + timedelta(days=1)
                            live['due'] = tomorrow.timestamp()
                        else:
                            self.data['jobs'].remove(live)
                    else:
                        live['next'] = now + 5
                    self.save()
                # Do not execute stale overnight jobs unexpectedly after restart/wake.
                if now - job['due'] > 300 and not fade:
                    self.log('Missed while app/Mac was inactive: ' + job['name'], False)
                    continue
                try:
                    if fade and now < job['due']:
                        fraction = (now - job['start']) / (job['due'] - job['start'])
                        params = {'dimming': max(10, round(job['initial'] * (1 - fraction)))}
                        reply = wiz.request(job['ip'], 'setPilot', params, timeout=1)
                        if reply['result'].get('success') is not True:
                            raise RuntimeError('The bulb did not acknowledge the fade step.')
                    else:
                        self.apply(job['ip'], job['params'], job['name'], remember=False, manual=False)
                except (OSError, ValueError, RuntimeError) as error:
                    self.log(job['name'] + ': ' + str(error), False)
                    if fade:
                        self.cancel_for(job['ip'])

    def run(self):
        while not self.stop_event.wait(1):
            try:
                self.tick()
            except Exception as error:
                self.log('Scheduler: ' + str(error), False)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, body, kind='application/json'):
        data = json.dumps(body).encode() if kind == 'application/json' else body
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host') in ('127.0.0.1:%s' % self.server.server_port,
                                          'localhost:%s' % self.server.server_port)

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Local access only.'})
        parsed = urlparse(self.path)
        try:
            if parsed.path == '/api/config':
                return self.send(200, {**self.server.controller.public(), 'token': self.server.token})
            if parsed.path == '/api/status':
                ip = parse_qs(parsed.query).get('ip', [''])[0]
                return self.send(200, self.server.controller.status(ip, info=True))
            if parsed.path == '/api/sync':
                return self.send(200, self.server.controller.sync.public())
            if parsed.path == '/api/capture':
                return self.send(200, capture_info())
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8')}
            files.update({'/sync.js': ('sync.js', 'text/javascript; charset=utf-8'),
                          '/sync.css': ('sync.css', 'text/css; charset=utf-8')})
            if parsed.path not in files:
                return self.send(404, {'error': 'Not found.'})
            name, kind = files[parsed.path]
            return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
        except (OSError, RuntimeError) as error:
            return self.send(503, {'error': str(error), 'online': False})
        except ValueError as error:
            return self.send(400, {'error': str(error)})

    def do_POST(self):
        expected = 'http://' + self.headers.get('Host', '')
        if (not self.valid_host() or self.headers.get('X-Luma-Token') != self.server.token
                or self.headers.get('Origin', expected) != expected):
            return self.send(403, {'error': 'Reload the local app to connect.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                raise ValueError('Invalid request size.')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('Expected an object.')
            c = self.server.controller
            path = urlparse(self.path).path
            if path == '/api/control':
                result = c.apply(body['ip'], body['params'], str(body.get('label', 'Light updated'))[:80])
            elif path == '/api/sync/start':
                result = c.sync.start(body['ip'], body.get('options', {}))
            elif path == '/api/sync/stop':
                result = c.sync.stop()
            elif path == '/api/capture/permission':
                result = capture_info(request_permission=True)
            elif path == '/api/undo':
                ip = body['ip']
                with c.known(ip):
                    if ip not in c.undo:
                        raise ValueError('Nothing to undo yet in this app session.')
                    result = c.apply(ip, c.undo[ip], 'Previous setting restored')
            elif path == '/api/lights':
                result = c.add_light(body['ip'], body.get('name'), body.get('room'))
            elif path == '/api/rename':
                c.known(body['ip'])
                with c.lock:
                    for light in c.data['lights']:
                        if light['ip'] == body['ip']:
                            light['name'] = str(body.get('name', light['name']))[:50]
                            light['room'] = str(body.get('room', light['room']))[:40]
                    c.save()
                result = c.public()
            elif path == '/api/discover':
                network = wiz.local_network()
                devices = wiz.discover(str(network), wait=1)
                result = {'devices': devices, 'network': str(network)}
            elif path == '/api/favorites':
                with c.known(body['ip']):
                    params = snapshot(c.status(body['ip'])['pilot'])
                with c.lock:
                    if len(c.data['favorites']) >= 30:
                        raise ValueError('Favorite limit reached (30).')
                    c.data['favorites'].append({'id': secrets.token_hex(6),
                                               'name': str(body.get('name', 'My mood'))[:40],
                                               'params': params})
                    c.save()
                result = c.public()
            elif path == '/api/jobs':
                # The newly requested routine owns this light from now on.
                c.sync.stop(restore=True, ip=body['ip'])
                result = c.add_job(body)
            elif path == '/api/delete':
                collection = body.get('collection')
                if collection not in ('jobs', 'favorites', 'lights'):
                    raise ValueError('Unknown collection.')
                if collection == 'lights':
                    c.sync.stop(ip=body['id'])
                with c.lock:
                    key = 'ip' if collection == 'lights' else 'id'
                    c.data[collection] = [item for item in c.data[collection] if item[key] != body['id']]
                    if collection == 'lights':
                        c.data['jobs'] = [job for job in c.data['jobs'] if job['ip'] != body['id']]
                    c.save()
                result = c.public()
            else:
                return self.send(404, {'error': 'Not found.'})
            return self.send(200, result)
        except (ValueError, KeyError, TypeError) as error:
            return self.send(400, {'error': str(error)})
        except (OSError, RuntimeError) as error:
            self.server.controller.log(str(error), False)
            return self.send(503, {'error': str(error)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--parent-pid', type=int, help='Exit when the owning desktop app exits')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.controller = Controller()
    server.token = secrets.token_urlsafe(32)
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    if args.parent_pid:
        def watch_parent():
            while not server.controller.stop_event.wait(1):
                if os.getppid() != args.parent_pid:
                    shutdown()
                    return
        threading.Thread(target=watch_parent, daemon=True).start()
    threading.Thread(target=server.controller.run, daemon=True).start()
    print('Luma is ready at http://127.0.0.1:%s' % server.server_port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.controller.stop_event.set()
        server.controller.sync.stop()
        server.server_close()


if __name__ == '__main__':
    main()
