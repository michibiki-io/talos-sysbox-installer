#!/usr/bin/env python3
"""Combine native build outputs without rewriting either architecture's inputs."""
import argparse
import json
from pathlib import Path
from stage_release import validate_assets


def combine(directory, require_oracle=False):
    records = {}
    images = set()
    for arch in ('amd64', 'arm64'):
        root = directory / arch
        record = json.loads((root / 'images.json').read_text())
        if record.get('architecture') != arch:
            raise RuntimeError('Native build output architecture mismatch')
        if 'probe' not in record:
            raise RuntimeError('Release build requires probe for both targets')
        validate_assets(record, root)
        if require_oracle and arch == 'arm64' and 'disk_artifacts' not in record:
            raise RuntimeError('New release requires Standard Oracle ARM64 disk assets')
        selected = set(record['image_config_digests'])
        if images & selected:
            raise RuntimeError('Architecture outputs share image tags')
        images.update(selected)
        records[arch] = record
    result = {'schema_version': 2, 'architectures': records}
    (directory / 'images.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, default=Path('.build'))
    parser.add_argument('--require-oracle', action='store_true')
    args = parser.parse_args()
    combine(args.directory, args.require_oracle)
