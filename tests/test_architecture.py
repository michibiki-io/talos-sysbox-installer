import copy
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_build as fixtures
import test_proc_transfer as proc_fixtures

build, stage, reuse, publish = fixtures.build, fixtures.stage, fixtures.reuse, fixtures.publish
proc = build.proc_transfer
ROOT = fixtures.ROOT
clients = fixtures.load('install_test_tools')
combine = fixtures.load_release('combine_inputs')


def archive(path, arch, image='example:fixture'):
    with tarfile.open(path, 'w') as tf:
        for name, data in [('manifest.json', json.dumps([{'Config': 'config.json', 'RepoTags': [image], 'Layers': []}]).encode()),
                           ('config.json', json.dumps({'architecture': arch, 'os': 'linux'}).encode())]:
            item = tarfile.TarInfo(name)
            item.size = len(data)
            tf.addfile(item, io.BytesIO(data))


class ArchitectureTests(unittest.TestCase):
    def test_pinned_inputs_preserve_versions_and_host_target_distinction(self):
        for arch, machine in [('amd64', 'x86_64'), ('arm64', 'aarch64')]:
            path, lock = build.select_lock(target=arch)
            self.assertEqual(lock['architecture'], arch)
            self.assertEqual(lock['talos'], '1.14.5')
            self.assertEqual(lock['sysbox'], '0.7.1')
            self.assertEqual(clients.host_lock(machine)['test_tools'], lock['test_tools'])
            self.assertIn('linux_' + arch, lock['sysbox_package']['url'])
            for tool in lock['test_tools'].values():
                self.assertIn(arch, tool['url'])
                self.assertRegex(tool['sha256'], r'^[0-9a-f]{64}$')
            with self.assertRaisesRegex(ValueError, 'does not match'):
                build.select_lock(path, 'arm64' if arch == 'amd64' else 'amd64')
        with self.assertRaises(ValueError): clients.host_lock('riscv64')
        with self.assertRaises(ValueError): build.select_lock(target='riscv64')

    def test_oci_uses_actual_config_and_rejects_wrong_target(self):
        for arch in ('amd64', 'arm64'):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as work:
                root = Path(work); path = root/'image.tar'; archive(path, arch)
                build.docker_to_oci(path, root/'oci')
                platform = json.loads((root/'oci/index.json').read_text())['manifests'][0]['platform']
                self.assertEqual(platform, {'os': 'linux', 'architecture': arch})
                with self.assertRaises(ValueError): build.archive_platform(path, 'arm64' if arch == 'amd64' else 'amd64')

    def test_elf_accepts_only_selected_64_bit_little_endian_machine(self):
        for arch, machine in proc.ELF_MACHINES.items():
            header = b'\x7fELF\x02\x01' + bytes(12) + machine.to_bytes(2, 'little')
            proc.verify_elf(header, arch)
            for target in proc.ELF_MACHINES:
                if target != arch:
                    with self.assertRaises(ValueError): proc.verify_elf(header, target)
            for changed in (header[:4] + b'\x01' + header[5:], header[:5] + b'\x02' + header[6:], b''):
                with self.assertRaises(ValueError): proc.verify_elf(changed, arch)

    def test_build_propagates_target_to_platform_profile_archives_and_tags(self):
        for arch in ('amd64', 'arm64'):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as work:
                root = Path(work); calls = []
                def run(args, **kw):
                    calls.append(args)
                    if args[:2] == ['docker', 'buildx'] and '--output' in args:
                        dest = Path(args[args.index('--output')+1].split('dest=')[1])
                        (dest/'build-metadata.json').write_text('{}')
                    elif args[:2] == ['docker', 'save']:
                        archive(Path(args[args.index('-o')+1]), arch, args[-1])
                    elif args[:2] == ['docker', 'run']:
                        profile = json.loads(kw['input'])
                        self.assertEqual(profile['arch'], arch)
                        destination = Path(args[args.index('-v', args.index('-v')+1)+1].removesuffix(':/out'))
                        archive(destination/('installer-'+arch+'.tar'), arch)
                with mock.patch.object(sys, 'argv', ['build.py', '--arch', arch, '--output', str(root)]), \
                     mock.patch.object(build, 'run', side_effect=run), \
                     mock.patch.object(build, 'source_archive'), \
                     mock.patch.object(proc, 'verify_extension_archive', return_value={}):
                    build.main()
                result = json.loads((root/'images.json').read_text())
                self.assertEqual(result['architecture'], arch)
                self.assertEqual(len(result['installers']), 2)
                for args in calls:
                    if args[:2] in (['docker', 'buildx'], ['docker', 'run']):
                        self.assertEqual(args[args.index('--platform')+1], 'linux/'+arch)
                self.assertEqual(result['tag'].endswith('-arm64'), arch == 'arm64')
                for item in result['image_archives'].values(): build.archive_platform(root/item['file'], arch)

    def test_probe_builder_selects_arch_before_first_build(self):
        for arch in ('amd64', 'arm64'):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as work:
                with mock.patch.object(sys, 'argv', ['probe', '--builder', 'fixture', '--arch', arch, '--output', work]), \
                     mock.patch.object(fixtures.probe_build.build, 'run', side_effect=RuntimeError('first-build')) as run:
                    with self.assertRaisesRegex(RuntimeError, 'first-build'): fixtures.probe_build.main()
                args = run.call_args.args[0]
                self.assertEqual(args[args.index('--platform')+1], 'linux/'+arch)
                self.assertEqual(args[args.index('-t')+1].endswith('-arm64'), arch == 'arm64')

    def test_launcher_checks_actual_elf_against_artifact_architecture(self):
        launcher = proc_fixtures.OperatorContracts.module('arm_launcher', 'build/runtime_launcher.py')
        for arch, machine in proc.ELF_MACHINES.items():
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as work:
                root = Path(work)
                values = {name: b'fixed' for name in launcher.REQUIRED_ARTIFACTS}
                for name in launcher.VALIDATION_BINARIES:
                    values[name] = b'\x7fELF\x02\x01' + bytes(12) + machine.to_bytes(2, 'little')
                for name, value in values.items():
                    path = root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
                lock = {'schema_version': 1, 'architecture': arch, 'contract': launcher.CONTRACT,
                        'build_profile': proc.BUILD_PROFILE, 'launcher_python_minimum': '3.12',
                        'revision': 'a'*40, 'source_lock_sha256': 'b'*64, 'artifact_sha256': proc.inventory(root)}
                (root/'artifacts.lock.json').write_text(json.dumps(lock))
                self.assertEqual(launcher.verify_artifacts(root)['architecture'], arch)
                lock['architecture'] = 'arm64' if arch == 'amd64' else 'amd64'
                (root/'artifacts.lock.json').write_text(json.dumps(lock))
                with self.assertRaisesRegex(ValueError, 'ABI'): launcher.verify_artifacts(root)

    def test_launcher_rejects_host_artifact_and_probe_architecture_mismatches(self):
        launcher = proc_fixtures.OperatorContracts.module('host_launcher', 'build/runtime_launcher.py')
        for machine, arch in [('x86_64', 'amd64'), ('aarch64', 'arm64')]:
            wrong = 'arm64' if arch == 'amd64' else 'amd64'
            for artifact_arch, probe_arch in [(wrong, arch), (arch, wrong)]:
                with self.subTest(host=arch, artifact=artifact_arch, probe=probe_arch), tempfile.TemporaryDirectory() as work:
                    image_lock = Path(work)/'image-lock.json'
                    image_lock.write_text(json.dumps({'validation_artifacts_lock_sha256': 'a'*64,
                        'test_images': {'proc_transfer_probe': {'architecture': probe_arch}}}))
                    with mock.patch.object(launcher.sys, 'version_info', (3, 12)), \
                         mock.patch.object(launcher.os, 'geteuid', return_value=0), \
                         mock.patch.object(launcher.os, 'uname', return_value=SimpleNamespace(machine=machine)), \
                         mock.patch.object(launcher, 'trusted_runtime_namespaces', return_value={}), \
                         mock.patch.object(launcher, 'verify_artifacts', return_value={'architecture': artifact_arch}), \
                         mock.patch.object(launcher, 'digest', return_value='a'*64), \
                         mock.patch.object(launcher.subprocess, 'check_output') as commands:
                        with self.assertRaisesRegex(ValueError, 'trusted host architecture'):
                            launcher.run(SimpleNamespace(image_lock=image_lock))
                        commands.assert_not_called()

    def test_extension_rejects_architecture_mismatch_even_with_matching_payload_hashes(self):
        with tempfile.TemporaryDirectory() as work:
            path, metadata = proc_fixtures.Integration().image_archive(work)
            metadata['inputs'] = {'architecture': 'arm64'}
            with self.assertRaisesRegex(ValueError, 'architecture'): proc.verify_extension_archive(path, metadata)

    def test_target_pod_tools_use_target_helm_member_on_other_host(self):
        for arch in ('amd64', 'arm64'):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode='w:gz') as tf:
                item = tarfile.TarInfo('linux-'+arch+'/helm'); item.size = 4; tf.addfile(item, io.BytesIO(b'helm'))
            payload = data.getvalue()
            runner = fixtures.validation.Runner.__new__(fixtures.validation.Runner)
            runner.lock = {'architecture': arch, 'test_tools': {'helm': {'url': 'fixture', 'sha256': __import__('hashlib').sha256(payload).hexdigest()}}}
            with mock.patch.object(runner, 'execute'), mock.patch.object(runner, 'upload') as upload, \
                 mock.patch.object(fixtures.validation.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
                runner.tools()
            upload.assert_called_once_with('/usr/local/bin/helm', b'helm')


class MultiArchitectureReleaseTests(unittest.TestCase):
    def inputs(self, root):
        for arch in ('amd64', 'arm64'):
            (root/arch).mkdir()
            fixtures.BuildTests().probe_inputs(root/arch, arch)
        return combine.combine(root)

    def test_all_sources_and_archives_staged_before_marker_and_restore_validates_both(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); inputs = self.inputs(root)
            self.assertEqual(len(stage.validate_assets(inputs, root)), 12)
            with mock.patch.object(stage, 'find_release', return_value=None), mock.patch.object(stage.subprocess, 'run') as run:
                stage.stage('example/repo', 'v0.1.0', root, {'preflight': True, 'inputs': inputs})
            uploaded = run.call_args_list[1].args[0][4:]
            self.assertEqual({Path(p).name for p in uploaded}, set(stage.asset_names(inputs)))
            self.assertEqual(run.call_args_list[-1].args[0][-1], str(root/'images.json'))
            with mock.patch.object(reuse.subprocess, 'run') as run:
                reuse.restore_images(inputs, root)
                self.assertEqual(run.call_count, 8)
            (root/'arm64/probe-image.tar').write_bytes(b'corrupt')
            with mock.patch.object(reuse.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'checksum'): reuse.restore_images(inputs, root)
                run.assert_not_called()

    def test_second_arch_collision_prevents_every_push_and_success_keeps_probe_locks(self):
        with tempfile.TemporaryDirectory() as work:
            inputs = self.inputs(Path(work))
            def output(args, **kw):
                if args[1] == 'info': return '[]'
                return next(record['image_config_digests'][args[-1]] for record in inputs['architectures'].values() if args[-1] in record['image_config_digests'])
            def manifest(image):
                return {'config': {'digest': 'sha256:wrong'}} if image.endswith('-arm64') else None
            with mock.patch.object(publish.subprocess, 'check_output', side_effect=output), \
                 mock.patch.object(publish, 'existing_manifest', side_effect=manifest), \
                 mock.patch.object(publish.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'Immutable'): publish.publish_images(inputs)
                run.assert_not_called()
            with mock.patch.object(publish.subprocess, 'check_output', side_effect=output), \
                 mock.patch.object(publish, 'existing_manifest', return_value=None), \
                 mock.patch.object(publish, 'anonymous_digest', return_value='sha256:'+'a'*64), \
                 mock.patch.object(publish.subprocess, 'run') as run:
                result = publish.publish_images(inputs)
                self.assertEqual(run.call_count, 8)
            self.assertEqual({i['architecture'] for i in result['images']}, {'amd64', 'arm64'})
            self.assertIn('amd64-build-metadata.json', result['licenses'])
            self.assertIn('arm64-build-metadata.json', result['licenses'])
            for arch, lock in result['test_image_locks'].items():
                self.assertEqual(lock['test_images']['proc_transfer_probe']['architecture'], arch)
                self.assertTrue(lock['test_images']['proc_transfer_probe']['anonymous_pull_verified'])

    def test_missing_or_mislabeled_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            inputs = self.inputs(Path(work))
            for changed in [dict(inputs, architectures={'amd64': inputs['architectures']['amd64']}), copy.deepcopy(inputs)]:
                if 'arm64' in changed['architectures']: changed['architectures']['arm64']['architecture'] = 'amd64'
                with self.assertRaises(RuntimeError): stage.targets(changed)
                with self.assertRaises(RuntimeError): publish.publish_images(changed, preflight_only=True)

    def test_reuse_downloads_original_archives_for_both_targets_without_rebuild(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); inputs = self.inputs(root)
            original = {arch: {name: (root/arch/name).read_bytes() for name in [*stage.ARCHIVES, 'probe-image.tar']} for arch in inputs['architectures']}
            release = {'draft': False, 'assets': [{'name': n} for n in ['images.json', *stage.asset_names(inputs)]]}
            destination = root/'restore'; destination.mkdir(); previous = Path.cwd()
            def run(args, **kw):
                if args[:3] != ['gh', 'release', 'download']: return
                directory = Path(args[args.index('--dir')+1]); name = args[args.index('--pattern')+1]
                if name == 'images.json': (directory/name).write_text(json.dumps(inputs))
                else:
                    arch, filename = name.split('-', 1); (directory/name).write_bytes(original[arch][filename])
            try:
                os.chdir(destination)
                with mock.patch.object(sys, 'argv', ['reuse', '--tag', 'v0.1.0']), \
                     mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': 'example/repo', 'GITHUB_OUTPUT': str(root/'output')}), \
                     mock.patch.object(reuse, 'find_release', return_value=release), \
                     mock.patch.object(reuse.subprocess, 'run', side_effect=run) as calls:
                    reuse.main()
                loads = [c.args[0] for c in calls.call_args_list if c.args[0][:2] == ['docker', 'load']]
                self.assertEqual(len(loads), 8)
                self.assertIn('reuse=true', (root/'output').read_text())
            finally: os.chdir(previous)

    def test_legacy_amd64_reuse_without_architecture_key_keeps_unprefixed_assets(self):
        for with_probe in (False, True):
            with self.subTest(probe=with_probe), tempfile.TemporaryDirectory() as work:
                root = Path(work); source = root/'source'; source.mkdir()
                helper = fixtures.BuildTests()
                inputs = helper.probe_inputs(source) if with_probe else helper.original_archives(source)
                inputs.pop('architecture')
                names = [*stage.ARCHIVES, *(['probe-image.tar'] if with_probe else [])]
                original = {name: (source/name).read_bytes() for name in names}
                release = {'draft': False, 'assets': [{'name': n} for n in ['images.json', *stage.asset_names(inputs)]]}
                destination = root/'restore'; destination.mkdir(); previous = Path.cwd()
                def run(args, **kw):
                    if args[:3] != ['gh', 'release', 'download']: return
                    directory = Path(args[args.index('--dir')+1]); name = args[args.index('--pattern')+1]
                    self.assertIn(name, ['images.json', *names])
                    if name == 'images.json': (directory/name).write_text(json.dumps(inputs))
                    else: (directory/name).write_bytes(original[name])
                try:
                    os.chdir(destination)
                    with mock.patch.object(sys, 'argv', ['reuse', '--tag', 'v0.1.0']), \
                         mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': 'example/repo', 'GITHUB_OUTPUT': str(root/'output')}), \
                         mock.patch.object(reuse, 'find_release', return_value=release), \
                         mock.patch.object(reuse.subprocess, 'run', side_effect=run) as calls:
                        reuse.main()
                    loads = [c.args[0] for c in calls.call_args_list if c.args[0][:2] == ['docker', 'load']]
                    self.assertEqual(len(loads), len(names))
                    self.assertEqual({Path(c[-1]) for c in loads}, {Path('.build')/name for name in names})
                    self.assertNotIn('architecture', json.loads(Path('.build/images.json').read_text()))
                finally: os.chdir(previous)

    def test_inspect_only_reports_staged_and_completed_without_download_or_load(self):
        for completed in (False, True):
            with self.subTest(completed=completed), tempfile.TemporaryDirectory() as work:
                root = Path(work); previous = Path.cwd()
                names = ['images.json'] + (['release-images.json'] if completed else [])
                release = {'draft': not completed, 'assets': [{'name': n} for n in names]}
                try:
                    os.chdir(root)
                    with mock.patch.object(sys, 'argv', ['reuse', '--tag', 'v0.1.0', '--inspect-only']), \
                         mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': 'example/repo', 'GITHUB_OUTPUT': str(root/'output')}), \
                         mock.patch.object(reuse, 'find_release', return_value=release), \
                         mock.patch.object(reuse.subprocess, 'run') as commands, \
                         mock.patch.object(reuse, 'restore_images') as restore:
                        reuse.main()
                        commands.assert_not_called(); restore.assert_not_called()
                    self.assertFalse(Path('.build').exists())
                    self.assertEqual((root/'output').read_text(), 'reuse=true\ncompleted='+str(completed).lower()+'\n')
                finally: os.chdir(previous)

    def test_v2_published_and_partial_release_reuse_preserves_existing_images(self):
        with tempfile.TemporaryDirectory() as work:
            inputs = self.inputs(Path(work))
            configs = {image: digest for target in inputs['architectures'].values() for image, digest in target['image_config_digests'].items()}
            def existing(image): return {'config': {'digest': configs[image]}}
            with mock.patch.object(publish, 'existing_manifest', side_effect=existing), \
                 mock.patch.object(publish, 'anonymous_digest', return_value='sha256:'+'a'*64), \
                 mock.patch.object(publish.subprocess, 'check_output') as loaded, \
                 mock.patch.object(publish.subprocess, 'run') as push:
                result = publish.publish_images(inputs, verify_existing=True)
                self.assertEqual(len(result['images']), 8)
                push.assert_not_called(); loaded.assert_not_called()
            def output(args, **kw): return '[]' if args[1] == 'info' else configs[args[-1]]
            def partial(image): return None if image.endswith('-arm64') else existing(image)
            with mock.patch.object(publish, 'existing_manifest', side_effect=partial), \
                 mock.patch.object(publish, 'anonymous_digest', return_value='sha256:'+'a'*64), \
                 mock.patch.object(publish.subprocess, 'check_output', side_effect=output), \
                 mock.patch.object(publish.subprocess, 'run') as push:
                result = publish.publish_images(inputs)
                self.assertEqual(len(result['images']), 8)
                self.assertEqual(push.call_count, 4)
                self.assertTrue(all(c.args[0][-1].endswith('-arm64') for c in push.call_args_list))


if __name__ == '__main__': unittest.main()
