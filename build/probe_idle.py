#!/usr/bin/python3
"""Identifiable dedicated probe PID 1; no background workload or provisioning."""
import os
import re
import signal
import sys
if len(sys.argv) != 2 or not re.fullmatch('[0-9a-f]{32}', sys.argv[1]) or os.getuid() != 1001 or os.getgid() != 1001:
    raise SystemExit('owned UID/GID 1001 probe token required')
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    signal.pause()
