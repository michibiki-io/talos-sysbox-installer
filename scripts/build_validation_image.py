#!/usr/bin/env python3
"""Build a locked probe archive and corresponding sources; never publish."""
import argparse
import json
import re
import subprocess
import shutil
import sys
import tempfile
import tarfile
from pathlib import Path
import build
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'build'))
import proc_transfer


def verify_flattened_archive(archive):
    """Reject inherited layers in the actual exported distribution artifact."""
    with tarfile.open(archive) as tf:
        entry, = json.load(tf.extractfile('manifest.json'))
        config = json.load(tf.extractfile(entry['Config']))
    count = len(config['rootfs']['diff_ids'])
    if count != 1 or len(entry['Layers']) != 1:
        raise RuntimeError('probe image must contain exactly one rootfs layer')
    return count


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--builder', required=True)
    p.add_argument('--image', help='Optional immutable release tag; build/export only')
    p.add_argument('--production-metadata', type=Path)
    p.add_argument('--lock', type=Path, default=ROOT / 'versions.lock.json')
    p.add_argument('--network', choices=('default', 'host'), default='default')
    a = p.parse_args()
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / 'test-images.lock.json').unlink(missing_ok=True)
    lock = json.loads(a.lock.read_text())
    source = proc_transfer.load_lock(ROOT / 'build/proc-transfer.lock.json')
    image = 'docker.io/talos-sysbox-local/proc-transfer-probe:' + source['revision'][:12]
    if a.image:
        if not re.fullmatch(r'ghcr.io/[a-z0-9_.-]+/talos-sysbox-proc-transfer-probe:v\d+\.\d+\.\d+-talos-v\d+\.\d+\.\d+', a.image):
            p.error('invalid immutable probe release tag')
        image = a.image
    with tempfile.TemporaryDirectory(prefix='validation-context-') as work:
        context = Path(work)
        for name in ('build', 'supervisor'):
            shutil.copytree(ROOT / name, context / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (context / 'scripts').mkdir()
        for name in ('build.py', 'publish.py', 'build_validation_image.py', 'update_proc_transfer.py', 'proc_transfer_operator.py'):
            shutil.copy2(ROOT / 'scripts' / name, context / 'scripts' / name)
        shutil.copy2(a.lock, context / 'versions.lock.json')
        shutil.copy2(ROOT / 'LICENSE', context / 'LICENSE')
        args = ['docker', 'buildx', 'build', '--builder', a.builder, '--platform', 'linux/amd64', '--network', a.network,
                '-f', str(context / 'build/Dockerfile')]
        for name, value in {'GO_IMAGE': lock['go'], 'UBUNTU_IMAGE': lock['ubuntu'], 'APT_SNAPSHOT': lock['apt_snapshot'], 'CATALOGUE_IMAGE': lock['catalogue']}.items():
            args += ['--build-arg', name + '=' + value]
        build.run([*args, '--target', 'validation-probe', '--load', '-t', image, str(context)])
        # Export the source-built static ctr for a public BusyBox bootstrap Pod.
        # It imports the probe into Talos CRI without a pre-existing node shell.
        with tempfile.TemporaryDirectory(prefix='validation-tools-', dir=out) as staging:
            export = args + ['--target', 'validation-tools', '--output', 'type=local,dest=' + staging]
            build.run([*export, str(context)])
            if (out / 'tools').exists(): shutil.rmtree(out / 'tools')
            shutil.move(staging, out / 'tools')
        with tempfile.TemporaryDirectory(prefix='probe-sources-', dir=out) as staging:
            build.run([*args, '--target', 'validation-sources', '--output', 'type=local,dest=' + staging, str(context)])
            if (out / 'corresponding-source').exists(): shutil.rmtree(out / 'corresponding-source')
            shutil.move(staging, out / 'corresponding-source')
    if a.production_metadata:
        expected = json.loads(a.production_metadata.read_text())
        actual = json.loads((out / 'corresponding-source/production-build-metadata.json').read_text())
        for key in ('binary_sha256', 'supervisor_sha256', 'source_inputs_sha256'):
            if expected[key] != actual[key]:
                raise RuntimeError('probe and installers were built from different inputs/outputs')
    user = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Config.User}}', image], text=True).strip()
    if user != '1001:1001':
        raise RuntimeError('probe image must default to UID/GID 1001')
    archive = out / 'probe-image.tar'
    build.run(['docker', 'save', '-o', str(archive), image])
    rootfs_layer_count = verify_flattened_archive(archive)
    with tempfile.TemporaryDirectory(prefix='probe-oci-', dir=out) as work:
        layout = Path(work) / 'layout'
        oci_digest = build.docker_to_oci(archive, layout)
        index = json.loads((layout / 'index.json').read_text())
        index['manifests'][0]['annotations'] = {'org.opencontainers.image.ref.name': image}
        (layout / 'index.json').write_text(json.dumps(index))
        with tarfile.open(out / 'probe-oci.tar', 'w') as exported:
            for path in sorted(layout.iterdir()):
                exported.add(path, arcname=path.name, filter=build.archive_member)
    record = {'schema_version': 1, 'test_images': {'proc_transfer_probe': {
        'local_tag': image, 'oci_digest': oci_digest, 'config_digest': build.archive_config_digest(archive),
        'archive': 'probe-oci.tar', 'archive_sha256': build.file_sha256(out / 'probe-oci.tar'),
        'docker_archive': archive.name, 'docker_archive_sha256': build.file_sha256(archive), 'anonymous_pull_verified': False,
        'rootfs_layer_count': rootfs_layer_count,
        'distribution': 'local-archive-import-only', 'architecture': 'amd64', 'uid': 1001, 'gid': 1001,
        'bubblewrap': '0.12.0', 'launcher_python_minimum': '3.12'},
        'proc_transfer_bootstrap': lock['test_images']['busybox']},
        'validation_artifacts_lock_sha256': build.file_sha256(out / 'tools/artifacts.lock.json'),
        'bootstrap_ctr_sha256': build.file_sha256(out / 'tools/ctr'),
        'image_versions_sha256': build.file_sha256(out / 'tools/image-versions.json'),
        'image_versions': json.loads((out / 'tools/image-versions.json').read_text()),
        'source_lock_sha256': proc_transfer.sha256(ROOT / 'build/proc-transfer.lock.json'),
        'inputs': {'go': lock['go'], 'ubuntu': lock['ubuntu'], 'apt_snapshot': lock['apt_snapshot'], 'bubblewrap': lock['validation_sources']['bubblewrap']}}
    (out / 'test-images.lock.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')

if __name__ == '__main__':
    main()
