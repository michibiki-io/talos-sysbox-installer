#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request


CC_SUBJECT_RE = re.compile(r"^(?P<type>[a-zA-Z][\w-]*)(?:\([^)]+\))?(?P<breaking>!)?:\s+(?P<summary>.+)$")
BREAKING_RE = re.compile(r"(^|\n)BREAKING CHANGES?:", re.IGNORECASE)
ISSUE_RE = re.compile(r"(?<!\w)#\d+\b")


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout


def api_get_json(url: str, token: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "repository-release-workflow",
        },
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def api_get_json_with_headers(url: str, token: str) -> tuple[object, dict[str, str]]:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate release notes from commit history.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--previous-tag", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--pr-number")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def git_log_entries(previous_tag: str, target: str) -> list[dict[str, str]]:
    revision_range = f"{previous_tag}..{target}" if previous_tag else target
    raw = run_git("log", "--reverse", "--format=%H%x1f%s%x1f%B%x1e", revision_range)
    entries: list[dict[str, str]] = []
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        parts = record.strip("\n").split("\x1f")
        if len(parts) < 3:
            continue
        sha, subject, body = parts[0].strip(), parts[1].strip(), parts[2].strip()
        entries.append({"sha": sha, "subject": subject, "body": body})
    return entries


def next_link(header_value: str) -> str:
    for part in header_value.split(","):
        section = part.strip()
        if 'rel="next"' in section:
            start = section.find("<")
            end = section.find(">", start + 1)
            if start != -1 and end != -1:
                return section[start + 1 : end]
    return ""


def pr_commit_entries(repo: str, pr_number: str, token: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    if not repo or not pr_number or not token:
        return entries
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/commits?per_page=100"
    while url:
        payload, headers = api_get_json_with_headers(url, token)
        if not isinstance(payload, list):
            raise RuntimeError("unexpected GitHub API response for pull request commits")
        for item in payload:
            if not isinstance(item, dict):
                continue
            commit_info = item.get("commit", {})
            if not isinstance(commit_info, dict):
                continue
            message = str(commit_info.get("message", "")).strip()
            subject = message.splitlines()[0].strip() if message else ""
            body = "\n".join(message.splitlines()[1:]).strip() if message else ""
            sha = str(item.get("sha", "")).strip()
            entries.append({"sha": sha, "subject": subject, "body": body})
        url = next_link(headers.get("link", ""))
    return entries


def associated_pr(repo: str, sha: str, token: str, cache: dict[str, dict[str, str] | None]) -> dict[str, str] | None:
    if sha in cache:
        return cache[sha]
    if not token:
        cache[sha] = None
        return None
    url = f"https://api.github.com/repos/{repo}/commits/{sha}/pulls"
    try:
        payload = api_get_json(url, token)
    except (urllib.error.URLError, urllib.error.HTTPError):
        cache[sha] = None
        return None
    if not isinstance(payload, list) or not payload:
        cache[sha] = None
        return None
    item = payload[0]
    if not isinstance(item, dict):
        cache[sha] = None
        return None
    number = item.get("number")
    html_url = item.get("html_url")
    if not isinstance(number, int) or not isinstance(html_url, str):
        cache[sha] = None
        return None
    cache[sha] = {"number": str(number), "url": html_url}
    return cache[sha]


def normalize_subject(subject: str) -> tuple[str, str | None, bool]:
    parsed = CC_SUBJECT_RE.match(subject)
    if not parsed:
        return subject, None, False
    commit_type = parsed.group("type").lower()
    summary = parsed.group("summary").strip()
    breaking = bool(parsed.group("breaking"))
    return summary, commit_type, breaking


def release_group(subject: str, body: str) -> str | None:
    summary, commit_type, breaking = normalize_subject(subject)
    if breaking or BREAKING_RE.search(body):
        return "Breaking Changes"
    if commit_type == "feat":
        return "Features"
    if commit_type == "hotfix":
        return "Hot Fixes"
    if commit_type == "fix":
        return "Bug Fixes"
    return None


def build_entry(repo: str, entry: dict[str, str], token: str, cache: dict[str, dict[str, str] | None]) -> tuple[str, str] | None:
    subject = entry["subject"]
    body = entry["body"]
    group = release_group(subject, body)
    if not group:
        return None
    summary, _, _ = normalize_subject(subject)
    issue_refs = sorted(set(ISSUE_RE.findall(subject + "\n" + body)))
    pr = associated_pr(repo, entry["sha"], token, cache)
    metadata: list[str] = []
    if pr:
        metadata.append(f"PR [#{pr['number']}]({pr['url']})")
    missing_issue_refs = [ref for ref in issue_refs if ref not in summary]
    if missing_issue_refs:
        metadata.append("Issues " + ", ".join(missing_issue_refs))
    suffix = f" ({'; '.join(metadata)})" if metadata else ""
    return group, f"- {summary}{suffix}"


def render_markdown(previous_tag: str, target: str, grouped_entries: dict[str, list[str]]) -> str:
    lines = ["## Release Notes", ""]
    if previous_tag:
        lines.append(f"Range: `{previous_tag}..{target}`")
    else:
        lines.append(f"Target: `{target}`")
    lines.append("")
    order = ["Breaking Changes", "Features", "Hot Fixes", "Bug Fixes"]
    wrote_section = False
    for section in order:
        entries = grouped_entries.get(section, [])
        if not entries:
            continue
        wrote_section = True
        lines.append(f"### {section}")
        lines.extend(entries)
        lines.append("")
    if not wrote_section:
        lines.append("No releasable commit subjects were found in the selected range.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    entries: list[dict[str, str]] = []
    if args.pr_number and token:
        try:
            entries = pr_commit_entries(args.repo, args.pr_number, token)
        except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError):
            entries = []
    if not entries:
        entries = git_log_entries(args.previous_tag, args.target)
    pr_cache: dict[str, dict[str, str] | None] = {}
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        built = build_entry(args.repo, entry, token, pr_cache)
        if not built:
            continue
        group, line = built
        grouped.setdefault(group, []).append(line)
    output_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(args.previous_tag, args.target, grouped))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - workflow entry point
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
