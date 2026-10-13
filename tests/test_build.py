import importlib.util
import hashlib
import io
import json
import tarfile
import tempfile
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

class BuildTests(unittest.TestCase):
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
            with mock.patch.object(build,'ROOT',root): build.source_archive(out,selected)
            with tarfile.open(out/'sources.tar.gz') as tf:
                self.assertEqual(tf.extractfile('project/versions.lock.json').read(),selected.read_bytes())
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

if __name__ == '__main__': unittest.main()
