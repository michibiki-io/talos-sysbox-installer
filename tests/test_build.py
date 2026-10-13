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

class BuildTests(unittest.TestCase):
    def test_catalogue_rejects_official_image_not_in_locked_release(self):
        lock=json.loads((ROOT/'versions.lock.json').read_text())
        entries='\n'.join(lock['extensions'].values())
        official.verify_catalogue(lock,entries)
        lock['extensions']['iscsi-tools']='unrelated@sha256:'+'0'*64
        with self.assertRaises(RuntimeError):official.verify_catalogue(lock,entries)

    def test_version_tag_collision_prevents_all_pushes(self):
        images=['ghcr.io/example/'+name+':v0.1.0-talos-v1.12.6' for name in ['extension','standard','vmware']]
        inputs={'extension':{'image':images[0]},'installers':[{'image':i} for i in images[1:]]}
        with mock.patch.object(publish,'existing_manifest',side_effect=[None,{'config':{'digest':'sha256:old'}}]),mock.patch.object(publish.subprocess,'check_output',return_value='sha256:new'),mock.patch.object(publish.subprocess,'run') as push:
            with self.assertRaises(RuntimeError):publish.publish_images(inputs)
            push.assert_not_called()

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

if __name__ == '__main__': unittest.main()
