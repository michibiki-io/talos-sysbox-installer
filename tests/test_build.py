import importlib.util
import hashlib
import io
import json
import tarfile
import tempfile
import os
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

def load_release(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts/release'/f'{name}.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

stage=load_release('stage_release')
reuse=load_release('reuse_inputs')

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
