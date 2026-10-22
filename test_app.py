from datetime import datetime
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import Controller, Handler, ThreadingHTTPServer, pilot_params, snapshot


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.c = Controller(Path(self.temp.name) / 'state.json')
        self.pilot = {'state': True, 'dimming': 80, 'sceneId': 0, 'r': 255, 'g': 0, 'b': 0}

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, ip, method, params=None, **kwargs):
        if method == 'getPilot':
            return {'result': dict(self.pilot)}
        self.pilot.update(params)
        return {'result': {'success': True}}

    def test_rejects_invalid_and_conflicting_commands(self):
        for params in ({'sceneId': 0}, {'r': 255}, {'dimming': 0}, {'state': 1},
                       {'temp': 2700, 'r': 255, 'g': 0, 'b': 0}, {'reboot': True},
                       {'speed': 999}, {'sceneId': True}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                pilot_params(params)

    def test_snapshot_does_not_send_scene_zero(self):
        saved = snapshot(self.pilot)
        self.assertNotIn('sceneId', saved)
        self.assertEqual(saved['r'], 255)
        pilot_params(saved)

    def test_control_and_undo_restore_original(self):
        with patch('app.wiz.request', side_effect=self.transport):
            self.c.apply('192.168.1.8', {'dimming': 50})
            self.assertEqual(self.pilot['dimming'], 50)
            self.c.apply('192.168.1.8', self.c.undo['192.168.1.8'])
            self.assertEqual(self.pilot['dimming'], 80)

    def test_timer_persists_and_runs_once(self):
        with patch('app.wiz.request', side_effect=self.transport):
            self.c.add_job({'ip': '192.168.1.8', 'kind': 'timer', 'minutes': 1,
                            'params': {'state': False}})
            restored = Controller(self.c.file)
            self.assertEqual(len(restored.data['jobs']), 1)
            with patch('app.time.time', return_value=time.time() + 61):
                restored.tick()
                restored.tick()
            self.assertFalse(self.pilot['state'])
            self.assertEqual(restored.data['jobs'], [])

    def test_manual_change_cancels_fade(self):
        with patch('app.wiz.request', side_effect=self.transport):
            self.c.add_job({'ip': '192.168.1.8', 'kind': 'fade', 'minutes': 20})
            self.c.apply('192.168.1.8', {'dimming': 70})
        self.assertEqual(self.c.data['jobs'], [])

    def test_fade_dims_then_turns_off(self):
        with patch('app.wiz.request', side_effect=self.transport):
            job = self.c.add_job({'ip': '192.168.1.8', 'kind': 'fade', 'minutes': 2})
            with patch('app.time.time', return_value=job['start'] + 60):
                self.c.tick()
                self.assertEqual(self.pilot['dimming'], 40)
            with patch('app.time.time', return_value=job['due'] + 1):
                self.c.tick()
                self.assertFalse(self.pilot['state'])
        self.assertEqual(self.c.data['jobs'], [])

    def test_missed_timer_is_skipped(self):
        self.c.add_job({'ip': '192.168.1.8', 'kind': 'timer', 'minutes': 1,
                        'params': {'state': False}})
        with patch('app.time.time', return_value=time.time() + 1000), patch('app.wiz.request') as request:
            self.c.tick()
            request.assert_not_called()
        self.assertIn('Missed', self.c.logs[0]['message'])

    def test_daily_repeats_at_next_local_day(self):
        job = self.c.add_job({'ip': '192.168.1.8', 'kind': 'daily', 'time': '23:59',
                              'params': {'state': False}})
        original = job['due']
        with patch('app.time.time', return_value=original + 1), patch('app.wiz.request', side_effect=self.transport):
            # Keep this test's due time aligned with datetime.now used by the scheduler.
            self.c.tick()
        self.assertEqual(len(self.c.data['jobs']), 1)
        self.assertGreater(self.c.data['jobs'][0]['due'], original)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.controller = Controller(Path(self.temp.name) / 'state.json')
        self.server.token = 'test-token'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def call(self, method, path, body=None, headers=None):
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers=headers or {})
        response = conn.getresponse()
        content = response.read()
        status = response.status
        conn.close()
        return status, content

    def test_home_and_config(self):
        self.assertEqual(self.call('GET', '/')[0], 200)
        status, content = self.call('GET', '/api/config')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)['token'], 'test-token')

    def test_cross_origin_and_missing_token_cannot_control_light(self):
        body = {'ip': '192.168.1.8', 'params': {'state': False}}
        with patch('app.wiz.request') as request:
            self.assertEqual(self.call('POST', '/api/control', body)[0], 403)
            self.assertEqual(self.call('POST', '/api/control', body,
                                      {'X-Luma-Token': 'test-token', 'Origin': 'https://example.com'})[0], 403)
            self.assertEqual(self.call('GET', '/api/config', headers={'Host': 'example.com'})[0], 403)
            request.assert_not_called()

    def test_invalid_input_is_rejected_before_device_access(self):
        body = {'ip': '192.168.1.8', 'params': {'sceneId': 0}}
        with patch('app.wiz.request') as request:
            self.assertEqual(self.call('POST', '/api/control', body,
                                      {'X-Luma-Token': 'test-token'})[0], 400)
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
