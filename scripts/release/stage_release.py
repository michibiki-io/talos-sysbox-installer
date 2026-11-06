#!/usr/bin/env python3
"""Retain original archives and matching sources before any registry push."""
import argparse
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from build import archive_config_digest, file_sha256, archive_platform

ARCHIVES=('extension.tar','standard-image.tar','vmware-image.tar')


def targets(inputs):
    if 'architectures' not in inputs:
        return None
    records = inputs['architectures']
    if inputs.get('schema_version') != 2 or set(records) != {'amd64', 'arm64'}:
        raise RuntimeError('Expected both release architectures')
    for arch, record in records.items():
        if record.get('architecture') != arch or 'architectures' in record:
            raise RuntimeError('Release architecture record mismatch')
    return records


def asset_names(inputs):
    records = targets(inputs)
    if records is not None:
        return [arch + '-' + name for arch, record in records.items() for name in asset_names(record)]
    names = ['sources.tar.gz', 'build-metadata.json', *ARCHIVES]
    if 'probe' in inputs:
        names.append('probe-image.tar')
    return names


def find_release(repo,tag):
    pages=json.loads(subprocess.check_output(['gh','api','--paginate','--slurp',f'repos/{repo}/releases'],text=True))
    return next((r for page in pages for r in page if r['tag_name']==tag),None)


def validate_archives(inputs,directory):
    records = targets(inputs)
    if records is not None:
        return [path for arch, record in records.items() for path in validate_archives(record, directory / arch)]
    images=[inputs['extension']['image'],*(item['image'] for item in inputs['installers'])]
    if len(images)!=3 or len(set(images))!=3:raise RuntimeError('Expected three distinct original images')
    filenames=list(ARCHIVES)
    if 'probe' in inputs:
        images.append(inputs['probe']['image']); filenames.append('probe-image.tar')
    if len(set(images)) != len(images):raise RuntimeError('Duplicate original images')
    paths=[]
    for image,filename in zip(images,filenames):
        record=inputs['image_archives'][image]
        if record['file']!=filename:raise RuntimeError('Unexpected original archive name/order')
        path=directory/filename
        if file_sha256(path)!=record['sha256']:raise RuntimeError('Original image archive checksum mismatch')
        with tarfile.open(path) as archive:
            entry,=json.load(archive.extractfile('manifest.json'))
            if entry['RepoTags']!=[image]:raise RuntimeError('Original archive would load unexpected image tags')
        if archive_config_digest(path)!=inputs['image_config_digests'][image]:raise RuntimeError('Original archive configuration mismatch')
        archive_platform(path, inputs.get('architecture', 'amd64'))
        paths.append(path)
    return paths


def validate_assets(inputs,directory):
    """Validate complete local release assets without any remote operations."""
    records = targets(inputs)
    if records is not None:
        return [path for arch, record in records.items() for path in validate_assets(record, directory / arch)]
    paths=validate_archives(inputs,directory)
    assets=[directory/'sources.tar.gz',directory/'evidence/build-metadata.json',*paths]
    if any(not p.is_file() or p.stat().st_size>=2*1024**3 for p in assets):raise RuntimeError('Missing or oversized release asset')
    return assets


def stage(repo,tag,directory,preflight):
    inputs=json.loads((directory/'images.json').read_text())
    if preflight!={'preflight':True,'inputs':inputs}:raise RuntimeError('Successful preflight does not match these original inputs')
    assets=validate_assets(inputs,directory)
    records = targets(inputs)
    if records is not None:
        # Prefix every asset, including sources and metadata, before gh flattens
        # paths into one release. The original per-target inputs remain exact.
        export = directory / 'release-assets'
        export.mkdir(exist_ok=True)
        renamed = []
        for arch, record in records.items():
            for path in validate_assets(record, directory / arch):
                target = export / (arch + '-' + path.name)
                target.unlink(missing_ok=True)
                os.link(path, target)
                renamed.append(target)
        assets = renamed
    release=find_release(repo,tag)
    if release:
        names={a['name'] for a in release['assets']}
        if not release['draft'] or 'images.json' in names:raise RuntimeError('Refusing to replace original staged or public release assets')
        # An incomplete draft has never passed the final images.json marker;
        # this workflow cannot have pushed its binaries. Only it is replaceable.
        upload=['gh','release','upload',tag,'--clobber']
    else:
        subprocess.run(['gh','release','create',tag,'--draft','--title',tag,'--notes','Source assets staged; image publication pending.'],check=True)
        upload=['gh','release','upload',tag]
    subprocess.run([*upload,*(str(p) for p in assets)],check=True)
    # Marker last: a retry may only reuse inputs whose archives/sources exist.
    subprocess.run(['gh','release','upload',tag,str(directory/'images.json')],check=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--tag',required=True)
    parser.add_argument('--directory',type=Path,default=Path('.build'))
    parser.add_argument('--preflight',type=Path,required=True)
    args=parser.parse_args()
    stage(os.environ['GITHUB_REPOSITORY'],args.tag,args.directory,json.loads(args.preflight.read_text()))


if __name__=='__main__':main()
