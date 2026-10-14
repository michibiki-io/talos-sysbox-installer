#!/usr/bin/python3
"""Build bwrap 0.12.0 and retain matching validation image source/licenses."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

from validation_packages import normalize

if sys.version_info < (3, 12):
    raise ValueError('probe and trusted launcher require Python >=3.12')
lock = json.loads(Path('/work/versions.lock.json').read_text())
spec = lock['validation_sources']['bubblewrap']
if spec['version'] != '0.12.0':
    raise ValueError('original probe requires bubblewrap 0.12.0')
# Align inherited base binaries before building bwrap or collecting notices.
package_inventory = normalize(lock['apt_snapshot'])
sources = Path('/opt/validation-sources')
sources.mkdir()
archive = sources / 'bubblewrap.tar.gz'
data = urllib.request.urlopen(spec['url'], timeout=120).read()
if hashlib.sha256(data).hexdigest() != spec['sha256']:
    raise ValueError('bubblewrap archive checksum mismatch')
archive.write_bytes(data)
root = Path('/work/bubblewrap')
root.mkdir()
with tarfile.open(archive) as tf:
    tf.extractall(root, filter='data')
source, = root.iterdir()
subprocess.run(['meson', 'setup', 'out', '-Dman=disabled', '-Dtests=false', '--prefix=/usr'], cwd=source, check=True)
subprocess.run(['ninja', '-C', 'out', 'install'], cwd=source, check=True)
version = subprocess.check_output(['bwrap', '--version'], text=True).strip()
if version != 'bubblewrap 0.12.0':
    raise ValueError('incorrect effective bwrap')
# This separate test image contains compiler/Python/nsenter packages too. Keep
# the signed-snapshot source packages and notices for every installed package,
# rather than mislabeling the helper MIT license as the complete inventory.
licenses = Path('/opt/validation-licenses')
shutil.copytree('/usr/share/doc', licenses / 'packages', symlinks=True)
shutil.copytree('/usr/share/common-licenses', licenses / 'common-licenses')
shutil.copy2(source / 'COPYING', licenses / 'BUBBLEWRAP-COPYING')
(sources / 'binary-packages.json').write_text(json.dumps(package_inventory, indent=2) + '\n')
package_inputs = sorted({(p['source'], p['source_version']) for p in package_inventory['effective']})
(sources / 'packages.json').write_text(json.dumps(package_inputs, indent=2) + '\n')
# Batch exact versions to avoid reloading snapshot indexes for each package.
subprocess.run(['sh', '/work/apt_retry.sh', 'source', '--only-source', '--download-only',
                *(package + '=' + version for package, version in package_inputs)], cwd=sources, check=True)
Path('/opt/proc-transfer/image-versions.json').write_text(json.dumps({
    'bubblewrap': '0.12.0', 'python': subprocess.check_output(['python3', '--version'], text=True).strip(),
    'cc': subprocess.check_output(['cc', '--version'], text=True).splitlines()[0],
    'source': spec, 'apt_snapshot': lock['apt_snapshot'],
    'apt_packages': package_inventory['effective'],
    'image_binary_sha256': {str(Path(shutil.which(name)).resolve()): hashlib.sha256(Path(shutil.which(name)).resolve().read_bytes()).hexdigest()
                            for name in ('bwrap', 'python3', 'cc', 'nsenter')}}, indent=2) + '\n')
inventory = {str(path.relative_to(sources)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sources.rglob('*') if path.is_file()}
(sources / 'source-files.sha256.json').write_text(json.dumps(inventory, indent=2, sort_keys=True) + '\n')
# No rootfs mutation or package download is needed at verification time.
