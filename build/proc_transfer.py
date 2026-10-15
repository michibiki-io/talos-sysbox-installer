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
CONTRACT = 'proc-transfer-v1-thread-pidfd'
COMPONENTS = {'root': 'sysbox', 'fs': 'sysbox-fs', 'runc': 'sysbox-runc',
              'ipc': 'sysbox-ipc', 'libs': 'sysbox-libs', 'fuse': 'sysbox-fs/bazil'}
PRODUCTION_INPUTS = {'helper/proc-mount-helper.c', 'patches/proc-transfer.patch'}
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
    for name, digest in manifest['source_sha256'].items():
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
    patch = integration / 'patches/proc-transfer.patch'
    args = ['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-i', str(patch)]
    subprocess.run([*args, '--dry-run'], cwd=root, check=True)
    subprocess.run(args, cwd=root, check=True)


def verify_pair(local, binaries):
    """Validate the actual backend payload, ownership and static paired ELFs."""
    root = local / 'lib/containers/sysbox-fs'
    for name in ('sysbox-fs', 'proc-mount-helper'):
        path = root / name
        if not path.is_file() or path.is_symlink() or sha256(path) != binaries[name]:
            raise ValueError('missing/mismatched production pair: ' + name)
        stat = path.stat()
        if stat.st_uid != 0 or stat.st_gid != 0 or stat.st_mode & 0o777 != 0o755:
            raise ValueError('production pair must be root-owned mode 0755')
        header = path.read_bytes()[:20]
        if header[:4] != b'\x7fELF' or header[4:6] != bytes([2, 1]) or int.from_bytes(header[18:20], 'little') != 62:
            raise ValueError('production pair must be amd64 ELF')
        program = subprocess.check_output(['readelf', '-l', str(path)], text=True)
        dynamic = subprocess.check_output(['readelf', '-d', str(path)], text=True)
        if 'INTERP' in program or '(NEEDED)' in dynamic:
            raise ValueError('production pair must be static')
    for forbidden in ('helper-checkpoint', 'checkpoint-requester', 'launcher.py'):
        if list(local.rglob(forbidden)):
            raise ValueError('validation artifact in production rootfs')


def verify_extension_archive(archive, metadata):
    """Assert final OCI-layer ownership, installed hashes and validation exclusion."""
    expected = {
        'rootfs/usr/local/bin/sysbox-fs': metadata['binary_sha256']['sysbox-fs'],
        'rootfs/usr/local/lib/containers/sysbox-fs/sysbox-fs': metadata['binary_sha256']['sysbox-fs'],
        'rootfs/usr/local/lib/containers/sysbox-fs/proc-mount-helper': metadata['binary_sha256']['proc-mount-helper'],
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
                    if name.startswith('rootfs/') and Path(name).name in ('helper-checkpoint', 'checkpoint-requester', 'launcher.py', 'runtime_exec.py', 'probe_idle.py'):
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
    return {'pair_sha256': metadata['proc_transfer']['output_sha256'], 'root_owned_mode_0755': True,
            'test_helpers_absent': True}
