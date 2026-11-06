#!/usr/bin/env python3
"""Container-only conversion and inspection of a stopped Oracle ARM64 disk."""
import argparse
import binascii
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tarfile
import tempfile

GIB = 1024**3
SIZE = 50 * GIB
PREFIX = 'arm64-standard-oracle'


def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, **kwargs)


def output(*args):
    return subprocess.check_output(list(args))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(4*1024*1024), b''):
            h.update(b)
    return h.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def region_sha(path, start, length):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        f.seek(start)
        while length:
            b = f.read(min(length, 4*1024*1024))
            require(bool(b), 'truncated partition')
            h.update(b)
            length -= len(b)
    return h.hexdigest()


def gpt(path):
    size = path.stat().st_size
    require(size % 512 == 0, 'unaligned raw size')
    with path.open('rb') as f:
        def header(lba):
            f.seek(lba*512)
            b = f.read(512)
            require(b[:8] == b'EFI PART', 'missing GPT signature')
            n, crc = struct.unpack_from('<II', b, 12)
            require(92 <= n <= 512, 'invalid GPT header size')
            data = bytearray(b[:n]); data[16:20] = b'\0'*4
            require(binascii.crc32(data) == crc, 'GPT header CRC mismatch')
            current, backup, first, last = struct.unpack_from('<QQQQ', b, 24)
            table, count, entrysize, tablecrc = struct.unpack_from('<QIII', b, 72)
            require(current == lba and 0 < backup < size//512, 'GPT location mismatch')
            require(0 < count <= 4096 and 128 <= entrysize <= 4096, 'GPT table bounds')
            f.seek(table*512); entries = f.read(count*entrysize)
            require(binascii.crc32(entries) == tablecrc, 'GPT entries CRC mismatch')
            return dict(current=current, backup=backup, first=first, last=last,
                        guid=b[56:72].hex(), entries=entries, count=count, entrysize=entrysize)
        primary = header(1); backup = header(size//512-1)
        require(primary['backup'] == backup['current'] and backup['backup'] == 1,
                'backup GPT is not at final disk end')
        require(primary['entries'] == backup['entries'] and primary['guid'] == backup['guid'],
                'primary/backup GPT differ')
        partitions = []
        for i in range(primary['count']):
            b = primary['entries'][i*primary['entrysize']:(i+1)*primary['entrysize']]
            if b[:16] == b'\0'*16:
                continue
            start, end = struct.unpack_from('<QQ', b, 32)
            require(primary['first'] <= start <= end <= primary['last'], 'partition outside GPT bounds')
            label = b[56:128].decode('utf-16-le').rstrip('\0')
            require(not partitions or start > partitions[-1]['end_lba'], 'overlapping partitions')
            partitions.append(dict(number=i+1, label=label, start_lba=start, end_lba=end,
                                   sha256=region_sha(path, start*512, (end-start+1)*512)))
    require([p['label'] for p in partitions] == ['EFI', 'META'],
            'expected only EFI/META, no STATE/EPHEMERAL/extra disk')
    return dict(primary_lba=1, backup_lba=size//512-1, crc='PASS', partitions=partitions)


def pe(data):
    require(data[:2] == b'MZ' and len(data) >= 64, 'missing PE DOS header')
    offset = struct.unpack_from('<I', data, 60)[0]
    require(data[offset:offset+4] == b'PE\0\0', 'missing PE signature')
    machine, count = struct.unpack_from('<HH', data, offset+4)
    optional = struct.unpack_from('<H', data, offset+20)[0]
    require(machine == 0xaa64, 'PE boot asset is not ARM64')
    require(struct.unpack_from('<H', data, offset+24)[0] == 0x20b, 'not PE32+')
    require(struct.unpack_from('<H', data, offset+24+68)[0] == 10, 'not EFI application')
    sections = {}
    for i in range(count):
        at = offset+24+optional+40*i
        name = data[at:at+8].rstrip(b'\0').decode('ascii')
        virtual, _, size, ptr = struct.unpack_from('<IIII', data, at+8)
        length = min(virtual, size)
        require(ptr+length <= len(data), 'truncated PE section')
        sections[name] = data[ptr:ptr+length]
    return sections


def cpio(data):
    """Read concatenated newc archives; return selected payloads and remainder."""
    selected = {}; at = 0
    while at < len(data):
        while at < len(data) and data[at] == 0:
            at += 1
        if data[at:at+6] not in (b'070701', b'070702'):
            break
        require(at+110 <= len(data), 'truncated cpio header')
        fields = [int(data[at+6+i*8:at+14+i*8], 16) for i in range(13)]
        length, namesize = fields[6], fields[11]
        require(0 < namesize <= 4096, 'invalid cpio name')
        name = data[at+110:at+110+namesize-1].decode().removeprefix('./')
        start = (at+110+namesize+3) & ~3
        require(start+length <= len(data), 'truncated cpio payload')
        if name.endswith('.sqsh') or name.endswith('extensions.yaml'):
            require(name not in selected, 'duplicate extension cpio member')
            selected[name] = data[start:start+length]
        at = (start+length+3) & ~3
    return selected, data[at:]


def initramfs(data):
    selected, remainder = cpio(data)
    formats = []
    if remainder:
        if remainder.startswith(b'\x28\xb5\x2f\xfd'):
            formats.append('zstd (magic, concatenated frames)')
            decoded = subprocess.check_output(['zstd', '-d', '-c', '--quiet'], input=remainder)
        elif remainder.startswith(b'\xfd7zXZ\0'):
            formats.append('xz (magic)'); decoded = lzma.decompress(remainder)
        elif remainder.startswith(b'\x1f\x8b'):
            formats.append('gzip (magic)'); decoded = gzip.decompress(remainder)
        else:
            raise ValueError('unknown initramfs compression magic')
        more, rest = cpio(decoded)
        require(not rest, 'unparsed initramfs tail')
        require(not (selected.keys() & more.keys()), 'duplicate compressed cpio payload')
        selected.update(more)
    require(selected, 'missing extension payloads in initramfs')
    return selected, formats


def tar_inventory(path):
    files = {}
    with tarfile.open(path) as tf:
        for m in tf:
            name = m.name.removeprefix('./')
            if not name.startswith('rootfs/') or name == 'rootfs/':
                continue
            rel = name[len('rootfs/'):].rstrip('/')
            if not rel:
                continue
            if m.isfile() or m.islnk():
                digest = hashlib.sha256(tf.extractfile(m).read()).hexdigest()
                files[rel] = ('file', m.mode, digest)
            elif m.issym():
                files[rel] = ('symlink', m.mode, m.linkname)
            elif m.isdir():
                files[rel] = ('directory', m.mode, '')
            else:
                raise ValueError('unexpected extension rootfs member type')
        manifest = tf.extractfile('manifest.yaml').read()
    return files, manifest


def root_inventory(root):
    files = {}
    for path in root.rglob('*'):
        name = str(path.relative_to(root)); s = path.lstat()
        if path.is_symlink():
            files[name] = ('symlink', s.st_mode & 0o7777, os.readlink(path))
        elif path.is_file():
            files[name] = ('file', s.st_mode & 0o7777, sha(path))
        elif path.is_dir():
            files[name] = ('directory', s.st_mode & 0o7777, '')
        else:
            raise ValueError('unexpected squashfs node')
    return files


def compare_inventory(actual, original):
    # Talos 1.12.6's extension unpack loses the Unix sticky bit on these
    # existing Sysbox private tmp directories. Retain that observed difference
    # explicitly; never ignore arbitrary payload/mode changes.
    differences = []
    normalized = dict(original)
    for name in ('usr/local/lib/containers/sysbox-fs/tmp', 'usr/local/lib/containers/sysbox-mgr/tmp'):
        if original.get(name) == ('directory',0o1777,'') and actual.get(name) == ('directory',0o777,''):
            differences.append({'path':name,'input_mode':'1777','embedded_mode':'0777',
                                'reason':'pinned Talos extension unpack normalization; runtime UNRUN'})
            normalized[name] = actual[name]
    require(actual == normalized, 'embedded extension content differs from exact Standard input')
    return differences


def boot_assets(raw, table, directory):
    import yaml
    efi = table['partitions'][0]
    fat = str(raw)+'@@'+str(efi['start_lba']*512)
    with tempfile.TemporaryDirectory(prefix='boot-', dir=directory) as tmp:
        tmp = Path(tmp)
        # Inspection only: no installer UKI section is an input to generation.
        with tarfile.open(directory/'standard.tar') as tf:
            baseline_uki = pe(tf.extractfile('usr/install/arm64/vmlinuz.efi').read())
            baseline_loader = tf.extractfile('usr/install/arm64/systemd-boot.efi').read()
        baseline_payloads, _ = initramfs(baseline_uki['.initrd'])
        baseline_cfg = yaml.safe_load(next(b for n,b in baseline_payloads.items() if n.endswith('extensions.yaml')))
        baseline_inventories = []
        for i,layer in enumerate(baseline_cfg['layers']):
            b = next(b for n,b in baseline_payloads.items() if Path(n).name == layer['image'])
            squash = tmp/f'standard-{i}.sqsh'; squash.write_bytes(b)
            root = tmp/f'standard-{i}'
            run('unsquashfs','-no-progress','-processors','2','-d',str(root),str(squash),stdout=subprocess.DEVNULL)
            baseline_inventories.append(root_inventory(root))
        run('mcopy', '-i', fat, '::/EFI/BOOT/BOOTAA64.EFI', str(tmp/'boot.efi'))
        boot = (tmp/'boot.efi').read_bytes(); pe(boot)
        require(boot == baseline_loader, 'Oracle loader differs from Standard ARM64 boot asset')
        run('mcopy', '-i', fat, '::/EFI/Linux/*.efi', str(tmp))
        ukis = []
        for path in sorted(tmp.glob('*.efi')):
            if path.name == 'boot.efi':
                continue
            data = path.read_bytes(); sections = pe(data)
            args = shlex.split(sections['.cmdline'].rstrip(b'\0').decode())
            require([x for x in args if x.startswith('console=')] == ['console=ttyAMA0,9600'], 'console mismatch')
            require('talos.platform=oracle' in args, 'wrong platform in UKI')
            require(not any(x.startswith(('talos.config=', 'ip=', 'talos.hostname=', 'talos.role=')) for x in args), 'embedded identity/config argument')
            kernel = sections['.linux']
            require(kernel == baseline_uki['.linux'], 'Oracle kernel differs from Standard ARM64 kernel')
            if kernel.startswith(b'MZ'):
                pe(kernel); kernel_format = 'ARM64 EFI zboot PE'
            else:
                require(kernel[56:60] == b'ARM\x64', 'kernel is not ARM64 Image')
                kernel_format = 'ARM64 Image'
            payloads, compression = initramfs(sections['.initrd'])
            configs = [b for n,b in payloads.items() if n.endswith('extensions.yaml')]
            require(len(configs) == 1, 'missing/duplicate extensions metadata')
            layers = yaml.safe_load(configs[0])['layers']
            require(layers == baseline_cfg['layers'], 'Oracle and Standard extension metadata differ')
            expected = json.loads((directory/'request.json').read_text())['extension_names']
            require([x['metadata']['name'] for x in layers] == expected, 'extension order/identity mismatch')
            extensions = []
            for i,layer in enumerate(layers):
                name = layer['image']; require(Path(name).name == name, 'invalid squashfs filename')
                matches = [b for n,b in payloads.items() if Path(n).name == name]
                require(len(matches) == 1 and matches[0][:4] == b'hsqs', 'missing squashfs payload')
                squash = tmp/f'ext-{i}.sqsh'; squash.write_bytes(matches[0])
                root = tmp/f'root-{i}'
                run('unsquashfs', '-no-progress', '-processors', '2', '-d', str(root), str(squash), stdout=subprocess.DEVNULL)
                actual = root_inventory(root)
                require(actual == baseline_inventories[i], 'Oracle embedded payload/modes differ from Standard installer')
                original, manifest = tar_inventory(directory/f'extension-{i}.tar')
                mode_differences = compare_inventory(actual,original)
                require(yaml.safe_load(manifest)['metadata'] == layer['metadata'], 'extension manifest metadata mismatch')
                if i == 0:
                    metadata = json.loads((directory/'build-metadata.json').read_text())
                    for binary, digest in metadata['binary_sha256'].items():
                        location = ('usr/local/lib/containers/sysbox-fs/' if binary.endswith('mount-helper') else 'usr/local/bin/')+binary
                        p = root/location
                        require(sha(p) == digest, 'Sysbox binary SHA256 mismatch: '+binary)
                        head = p.read_bytes()[:20]
                        require(head[:4] == b'\x7fELF' and int.from_bytes(head[18:20], 'little') == 183, 'Sysbox payload not ARM64 ELF')
                    require(sha(root/'usr/local/lib/containers/sysbox-fs/supervisor') == metadata['supervisor_sha256'], 'supervisor mismatch')
                extensions.append(dict(metadata=layer['metadata'], squashfs_sha256=sha(squash),
                                       rootfs_entries=len(actual), input_content_comparison='PASS',
                                       standard_installer_payload_comparison='PASS',
                                       mode_differences=mode_differences))
            ukis.append(dict(file=path.name, sha256=sha(path), cmdline=args, kernel_format=kernel_format,
                             kernel_sha256=hashlib.sha256(kernel).hexdigest(), initrd_sha256=hashlib.sha256(sections['.initrd']).hexdigest(),
                             initrd_compression=compression, extensions=extensions))
        require(ukis, 'no UKI on EFI partition')
        return dict(firmware='UEFI ARM64', loader='systemd-boot', loader_sha256=hashlib.sha256(boot).hexdigest(),
                    console=['console=ttyAMA0,9600'], ukis=ukis, metadata='oracle /opc/v2/ via talos.platform=oracle (pinned Talos source; network NOT_RUN)')


def info(path, fmt):
    data = json.loads(output('qemu-img', 'info', '--output=json', str(path)))
    require(data['format'] == fmt and data['virtual-size'] == SIZE, 'disk format/virtual capacity mismatch')
    require(not data.get('backing-filename') and not data.get('full-backing-filename'), 'backing dependency')
    return data


def vmdk_descriptor(path):
    with path.open('rb') as f:
        head = f.read(512)
        require(head[:4] == b'KDMV', 'not sparse VMDK magic')
        flags = struct.unpack_from('<I', head, 8)[0]
        capacity, _, offset, length = struct.unpack_from('<QQQQ', head, 12)
        require(capacity*512 == SIZE and flags & 0x10000, 'VMDK capacity/compression flag')
        require(0 < offset <= 128 and 0 < length <= 128, 'invalid embedded descriptor bounds')
        f.seek(offset*512); text = f.read(length*512).rstrip(b'\0').decode()
    require('createType="streamOptimized"' in text and 'parentCID=ffffffff' in text,
            'not standalone streamOptimized descriptor')
    extents = [line for line in text.splitlines() if line.startswith(('RW ', 'RDONLY ', 'NOACCESS '))]
    require(len(extents) == 1, 'external/multiple VMDK extents')
    fields = shlex.split(extents[0])
    require(fields == ['RW', str(SIZE//512), 'SPARSE', path.name], 'external VMDK extent dependency')
    require('parentFileNameHint' not in text, 'VMDK parent dependency')
    return dict(create_type='streamOptimized', standalone=True, embedded_descriptor=text)


def convert(directory):
    raw = directory/'oracle-arm64.raw'
    require(raw.is_file() and raw.stat().st_size < 8*GIB, 'unexpected scratch disk')
    original = dict(sha256=sha(raw), bytes=raw.stat().st_size, gpt=gpt(raw))
    (directory/'original-imager-output.json').write_text(json.dumps(original,indent=2)+'\n')
    with raw.open('r+b') as f:
        f.truncate(SIZE)
    run('sgdisk', '-e', str(raw))
    table = gpt(raw)
    require(table['partitions'] == original['gpt']['partitions'], 'normalization altered partition bytes/bounds')
    run('sgdisk', '-v', str(raw))
    boot = boot_assets(raw, table, directory)
    qcow = directory/(PREFIX+'.qcow2'); vmdk = directory/(PREFIX+'.vmdk')
    run('qemu-img', 'convert', '-f', 'raw', '-O', 'qcow2', '-o', 'cluster_size=8192', str(raw), str(qcow))
    run('qemu-img', 'check', '-f', 'qcow2', str(qcow))
    run('qemu-img', 'convert', '-f', 'qcow2', '-O', 'vmdk', '-o', 'subformat=streamOptimized,compat6=on', str(qcow), str(vmdk))
    qi = info(qcow, 'qcow2'); vi = info(vmdk, 'vmdk'); descriptor = vmdk_descriptor(vmdk)
    run('qemu-img', 'compare', '-f', 'raw', '-F', 'qcow2', str(raw), str(qcow))
    run('qemu-img', 'compare', '-f', 'raw', '-F', 'vmdk', str(raw), str(vmdk))
    zipped = directory/(PREFIX+'.qcow2.zst')
    run('zstd', '-T2', '-8', '--no-progress', str(qcow), '-o', str(zipped))
    run('zstd', '-t', str(zipped))
    with (directory/'roundtrip.qcow2').open('xb') as f:
        run('zstd', '-d', '-c', str(zipped), stdout=f)
    require(sha(directory/'roundtrip.qcow2') == sha(qcow), 'compression roundtrip hash mismatch')
    files = {p.name: dict(sha256=sha(p), bytes=p.stat().st_size, virtual_bytes=SIZE,
                         format=fmt) for p,fmt in [(qcow,'qcow2'),(vmdk,'vmdk/streamOptimized'),(zipped,'qcow2+zstd')]}
    tools = {cmd: output(cmd, '--version').decode().splitlines()[0] for cmd in ('qemu-img','zstd','sgdisk','mcopy')}
    version = subprocess.run(['unsquashfs','-version'],capture_output=True,text=True)
    require(version.returncode in (0,1) and version.stdout.startswith('unsquashfs version ') and not version.stderr,
            'cannot establish unsquashfs version')
    tools['unsquashfs'] = version.stdout.splitlines()[0]  # 4.6.1 exits 1 for -version.
    result = dict(original_imager_output=original, normalized_source=dict(sha256=sha(raw), bytes=SIZE, gpt=table),
                  boot_assets=boot, qemu_info={'qcow2':qi,'vmdk':vi}, vmdk=descriptor, artifacts=files,
                  tools=tools, packages=Path('/oracle/packages.txt').read_text(),
                  validation=dict(generation='PASS', qcow2_check='PASS', exact_capacity='PASS', backing_free='PASS',
                                  guest_content_compare='PASS', partition_preservation='PASS', compressed_roundtrip='PASS',
                                  static_boot_assets='PASS', OCI='NOT_RUN', boot='NOT_RUN',
                                  sysbox_arm64='UNRUN', docker_in_sysbox='UNRUN', kind='UNRUN'))
    (directory/'inspection.json').write_text(json.dumps(result,indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('directory', type=Path)
    convert(parser.parse_args().directory)
