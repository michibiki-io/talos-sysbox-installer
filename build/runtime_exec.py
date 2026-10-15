#!/usr/bin/python3
"""Bounded UID1001 exec; remove only its unique server-side exec on interruption."""
import os
import re
import signal
from pathlib import Path
import tempfile
import subprocess
import sys
import uuid


def execute(arguments, container, timeout=120):
    if not re.fullmatch('[0-9a-f]{64}', container) or not 1 <= timeout <= 600:
        raise ValueError('invalid owned probe or deadline')
    prefix = ['/opt/proc-transfer/ctr', '--address=/run/containerd/containerd.sock', '--namespace=k8s.io', 'tasks']
    owned = 'proc-transfer-' + uuid.uuid4().hex
    # Ignore TMPDIR: storage belongs to the launcher Pod, never a host mount.
    # One private directory per bounded exec; ctr creates only its IO FIFOs.
    with tempfile.TemporaryDirectory(prefix='proc-transfer-fifo-', dir='/tmp') as private:
        local = Path(private)
        local.chmod(0o700)
        # hostPID makes our Python PID meaningful to the daemon. Use this same
        # spelling for ctr and shim; a plain Pod /tmp path names different files.
        visible = Path('/proc') / str(os.getpid()) / 'root' / local.relative_to('/')
        if not local.samefile(visible) or local.stat().st_mode & 0o777 != 0o700:
            raise ValueError('private FIFO directory identity/mode mismatch')
        command = [*prefix, 'exec', '--fifo-dir=' + str(visible),
                   '--exec-id=' + owned, '--user=1001:1001', container, *arguments]
        try:
            return subprocess.run(command, timeout=timeout).returncode
        except BaseException:
            # ctr death does not stop its server exec. Delete exactly our ID
            # before TemporaryDirectory removes storage, including on failure.
            try:
                cleanup = subprocess.run([*prefix, 'delete', '--force', '--exec-id=' + owned, container], timeout=15)
                if cleanup.returncode not in (0, 137):
                    print('Owned exec removal not proven; owned namespace cleanup required.', file=sys.stderr)
            except BaseException:
                print('Owned exec removal failed; owned namespace cleanup required.', file=sys.stderr)
            raise


def main():
    def interrupted(signum, frame):
        raise InterruptedError('exec interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        return execute(sys.argv[1:], os.environ['PROC_TRANSFER_PROBE_CONTAINER'],
                       int(os.environ.get('PROC_TRANSFER_EXEC_TIMEOUT', '120')))
    except (subprocess.TimeoutExpired, InterruptedError):
        return 124


if __name__ == '__main__':
    raise SystemExit(main())
