import copy
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('resolve_merged_pr', ROOT/'scripts/release/resolve_merged_pr.py')
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)
spec = importlib.util.spec_from_file_location('compute_release', ROOT/'scripts/release/compute_release.py')
compute = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compute)


class ReleaseTriggerTests(unittest.TestCase):
    def pr(self):
        return {'number': 2, 'merged': True, 'merged_at': '2026-10-14T00:00:00Z',
                'merge_commit_sha': 'a'*40, 'commits': 3,
                'base': {'ref': 'trunk', 'sha': 'b'*40, 'repo': {'full_name': 'example/project'}},
                'head': {'ref': 'feature/test', 'sha': 'c'*40}}

    def test_merged_fork_pr_uses_confirmed_metadata(self):
        pr = self.pr()
        pr['head']['repo'] = {'full_name': 'contributor/fork'}
        with mock.patch.object(resolver, 'api', side_effect=[[pr], pr]):
            result = resolver.resolve('example/project', 'trunk', 'a'*40)
        self.assertEqual(result, {'eligible': 'true', 'number': '2', 'base_ref': 'trunk',
                                 'base_sha': 'b'*40, 'head_sha': 'c'*40, 'commit_count': '3'})

    def test_no_pr_and_ineligible_prs_do_not_release(self):
        for field, value in [('merged_at', None), ('merge_commit_sha', 'd'*40),
                             ('base', {'ref': 'other', 'repo': {'full_name': 'example/project'}}),
                             ('base', {'ref': 'trunk', 'repo': {'full_name': 'different/project'}}),
                             ('head', {'ref': 'fix/test'})]:
            pr = self.pr(); pr[field] = value
            with self.subTest(field=field, value=value), mock.patch.object(resolver, 'api', return_value=[pr]):
                self.assertEqual(resolver.resolve('example/project', 'trunk', 'a'*40), {'eligible': 'false'})
        with mock.patch.object(resolver, 'api', return_value=[]):
            self.assertEqual(resolver.resolve('example/project', 'trunk', 'a'*40), {'eligible': 'false'})

    def test_ambiguity_api_errors_and_changed_pr_fail_closed(self):
        pr = self.pr(); changed = copy.deepcopy(pr); changed['merged'] = False
        for responses in [ [[pr, pr]], [[pr], changed] ]:
            with mock.patch.object(resolver, 'api', side_effect=responses), self.assertRaises(RuntimeError):
                resolver.resolve('example/project', 'trunk', 'a'*40)
        with mock.patch.object(resolver, 'api', side_effect=RuntimeError('API failure')), self.assertRaises(RuntimeError):
            resolver.resolve('example/project', 'trunk', 'a'*40)

    def test_associated_pr_pagination(self):
        pr = self.pr(); other = copy.deepcopy(pr); other['merged_at'] = None
        with mock.patch.object(resolver, 'api', side_effect=[[other]*100, [pr], pr]) as api:
            self.assertEqual(resolver.resolve('example/project', 'trunk', 'a'*40)['number'], '2')
            self.assertIn('page=2', api.call_args_list[1].args[0])

    def test_other_events_and_nondefault_pushes_never_query_api(self):
        env = {'GITHUB_REPOSITORY': 'example/project', 'RELEASE_BRANCH': 'trunk',
               'GITHUB_SHA': 'a'*40, 'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/trunk'}
        for key, value in [('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REF', 'refs/heads/feature/test'),
                           ('GITHUB_REF', 'refs/tags/v1.0.0')]:
            with mock.patch.dict(os.environ, dict(env, **{key: value})), mock.patch.object(resolver, 'api') as api:
                with self.assertRaises(RuntimeError): resolver.main()
                api.assert_not_called()

    def test_default_push_writes_only_verified_metadata(self):
        env = {'GITHUB_REPOSITORY': 'example/project', 'RELEASE_BRANCH': 'trunk',
               'GITHUB_SHA': 'a'*40, 'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/trunk'}
        for eligible in (False, True):
            with self.subTest(eligible=eligible), tempfile.TemporaryDirectory() as d:
                output = Path(d)/'output'
                with mock.patch.dict(os.environ, dict(env, GITHUB_OUTPUT=str(output))), mock.patch.object(resolver, 'api', side_effect=[[self.pr()], self.pr()] if eligible else [[]]):
                    resolver.main()
                values = dict(line.split('=', 1) for line in output.read_text().splitlines())
                self.assertEqual(values['eligible'], str(eligible).lower())
                if eligible:
                    self.assertEqual(values['number'], '2')
                    self.assertEqual(values['head_sha'], 'c'*40)
                else:
                    self.assertEqual(values, {'eligible': 'false'})

    def test_revalidated_metadata_cannot_inject_output_or_change_target(self):
        for field, value in [('base', {'ref': 'other', 'repo': {'full_name': 'example/project'}, 'sha': 'b'*40}),
                             ('merge_commit_sha', 'd'*40), ('commits', '3\neligible=true'),
                             ('head', {'ref': 'feature/test', 'sha': 'bad\nnumber=1'})]:
            changed = copy.deepcopy(self.pr()); changed[field] = value
            with self.subTest(field=field), mock.patch.object(resolver, 'api', side_effect=[[self.pr()], changed]), self.assertRaises(RuntimeError):
                resolver.resolve('example/project', 'trunk', 'a'*40)

    def test_workflow_separates_feature_pushes_from_release_concurrency(self):
        workflow = (ROOT/'.github/workflows/release.yml').read_text()
        group = re.search(r'^concurrency:\n  group: (.+)$', workflow, re.MULTILINE).group(1)
        default = group.replace('${{ github.repository }}', 'example/project').replace('${{ github.ref }}', 'refs/heads/trunk')
        feature = group.replace('${{ github.repository }}', 'example/project').replace('${{ github.ref }}', 'refs/heads/feature/test')
        self.assertNotEqual(default, feature)
        self.assertNotIn('pull_request_target:', workflow)
        self.assertIn("github.ref == format('refs/heads/{0}', github.event.repository.default_branch)", workflow)


class ReleaseVersionTests(unittest.TestCase):
    def test_initial_version_does_not_reuse_abandoned_tag(self):
        for kind in ('patch', 'minor', 'major'):
            self.assertEqual(compute.compute_next_tag('', kind), 'v0.2.0')
        self.assertEqual(compute.compute_next_tag('v0.2.0', 'patch'), 'v0.2.1')

    def test_commit_messages_and_existing_tag_targets_remain_authoritative(self):
        env = {'GITHUB_REPOSITORY': '', 'GITHUB_TOKEN': '', 'MERGE_COMMIT_SHA': 'a'*40,
               'PR_NUMBER': '', 'PR_TITLE': 'feat: misleading title'}
        commits = [{'sha': 'c'*40, 'subject': 'fix: retry downloads', 'message': 'fix: retry downloads'}]
        for target in ('a'*40, 'd'*40):
            with self.subTest(target=target), mock.patch.dict(os.environ, env), mock.patch.object(compute, 'fallback_commits_from_git', return_value=commits), mock.patch.object(compute, 'previous_tag_for_merge', return_value='v0.2.0'), mock.patch.object(compute, 'tag_commit', return_value=target), mock.patch.object(compute, 'run_git', return_value='a'*40), mock.patch.object(compute, 'write_outputs') as write:
                if target == 'a'*40:
                    self.assertEqual(compute.main(), 0)
                    self.assertEqual(write.call_args.args[0]['release_kind'], 'patch')
                    self.assertEqual(write.call_args.args[0]['git_tag'], 'v0.2.1')
                    self.assertEqual(write.call_args.args[0]['tag_exists'], 'true')
                else:
                    with self.assertRaises(RuntimeError): compute.main()
                    write.assert_not_called()


class AptRetryTests(unittest.TestCase):
    def operation(self, failures, arguments=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'apt-get').write_text('#!/bin/sh\nn=0; test ! -f "$COUNT" || n=$(cat "$COUNT")\nn=$((n+1)); echo "$n" > "$COUNT"\nprintf "%s\\n" "$*" >> "$ARGS"\ntest "$n" -gt "$FAILURES" || exit 100\n')
            (root/'sleep').write_text('#!/bin/sh\nexit 0\n')
            for executable in ('apt-get', 'sleep'): (root/executable).chmod(0o700)
            env = dict(os.environ, PATH=str(root)+':'+os.environ['PATH'], COUNT=str(root/'count'), ARGS=str(root/'args'), FAILURES=str(failures))
            result = subprocess.run(['sh', str(ROOT/'build/apt_retry.sh'), *(arguments or ['-o', 'APT::Update::Error-Mode=any', 'update'])], env=env, capture_output=True)
            return result.returncode, int((root/'count').read_text()), (root/'args').read_text().splitlines()

    def test_transient_failure_retries_unchanged_operation(self):
        status, attempts, args = self.operation(1)
        self.assertEqual((status, attempts), (0, 2))
        self.assertEqual(args[0], args[1])
        self.assertIn('APT::Update::Error-Mode=any update', args[0])

    def test_persistent_failure_is_not_success_or_unbounded_retry(self):
        status, attempts, args = self.operation(9)
        self.assertEqual((status, attempts), (100, 3))
        self.assertEqual(len(set(args)), 1)

    def test_binary_and_exact_version_source_downloads_retry_same_arguments(self):
        for arguments in (['install', '-y', '--no-install-recommends', 'ca-certificates'],
                          ['source', '--download-only', 'example=1.2.3']):
            with self.subTest(arguments=arguments):
                status, attempts, args = self.operation(2, arguments)
                self.assertEqual((status, attempts), (0, 3))
                self.assertEqual(len(set(args)), 1)
                self.assertTrue(args[0].endswith(' '.join(arguments)))


if __name__ == '__main__':
    unittest.main()
