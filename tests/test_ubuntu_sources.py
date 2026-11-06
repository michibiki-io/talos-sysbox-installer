import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
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
        with mock.patch.object(sources.subprocess,'run') as apt,mock.patch.object(sources.subprocess,'check_output') as uris:
            self.assertEqual(sources.download(['dash=1'],Path('/sources'),self.snapshot),[])
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
