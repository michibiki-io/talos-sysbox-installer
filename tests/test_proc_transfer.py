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
        for missing in ('helper/proc-mount-helper.c', 'tests/runtime/sysfs_oracle.py',
                        'tests/static/test_sysfs_oracle.py'):
            changed = copy.deepcopy(self.lock)
            del changed['input_sha256'][missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError): self.load(changed)
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
                    'components': {name: {'repository': 'https://github.com/nestybox/' + ('fuse' if name == 'fuse' else component) + '.git',
                          'revision': (self.versions['sources']['sysbox'] if name == 'root' else self.versions['sysbox_components'][component])['commit']}
                         for name, component in {'root': 'sysbox', 'fs': 'sysbox-fs', 'runc': 'sysbox-runc',
                           'ipc': 'sysbox-ipc', 'libs': 'sysbox-libs', 'fuse': 'sysbox-fs/bazil'}.items()},
                    'builder': 'golang:1.27.2-trixie@sha256:47667934874913a449b96faa7ef4777fa285d9ec29126ee80ed61c45b7cccf0e',
                    'generator': 'github.com/golang/protobuf/protoc-gen-go@v1.3.5', 'source_sha256': {}}
        for name in ('build', 'helper', 'patches'): (root / name).mkdir()
        (root / 'helper/proc-mount-helper.c').write_text('10 requester thread pidfd; trusted parent-user launcher; supported proc flags')
        changed = ('sysbox-fs/nsenter/event.go', 'sysbox-fs/nsenter/eventService.go', 'sysbox-fs/seccomp/mount.go',
                   'sysbox-fs/seccomp/proc_transfer.go', 'sysbox-fs/seccomp/tracer.go', 'sysbox-runc/libcontainer/nsenter/nsexec.c')
        (root / 'patches/proc-transfer.patch').write_text('\n'.join('+++ b/' + name for name in changed) + '\nconst procPidfdThread = 0x80\nSYSBOX_PROC_TRANSFER_HELPER WithChildReaperGuard NotifIDValid')
        (root / 'helper/sysfs-mount-helper.c').write_text('sysfs-transfer: 7 requester Network; 10 requester thread pidfd; 200/201; supported sysfs flags; /tmp')
        (root / 'patches/sysfs-transfer.patch').write_text('\n'.join('+++ b/' + name for name in sorted(p.SYSFS_PATCH_PATHS)) + '\nSYSBOX_SYSFS_TRANSFER_HELPER CaptureSysfsPolicy WithChildReaperGuard')
        manifest['source_sha256'] = {name: p.sha256(root / name) for name in p.PRODUCTION_INPUTS}
        (root / 'build/sources.lock.json').write_text(json.dumps(manifest))
        return manifest

    def test_manifest_revisions_hashes_contract_and_patch_scope(self):
        for failure in ('revision', 'hash', 'schema', 'generator', 'builder', 'scope', 'helper',
                        'fuse-revision', 'fuse-repository', 'fuse-fields', 'missing', 'extra', 'unknown', 'invalid-hash'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as work:
                root = Path(work); manifest = self.fixture(work)
                p.verify_manifest(root, self.versions)
                if failure == 'fuse-revision': manifest['components']['fuse']['revision'] = 'a' * 40
                if failure == 'fuse-fields': manifest['components']['fuse']['optional'] = True
                if failure == 'invalid-hash': manifest['source_sha256']['helper/proc-mount-helper.c'] = 'invalid'
                if failure == 'fuse-repository': manifest['components']['fuse']['repository'] = 'https://github.com/other/fuse.git'
                if failure == 'missing': del manifest['components']['fuse']
                if failure == 'extra': manifest['components']['mgr'] = manifest['components']['fs']
                if failure == 'unknown': manifest['components']['future'] = manifest['components'].pop('fuse')
                if failure == 'builder': manifest['builder'] = 'future'
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

    def test_candidate_updater_checks_baseline_actual_bytes_and_preserves_full_inventory(self):
        updater = OperatorContracts.module('updater', 'scripts/update_proc_transfer.py')
        for change in ('tests-only', 'helper', 'patch', 'missing-required', 'fuse-pin', 'source-hash', 'contract', 'sysfs-helper', 'sysfs-patch', 'unknown-production', 'missing-oracle', 'missing-oracle-test'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as work:
                root = Path(work); source = root / 'source'; source.mkdir()
                manifest = self.fixture(source)
                for name in p.REQUIRED - set(p.inventory(source)):
                    path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('fixture')
                baseline = copy.deepcopy(self.lock)
                baseline['input_sha256'] = p.inventory(source)
                project = root / 'project'; (project / 'build').mkdir(parents=True)
                (project / 'build/proc-transfer.lock.json').write_text(json.dumps(baseline))
                versions = root / 'versions.json'; versions.write_text(json.dumps(self.versions))
                # Change real bytes AND refresh upstream hashes: old updater
                # accepted this production drift. Baseline must still reject it.
                target = {'helper': 'helper/proc-mount-helper.c', 'patch': 'patches/proc-transfer.patch',
                          'tests-only': 'tests/runtime/verify.c', 'missing-required': 'tests/runtime/verify.c',
                          'fuse-pin': 'tests/runtime/verify.c', 'source-hash': 'tests/runtime/verify.c',
                          'contract': 'tests/runtime/verify.c', 'sysfs-helper': 'helper/sysfs-mount-helper.c',
                          'sysfs-patch': 'patches/sysfs-transfer.patch', 'unknown-production': 'tests/runtime/verify.c',
                          'missing-oracle': 'tests/runtime/sysfs_oracle.py',
                          'missing-oracle-test': 'tests/static/test_sysfs_oracle.py'}[change]
                if change in ('missing-required', 'missing-oracle', 'missing-oracle-test'): (source / target).unlink()
                else: (source / target).write_text((source / target).read_text() + '\n/* refreshed */')
                for name in manifest['source_sha256']: manifest['source_sha256'][name] = p.sha256(source / name)
                if change == 'unknown-production':
                    (source / 'helper/unknown.c').write_text('unapproved production')
                    manifest['source_sha256']['helper/unknown.c'] = p.sha256(source / 'helper/unknown.c')
                if change == 'fuse-pin': manifest['components']['fuse']['revision'] = 'f'*40
                if change == 'source-hash': manifest['source_sha256']['helper/proc-mount-helper.c'] = 'f'*64
                if change == 'contract': manifest['builder'] = 'future'
                (source / 'build/sources.lock.json').write_text(json.dumps(manifest))
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode='w:gz') as tf: tf.add(source, arcname='upstream')
                output = root / 'candidate.json'; revision = 'a' * 40
                with mock.patch.object(updater, 'ROOT', project), \
                     mock.patch.object(sys, 'argv', ['update', '--revision', revision, '--output', str(output), '--versions', str(versions)]), \
                     mock.patch.object(updater.urllib.request, 'urlopen', return_value=io.BytesIO(archive.getvalue())) as request:
                    if change == 'tests-only':
                        updater.main()
                        candidate = p.load_lock(output)
                        self.assertEqual(candidate['input_sha256'], p.inventory(source))
                        self.assertEqual(candidate['sha256'], hashlib.sha256(archive.getvalue()).hexdigest())
                        for name in p.PRODUCTION_INPUTS:
                            self.assertEqual(candidate['input_sha256'][name], baseline['input_sha256'][name])
                    else:
                        with self.assertRaisesRegex(ValueError, 'production input|required inputs|component revision|manifest hash|build contract'): updater.main()
                        self.assertFalse(output.exists())
                    request.assert_called_once_with(p.archive_url(revision), timeout=120)
                self.assertEqual(json.loads((project / 'build/proc-transfer.lock.json').read_text()), baseline)

    def test_approved_production_inputs_remain_previous_reviewed_baseline(self):
        expected = {'helper/proc-mount-helper.c': '78444175cb3e046093442db8e5cad116c2310d4c4879a26d05ec0ba386217208',
                    'patches/proc-transfer.patch': 'd7a6b3962fe4d58eeed0537660f226d3542bccb71dc0c29c8069b5d61fe88838',
                    'helper/sysfs-mount-helper.c': 'cde00c750f1c89f9b61f0e036a39c489510a2fa89594d124635938b88ce2bd54',
                    'patches/sysfs-transfer.patch': '6880096cc27c9709621f7125786a4f7d4c1234bff2109a35c008cca3711ab3de'}
        self.assertEqual({name: self.lock['input_sha256'][name] for name in expected}, expected)


    def test_fixed_builder_selects_proc_regressions_and_inventories_without_upstream_recipe(self):
        import ast
        import shutil
        from types import SimpleNamespace
        # Load only this function: importing assemble would read /work inputs.
        tree = ast.parse((ROOT / 'build/assemble.py').read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'build_validation')
        for oracle_failure in (False, True):
            with self.subTest(oracle_failure=oracle_failure), tempfile.TemporaryDirectory() as work:
                root = Path(work); source = root / 'source'; source.mkdir(); self.fixture(source)
                for name in p.REQUIRED - set(p.inventory(source)):
                    path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('fixture')
                (source / 'tests/runtime/sysfs_oracle.py').write_text(
                    'IDENTITY = ' + repr('wrong' if oracle_failure else 'reviewed') + '\n')
                (source / 'tests/runtime/sysfs-network.py').write_text(
                    'from sysfs_oracle import IDENTITY\nassert IDENTITY == "reviewed"\n')
                (source / 'tests/static/test_sysfs_oracle.py').write_text(
                    'import pathlib, sys, unittest\n'
                    'sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "runtime"))\n'
                    'import sysfs_oracle\n'
                    'class Oracle(unittest.TestCase):\n'
                    ' def test_identity(self): self.assertEqual(sysfs_oracle.IDENTITY, "reviewed")\n')
                (source / 'tests/runtime/sysfs-unused.py').write_text('raise RuntimeError("must not execute")')
                fs = root / 'components/sysbox-fs/seccomp'; fs.mkdir(parents=True)
                for directory in ('sysfstransfer', 'mount', 'handler/implementations', 'fuse', 'bazil/fs'):
                    (root / 'components/sysbox-fs' / directory).mkdir(parents=True, exist_ok=True)
                def copy(source_path, destination):
                    source_path = Path(source_path)
                    if str(source_path).startswith('/work/project/'):
                        source_path = ROOT / source_path.relative_to('/work/project')
                    if str(source_path) == '/work/ctr': Path(destination).write_bytes(b'fixed ctr')
                    else: shutil.copy2(source_path, destination)
                commands = []
                def command(args, **kwargs):
                    commands.append((args, kwargs))
                    if 'test_sysfs_oracle.py' in args:
                        return subprocess.run([sys.executable, *args[1:]], **kwargs)
                    if args[0] == 'cc': Path(args[-1]).write_bytes(b'checkpoint tool')
                    return subprocess.CompletedProcess(args, 1 if args[0].endswith('mount-helper') else 0,
                                                       stdout='', stderr=(b'sysfs-transfer:' if 'sysfs' in args[0] else b'proc-transfer:') if args[0].endswith('mount-helper') else '')
                namespace = {'OUT': root, 'Path': lambda value: ROOT / Path(value).relative_to('/work/project'),
                             'proc_transfer': p, 'integration_record': {'lock': self.lock},
                             'stable_json': json.dumps, 'subprocess': SimpleNamespace(run=command),
                             'shutil': SimpleNamespace(copy2=copy, copytree=shutil.copytree)}
                exec(compile(ast.Module(body=[function], type_ignores=[]), 'build/assemble.py', 'exec'), namespace)
                if oracle_failure:
                    with self.assertRaisesRegex(RuntimeError, 'selected upstream regression failed'):
                        namespace['build_validation'](source, root / 'components', {'GOFLAGS': '-mod=readonly'})
                    results = json.loads((root / 'validation/upstream-results.json').read_text())
                    self.assertEqual(results[-1]['status'], 'failed')
                    self.assertIn('test_sysfs_oracle.py', results[-1]['command'])
                    self.assertFalse((root / 'validation/artifacts.lock.json').exists())
                    continue
                namespace['build_validation'](source, root / 'components', {'GOFLAGS': '-mod=readonly'})
                for directory in ('runtime', 'oracle/tests/runtime'):
                    subprocess.run([sys.executable, '-E', str(root / 'validation' / directory / 'sysfs-network.py')],
                                   cwd=root, check=True, capture_output=True, text=True)
                python_checks = [args for args, _ in commands if args[0] == 'python3']
                self.assertEqual([args[args.index('-p')+1] for args in python_checks if '-p' in args],
                                 ['test_lifecycle.py', 'test_proc_verifier.py', 'test_sysfs.py', 'test_sysfs_oracle.py'])
                self.assertEqual(len(commands), 19)
                self.assertTrue(any('./sysfstransfer' in args for args, _ in commands))
                for args, kwargs in commands:
                    self.assertFalse(any(value.endswith('scripts/build.py') for value in args))
                record = json.loads((root / 'validation/artifacts.lock.json').read_text())
                self.assertEqual(record['artifact_sha256']['runtime/sysfs-unused.py'], p.sha256(source / 'tests/runtime/sysfs-unused.py'))
                for name in ('runtime/sysfs_oracle.py', 'oracle/tests/runtime/sysfs_oracle.py',
                             'oracle/tests/static/test_sysfs_oracle.py'):
                    original = name.removeprefix('oracle/') if name.startswith('oracle/') else 'tests/' + name
                    self.assertEqual(record['artifact_sha256'][name], p.sha256(source / original))
                self.assertEqual(record['revision'], self.lock['revision'])
                self.assertEqual(record['source_lock_sha256'], p.sha256(ROOT / 'build/proc-transfer.lock.json'))
                results = json.loads((root / 'validation/upstream-results.json').read_text())
                self.assertEqual(len(results), 13)
                self.assertTrue(all(result['status'] == 'passed' for result in results))

    def test_full_inventory_detects_changed_missing_and_extra_files(self):
        for target in ('helper/proc-mount-helper.c', 'tests/runtime/sysfs_oracle.py',
                       'tests/static/test_sysfs_oracle.py'):
            for failure in ('changed', 'missing', 'extra'):
                with self.subTest(target=target, failure=failure), tempfile.TemporaryDirectory() as work:
                    source = Path(work) / 'source'; source.mkdir()
                    self.fixture(source)
                    for name in ('tests/runtime/sysfs_oracle.py', 'tests/static/test_sysfs_oracle.py'):
                        path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('original')
                    recorded = p.inventory(source)
                    if failure == 'changed': (source / target).write_text('altered')
                    if failure == 'missing': (source / target).unlink()
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
            (integration / 'patches/sysfs-transfer.patch').write_text('--- a/target\n+++ b/target\n@@ -1,3 +1,3 @@\n first\n-new\n+sysfs\n last\n')
            p.apply_patch(root, integration)
            self.assertEqual((root / 'target').read_text(), 'first\nsysfs\nlast\n')

    def test_patch_dry_run_failure_prevents_application_and_fuzz_is_zero(self):
        with mock.patch.object(p.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'patch')) as command:
            with self.assertRaises(subprocess.CalledProcessError): p.apply_patch(Path('/source'), Path('/integration'))
            self.assertEqual(command.call_count, 1)
            self.assertIn('--fuzz=0', command.call_args.args[0])
            self.assertIn('--dry-run', command.call_args.args[0])

    def image_archive(self, work, *, uid=0, mismatch=False, test_helper=False):
        values = {'sysbox-fs': b'fs', 'proc-mount-helper': b'helper', 'sysfs-mount-helper': b'sysfs-helper', 'supervisor': b'supervisor'}
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in values.items()}
        metadata = {'binary_sha256': hashes, 'supervisor_sha256': hashes['supervisor'],
                    'proc_transfer': {'build_profile': p.BUILD_PROFILE, 'output_sha256': {name: hashes[name] for name in p.PRODUCTION_BINARIES}}}
        layer = io.BytesIO()
        with tarfile.open(fileobj=layer, mode='w') as tf:
            names = ['rootfs/usr/local/bin/sysbox-fs'] + ['rootfs/usr/local/lib/containers/sysbox-fs/' + name for name in values]
            if test_helper: names.append('rootfs/usr/local/lib/containers/sysbox-fs/' + (test_helper if isinstance(test_helper, str) else 'helper-checkpoint'))
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
            for changes in ({'uid': 1001}, {'mismatch': True}, *({'test_helper': name} for name in p.VALIDATION_ONLY)):
                archive, metadata = self.image_archive(work, **changes)
                with self.assertRaises(ValueError): p.verify_extension_archive(archive, metadata)

    def test_installed_pair_rejects_missing_mismatched_or_wrong_architecture(self):
        with tempfile.TemporaryDirectory() as work:
            local = Path(work); root = local / 'lib/containers/sysbox-fs'; root.mkdir(parents=True)
            with self.assertRaises(ValueError): p.verify_pair(local, {'sysbox-fs': 'a' * 64})
            (root / 'sysbox-fs').write_bytes(b'bad'); (root / 'sysbox-fs').chmod(0o755)
            with self.assertRaises(ValueError): p.verify_pair(local, {'sysbox-fs': hashlib.sha256(b'bad').hexdigest()})

    def test_sysfs_production_inventory_and_contract_reject_unknown_and_tampered_inputs(self):
        for failure in ('unknown', 'missing', 'scope', 'helper-contract', 'unrelated-production'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as work:
                root = Path(work); manifest = self.fixture(root)
                if failure == 'unrelated-production':
                    (root / 'production.go').write_text('unapproved')
                    manifest['source_sha256']['production.go'] = p.sha256(root / 'production.go')
                if failure == 'unknown':
                    (root / 'patches/extra.patch').write_text('unapproved')
                    manifest['source_sha256']['patches/extra.patch'] = p.sha256(root / 'patches/extra.patch')
                if failure == 'missing':
                    del manifest['source_sha256']['helper/sysfs-mount-helper.c']
                if failure == 'scope':
                    path = root / 'patches/sysfs-transfer.patch'
                    path.write_text(path.read_text() + '\n+++ b/sysbox-mgr/main.go')
                    manifest['source_sha256'][str(path.relative_to(root))] = p.sha256(path)
                if failure == 'helper-contract':
                    path = root / 'helper/sysfs-mount-helper.c'; path.write_text('unsupported ABI')
                    manifest['source_sha256'][str(path.relative_to(root))] = p.sha256(path)
                (root / 'build/sources.lock.json').write_text(json.dumps(manifest))
                with self.assertRaises(ValueError): p.verify_manifest(root, self.versions)

    def test_image_profile_and_triple_inventory_reject_proc_only_same_revision(self):
        with tempfile.TemporaryDirectory() as work:
            archive, metadata = self.image_archive(work)
            for failure in ('profile', 'missing-sysfs', 'changed-sysfs'):
                changed = copy.deepcopy(metadata)
                if failure == 'profile': changed['proc_transfer']['build_profile'] = 'proc-only'
                if failure == 'missing-sysfs': changed['proc_transfer']['output_sha256'].pop('sysfs-mount-helper')
                if failure == 'changed-sysfs': changed['proc_transfer']['output_sha256']['sysfs-mount-helper'] = 'f'*64
                with self.subTest(failure=failure), self.assertRaises(ValueError):
                    p.verify_extension_archive(archive, changed)

    def test_patch_order_proc_then_sysfs_and_each_dry_run_precedes_mutation(self):
        with mock.patch.object(p.subprocess, 'run') as command:
            p.apply_patch(Path('/source'), Path('/integration'))
        calls = [c.args[0] for c in command.call_args_list]
        self.assertEqual([Path(c[c.index('-i')+1]).name for c in calls],
                         ['proc-transfer.patch']*2 + ['sysfs-transfer.patch']*2)
        self.assertEqual(['--dry-run' in c for c in calls], [True, False, True, False])
        self.assertTrue(all('--fuzz=0' in c for c in calls))

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
            for name in launcher.VALIDATION_BINARIES:
                values[name] = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\0'
            for name, value in values.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
            lock = {'schema_version': 1, 'architecture': 'amd64', 'contract': launcher.CONTRACT,
                    'build_profile': p.BUILD_PROFILE, 'launcher_python_minimum': '3.12', 'revision': 'a' * 40, 'source_lock_sha256': 'b' * 64,
                    'artifact_sha256': {name: hashlib.sha256(value).hexdigest() for name, value in values.items()}}
            def save(): (root / 'artifacts.lock.json').write_text(json.dumps(lock))
            save(); launcher.verify_artifacts(root)
            for missing in launcher.REQUIRED_ARTIFACTS:
                changed = lock['artifact_sha256'].pop(missing); save()
                with self.assertRaises(ValueError): launcher.verify_artifacts(root)
                lock['artifact_sha256'][missing] = changed
            save()
            for name in ('runtime/sysfs_oracle.py', 'oracle/tests/runtime/sysfs_oracle.py',
                         'oracle/tests/static/test_sysfs_oracle.py'):
                path = root / name
                for failure in ('missing', 'changed', 'symlink'):
                    if failure == 'missing': path.unlink()
                    elif failure == 'changed': path.write_bytes(b'tampered')
                    else:
                        path.unlink(); path.symlink_to(root / 'launcher.py')
                    with self.subTest(name=name, failure=failure), self.assertRaises((ValueError, FileNotFoundError)):
                        launcher.verify_artifacts(root)
                    path.unlink(missing_ok=True); path.write_bytes(values[name])
            (root / 'ctr').write_bytes(b'changed')
            with self.assertRaises(ValueError): launcher.verify_artifacts(root)
            lock['artifact_sha256']['ctr'] = hashlib.sha256(b'changed').hexdigest(); save()
            with self.assertRaisesRegex(ValueError, 'ABI'): launcher.verify_artifacts(root)
            mapping = root / 'uid_map'; mapping.write_text('0 100000 65536\n')
            self.assertEqual(launcher.mapped(mapping, 1001), 101001)
            with self.assertRaises(ValueError): launcher.mapped(mapping, 65536)
            mapping.write_text('0 0 4294967295\n')
            with self.assertRaises(ValueError): launcher.mapped(mapping, 1001)

    def test_probe_process_root_oracle_integrity_with_valid_launcher_payload(self):
        launcher = self.module('probe_oracle_launcher', 'build/runtime_launcher.py')
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); payload = root / 'payload'; proc = root / 'proc/123'
            values = {name: name.encode() for name in launcher.REQUIRED_ARTIFACTS | {'oracle/additional-input.txt'}}
            for name in launcher.VALIDATION_BINARIES:
                values[name] = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\0'
            for name, value in values.items():
                path = payload / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
            lock = {'schema_version': 1, 'architecture': 'amd64', 'contract': launcher.CONTRACT,
                    'build_profile': p.BUILD_PROFILE, 'launcher_python_minimum': '3.12', 'revision': 'a' * 40,
                    'source_lock_sha256': 'b' * 64,
                    'artifact_sha256': {name: hashlib.sha256(value).hexdigest() for name, value in values.items()}}
            (payload / 'artifacts.lock.json').write_text(json.dumps(lock))
            artifacts = launcher.verify_artifacts(payload)
            for name, value in values.items():
                if name.startswith('oracle/'):
                    path = proc / 'root/tmp/issue5-repo' / name.removeprefix('oracle/')
                    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
            launcher.verify_oracle_root(proc, artifacts)
            # Every inventoried oracle copy is checked, including inputs beyond
            # the required module and static test; launcher copies stay valid.
            for name in sorted(name for name in values if name.startswith('oracle/')):
                path = proc / 'root/tmp/issue5-repo' / name.removeprefix('oracle/')
                for failure in ('changed', 'missing', 'symlink'):
                    with self.subTest(name=name, failure=failure):
                        if failure == 'changed': path.write_bytes(b'tampered')
                        elif failure == 'missing': path.unlink()
                        else:
                            path.unlink(); path.symlink_to(payload / name)
                        launcher.verify_artifacts(payload)
                        if failure == 'missing':
                            with self.assertRaises(FileNotFoundError): launcher.verify_oracle_root(proc, artifacts)
                        else:
                            with self.assertRaisesRegex(ValueError, 'self-contained upstream oracle mismatch'):
                                launcher.verify_oracle_root(proc, artifacts)
                        path.unlink(missing_ok=True); path.write_bytes(values[name])
            launcher.verify_oracle_root(proc, artifacts)

    def test_manifest_renderer_keeps_actual_probe_filter_and_dedicated_trust_scope(self):
        operator = self.module('operator', 'scripts/proc_transfer_operator.py')
        lock = {'schema_version': 1, 'build_profile': p.BUILD_PROFILE, 'test_images': {'proc_transfer_probe': {
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
        self.assertEqual({v['hostPath']['path'] for v in trusted['volumes']}, {'/usr/local', '/run/containerd'})
        self.assertEqual({v['mountPath'] for v in trusted['containers'][0]['volumeMounts']}, {'/usr/local', '/run/containerd'})
        self.assertTrue(all(v['readOnly'] for v in trusted['containers'][0]['volumeMounts']))
        self.assertNotIn('volumes', actual)
        lock['test_images']['proc_transfer_probe']['bubblewrap'] = '0.9.0'
        with self.assertRaises(ValueError): operator.render(lock, 'authorized-node', 'owned-namespace', 'a' * 32)

    def test_running_backend_rejects_stale_executable_namespace_and_environment(self):
        from types import SimpleNamespace
        launcher = self.module('backend_launcher', 'build/runtime_launcher.py')
        with tempfile.TemporaryDirectory() as work:
            root=Path(work); backend=root/'123'; (backend/'root').mkdir(parents=True); (backend/'ns').mkdir()
            (backend/'exe').write_bytes(b'fs'); (backend/'root/proc-mount-helper').write_bytes(b'helper')
            (backend/'root/sysfs-mount-helper').write_bytes(b'sysfs-helper')
            (backend/'environ').write_bytes(b'SYSBOX_PROC_TRANSFER_HELPER=/proc-mount-helper\0SYSBOX_SYSFS_TRANSFER_HELPER=/sysfs-mount-helper\0')
            (backend/'ns/user').symlink_to('user:[2]'); (backend/'ns/pid').symlink_to('pid:[1]')
            metadata={'binary_sha256': {'sysbox-fs': hashlib.sha256(b'fs').hexdigest(),
                                       'sysfs-mount-helper': hashlib.sha256(b'sysfs-helper').hexdigest(), 'proc-mount-helper': hashlib.sha256(b'helper').hexdigest()}}
            host={'user': 'user:[2]', 'pid': 'pid:[1]'}
            with mock.patch.object(launcher.Path, 'stat', return_value=SimpleNamespace(st_uid=0, st_gid=0, st_mode=0o100755)):
                launcher.verify_backend(123, metadata, host, root)
                original = (backend/'environ').read_bytes()
                for changed in (original.replace(b'SYSBOX_SYSFS_TRANSFER_HELPER=/sysfs-mount-helper', b''),
                                original.replace(b'/sysfs-mount-helper', b'/caller-selected'),
                                original + b'SYSBOX_SYSFS_TRANSFER_HELPER=/sysfs-mount-helper\0'):
                    (backend/'environ').write_bytes(changed)
                    with self.assertRaisesRegex(ValueError, 'environment'): launcher.verify_backend(123, metadata, host, root)
                (backend/'environ').write_bytes(original)
                (backend/'root/sysfs-mount-helper').write_bytes(b'tampered')
                with self.assertRaisesRegex(ValueError, 'path mismatch'): launcher.verify_backend(123, metadata, host, root)
                (backend/'root/sysfs-mount-helper').write_bytes(b'sysfs-helper')
                for stat in (SimpleNamespace(st_uid=1001, st_gid=0, st_mode=0o100755),
                             SimpleNamespace(st_uid=0, st_gid=1001, st_mode=0o100755),
                             SimpleNamespace(st_uid=0, st_gid=0, st_mode=0o100777)):
                    with mock.patch.object(launcher.Path, 'stat', return_value=stat):
                        with self.assertRaisesRegex(ValueError, 'path mismatch'): launcher.verify_backend(123, metadata, host, root)
                (backend/'root/sysfs-mount-helper').unlink()
                (backend/'root/sysfs-mount-helper').symlink_to('proc-mount-helper')
                with self.assertRaisesRegex(ValueError, 'path mismatch'): launcher.verify_backend(123, metadata, host, root)
                (backend/'root/sysfs-mount-helper').unlink()
                (backend/'root/sysfs-mount-helper').write_bytes(b'sysfs-helper')
                (backend/'exe').write_bytes(b'stale')
                with self.assertRaisesRegex(ValueError, 'running fs executable'): launcher.verify_backend(123, metadata, host, root)
                (backend/'exe').write_bytes(b'fs')
                (backend/'ns/user').unlink(); (backend/'ns/user').symlink_to('user:[9]')
                with self.assertRaisesRegex(ValueError, 'initial User'): launcher.verify_backend(123, metadata, host, root)
                (backend/'ns/user').unlink(); (backend/'ns/user').symlink_to('user:[2]')
                (backend/'environ').write_bytes(b'SYSBOX_PROC_TRANSFER_HELPER=/caller-chosen\0')
                with self.assertRaisesRegex(ValueError, 'environment'): launcher.verify_backend(123, metadata, host, root)

    def test_sysfs_runtime_route_covers_real_path_matrix_without_failure_daemon(self):
        route = self.module('sysfs_runtime_route', 'build/sysfs_runtime.py')
        commands = list(route.commands(Path('/tmp/owned-target')))
        real = [c for c in commands if '/opt/proc-transfer/thread-sysfs' in c]
        self.assertEqual(len(real), 8)
        for entry in ('bwrap', 'unshare'):
            self.assertEqual({tuple(c[-2:]) for c in real if c[0] == entry},
                             {(mode, network) for mode in ('readwrite', 'readonly') for network in ('shared-net', 'new-net')})
        self.assertTrue(all('--ro-bind' in c and '/tmp/issue5-repo' in c for c in real if c[0] == 'bwrap'))
        self.assertTrue(any(c[-2:] == ['/opt/proc-transfer/runtime/sysfs-interleave.py', '/sys'] for c in commands))
        self.assertTrue(any(c[-1] == '/opt/proc-transfer/runtime/sysfs-write-policy.py' for c in commands))
        self.assertFalse(any('failure' in arg or 'dentry-protection' in arg for c in commands for arg in c))
        self.assertEqual(commands[-1], ['unshare', '--user', '--map-root-user', '--mount', '--net', '--',
                                       '/usr/bin/python3', '/opt/proc-transfer/runtime/sysfs-write-policy.py'])
        with mock.patch.object(route.os, 'getuid', return_value=1001), \
             mock.patch.object(route.os, 'getgid', return_value=1001), \
             mock.patch.object(route.tempfile, 'TemporaryDirectory') as temporary, \
             mock.patch.object(route.subprocess, 'run') as run:
            temporary.return_value.__enter__.return_value = '/tmp/owned-target'
            route.main()
            self.assertEqual(run.call_args_list, [mock.call(c, check=True, timeout=45) for c in commands])
            run.reset_mock(); run.side_effect = subprocess.CalledProcessError(1, commands[0])
            with self.assertRaises(subprocess.CalledProcessError): route.main()
            self.assertEqual(run.call_count, 1)

        with mock.patch.object(route.os, 'getuid', return_value=0), mock.patch.object(route.subprocess, 'run') as run:
            with self.assertRaises(ValueError): route.main()
            run.assert_not_called()

    def test_proc_only_artifact_and_operator_profiles_are_rejected(self):
        operator = self.module('profile_operator', 'scripts/proc_transfer_operator.py')
        for profile in (None, 'proc-only'):
            lock = {'schema_version': 1, 'build_profile': profile, 'test_images': {
                'proc_transfer_probe': {'distribution': 'local-archive-import-only'}}}
            with self.assertRaises(ValueError): operator.render(lock, 'approved', 'owned', 'a'*32)

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
        lock = {'schema_version': 1, 'build_profile': p.BUILD_PROFILE, 'test_images': {'proc_transfer_probe': probe,
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

    def test_node_namespace_gate_is_independent_and_fails_before_socket_query(self):
        launcher = self.module('namespace_launcher', 'build/runtime_launcher.py')
        with tempfile.TemporaryDirectory() as work:
            proc = Path(work)
            for name in ('1', 'self'): (proc / name / 'ns').mkdir(parents=True)
            for name, identity in (('pid', 'pid:[1]'), ('user', 'user:[2]')):
                for who in ('1', 'self'): (proc / who / 'ns' / name).symlink_to(identity)
            mapping = proc / 'self/uid_map'; mapping.write_text('0 0 4294967295\n')
            self.assertEqual(launcher.trusted_host_namespaces(proc), {'pid': 'pid:[1]', 'user': 'user:[2]'})
            for name in ('pid', 'user'):
                link = proc / 'self/ns' / name; original = link.readlink()
                link.unlink(); link.symlink_to(name + ':[9]')
                with self.assertRaisesRegex(ValueError, 'independently observe'): launcher.trusted_host_namespaces(proc)
                link.unlink(); link.symlink_to(original)
            mapping.write_text('0 100000 65536\n')
            with self.assertRaisesRegex(ValueError, 'initial User'): launcher.trusted_host_namespaces(proc)
        with mock.patch.object(launcher.sys, 'version_info', (3, 12)), \
             mock.patch.object(launcher.os, 'geteuid', return_value=0), \
             mock.patch.object(launcher.os, 'uname') as uname, \
             mock.patch.object(launcher, 'trusted_host_namespaces', side_effect=ValueError('untrusted')), \
             mock.patch.object(launcher, 'task_pid') as task:
            uname.return_value.machine = 'x86_64'
            with self.assertRaisesRegex(ValueError, 'untrusted'): launcher.run(None)
            task.assert_not_called()

    def test_system_socket_uses_verified_node_root_after_pair_checks(self):
        from types import SimpleNamespace
        launcher = self.module('socket_launcher', 'build/runtime_launcher.py')
        sha = 'b'*64; revision = 'a'*40
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); (root / 'image-versions.json').write_text('{}')
            lock = {'schema_version': 1, 'build_profile': p.BUILD_PROFILE, 'validation_artifacts_lock_sha256': sha,
                    'source_lock_sha256': sha, 'image_versions_sha256': hashlib.sha256(b'{}').hexdigest(),
                    'test_images': {'proc_transfer_probe': {'distribution': 'local-archive-import-only',
                     'anonymous_pull_verified': False, 'local_tag': 'probe:fixed',
                     'oci_digest': 'sha256:' + sha, 'config_digest': 'sha256:' + 'c'*64}}}
            image_lock = root / 'images.json'; image_lock.write_text(json.dumps(lock))
            metadata_path = root / 'metadata.json'
            metadata = {'build_mode': 'release', 'proc_transfer': {'build_profile': p.BUILD_PROFILE, 'output_sha256': {name: sha for name in p.PRODUCTION_BINARIES}, 'lock': {'revision': revision}},
                        'source_inputs_sha256': {'build/proc-transfer.lock.json': sha},
                        'binary_sha256': {name: sha for name in (*p.PRODUCTION_BINARIES, 'sysbox-runc', 'sysbox-mgr')},
                        'supervisor_sha256': sha}
            args = SimpleNamespace(image_lock=image_lock, metadata=metadata_path, container='d'*64)
            for failure in (None, 'revision', 'source-lock', 'binary', 'profile', 'inventory'):
                changed = copy.deepcopy(metadata)
                if failure == 'profile': changed['proc_transfer']['build_profile'] = 'proc-only'
                if failure == 'inventory': changed['proc_transfer']['output_sha256'].pop('sysfs-mount-helper')
                if failure == 'revision': changed['proc_transfer']['lock']['revision'] = 'e'*40
                if failure == 'source-lock': changed['source_inputs_sha256']['build/proc-transfer.lock.json'] = 'e'*64
                if failure == 'binary': changed['binary_sha256']['sysbox-fs'] = 'e'*64
                metadata_path.write_text(json.dumps(changed))
                with self.subTest(failure=failure), \
                     mock.patch.object(launcher, 'BASE', root), \
                     mock.patch.object(launcher.sys, 'version_info', (3, 12)), \
                     mock.patch.object(launcher.os, 'geteuid', return_value=0), \
                     mock.patch.object(launcher.os, 'uname', return_value=SimpleNamespace(machine='x86_64')), \
                     mock.patch.object(launcher, 'trusted_host_namespaces', return_value={'pid': 'pid:[1]', 'user': 'user:[2]'}), \
                     mock.patch.object(launcher, 'verify_artifacts', return_value={'revision': revision, 'source_lock_sha256': sha}), \
                     mock.patch.object(launcher, 'digest', return_value=sha), \
                     mock.patch.object(launcher.Path, 'stat', return_value=SimpleNamespace(st_uid=0, st_gid=0, st_mode=0o100755)), \
                     mock.patch.object(launcher.subprocess, 'check_output', side_effect=[
                         'REF TYPE DIGEST\nprobe:fixed type sha256:' + sha + '\n', b'{"Image":"probe:fixed"}']), \
                     mock.patch.object(launcher, 'task_pid', side_effect=RuntimeError('socket reached')) as task:
                    if failure:
                        with self.assertRaises(ValueError): launcher.run(args)
                        task.assert_not_called()
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'socket reached'): launcher.run(args)
                        task.assert_called_once_with('/proc/1/root/system/run/containerd/containerd.sock', 'talos-sysbox', 'sysbox-fs')

    def test_fifo_is_private_same_inode_through_process_root_and_exit_is_preserved(self):
        import os
        wrapper = self.module('fifo_wrapper', 'build/runtime_exec.py')
        real_run = subprocess.run
        directories = []
        def ctr(command, **kwargs):
            visible = Path(next(x.split('=', 1)[1] for x in command if x.startswith('--fifo-dir=')))
            self.assertEqual(visible.parts[1:4], ('proc', str(os.getpid()), 'root'))
            local = Path('/') / Path(*visible.parts[4:])
            directories.append(local)
            self.assertEqual(local.parent, Path('/tmp'))
            self.assertEqual(local.stat().st_mode & 0o777, 0o700)
            self.assertEqual(local.stat().st_uid, os.geteuid())
            self.assertTrue(local.samefile(visible))
            os.mkfifo(visible / 'stdin', 0o600)
            # A separate daemon-side process resolves the supplied path to the
            # exact FIFO inode created from the client's Pod-local view.
            observed = real_run([sys.executable, '-c',
                'import os,sys; s=os.stat(sys.argv[1]); print(s.st_dev,s.st_ino)',
                str(visible / 'stdin')], capture_output=True, text=True, check=True, timeout=5)
            st = (local / 'stdin').stat()
            self.assertEqual(observed.stdout.strip(), f'{st.st_dev} {st.st_ino}')
            self.assertIn('--user=1001:1001', command)
            self.assertEqual(command[-4:], ['a'*64, 'sh', '-c', 'exit 23'])
            self.assertEqual(kwargs['timeout'], 19)
            return subprocess.CompletedProcess(command, 23)
        with mock.patch.object(wrapper.subprocess, 'run', side_effect=ctr), \
             mock.patch.dict(os.environ, {'TMPDIR': '/run/containerd'}):
            self.assertEqual(wrapper.execute(['sh', '-c', 'exit 23'], 'a'*64, 19), 23)
        self.assertTrue(directories)
        self.assertTrue(all(not directory.exists() for directory in directories))

    def test_fifo_identity_failure_never_starts_ctr_and_cleans_storage(self):
        wrapper = self.module('fifo_bad_identity', 'build/runtime_exec.py')
        directories = []
        def mismatch(local, other):
            directories.append(local)
            return False
        with mock.patch.object(wrapper.Path, 'samefile', mismatch), \
             mock.patch.object(wrapper.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'identity/mode'): wrapper.execute(['true'], 'a'*64)
            run.assert_not_called()
        self.assertTrue(all(not directory.exists() for directory in directories))

    def test_fifo_wrong_mode_fails_closed_before_exec(self):
        wrapper = self.module('fifo_bad_mode', 'build/runtime_exec.py')
        chmod = Path.chmod; directories = []
        def wrong_mode(local, mode):
            directories.append(local)
            chmod(local, 0o755)
        with mock.patch.object(wrapper.Path, 'chmod', wrong_mode), \
             mock.patch.object(wrapper.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'identity/mode'): wrapper.execute(['true'], 'a'*64)
            run.assert_not_called()
        self.assertTrue(directories)
        self.assertTrue(all(not directory.exists() for directory in directories))

    def test_fifo_cleanup_follows_bounded_owned_delete_for_all_exceptional_paths(self):
        wrapper = self.module('fifo_cleanup', 'build/runtime_exec.py')
        for error in (subprocess.TimeoutExpired('exec', 10), InterruptedError('cancelled'), OSError('launch failed')):
            for cleanup in (0, 137, 1, subprocess.TimeoutExpired('delete', 15), InterruptedError('cancelled again')):
                with self.subTest(error=type(error).__name__, cleanup=str(cleanup)):
                    commands = []; directories = []
                    def ctr(command, **kwargs):
                        commands.append(command)
                        if len(commands) == 1:
                            directory = Path(next(x.split('=', 1)[1] for x in command if x.startswith('--fifo-dir=')))
                            directories.append(directory)
                            (directory / 'still-needed').write_text('owned')
                            raise error
                        self.assertEqual(kwargs['timeout'], 15)
                        self.assertTrue((directories[0] / 'still-needed').exists())
                        self.assertEqual(command, commands[0][:4] + ['delete', '--force',
                            next(x for x in commands[0] if x.startswith('--exec-id=')), 'a'*64])
                        if isinstance(cleanup, BaseException): raise cleanup
                        return subprocess.CompletedProcess(command, cleanup)
                    with mock.patch.object(wrapper.subprocess, 'run', side_effect=ctr), \
                         mock.patch.object(wrapper.sys, 'stderr', new=io.StringIO()):
                        with self.assertRaises(type(error)) as raised: wrapper.execute(['false'], 'a'*64, 10)
                        self.assertIs(raised.exception, error)
                    self.assertEqual(len(commands), 2)
                    self.assertFalse(directories[0].exists())

    def test_exec_main_cancellation_deadline_status_and_invalid_inputs(self):
        import signal
        wrapper = self.module('exec_main', 'build/runtime_exec.py')
        for container, timeout in (('unowned', 1), ('a'*64, 0), ('a'*64, 601)):
            with mock.patch.object(wrapper.tempfile, 'TemporaryDirectory') as storage, \
                 mock.patch.object(wrapper.subprocess, 'run') as run:
                with self.assertRaises(ValueError): wrapper.execute(['false'], container, timeout)
                storage.assert_not_called(); run.assert_not_called()
        for event in (signal.SIGTERM, signal.SIGINT, 'deadline', 'exit'):
            handlers = {}
            def invoke(arguments, container, timeout):
                self.assertEqual((arguments, container, timeout), (['sh', '-c', 'exit 37'], 'a'*64, 21))
                if event == 'deadline': raise subprocess.TimeoutExpired('exec', timeout)
                if event == 'exit': return 37
                handlers[event](event, None)
            with self.subTest(event=event), \
                 mock.patch.object(wrapper.signal, 'signal', side_effect=lambda sig, handler: handlers.update({sig: handler})), \
                 mock.patch.object(wrapper, 'execute', side_effect=invoke), \
                 mock.patch.object(wrapper.sys, 'argv', ['runtime_exec.py', 'sh', '-c', 'exit 37']), \
                 mock.patch.dict(wrapper.os.environ, {'PROC_TRANSFER_PROBE_CONTAINER': 'a'*64, 'PROC_TRANSFER_EXEC_TIMEOUT': '21'}):
                self.assertEqual(wrapper.main(), 37 if event == 'exit' else 124)

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
