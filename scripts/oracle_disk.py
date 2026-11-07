#!/usr/bin/env python3
"""Generate Oracle ARM64 disks from exact Standard inputs; tools run in containers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import uuid

from build import ROOT, composition, file_sha256, select_lock, archive_config_digest

PREFIX = 'arm64-standard-oracle'


def fixed(ref):
    if not re.fullmatch(r'.+@sha256:[0-9a-f]{64}', ref):
        raise ValueError('input must be digest pinned: '+str(ref))
    return ref


def call(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def capture(args):
    return subprocess.check_output(args, text=True).strip()


def selection(record, lock, metadata):
    if record.get('architecture') != 'arm64' or metadata['inputs'] != lock:
        raise ValueError('Standard metadata/lock architecture or inputs mismatch')
    standard, = [i for i in record['installers'] if i['variant'] == 'standard']
    expected = composition(lock, standard['extensions'][0], 'standard')
    for key,value in expected.items():
        if standard.get(key) != value:
            raise ValueError('Standard input mismatch: '+key)
    custom = standard['extensions'][0]
    if custom.startswith('oci-layout@'):
        if custom != 'oci-layout@'+record['extension']['oci_digest']:
            raise ValueError('Standard OCI layout mismatch')
    elif fixed(custom) != record['extension']['digestRef']:
        raise ValueError('published Standard extension mismatch')
    return standard


def verify_layout(directory, digest):
    index = json.loads((directory/'index.json').read_text())
    descriptor, = index['manifests']
    if descriptor['digest'] != digest or descriptor['platform'] != {'architecture':'arm64','os':'linux'}:
        raise ValueError('OCI layout platform/manifest mismatch')
    def read(desc):
        path = directory/'blobs/sha256'/desc['digest'].removeprefix('sha256:')
        if file_sha256(path) != desc['digest'].removeprefix('sha256:') or path.stat().st_size != desc['size']:
            raise ValueError('OCI layout blob hash/size mismatch')
        return path
    manifest = json.loads(read(descriptor).read_text())
    config = json.loads(read(manifest['config']).read_text())
    if config['architecture'] != 'arm64' or config['os'] != 'linux':
        raise ValueError('OCI configuration is not ARM64')
    for layer in manifest['layers']:
        read(layer)


def profile(standard, layout):
    extensions = [({'ociPath':'/input/extension-oci'} if layout and i == 0 else {'imageRef':fixed(ref)})
                  for i,ref in enumerate(standard['extensions'])]
    return {'baseProfileName':'oracle', 'arch':'arm64', 'platform':'oracle', 'version':'v'+standard['talos'],
            'input':{'baseInstaller':{'imageRef':fixed(standard['base_installer'])}, 'systemExtensions':extensions},
            'customization':{'extraKernelArgs':['console=ttyAMA0,9600']},
            'output':{'kind':'image','outFormat':'raw','imageOptions':{'diskSize':2*1024**3,'diskFormat':'raw','bootloader':'sd-boot'}}}


def budget(directory):
    # Small imager scratch + bounded conversion/extraction copies;
    # leave margin for unrelated work. This is not a 50 GiB raw allocation.
    if shutil.disk_usage(directory).free < 12*1024**3:
        raise RuntimeError('insufficient space: need at least 12 GiB free for Oracle generation')


def container_limits():
    cpus = sorted(os.sched_getaffinity(0))[:2]
    return ['--cpus','2','--cpuset-cpus',','.join(map(str,cpus)), '--memory','4g','--memory-swap','4g',
            '--pids-limit','256','--security-opt','no-new-privileges']


def generator_permissions():
    # Official rootless imager: no mount/mknod capability, device rules or host /dev.
    return ['--cap-drop','ALL']


def reconcile_container(name):
    state = subprocess.run(['docker','inspect',name],capture_output=True,text=True)
    if state.returncode == 0:
        call(['docker','stop','--time','30',name],stdout=subprocess.DEVNULL)
        call(['docker','rm',name],stdout=subprocess.DEVNULL)
    elif 'No such' not in state.stderr:
        raise RuntimeError('cannot establish owned container state: '+name)


def build_tools(executor, arch, stage, builder):
    with tempfile.TemporaryDirectory(prefix='oracle-context-') as tmp:
        context = Path(tmp); (context/'build').mkdir()
        shutil.copytree(ROOT/'build/oracle', context/'build/oracle', ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        shutil.copy2(ROOT/'build/apt_retry.sh', context/'build/apt_retry.sh')
        images = {}
        for target in ('generator','tools'):
            iid = stage/(target+'.iid')
            call(['docker','buildx','build','--builder',builder,'--platform','linux/'+arch,'--load',
                  '--iidfile',str(iid),'--build-arg','GO_IMAGE='+fixed(executor['go']),
                  '--build-arg','UBUNTU_IMAGE='+fixed(executor['ubuntu']),
                  '--build-arg','IMAGER_IMAGE='+fixed(executor['imager']),
                  '--build-arg','APT_SNAPSHOT='+executor['apt_snapshot'],'--target',target,
                  '-f',str(context/'build/oracle/Dockerfile'),str(context)])
            images[target] = iid.read_text().strip()
        return images


def export_extension(ref, path, nonce, expected_config=None):
    if '@sha256:' in ref and not ref.startswith('oci-layout@'):
        call(['docker','pull','--platform','linux/arm64',fixed(ref)])
    config = json.loads(capture(['docker','image','inspect',ref]))[0]
    if config['Architecture'] != 'arm64' or config['Os'] != 'linux' or (expected_config and config['Id'] != expected_config):
        raise ValueError('extension archive/input architecture or configuration mismatch')
    name = nonce+'-'+path.stem
    container = capture(['docker','create','--name',name,'--label','talos.oracle.task='+nonce,
                         '--platform','linux/arm64','--entrypoint','/never-executed',config['Id']])
    try:
        call(['docker','export','-o',str(path),container])
    finally:
        call(['docker','rm',container])
    return {'ref':ref, 'config_digest':config['Id'], 'architecture':config['Architecture'],
            'export_sha256':file_sha256(path)}


def generate(record, standard, lock, executor, arch, metadata, output, builder, local=None, project_version=None, standard_ref=None):
    if output.exists():
        raise FileExistsError('refusing existing Oracle output: '+str(output))
    output.parent.mkdir(parents=True,exist_ok=True); budget(output.parent)
    nonce = 'talos-oracle-'+uuid.uuid4().hex
    stage = Path(tempfile.mkdtemp(prefix=output.name+'.partial-',dir=output.parent))
    recipe = {str(p.relative_to(ROOT)):file_sha256(p) for p in
              [ROOT/'scripts/oracle_disk.py',ROOT/'build/apt_retry.sh',*sorted((ROOT/'build/oracle').glob('*'))] if p.is_file()}
    # A failed directory is evidence, never a successful artifact set. It is
    # retained for inspection; a retry always uses a fresh unique directory.
    try:
        (stage/'build-metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
        custom = standard['extensions'][0]; layout = custom.startswith('oci-layout@')
        if layout:
            if local is None:
                raise ValueError('local OCI Standard requires --build-output')
            verify_layout(local/'extension-oci',record['extension']['oci_digest'])
            if archive_config_digest(local/'extension.tar') != record['image_config_digests'][record['extension']['image']]:
                raise ValueError('local extension archive differs from Standard configuration')
        request = profile(standard,layout)
        (stage/'profile.yaml').write_text(json.dumps(request,indent=2)+'\n')
        extension_names = []
        exports = []
        # Never start ARM64 payload containers on an AMD64 executor.
        for i,ref in enumerate(standard['extensions']):
            actual = record['extension']['image'] if layout and i == 0 else ref
            expected = record['image_config_digests'][actual] if layout and i == 0 else None
            export = stage/f'extension-{i}.tar'
            exports.append(export_extension(actual,export,nonce,expected))
            import tarfile
            with tarfile.open(export) as tf:
                # The manifest is parsed inside the tools container; preserve
                # exact bytes here without installing a host YAML package.
                (stage/f'manifest-{i}.yaml').write_bytes(tf.extractfile('manifest.yaml').read())
        if standard_ref is None:
            raise ValueError('immutable Standard installer verification reference required')
        standard_export = export_extension(standard_ref,stage/'standard.tar',nonce,
                                          record['image_config_digests'].get(standard['image']))
        images = build_tools(executor,arch,stage,builder)
        # Resolve extension identity with the same container-only YAML parser.
        try:
            names = capture(['docker','run','--name',nonce+'-names','--label','talos.oracle.task='+nonce,
                             *container_limits(),'--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--network','none',
                             '-v',str(stage)+':/out:ro','--entrypoint','python3',images['tools'],'-c',
                             'import yaml,json; print(json.dumps([yaml.safe_load(open("/out/manifest-%s.yaml"%i))["metadata"]["name"] for i in range(3)]))'])
        finally:
            reconcile_container(nonce+'-names')
        extension_names = json.loads(names)
        (stage/'request.json').write_text(json.dumps({'extension_names':extension_names})+'\n')
        name = nonce+'-generator'
        base = ['docker','run','--name',name,'--label','talos.oracle.task='+nonce,*container_limits(),
                '--user',f'{os.getuid()}:{os.getgid()}',*generator_permissions(),'--tmpfs','/tmp:rw,nosuid,nodev,size=2g',
                '-v',str(stage)+':/out']
        if layout:
            base += ['-v',str(local)+':/input:ro']
        try:
            with (stage/'generator.log').open('wb') as log:
                call([*base,'-i',images['generator']],input=json.dumps(request).encode(),stdout=log,stderr=subprocess.STDOUT)
        finally:
            # Remove only our named container; no host loop/device state is used.
            reconcile_container(name)
        budget(stage)
        try:
            with (stage/'conversion.log').open('wb') as log:
                call(['docker','run','--name',nonce+'-convert','--label','talos.oracle.task='+nonce,
                      '--network','none',*container_limits(),'--cap-drop','ALL','--cap-add','DAC_OVERRIDE',
                      '--tmpfs','/tmp:rw,nosuid,nodev,size=512m','-v',str(stage)+':/out',
                      images['tools'],'/out'],stdout=log,stderr=subprocess.STDOUT)
        finally:
            reconcile_container(nonce+'-convert')
        inspection = json.loads((stage/'inspection.json').read_text())
        source_commit = capture(['git','-C',str(ROOT),'rev-parse','HEAD'])
        source_dirty = bool(capture(['git','-C',str(ROOT),'status','--porcelain','--untracked-files=normal']))
        if any(file_sha256(ROOT/name) != digest for name,digest in recipe.items()):
            raise RuntimeError('generation recipe changed while running; refusing false provenance')
        result = {'schema_version':1,'project_version':project_version or record['tag'], 'source_commit':source_commit,
                  'talos':lock['talos'],'sysbox':lock['sysbox'],'architecture':'arm64','platform':'oracle','variant':'standard',
                  'executor_architecture':arch, 'imager':executor['imager'], 'standard':standard,
                  'base_installer':lock['base_installer'], 'extensions':standard['extensions'],
                  'base_installer_role':'Standard composition/profile reference; disk boot assets come from pinned executor imager /usr/install/arm64 and are compared with Standard installer',
                  'extension_exports':exports, 'standard_installer_export':standard_export,
                  'source_dirty':source_dirty, 'executor_inputs':executor, 'tool_images':images,
                  'profile':request,'profile_sha256':file_sha256(stage/'profile.yaml'),'generation_sha256':recipe,
                  'build_metadata_sha256':file_sha256(stage/'build-metadata.json'),
                  'isolation':{'cpus':2,'memory_bytes':4*1024**3,'pids':256,'tmpfs_bytes':2*1024**3,
                               'host_dev_bind':False,'generator_uid':os.getuid(),'generator_capabilities':[],
                               'loop':{'status':'NOT_USED','reason':'official rootless Talos imager'},
                               'container_removed':True,'scratch_initial_request_bytes':2*1024**3}, **inspection}
        meta = stage/(PREFIX+'.inputs.json'); meta.write_text(json.dumps(result,indent=2)+'\n')
        checksum = stage/(PREFIX+'.sha256')
        checksum.write_text(''.join(v['sha256']+'  '+k+'\n' for k,v in inspection['artifacts'].items())+file_sha256(meta)+'  '+meta.name+'\n')
        files = [PREFIX+suffix for suffix in ('.qcow2.zst','.vmdk','.inputs.json','.sha256')]
        # Success directory and success record are published only after every
        # check. Large scratch/exports stay out of the distributable directory.
        final = stage/'complete';final.mkdir()
        for filename in files:
            path = stage/filename
            if path.stat().st_size >= 2*1024**3:
                raise RuntimeError('Oracle release asset exceeds existing 2 GiB limit')
            path.rename(final/filename)
        final.rename(output)
        return {'architecture':'arm64','platform':'oracle','variant':'standard',
                'inputs':PREFIX+'.inputs.json','files':{n:{'sha256':file_sha256(output/n),'bytes':(output/n).stat().st_size} for n in files}}
    except BaseException as error:
        (stage/'FAILED.txt').write_text(type(error).__name__+': '+str(error)+'\n')
        print('Oracle generation failed; evidence retained at '+str(stage),file=__import__('sys').stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--build-output',type=Path)
    group.add_argument('--release-inputs',type=Path)
    parser.add_argument('--build-metadata',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--project-version')
    parser.add_argument('--builder',default=os.environ.get('BUILDX_BUILDER','default'))
    a = parser.parse_args()
    arch = {'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())
    if arch is None:
        parser.error('unsupported executor architecture')
    _, lock = select_lock(target='arm64'); _, executor = select_lock(target=arch)
    local = a.build_output.resolve() if a.build_output else None
    if local:
        record = json.loads((local/'images.json').read_text())
        metadata_path = local/'evidence/build-metadata.json'
        destination = a.output.resolve() if a.output else local/'oracle'
        if record.get('disk_artifacts'):
            raise RuntimeError('original build already has Oracle artifacts; refusing replacement')
    else:
        if not a.build_metadata or not a.output:
            parser.error('--release-inputs requires --build-metadata and --output')
        published = json.loads(a.release_inputs.read_text())
        record = published['inputs']['architectures']['arm64']
        metadata_path = a.build_metadata
        destination = a.output.resolve()
    metadata = json.loads(metadata_path.read_text())
    standard = selection(record,lock,metadata)
    standard_ref = record['image_config_digests'][standard['image']] if local else next(
        fixed(i['digestRef']) for i in published['images'] if i['image'] == standard['image'])
    result = generate(record,standard,lock,executor,arch,metadata,destination,a.builder,local,a.project_version,standard_ref)
    if local:
        record['disk_artifacts'] = result
        temporary = local/'images.oracle.tmp'
        temporary.write_text(json.dumps(record,indent=2)+'\n'); temporary.replace(local/'images.json')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
