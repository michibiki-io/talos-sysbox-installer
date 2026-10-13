#!/usr/bin/env python3
"""On rerun, use the original published release's inputs without a rebuild."""
import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--tag',required=True)
    a=parser.parse_args()
    # A tag can exist before assets or publication. Unknown gh errors fail closed.
    releases=json.loads(subprocess.check_output(['gh','api','--paginate','--slurp',f'repos/{os.environ["GITHUB_REPOSITORY"]}/releases'],text=True))
    release=next((r for page in releases for r in page if r['tag_name']==a.tag),None)
    names={asset['name'] for asset in release['assets']} if release else set()
    published=bool(release and 'release-images.json' in names)
    staged=bool(release and 'images.json' in names)
    if staged:
        if not {'images.json','build-metadata.json','sources.tar.gz'}<=names:
            raise RuntimeError('Original release is missing matching source/provenance assets')
        Path('.build').mkdir(exist_ok=True)
        command=['gh','release','download',a.tag,'--pattern','images.json','--dir','.build']
        if published:command+=['--pattern','release-images.json']
        subprocess.run(command,check=True)
    with open(os.environ['GITHUB_OUTPUT'],'a') as output:output.write('reuse='+str(staged).lower()+'\n')


if __name__=='__main__':main()
