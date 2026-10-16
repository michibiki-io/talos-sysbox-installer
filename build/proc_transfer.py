#!/usr/bin/env python3
"""Fail-closed public source integration; the lock is the sole source authority."""
import hashlib
import json
import re
import subprocess
import tarfile
import urllib.request
from pathlib import Path

REPOSITORY = 'https://github.com/michibiki-io/sysbox-proc-transfer'
CONTRACT = 'proc-sysfs-transfer-v1-thread-pidfd'
BUILD_PROFILE = 'proc-sysfs-fuse-v1'
PRODUCTION_BINARIES = ('sysbox-fs', 'proc-mount-helper', 'sysfs-mount-helper')
VALIDATION_ONLY = ('helper-checkpoint', 'checkpoint-requester', 'sysfs-checkpoint',
                   'checkpoint-sysfs', 'sysfs-failure-helper', 'thread-sysfs',
                   'launcher.py', 'runtime_exec.py', 'probe_idle.py', 'sysfs_runtime.py',
                   'sysfs-failures.py', 'sysfs-dentry-protection.py', 'sysfs_oracle.py',
                   'test_sysfs_oracle.py')
COMPONENTS = {'root': 'sysbox', 'fs': 'sysbox-fs', 'runc': 'sysbox-runc',
              'ipc': 'sysbox-ipc', 'libs': 'sysbox-libs', 'fuse': 'sysbox-fs/bazil'}
PRODUCTION_INPUTS = {'helper/proc-mount-helper.c', 'patches/proc-transfer.patch',
                     'helper/sysfs-mount-helper.c', 'patches/sysfs-transfer.patch'}
REQUIRED = {'build/sources.lock.json', 'helper/proc-mount-helper.c',
            'patches/proc-transfer.patch', 'LICENSE', 'LICENSES/Apache-2.0.txt',
            'THIRD_PARTY_NOTICES.md', 'tests/upstream/proc_policy_test.go',
            'tests/static/test_lifecycle.py', 'tests/static/test_proc_verifier.py',
            'tests/unit/proc-verifier.c', 'tests/unit/lifecycle.c',
            'tests/unit/helper-checkpoint.c', 'tests/unit/helper-liveness.c',
            'tests/runtime/verify.sh', 'tests/runtime/verify.c',
            'tests/runtime/thread-proc.c', 'tests/runtime/verify-sys.py',
            'tests/runtime/helper-rejections.py', 'tests/runtime/checkpoints.py',
            'tests/runtime/checkpoint-requester.c', 'tests/runtime/stress-proc.py'}
REQUIRED |= PRODUCTION_INPUTS | {
    'LICENSES/bazil-fuse.txt',
    'tests/runtime/checkpoint-sysfs.c',
    'tests/runtime/sysfs-dentry-protection.py',
    'tests/runtime/sysfs-failures.py',
    'tests/runtime/sysfs-interleave.py',
    'tests/runtime/sysfs-network.py',
    'tests/runtime/sysfs-write-policy.py',
    'tests/runtime/sysfs_oracle.py',
    'tests/runtime/thread-sysfs.c',
    'tests/static/test_contracts.py',
    'tests/static/test_sysfs.py',
    'tests/static/test_sysfs_oracle.py',
    'tests/unit/sysfs-checkpoint.c',
    'tests/unit/sysfs-failure-helper.c',
    'tests/unit/sysfs-liveness.c',
    'tests/upstream/fuse_container_test.go',
    'tests/upstream/fuse_identity_test.go',
    'tests/upstream/sysfs_dentry_test.go',
    'tests/upstream/sysfs_metadata_test.go',
    'tests/upstream/sysfs_policy_test.go',
    'tests/upstream/sysfs_snapshot_test.go',
}

SYSFS_PATCH_PATHS = {
    'sysbox-fs/bazil/fs/serve.go',
    'sysbox-fs/domain/sysfs.go',
    'sysbox-fs/fuse/dir.go',
    'sysbox-fs/fuse/file.go',
    'sysbox-fs/fuse/server.go',
    'sysbox-fs/fuse/service.go',
    'sysbox-fs/handler/implementations/passThrough.go',
    'sysbox-fs/handler/implementations/sysDevicesVirtual.go',
    'sysbox-fs/handler/implementations/sysfsNetwork.go',
    'sysbox-fs/mount/infoParser.go',
    'sysbox-fs/mount/service.go',
    'sysbox-fs/seccomp/mount.go',
    'sysbox-fs/seccomp/sysfs_transfer.go',
    'sysbox-fs/state/container.go',
    'sysbox-fs/state/containerDB.go',
    'sysbox-fs/sysfstransfer/transfer.go',
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive_url(revision):
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('integration revision must be a full immutable commit')
    return REPOSITORY.replace('https://github.com/', 'https://codeload.github.com/') + '/tar.gz/' + revision


def load_lock(path):
    lock = json.loads(path.read_text())
    if set(lock) != {'schema_version', 'contract', 'repository', 'revision', 'url', 'sha256', 'input_sha256'}:
        raise ValueError('unsupported integration lock fields')
    if type(lock['schema_version']) is not int or lock['schema_version'] != 1 or lock['contract'] != CONTRACT or lock['repository'] != REPOSITORY:
        raise ValueError('unsupported proc-transfer contract/repository')
    if lock['url'] != archive_url(lock['revision']):
        raise ValueError('integration archive URL does not match immutable revision')
    if not REQUIRED <= set(lock['input_sha256']):
        raise ValueError('missing required integration input')
    if {name for name in lock['input_sha256'] if name.startswith(('helper/', 'patches/'))} != PRODUCTION_INPUTS:
        raise ValueError('unsupported production input inventory')
    for name, digest in [('archive', lock['sha256']), *lock['input_sha256'].items()]:
        if not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid input digest: ' + name)
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('unsafe integration input path')
    return lock


def unpack(archive, destination):
    """Accept only normal public archive files; no links, traversal or duplicates."""
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as tf:
        prefix = None
        seen = set()
        for member in tf:
            parts = Path(member.name).parts
            if not parts or Path(member.name).is_absolute() or '..' in parts:
                raise ValueError('unsafe integration archive path')
            if prefix is None:
                prefix = parts[0]
            if parts[0] != prefix:
                raise ValueError('multiple archive roots')
            if len(parts) == 1 and member.isdir():
                continue
            if not (member.isfile() or member.isdir()) or len(parts) < 2:
                raise ValueError('unsupported integration archive member')
            name = str(Path(*parts[1:]))
            if name in seen:
                raise ValueError('duplicate integration archive member')
            seen.add(name)
            path = destination / name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tf.extractfile(member).read())
                path.chmod(0o755 if member.mode & 0o111 else 0o644)


def inventory(root):
    return {str(p.relative_to(root)): sha256(p) for p in sorted(root.rglob('*')) if p.is_file()}


def verify_manifest(root, versions):
    manifest = json.loads((root / 'build/sources.lock.json').read_text())
    if set(manifest) != {'schema_version', 'upstream_tag', 'components', 'builder', 'generator', 'source_sha256'}:
        raise ValueError('unsupported upstream manifest fields')
    if manifest['generator'] != 'github.com/golang/protobuf/protoc-gen-go@v1.3.5' or manifest['builder'] != 'golang:1.27.2-trixie@sha256:47667934874913a449b96faa7ef4777fa285d9ec29126ee80ed61c45b7cccf0e':
        raise ValueError('unsupported upstream build contract; review adapter before updating')
    if type(manifest['schema_version']) is not int or manifest['schema_version'] != 1 or manifest['upstream_tag'] != 'v' + versions['sysbox'] or set(manifest['components']) != set(COMPONENTS):
        raise ValueError('unsupported upstream source manifest')
    for name, component in COMPONENTS.items():
        spec = manifest['components'][name]
        downstream = versions['sources']['sysbox'] if name == 'root' else versions['sysbox_components'][component]
        repository = 'fuse' if name == 'fuse' else component
        if spec != {'repository': 'https://github.com/nestybox/' + repository + '.git', 'revision': downstream['commit']}:
            raise ValueError('Sysbox component revision/repository mismatch: ' + name)
    if not PRODUCTION_INPUTS <= set(manifest['source_sha256']):
        raise ValueError('upstream manifest lacks production input hashes')
    files = inventory(root)
    production = {name for name in files if name.startswith(('helper/', 'patches/'))}
    declared = {name for name in manifest['source_sha256'] if name.startswith(('helper/', 'patches/'))}
    if production != PRODUCTION_INPUTS or declared != PRODUCTION_INPUTS:
        raise ValueError('unsupported production input inventory')
    for name, digest in manifest['source_sha256'].items():
        if name not in PRODUCTION_INPUTS and not name.startswith(('tests/', 'docs/')):
            raise ValueError('unsupported production input: ' + name)
        if not re.fullmatch('[0-9a-f]{64}', digest) or name not in files or files[name] != digest:
            raise ValueError('upstream source manifest hash mismatch: ' + name)
    # This adapter supports the reviewed FD/argument ABI only. A new upstream
    # contract requires code review, not just regeneration of checksums.
    helper = (root / 'helper/proc-mount-helper.c').read_text()
    patch = (root / 'patches/proc-transfer.patch').read_text()
    for required in ('10 requester thread pidfd', 'trusted parent-user launcher', 'supported proc flags'):
        if required not in helper:
            raise ValueError('unsupported helper contract')
    for required in ('const procPidfdThread = 0x80', 'SYSBOX_PROC_TRANSFER_HELPER', 'WithChildReaperGuard', 'NotifIDValid'):
        if required not in patch:
            raise ValueError('unsupported patch contract')
    changed = re.findall(r'^\+\+\+ b/(.+)$', patch, re.M)
    allowed = {'sysbox-fs/nsenter/event.go', 'sysbox-fs/nsenter/eventService.go',
               'sysbox-fs/seccomp/mount.go', 'sysbox-fs/seccomp/proc_transfer.go',
               'sysbox-fs/seccomp/tracer.go', 'sysbox-runc/libcontainer/nsenter/nsexec.c'}
    if set(changed) != allowed:
        raise ValueError('unsupported patch scope')
    sysfs = (root / 'patches/sysfs-transfer.patch').read_text()
    if set(re.findall(r'^\+\+\+ b/(.+)$', sysfs, re.M)) != SYSFS_PATCH_PATHS:
        raise ValueError('unsupported sysfs patch scope')
    for required in ('SYSBOX_SYSFS_TRANSFER_HELPER', 'CaptureSysfsPolicy', 'WithChildReaperGuard'):
        if required not in sysfs:
            raise ValueError('unsupported sysfs patch contract')
    helper = (root / 'helper/sysfs-mount-helper.c').read_text()
    for required in ('sysfs-transfer:', '7 requester Network', '10 requester thread pidfd', '200/201', 'supported sysfs flags', '/tmp'):
        if required not in helper:
            raise ValueError('unsupported sysfs helper contract')
    return manifest


def verify_production_inputs(root, baseline):
    """Candidate updates may refresh tests, never silently change production."""
    for name in sorted(PRODUCTION_INPUTS):
        if sha256(root / name) != baseline['input_sha256'][name]:
            raise ValueError('candidate changes reviewed production input: ' + name)


def fetch(lock, archive, root, versions):
    data = urllib.request.urlopen(lock['url'], timeout=120).read()
    if hashlib.sha256(data).hexdigest() != lock['sha256']:
        raise ValueError('integration archive checksum mismatch')
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(data)
    unpack(archive, root)
    if inventory(root) != lock['input_sha256']:
        raise ValueError('integration file inventory/hash mismatch')
    return verify_manifest(root, versions)


def apply_patch(root, integration):
    for name in ('proc-transfer.patch', 'sysfs-transfer.patch'):
        patch = integration / 'patches' / name
        args = ['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-i', str(patch)]
        subprocess.run([*args, '--dry-run'], cwd=root, check=True)
        subprocess.run(args, cwd=root, check=True)


def verify_pair(local, binaries):
    """Validate the actual backend payload, ownership and static matched ELFs."""
    root = local / 'lib/containers/sysbox-fs'
    for name in PRODUCTION_BINARIES:
        path = root / name
        if not path.is_file() or path.is_symlink() or sha256(path) != binaries[name]:
            raise ValueError('missing/mismatched production triple: ' + name)
        stat = path.stat()
        if stat.st_uid != 0 or stat.st_gid != 0 or stat.st_mode & 0o777 != 0o755:
            raise ValueError('production triple must be root-owned mode 0755')
        header = path.read_bytes()[:20]
        if header[:4] != b'\x7fELF' or header[4:6] != bytes([2, 1]) or int.from_bytes(header[18:20], 'little') != 62:
            raise ValueError('production triple must be amd64 ELF')
        program = subprocess.check_output(['readelf', '-l', str(path)], text=True)
        dynamic = subprocess.check_output(['readelf', '-d', str(path)], text=True)
        if 'INTERP' in program or '(NEEDED)' in dynamic:
            raise ValueError('production triple must be static')
    for forbidden in VALIDATION_ONLY:
        if list(local.rglob(forbidden)):
            raise ValueError('validation artifact in production rootfs')


def verify_extension_archive(archive, metadata):
    """Assert final OCI-layer ownership, installed hashes and validation exclusion."""
    if metadata['proc_transfer'].get('build_profile') != BUILD_PROFILE:
        raise ValueError('production build profile mismatch')
    outputs = metadata['proc_transfer']['output_sha256']
    if set(outputs) != set(PRODUCTION_BINARIES) or any(outputs[name] != metadata['binary_sha256'][name] for name in outputs):
        raise ValueError('production triple inventory mismatch')
    expected = {
        'rootfs/usr/local/bin/sysbox-fs': metadata['binary_sha256']['sysbox-fs'],
        'rootfs/usr/local/lib/containers/sysbox-fs/sysbox-fs': metadata['binary_sha256']['sysbox-fs'],
        'rootfs/usr/local/lib/containers/sysbox-fs/proc-mount-helper': metadata['binary_sha256']['proc-mount-helper'],
        'rootfs/usr/local/lib/containers/sysbox-fs/sysfs-mount-helper': metadata['binary_sha256']['sysfs-mount-helper'],
        'rootfs/usr/local/lib/containers/sysbox-fs/supervisor': metadata['supervisor_sha256'],
    }
    files = {}
    links = {}
    with tarfile.open(archive) as outer:
        entry, = json.load(outer.extractfile('manifest.json'))
        for layer in entry['Layers']:
            with tarfile.open(fileobj=outer.extractfile(layer)) as tf:
                for member in tf:
                    name = member.name.removeprefix('./')
                    if Path(name).name.startswith('.wh.'):
                        raise ValueError('unexpected whiteout in scratch extension image')
                    if name.startswith('rootfs/') and Path(name).name in VALIDATION_ONLY:
                        raise ValueError('test-only artifact in extension image')
                    if name in expected:
                        if member.uid != 0 or member.gid != 0 or member.mode != 0o755:
                            raise ValueError('incorrect image payload ownership/mode')
                        if member.isfile():
                            files[name] = hashlib.sha256(tf.extractfile(member).read()).hexdigest()
                        elif member.islnk():
                            links[name] = member.linkname.removeprefix('./')
                        else:
                            raise ValueError('image payload must be regular executable')
        for name, target in links.items():
            if target not in files:
                raise ValueError('unresolved image payload hardlink')
            files[name] = files[target]
    if files != expected:
        raise ValueError('extension image does not contain the matched production payload')
    return {'build_profile': BUILD_PROFILE, 'production_sha256': metadata['proc_transfer']['output_sha256'], 'root_owned_mode_0755': True,
            'test_helpers_absent': True}
