#!/usr/bin/env python3
"""On rerun, use the original published release's inputs without a rebuild."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from stage_release import ARCHIVES, find_release, validate_archives


def restore_images(inputs,directory):
    paths=validate_archives(inputs,directory)
    # Validate all archives before the first Docker mutation.
    for path in paths:subprocess.run(['docker','load','-i',str(path)],check=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--tag',required=True)
    a=parser.parse_args()
    # A tag can exist before assets or publication. Unknown gh errors fail closed.
    release=find_release(os.environ['GITHUB_REPOSITORY'],a.tag)
    names={asset['name'] for asset in release['assets']} if release else set()
    published=bool(release and 'release-images.json' in names)
    staged=bool(release and 'images.json' in names)
    if release and not staged and not release['draft']:
        raise RuntimeError('Public release is missing original inputs; refusing replacement')
    if staged:
        if not {'images.json','build-metadata.json','sources.tar.gz',*ARCHIVES}<=names:
            raise RuntimeError('Original release is missing matching source/provenance assets')
        Path('.build').mkdir(exist_ok=True)
        command=['gh','release','download',a.tag,'--pattern','images.json','--dir','.build']
        if published:command+=['--pattern','release-images.json']
        else:
            for filename in ARCHIVES:command+=['--pattern',filename]
        subprocess.run(command,check=True)
        inputs=json.loads(Path('.build/images.json').read_text())
        if 'probe' in inputs:
            if 'probe-image.tar' not in names:raise RuntimeError('Original probe archive missing')
            if not published:
                subprocess.run(['gh','release','download',a.tag,'--pattern','probe-image.tar','--dir','.build'],check=True)
        if not published:restore_images(inputs,Path('.build'))
    with open(os.environ['GITHUB_OUTPUT'],'a') as output:
        output.write('reuse='+str(staged).lower()+'\ncompleted='+str(published).lower()+'\n')


if __name__=='__main__':main()
