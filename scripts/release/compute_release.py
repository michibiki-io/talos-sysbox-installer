#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


INITIAL_VERSION = "v0.1.0"
CC_SUBJECT_RE = re.compile(r"^(?P<type>[a-zA-Z][\w-]*)(?:\([^)]+\))?(?P<breaking>!)?:\s+(?P<summary>.+)$")
BREAKING_RE = re.compile(r"(^|\n)BREAKING CHANGES?:", re.IGNORECASE)
PR_TITLE_PREFIX_RE = re.compile(
    r"^(feat|fix|docs|refactor|test|chore|ci|build|perf|style|revert)(?:\([^)]+\))?!?:\s+",
    re.IGNORECASE,
)
SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def run_git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def api_get_json(url: str, token: str) -> tuple[object, dict[str, str]]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "repository-release-workflow",
        },
    )
    with urllib.request.urlopen(request) as response:
        headers = {key.lower(): value for key, value in response.headers.items()}
        return json.load(response), headers


def next_link(header_value: str) -> str:
    for part in header_value.split(","):
        section = part.strip()
        if 'rel="next"' in section:
            start = section.find("<")
            end = section.find(">", start + 1)
            if start != -1 and end != -1:
                return section[start + 1 : end]
    return ""


def fetch_pr_commits(repo: str, pr_number: str, token: str) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    if not repo or not pr_number or not token:
        return commits
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/commits?per_page=100"
    while url:
        payload, headers = api_get_json(url, token)
        if not isinstance(payload, list):
            raise RuntimeError("unexpected GitHub API response for pull request commits")
        for item in payload:
            if not isinstance(item, dict):
                continue
            commit_info = item.get("commit", {})
            if not isinstance(commit_info, dict):
                continue
            message = str(commit_info.get("message", "")).strip()
            sha = str(item.get("sha", "")).strip()
            subject = message.splitlines()[0].strip() if message else ""
            commits.append({"sha": sha, "subject": subject, "message": message})
        url = next_link(headers.get("link", ""))
    return commits


def git_object_exists(reference: str) -> bool:
    if not reference:
        return False
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{reference}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        return 0
    return parsed if parsed > 0 else 0


def commit_entries(shas: list[str]) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    for sha in shas:
        message = run_git("show", "-s", "--format=%B", sha)
        subject = message.splitlines()[0].strip() if message else ""
        commits.append({"sha": sha, "subject": subject, "message": message})
    return commits


def pr_head_commits_from_git(pr_base_sha: str, pr_head_sha: str) -> list[dict[str, str]]:
    if not git_object_exists(pr_base_sha) or not git_object_exists(pr_head_sha):
        return []
    merge_base = run_git("merge-base", pr_base_sha, pr_head_sha, check=False)
    if not merge_base:
        return []
    shas = [line for line in run_git("rev-list", "--reverse", f"{merge_base}..{pr_head_sha}").splitlines() if line]
    return commit_entries(shas)


def fallback_commits_from_git(
    merge_sha: str,
    pr_base_sha: str,
    pr_head_sha: str,
    pr_commit_count: int,
) -> list[dict[str, str]]:
    parent_line = run_git("rev-list", "--parents", "-n", "1", merge_sha)
    parts = parent_line.split()
    shas: list[str] = []
    if len(parts) >= 3:
        shas = [line for line in run_git("rev-list", "--reverse", f"{parts[1]}..{parts[2]}").splitlines() if line]
        return commit_entries(shas)

    pr_head_commits = pr_head_commits_from_git(pr_base_sha, pr_head_sha)
    if pr_head_commits:
        return pr_head_commits

    if len(parts) == 2:
        if pr_commit_count > 1:
            eprint(
                "warning: PR head/base commits were unavailable for a multi-commit single-parent merge; "
                "skipping git fallback to avoid mixing unrelated branch commits."
            )
            return []
        shas = [line for line in run_git("rev-list", "--reverse", f"{parts[1]}..{merge_sha}").splitlines() if line]
    else:
        shas = [merge_sha]
    return commit_entries(shas)


def parse_semver(tag: str) -> tuple[int, int, int]:
    match = SEMVER_RE.match(tag)
    if not match:
        raise RuntimeError(f"unsupported semver tag: {tag}")
    return tuple(int(group) for group in match.groups())


def reachable_semver_tags(reference: str) -> list[str]:
    output = run_git("tag", "--merged", reference, "--list", "v[0-9]*", "--sort=-version:refname")
    return [line for line in output.splitlines() if SEMVER_RE.match(line.strip())]


def previous_tag_for_merge(merge_sha: str) -> str:
    parent_line = run_git("rev-list", "--parents", "-n", "1", merge_sha)
    parts = parent_line.split()
    reference = parts[1] if len(parts) >= 2 else merge_sha
    tags = reachable_semver_tags(reference)
    return tags[0] if tags else ""


def classify_release(commits: list[dict[str, str]]) -> str:
    saw_minor = False
    saw_patch = False
    for commit in commits:
        message = commit.get("message", "")
        subject = commit.get("subject", "")
        if BREAKING_RE.search(message):
            return "major"
        parsed = CC_SUBJECT_RE.match(subject)
        if not parsed:
            continue
        commit_type = parsed.group("type").lower()
        if parsed.group("breaking"):
            return "major"
        if commit_type == "feat":
            saw_minor = True
        elif commit_type in {"fix", "hotfix"}:
            saw_patch = True
    if saw_minor:
        return "minor"
    if saw_patch:
        return "patch"
    return "none"


def compute_next_tag(previous_tag: str, release_kind: str) -> str:
    if release_kind == "none":
        return ""
    if not previous_tag:
        return INITIAL_VERSION
    major, minor, patch = parse_semver(previous_tag)
    if release_kind == "major":
        return f"v{major + 1}.0.0"
    if release_kind == "minor":
        return f"v{major}.{minor + 1}.0"
    return f"v{major}.{minor}.{patch + 1}"


def tag_commit(tag: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def write_outputs(values: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{key}={value}" for key, value in values.items()]
    payload = "\n".join(lines) + "\n"
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        sys.stdout.write(payload)


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    merge_sha = os.environ.get("MERGE_COMMIT_SHA", "").strip()
    pr_base_sha = os.environ.get("PR_BASE_SHA", "").strip()
    pr_head_sha = os.environ.get("PR_HEAD_SHA", "").strip()
    pr_commit_count = positive_int(os.environ.get("PR_COMMIT_COUNT", "").strip())
    pr_number = os.environ.get("PR_NUMBER", "").strip()
    _pr_body = os.environ.get("PR_BODY", "")
    pr_title = os.environ.get("PR_TITLE", "").strip()

    if not merge_sha:
        raise RuntimeError("MERGE_COMMIT_SHA is required")

    if pr_title and PR_TITLE_PREFIX_RE.match(pr_title):
        eprint("PR title looks like a Conventional Commit subject, but it will be ignored for release classification.")

    commits: list[dict[str, str]] = []
    if repo and pr_number and token:
        try:
            commits = fetch_pr_commits(repo, pr_number, token)
        except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            eprint(f"warning: could not read PR commits from GitHub API, falling back to git history: {exc}")
    if not commits:
        commits = fallback_commits_from_git(merge_sha, pr_base_sha, pr_head_sha, pr_commit_count)
    if not commits:
        eprint("warning: no commit messages were available for release classification; no release will be created.")

    release_kind = classify_release(commits) if commits else "none"
    previous_tag = previous_tag_for_merge(merge_sha)
    git_tag = compute_next_tag(previous_tag, release_kind)
    docker_tag = git_tag[1:] if git_tag.startswith("v") else ""
    tag_exists = "false"

    if git_tag:
        existing_target = tag_commit(git_tag)
        if existing_target:
            target_commit = run_git("rev-parse", merge_sha)
            if existing_target != target_commit:
                raise RuntimeError(
                    f"computed tag {git_tag} already exists and points to {existing_target}, not {target_commit}"
                )
            tag_exists = "true"

    write_outputs(
        {
            "release_required": "true" if release_kind != "none" else "false",
            "release_kind": release_kind,
            "previous_tag": previous_tag,
            "git_tag": git_tag,
            "docker_tag": docker_tag,
            "tag_exists": tag_exists,
        }
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - workflow entry point
        eprint(f"error: {exc}")
        raise SystemExit(1)
