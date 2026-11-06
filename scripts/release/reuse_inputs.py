#!/usr/bin/env python3
"""On rerun, use the original published release's inputs without a rebuild."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from stage_release import ARCHIVES, find_release, validate_archives, targets, asset_names
from disk_artifacts import disk_record, disk_paths


def restore_images(inputs,directory):
    paths=validate_archives(inputs,directory)
    for arch, record in (targets(inputs) or {'':inputs}).items():
        disk_paths(record,directory/arch)
    # Validate all archives before the first Docker mutation.
    for path in paths:subprocess.run(['docker','load','-i',str(path)],check=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--tag',required=True)
    parser.add_argument('--inspect-only', action='store_true')
    a=parser.parse_args()
    # A tag can exist before assets or publication. Unknown gh errors fail closed.
    release=find_release(os.environ['GITHUB_REPOSITORY'],a.tag)
    names={asset['name'] for asset in release['assets']} if release else set()
    published=bool(release and 'release-images.json' in names)
    staged=bool(release and 'images.json' in names)
    if release and not staged and not release['draft']:
        raise RuntimeError('Public release is missing original inputs; refusing replacement')
    if staged and not a.inspect_only:
        Path('.build').mkdir(exist_ok=True)
        command=['gh','release','download',a.tag,'--pattern','images.json','--dir','.build']
        if published:command+=['--pattern','release-images.json']
        subprocess.run(command,check=True)
        inputs=json.loads(Path('.build/images.json').read_text())
        if not set(asset_names(inputs)) <= names:
            raise RuntimeError('Original release is missing matching source/provenance assets')
        # Even a completed release must retain its exact original disk bytes.
        # This has no effect on pre-Oracle releases and never rebuilds an asset.
        for arch, record in (targets(inputs) or {'': inputs}).items():
            disks = disk_record(record)
            if disks is not None:
                destination = Path('.build')/arch/'oracle'
                destination.mkdir(parents=True,exist_ok=True)
                for filename in disks['files']:
                    subprocess.run(['gh','release','download',a.tag,'--pattern',filename,'--dir',str(destination)],check=True)
                disk_paths(record,Path('.build')/arch)
        if not published:
            records = targets(inputs)
            for arch, record in (records or {'': inputs}).items():
                directory = Path('.build') / arch
                directory.mkdir(parents=True, exist_ok=True)
                for filename in [*ARCHIVES, *(['probe-image.tar'] if 'probe' in record else [])]:
                    asset = arch + '-' + filename if arch else filename
                    subprocess.run(['gh','release','download',a.tag,'--pattern',asset,'--dir',str(directory)],check=True)
                    if arch:
                        (directory / asset).rename(directory / filename)
        if not published:restore_images(inputs,Path('.build'))
    with open(os.environ['GITHUB_OUTPUT'],'a') as output:
        output.write('reuse='+str(staged).lower()+'\ncompleted='+str(published).lower()+'\n')


if __name__=='__main__':main()
