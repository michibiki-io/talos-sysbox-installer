#!/usr/bin/python3
"""Actual installed consumer path. Run as the isolated UID/GID1001 probe only."""
import os
from pathlib import Path
import subprocess
import tempfile

BASE = Path('/opt/proc-transfer')
ORACLE = Path('/tmp/issue5-repo')


def commands(target):
    probe = str(BASE / 'thread-sysfs')
    for network in ('shared-net', 'new-net'):
        for mode in ('readwrite', 'readonly'):
            yield ['bwrap', '--die-with-parent', '--unshare-all', '--share-net',
                   '--bind', '/', '/', '--dev-bind', '/dev', '/dev', '--tmpfs', '/tmp',
                   '--ro-bind', str(ORACLE), str(ORACLE), '--ro-bind', probe, probe,
                   '--cap-add', 'CAP_SYS_ADMIN', '--cap-add', 'CAP_NET_ADMIN', '--',
                   probe, '/tmp/thread-sysfs', mode, network]
            yield ['unshare', '--user', '--map-root-user', '--mount', '--pid', '--fork', '--',
                   probe, str(target / (network + '-' + mode)), mode, network]
    yield ['bwrap', '--die-with-parent', '--unshare-all', '--bind', '/', '/',
           '--dev-bind', '/dev', '/dev', '--cap-add', 'CAP_SYS_ADMIN',
           '--cap-add', 'CAP_NET_ADMIN', '--', '/usr/bin/python3',
           str(BASE / 'runtime/sysfs-interleave.py'), '/sys']
    yield ['unshare', '--user', '--map-root-user', '--mount', '--net', '--',
           '/usr/bin/python3', str(BASE / 'runtime/sysfs-write-policy.py')]


def main():
    if (os.getuid(), os.getgid()) != (1001, 1001):
        raise ValueError('requires dedicated UID/GID1001 probe')
    with tempfile.TemporaryDirectory(prefix='sysfs-runtime-', dir='/tmp') as work:
        for command in commands(Path(work)):
            subprocess.run(command, check=True, timeout=45)


if __name__ == '__main__':
    main()
