#!/usr/bin/env python3
"""Dependency-free WiZ LAN controller. Python 3.9+, macOS/Linux."""
import argparse
import collections
import ipaddress
import json
import socket
import subprocess
import sys
import time

PORT = 38899


def ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
        if not address.is_private or address.is_multicast or address.is_unspecified:
            raise ValueError()
        return str(address)
    except ValueError:
        raise argparse.ArgumentTypeError('Enter a local IPv4 address.')


def bounded(low, high):
    def parse(value):
        try:
            number = int(value)
            if low <= number <= high:
                return number
        except ValueError:
            pass
        raise argparse.ArgumentTypeError('Expected an integer from %d to %d.' % (low, high))
    return parse


def request(ip, method, params=None, timeout=2, port=PORT):
    """Retry idempotent commands; accept only matching replies from the target."""
    payload = json.dumps({'method': method, 'params': params or {}}).encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((ip, port))
        for attempt in range(3):
            sock.send(payload)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                sock.settimeout(max(.001, deadline - time.monotonic()))
                try:
                    reply = json.loads(sock.recv(16384))
                except socket.timeout:
                    break
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(reply, dict) or reply.get('method') != method:
                    continue
                if 'error' in reply:
                    raise RuntimeError('Light returned: ' + json.dumps(reply['error']))
                if not isinstance(reply.get('result'), dict):
                    continue
                return reply
    raise TimeoutError('No WiZ reply from %s after 3 attempts.' % ip)


def local_network():
    """Find the active private IPv4 subnet on macOS, without assuming /24."""
    result = subprocess.run(['/sbin/route', '-n', 'get', 'default'],
                            capture_output=True, text=True, check=True)
    interface = next(line.split(':', 1)[1].strip() for line in result.stdout.splitlines()
                     if line.strip().startswith('interface:'))
    result = subprocess.run(['/sbin/ifconfig', interface],
                            capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        words = line.split()
        if words[:1] == ['inet']:
            mask = words[words.index('netmask') + 1]
            if mask.startswith('0x'):
                mask = str(ipaddress.IPv4Address(int(mask, 16)))
            return ipaddress.IPv4Network(words[1] + '/' + mask, strict=False)
    raise RuntimeError('Cannot find the active IPv4 network. Pass --network explicitly.')


def discover(network, wait=3):
    network = ipaddress.IPv4Network(network, strict=False)
    if not network.is_private or network.num_addresses > 1024:
        raise ValueError('Discovery requires a private subnet of /22 or smaller.')
    payload = b'{"method":"getPilot","params":{}}'
    errors = collections.Counter()
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(('', 0))
        # Receive broadcast replies before probing hosts: flooding the entire subnet
        # first can lose replies while the OS resolves hundreds of ARP entries.
        batches = [[network.broadcast_address], [network.broadcast_address]]
        hosts = list(network.hosts())
        batches.extend(hosts[index:index + 16] for index in range(0, len(hosts), 16))
        for index, batch in enumerate(batches):
            if index == 2 and found:
                break
            for address in batch:
                try:
                    sock.sendto(payload, (str(address), PORT))
                except OSError as error:
                    errors[str(error)] += 1
            deadline = time.monotonic() + (wait if index < 2 else .2)
            while time.monotonic() < deadline:
                sock.settimeout(max(.001, deadline - time.monotonic()))
                try:
                    data, sender = sock.recvfrom(16384)
                    reply = json.loads(data)
                    if (sender[1] == PORT and ipaddress.IPv4Address(sender[0]) in network
                            and isinstance(reply, dict) and reply.get('method') == 'getPilot'
                            and isinstance(reply.get('result'), dict)
                            and reply['result'].get('mac')):
                        found[sender[0]] = reply['result']
                except socket.timeout:
                    break
                except (ValueError, UnicodeDecodeError):
                    continue
                except OSError as error:
                    errors[str(error)] += 1
        if errors:
            print('Network errors: ' + json.dumps(dict(errors)), file=sys.stderr)
    return found


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    scan = sub.add_parser('discover', help='Read-only WiZ scan on the local subnet')
    scan.add_argument('--network', help='Example: 192.168.1.0/24; auto-detected on macOS')
    for name in ['status', 'info', 'on', 'off', 'brightness', 'color', 'white']:
        cmd = sub.add_parser(name)
        cmd.add_argument('ip', type=ipv4)
        if name == 'brightness':
            cmd.add_argument('percent', type=bounded(10, 100))
        elif name == 'color':
            for channel in ['red', 'green', 'blue']:
                cmd.add_argument(channel, type=bounded(0, 255))
        elif name == 'white':
            cmd.add_argument('kelvin', type=bounded(2200, 6500),
                             help='Conservative range; actual range depends on the light')
    return p


def main():
    args = parser().parse_args()
    try:
        if args.command == 'discover':
            devices = discover(args.network or str(local_network()))
            print(json.dumps(devices, indent=2))
            if not devices:
                print('No WiZ lights found. See README.md for network troubleshooting.', file=sys.stderr)
                return 1
            return 0
        if args.command in ('status', 'info'):
            method = 'getPilot' if args.command == 'status' else 'getSystemConfig'
            print(json.dumps(request(args.ip, method), indent=2))
            return 0
        params = {'state': args.command != 'off'}
        if args.command == 'brightness':
            params['dimming'] = args.percent
        elif args.command == 'color':
            params.update(r=args.red, g=args.green, b=args.blue)
        elif args.command == 'white':
            params.update(temp=args.kelvin)
        reply = request(args.ip, 'setPilot', params)
        if reply['result'].get('success') is not True:
            raise RuntimeError('Light did not confirm success: ' + json.dumps(reply))
        print(json.dumps({'acknowledgement': reply,
                          'current_state': request(args.ip, 'getPilot')}, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, StopIteration) as error:
        print('Error: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
