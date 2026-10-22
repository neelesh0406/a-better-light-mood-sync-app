"""Latest-sample-only screen/audio -> WiZ streaming, with bounded UDP traffic."""
import colorsys
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

HELPER = Path(__file__).resolve().parent / 'Luma Capture.app/Contents/MacOS/LumaCapture'


def capture_info(request_permission=False):
    if not HELPER.exists():
        raise RuntimeError('Build the capture helper first: python3 build_capture.py')
    try:
        result = subprocess.run([str(HELPER), '--permission' if request_permission else '--status'],
                                capture_output=True, text=True, timeout=20, check=True)
    except subprocess.SubprocessError as error:
        raise RuntimeError('macOS capture setup did not finish. Check the permission prompt or System Settings, then retry.') from error
    for line in reversed(result.stdout.splitlines()):
        if line.startswith('{'):
            return json.loads(line)
    raise RuntimeError('The macOS capture helper did not respond.')


def settings(raw):
    result = {'mode': 'screen', 'display': 0, 'region': 'whole', 'method': 'vibrant',
              'fps': 15, 'smoothing': 0, 'boost': 125, 'brightness': 100,
              'follow': False, 'sensitivity': 5, 'palette': 'spectrum'}
    if not isinstance(raw, dict) or set(raw) - set(result):
        raise ValueError('Unknown sync setting.')
    result.update(raw)
    for key, choices in [('mode', ('screen', 'music')), ('region', ('whole', 'center', 'edges', 'left', 'right')),
                         ('method', ('vibrant', 'average')), ('palette', ('spectrum', 'violet', 'ocean', 'ember'))]:
        if result[key] not in choices:
            raise ValueError('Invalid ' + key)
    for key, lo, hi in [('fps', 5, 15), ('display', 0, 2**32-1), ('smoothing', 0, 85),
                        ('boost', 50, 200), ('brightness', 10, 100), ('sensitivity', 1, 10)]:
        if type(result[key]) is not int or not lo <= result[key] <= hi:
            raise ValueError('Invalid ' + key)
    if type(result['follow']) is not bool:
        raise ValueError('Follow brightness must be true or false.')
    return result


class ColorMapper:
    def __init__(self, options):
        self.options = options
        self.previous = None
        self.phase = 0.0

    def map(self, video, audio, now):
        o = self.options
        if o['mode'] == 'screen':
            if video is None:
                return None
            source = video[o['method']]
            h, s, value = colorsys.rgb_to_hsv(*[v/255 for v in source])
            if value < .03:
                channels = list(self.previous[:3]) if self.previous else [255, 170, 90]
            else:
                channels = [v*255 for v in colorsys.hsv_to_rgb(h, min(1, s*o['boost']/100), 1)]
            brightness = max(10, round(o['brightness'] * max(.1, video['luma'])**.65)) if o['follow'] else o['brightness']
            if value < .03:
                brightness = 10
        else:
            if audio is None:
                return None
            fresh = now-audio.get('time', now) < .3
            gain = o['sensitivity'] * 2.5
            energy = min(1, audio.get('rms', 0)*gain) if fresh else 0
            bands = [audio.get(key, 0) for key in ('bass', 'mid', 'treble')]
            total = sum(bands)
            # Frequency balance selects the color; amplitude controls brightness.
            if o['palette'] == 'spectrum':
                h = (0.95*bands[2] + .55*bands[1] + .02*bands[0])/max(.00001,total)
            else:
                base = {'violet': .73, 'ocean': .5, 'ember': .02}[o['palette']]
                h = base + energy*.12
            channels = [v*255 for v in colorsys.hsv_to_rgb(h%1, .9, 1)]
            brightness = max(10, round(10+(o['brightness']-10)*energy**.65))
        values = channels + [brightness]
        if self.previous is not None and o['smoothing']:
            alpha = 1-o['smoothing']/100
            # Preserve hard movie cuts instead of trailing the previous scene.
            if max(abs(values[i]-self.previous[i]) for i in range(3)) > 160:
                alpha = 1
            values = [old+(new-old)*alpha for old,new in zip(self.previous,values)]
        self.previous = values
        return {'state': True, 'r': round(values[0]), 'g': round(values[1]),
                'b': round(values[2]), 'dimming': max(10,min(100,round(values[3])))}


class LiveSync:
    def __init__(self, controller):
        self.controller = controller
        self.lifecycle = threading.RLock()
        self.lock = threading.RLock()
        self.thread = None
        self.process = None
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.video = self.audio = None
        self.restore = True
        self.original = None
        self.state = {'active': False, 'error': None, 'sent': 0, 'acknowledged': 0}

    def public(self):
        with self.lock:
            return dict(self.state)

    def start(self, ip, raw):
        options = settings(raw)
        self.controller.known(ip)
        capability = capture_info()
        if not capability.get('permission'):
            raise RuntimeError('Allow Screen & System Audio Recording first using the permission button, then click Start again.')
        if options['display'] not in [d['id'] for d in capability['displays']]:
            raise ValueError('Choose an available display.')
        with self.lifecycle:
            self.stop()
            self.original = self.controller.status(ip)['pilot']
            self.stop_event = threading.Event()
            self.ready = threading.Event()
            self.video = self.audio = None
            self.restore = True
            with self.lock:
                self.state = {'active': False, 'starting': True, 'ip': ip, 'options': options,
                              'error': None, 'sent': 0, 'acknowledged': 0,
                              'captureToSendMs': None, 'sendRate': 0, 'color': None}
            self.process = subprocess.Popen([str(HELPER), str(options['display']), options['mode'], options['region'], str(os.getpid())],
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, bufsize=1)
            threading.Thread(target=self.read, args=(self.process,), daemon=True).start()
            if not self.ready.wait(8) or self.public().get('error'):
                message = self.public().get('error') or 'macOS capture did not start. Check capture permission and retry.'
                self.restore = False
                self.terminate()
                with self.lock:
                    self.state.update(starting=False, error=message)
                raise RuntimeError(message)
            self.controller.cancel_for(ip)
            with self.lock:
                self.state.update(active=True, starting=False)
            self.thread = threading.Thread(target=self.run, args=(ip, options, self.process), daemon=True)
            self.thread.start()
            self.controller.log('Live sync started: ' + options['mode'])
            return self.public()

    def read(self, process):
        try:
            for line in process.stdout:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                with self.lock:
                    if process is not self.process:
                        return
                    event = data.get('event')
                    if event == 'ready':
                        self.ready.set()
                    elif event == 'error':
                        self.state['error'] = str(data.get('message', 'Capture stopped.'))
                        self.ready.set()
                        self.stop_event.set()
                    elif event == 'video':
                        self.video = data
                    elif event == 'audio':
                        self.audio = data
        finally:
            process.stdout.close()
            self.ready.set()

    def terminate(self):
        process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)

    def stop(self, restore=True, ip=None):
        with self.lifecycle:
            if ip is not None and self.public().get('ip') != ip:
                return self.public()
            self.restore = restore
            self.stop_event.set()
            if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
                self.thread.join(timeout=5)
                if self.thread.is_alive():
                    raise RuntimeError('Sync is stopping; please retry in a moment.')
            self.terminate()
            with self.lock:
                self.state.update(active=False, starting=False)
            return self.public()

    def run(self, ip, options, process):
        import wiz
        from app import snapshot
        mapper = ColorMapper(options)
        previous = None
        last_send = 0.0
        started = time.time()
        last_ack = started
        rate_start, rate_count = time.monotonic(), 0
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((ip, wiz.PORT))
                sock.setblocking(False)
                while not self.stop_event.is_set():
                    tick = time.monotonic()
                    if process.poll() is not None:
                        raise RuntimeError(self.public().get('error') or 'Capture helper stopped. Check macOS capture permission.')
                    with self.lock:
                        video, audio = self.video, self.audio
                    params = mapper.map(video, audio, time.time())
                    # Skip unchanged colors; a one-second heartbeat retains liveness.
                    if params and (params != previous or tick-last_send >= 1):
                        sock.send(json.dumps({'method': 'setPilot', 'params': params}).encode())
                        previous, last_send = params, tick
                        rate_count += 1
                        sample = video if options['mode'] == 'screen' else audio
                        with self.lock:
                            self.state['sent'] += 1
                            self.state['color'] = [params[k] for k in ('r','g','b')]
                            self.state['brightness'] = params['dimming']
                            self.state['captureToSendMs'] = round(max(0,(time.time()-sample['time'])*1000),1)
                            self.state['audioLevel'] = audio.get('rms',0) if audio else 0
                    # Drain acknowledgements without ever waiting for a round trip.
                    while True:
                        try:
                            reply = json.loads(sock.recv(16384))
                        except BlockingIOError:
                            break
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if not isinstance(reply,dict) or reply.get('method') != 'setPilot':
                            continue
                        if 'error' in reply:
                            raise RuntimeError('Bulb rejected live sync: ' + json.dumps(reply['error']))
                        if reply.get('result',{}).get('success') is True:
                            last_ack = time.time()
                            with self.lock:
                                self.state['acknowledged'] += 1
                    if params and time.time()-last_ack > 4:
                        raise RuntimeError('The bulb stopped acknowledging sync commands. Check Wi-Fi connectivity.')
                    if not params and time.time()-started > 8:
                        raise RuntimeError('No capture samples received. Check capture permission; for music, start system audio playback.')
                    if tick-rate_start >= 1:
                        with self.lock:
                            self.state['sendRate'] = round(rate_count/(tick-rate_start),1)
                        rate_start,rate_count = tick,0
                    self.stop_event.wait(max(0, 1/options['fps']-(time.monotonic()-tick)))
        except (OSError,RuntimeError,ValueError) as error:
            with self.lock:
                self.state['error'] = str(error)
            self.controller.log('Live sync: ' + str(error),False)
        finally:
            self.terminate()
            if self.restore and self.original:
                try:
                    reply = wiz.request(ip,'setPilot',snapshot(self.original),timeout=.5)
                    if reply['result'].get('success') is not True:
                        raise RuntimeError('Restore was not acknowledged.')
                except (OSError,RuntimeError) as error:
                    self.controller.log('Could not restore after sync: '+str(error),False)
                    with self.lock:
                        self.state['error'] = 'Sync stopped; restore failed: '+str(error)
            with self.lock:
                self.state.update(active=False,starting=False)
            self.controller.log('Live sync stopped' + ('; original light restored' if self.restore else ''))
