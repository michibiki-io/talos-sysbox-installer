#!/usr/bin/env python3
"""Install checksum-locked clients in disposable state, never global paths."""
import hashlib
import json
import os
import urllib.request
from pathlib import Path

root=Path(__file__).resolve().parents[1]
lock=json.loads((root/'versions.lock.json').read_text())
destination=root/'.tmp/e2e/bin'
destination.mkdir(parents=True,exist_ok=True)
for name in ('kubectl','talosctl'):
    spec=lock['test_tools'][name]
    data=urllib.request.urlopen(spec['url'],timeout=120).read()
    if hashlib.sha256(data).hexdigest()!=spec['sha256']: raise RuntimeError('client checksum mismatch')
    path=destination/name; path.write_bytes(data); path.chmod(0o755)
if os.environ.get('GITHUB_PATH'):
    with open(os.environ['GITHUB_PATH'],'a') as stream: stream.write(str(destination)+'\n')
