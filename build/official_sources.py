"""Retain the corresponding sources for the pinned, redistributed Talos images."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path


def stable_json(value):
    """Keep provenance bytes independent of filesystem/dictionary ordering."""
    return json.dumps(value,indent=2,sort_keys=True)+'\n'


def verify_catalogue(lock, entries):
    approved=set(entries.splitlines())
    for name in [*lock['standard_extensions'], 'vmtoolsd-guest-agent']:
        if lock['extensions'][name] not in approved:
            raise RuntimeError('Official extension is absent from the pinned Talos catalogue: '+name)


def download(spec, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    cache=Path('/source-cache')/spec['sha256']
    if cache.is_file() and hashlib.file_digest(cache.open('rb'),'sha256').hexdigest()==spec['sha256']:
        shutil.copy2(cache,target);return
    with urllib.request.urlopen(spec['url'],timeout=180) as response, target.open('wb') as out:
        digest=hashlib.sha256()
        while data:=response.read(1024*1024):
            digest.update(data);out.write(data)
    if digest.hexdigest()!=spec['sha256']:
        target.unlink()
        raise RuntimeError('Official source checksum mismatch: '+spec['url'])
    cache.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,cache)


def is_notice(name):
    path=Path(name)
    return path.name.lower().startswith(('licen','copying','notice','copyright','patents','unlicense','oss_disclosures')) or 'LICENSES' in path.parts


def retain_notices(archive, destination):
    """Read regular notice files, without extracting upstream paths or links."""
    def write(name,data):
        parts=Path(name).parts
        if Path(name).is_absolute() or '..' in parts:raise RuntimeError('Unsafe notice path')
        target=destination.joinpath(*parts)
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    if archive.suffix=='.zip':
        with zipfile.ZipFile(archive) as tf:
            for item in tf.infolist():
                if not item.is_dir() and is_notice(item.filename):write(item.filename,tf.read(item))
    else:
        with tarfile.open(archive) as tf:
            for item in tf:
                if item.isfile() and is_notice(item.name):write(item.name,tf.extractfile(item).read())


def collect(lock, source_root, licenses, manifest_path):
    manifest=json.loads(manifest_path.read_text())
    if manifest['talos']!=lock['talos'] or manifest['base_installer']!=lock['base_installer'] or manifest['extensions']!=lock['extensions']:
        raise RuntimeError('Official source inventory must match the selected image lock')
    root=source_root/'official';root.mkdir()
    for name,spec in manifest['frameworks'].items():
        print('Retaining official build recipes: '+name,flush=True)
        archive=root/'recipes'/(name+'.tar.gz');download(spec,archive)
        retain_notices(archive,licenses/'official'/name)
    sbom=root/'talos-amd64.spdx.json';download(manifest['sbom'],sbom)
    # Version/license inventory belongs to the image, not the current upstream branch.
    packages=json.loads(sbom.read_text())['packages']
    native=[]
    for spec in manifest['native_sources']:
        print('Retaining official source: '+spec['component'],flush=True)
        archive=root/'archives'/spec['sha256']/spec['file']
        if not archive.exists():download(spec,archive)
        retain_notices(archive,licenses/'official'/spec['component'])
        native.append(dict(spec,archive=str(archive.relative_to(source_root))))
    modules=set()
    local_modules=[]
    with tarfile.open(root/'recipes/talos.tar.gz') as tf:
        talos_mod=next(m for m in tf if m.name.count('/')==1 and m.name.endswith('/go.mod'))
        talos_go_mod=tf.extractfile(talos_mod).read().decode()
    for package in packages:
        if any(e['referenceType']=='purl' and e['referenceLocator'].startswith('pkg:golang/') for e in package.get('externalRefs',[])):
            if package['versionInfo']=='UNKNOWN':
                if package['name']!='github.com/siderolabs/talos/pkg/machinery' or not re.search(r'github.com/siderolabs/talos/pkg/machinery\s*=>\s*\./pkg/machinery',talos_go_mod):
                    raise RuntimeError('Unresolved local module in pinned Talos SBOM')
                local_modules.append({'Path':package['name'],'source':'recipes/talos.tar.gz:pkg/machinery','ref':manifest['frameworks']['talos']['ref']})
            else:modules.add(package['name']+'@'+package['versionInfo'])
    # The optional VMware agent has a separate module graph from the Talos SBOM.
    with tarfile.open(root/'recipes/talos-vmtoolsd.tar.gz') as tf:
        mod=next(m for m in tf if m.name.count('/')==1 and m.name.endswith('/go.mod'))
        modules.update(name+'@'+version for name,version in re.findall(r'^\s*(\S+)\s+(v\S+)\s*(?://.*)?$',tf.extractfile(mod).read().decode(),re.M))
    env=dict(os.environ,GOTOOLCHAIN='local',GOPROXY='https://proxy.golang.org',GOSUMDB='sum.golang.org',GOPRIVATE='',GONOSUMDB='',GONOPROXY='')
    records=[]
    # An empty module directory avoids changing the project's effective module locks.
    with tempfile.TemporaryDirectory(prefix='official-modules-') as tmp:
        for module in sorted(modules):
            print('Retaining corresponding Go module: '+module,flush=True)
            result=json.loads(subprocess.check_output(['go','mod','download','-json',module],cwd=tmp,env=env,text=True))
            if result.get('Error') or not result.get('Sum') or not result.get('GoModSum'):
                raise RuntimeError('Incomplete checksum-verified corresponding Go source: '+module)
            relative=Path(result['Zip']).relative_to(Path('/root/go/pkg/mod/cache/download')).parent
            for key in ('Zip','GoMod','Info'):
                original=Path(result[key]);target=root/'go-modules'/relative/original.name
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(original.read_bytes())
            retain_notices(Path(result['Zip']),licenses/'official/go-modules')
            records.append({k:result[k] for k in ('Path','Version','Sum','GoModSum')})
    inventory={'image_inputs':{'base_installer':manifest['base_installer'],'extensions':manifest['extensions']},'frameworks':manifest['frameworks'],'native_sources':native,'go_modules':records,'local_modules':local_modules,'sbom_packages':packages,'source_provision':'All listed archives, module sources, upstream build recipes, configuration and patches are in sources.tar.gz of the same release. VMware-only agent sources are included in this common source bundle.'}
    (root/'inventory.json').write_text(stable_json(inventory))
    (licenses/'official-inventory.json').write_text(stable_json(inventory))
    return inventory
