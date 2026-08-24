#!/usr/bin/env python3
"""Archive old GitHub issue reports to files, then safely delete their cards."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

API_BASE = "https://api.github.com"
REPORT_MARKER = "<!-- refurbed-watch-report -->"
FOLDED_MARKER = "<!-- refurbed-watch-report-folded -->"
BOT_LOGIN = "github-actions[bot]"
DEFAULT_MANIFEST = ".issue-archive-manifest.json"


def is_report(comment: dict) -> bool:
    body = comment.get("body") or ""
    login = (comment.get("user") or {}).get("login")
    return login == BOT_LOGIN and (
        REPORT_MARKER in body
        or FOLDED_MARKER in body
        or "### Current standings" in body
    )


def format_timestamp(created_at: str) -> str:
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except ValueError:
        return created_at


def original_report_body(body: str) -> str:
    """Remove the previous per-comment fold wrapper, preserving its report."""
    if FOLDED_MARKER in body:
        match = re.search(r"</summary>\s*(.*?)\s*</details>\s*$", body, re.DOTALL)
        if match:
            body = match.group(1)
    body = body.replace(FOLDED_MARKER, "").replace(REPORT_MARKER, "")
    return body.strip()


def archive_path_for(comment: dict) -> Path:
    created = datetime.fromisoformat(comment["created_at"].replace("Z", "+00:00"))
    return Path(created.strftime("%Y-%m")) / f"{created:%d}.md"


def archive_header(repo: str, issue: int, created_at: str) -> str:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    live_url = f"https://github.com/{repo}/issues/{issue}"
    return (
        f"# Refurbed Watch archive — {created:%Y-%m-%d}\n\n"
        f"[← Back to the live results issue]({live_url})\n"
    )


def prepare_archives(comments: list[dict], history_dir: Path, keep: int,
                     repo: str, issue: int) -> list[dict]:
    reports = sorted(
        (comment for comment in comments if is_report(comment)),
        key=lambda comment: (comment.get("created_at", ""), comment.get("id", 0)),
    )
    old_reports = reports[:-keep] if keep else reports
    manifest: list[dict] = []

    for comment in old_reports:
        relative_path = Path("history") / archive_path_for(comment)
        disk_path = history_dir / archive_path_for(comment)
        marker = f"<!-- github-comment-id:{comment['id']} -->"
        disk_path.parent.mkdir(parents=True, exist_ok=True)

        existing = disk_path.read_text(encoding="utf-8") if disk_path.exists() else ""
        if not existing:
            existing = archive_header(repo, issue, comment["created_at"])

        if marker not in existing:
            report = original_report_body(comment.get("body") or "")
            stamp = format_timestamp(comment.get("created_at") or "unknown time")
            entry = (
                f"\n{marker}\n"
                f"<details>\n"
                f"<summary>Update — {stamp}</summary>\n\n"
                f"{report}\n\n"
                f"</details>\n"
            )
            disk_path.write_text(existing.rstrip() + "\n" + entry, encoding="utf-8")

        manifest.append({
            "comment_id": comment["id"],
            "archive_path": relative_path.as_posix(),
            "marker": marker,
        })

    return manifest


def verify_manifest(manifest: list[dict], remote_files: dict[str, str]) -> list[int]:
    verified: list[int] = []
    for entry in manifest:
        content = remote_files.get(entry["archive_path"], "")
        if entry["marker"] not in content:
            raise RuntimeError(
                f"Archive verification failed for comment {entry['comment_id']} "
                f"in {entry['archive_path']}"
            )
        verified.append(entry["comment_id"])
    return verified


def api_request(path: str, token: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        API_BASE + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "refurbed-watch-history-archiver",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def get_all_comments(repo: str, issue: int, token: str) -> list[dict]:
    comments: list[dict] = []
    page = 1
    while True:
        path = f"/repos/{repo}/issues/{issue}/comments?per_page=100&page={page}"
        batch = api_request(path, token)
        comments.extend(batch)
        if len(batch) < 100:
            return comments
        page += 1


def get_remote_archive_files(repo: str, ref: str, manifest: list[dict],
                             token: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted({entry["archive_path"] for entry in manifest}):
        encoded_path = urllib.parse.quote(path, safe="/")
        encoded_ref = urllib.parse.quote(ref, safe="")
        result = api_request(
            f"/repos/{repo}/contents/{encoded_path}?ref={encoded_ref}", token
        )
        if result.get("encoding") == "base64" and result.get("content"):
            encoded_content = result["content"]
        else:
            # GitHub's Contents API omits inline content for files over 1 MB.
            # The linked Git Blob API still returns the full file (up to 100 MB).
            git_url = result.get("git_url")
            if not git_url:
                raise RuntimeError(f"No downloadable content for {path}")
            blob_path = urllib.parse.urlsplit(git_url).path
            blob = api_request(blob_path, token)
            encoded_content = blob["content"]
        files[path] = base64.b64decode(encoded_content).decode("utf-8")
    return files


def prepare(repo: str, issue: int, token: str, keep: int,
            history_dir: Path, manifest_path: Path) -> int:
    comments = get_all_comments(repo, issue, token)
    manifest = prepare_archives(comments, history_dir, keep, repo, issue)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return len(manifest)


def delete_verified(repo: str, token: str, ref: str, manifest_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    remote_files = get_remote_archive_files(repo, ref, manifest, token)
    comment_ids = verify_manifest(manifest, remote_files)
    for comment_id in comment_ids:
        api_request(
            f"/repos/{repo}/issues/comments/{comment_id}", token, method="DELETE"
        )
    manifest_path.unlink(missing_ok=True)
    return len(comment_ids)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "delete"])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--issue", type=int, default=2)
    parser.add_argument("--keep", type=int, default=4)
    parser.add_argument("--history-dir", type=Path, default=Path("history"))
    parser.add_argument("--manifest", type=Path, default=Path(DEFAULT_MANIFEST))
    parser.add_argument("--ref", default="main")
    args = parser.parse_args()

    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    if args.keep < 0:
        parser.error("--keep cannot be negative")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GH_TOKEN or GITHUB_TOKEN is required")

    if args.mode == "prepare":
        count = prepare(
            args.repo, args.issue, token, args.keep, args.history_dir, args.manifest
        )
        print(f"Prepared {count} old report comment(s) for archival.")
    else:
        count = delete_verified(args.repo, token, args.ref, args.manifest)
        print(f"Verified and deleted {count} archived comment card(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
