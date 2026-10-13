#!/usr/bin/env python3
"""Assemble helpers only inside the locked builder; never reads host programs."""
import hashlib
import base64
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from official_sources import collect, verify_catalogue, is_notice, stable_json

LOCK = json.loads(Path('/work/versions.lock.json').read_text())
OUT = Path('/out')
LOCAL = OUT / 'rootfs/usr/local'
SOURCES = OUT / 'sources'
LIC = LOCAL / 'share/licenses/talos-sysbox'
packages = set()


def run(args):
    return subprocess.check_output(args, text=True).strip()


def download(spec, target):
    data = urllib.request.urlopen(spec['url'], timeout=120).read()
    if hashlib.sha256(data).hexdigest() != spec['sha256']:
        raise RuntimeError('Upstream archive checksum mismatch')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def owner(path):
    try:
        result = run(['dpkg-query', '-S', str(path)])
    except subprocess.CalledProcessError:
        # dpkg records pre-usrmerge paths for some files.
        alt = str(path).replace('/usr/lib/', '/lib/', 1).replace('/usr/bin/', '/bin/', 1)
        result = run(['dpkg-query', '-S', alt])
    packages.add(result.split(': ')[0])


def copy_file(path, root, destination=None):
    path = Path(path)
    dest = root / (destination or str(path)).lstrip('/')
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path.resolve(), dest)
    owner(path.resolve())


def helper(path, root, destination=None):
    copy_file(path, root, destination)
    result = subprocess.run(['ldd', str(path)], capture_output=True, text=True)
    if 'not found' in result.stdout:
        raise RuntimeError('Unresolved helper library')
    for dependency in re.findall(r'(/[\w./+\-]+)', result.stdout):
        copy_file(dependency, root)


def extract_source(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        members=[]
        for m in tf:
            parts=Path(m.name).parts[1:]
            if not parts: continue
            m.name=str(Path(*parts))
            members.append(m)
        tf.extractall(destination, members=members, filter='data')


def rebuild_sysbox():
    source_root=Path('/work/sysbox-source')
    extract_source(SOURCES/'sysbox.tar.gz', source_root)
    for component,spec in LOCK['sysbox_components'].items():
        archive=SOURCES/(component+'.tar.gz')
        download(spec, archive)
        extract_source(archive, source_root/component)
    # Use the upstream packaging compatibility adjustment, inside this builder
    # only. The exact adjustment is included in the corresponding build source.
    header=Path('/usr/include/linux/seccomp.h')
    text=header.read_text()
    header.write_text(re.sub(r'(#define\s+SECCOMP_IOCTL_NOTIF_ID_VALID\s+)SECCOMP_IOW\(2, __u64\)', r'\1SECCOMP_IOR(2, __u64)', text))
    target=Path('/work/rebuilt');target.mkdir()
    links=source_root/'link-maps';links.mkdir()
    env=dict(os.environ,CGO_ENABLED='1',GOTOOLCHAIN='local',GOFLAGS='-mod=readonly -p=2',GOMAXPROCS='2',GOPROXY='https://proxy.golang.org',GOSUMDB='sum.golang.org',GOPRIVATE='',GONOSUMDB='',GONOPROXY='')
    generator=SOURCES/'protobuf-generator.zip'
    download(LOCK['protobuf_generator'],generator)
    generator_root=Path('/work/protobuf-generator');generator_root.mkdir()
    with zipfile.ZipFile(generator) as z:
        z.extractall(generator_root)
    module=generator_root/f'github.com/golang/protobuf@v{LOCK["protobuf_generator"]["version"]}'
    tool=Path('/work/protoc-gen-go')
    subprocess.run(['go','build','-trimpath','-o',str(tool),'./protoc-gen-go'],cwd=module,env=dict(env,CGO_ENABLED='0'),check=True)
    destination=LIC/'protobuf-generator';destination.mkdir()
    for license_file in module.glob('LICENSE*'):
        shutil.copy2(license_file,destination/license_file.name)
    env['PATH']='/work:'+env['PATH']
    for part in ('sysboxFs','sysboxMgr'):
        proto_dir=source_root/'sysbox-ipc'/(part+'Grpc')/(part+'Protobuf')
        subprocess.run(['protoc','-I.','-I/usr/include',part+'Protobuf.proto','--go_out=plugins=grpc,paths=source_relative:.'],cwd=proto_dir,env=env,check=True)
        generated=SOURCES/'generated/sysbox-ipc'/(part+'Grpc')/(part+'Protobuf')
        generated.mkdir(parents=True,exist_ok=True)
        shutil.copy2(proto_dir/(part+'Protobuf.pb.go'),generated)
    for component,tags,package in [('sysbox-runc','seccomp apparmor idmapped_mnt netgo osusergo','.'),('sysbox-mgr','idmapped_mnt netgo osusergo','.'),('sysbox-fs','netgo osusergo','./cmd/sysbox-fs')]:
        module_locks=Path('/work/project/build/sysbox-module-locks')/component
        generating=os.environ.get('GENERATE_MODULE_LOCKS')=='1'
        if not generating and module_locks.is_dir():
            for name in ('go.mod','go.sum'):
                shutil.copy2(module_locks/name,source_root/component/name)
        commit=LOCK['sysbox_components'][component]['commit']
        linkmap=links/(component+'.map')
        flags=f"-buildid= -w -linkmode=external -extldflags '-static -Wl,-Map,{linkmap}' -X 'main.edition=Community Edition (CE)' -X main.version={LOCK['sysbox']} -X main.commitId={commit} -X main.builtAt=locked-source-build -X main.builtBy=michibiki.io"
        build_env=dict(env,GOFLAGS='-mod=mod -p=2') if generating else env
        subprocess.run(['go','build','-buildvcs=false','-trimpath','-tags',tags,'-ldflags',flags,'-o',str(target/component),package],cwd=source_root/component,env=build_env,check=True)
        for name in ('go.mod','go.sum'):
            file=source_root/component/name
            dest=SOURCES/'effective-modules'/component/name
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,dest)
            if generating:
                print('MODULE_LOCK::'+component+'::'+name+'::'+base64.b64encode(file.read_bytes()).decode(),flush=True)
        # Linker maps identify the actual static archives. Collect their owners'
        # matching signed-snapshot source packages and license texts too.
        for library in set(re.findall(r'(/[^\s()]+\.a)',linkmap.read_text())):
            if Path(library).is_file(): owner(Path(library).resolve())
    for path in source_root.rglob('*'):
        if path.is_file() and is_notice(str(path)):
            destination=LIC/'sysbox-source'/path.relative_to(source_root)
            destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,destination)
    # Retain the exact checksum-verified module archives used by the build.
    module_cache=Path('/root/go/pkg/mod/cache/download')
    if module_cache.exists():
        for path in module_cache.rglob('*'):
            if path.is_file() and path.suffix in ('.zip','.mod','.info') and 'sumdb' not in path.parts:
                relative=path.relative_to(module_cache)
                if (SOURCES/'official/go-modules'/relative).is_file():continue
                destination=SOURCES/'go-modules'/relative
                destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,destination)
    for path in Path('/root/go/pkg/mod').rglob('*'):
        if path.is_file() and is_notice(str(path)) and '/cache/' not in str(path):
            destination=LIC/'go-modules'/path.relative_to(Path('/root/go/pkg/mod'))
            destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,destination)
    return target


def main():
    verify_catalogue(LOCK, Path('/work/catalogue-image-digests').read_text())
    LIC.mkdir(parents=True)
    shutil.copy2('/work/LICENSE', LIC / 'MIT')
    shutil.copy2('/work/GO-LICENSE', LIC / 'GO-LICENSE')
    shutil.copy2('/work/GO-PATENTS', LIC / 'GO-PATENTS')
    SOURCES.mkdir(parents=True)
    LOCAL.joinpath('bin').mkdir()
    LOCAL.joinpath('etc/containers').mkdir(parents=True)
    download(LOCK['sysbox_package'], Path('/work/sysbox.deb'))
    subprocess.run(['dpkg-deb', '--extract', '/work/sysbox.deb', '/work/sysbox'], check=True)
    for component, spec in LOCK['sources'].items():
        archive = SOURCES / (component + '.tar.gz')
        download(spec, archive)
        with tarfile.open(archive) as tf:
            for m in tf:
                if m.isfile() and is_notice(m.name):
                    dest = LIC / component / Path(*Path(m.name).parts[1:])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(tf.extractfile(m).read())
    shutil.copytree('/work/sysbox/usr/share/doc', LIC / 'sysbox-package', dirs_exist_ok=True)
    ctr_source=Path('/work/containerd-source')
    extract_source(SOURCES/'containerd.tar.gz',ctr_source)
    subprocess.run(['go','build','-buildvcs=false','-trimpath','-tags','netgo osusergo','-ldflags','-buildid= -s -w -X github.com/containerd/containerd/v2/version.Version=v2.1.6','-o','/work/ctr','./cmd/ctr'],cwd=ctr_source,env=dict(os.environ,CGO_ENABLED='0',GOTOOLCHAIN='local',GOFLAGS='-mod=vendor -p=2'),check=True)
    ctr_build_info=run(['go','version','-m','/work/ctr'])
    if not re.search(r'\bCGO_ENABLED=0\b',ctr_build_info):
        raise RuntimeError('ctr has unaccounted C linkage; refusing redistribution')
    official_inventory=collect(LOCK,SOURCES,LIC,Path('/work/project/build/official-sources.lock.json'))
    rebuilt=rebuild_sysbox()
    binaries = {}
    for daemon in ('sysbox-runc', 'sysbox-mgr', 'sysbox-fs'):
        source = rebuilt / daemon
        shutil.copy2(source, LOCAL / 'bin' / daemon)
        binaries[daemon] = hashlib.sha256(source.read_bytes()).hexdigest()
    for daemon in ('sysbox-mgr', 'sysbox-fs'):
        root = LOCAL / 'lib/containers' / daemon
        root.joinpath('etc').mkdir(parents=True)
        for directory in ('dev','proc','sys','run','var','tmp'):
            root.joinpath(directory).mkdir(exist_ok=True)
        root.joinpath('tmp').chmod(0o1777)
        root.joinpath('etc/os-release').write_text(f'NAME="Talos"\nID=talos\nVERSION_ID=v{LOCK["talos"]}\n')
        for name in ('subuid', 'subgid'):
            root.joinpath('etc', name).write_text('sysbox:100000:65536\n')
        shutil.copy2(LOCAL / 'bin' / daemon, root / daemon)
        shutil.copy2('/work/ctr', root / 'ctr')
        shutil.copy2('/work/supervisor', root / 'supervisor')
        helper('/bin/sh', root, '/bin/sh')
        programs = ('rsync', 'modprobe', 'iptables') if daemon == 'sysbox-mgr' else ('fusermount3',)
        for name in programs:
            helper(shutil.which(name), root, '/bin/' + name)
        if daemon == 'sysbox-mgr':
            for path in Path('/usr/lib/x86_64-linux-gnu/xtables').glob('*.so'):
                helper(path, root)
        mounts = ''.join(f'    - source: {p}\n      destination: {p}\n      type: bind\n      options: [rbind, rshared, rw]\n' for p in ('/run', '/var', '/system/run'))
        # Socket readiness is checked inside the supervisor with a deadline;
        # a Talos path dependency would wait without that bound.
        config = f'''name: {daemon}
container:
  entrypoint: ./supervisor
  args: [{daemon}]
  environment: [PATH=/bin:/usr/bin:/usr/local/bin]
  mounts:
{mounts}  security:
    writeableSysfs: true
    rootfsPropagation: shared
    maskedPaths: []
    readonlyPaths: []
depends:
  - service: cri
restart: always
logToConsole: false
'''
        LOCAL.joinpath('etc/containers', daemon + '.yaml').write_text(config)
    metadata = []
    for package in sorted(packages):
        fields = run(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}', package]).split('\t')
        binary, version, source, source_version = fields
        copyright_file = Path('/usr/share/doc') / binary.split(':')[0] / 'copyright'
        if not copyright_file.is_file():
            raise RuntimeError('Missing package copyright: ' + binary)
        dest = LIC / binary.split(':')[0]
        dest.mkdir(exist_ok=True)
        shutil.copy2(copyright_file, dest / 'copyright')
        for license_file in Path('/usr/share/common-licenses').iterdir():
            if license_file.is_file():
                common = LIC / 'common-licenses'
                common.mkdir(exist_ok=True)
                shutil.copy2(license_file, common / license_file.name)
        subprocess.run(['apt-get', 'source', '--download-only', source + '=' + source_version], cwd=SOURCES, check=True)
        metadata.append({'binary': binary, 'version': version, 'source': source, 'source_version': source_version, 'provision': 'Corresponding source archive attached to the same release, including Debian/Ubuntu packaging and patches.'})
    hashes = {str(p.relative_to(SOURCES)): hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCES.rglob('*') if p.is_file()}
    source_inputs={str(p.relative_to(Path('/work/project'))):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('/work/project').rglob('*') if p.is_file()}
    report = {'inputs': LOCK, 'build_mode':'module-lock-generation' if os.environ.get('GENERATE_MODULE_LOCKS')=='1' else 'release', 'source_inputs_sha256':source_inputs, 'sysbox_build': 'All three binaries rebuilt from checksum-locked release submodule sources with read-only Go module resolution and checksum verification; linked static archives identified by GNU linker maps.', 'binary_sha256': binaries, 'supervisor_sha256': hashlib.sha256(Path('/work/supervisor').read_bytes()).hexdigest(), 'ctr_build_info':ctr_build_info, 'official_source_inventory':official_inventory, 'packages': metadata, 'source_files_sha256': hashes, 'source_provision': 'Redistribute sources.tar.gz beside all images; licenses and NOTICE are in /usr/local/share/licenses/talos-sysbox. Matching library and application sources plus this repository build scripts permit rebuilding/relinking static LGPL components.'}
    OUT.joinpath('build-metadata.json').write_text(stable_json(report))
    shutil.copy2(OUT / 'build-metadata.json', LIC / 'build-metadata.json')
    OUT.joinpath('manifest.yaml').write_text(f'''version: v1alpha1
metadata:
  name: sysbox
  version: {LOCK['sysbox']}
  author: michibiki.io
  description: Sysbox runtime and supervised node backends
  compatibility:
    talos:
      version: "= v{LOCK['talos']}"
''')


if __name__ == '__main__':
    main()
