#!/usr/bin/env python3
"""Bind a default-branch push to exactly one eligible merged PR, fail closed."""
import json
import os
import re
import urllib.request


def api(path):
    request = urllib.request.Request(
        'https://api.github.com/repos/' + os.environ['GITHUB_REPOSITORY'] + path,
        headers={'Accept': 'application/vnd.github+json',
                 'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                 'User-Agent': 'talos-sysbox-release'},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def eligible(pr, repo, branch, sha):
    return (bool(pr.get('merged_at'))
            and pr.get('merge_commit_sha') == sha
            and pr.get('base', {}).get('repo', {}).get('full_name') == repo
            and pr.get('base', {}).get('ref') == branch
            and pr.get('head', {}).get('ref', '').startswith('feature/'))


def resolve(repo, branch, sha):
    matches = []
    page = 1
    while True:
        prs = api(f'/commits/{sha}/pulls?per_page=100&page={page}')
        if not isinstance(prs, list):
            raise RuntimeError('Unexpected associated PR response')
        matches.extend(pr for pr in prs if eligible(pr, repo, branch, sha))
        if len(prs) < 100:
            break
        page += 1
    if not matches:
        return {'eligible': 'false'}
    if len(matches) != 1:
        raise RuntimeError('Ambiguous merged PR; refusing publication')
    number = matches[0]['number']
    if not isinstance(number, int) or number <= 0:
        raise RuntimeError('Invalid PR number')
    pr = api(f'/pulls/{number}')
    if not pr.get('merged') or not eligible(pr, repo, branch, sha):
        raise RuntimeError('PR changed or is not the pushed merge commit')
    result = {'eligible': 'true', 'number': str(number), 'base_ref': branch,
              'base_sha': pr['base']['sha'], 'head_sha': pr['head']['sha'],
              'commit_count': str(pr['commits'])}
    if not all(re.fullmatch(r'[0-9a-f]{40}', result[key]) for key in ('base_sha', 'head_sha')):
        raise RuntimeError('Invalid PR commit metadata')
    if not result['commit_count'].isdigit() or int(result['commit_count']) <= 0:
        raise RuntimeError('Invalid PR commit count')
    return result


def main():
    repo = os.environ['GITHUB_REPOSITORY']
    branch = os.environ['RELEASE_BRANCH']
    sha = os.environ['GITHUB_SHA']
    if (os.environ.get('GITHUB_EVENT_NAME') != 'push'
            or os.environ.get('GITHUB_REF') != 'refs/heads/' + branch
            or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo)
            or not re.fullmatch(r'[0-9a-f]{40}', sha)
            or not re.fullmatch(r'[^\s]+', branch)):
        raise RuntimeError('Expected a default-branch push')
    values = resolve(repo, branch, sha)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        for key, value in values.items():
            output.write(f'{key}={value}\n')
    print('Eligible merged feature PR: ' + values['eligible'])


if __name__ == '__main__':
    main()
