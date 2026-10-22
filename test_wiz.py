import json
import socket
import unittest
from unittest.mock import MagicMock, patch

import wiz


class ProtocolTests(unittest.TestCase):
    def transport(self, responses):
        transport = MagicMock()
        transport.__enter__.return_value = transport
        transport.recv.side_effect = responses
        return transport

    def test_ignores_malformed_and_unrelated_replies(self):
        transport = self.transport([
            b'not-json', b'[]', b'{"method":"other","result":{}}',
            b'{"method":"getPilot","result":{"state":true}}',
        ])
        with patch('wiz.socket.socket', return_value=transport):
            response = wiz.request('192.168.1.20', 'getPilot')
        self.assertTrue(response['result']['state'])

    def test_reports_device_error(self):
        transport = self.transport([b'{"method":"setPilot","error":{"code":-1}}'])
        with patch('wiz.socket.socket', return_value=transport):
            with self.assertRaisesRegex(RuntimeError, 'Light returned'):
                wiz.request('192.168.1.20', 'setPilot', {'state': True})

    def test_packet_loss_retries_then_reports_failure(self):
        transport = self.transport([socket.timeout(), socket.timeout(), socket.timeout()])
        with patch('wiz.socket.socket', return_value=transport):
            with self.assertRaises(TimeoutError):
                wiz.request('192.168.1.20', 'getPilot')
        self.assertEqual(transport.send.call_count, 3)

    def test_lost_first_reply_recovers(self):
        transport = self.transport([
            socket.timeout(), b'{"method":"setPilot","result":{"success":true}}',
        ])
        with patch('wiz.socket.socket', return_value=transport):
            result = wiz.request('192.168.1.20', 'setPilot', {'state': False})
        self.assertTrue(result['result']['success'])
        self.assertEqual(transport.send.call_count, 2)
        packet = json.loads(transport.send.call_args.args[0])
        self.assertIs(packet['params']['state'], False)


if __name__ == '__main__':
    unittest.main()
