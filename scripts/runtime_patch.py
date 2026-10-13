#!/usr/bin/env python3
"""Generate a reviewed machine-file patch from an explicitly supplied fragment."""
import argparse
import json
import os
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


def machine_file_patch(existing, machine_files=None):
    entry={'path':'/etc/cri/conf.d/20-customization.part','permissions':420,'op':'create','content':merge(existing)}
    if machine_files is None:
        return {'machine':{'files':[entry]}}
    matches=[i for i,item in enumerate(machine_files) if item.get('path')==entry['path']]
    if len(matches)!=1:
        raise ValueError('expected exactly one existing customization entry')
    replacement=dict(machine_files[matches[0]])
    if replacement.get('content')!=existing:
        raise ValueError('input fragment does not exactly match the existing customization entry; export and reconcile current content')
    replacement.update(content=entry['content'],op='create')
    # Talos rejects JSON6902 for multi-document configs. Its strategic delete
    # selector removes just this path before the normal merge appends the
    # replacement, within a single patch/apply operation. Other entries stay.
    return {'version':'v1alpha1','machine':{'files':[{'path':entry['path'],'$patch':'delete'},replacement]}}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input-fragment', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    state=p.add_mutually_exclusive_group(required=True)
    state.add_argument('--no-existing-machine-file',action='store_true',help='operator has verified this path has no machine.files entry')
    state.add_argument('--existing-machine-files',type=Path,help='private JSON array containing the current complete machine.files list')
    a = p.parse_args()
    files=json.loads(a.existing_machine_files.read_text()) if a.existing_machine_files else None
    patch=machine_file_patch(a.input_fragment.read_text(),files)
    # JSON is accepted as a Talos machineconfig patch; no YAML dependency.
    fd=os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as output:
        os.fchmod(output.fileno(),0o600)
        output.write(json.dumps(patch,indent=2)+'\n')


if __name__ == '__main__': main()
