#!/usr/bin/python3
"""Trusted node-context verification of a dedicated, uniquely tagged probe only.

Requires Python >=3.12, initial-user root, host PID/proc access, CAP_SYS_ADMIN,
CAP_SYS_PTRACE, CAP_SETUID/GID, CAP_SYS_CHROOT and access to CRI/system containerd.
No daemon changes, package installation or seccomp disabling. Raw subprocess
output stays in a private local log; the result contains no target identifiers.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import traceback
import time

BASE = Path('/opt/proc-transfer')
CONTRACT = 'proc-sysfs-transfer-v1-thread-pidfd'
BUILD_PROFILE = 'proc-sysfs-fuse-v1'
PRODUCTION_BINARIES = ('sysbox-fs', 'proc-mount-helper', 'sysfs-mount-helper')
VALIDATION_BINARIES = ('ctr', 'helper-checkpoint', 'checkpoint-requester', 'sysfs-checkpoint', 'checkpoint-sysfs', 'thread-sysfs', 'sysfs-failure-helper')
REQUIRED_ARTIFACTS = {'launcher.py', 'runtime_exec.py', 'probe_idle.py', 'ctr',
                      *VALIDATION_BINARIES, 'sysfs_runtime.py', 'upstream-results.json',
                      *('runtime/' + name for name in ('verify.sh', 'verify.c', 'thread-proc.c',
                        'verify-sys.py', 'helper-rejections.py', 'checkpoints.py',
                        'checkpoint-requester.c', 'stress-proc.py', 'thread-sysfs.c', 'checkpoint-sysfs.c',
                        'sysfs-network.py', 'sysfs-interleave.py', 'sysfs-write-policy.py',
                        'sysfs-failures.py', 'sysfs-dentry-protection.py')),
                      'oracle/tests/runtime/sysfs-network.py', 'oracle/tests/runtime/sysfs-interleave.py'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mapped(path, identifier):
    for line in path.read_text().splitlines():
        inside, outside, count = map(int, line.split())
        if inside <= identifier < inside + count:
            if outside == 0:
                raise ValueError('probe must have a noninitial user mapping')
            return outside + identifier - inside
    raise ValueError('probe caller has no mapping')


def task_pid(address, namespace, container):
    data = subprocess.check_output(['/opt/proc-transfer/ctr', '--address=' + address,
                                    '--namespace=' + namespace, 'tasks', 'list'], text=True, timeout=20)
    rows = [line.split() for line in data.splitlines()[1:] if line.split() and line.split()[0] == container]
    if len(rows) != 1 or rows[0][2] != 'RUNNING':
        raise ValueError('owned task is not running')
    return int(rows[0][1])


def probe_processes(container):
    data = subprocess.check_output([str(BASE / 'ctr'), '--address=/run/containerd/containerd.sock',
                                    '--namespace=k8s.io', 'tasks', 'ps', container], text=True, timeout=10)
    rows = data.splitlines()
    if not rows or rows[0].split() != ['PID', 'INFO']:
        raise ValueError('cannot prove owned probe process inventory')
    return {int(row.split()[0]) for row in rows[1:] if row.split()}


def verify_exec_cleanup(container, outer):
    deadline = time.monotonic() + 10
    while True:
        if probe_processes(container) == {outer}:
            return
        if time.monotonic() >= deadline:
            raise ValueError('owned probe descendants remain after runtime checks')
        time.sleep(0.2)


def verify_artifacts(base):
    lock = json.loads((base / 'artifacts.lock.json').read_text())
    if (type(lock['schema_version']) is not int or lock['schema_version'] != 1 or
            lock['architecture'] != 'amd64' or lock.get('contract') != CONTRACT or
            lock.get('launcher_python_minimum') != '3.12' or lock.get('build_profile') != BUILD_PROFILE or
            not re.fullmatch('[0-9a-f]{40}', lock.get('revision', '')) or
            not re.fullmatch('[0-9a-f]{64}', lock.get('source_lock_sha256', '')) or
            not REQUIRED_ARTIFACTS <= set(lock['artifact_sha256'])):
        raise ValueError('unsupported validation artifacts')
    for name, sha in lock['artifact_sha256'].items():
        relative = Path(name)
        if (relative.is_absolute() or '..' in relative.parts or not re.fullmatch('[0-9a-f]{64}', sha)
                or (base / relative).is_symlink() or digest(base / relative) != sha):
            raise ValueError('validation artifact hash mismatch')
    for name in VALIDATION_BINARIES:
        header = (base / name).read_bytes()[:20]
        if header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != 62:
            raise ValueError('validation executable ABI must be amd64 ELF')
    return lock


def verify_backend(backend, metadata, host, proc_root=Path('/proc')):
    if digest(proc_root / str(backend) / 'exe') != metadata['binary_sha256']['sysbox-fs']:
        raise ValueError('running fs executable differs from downstream build')
    environment = (proc_root / str(backend) / 'environ').read_bytes().split(b'\0')
    for filesystem in ('proc', 'sysfs'):
        key = ('SYSBOX_' + filesystem.upper() + '_TRANSFER_HELPER=').encode()
        name = filesystem + '-mount-helper'
        if [value for value in environment if value.startswith(key)] != [key + ('/' + name).encode()]:
            raise ValueError('actual backend helper environment mismatch')
        backend_helper = proc_root / str(backend) / 'root' / name
        helper_stat = backend_helper.stat()
        if (backend_helper.is_symlink() or helper_stat.st_uid != 0 or helper_stat.st_gid != 0
                or helper_stat.st_mode & 0o777 != 0o755
                or digest(backend_helper) != metadata['binary_sha256'][name]):
            raise ValueError('actual backend helper path mismatch')
    if os.readlink(proc_root / str(backend) / 'ns/user') != host['user']:
        raise ValueError('fs backend must use the trusted initial User namespace')
    if os.readlink(proc_root / str(backend) / 'ns/pid') != host['pid']:
        raise ValueError('fs backend must join node PID namespace')


def trusted_host_namespaces(proc_root=Path('/proc')):
    host = {name: os.readlink(proc_root / '1/ns' / name) for name in ('pid', 'user')}
    if any(os.readlink(proc_root / 'self/ns' / name) != host[name] for name in host):
        raise ValueError('launcher must independently observe node PID and initial User namespaces')
    if (proc_root / 'self/uid_map').read_text().split() != ['0', '0', '4294967295']:
        raise ValueError('launcher requires the initial User namespace')
    return host


def run(args):
    if sys.version_info < (3, 12) or os.geteuid() != 0 or os.uname().machine != 'x86_64':
        raise ValueError('requires trusted amd64 root and Python >=3.12')
    host = trusted_host_namespaces()
    artifacts = verify_artifacts(BASE)
    image_lock = json.loads(args.image_lock.read_text())
    if digest(BASE / 'artifacts.lock.json') != image_lock['validation_artifacts_lock_sha256']:
        raise ValueError('validation artifact inventory is not anchored by image lock')
    probe_image = image_lock['test_images']['proc_transfer_probe']
    image_versions = (BASE / 'image-versions.json').read_bytes()
    if (image_lock['schema_version'] != 1 or image_lock.get('build_profile') != BUILD_PROFILE or
            image_lock['source_lock_sha256'] != artifacts['source_lock_sha256'] or
            hashlib.sha256(image_versions).hexdigest() != image_lock['image_versions_sha256']):
        raise ValueError('validation image/source record mismatch')
    public = probe_image['distribution'] == 'public-registry'
    if probe_image['distribution'] not in ('public-registry', 'local-archive-import-only') or probe_image['anonymous_pull_verified'] is not public:
        raise ValueError('unverified probe distribution')
    image_ref = probe_image['image'] if public else probe_image['local_tag']
    if public and (not re.fullmatch(r'ghcr.io/[a-z0-9_.-]+/talos-sysbox-proc-transfer-probe@sha256:[0-9a-f]{64}', image_ref) or image_ref.rsplit('@', 1)[1] != probe_image['oci_digest']):
        raise ValueError('public probe identity mismatch')
    images = subprocess.check_output([str(BASE / 'ctr'), '--address=/run/containerd/containerd.sock',
                                      '--namespace=k8s.io', 'images', 'list'], text=True, timeout=20)
    imported = [line.split() for line in images.splitlines()[1:] if line.split() and line.split()[0] == image_ref]
    if len(imported) != 1 or imported[0][2] != probe_image['oci_digest']:
        raise ValueError('imported probe OCI digest mismatch')
    container = json.loads(subprocess.check_output([str(BASE / 'ctr'), '--address=/run/containerd/containerd.sock',
                           '--namespace=k8s.io', 'containers', 'info', args.container], timeout=20))
    image = container.get('Image', container.get('image'))
    allowed = {image_ref, probe_image['config_digest']}
    if not public: allowed.add(image_ref.rsplit(':', 1)[0] + '@' + probe_image['oci_digest'])
    image_rows = [line.split() for line in images.splitlines()[1:] if line.split() and line.split()[0] == image]
    if image not in allowed or len(image_rows) != 1 or image_rows[0][2] != probe_image['oci_digest']:
        raise ValueError('owned probe is not using the locked imported image')
    metadata = json.loads(args.metadata.read_text())
    if metadata['proc_transfer'].get('build_profile') != BUILD_PROFILE:
        raise ValueError('validation/production build profile mismatch')
    if metadata['build_mode'] != 'release' or artifacts['revision'] != metadata['proc_transfer']['lock']['revision']:
        raise ValueError('validation/production revision mismatch')
    outputs = metadata['proc_transfer'].get('output_sha256', {})
    if set(outputs) != set(PRODUCTION_BINARIES) or any(outputs[name] != metadata['binary_sha256'][name] for name in outputs):
        raise ValueError('production triple inventory mismatch')
    source_digest = metadata['source_inputs_sha256']['build/proc-transfer.lock.json']
    if source_digest != artifacts['source_lock_sha256']:
        raise ValueError('validation/production source lock mismatch')
    local = Path('/usr/local')
    for name in PRODUCTION_BINARIES:
        path = local / 'lib/containers/sysbox-fs' / name
        st = path.stat()
        if path.is_symlink() or st.st_uid != 0 or st.st_gid != 0 or st.st_mode & 0o777 != 0o755 or digest(path) != metadata['binary_sha256'][name]:
            raise ValueError('installed triple hash/ownership mismatch')
    for name in ('sysbox-runc', 'sysbox-mgr', 'sysbox-fs'):
        if digest(local / 'bin' / name) != metadata['binary_sha256'][name]:
            raise ValueError('installed runtime front path mismatch')
    for daemon in ('sysbox-mgr', 'sysbox-fs'):
        if digest(local / 'lib/containers' / daemon / 'supervisor') != metadata['supervisor_sha256']:
            raise ValueError('installed supervisor mismatch')
    backend = task_pid('/proc/1/root/system/run/containerd/containerd.sock', 'talos-sysbox', 'sysbox-fs')
    verify_backend(backend, metadata, host)
    outer = task_pid('/run/containerd/containerd.sock', 'k8s.io', args.container)
    pin = os.pidfd_open(outer)
    try:
        proc = Path('/proc') / str(outer)
        expected = [b'/usr/bin/python3', b'/opt/proc-transfer/probe_idle.py', args.token.encode()]
        if proc.joinpath('cmdline').read_bytes().rstrip(b'\0').split(b'\0') != expected:
            raise ValueError('refusing an unrelated workload')
        if os.readlink(proc / 'ns/user') == host['user'] or os.readlink(proc / 'ns/pid') == host['pid']:
            raise ValueError('probe namespace isolation missing')
        for name, sha in json.loads(image_versions)['image_binary_sha256'].items():
            relative = Path(name)
            if not relative.is_absolute() or '..' in relative.parts or digest(proc / 'root' / str(relative).lstrip('/')) != sha:
                raise ValueError('probe binary differs from fixed image build')
        for name, sha in artifacts['artifact_sha256'].items():
            if name.startswith('oracle/'):
                path = proc / 'root/tmp/issue5-repo' / name.removeprefix('oracle/')
                if path.is_symlink() or digest(path) != sha:
                    raise ValueError('self-contained upstream oracle mismatch')
        uid, gid = mapped(proc / 'uid_map', 1001), mapped(proc / 'gid_map', 1001)
        status = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines() if ':' in line)
        if int(status['Uid'].split()[1]) != uid or int(status['Gid'].split()[1]) != gid or int(status['Seccomp']) != 2 or int(status['CapEff'], 16) != 0 or int(status['NoNewPrivs']) != 1:
            raise ValueError('probe credentials or production seccomp missing')
        if probe_processes(args.container) != {outer}:
            raise ValueError('dedicated probe contains unexpected processes before verification')
        result = {'architecture': 'amd64', 'kernel': os.uname().release,
                  'revision': artifacts['revision'], 'runtime_binary_sha256': metadata['proc_transfer']['output_sha256'],
                  'build_profile': BUILD_PROFILE, 'validation_image_oci_digest': probe_image['oci_digest'],
                  'unrun': {'failure_helper_injection': 'Requires separately approved disposable daemon; installed consumer is never reconfigured',
                            'complete_policy_dentry': 'Requires disposable container with masks, RO paths and nested protection before registration',
                            'outer_mapped_root_write_policy': 'Requires separately registered disposable mapped-root container'},
                  'probe_security_context': {'uid': 1001, 'gid': 1001, 'runAsNonRoot': True,
                    'allowPrivilegeEscalation': False, 'capabilities_drop': ['ALL'],
                    'kubernetes_outer_seccomp': 'Unconfined', 'sysbox_seccomp': 2,
                    'CapEff': 0, 'NoNewPrivs': 1}, 'versions': {}, 'checks': {}, 'cleanup': 'unrun', 'namespace_cleanup': 'unrun', 'passed': False}
        args.log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = dict(os.environ, PROC_TRANSFER_PROBE_CONTAINER=args.container)
        prefix = ['/usr/bin/python3', str(BASE / 'runtime_exec.py')]
        with args.log.open('wb') as log:
            args.log.chmod(0o600)
            def check(label, command, timeout=150, exec_timeout=120):
                if select.select([pin], [], [], 0)[0]:
                    raise ValueError('owned probe exited')
                process = subprocess.Popen(command, stdout=log, stderr=log, env=dict(env, PROC_TRANSFER_EXEC_TIMEOUT=str(exec_timeout)))
                try:
                    status = process.wait(timeout=timeout)
                except BaseException:
                    process.terminate()
                    try:
                        process.wait(timeout=25)  # Allow exec wrapper's bounded server cleanup.
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)
                    result['checks'][label] = 'failed'
                    raise
                result['checks'][label] = 'passed' if status == 0 else 'failed'
                if status:
                    raise RuntimeError('runtime check failed: ' + label)
            try:
                version = subprocess.check_output([*prefix, 'bwrap', '--version'], env=dict(env, PROC_TRANSFER_EXEC_TIMEOUT='10'), timeout=35, text=True).strip()
                if version != 'bubblewrap 0.12.0':
                    raise ValueError('original reproduction requires bubblewrap 0.12.0')
                result['versions']['bubblewrap'] = version
                result['versions']['launcher_python'] = sys.version.split()[0]
                for tool, command in [('probe_python', ['python3', '--version']), ('probe_cc', ['cc', '--version'])]:
                    result['versions'][tool] = subprocess.check_output([*prefix, *command], env=dict(env, PROC_TRANSFER_EXEC_TIMEOUT='10'), text=True, timeout=35).splitlines()[0]
                check('actual_sysbox_original_protections_sys_nonleader', [*prefix, 'bash', str(BASE / 'runtime/verify.sh'), host['pid'], host['user']])
                check('actual_sysfs_network_fuse_mappedroot_nonleader', [*prefix, 'python3', str(BASE / 'sysfs_runtime.py')], 575, 550)
                check('concurrent_mounts_reaping', [*prefix, 'python3', str(BASE / 'runtime/stress-proc.py')], 345, 300)
                # Only the direct-helper fixtures enter via trusted nsenter to
                # avoid an inherited notification listener. The actual-path
                # verification above executes through the production runtime.
                direct = ['nsenter', '--target', str(outer), '--user', '--mount', '--pid', '--net', '--ipc', '--uts', '--cgroup', '--root', '--wd',
                          '--setuid', '1001', '--setgid', '1001', '/usr/bin/env', 'PATH=/usr/bin:/bin']
                common = ['--outer-pid', str(outer), '--uid', '1001', '--gid', '1001']
                for filesystem in ('proc', 'sysfs'):
                    modes = ['wrong-owner', 'dead-requester', 'unsupported-flags', 'mapped-launcher']
                    if filesystem == 'sysfs': modes += ['wrong-network', 'deeper-user', 'backing-symlink']
                    for mode in modes:
                        check(filesystem + '_reject_' + mode, ['python3', str(BASE / 'runtime/helper-rejections.py'), *common,
                              '--filesystem', filesystem, '--helper', '/usr/local/lib/containers/sysbox-fs/' + filesystem + '-mount-helper',
                              '--mode', mode, '--exec', *direct])
                    helper, requester = (('helper-checkpoint', 'checkpoint-requester') if filesystem == 'proc'
                                         else ('sysfs-checkpoint', 'checkpoint-sysfs'))
                    for kind in ('leader', 'nonleader'):
                        for stage in ('launch', 'staging', 'transfer', 'commit'):
                            for action in ('interrupt', 'thread-exit', 'process-exit', 'exec'):
                                label = '_'.join((filesystem, 'checkpoint', kind, stage, action))
                                check(label, ['python3', str(BASE / 'runtime/checkpoints.py'), *common,
                                      '--filesystem', filesystem, '--helper', str(BASE / helper), '--requester', str(BASE / requester),
                                      '--requester-kind', kind, '--stage', stage, '--action', action, '--exec', *direct])
                result['cleanup'] = 'failed'
                verify_exec_cleanup(args.container, outer)
                result['checks']['owned_probe_process_cleanup'] = 'passed'
                result['cleanup'] = 'passed'  # Upstream fixture assertions plus independent process inventory.
                result['passed'] = True
            finally:
                args.result.parent.mkdir(parents=True, exist_ok=True)
                args.result.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    finally:
        os.close(pin)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', required=True)
    parser.add_argument('--token', required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--image-lock', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{64}', args.container) or not re.fullmatch('[0-9a-f]{32}', args.token):
        parser.error('dedicated container ID and probe token required')
    args.result.unlink(missing_ok=True)
    try:
        run(args)
    except Exception as error:
        args.log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with args.log.open('ab') as log:
            args.log.chmod(0o600)
            log.write(traceback.format_exc().encode())
        if not args.result.exists():
            args.result.parent.mkdir(parents=True, exist_ok=True)
            args.result.write_text(json.dumps({'passed': False, 'cleanup': 'unrun',
                                   'checks': {'preflight': 'failed'}, 'error_type': type(error).__name__}) + '\n')
        raise

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Proc-transfer verification failed (' + type(error).__name__ + '); inspect private diagnostics.', file=sys.stderr)
        raise SystemExit(1)
