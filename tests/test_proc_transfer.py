"""Integration regressions without a runtime, privileged mounts or network."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'build'))
import proc_transfer as p


class Integration(unittest.TestCase):
    def setUp(self):
        self.lock = p.load_lock(ROOT / 'build/proc-transfer.lock.json')
        self.versions = json.loads((ROOT / 'versions.lock.json').read_text())

    def load(self, lock):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'lock.json'
            path.write_text(json.dumps(lock))
            return p.load_lock(path)

    def test_lock_rejects_floating_repository_url_schema_and_missing_inputs(self):
        changes = [('revision', 'main'), ('repository', 'https://example.com/alternate'),
                   ('url', self.lock['url'] + '?branch=main'), ('schema_version', 2), ('schema_version', True),
                   ('contract', 'future-sysfs'), ('sha256', 'invalid')]
        for name, value in changes:
            with self.subTest(name=name):
                changed = copy.deepcopy(self.lock); changed[name] = value
                with self.assertRaises(ValueError): self.load(changed)
        changed = copy.deepcopy(self.lock)
        del changed['input_sha256']['helper/proc-mount-helper.c']
        with self.assertRaises(ValueError): self.load(changed)
        changed = copy.deepcopy(self.lock); changed['input_sha256']['../escape'] = 'a' * 64
        with self.assertRaises(ValueError): self.load(changed)

    def test_archive_mismatch_stops_before_extraction(self):
        with tempfile.TemporaryDirectory() as work, mock.patch.object(p.urllib.request, 'urlopen') as response:
            response.return_value.read.return_value = b'bad archive'
            with self.assertRaisesRegex(ValueError, 'archive checksum mismatch'):
                p.fetch(self.lock, Path(work) / 'source.tar', Path(work) / 'source', self.versions)
            self.assertFalse((Path(work) / 'source').exists())

    def test_archive_rejects_links_traversal_and_duplicates(self):
        for kind in ('symlink', 'traversal', 'duplicate'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as work:
                archive = Path(work) / 'source.tar'
                with tarfile.open(archive, 'w') as tf:
                    member = tarfile.TarInfo('root/../escape' if kind == 'traversal' else 'root/file')
                    if kind == 'symlink':
                        member.type = tarfile.SYMTYPE; member.linkname = '/escape'; tf.addfile(member)
                    else:
                        member.size = 1; tf.addfile(member, io.BytesIO(b'x'))
                        if kind == 'duplicate': tf.addfile(member, io.BytesIO(b'y'))
                with self.assertRaises(ValueError): p.unpack(archive, Path(work) / 'source')

    def fixture(self, work):
        root = Path(work)
        manifest = {'schema_version': 1, 'upstream_tag': 'v0.7.1',
                    'components': {name: {'repository': 'https://github.com/nestybox/' + component + '.git',
                          'revision': (self.versions['sources']['sysbox'] if name == 'root' else self.versions['sysbox_components'][component])['commit']}
                         for name, component in p.COMPONENTS.items()},
                    'builder': 'golang:1.27.2-trixie@sha256:47667934874913a449b96faa7ef4777fa285d9ec29126ee80ed61c45b7cccf0e',
                    'generator': 'github.com/golang/protobuf/protoc-gen-go@v1.3.5', 'source_sha256': {}}
        for name in ('build', 'helper', 'patches'): (root / name).mkdir()
        (root / 'helper/proc-mount-helper.c').write_text('10 requester thread pidfd; trusted parent-user launcher; supported proc flags')
        changed = ('sysbox-fs/nsenter/event.go', 'sysbox-fs/nsenter/eventService.go', 'sysbox-fs/seccomp/mount.go',
                   'sysbox-fs/seccomp/proc_transfer.go', 'sysbox-fs/seccomp/tracer.go', 'sysbox-runc/libcontainer/nsenter/nsexec.c')
        (root / 'patches/proc-transfer.patch').write_text('\n'.join('+++ b/' + name for name in changed) + '\nconst procPidfdThread = 0x80\nSYSBOX_PROC_TRANSFER_HELPER WithChildReaperGuard NotifIDValid')
        manifest['source_sha256'] = {name: p.sha256(root / name) for name in ('helper/proc-mount-helper.c', 'patches/proc-transfer.patch')}
        (root / 'build/sources.lock.json').write_text(json.dumps(manifest))
        return manifest

    def test_manifest_revisions_hashes_contract_and_patch_scope(self):
        for failure in ('revision', 'hash', 'schema', 'generator', 'scope', 'helper'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as work:
                root = Path(work); manifest = self.fixture(work)
                p.verify_manifest(root, self.versions)
                if failure == 'revision': manifest['components']['fs']['revision'] = 'a' * 40
                if failure == 'hash': manifest['source_sha256']['helper/proc-mount-helper.c'] = 'b' * 64
                if failure == 'schema': manifest['schema_version'] = 2
                if failure == 'generator': manifest['generator'] = 'future'
                if failure == 'scope':
                    patch = root / 'patches/proc-transfer.patch'
                    patch.write_text(patch.read_text().replace('sysbox-fs/nsenter/event.go', 'sysbox-mgr/main.go'))
                if failure == 'helper': (root / 'helper/proc-mount-helper.c').write_text('unsupported')
                if failure in ('scope', 'helper'):
                    manifest['source_sha256'] = {name: p.sha256(root / name) for name in manifest['source_sha256']}
                (root / 'build/sources.lock.json').write_text(json.dumps(manifest))
                expected = {'scope': '^unsupported patch scope$', 'helper': '^unsupported helper contract$'}.get(failure, '.')
                with self.assertRaisesRegex(ValueError, expected): p.verify_manifest(root, self.versions)

    def test_full_inventory_detects_changed_missing_and_extra_files(self):
        for failure in ('changed', 'missing', 'extra'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as work:
                source = Path(work) / 'source'; source.mkdir()
                self.fixture(source)
                recorded = p.inventory(source)
                if failure == 'changed': (source / 'helper/proc-mount-helper.c').write_text('altered')
                if failure == 'missing': (source / 'helper/proc-mount-helper.c').unlink()
                if failure == 'extra': (source / 'extra').write_text('unrecorded')
                archive = Path(work) / 'source.tar'
                with tarfile.open(archive, 'w') as tf: tf.add(source, arcname='root')
                lock = dict(self.lock, sha256=p.sha256(archive), input_sha256=recorded)
                with mock.patch.object(p.urllib.request, 'urlopen') as response:
                    response.return_value.read.return_value = archive.read_bytes()
                    with self.assertRaisesRegex(ValueError, 'inventory/hash mismatch'):
                        p.fetch(lock, Path(work) / 'download.tar', Path(work) / 'unpacked', self.versions)

    @unittest.skipUnless(__import__('shutil').which('patch'), 'requires GNU patch')
    def test_real_patch_requires_all_context_and_rejects_fuzzy_match(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); integration = root / 'integration'; (integration / 'patches').mkdir(parents=True)
            (root / 'target').write_text('different\nold\nlast\n')
            patch = '--- a/target\n+++ b/target\n@@ -1,3 +1,3 @@\n first\n-old\n+new\n last\n'
            (integration / 'patches/proc-transfer.patch').write_text(patch)
            with self.assertRaises(subprocess.CalledProcessError): p.apply_patch(root, integration)
            self.assertEqual((root / 'target').read_text(), 'different\nold\nlast\n')
            (root / 'target').write_text('first\nold\nlast\n')
            p.apply_patch(root, integration)
            self.assertEqual((root / 'target').read_text(), 'first\nnew\nlast\n')

    def test_patch_dry_run_failure_prevents_application_and_fuzz_is_zero(self):
        with mock.patch.object(p.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'patch')) as command:
            with self.assertRaises(subprocess.CalledProcessError): p.apply_patch(Path('/source'), Path('/integration'))
            self.assertEqual(command.call_count, 1)
            self.assertIn('--fuzz=0', command.call_args.args[0])
            self.assertIn('--dry-run', command.call_args.args[0])

    def image_archive(self, work, *, uid=0, mismatch=False, test_helper=False):
        values = {'sysbox-fs': b'fs', 'proc-mount-helper': b'helper', 'supervisor': b'supervisor'}
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in values.items()}
        metadata = {'binary_sha256': hashes, 'supervisor_sha256': hashes['supervisor'],
                    'proc_transfer': {'output_sha256': {name: hashes[name] for name in ('sysbox-fs', 'proc-mount-helper')}}}
        layer = io.BytesIO()
        with tarfile.open(fileobj=layer, mode='w') as tf:
            names = ['rootfs/usr/local/bin/sysbox-fs'] + ['rootfs/usr/local/lib/containers/sysbox-fs/' + name for name in values]
            if test_helper: names.append('rootfs/usr/local/lib/containers/sysbox-fs/helper-checkpoint')
            for name in names:
                data = b'bad' if mismatch else values.get(Path(name).name, b'test')
                member = tarfile.TarInfo(name); member.uid = uid; member.mode = 0o755; member.size = len(data)
                tf.addfile(member, io.BytesIO(data))
        archive = Path(work) / 'extension.tar'
        with tarfile.open(archive, 'w') as tf:
            for name, data in [('manifest.json', json.dumps([{'Layers': ['layer.tar']}]).encode()), ('layer.tar', layer.getvalue())]:
                member = tarfile.TarInfo(name); member.size = len(data); tf.addfile(member, io.BytesIO(data))
        return archive, metadata

    def test_image_assertions_reject_mismatch_nonroot_and_checkpoint_helper(self):
        with tempfile.TemporaryDirectory() as work:
            archive, metadata = self.image_archive(work)
            self.assertTrue(p.verify_extension_archive(archive, metadata)['test_helpers_absent'])
            for changes in ({'uid': 1001}, {'mismatch': True}, {'test_helper': True}):
                archive, metadata = self.image_archive(work, **changes)
                with self.assertRaises(ValueError): p.verify_extension_archive(archive, metadata)

    def test_installed_pair_rejects_missing_mismatched_or_wrong_architecture(self):
        with tempfile.TemporaryDirectory() as work:
            local = Path(work); root = local / 'lib/containers/sysbox-fs'; root.mkdir(parents=True)
            with self.assertRaises(ValueError): p.verify_pair(local, {'sysbox-fs': 'a' * 64})
            (root / 'sysbox-fs').write_bytes(b'bad'); (root / 'sysbox-fs').chmod(0o755)
            with self.assertRaises(ValueError): p.verify_pair(local, {'sysbox-fs': hashlib.sha256(b'bad').hexdigest()})

    def test_runtime_launcher_rejects_invalid_arguments_before_runtime_operations(self):
        result = subprocess.run([sys.executable, str(ROOT / 'build/runtime_launcher.py'), '--container', 'wrong', '--token', 'wrong',
                                 '--metadata', 'unused', '--image-lock', 'unused', '--result', 'unused', '--log', 'unused'], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)

class OperatorContracts(unittest.TestCase):
    @staticmethod
    def module(name, path):
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_independent_launcher_artifact_integrity_and_mapping(self):
        launcher = self.module('launcher', 'build/runtime_launcher.py')
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            values = {name: b'fixed' for name in launcher.REQUIRED_ARTIFACTS}
            for name in ('ctr', 'helper-checkpoint', 'checkpoint-requester'):
                values[name] = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\0'
            for name, value in values.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
            lock = {'schema_version': 1, 'architecture': 'amd64', 'contract': launcher.CONTRACT,
                    'launcher_python_minimum': '3.12', 'revision': 'a' * 40, 'source_lock_sha256': 'b' * 64,
                    'artifact_sha256': {name: hashlib.sha256(value).hexdigest() for name, value in values.items()}}
            def save(): (root / 'artifacts.lock.json').write_text(json.dumps(lock))
            save(); launcher.verify_artifacts(root)
            for missing in launcher.REQUIRED_ARTIFACTS:
                changed = lock['artifact_sha256'].pop(missing); save()
                with self.assertRaises(ValueError): launcher.verify_artifacts(root)
                lock['artifact_sha256'][missing] = changed
            save()
            (root / 'ctr').write_bytes(b'changed')
            with self.assertRaises(ValueError): launcher.verify_artifacts(root)
            lock['artifact_sha256']['ctr'] = hashlib.sha256(b'changed').hexdigest(); save()
            with self.assertRaisesRegex(ValueError, 'ABI'): launcher.verify_artifacts(root)
            mapping = root / 'uid_map'; mapping.write_text('0 100000 65536\n')
            self.assertEqual(launcher.mapped(mapping, 1001), 101001)
            with self.assertRaises(ValueError): launcher.mapped(mapping, 65536)
            mapping.write_text('0 0 4294967295\n')
            with self.assertRaises(ValueError): launcher.mapped(mapping, 1001)

    def test_manifest_renderer_keeps_actual_probe_filter_and_dedicated_trust_scope(self):
        operator = self.module('operator', 'scripts/proc_transfer_operator.py')
        lock = {'schema_version': 1, 'test_images': {'proc_transfer_probe': {
            'distribution': 'local-archive-import-only', 'anonymous_pull_verified': False,
            'uid': 1001, 'gid': 1001, 'bubblewrap': '0.12.0', 'oci_digest': 'sha256:' + 'a' * 64,
            'local_tag': 'docker.io/issue5-local/proc-transfer-probe:locked'},
            'proc_transfer_bootstrap': 'busybox:1.36@sha256:' + 'b' * 64}}
        manifests = operator.render(lock, 'authorized-node', 'owned-namespace', 'a' * 32)
        actual, trusted = manifests['probe']['spec'], manifests['launcher']['spec']
        self.assertEqual(actual['runtimeClassName'], 'sysbox-runc')
        self.assertFalse(actual['hostUsers']); self.assertTrue(trusted['hostUsers']); self.assertTrue(trusted['hostPID'])
        self.assertEqual(actual['containers'][0]['securityContext'], {'runAsUser': 1001, 'runAsGroup': 1001, 'privileged': False,
                         'runAsNonRoot': True, 'allowPrivilegeEscalation': False,
                         'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'Unconfined'}})
        for spec in (actual, trusted):
            self.assertEqual(spec['containers'][0]['imagePullPolicy'], 'Never')
            if spec is trusted: self.assertNotIn('seccompProfile', spec['containers'][0]['securityContext'])
            self.assertFalse(spec['automountServiceAccountToken'])
        lock['test_images']['proc_transfer_probe']['bubblewrap'] = '0.9.0'
        with self.assertRaises(ValueError): operator.render(lock, 'authorized-node', 'owned-namespace', 'a' * 32)

    def test_running_backend_rejects_stale_executable_namespace_and_environment(self):
        from types import SimpleNamespace
        launcher = self.module('backend_launcher', 'build/runtime_launcher.py')
        with tempfile.TemporaryDirectory() as work:
            root=Path(work); backend=root/'123'; (backend/'root').mkdir(parents=True); (backend/'ns').mkdir()
            (backend/'exe').write_bytes(b'fs'); (backend/'root/proc-mount-helper').write_bytes(b'helper')
            (backend/'environ').write_bytes(b'SYSBOX_PROC_TRANSFER_HELPER=/proc-mount-helper\0')
            (backend/'ns/user').symlink_to('user:[2]'); (backend/'ns/pid').symlink_to('pid:[1]')
            metadata={'binary_sha256': {'sysbox-fs': hashlib.sha256(b'fs').hexdigest(),
                                       'proc-mount-helper': hashlib.sha256(b'helper').hexdigest()}}
            host={'user': 'user:[2]', 'pid': 'pid:[1]'}
            with mock.patch.object(launcher.Path, 'stat', return_value=SimpleNamespace(st_uid=0, st_gid=0, st_mode=0o100755)):
                launcher.verify_backend(123, metadata, host, root)
                (backend/'exe').write_bytes(b'stale')
                with self.assertRaisesRegex(ValueError, 'running fs executable'): launcher.verify_backend(123, metadata, host, root)
                (backend/'exe').write_bytes(b'fs')
                (backend/'ns/user').unlink(); (backend/'ns/user').symlink_to('user:[9]')
                with self.assertRaisesRegex(ValueError, 'initial User'): launcher.verify_backend(123, metadata, host, root)
                (backend/'ns/user').unlink(); (backend/'ns/user').symlink_to('user:[2]')
                (backend/'environ').write_bytes(b'SYSBOX_PROC_TRANSFER_HELPER=/caller-chosen\0')
                with self.assertRaisesRegex(ValueError, 'environment'): launcher.verify_backend(123, metadata, host, root)

    def test_cleanup_requires_observed_owned_process_inventory(self):
        launcher = self.module('launcher_cleanup', 'build/runtime_launcher.py')
        with mock.patch.object(launcher, 'probe_processes', side_effect=[{10, 11}, {10}]), mock.patch.object(launcher.time, 'sleep'):
            launcher.verify_exec_cleanup('a'*64, 10)
        with mock.patch.object(launcher, 'probe_processes', return_value={10, 11}), mock.patch.object(launcher.time, 'monotonic', side_effect=[0, 11]):
            with self.assertRaisesRegex(ValueError, 'descendants remain'): launcher.verify_exec_cleanup('a'*64, 10)
        with mock.patch.object(launcher.subprocess, 'check_output', return_value='unexpected output'):
            with self.assertRaisesRegex(ValueError, 'inventory'): launcher.probe_processes('a'*64)

    def test_public_probe_requires_verified_exact_digest(self):
        operator = self.module('operator_public', 'scripts/proc_transfer_operator.py')
        probe = {'distribution': 'public-registry', 'anonymous_pull_verified': True, 'uid': 1001,
                 'gid': 1001, 'bubblewrap': '0.12.0', 'oci_digest': 'sha256:' + 'a'*64,
                 'image': 'ghcr.io/example/talos-sysbox-proc-transfer-probe@sha256:' + 'a'*64}
        lock = {'schema_version': 1, 'test_images': {'proc_transfer_probe': probe,
                'proc_transfer_bootstrap': 'busybox@sha256:' + 'b'*64}}
        manifests = operator.render(lock, 'approved', 'owned', 'a'*32)
        self.assertNotIn('bootstrap', manifests)
        for name in ('probe', 'launcher'):
            container = manifests[name]['spec']['containers'][0]
            self.assertEqual(container['image'], probe['image'])
            self.assertEqual(container['imagePullPolicy'], 'IfNotPresent')
        for key, value in [('anonymous_pull_verified', False), ('image', probe['image'].replace('a'*64, 'b'*64))]:
            changed = copy.deepcopy(lock); changed['test_images']['proc_transfer_probe'][key] = value
            with self.assertRaises(ValueError): operator.render(changed, 'approved', 'owned', 'a'*32)

    def test_exec_timeout_removes_only_owned_exec_and_propagates_failure(self):
        wrapper = self.module('exec_wrapper', 'build/runtime_exec.py')
        timeout = subprocess.TimeoutExpired('exec', 300)
        with mock.patch.object(wrapper.subprocess, 'run', side_effect=[timeout, subprocess.CompletedProcess([], 137)]) as run:
            with self.assertRaises(subprocess.TimeoutExpired): wrapper.execute(['python3', 'stress-proc.py'], 'a'*64, 300)
            first, cleanup = run.call_args_list
            self.assertEqual(first.kwargs['timeout'], 300)
            self.assertEqual(cleanup.kwargs['timeout'], 15)
            self.assertIn('--force', cleanup.args[0])
            self.assertIn('delete', cleanup.args[0])
            owned = next(x for x in first.args[0] if x.startswith('--exec-id='))
            self.assertIn(owned, cleanup.args[0]); self.assertEqual(cleanup.args[0][-1], 'a'*64)
            self.assertNotIn('--all', cleanup.args[0])
        with mock.patch.object(wrapper.subprocess, 'run', side_effect=[timeout, subprocess.TimeoutExpired('delete', 15)]):
            with self.assertRaises(subprocess.TimeoutExpired): wrapper.execute(['false'], 'a'*64, 300)


if __name__ == '__main__': unittest.main()
