#!/usr/bin/env python3
"""Assemble helpers only inside the locked builder; never reads host programs."""
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

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


def main():
    LIC.mkdir(parents=True)
    shutil.copy2('/work/LICENSE', LIC / 'MIT')
    shutil.copy2('/work/GO-LICENSE', LIC / 'GO-LICENSE')
    shutil.copy2('/work/GO-PATENTS', LIC / 'GO-PATENTS')
    SOURCES.mkdir(parents=True)
    LOCAL.joinpath('bin').mkdir()
    LOCAL.joinpath('etc/containers').mkdir(parents=True)
    download(LOCK['sysbox_package'], Path('/work/sysbox.deb'))
    subprocess.run(['dpkg-deb', '--extract', '/work/sysbox.deb', '/work/sysbox'], check=True)
    download(LOCK['containerd_package'], Path('/work/containerd.tar.gz'))
    with tarfile.open('/work/containerd.tar.gz') as archive:
        member = archive.getmember('bin/ctr')
        Path('/work/ctr').write_bytes(archive.extractfile(member).read())
        Path('/work/ctr').chmod(0o755)
    for component, spec in LOCK['sources'].items():
        archive = SOURCES / (component + '.tar.gz')
        download(spec, archive)
        with tarfile.open(archive) as tf:
            for m in tf:
                if m.isfile() and Path(m.name).name in ('LICENSE', 'NOTICE'):
                    dest = LIC / component / Path(*Path(m.name).parts[1:])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(tf.extractfile(m).read())
    shutil.copytree('/work/sysbox/usr/share/doc', LIC / 'sysbox-package', dirs_exist_ok=True)
    binaries = {}
    for daemon in ('sysbox-runc', 'sysbox-mgr', 'sysbox-fs'):
        source = Path('/work/sysbox/usr/bin') / daemon
        shutil.copy2(source, LOCAL / 'bin' / daemon)
        binaries[daemon] = hashlib.sha256(source.read_bytes()).hexdigest()
    for daemon in ('sysbox-mgr', 'sysbox-fs'):
        root = LOCAL / 'lib/containers' / daemon
        root.joinpath('etc').mkdir(parents=True)
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
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCES.iterdir() if p.is_file()}
    report = {'inputs': LOCK, 'binary_sha256': binaries, 'supervisor_sha256': hashlib.sha256(Path('/work/supervisor').read_bytes()).hexdigest(), 'packages': metadata, 'source_files_sha256': hashes, 'source_provision': 'Redistribute sources.tar.gz beside all images; licenses and NOTICE are in /usr/local/share/licenses/talos-sysbox.'}
    OUT.joinpath('build-metadata.json').write_text(json.dumps(report, indent=2) + '\n')
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
