#!/usr/bin/env python3
"""Fold old watch reports in a GitHub issue while keeping recent ones open."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request

API_BASE = "https://api.github.com"
REPORT_MARKER = "<!-- refurbed-watch-report -->"
FOLDED_MARKER = "<!-- refurbed-watch-report-folded -->"
BOT_LOGIN = "github-actions[bot]"


def is_expanded_report(comment: dict) -> bool:
    body = comment.get("body") or ""
    login = (comment.get("user") or {}).get("login")
    if login != BOT_LOGIN or FOLDED_MARKER in body:
        return False
    return REPORT_MARKER in body or "### Current standings" in body


def folded_body(comment: dict) -> str:
    body = (comment.get("body") or "").strip()
    created = comment.get("created_at") or "unknown time"
    stamp = created.replace("T", " ").replace(":00Z", " UTC")
    return (
        f"{FOLDED_MARKER}\n"
        f"<details>\n"
        f"<summary>Archived update — {stamp}</summary>\n\n"
        f"{body}\n\n"
        f"</details>"
    )


def updates_to_fold(comments: list[dict], keep: int = 4) -> list[tuple[int, str]]:
    reports = sorted(
        (comment for comment in comments if is_expanded_report(comment)),
        key=lambda comment: (comment.get("created_at", ""), comment.get("id", 0)),
    )
    old_reports = reports[:-keep] if keep else reports
    return [(comment["id"], folded_body(comment)) for comment in old_reports]


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
            "User-Agent": "refurbed-watch-history-folder",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


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


def fold_history(repo: str, issue: int, token: str, keep: int = 4) -> int:
    comments = get_all_comments(repo, issue, token)
    updates = updates_to_fold(comments, keep=keep)
    for comment_id, body in updates:
        api_request(
            f"/repos/{repo}/issues/comments/{comment_id}",
            token,
            method="PATCH",
            payload={"body": body},
        )
    return len(updates)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--issue", type=int, default=2)
    parser.add_argument("--keep", type=int, default=4)
    args = parser.parse_args()

    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    if args.keep < 0:
        parser.error("--keep cannot be negative")

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GH_TOKEN or GITHUB_TOKEN is required")

    count = fold_history(args.repo, args.issue, token, keep=args.keep)
    print(f"Folded {count} old report comment(s); kept the latest {args.keep} expanded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
