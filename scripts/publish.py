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
    error=result.stderr.lower()
    # GHCR reports manifest unknown for both a missing repository and tag.
    # Never classify auth/transport failures as permission to create a tag.
    if not any(word in error for word in ('unauthorized','denied','forbidden','timeout','connection')) and any(word in error for word in ('no such manifest:','manifest unknown','name unknown')):return None
    raise RuntimeError('Cannot establish registry tag state; refusing publication')


def publish_images(inputs, verify_existing=False, preflight_only=False):
    images=[inputs['extension']['image'],*(i['image'] for i in inputs['installers'])]
    if len(images)!=3:raise RuntimeError('release requires extension and both installers')
    if not verify_existing:
        store=dict(json.loads(subprocess.check_output(['docker','info','--format','{{json .DriverStatus}}'],text=True)))
        if 'driver-type' in store:
            raise RuntimeError('Publication requires the classic Docker image store; containerd image store is not supported')
    # Preflight all tags before any push, so one collision prevents partial
    # publication of a different rebuild under the same fixed version.
    states=[]
    for image in images:
        if not image.startswith('ghcr.io/') or not re.search(r':v\d+\.\d+\.\d+-talos-v\d+\.\d+\.\d+$',image):
            raise RuntimeError('unexpected release image name/tag')
        manifest=existing_manifest(image)
        expected=inputs['image_config_digests'][image]
        if verify_existing:
            if manifest is None:raise RuntimeError('Original release tag is missing; verification cannot publish it')
        else:
            loaded=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',image],text=True).strip()
            if loaded!=expected:raise RuntimeError('Loaded image does not match the recorded archive configuration digest')
        if manifest and manifest['config']['digest']!=expected:
            raise RuntimeError('Immutable release tag already points to a different image; refusing overwrite')
        states.append((image,manifest))
    if preflight_only:
        return {'preflight':True,'inputs':copy.deepcopy(inputs)}
    # Create all three packages before anonymous checks. New GHCR packages may
    # need an owner visibility change; verify-existing can then finish safely.
    for image,manifest in states:
        if manifest is None:subprocess.run(['docker','push',image],check=True)
    records=[]
    for image,_ in states:
        digest=anonymous_digest(image)
        records.append({'image':image,'digestRef':image.split(':')[0]+'@'+digest,'digest':digest,'anonymous_pull':True,'architecture':'amd64'})
    return {'images':records,'inputs':published_inputs(inputs,records[0]['digestRef']),
            'licenses':'See build-metadata.json and /usr/local/share/licenses/talos-sysbox in the extension.',
            'corresponding_source':'sources.tar.gz of this release contains matching Sysbox, Talos/kernel, official extensions, Go modules and Ubuntu sources, build recipes and packaging patches.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--images',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--verify-existing',action='store_true',help='verify original release inputs without rebuilding or pushing')
    mode.add_argument('--preflight-only',action='store_true',help='check store, loaded images and all tags without publishing')
    a=p.parse_args();inputs=json.loads(a.images.read_text())
    a.output.write_text(json.dumps(publish_images(inputs,a.verify_existing,a.preflight_only),indent=2)+'\n')


if __name__=='__main__':main()
