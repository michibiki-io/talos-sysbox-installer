#!/usr/bin/env python3
"""Generate a reviewed machine-file patch from an explicitly supplied fragment."""
import argparse
import json
import tomllib
from pathlib import Path

HANDLER = '''
[plugins."io.containerd.cri.v1.runtime".containerd.runtimes.sysbox-runc]
  runtime_type = "io.containerd.runc.v2"
  base_runtime_spec = "/etc/cri/conf.d/base-spec.json"
[plugins."io.containerd.cri.v1.runtime".containerd.runtimes.sysbox-runc.options]
  BinaryName = "/usr/local/bin/sysbox-runc"
  SystemdCgroup = false
'''


def merge(existing):
    config = tomllib.loads(existing)
    runtimes = config.get('plugins', {}).get('io.containerd.cri.v1.runtime', {}).get('containerd', {}).get('runtimes', {})
    if 'sysbox-runc' in runtimes:
        raise ValueError('sysbox-runc already exists; inspect and reconcile rather than overwrite')
    merged = existing.rstrip()+'\n'+HANDLER
    after = tomllib.loads(merged)
    # Preserve the supplied fragment, including unrelated handler options.
    added = after['plugins']['io.containerd.cri.v1.runtime']['containerd']['runtimes'].pop('sysbox-runc')
    if added['options']['SystemdCgroup'] is not False:
        raise ValueError('invalid handler')
    for name, options in runtimes.items():
        if after['plugins']['io.containerd.cri.v1.runtime']['containerd']['runtimes'][name] != options:
            raise ValueError('existing handler changed')
    return merged


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input-fragment', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    a = p.parse_args()
    content = merge(a.input_fragment.read_text())
    # JSON is accepted as a Talos machineconfig patch; no YAML dependency.
    a.output.write_text(json.dumps({'machine':{'files':[{'path':'/etc/cri/conf.d/20-customization.part','permissions':420,'op':'create','content':content}]}},indent=2)+'\n')


if __name__ == '__main__': main()
