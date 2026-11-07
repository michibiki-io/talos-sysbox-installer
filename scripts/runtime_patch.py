#!/usr/bin/env python3
"""Generate fresh Talos 1.14 runtime configuration without replacing CRI parts."""
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
CUSTOMIZATION_NAME = 'sysbox'


def configuration(existing, max_user_namespaces=11255):
    """Preflight the supplied CRI fragment; emit only our named addition.

    The operator must also verify the named document and handler do not already
    exist in other parts. This function never rewrites the supplied fragment.
    """
    config = tomllib.loads(existing)
    runtimes = config.get('plugins', {}).get('io.containerd.cri.v1.runtime', {}).get('containerd', {}).get('runtimes', {})
    if 'sysbox-runc' in runtimes:
        raise ValueError('sysbox-runc already exists; reconcile existing CRI customizations before registration')
    if type(max_user_namespaces) is not int or not 0 < max_user_namespaces < 2**31:
        raise ValueError('max_user_namespaces must be a positive signed 32-bit integer')
    return [
        {'apiVersion': 'v1alpha1', 'kind': 'SysctlConfig',
         'params': {'user.max_user_namespaces': str(max_user_namespaces)}},
        {'apiVersion': 'v1alpha1', 'kind': 'CRICustomizationConfig',
         'name': CUSTOMIZATION_NAME, 'content': HANDLER},
    ]


def encode_documents(documents):
    # JSON documents separated by YAML markers are accepted by Talos directly.
    return '\n---\n'.join(json.dumps(document, indent=2) for document in documents)+'\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-fragment', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--max-user-namespaces', type=int, default=11255)
    parser.add_argument('--no-existing-customization', action='store_true', required=True,
                        help='operator verified no sysbox named document or sysbox-runc handler exists in any CRI part, including legacy machine.files')
    args = parser.parse_args()
    data = encode_documents(configuration(args.input_fragment.read_text(), args.max_user_namespaces))
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(data)


if __name__ == '__main__':
    main()
