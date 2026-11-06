import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'build'))
import ubuntu_sources as sources


class UbuntuSourcesTests(unittest.TestCase):
    snapshot='20261001T000000Z'
    data=b'exact authenticated source'

    def uri(self):
        digest=hashlib.sha256(self.data).hexdigest()
        return "'https://snapshot.ubuntu.com/ubuntu/"+self.snapshot+"/pool/main/d/dash/dash_1.dsc' dash_1.dsc "+str(len(self.data))+' SHA256:'+digest+'\n'

    def test_only_exact_snapshot_sha256_records_are_accepted(self):
        record,=sources.archive_inputs(self.uri(),self.snapshot)
        self.assertEqual(record['url'],'https://archive.ubuntu.com/ubuntu/pool/main/d/dash/dash_1.dsc')
        for text in ('no records',self.uri().replace('SHA256:','MD5Sum:'),self.uri().replace(self.snapshot,'20261002T000000Z'),
                     self.uri().replace('snapshot.ubuntu.com','untrusted.example'),self.uri().replace("' dash_1.dsc","' ../dash_1.dsc"),
                     self.uri()+self.uri().replace(' '+str(len(self.data))+' ',' 999 ')):
            with self.subTest(text=text),self.assertRaises(ValueError):sources.archive_inputs(text,self.snapshot)

    def test_successful_apt_has_no_fallback_or_source_input_changes(self):
        with mock.patch.object(sources.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'download log\n','retry log\n')) as apt, \
             mock.patch.object(sources.subprocess,'check_output') as uris,mock.patch('sys.stdout',new_callable=io.StringIO) as log:
            self.assertEqual(sources.download(['dash=1'],Path('/sources'),self.snapshot),[])
            self.assertIn('download log',log.getvalue());self.assertIn('retry log',log.getvalue())
            apt.assert_called_once();uris.assert_not_called()
            self.assertEqual(apt.call_args.args[0],['sh','/work/apt_retry.sh','source','--download-only','dash=1'])

    def test_auth_hash_signature_and_other_failures_are_not_fallback(self):
        for log in ('403 Forbidden','Hash Sum mismatch 503 Service Unavailable','signature 503 Service Unavailable','failed'):
            error=subprocess.CalledProcessError(100,[],output=log)
            with mock.patch.object(sources.subprocess,'run',side_effect=error),mock.patch.object(sources.subprocess,'check_output') as uris:
                with self.assertRaises(subprocess.CalledProcessError):sources.download(['dash=1'],Path('/sources'),self.snapshot)
                uris.assert_not_called()

    def test_503_official_archive_is_hashed_then_original_apt_revalidates(self):
        error=subprocess.CalledProcessError(100,[],output='503  Service Unavailable\n')
        with tempfile.TemporaryDirectory() as work:
            root=Path(work)
            with mock.patch.object(sources.subprocess,'run',side_effect=[error,None]) as apt, \
                 mock.patch.object(sources.subprocess,'check_output',return_value=self.uri()), \
                 mock.patch.object(sources.urllib.request,'urlopen',return_value=io.BytesIO(self.data)) as fetch:
                used=sources.download(['dash=1'],root,self.snapshot,only_source=True)
            self.assertEqual((root/'dash_1.dsc').read_bytes(),self.data)
            self.assertEqual(used[0]['sha256'],hashlib.sha256(self.data).hexdigest())
            self.assertEqual(apt.call_count,2);self.assertEqual(apt.call_args_list[0].args,apt.call_args_list[1].args)
            self.assertTrue(fetch.call_args.args[0].startswith('https://archive.ubuntu.com/'))
            self.assertEqual(len(list(root.iterdir())),1)

    def test_signed_sha512_is_verified_and_sha256_is_also_recorded(self):
        text=self.uri().replace('SHA256:'+hashlib.sha256(self.data).hexdigest(),
                                'SHA512:'+hashlib.sha512(self.data).hexdigest())
        record,=sources.archive_inputs(text,self.snapshot)
        with tempfile.TemporaryDirectory() as work,mock.patch.object(sources.urllib.request,'urlopen',return_value=io.BytesIO(self.data)):
            self.assertTrue(sources.download_exact(record,Path(work)))
            self.assertEqual(record['sha512'],hashlib.sha512(self.data).hexdigest())
            self.assertEqual(record['sha256'],hashlib.sha256(self.data).hexdigest())

    def test_superseded_source_404_uses_exact_publication_and_retains_signed_hash(self):
        record,=sources.archive_inputs(self.uri(),self.snapshot)
        response=io.BytesIO(self.data)
        response.geturl=lambda:'https://launchpadlibrarian.net/123/dash_1.dsc'
        missing=urllib.error.HTTPError(record['url'],404,'Not Found',{},None)
        unavailable=subprocess.CalledProcessError(100,[],output='503 Service Unavailable\n')
        with tempfile.TemporaryDirectory() as work,mock.patch.object(
                sources.urllib.request,'urlopen',side_effect=[missing,response]) as fetch, \
             mock.patch.object(sources.subprocess,'run',side_effect=[unavailable,None]) as apt, \
             mock.patch.object(sources.subprocess,'check_output',return_value=self.uri()):
            used=sources.download(['dash=1:1+ubuntu1'],Path(work),self.snapshot)
            record,=used
            self.assertEqual((Path(work)/record['file']).read_bytes(),self.data)
            self.assertEqual(apt.call_args_list[0].args,apt.call_args_list[1].args)
            self.assertEqual(fetch.call_args.args[0],
                'https://launchpad.net/ubuntu/+archive/primary/+sourcefiles/dash/1%3A1%2Bubuntu1/dash_1.dsc')
            self.assertEqual(record['url'],'https://launchpadlibrarian.net/123/dash_1.dsc')
            self.assertEqual(record['sha256'],hashlib.sha256(self.data).hexdigest())
            self.assertIn('archive.ubuntu.com',record['archive_url'])

    def test_publication_fallback_rejects_unknown_source_other_http_errors_and_wrong_bytes(self):
        for status,specs,data in ((403,['dash=1'],self.data),(404,['other=1'],self.data),
                                  (404,['dash=1','dash=2'],self.data),(404,['dash=1'],b'x'*len(self.data))):
            record,=sources.archive_inputs(self.uri(),self.snapshot)
            response=io.BytesIO(data);response.geturl=lambda:'https://launchpadlibrarian.net/123/dash_1.dsc'
            error=urllib.error.HTTPError(record['url'],status,'failed',{},None)
            with self.subTest(status=status,specs=specs),tempfile.TemporaryDirectory() as work,mock.patch.object(
                    sources.urllib.request,'urlopen',side_effect=[error,response]) as fetch:
                with self.assertRaises((ValueError,urllib.error.HTTPError)):
                    sources.download_exact(record,Path(work),specs)
                self.assertEqual(list(Path(work).iterdir()),[])
                self.assertEqual(fetch.call_count,2 if status==404 and specs==['dash=1'] else 1)

    def test_real_fixed_snapshot_encoded_filename_sha512_record(self):
        # Actual native ARM64 CI 42178242675, job 127761593518, apt --print-uris.
        text="'http://snapshot.ubuntu.com/ubuntu/20261001T000000Z/pool/main/x/xz-utils/xz-utils_5.6.1%2breally5.4.5-1ubuntu0.3.dsc' xz-utils_5.6.1+really5.4.5-1ubuntu0.3.dsc 2505 SHA512:317b65fd00056cc31250763e70de5e106c7ee4aeda701bd6e080a3d96cf14ba2fd5b2739fe65e2a356bf37afcb43d388159af6e1fb480ad5ec645b5ce404b2eb"
        record,=sources.archive_inputs(text,self.snapshot)
        self.assertEqual(record['bytes'],2505)
        self.assertEqual(record['file'],'xz-utils_5.6.1+really5.4.5-1ubuntu0.3.dsc')
        self.assertEqual(record['sha512'],text.split('SHA512:')[1])

    def test_wrong_archive_hash_size_or_existing_bytes_fail_closed(self):
        record,=sources.archive_inputs(self.uri(),self.snapshot)
        for data in (self.data+b'x',b'short',b'x'*len(self.data)):
            with tempfile.TemporaryDirectory() as work,mock.patch.object(sources.urllib.request,'urlopen',return_value=io.BytesIO(data)):
                root=Path(work)
                with self.assertRaises(ValueError):sources.download_exact(record,root)
                self.assertEqual(list(root.iterdir()),[])
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);(root/record['file']).write_bytes(b'keep existing evidence')
            with mock.patch.object(sources.urllib.request,'urlopen') as fetch:
                with self.assertRaises(ValueError):sources.download_exact(record,root)
                fetch.assert_not_called()
            self.assertEqual((root/record['file']).read_bytes(),b'keep existing evidence')


if __name__=='__main__':unittest.main()
