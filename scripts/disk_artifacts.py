"""Backward-compatible release records for non-container Oracle disk assets."""
import json
from pathlib import Path
from build import file_sha256

PREFIX = 'arm64-standard-oracle'
FILENAMES = tuple(PREFIX+s for s in ('.qcow2.zst','.vmdk','.inputs.json','.sha256'))
SIZE = 53_687_091_200


def disk_record(inputs):
    record = inputs.get('disk_artifacts')
    if record is None:
        return None  # Original releases remain valid without Oracle disks.
    if (inputs.get('architecture') != 'arm64' or
        {k:record.get(k) for k in ('architecture','platform','variant')} !=
        {'architecture':'arm64','platform':'oracle','variant':'standard'} or
        record.get('inputs') != PREFIX+'.inputs.json' or set(record.get('files',{})) != set(FILENAMES)):
        raise RuntimeError('Oracle disk release record mismatch')
    return record


def disk_paths(inputs, directory):
    record = disk_record(inputs)
    if record is None:
        return []
    paths = [directory/'oracle'/name for name in FILENAMES]
    for path in paths:
        expected = record['files'][path.name]
        if (not path.is_file() or path.is_symlink() or path.stat().st_size != expected['bytes'] or
            path.stat().st_size >= 2*1024**3 or file_sha256(path) != expected['sha256']):
            raise RuntimeError('Missing, oversized or modified Oracle disk asset: '+path.name)
    metadata = json.loads((directory/'oracle'/record['inputs']).read_text())
    standard, = [i for i in inputs['installers'] if i['variant'] == 'standard']
    if (metadata.get('schema_version') != 1 or metadata.get('architecture') != 'arm64' or
        metadata.get('platform') != 'oracle' or metadata.get('variant') != 'standard' or
        metadata.get('standard') != standard or metadata.get('extensions') != standard['extensions'] or
        metadata.get('talos') != standard['talos'] or metadata.get('sysbox') != standard['sysbox']):
        raise RuntimeError('Oracle metadata differs from Standard inputs')
    validation = metadata['validation']
    for name in ('generation','qcow2_check','exact_capacity','backing_free','guest_content_compare',
                 'partition_preservation','compressed_roundtrip','static_boot_assets'):
        if validation.get(name) != 'PASS':
            raise RuntimeError('Incomplete Oracle validation: '+name)
    if validation.get('OCI') != 'NOT_RUN' or any(validation.get(k) != 'UNRUN' for k in ('sysbox_arm64','docker_in_sysbox','kind')):
        raise RuntimeError('Oracle/runtime validation exceeds this build scope')
    expected_names = {PREFIX+s for s in ('.qcow2','.vmdk','.qcow2.zst')}
    if set(metadata['artifacts']) != expected_names:
        raise RuntimeError('Oracle artifact inventory mismatch')
    for name,item in metadata['artifacts'].items():
        if item['virtual_bytes'] != SIZE:
            raise RuntimeError('Oracle virtual capacity mismatch')
        if name in record['files'] and any(item[k] != record['files'][name][k] for k in ('sha256','bytes')):
            raise RuntimeError('Oracle file/metadata metrics mismatch')
    expected_checksums = {name:item['sha256'] for name,item in metadata['artifacts'].items()}
    expected_checksums[record['inputs']] = record['files'][record['inputs']]['sha256']
    lines = (directory/'oracle'/(PREFIX+'.sha256')).read_text().splitlines()
    if len(lines) != len(expected_checksums) or {line[66:]:line[:64] for line in lines if line[64:66] == '  '} != expected_checksums:
        raise RuntimeError('Oracle checksum list mismatch')
    return paths


def release_asset_name(arch, path):
    # Disk names already include target architecture; container/source names do not.
    return path.name if path.name in FILENAMES else (arch+'-' if arch else '')+path.name
