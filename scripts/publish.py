#!/usr/bin/env python3
"""Push built release images and verify anonymous registry access."""
import argparse
import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path


def anonymous_digest(image):
    host, path=image.split('/',1)
    repo,tag=path.rsplit(':',1)
    scope='repository:'+repo+':pull'
    token_url='https://'+host+'/token?'+urllib.parse.urlencode({'service':host,'scope':scope})
    token=json.load(urllib.request.urlopen(token_url,timeout=30))['token']
    req=urllib.request.Request('https://'+host+'/v2/'+repo+'/manifests/'+tag,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'})
    with urllib.request.urlopen(req,timeout=30) as response:
        manifest=json.load(response)
        digest=response.headers['Docker-Content-Digest']
    # Prove anonymous access to every blob, not only a public tag/manifest.
    for blob in [manifest['config'], *manifest['layers']]:
        req=urllib.request.Request('https://'+host+'/v2/'+repo+'/blobs/'+blob['digest'],headers={'Authorization':'Bearer '+token},method='HEAD')
        with urllib.request.urlopen(req,timeout=30) as response:
            if response.status!=200 or int(response.headers['Content-Length'])!=blob['size']:
                raise RuntimeError('anonymous blob access failed')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',digest): raise RuntimeError('missing registry digest')
    return digest


def main():
    p=argparse.ArgumentParser(); p.add_argument('--images',type=Path,required=True); p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(); inputs=json.loads(a.images.read_text())
    images=[inputs['extension']['image'],*(i['image'] for i in inputs['installers'])]
    if len(images)!=3: raise RuntimeError('release requires extension and both installers')
    records=[]
    for image in images:
        if not image.startswith('ghcr.io/') or not re.search(r':v\d+\.\d+\.\d+-talos-v\d+\.\d+\.\d+$',image):
            raise RuntimeError('unexpected release image name/tag')
        subprocess.run(['docker','push',image],check=True)
        # No Docker credentials or token supplied here; use an anonymous token.
        # A private newly-created GHCR package fails closed until its owner
        # explicitly makes it public. Never claim publication on private access.
        digest=anonymous_digest(image)
        records.append({'image':image,'digestRef':image.split(':')[0]+'@'+digest,'digest':digest,'anonymous_pull':True,'architecture':'amd64'})
    a.output.write_text(json.dumps({'images':records,'inputs':inputs,'licenses':'See build-metadata.json and /usr/local/share/licenses/talos-sysbox in the extension.','corresponding_source':'sources.tar.gz attached to this release contains the matching upstream and Ubuntu package sources and packaging patches.'},indent=2)+'\n')


if __name__=='__main__': main()
