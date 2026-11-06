#!/usr/bin/env python3
"""Install checksum-locked host clients in disposable state, never global paths."""
import hashlib
import json
import os
import platform
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def host_lock(machine=None):
    machine = platform.machine() if machine is None else machine
    names = {'x86_64': 'versions.lock.json', 'aarch64': 'versions.arm64.lock.json'}
    if machine not in names:
        raise ValueError('unsupported client host architecture: ' + machine)
    return json.loads((ROOT / names[machine]).read_text())


def main():
    lock = host_lock()
    destination = ROOT / '.tmp/e2e/bin'
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('kubectl', 'talosctl'):
        spec = lock['test_tools'][name]
        data = urllib.request.urlopen(spec['url'], timeout=120).read()
        if hashlib.sha256(data).hexdigest() != spec['sha256']:
            raise RuntimeError('client checksum mismatch')
        path = destination / name
        path.write_bytes(data)
        path.chmod(0o755)
    if os.environ.get('GITHUB_PATH'):
        with open(os.environ['GITHUB_PATH'], 'a') as stream:
            stream.write(str(destination) + '\n')


if __name__ == '__main__':
    main()
