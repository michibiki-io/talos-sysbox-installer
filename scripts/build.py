#!/usr/bin/env python3
"""Build amd64 images and immutable composition records. No registry needed locally."""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kw):
    return subprocess.run(args, check=True, **kw)


def composition(lock, custom, variant):
    names = list(lock['standard_extensions'])
    if 'vmtoolsd-guest-agent' in names:
        raise ValueError('standard must not contain the VMware agent')
    if len(set(names)) != len(names):
        raise ValueError('duplicate extension')
    if variant == 'vmware':
        names.append('vmtoolsd-guest-agent')
    images = [lock['extensions'][name] for name in names]
    if not all(re.fullmatch(r'.+@sha256:[0-9a-f]{64}', x) for x in images):
        raise ValueError('official extension must be digest pinned')
    return {'variant': variant, 'architecture': lock['architecture'], 'talos': lock['talos'], 'sysbox': lock['sysbox'], 'imager': lock['imager'], 'base_installer': lock['base_installer'], 'catalogue': lock['catalogue'], 'extensions': [custom, *images]}


def docker_to_oci(archive, output):
    output.mkdir()
    blobs = output / 'blobs/sha256'
    blobs.mkdir(parents=True)
    def blob(data, media):
        sha = hashlib.sha256(data).hexdigest()
        (blobs / sha).write_bytes(data)
        return {'mediaType': media, 'digest': 'sha256:'+sha, 'size': len(data)}
    with tarfile.open(archive) as tf:
        entry, = json.load(tf.extractfile('manifest.json'))
        config = blob(tf.extractfile(entry['Config']).read(), 'application/vnd.oci.image.config.v1+json')
        layers = [blob(tf.extractfile(p).read(), 'application/vnd.oci.image.layer.v1.tar') for p in entry['Layers']]
    manifest = blob(json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'config': config, 'layers': layers}, separators=(',', ':')).encode(), 'application/vnd.oci.image.manifest.v1+json')
    manifest['platform'] = {'architecture': 'amd64', 'os': 'linux'}
    (output / 'index.json').write_text(json.dumps({'schemaVersion':2,'manifests':[manifest]}))
    (output / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
    return manifest['digest']


def retag_archive(source, target, image):
    # imager names the archive after the base image. Rewrite only RepoTags
    # before docker load so no SideroLabs tag is overwritten or pushed.
    with tarfile.open(source) as src, tarfile.open(target, 'w') as dst:
        for m in src:
            stream = src.extractfile(m) if m.isfile() else None
            if m.name == 'manifest.json':
                entries = json.load(stream)
                if len(entries) != 1:
                    raise ValueError('expected exactly one installer image')
                entries[0]['RepoTags'] = [image]
                data = json.dumps(entries).encode()
                m.size = len(data)
                stream = io.BytesIO(data)
            dst.addfile(m, stream)


def source_archive(out, selected_lock):
    with tarfile.open(out/'sources.tar.gz','w:gz') as tf:
        tf.add(out/'evidence/sources', arcname='sources')
        tf.add(out/'evidence/build-metadata.json', arcname='build-metadata.json')
        project=out/'evidence/project'
        if hashlib.sha256((project/'versions.lock.json').read_bytes()).digest()!=hashlib.sha256(selected_lock.read_bytes()).digest():
            raise RuntimeError('Selected lock changed after the isolated build')
        tf.add(project,arcname='project')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--version', default='0.1.0')
    p.add_argument('--registry-prefix', default='ghcr.io/michibiki-io')
    p.add_argument('--builder', default=os.environ.get('BUILDX_BUILDER', 'default'))
    p.add_argument('--network', choices=('default','host'), default='default')
    p.add_argument('--output', type=Path, default=ROOT / '.build')
    p.add_argument('--lock', type=Path, default=ROOT / 'versions.lock.json')
    p.add_argument('--extension-only', action='store_true')
    a = p.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', a.version):
        p.error('version must be project semantic version without v')
    lock = json.loads(a.lock.read_text())
    if lock['architecture'] != 'amd64':
        p.error('only amd64 is supported')
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out/'images.json').unlink(missing_ok=True)
    tag = f'v{a.version}-talos-v{lock["talos"]}'
    extension = a.registry_prefix+'/talos-sysbox-extension:'+tag
    # An allowlisted staging context physically excludes configs/logs even if
    # a caller supplies a different .dockerignore or stores secrets nearby.
    with tempfile.TemporaryDirectory(prefix='sysbox-build-') as tmp:
        context = Path(tmp)
        for name in ('build', 'supervisor'):
            shutil.copytree(ROOT / name, context / name, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (context/'scripts').mkdir()
        for name in ('build.py','publish.py'):shutil.copy2(ROOT/'scripts'/name,context/'scripts'/name)
        shutil.copy2(a.lock, context / 'versions.lock.json')
        shutil.copy2(ROOT / 'LICENSE', context / 'LICENSE')
        common = ['docker','buildx','build','--builder',a.builder,'--network',a.network,'--platform','linux/amd64','--build-arg','GO_IMAGE='+lock['go'],'--build-arg','UBUNTU_IMAGE='+lock['ubuntu'],'--build-arg','APT_SNAPSHOT='+lock['apt_snapshot'],'--build-arg','CATALOGUE_IMAGE='+lock['catalogue'],'-f',str(context/'build/Dockerfile')]
        run([*common,'--target','evidence','--output','type=local,dest='+str(out/'evidence'),str(context)])
        run([*common,'--target','extension','--load','-t',extension,str(context)])
    run(['docker','save','-o',str(out/'extension.tar'),extension])
    oci = out / 'extension-oci'
    if oci.exists(): shutil.rmtree(oci)
    digest = docker_to_oci(out/'extension.tar', oci)
    record = {'tag':tag, 'extension':{'image':extension, 'oci_digest':digest}, 'installers':[]}
    source_archive(out,a.lock)
    if not a.extension_only:
        for variant in ('standard','vmware'):
            image = a.registry_prefix+'/talos-sysbox-installer-'+variant+':'+tag
            # This is the manifest of our local uncompressed OCI layout, not
            # the compressed manifest later published by docker push.
            inputs = composition(lock, 'oci-layout@'+digest, variant)
            destination = out / variant
            destination.mkdir(exist_ok=True)
            # Talos supports OCIPath in a custom profile. This avoids insecure
            # local registries while keeping official extensions as image refs.
            profile = {'baseProfileName':'installer','arch':'amd64','platform':'metal','input':{'baseInstaller':{'imageRef':lock['base_installer']},'systemExtensions':[{'ociPath':'/input/extension-oci'}, *({'imageRef':ref} for ref in inputs['extensions'][1:])]}}
            # JSON is a YAML subset accepted by imager.
            profile_path = out / (variant+'.profile.yaml')
            profile_path.write_text(json.dumps(profile))
            # For a complete stdin profile, pinned Talos ignores input flags.
            # Keep the profile as the single authoritative input path.
            args = ['docker','run','--rm','-i','--user',f'{os.getuid()}:{os.getgid()}','--network',a.network,'-v',str(out)+':/input:ro','-v',str(destination)+':/out',lock['imager'],'-']
            run(args,input=json.dumps(profile).encode())
            retag_archive(destination/'installer-amd64.tar', destination/'image.tar', image)
            run(['docker','load','-i',str(destination/'image.tar')])
            inputs['image'] = image
            (destination/'inputs.json').write_text(json.dumps(inputs,indent=2)+'\n')
            record['installers'].append(inputs)
    record['image_config_digests']={image:subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',image],text=True).strip() for image in [extension,*(i['image'] for i in record['installers'])]}
    (out/'images.json').write_text(json.dumps(record,indent=2)+'\n')


if __name__ == '__main__': main()
