#!/usr/bin/env python3
"""Push built release images and verify anonymous registry access."""
import argparse
import copy
import json
import re
import subprocess
import urllib.error
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


def published_inputs(inputs, extension_ref):
    """Resolve local composition to the anonymously verified registry digest."""
    result=copy.deepcopy(inputs)
    local='oci-layout@'+result['extension']['oci_digest']
    result['extension']['digestRef']=extension_ref
    for installer in result['installers']:
        if installer['extensions'][0]!=local:
            raise RuntimeError('installer does not reference the built local OCI layout')
        installer['extensions'][0]=extension_ref
    return result


def existing_manifest(image):
    # Use only the existing Docker login. Refuse unknown failures: an auth or
    # transport error cannot prove a tag is absent.
    result=subprocess.run(['docker','manifest','inspect',image],capture_output=True,text=True)
    if result.returncode==0:
        manifest=json.loads(result.stdout)
        if 'config' not in manifest:raise RuntimeError('Unexpected multi-platform release tag')
        return manifest
    if 'no such manifest:' in result.stderr.lower():return None
    raise RuntimeError('Cannot establish registry tag state; refusing publication')


def publish_images(inputs, verify_existing=False):
    images=[inputs['extension']['image'],*(i['image'] for i in inputs['installers'])]
    if len(images)!=3:raise RuntimeError('release requires extension and both installers')
    # Preflight all tags before any push, so one collision prevents partial
    # publication of a different rebuild under the same fixed version.
    states=[]
    for image in images:
        if not image.startswith('ghcr.io/') or not re.search(r':v\d+\.\d+\.\d+-talos-v\d+\.\d+\.\d+$',image):
            raise RuntimeError('unexpected release image name/tag')
        manifest=existing_manifest(image)
        if verify_existing:
            expected=inputs['image_config_digests'][image]
            if manifest is None:raise RuntimeError('Original release tag is missing; verification cannot publish it')
        else:
            expected=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',image],text=True).strip()
        if manifest and manifest['config']['digest']!=expected:
            raise RuntimeError('Immutable release tag already points to a different image; refusing overwrite')
        states.append((image,manifest))
    records=[]
    for image,manifest in states:
        if manifest is None:subprocess.run(['docker','push',image],check=True)
        digest=anonymous_digest(image)
        records.append({'image':image,'digestRef':image.split(':')[0]+'@'+digest,'digest':digest,'anonymous_pull':True,'architecture':'amd64'})
    return {'images':records,'inputs':published_inputs(inputs,records[0]['digestRef']),
            'licenses':'See build-metadata.json and /usr/local/share/licenses/talos-sysbox in the extension.',
            'corresponding_source':'sources.tar.gz of this release contains matching Sysbox, Talos/kernel, official extensions, Go modules and Ubuntu sources, build recipes and packaging patches.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--images',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--verify-existing',action='store_true',help='verify original release inputs without rebuilding or pushing')
    a=p.parse_args();inputs=json.loads(a.images.read_text())
    a.output.write_text(json.dumps(publish_images(inputs,a.verify_existing),indent=2)+'\n')


if __name__=='__main__':main()
