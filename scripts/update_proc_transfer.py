#!/usr/bin/env python3
"""Generate a reviewable candidate lock; never update the approved lock implicitly."""
import argparse
import json
import sys
import tempfile
import urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'build'))
import proc_transfer as integration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--versions', type=Path, default=ROOT / 'versions.lock.json')
    args = parser.parse_args()
    if args.output.resolve() == (ROOT / 'build/proc-transfer.lock.json').resolve() or args.output.exists():
        parser.error('use a new candidate file, then review the diff before replacing the approved lock')
    baseline = integration.load_lock(ROOT / 'build/proc-transfer.lock.json')
    url = integration.archive_url(args.revision)
    with tempfile.TemporaryDirectory() as work:
        archive = Path(work) / 'source.tar.gz'
        archive.write_bytes(urllib.request.urlopen(url, timeout=120).read())
        root = Path(work) / 'source'
        integration.unpack(archive, root)
        integration.verify_manifest(root, json.loads(args.versions.read_text()))
        integration.verify_production_inputs(root, baseline)
        lock = dict(schema_version=1, contract=integration.CONTRACT,
                    repository=integration.REPOSITORY, revision=args.revision,
                    url=url, sha256=integration.sha256(archive), input_sha256=integration.inventory(root))
        if not integration.REQUIRED <= set(lock['input_sha256']):
            raise ValueError('candidate lacks required inputs')
        args.output.write_text(json.dumps(lock, indent=2, sort_keys=True) + '\n')

if __name__ == '__main__':
    main()
