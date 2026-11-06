import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import binascii

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import oracle_disk as oracle
import disk_artifacts as disks
from build import composition, file_sha256

spec = importlib.util.spec_from_file_location('oracle_inspect', ROOT/'build/oracle/inspect.py')
inspect = importlib.util.module_from_spec(spec); spec.loader.exec_module(inspect)


class OracleInputsTests(unittest.TestCase):
    def test_probe_isolated_context_contains_every_dockerfile_project_script(self):
        import build_validation_image as probe
        class Inspected(Exception):pass
        with tempfile.TemporaryDirectory() as work:
            def inspect_context(args,**kwargs):
                context=Path(args[-1])
                recipe=(context/'build/Dockerfile').read_text()
                line=next(line for line in recipe.splitlines() if line.startswith('COPY scripts/') and '/work/project/scripts/' in line)
                missing=[name for name in line.split()[1:-1] if not (context/name).is_file()]
                self.assertEqual(missing,[],'isolated probe context omits Dockerfile source dependencies')
                raise Inspected()
            with mock.patch.object(sys,'argv',['probe','--builder','fixture','--output',str(Path(work)/'out')]), \
                 mock.patch.object(probe.build,'run',side_effect=inspect_context):
                with self.assertRaises(Inspected):probe.main()

    def inputs(self):
        lock = json.loads((ROOT/'versions.arm64.lock.json').read_text())
        custom = 'ghcr.io/example/extension@sha256:'+'a'*64
        standard = dict(composition(lock,custom,'standard'),image='ghcr.io/example/standard:v0.6.0')
        return lock, {'architecture':'arm64','tag':'v0.6.0','extension':{'digestRef':custom},'installers':[standard],
                      'image_config_digests':{standard['image']:'sha256:'+'b'*64}}, {'inputs':lock}

    def test_published_standard_requires_same_architecture_versions_and_all_inputs(self):
        lock,record,metadata = self.inputs()
        self.assertEqual(oracle.selection(record,lock,metadata),record['installers'][0])
        for field,value in [('architecture','amd64'),('talos','1.13.0'),('base_installer','mutable:tag'),
                            ('extensions',['other@sha256:'+'b'*64,*record['installers'][0]['extensions'][1:]])]:
            changed = copy.deepcopy(record); changed['installers'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                oracle.selection(changed,lock,metadata)
        wrong = copy.deepcopy(metadata);wrong['inputs']['architecture']='amd64'
        with self.assertRaises(ValueError):oracle.selection(record,lock,wrong)

    def test_profile_is_oracle_arm64_raw_small_scratch_and_exact_console_only(self):
        lock,record,_ = self.inputs();p = oracle.profile(record['installers'][0],False)
        self.assertEqual((p['baseProfileName'],p['arch'],p['platform']),('oracle','arm64','oracle'))
        self.assertEqual(p['version'],'v'+lock['talos'])
        self.assertEqual(p['customization'],{'extraKernelArgs':['console=ttyAMA0,9600']})
        self.assertLess(p['output']['imageOptions']['diskSize'],8*1024**3)
        self.assertEqual(p['input']['systemExtensions'],[{'imageRef':x} for x in record['installers'][0]['extensions']])
        for ref in ('tag:latest','image@sha256:short'):
            with self.assertRaises(ValueError):oracle.fixed(ref)

    def test_collision_and_low_space_fail_before_container_mutation(self):
        lock,record,metadata = self.inputs()
        with tempfile.TemporaryDirectory() as work:
            path = Path(work)/'output';path.mkdir()
            with mock.patch.object(oracle,'call') as mutate:
                with self.assertRaises(FileExistsError):
                    oracle.generate(record,record['installers'][0],lock,lock,'arm64',metadata,path,'default')
                mutate.assert_not_called()
            path.rmdir()
            with mock.patch.object(oracle.shutil,'disk_usage',return_value=shutil._ntuple_diskusage(100,99,1)),mock.patch.object(oracle,'call') as mutate:
                with self.assertRaisesRegex(RuntimeError,'insufficient space'):
                    oracle.generate(record,record['installers'][0],lock,lock,'arm64',metadata,path,'default')
                mutate.assert_not_called()
                self.assertEqual(list(Path(work).iterdir()),[])

    def test_generator_failure_reconciles_only_named_owned_container_and_loop(self):
        lock,record,metadata = self.inputs()
        def export(ref,path,*args):
            with tarfile.open(path,'w') as tf:
                b=b'metadata: {}';m=tarfile.TarInfo('manifest.yaml');m.size=len(b);tf.addfile(m,io.BytesIO(b))
            return {'ref':ref}
        for failure in ('generation','conversion'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as work:
                out=Path(work)/'output';commands=[]
                def call(args,**kwargs):
                    commands.append(args)
                    if args[:2]==['docker','run'] and args[-1]=='generator':
                        stage = next(Path(args[i+1][:-5]) for i,x in enumerate(args) if x=='-v' and args[i+1].endswith(':/out'))
                        (stage/'loop.json').write_text('{"loop":123,"after":"unbound"}')
                        if failure=='generation':raise RuntimeError('imager failed')
                    elif args[-2:]==['tools','/out']:
                        raise RuntimeError('conversion failed')
                with mock.patch.object(oracle,'export_extension',side_effect=export),mock.patch.object(oracle,'budget'), \
                     mock.patch.object(oracle,'build_tools',return_value={'generator':'generator','tools':'tools'}), \
                     mock.patch.object(oracle,'capture',return_value='["sysbox", "iscsi-tools", "util-linux-tools"]'), \
                     mock.patch.object(oracle,'call',side_effect=call), \
                     mock.patch.object(oracle.subprocess,'run',return_value=__import__('subprocess').CompletedProcess([],0,'[]','')):
                    with self.assertRaisesRegex(RuntimeError,'failed'):
                        oracle.generate(record,record['installers'][0],lock,lock,'arm64',metadata,out,'default',standard_ref='sha256:'+'b'*64)
                self.assertFalse(out.exists())
                self.assertEqual(len(list(Path(work).glob('*.partial-*/FAILED.txt'))),1)
                stop = [c for c in commands if c[:2]==['docker','stop'] and c[-1].endswith('-generator')]
                self.assertEqual(len(stop),1);self.assertTrue(stop[0][-1].startswith('talos-oracle-'))
                audit = [c for c in commands if c[-1]=='audit'];self.assertEqual(len(audit),1)
                self.assertNotIn('--privileged',audit[0]);self.assertNotIn('/dev:/dev',str(commands))
                self.assertFalse(any('-a' in c for c in commands))


class OracleFormatTests(unittest.TestCase):
    def test_uki_version_os_release_and_kernel_release_match_standard(self):
        sections={'.osrel':b'NAME="Talos"\nVERSION_ID=v1.12.6\n','.uname':b'6.18.14-talos\0'}
        result=inspect.uki_identity('Talos-v1.12.6.efi',sections,sections,'1.12.6')
        self.assertEqual(result['kernel_release'],'6.18.14-talos')
        for name,changed in [('Talos-1.12.6.efi',sections),('Talos-v1.12.6.efi',dict(sections,**{'.uname':b''})),
                             ('Talos-v1.12.6.efi',dict(sections,**{'.osrel':b'VERSION_ID=1.12.6\n'}))]:
            with self.subTest(name=name),self.assertRaises(ValueError):inspect.uki_identity(name,changed,sections,'1.12.6')

    def test_only_observed_talos_tmp_sticky_normalization_is_allowed_and_reported(self):
        path='usr/local/lib/containers/sysbox-fs/tmp'
        expected={path:('directory',0o1777,'')};actual={path:('directory',0o777,'')}
        self.assertEqual(inspect.compare_inventory(actual,expected)[0]['embedded_mode'],'0777')
        for changed in ({path:('directory',0o755,'')},{'different':('directory',0o777,'')},{}):
            with self.assertRaises(ValueError):inspect.compare_inventory(changed,expected)
    def gpt(self,path,sectors=512):
        table=bytearray(128*128)
        for i,(name,start,end) in enumerate([('EFI',34,60),('META',61,63)]):
            at=i*128;table[at:at+16]=b'x'*16;struct.pack_into('<QQ',table,at+32,start,end)
            label=name.encode('utf-16-le');table[at+56:at+56+len(label)]=label
        with path.open('wb') as f:f.truncate(sectors*512)
        for current,backup,entries in [(1,sectors-1,2),(sectors-1,1,sectors-33)]:
            h=bytearray(512);h[:8]=b'EFI PART';struct.pack_into('<III',h,8,0x10000,92,0)
            struct.pack_into('<QQQQ',h,24,current,backup,34,sectors-34)
            struct.pack_into('<QIII',h,72,entries,128,128,binascii.crc32(table))
            struct.pack_into('<I',h,16,binascii.crc32(h[:92]))
            with path.open('r+b') as f:f.seek(current*512);f.write(h);f.seek(entries*512);f.write(table)

    def test_gpt_requires_final_backup_location_and_both_crcs(self):
        with tempfile.TemporaryDirectory() as work:
            p=Path(work)/'disk';self.gpt(p)
            self.assertEqual([x['label'] for x in inspect.gpt(p)['partitions']],['EFI','META'])
            with p.open('r+b') as f:f.truncate(1024*512)
            with self.assertRaisesRegex(ValueError,'GPT signature'):inspect.gpt(p)
            self.gpt(p)
            with p.open('r+b') as f:f.seek(512+24);f.write(b'bad')
            with self.assertRaisesRegex(ValueError,'CRC'):inspect.gpt(p)

    def test_qemu_info_rejects_wrong_format_size_and_backing(self):
        good={'format':'qcow2','virtual-size':disks.SIZE}
        for data in [dict(good,format='raw'),dict(good,**{'virtual-size':disks.SIZE-512}),dict(good,**{'backing-filename':'other'})]:
            with mock.patch.object(inspect,'output',return_value=json.dumps(data).encode()),self.assertRaises(ValueError):
                inspect.info(Path('disk'),'qcow2')

    def test_pe_rejects_amd64_efi_assets(self):
        data=bytearray(512);data[:2]=b'MZ';struct.pack_into('<I',data,60,64);data[64:68]=b'PE\0\0'
        struct.pack_into('<HH',data,68,0x8664,0)
        with self.assertRaisesRegex(ValueError,'not ARM64'):inspect.pe(data)

    def test_compression_is_detected_by_magic_and_unknown_tail_fails(self):
        with self.assertRaisesRegex(ValueError,'compression magic'):inspect.initramfs(b'not really xz')
        with self.assertRaisesRegex(ValueError,'missing extension'):inspect.initramfs(b'\0'*512)


class OracleReleaseTests(unittest.TestCase):
    def fixture(self,root,image=None,boot='NOT_RUN'):
        lock,record,_=OracleInputsTests().inputs();dest=root/'oracle';dest.mkdir()
        if image:record['installers'][0]['image']=image
        files={}
        artifacts={}
        for suffix in ('.qcow2.zst','.vmdk','.qcow2'):
            name=disks.PREFIX+suffix
            artifacts[name]={'sha256':'a'*64,'bytes':1,'virtual_bytes':disks.SIZE,'format':suffix}
            if suffix!='.qcow2':
                path=dest/name;path.write_bytes(b'x');artifacts[name]['sha256']=file_sha256(path)
        validation={k:'PASS' for k in ('generation','qcow2_check','exact_capacity','backing_free','guest_content_compare','partition_preservation','compressed_roundtrip','static_boot_assets')}
        validation.update(OCI='NOT_RUN',boot=boot,sysbox_arm64='UNRUN',docker_in_sysbox='UNRUN',kind='UNRUN')
        metadata={'schema_version':1,'architecture':'arm64','platform':'oracle','variant':'standard','standard':record['installers'][0],
                  'extensions':record['installers'][0]['extensions'],'talos':lock['talos'],'sysbox':lock['sysbox'],
                  'validation':validation,'artifacts':artifacts}
        meta=dest/(disks.PREFIX+'.inputs.json');meta.write_text(json.dumps(metadata))
        checksums=dest/(disks.PREFIX+'.sha256');checksums.write_text(''.join(v['sha256']+'  '+n+'\n' for n,v in artifacts.items())+file_sha256(meta)+'  '+meta.name+'\n')
        for name in disks.FILENAMES:files[name]={'bytes':(dest/name).stat().st_size,'sha256':file_sha256(dest/name)}
        record['disk_artifacts']={'architecture':'arm64','platform':'oracle','variant':'standard','inputs':meta.name,'files':files}
        return record

    def test_original_releases_unchanged_and_new_asset_names_not_double_prefixed(self):
        self.assertEqual(disks.disk_paths({},Path('/nonexistent')),[])
        for name in disks.FILENAMES:self.assertEqual(disks.release_asset_name('arm64',Path(name)),name)
        self.assertEqual(disks.release_asset_name('arm64',Path('extension.tar')),'arm64-extension.tar')

    def test_stage_uploads_disks_before_marker_and_corruption_prevents_restore_mutations(self):
        import test_build
        stage=test_build.stage;reuse=test_build.reuse
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);record=test_build.BuildTests().original_archives(root,'arm64')
            disk_inputs=self.fixture(root,record['installers'][0]['image'])
            record['installers'][0]=disk_inputs['installers'][0]
            record['installers'][1]['variant']='vmware'
            record['disk_artifacts']=disk_inputs['disk_artifacts']
            (root/'images.json').write_text(json.dumps(record))
            with mock.patch.object(stage,'find_release',return_value=None),mock.patch.object(stage.subprocess,'run') as run:
                stage.stage('example/repo','v0.6.0',root,{'preflight':True,'inputs':record})
                commands=[c.args[0] for c in run.call_args_list]
                for n in disks.FILENAMES:self.assertIn(str(root/'oracle'/n),commands[1])
                self.assertEqual(commands[-1][-1],str(root/'images.json'))
            with (root/'oracle'/disks.FILENAMES[1]).open('ab') as f:f.write(b'corrupt')
            with mock.patch.object(reuse.subprocess,'run') as mutate:
                with self.assertRaisesRegex(RuntimeError,'modified'):reuse.restore_images(record,root)
                mutate.assert_not_called()
            with mock.patch.object(stage,'find_release') as lookup,mock.patch.object(stage.subprocess,'run') as mutate:
                with self.assertRaises(RuntimeError):stage.stage('example/repo','v0.6.0',root,{'preflight':True,'inputs':record})
                lookup.assert_not_called();mutate.assert_not_called()

    def test_missing_modified_or_mismatched_metadata_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);record=self.fixture(root);self.assertEqual(len(disks.disk_paths(record,root)),4)
            p=root/'oracle'/disks.FILENAMES[0];original=p.read_bytes();p.write_bytes(b'corrupt')
            with self.assertRaisesRegex(RuntimeError,'modified'):disks.disk_paths(record,root)
            p.write_bytes(original);p.unlink()
            with self.assertRaises(RuntimeError):disks.disk_paths(record,root)
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);record=self.fixture(root);record['installers'][0]['extensions'][0]='wrong'
            with self.assertRaisesRegex(RuntimeError,'Standard inputs'):disks.disk_paths(record,root)

    def test_consistently_hashed_boot_pass_claim_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);record=self.fixture(root,boot='PASS')
            with self.assertRaisesRegex(RuntimeError,'exceeds this build scope'):disks.disk_paths(record,root)


if __name__=='__main__':unittest.main()
