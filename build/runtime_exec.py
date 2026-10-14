#!/usr/bin/python3
"""Bounded UID1001 exec; remove only its unique server-side exec on interruption."""
import os
import re
import signal
import subprocess
import sys
import uuid


def execute(arguments, container, timeout=120):
    if not re.fullmatch('[0-9a-f]{64}', container) or not 1 <= timeout <= 600:
        raise ValueError('invalid owned probe or deadline')
    prefix = ['/opt/proc-transfer/ctr', '--address=/run/containerd/containerd.sock', '--namespace=k8s.io', 'tasks']
    owned = 'proc-transfer-' + uuid.uuid4().hex
    command = [*prefix, 'exec', '--exec-id=' + owned, '--user=1001:1001', container, *arguments]
    try:
        return subprocess.run(command, timeout=timeout).returncode
    except BaseException:
        # ctr client death alone does not stop the server-side process. This
        # pinned ctr's delete --force loads/deletes exactly this exec ID and
        # reports its exit code (137 after SIGKILL), never deletes the task.
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
