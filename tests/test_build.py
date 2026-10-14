import importlib.util
import hashlib
import io
import json
import tarfile
import tempfile
import os
import sys
import subprocess
import shutil
import tomllib
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

build = load('build')
runtime = load('runtime_patch')
publish = load('publish')
spec=importlib.util.spec_from_file_location('validate_runtime',ROOT/'test/e2e/scripts/validate_runtime.py')
validation=importlib.util.module_from_spec(spec);spec.loader.exec_module(validation)

spec=importlib.util.spec_from_file_location('official_sources',ROOT/'build/official_sources.py')
official=importlib.util.module_from_spec(spec);spec.loader.exec_module(official)

spec=importlib.util.spec_from_file_location('validation_packages', ROOT/'build/validation_packages.py')
probe_packages=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe_packages)

def load_release(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts/release'/f'{name}.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

stage=load_release('stage_release')
reuse=load_release('reuse_inputs')

class ProbePackageTests(unittest.TestCase):
    # Observed pinned Ubuntu base and signed 20261001 snapshot metadata.
    OLD = '1:3.1.2-2.1build1.1'
    CANDIDATE = '1:3.1.2-2.1ubuntu0.1'

    def installed(self):
        return [{'package': name, 'architecture': arch, 'version': self.OLD,
                 'source': 'audit', 'source_version': self.OLD}
                for name, arch in [('libaudit-common', 'all'), ('libaudit1', 'amd64')]]

    def repository(self):
        return {(p['package'], p['architecture'], self.CANDIDATE):
                {'source': ('audit', self.CANDIDATE), 'repository': 'signed snapshot index'}
                for p in self.installed()}

    def selected(self):
        return probe_packages.resolve(self.installed(),
            {p['package']: self.CANDIDATE for p in self.installed()},
            self.repository(), {('audit', self.CANDIDATE)})

    def test_inherited_audit_binaries_are_both_selected_before_source_collection(self):
        selected = self.selected()
        self.assertEqual(len(selected), 2)
        self.assertTrue(all(p['version'] == self.CANDIDATE and
                            p['source_version'] == self.CANDIDATE for p in selected))
        # Downloading the newer source while retaining either old binary is rejected.
        with self.assertRaisesRegex(ValueError, 'effective binary/source differs'):
            probe_packages.verify_effective(self.installed(), selected, self.installed())
        probe_packages.verify_effective(selected, selected, self.installed())

    def test_missing_repository_candidates_and_exact_sources_fail_closed(self):
        for candidates in ({}, {'libaudit1': '(none)'},
                           {p['package']: self.OLD for p in self.installed()}):
            with self.subTest(candidates=candidates), self.assertRaisesRegex(ValueError, 'no signed snapshot candidate'):
                probe_packages.resolve(self.installed(), candidates, self.repository(), {('audit', self.CANDIDATE)})
        with self.assertRaisesRegex(ValueError, 'no exact snapshot source'):
            probe_packages.resolve(self.installed(), {p['package']: self.CANDIDATE for p in self.installed()},
                                   self.repository(), {('audit', self.OLD)})

    def test_source_identity_preserves_binary_rebuild_and_implicit_versions(self):
        for source, expected in [(None, ('example', '2+b1')), ('upstream', ('upstream', '2+b1')),
                                 ('upstream (1:2)', ('upstream', '1:2'))]:
            record = {'Package': 'example', 'Version': '2+b1'}
            if source: record['Source'] = source
            self.assertEqual(probe_packages.source_identity(record), expected)
        selected = self.selected()
        for field, wrong in [('source', 'different'), ('source_version', self.OLD),
                             ('version', self.OLD), ('architecture', 'arm64')]:
            changed = [dict(p) for p in selected]; changed[0][field] = wrong
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'differs'):
                probe_packages.verify_effective(changed, selected, self.installed())
        with self.assertRaisesRegex(ValueError, 'removed'):
            probe_packages.verify_effective(selected[:1], selected[:1], self.installed())

    def targets(self):
        return [{'Identifier': kind, 'Trusted': 'yes', 'Origin': 'Ubuntu', 'Codename': 'noble',
                 'Suite': suite, 'Component': component, 'Architecture': 'amd64',
                 'URI': f'https://snapshot.ubuntu.com/ubuntu/20261001T000000Z/dists/{suite}/{component}/'
                        + ('source/Sources' if kind == 'Sources' else 'binary-amd64/Packages'),
                 'Filename': f'{kind}-{suite}-{component}'}
                for kind in ('Sources', 'Packages') for suite in ('noble', 'noble-updates', 'noble-security')
                for component in ('main', 'universe')]

    def control(self, records):
        return '\n\n'.join('\n'.join(f'{k}: {v}' for k, v in r.items()) for r in records)

    def test_unsigned_alternate_snapshot_and_incomplete_indexes_are_rejected(self):
        targets = self.targets()
        self.assertEqual(len(probe_packages.snapshot_targets(self.control(targets), '20261001T000000Z', 'amd64')), 12)
        for field, wrong in [('Trusted', 'no'), ('URI', targets[0]['URI'].replace('20261001', '20261002')),
                             ('URI', 'https://archive.ubuntu.com/ubuntu/dists/noble/main/source/Sources'),
                             ('Origin', 'other'), ('Component', 'restricted')]:
            changed = [dict(t) for t in targets]; changed[0][field] = wrong
            with self.subTest(field=field, wrong=wrong), self.assertRaisesRegex(ValueError, 'outside trusted'):
                probe_packages.snapshot_targets(self.control(changed), '20261001T000000Z', 'amd64')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            probe_packages.snapshot_targets(self.control(targets[:-1]), '20261001T000000Z', 'amd64')

    def test_normalization_installs_exact_versions_and_rechecks_added_dependencies(self):
        before = self.installed()
        final = self.selected()
        dependency = {'package': 'dependency', 'architecture': 'amd64', 'version': '2+b1',
                      'source': 'dependency-source', 'source_version': '2'}
        repo = self.repository()
        repo[('dependency', 'amd64', '2+b1')] = {'source': ('dependency-source', '2'), 'repository': 'signed snapshot index'}
        sources = {('audit', self.CANDIDATE), ('dependency-source', '2')}
        def dpkg(packages):
            return ''.join('installed\t' + '\t'.join(p[f] for f in
                ('package', 'architecture', 'version', 'source', 'source_version')) + '\n' for p in packages)
        for failure in ('none', 'old-binary', 'new-source-mismatch', 'missing-added-source'):
            effective = [dict(p) for p in final] + [dict(dependency)]
            if failure == 'old-binary': effective[0]['version'] = self.OLD
            if failure == 'new-source-mismatch': effective[-1]['source_version'] = 'wrong'
            inventories = iter([dpkg(before), dpkg(effective)])
            def output(args, **kwargs):
                if args[0] == 'dpkg': return 'amd64\n'
                if args[0] == 'dpkg-query': return next(inventories)
                if args[0] == 'apt-get': return self.control(self.targets())
                self.assertEqual(args[:4], ['apt-cache', '-o', 'Dir::State::status=/dev/null', 'policy'])
                self.assertIn('Dir::State::status=/dev/null', args)
                return ''.join(f"{name}:\n  Candidate: {repo_version}\n" for name, repo_version in
                    [('libaudit-common', self.CANDIDATE), ('libaudit1', self.CANDIDATE), ('dependency', '2+b1')])
            available = sources - {('dependency-source', '2')} if failure == 'missing-added-source' else sources
            with self.subTest(failure=failure), mock.patch.object(probe_packages.subprocess, 'check_output', side_effect=output), \
                 mock.patch.object(probe_packages, 'repository_inventory', return_value=(repo, available)), \
                 mock.patch.object(probe_packages.subprocess, 'run') as install:
                if failure == 'none':
                    result = probe_packages.normalize('20261001T000000Z')
                    self.assertEqual(result['before'], before)
                    self.assertEqual(len(result['effective']), 3)
                else:
                    with self.assertRaises(ValueError): probe_packages.normalize('20261001T000000Z')
                self.assertEqual(install.call_args.args[0], ['sh', '/work/apt_retry.sh', 'install', '-y',
                    '--no-install-recommends', '--no-remove', 'libaudit-common:all=' + self.CANDIDATE,
                    'libaudit1:amd64=' + self.CANDIDATE])

    def test_preflight_unavailable_candidate_or_source_prevents_any_install(self):
        installed = ''.join('installed\t' + '\t'.join(p[f] for f in
            ('package', 'architecture', 'version', 'source', 'source_version')) + '\n'
            for p in self.installed())
        for candidate, sources in [(self.OLD, {('audit', self.CANDIDATE)}), (self.CANDIDATE, set())]:
            def output(args, **kwargs):
                if args[0] == 'dpkg': return 'amd64\n'
                if args[0] == 'dpkg-query': return installed
                if args[0] == 'apt-get': return self.control(self.targets())
                return ''.join(f"{p['package']}:\n  Candidate: {candidate}\n" for p in self.installed())
            with self.subTest(candidate=candidate), \
                 mock.patch.object(probe_packages.subprocess, 'check_output', side_effect=output), \
                 mock.patch.object(probe_packages, 'repository_inventory', return_value=(self.repository(), sources)), \
                 mock.patch.object(probe_packages.subprocess, 'run') as install:
                with self.assertRaises(ValueError): probe_packages.normalize('20261001T000000Z')
                install.assert_not_called()

    def test_conflicting_binary_source_identity_is_rejected(self):
        targets = [{'Identifier': 'Packages', 'URI': 'signed snapshot index', 'Filename': f}
                   for f in ('first', 'second')]
        def read(filename):
            return self.control([{'Package': 'example', 'Architecture': 'amd64', 'Version': '2+b1',
                                  'Source': 'upstream (2)' if filename == 'first' else 'wrong (2)'}])
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            probe_packages.repository_inventory(targets, read)

    def test_source_batch_keeps_all_exact_versions_and_failure_prevents_completion(self):
        script = compile((ROOT/'build/validation_image.py').read_text(), 'validation_image.py', 'exec')
        effective = self.selected() + [
            {'package': 'example', 'source': 'example-source', 'source_version': '2+b1'},
            {'package': 'example-old', 'source': 'example-source', 'source_version': '1:1.0'}]
        inventory = {'effective': effective}
        expected = ['audit=' + self.CANDIDATE, 'example-source=1:1.0', 'example-source=2+b1']
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as tf:
            member = tarfile.TarInfo('bubblewrap/COPYING'); member.size = 7
            tf.addfile(member, io.BytesIO(b'license'))
        data = archive.getvalue()
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as work:
                root = Path(work)
                def image_path(value):
                    return root / str(value).lstrip('/')
                for directory in ('work', 'opt/proc-transfer', 'usr/bin'):
                    (root/directory).mkdir(parents=True)
                lock = {'apt_snapshot': '20261001T000000Z', 'validation_sources': {'bubblewrap': {
                    'version': '0.12.0', 'url': 'https://example.invalid/bubblewrap.tar.gz',
                    'sha256': hashlib.sha256(data).hexdigest()}}}
                (root/'work/versions.lock.json').write_text(json.dumps(lock))
                for name in ('bwrap', 'python3', 'cc', 'nsenter'):
                    (root/'usr/bin'/name).write_bytes(name.encode())
                def notices(src, dst, **kwargs):
                    dst.mkdir(parents=True); (dst/'copyright').write_text('package license')
                def command(args, **kwargs):
                    if args[0] != 'sh': return
                    self.assertEqual(args[:5], ['sh', '/work/apt_retry.sh', 'source', '--only-source', '--download-only'])
                    self.assertEqual(args[5:], expected)
                    self.assertEqual(kwargs, {'cwd': root/'opt/validation-sources', 'check': True})
                    (kwargs['cwd']/'audit.dsc').write_bytes(b'partial source')
                    if failure: raise subprocess.CalledProcessError(100, args)
                    (kwargs['cwd']/'example.dsc').write_bytes(b'remaining source')
                with mock.patch('pathlib.Path', side_effect=image_path), \
                     mock.patch.object(sys, 'version_info', (3, 12)), \
                     mock.patch.dict(sys.modules, {'validation_packages': probe_packages}), \
                     mock.patch.object(probe_packages, 'normalize', return_value=inventory), \
                     mock.patch('urllib.request.urlopen', return_value=io.BytesIO(data)), \
                     mock.patch.object(shutil, 'copytree', side_effect=notices), \
                     mock.patch.object(shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
                     mock.patch.object(subprocess, 'check_output', side_effect=lambda args, **kw:
                         {'bwrap': 'bubblewrap 0.12.0', 'python3': 'Python 3.12', 'cc': 'cc fixture'}[args[0]]), \
                     mock.patch.object(subprocess, 'run', side_effect=command) as run:
                    if failure:
                        with self.assertRaises(subprocess.CalledProcessError) as raised: exec(script, {})
                        self.assertEqual(raised.exception.returncode, 100)
                    else:
                        exec(script, {})
                    self.assertEqual(sum(call.args[0][0] == 'sh' for call in run.call_args_list), 1)
                sources = root/'opt/validation-sources'
                self.assertEqual(json.loads((sources/'binary-packages.json').read_text()), inventory)
                self.assertEqual(json.loads((sources/'packages.json').read_text()),
                                 [['audit', self.CANDIDATE], ['example-source', '1:1.0'], ['example-source', '2+b1']])
                self.assertEqual((root/'opt/validation-licenses/BUBBLEWRAP-COPYING').read_bytes(), b'license')
                manifest = sources/'source-files.sha256.json'
                self.assertEqual(manifest.exists(), not failure)
                self.assertEqual((root/'opt/proc-transfer/image-versions.json').exists(), not failure)
                if not failure:
                    hashes = json.loads(manifest.read_text())
                    self.assertEqual(hashes, {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                             for p in sources.iterdir() if p != manifest})

class BuildTests(unittest.TestCase):
    def probe_inputs(self, root):
        inputs = self.original_archives(root)
        image = 'ghcr.io/example/talos-sysbox-proc-transfer-probe:v0.1.0-talos-v1.12.6'
        build.retag_archive(root/'extension.tar', root/'probe-image.tar', image)
        config = build.archive_config_digest(root/'probe-image.tar')
        inputs['probe'] = {'image': image, 'test_image_lock': {'schema_version': 1,
            'test_images': {'proc_transfer_probe': {'local_tag': image, 'oci_digest': 'sha256:local',
            'config_digest': config, 'anonymous_pull_verified': False, 'distribution': 'local-archive-import-only'}}}}
        inputs['image_config_digests'][image] = config
        inputs['image_archives'][image] = {'file': 'probe-image.tar', 'sha256': build.file_sha256(root/'probe-image.tar')}
        (root/'images.json').write_text(json.dumps(inputs))
        return inputs

    def test_anonymous_proof_checks_original_manifest_config_and_every_blob(self):
        class Response(io.BytesIO):
            def __init__(self, data=b'', headers=None, status=200):
                super().__init__(data); self.headers=headers or {}; self.status=status
        manifest=json.dumps({'config': {'digest': 'sha256:config', 'size': 2},
                             'layers': [{'digest': 'sha256:layer', 'size': 3}]}).encode()
        sha='sha256:'+hashlib.sha256(manifest).hexdigest()
        for failure in ('none', 'manifest', 'config', 'blob'):
            calls=[]
            def request(url, **kwargs):
                calls.append(url)
                if isinstance(url, str): return Response(b'{"token":"anonymous"}')
                if '/manifests/' in url.full_url:
                    return Response(manifest, {'Docker-Content-Digest': 'sha256:wrong' if failure=='manifest' else sha})
                size = 2 if url.full_url.endswith('sha256:config') else 3
                return Response(headers={'Content-Length': str(size)}, status=403 if failure=='blob' else 200)
            with self.subTest(failure=failure), mock.patch.object(publish.urllib.request, 'urlopen', side_effect=request):
                if failure=='none':
                    self.assertEqual(publish.anonymous_digest('ghcr.io/example/probe:fixed', expected_config='sha256:config'), sha)
                    self.assertEqual(len(calls), 4)
                    self.assertTrue(all(call.get_method()=='HEAD' for call in calls[-2:]))
                else:
                    with self.assertRaises(RuntimeError):
                        publish.anonymous_digest('ghcr.io/example/probe:fixed', expected_config='sha256:other' if failure=='config' else 'sha256:config')

    def test_optional_probe_stages_and_restores_original_archive_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); inputs = self.probe_inputs(root)
            with mock.patch.object(stage, 'find_release', return_value=None), mock.patch.object(stage.subprocess, 'run') as run:
                stage.stage('example/repo', 'v0.1.0', root, {'preflight': True, 'inputs': inputs})
                self.assertIn(str(root/'probe-image.tar'), run.call_args_list[1].args[0])
                self.assertIn(str(root/'sources.tar.gz'), run.call_args_list[1].args[0])
                self.assertEqual(run.call_args_list[-1].args[0][-1], str(root/'images.json'))
            with mock.patch.object(reuse.subprocess, 'run') as run:
                reuse.restore_images(inputs, root)
                self.assertEqual(run.call_args_list[-1].args[0], ['docker', 'load', '-i', str(root/'probe-image.tar')])
            (root/'probe-image.tar').write_bytes(b'corrupt')
            with mock.patch.object(reuse.subprocess, 'run') as run:
                with self.assertRaises(RuntimeError): reuse.restore_images(inputs, root)
                run.assert_not_called()

    def test_probe_public_lock_only_after_all_anonymous_proofs_and_immutable_preflight(self):
        with tempfile.TemporaryDirectory() as work:
            inputs = self.probe_inputs(Path(work))
            def inspect(args, **kwargs):
                return '[]' if args[1] == 'info' else inputs['image_config_digests'][args[-1]]
            with mock.patch.object(publish, 'existing_manifest', return_value=None), mock.patch.object(publish.subprocess, 'check_output', side_effect=inspect), mock.patch.object(publish.subprocess, 'run') as push, mock.patch.object(publish, 'anonymous_digest', return_value='sha256:'+'a'*64) as proof:
                result = publish.publish_images(inputs)
                self.assertEqual(push.call_count, 4); self.assertEqual(proof.call_count, 4)
                public = result['test_image_lock']['test_images']['proc_transfer_probe']
                self.assertEqual(public['image'], inputs['probe']['image'].split(':')[0]+'@sha256:'+'a'*64)
                self.assertTrue(public['anonymous_pull_verified']); self.assertEqual(public['distribution'], 'public-registry')
                self.assertNotIn('local_tag', public)
                self.assertFalse(inputs['probe']['test_image_lock']['test_images']['proc_transfer_probe']['anonymous_pull_verified'])
                proof.side_effect = ['sha256:'+'a'*64]*3 + [RuntimeError('anonymous blob denied')]
                with self.assertRaisesRegex(RuntimeError, 'blob denied'): publish.publish_images(inputs)
            with mock.patch.object(publish, 'existing_manifest', side_effect=[None]*3+[{'config': {'digest': 'sha256:wrong'}}]), mock.patch.object(publish.subprocess, 'check_output', side_effect=inspect), mock.patch.object(publish.subprocess, 'run') as push:
                with self.assertRaisesRegex(RuntimeError, 'Immutable'): publish.publish_images(inputs)
                push.assert_not_called()

    def test_probe_source_offering_is_opt_in_and_complete_before_archive(self):
        with tempfile.TemporaryDirectory() as work:
            out = Path(work); (out/'evidence/sources').mkdir(parents=True)
            (out/'evidence/project').mkdir(); lock=out/'evidence/project/versions.lock.json'; lock.write_text('{}')
            (out/'evidence/build-metadata.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError, 'probe corresponding'): build.source_archive(out, lock, include_probe=True)
            source=out/'validation/corresponding-source'; source.mkdir(parents=True)
            (source/'package.dsc').write_text('fixed source')
            (out/'validation/test-images.lock.json').write_text('{}')
            build.source_archive(out, lock, include_probe=True)
            with tarfile.open(out/'sources.tar.gz') as archive:
                self.assertEqual(archive.extractfile('sources/probe/package.dsc').read(), b'fixed source')
                self.assertIn('probe-build-inputs.json', archive.getnames())
            build.source_archive(out, lock)
            with tarfile.open(out/'sources.tar.gz') as archive: self.assertNotIn('sources/probe', archive.getnames())

    def test_provenance_bytes_ignore_nested_dictionary_enumeration_order(self):
        original={'source_files':{'second':'b','first':'a'},'inputs':{'talos':'1.12.6','architecture':'amd64'}}
        reordered={'inputs':dict(reversed(list(original['inputs'].items()))),'source_files':dict(reversed(list(original['source_files'].items())))}
        self.assertEqual(official.stable_json(original).encode(),official.stable_json(reordered).encode())
        self.assertEqual(json.loads(official.stable_json(original)),original)
    def original_archives(self,directory):
        images=['ghcr.io/example/'+name+':v0.1.0-talos-v1.12.6' for name in ('extension','standard','vmware')]
        inputs={'extension':{'image':images[0],'oci_digest':'sha256:local'},'installers':[{'image':i,'extensions':['oci-layout@sha256:local']} for i in images[1:]],'image_config_digests':{},'image_archives':{}}
        for image,filename in zip(images,stage.ARCHIVES):
            archive=directory/filename
            with tarfile.open(archive,'w') as tf:
                for name,data in [('manifest.json',json.dumps([{'RepoTags':[image],'Config':'config.json','Layers':[]}]).encode()),('config.json',json.dumps({'architecture':'amd64','image':image}).encode())]:
                    member=tarfile.TarInfo(name);member.size=len(data);tf.addfile(member,io.BytesIO(data))
            inputs['image_config_digests'][image]=build.archive_config_digest(archive)
            inputs['image_archives'][image]={'file':filename,'sha256':build.file_sha256(archive)}
        (directory/'images.json').write_text(json.dumps(inputs))
        (directory/'sources.tar.gz').write_bytes(b'sources')
        (directory/'evidence').mkdir();(directory/'evidence/build-metadata.json').write_text('{}')
        return inputs

    def test_preflight_does_not_push_or_check_anonymous_access(self):
        with tempfile.TemporaryDirectory() as d:
            inputs=self.original_archives(Path(d))
            with mock.patch.object(publish,'existing_manifest',return_value=None),mock.patch.object(publish.subprocess,'check_output',side_effect=lambda args,**kw:'[]' if args[1]=='info' else inputs['image_config_digests'][args[-1]]),mock.patch.object(publish.subprocess,'run') as push,mock.patch.object(publish,'anonymous_digest') as anonymous:
                self.assertEqual(publish.publish_images(inputs,preflight_only=True),{'preflight':True,'inputs':inputs})
                push.assert_not_called();anonymous.assert_not_called()

    def test_failed_or_stale_preflight_cannot_create_release(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);inputs=self.original_archives(root)
            for preflight in ({'preflight':False,'inputs':inputs},{'preflight':True,'inputs':{}}):
                with mock.patch.object(stage.subprocess,'run') as mutate,mock.patch.object(stage,'find_release') as lookup:
                    with self.assertRaises(RuntimeError):stage.stage('example/repo','v0.1.0',root,preflight)
                    mutate.assert_not_called();lookup.assert_not_called()

    def test_stage_retains_all_archives_before_original_inputs_marker(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);inputs=self.original_archives(root)
            with mock.patch.object(stage,'find_release',return_value=None),mock.patch.object(stage.subprocess,'run') as run:
                stage.stage('example/repo','v0.1.0',root,{'preflight':True,'inputs':inputs})
                commands=[call.args[0] for call in run.call_args_list]
                self.assertIn('--draft',commands[0])
                for filename in stage.ARCHIVES:self.assertIn(str(root/filename),commands[1])
                self.assertNotIn(str(root/'images.json'),commands[1])
                self.assertEqual(commands[-1],['gh','release','upload','v0.1.0',str(root/'images.json')])

    def test_only_unmarked_draft_can_replace_partial_assets(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);inputs=self.original_archives(root);proof={'preflight':True,'inputs':inputs}
            with mock.patch.object(stage,'find_release',return_value={'draft':True,'assets':[]}),mock.patch.object(stage.subprocess,'run') as run:
                stage.stage('example/repo','v0.1.0',root,proof)
                self.assertIn('--clobber',run.call_args_list[0].args[0])
            for release in ({'draft':False,'assets':[]},{'draft':True,'assets':[{'name':'images.json'}]}):
                with mock.patch.object(stage,'find_release',return_value=release),mock.patch.object(stage.subprocess,'run') as run:
                    with self.assertRaises(RuntimeError):stage.stage('example/repo','v0.1.0',root,proof)
                    run.assert_not_called()

    def test_restore_checks_all_original_archives_before_any_load(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);inputs=self.original_archives(root)
            with mock.patch.object(reuse.subprocess,'run') as run:
                reuse.restore_images(inputs,root)
                self.assertEqual([c.args[0] for c in run.call_args_list],[['docker','load','-i',str(root/f)] for f in stage.ARCHIVES])
            with (root/stage.ARCHIVES[-1]).open('ab') as corrupt:corrupt.write(b'corruption')
            with mock.patch.object(reuse.subprocess,'run') as run:
                with self.assertRaisesRegex(RuntimeError,'checksum'):reuse.restore_images(inputs,root)
                run.assert_not_called()

    def test_partial_publication_pushes_only_missing_original_tags(self):
        with tempfile.TemporaryDirectory() as d:
            inputs=self.original_archives(Path(d));images=list(inputs['image_config_digests'])
            with mock.patch.object(publish,'existing_manifest',side_effect=[{'config':{'digest':inputs['image_config_digests'][images[0]]}},None,None]),mock.patch.object(publish.subprocess,'check_output',side_effect=lambda args,**kw:'[]' if args[1]=='info' else inputs['image_config_digests'][args[-1]]),mock.patch.object(publish.subprocess,'run') as push,mock.patch.object(publish,'anonymous_digest',return_value='sha256:'+'a'*64):
                publish.publish_images(inputs)
                self.assertEqual([c.args[0] for c in push.call_args_list],[['docker','push',i] for i in images[1:]])
    def test_catalogue_rejects_official_image_not_in_locked_release(self):
        lock=json.loads((ROOT/'versions.lock.json').read_text())
        entries='\n'.join(lock['extensions'].values())
        official.verify_catalogue(lock,entries)
        lock['extensions']['iscsi-tools']='unrelated@sha256:'+'0'*64
        with self.assertRaises(RuntimeError):official.verify_catalogue(lock,entries)

    def test_version_tag_collision_prevents_all_pushes(self):
        images=['ghcr.io/example/'+name+':v0.1.0-talos-v1.12.6' for name in ['extension','standard','vmware']]
        inputs={'extension':{'image':images[0]},'installers':[{'image':i} for i in images[1:]],'image_config_digests':{i:'sha256:new' for i in images}}
        with mock.patch.object(publish,'existing_manifest',side_effect=[None,{'config':{'digest':'sha256:old'}}]),mock.patch.object(publish.subprocess,'check_output',side_effect=lambda args,**kw:'[]' if args[1]=='info' else 'sha256:new'),mock.patch.object(publish.subprocess,'run') as push:
            with self.assertRaises(RuntimeError):publish.publish_images(inputs)
            push.assert_not_called()

    def test_first_private_package_does_not_prevent_other_pushes(self):
        images=['ghcr.io/example/'+name+':v0.1.0-talos-v1.12.6' for name in ['extension','standard','vmware']]
        inputs={'extension':{'image':images[0]},'installers':[{'image':i} for i in images[1:]],'image_config_digests':{i:'sha256:config' for i in images}}
        events=[]
        def anonymous(image, **kwargs):
            events.append(('anonymous',image))
            raise publish.urllib.error.HTTPError(image,403,'private package',{},None)
        with mock.patch.object(publish,'existing_manifest',return_value=None),mock.patch.object(publish.subprocess,'check_output',side_effect=lambda args,**kw:'[]' if args[1]=='info' else 'sha256:config'),mock.patch.object(publish.subprocess,'run',side_effect=lambda args,**kw:events.append(('push',args[-1]))),mock.patch.object(publish,'anonymous_digest',side_effect=anonymous):
            with self.assertRaises(publish.urllib.error.HTTPError):publish.publish_images(inputs)
        self.assertEqual(events,[*(('push',image) for image in images),('anonymous',images[0])])

    def test_missing_manifest_and_auth_failure_are_distinguished(self):
        for error in ('manifest unknown','name unknown','no such manifest: test'):
            with mock.patch.object(publish.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'',error)):
                self.assertIsNone(publish.existing_manifest('test'))
        for error in ('unauthorized: manifest unknown','denied','connection timeout','unexpected server error'):
            with mock.patch.object(publish.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'',error)):
                with self.assertRaises(RuntimeError):publish.existing_manifest('test')

    def test_containerd_store_fails_before_registry_or_push(self):
        images=['ghcr.io/example/'+name+':v0.1.0-talos-v1.12.6' for name in ['extension','standard','vmware']]
        inputs={'extension':{'image':images[0]},'installers':[{'image':i} for i in images[1:]]}
        with mock.patch.object(publish.subprocess,'check_output',return_value='[["driver-type","io.containerd.snapshotter.v1"]]'),mock.patch.object(publish,'existing_manifest') as inspect,mock.patch.object(publish.subprocess,'run') as push:
            with self.assertRaisesRegex(RuntimeError,'classic Docker image store'):publish.publish_images(inputs)
            inspect.assert_not_called();push.assert_not_called()

    def test_original_release_verification_never_rebuilds_or_pushes(self):
        images=['ghcr.io/example/'+name+':v0.1.0-talos-v1.12.6' for name in ['extension','standard','vmware']]
        inputs={'extension':{'image':images[0],'oci_digest':'sha256:local'},'installers':[{'image':i,'extensions':['oci-layout@sha256:local']} for i in images[1:]],'image_config_digests':{i:'sha256:config' for i in images}}
        with mock.patch.object(publish,'existing_manifest',return_value={'config':{'digest':'sha256:config'}}),mock.patch.object(publish,'anonymous_digest',return_value='sha256:'+'a'*64),mock.patch.object(publish.subprocess,'run') as push,mock.patch.object(publish.subprocess,'check_output') as inspect:
            self.assertEqual(len(publish.publish_images(inputs,True)['images']),3)
            push.assert_not_called();inspect.assert_not_called()

    def test_selected_lock_is_archived_and_verified(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'project';root.mkdir()
            for name in ('supervisor','build'): (root/name).mkdir()
            (root/'LICENSE').write_text('MIT')
            (root/'versions.lock.json').write_text('{"default":true}')
            selected=Path(d)/'custom.json';selected.write_text('{"custom":true}')
            out=Path(d)/'out';(out/'evidence/sources').mkdir(parents=True)
            metadata={'source_inputs_sha256':{'versions.lock.json':hashlib.sha256(selected.read_bytes()).hexdigest()}}
            (out/'evidence/build-metadata.json').write_text(json.dumps(metadata))
            (out/'evidence/project').mkdir()
            (out/'evidence/project/versions.lock.json').write_bytes(selected.read_bytes())
            with mock.patch.object(build,'ROOT',root): build.source_archive(out,selected)
            with tarfile.open(out/'sources.tar.gz') as tf:
                self.assertEqual(tf.extractfile('project/versions.lock.json').read(),selected.read_bytes())
                for member in tf:
                    self.assertEqual((member.uid,member.gid,member.uname,member.gname,member.mtime),(0,0,'root','root',0))
                    self.assertEqual(member.pax_headers,{})
            with mock.patch.object(validation,'ROOT',root):
                validation.verify_source_inputs(metadata,selected)
                with self.assertRaises(RuntimeError): validation.verify_source_inputs(metadata,root/'versions.lock.json')

    def test_published_composition_uses_registry_digest(self):
        local='sha256:'+'a'*64
        remote='ghcr.io/example/extension@sha256:'+'b'*64
        inputs={'extension':{'oci_digest':local},'installers':[
            {'extensions':['oci-layout@'+local,'official@sha256:'+'c'*64]},
            {'extensions':['oci-layout@'+local,'official@sha256:'+'d'*64]}]}
        resolved=publish.published_inputs(inputs,remote)
        self.assertEqual(resolved['extension']['digestRef'],remote)
        for before,after in zip(inputs['installers'],resolved['installers']):
            self.assertEqual(after['extensions'][0],remote)
            self.assertEqual(after['extensions'][1:],before['extensions'][1:])
            self.assertEqual(before['extensions'][0],'oci-layout@'+local)
        inputs['installers'][0]['extensions'][0]='unrelated@'+local
        with self.assertRaises(RuntimeError): publish.published_inputs(inputs,remote)

    def test_variants_share_locked_baseline(self):
        lock = json.loads((ROOT/'versions.lock.json').read_text())
        standard = build.composition(lock, 'custom@sha256:'+'a'*64, 'standard')
        vmware = build.composition(lock, 'custom@sha256:'+'a'*64, 'vmware')
        self.assertEqual(vmware['extensions'][:-1], standard['extensions'])
        self.assertIn('vmtoolsd-guest-agent', vmware['extensions'][-1])
        lock['standard_extensions'].append('vmtoolsd-guest-agent')
        with self.assertRaises(ValueError): build.composition(lock, 'custom', 'standard')

    def test_retag_archive_preserves_content(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d)/'source.tar', Path(d)/'target.tar'
            with tarfile.open(src, 'w') as tf:
                for name, content in [('manifest.json', json.dumps([{'RepoTags':['ghcr.io/siderolabs/installer-base:v1.12.6'],'Config':'config.json','Layers':[]}]).encode()), ('config.json', b'{"architecture":"amd64"}')]:
                    member = tarfile.TarInfo(name); member.size=len(content); tf.addfile(member,io.BytesIO(content))
            build.retag_archive(src, dst, 'ghcr.io/example/installer:v0.1.0-talos-v1.12.6')
            self.assertEqual(build.archive_config_digest(dst),'sha256:'+hashlib.sha256(b'{"architecture":"amd64"}').hexdigest())
            with tarfile.open(dst) as tf:
                self.assertEqual(json.load(tf.extractfile('manifest.json'))[0]['RepoTags'], ['ghcr.io/example/installer:v0.1.0-talos-v1.12.6'])
                self.assertEqual(tf.extractfile('config.json').read(), b'{"architecture":"amd64"}')
            digest = build.docker_to_oci(dst, Path(d)/'oci')
            self.assertEqual(json.loads((Path(d)/'oci/index.json').read_text())['manifests'][0]['digest'], digest)

    @unittest.skipUnless(shutil.which('talosctl') or os.environ.get('TALOSCTL_PATH'), 'requires checksum-locked talosctl')
    def test_talos_parser_replaces_entry_in_single_and_multi_document_configs(self):
        binary=os.environ.get('TALOSCTL_PATH') or shutil.which('talosctl')
        files=[{'path':'/var/other','permissions':420,'op':'create','content':'unchanged'},
               {'path':'/etc/cri/conf.d/20-customization.part','permissions':384,'op':'create','content':''}]
        original={'version':'v1alpha1','machine':{'type':'worker','files':files}}
        extra='---\napiVersion: v1alpha1\nkind: HostnameConfig\nhostname: test-host\n'
        patch=runtime.machine_file_patch('',files)
        with tempfile.TemporaryDirectory() as d:
            config=Path(d)/'config.yaml';patchfile=Path(d)/'patch.json'
            patchfile.write_text(json.dumps(patch))
            for suffix in ('',extra):
                config.write_text(json.dumps(original)+'\n'+suffix)
                result=subprocess.run([binary,'machineconfig','patch',str(config),'--patch','@'+str(patchfile)],capture_output=True,text=True,check=True)
                # Use another strategic delete as a semantic assertion: Talos
                # fails a selector that cannot find its preserved other entry.
                other={'machine':{'files':[{'path':'/var/other','$patch':'delete'}]}}
                patched=Path(d)/'patched.yaml';patched.write_text(result.stdout)
                subprocess.run([binary,'machineconfig','patch',str(patched),'--patch',json.dumps(other)],capture_output=True,text=True,check=True)
                self.assertEqual(result.stdout.count('path: /etc/cri/conf.d/20-customization.part'),1)
                self.assertIn('content: unchanged',result.stdout)
                self.assertIn('sysbox-runc',result.stdout)
                self.assertIn('permissions: 0o600',result.stdout)
                if suffix:self.assertIn('kind: HostnameConfig',result.stdout)

    def test_runtime_merge_preserves_default_and_other_handlers(self):
        existing = '''[plugins."io.containerd.cri.v1.runtime".containerd]
default_runtime_name="runc"
[plugins."io.containerd.cri.v1.runtime".containerd.runtimes.runc.options]
SystemdCgroup=true
[plugins."io.containerd.cri.v1.runtime".containerd.runtimes.other.options]
BinaryName="/other"
'''
        result = tomllib.loads(runtime.merge(existing))['plugins']['io.containerd.cri.v1.runtime']['containerd']
        self.assertEqual(result['default_runtime_name'], 'runc')
        self.assertEqual(result['runtimes']['other']['options']['BinaryName'], '/other')
        self.assertTrue(result['runtimes']['runc']['options']['SystemdCgroup'])
        self.assertFalse(result['runtimes']['sysbox-runc']['options']['SystemdCgroup'])
        with self.assertRaises(ValueError): runtime.merge(runtime.HANDLER)

    def test_existing_machine_file_is_replaced_not_appended(self):
        files=[{'path':'/var/other','content':'unchanged'},
               {'path':'/etc/cri/conf.d/20-customization.part','content':'','permissions':384}]
        patch=runtime.machine_file_patch('',files)
        self.assertEqual(patch['version'],'v1alpha1')
        self.assertEqual(patch['machine']['files'][0],{'path':'/etc/cri/conf.d/20-customization.part','$patch':'delete'})
        self.assertEqual(files[0]['content'],'unchanged')
        self.assertEqual(patch['machine']['files'][1]['op'],'create')
        self.assertEqual(patch['machine']['files'][1]['permissions'],384)
        with self.assertRaises(ValueError):runtime.machine_file_patch('',files+[files[1]])
        with self.assertRaises(ValueError):runtime.machine_file_patch('',files[:1])

    def test_stale_or_empty_fragment_cannot_discard_existing_handlers(self):
        existing='[plugins."io.containerd.cri.v1.runtime".containerd.runtimes.other.options]\nBinaryName="/other"\n'
        files=[{'path':'/etc/cri/conf.d/20-customization.part','content':existing}]
        for stale in ('',existing+'\n'):
            with self.assertRaisesRegex(ValueError,'exactly match'):runtime.machine_file_patch(stale,files)
        patch=runtime.machine_file_patch(existing,files)
        config=tomllib.loads(patch['machine']['files'][1]['content'])
        self.assertEqual(config['plugins']['io.containerd.cri.v1.runtime']['containerd']['runtimes']['other']['options']['BinaryName'],'/other')

if __name__ == '__main__': unittest.main()
