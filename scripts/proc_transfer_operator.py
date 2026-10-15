#!/usr/bin/env python3
"""Render private manifests for an approved public-digest or local archive probe.

Offline only. Import the checksum-verified OCI archive into CRI containerd with
trusted node tooling, then apply these manifests in the approved operation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import uuid


def render(lock, node, namespace, token):
    probe = lock['test_images']['proc_transfer_probe']
    public = probe['distribution'] == 'public-registry'
    if lock['schema_version'] != 1 or probe['distribution'] not in ('public-registry', 'local-archive-import-only') or probe['anonymous_pull_verified'] is not public:
        raise ValueError('unverified probe distribution')
    if probe['bubblewrap'] != '0.12.0' or probe['uid'] != 1001 or probe['gid'] != 1001 or not re.fullmatch('sha256:[0-9a-f]{64}', probe['oci_digest']):
        raise ValueError('unsupported probe')
    common = {'nodeName': node, 'automountServiceAccountToken': False, 'restartPolicy': 'Never', 'tolerations': [{'operator': 'Exists'}]}
    image = probe['image'] if public else probe['local_tag']
    if public and (not re.fullmatch(r'ghcr.io/[a-z0-9_.-]+/talos-sysbox-proc-transfer-probe@sha256:[0-9a-f]{64}', image) or image.rsplit('@', 1)[1] != probe['oci_digest']):
        raise ValueError('public probe requires its verified digest reference')
    pull_policy = 'IfNotPresent' if public else 'Never'
    bootstrap_image = lock['test_images']['proc_transfer_bootstrap']
    if not re.fullmatch('.+@sha256:[0-9a-f]{64}', bootstrap_image):
        raise ValueError('bootstrap must use an existing public digest pin')
    actual = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'proc-transfer-probe', 'namespace': namespace},
              'spec': {**common, 'runtimeClassName': 'sysbox-runc', 'hostUsers': False,
                       'containers': [{'name': 'probe', 'image': image, 'imagePullPolicy': pull_policy,
                                       'command': ['/usr/bin/python3', '/opt/proc-transfer/probe_idle.py', token],
                                       'securityContext': {'runAsUser': 1001, 'runAsGroup': 1001, 'privileged': False,
                                         'runAsNonRoot': True, 'allowPrivilegeEscalation': False,
                                         'capabilities': {'drop': ['ALL']},
                                         'seccompProfile': {'type': 'Unconfined'}},
                                       'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '2', 'memory': '1Gi'}}}]}}
    paths = ['/usr/local', '/run/containerd']
    trusted = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'proc-transfer-launcher', 'namespace': namespace},
               'spec': {**common, 'hostPID': True, 'hostUsers': True,
                        'containers': [{'name': 'launcher', 'image': image, 'imagePullPolicy': pull_policy,
                                        'command': ['/bin/sleep', 'infinity'],
                                        'securityContext': {'runAsUser': 0, 'runAsGroup': 0, 'privileged': True},
                                        'volumeMounts': [{'name': 'trusted-' + str(i), 'mountPath': path, 'readOnly': True} for i, path in enumerate(paths)],
                                        'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '2', 'memory': '1Gi'}}}],
                        'volumes': [{'name': 'trusted-' + str(i), 'hostPath': {'path': path, 'type': 'Directory'}} for i, path in enumerate(paths)]}}
    ns = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace, 'labels': {'pod-security.kubernetes.io/enforce': 'privileged'}}}
    bootstrap = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'proc-transfer-bootstrap', 'namespace': namespace},
                 'spec': {**common, 'hostUsers': True,
                          'containers': [{'name': 'bootstrap', 'image': bootstrap_image, 'command': ['/bin/sleep', '3600'],
                                          'securityContext': {'runAsUser': 0, 'runAsGroup': 0, 'privileged': False},
                                          'volumeMounts': [{'name': 'cri', 'mountPath': '/run/containerd', 'readOnly': True}],
                                          'resources': {'limits': {'cpu': '1', 'memory': '512Mi', 'ephemeral-storage': '8Gi'}}}],
                          'volumes': [{'name': 'cri', 'hostPath': {'path': '/run/containerd', 'type': 'Directory'}}]}}
    manifests = {'namespace': ns, 'probe': actual, 'launcher': trusted}
    if not public: manifests['bootstrap'] = bootstrap
    return manifests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-image-lock', type=Path, required=True)
    parser.add_argument('--expected-node', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    lock = json.loads(args.test_image_lock.read_text())
    probe = lock['test_images']['proc_transfer_probe']
    if probe['distribution'] == 'local-archive-import-only':
        relative = Path(probe['archive'])
        if relative.is_absolute() or len(relative.parts) != 1:
            raise ValueError('invalid local probe archive path')
        archive = args.test_image_lock.parent / relative
        tools = args.test_image_lock.parent / 'tools'
        for path, sha in [(tools / 'ctr', lock['bootstrap_ctr_sha256']), (tools / 'artifacts.lock.json', lock['validation_artifacts_lock_sha256'])]:
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise ValueError('fixed-source bootstrap tooling hash mismatch')
        with archive.open('rb') as file:
            if hashlib.file_digest(file, 'sha256').hexdigest() != probe['archive_sha256']:
                raise ValueError('probe archive checksum mismatch')
    token = uuid.uuid4().hex
    namespace = 'proc-transfer-' + token[:12]
    if args.output.exists():
        parser.error('use a new private output directory')
    args.output.mkdir(parents=True, mode=0o700)
    for name, manifest in render(lock, args.expected_node, namespace, token).items():
        target = args.output / (name + '.json')
        target.write_text(json.dumps(manifest, indent=2) + '\n'); target.chmod(0o600)
    private = args.output / 'operation.json'
    private.write_text(json.dumps({'namespace': namespace, 'token': token, 'image': probe.get('image', probe.get('local_tag')),
                                   'expected_oci_digest': probe['oci_digest']}, indent=2) + '\n')
    private.chmod(0o600)
    print('Rendered private manifests; cluster operations require the separate target/image/operation approval.')

if __name__ == '__main__': main()
