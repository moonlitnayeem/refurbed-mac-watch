#!/usr/bin/env python3

import json
import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import archive_issue_history as ah


def report(comment_id, created_at, body=None, user="github-actions[bot]"):
    return {
        "id": comment_id,
        "created_at": created_at,
        "body": body or f"### Current standings\nreport {comment_id}",
        "user": {"login": user},
    }


class ArchiveIssueHistoryTest(unittest.TestCase):
    def test_prepares_old_reports_and_keeps_latest_four_in_issue(self):
        folded = (
            f"{ah.FOLDED_MARKER}\n<details>\n"
            "<summary>Archived update — 2026-08-19 01:00 UTC</summary>\n\n"
            "### Current standings\nreport 1\n\n</details>"
        )
        comments = [
            report(1, "2026-08-19T01:00:20Z", folded),
            *[report(i, f"2026-08-19T0{i}:00:00Z") for i in range(2, 7)],
            report(99, "2026-08-19T00:00:00Z", "human note", "moonlitnayeem"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            manifest = ah.prepare_archives(
                comments, Path(tmp) / "history", keep=4,
                repo="moonlitnayeem/refurbed-mac-watch", issue=2,
            )
            archive = Path(tmp) / "history" / "2026-08" / "19.md"
            body = archive.read_text(encoding="utf-8")

            self.assertEqual([entry["comment_id"] for entry in manifest], [1, 2])
            self.assertIn("<!-- github-comment-id:1 -->", body)
            self.assertIn("<!-- github-comment-id:2 -->", body)
            self.assertIn("### Current standings\nreport 1", body)
            self.assertNotIn(ah.FOLDED_MARKER, body)
            self.assertIn("Back to the live results issue", body)

            # A retry before deletion must not duplicate archived reports.
            second = ah.prepare_archives(
                comments, Path(tmp) / "history", keep=4,
                repo="moonlitnayeem/refurbed-mac-watch", issue=2,
            )
            self.assertEqual(second, manifest)
            self.assertEqual(archive.read_text().count("<!-- github-comment-id:1 -->"), 1)

    def test_verification_requires_every_marker_on_remote(self):
        manifest = [
            {"comment_id": 1, "archive_path": "history/2026-08/19.md",
             "marker": "<!-- github-comment-id:1 -->"},
            {"comment_id": 2, "archive_path": "history/2026-08/19.md",
             "marker": "<!-- github-comment-id:2 -->"},
        ]
        remote = {"history/2026-08/19.md": "<!-- github-comment-id:1 -->"}

        with self.assertRaisesRegex(RuntimeError, "comment 2"):
            ah.verify_manifest(manifest, remote)

        remote["history/2026-08/19.md"] += "\n<!-- github-comment-id:2 -->"
        self.assertEqual(ah.verify_manifest(manifest, remote), [1, 2])

    def test_large_remote_archive_is_loaded_through_git_blob_api(self):
        marker = "<!-- github-comment-id:1 -->"
        manifest = [{"comment_id": 1, "archive_path": "history/2026-08/24.md",
                     "marker": marker}]
        contents_result = {
            "encoding": "none",
            "content": "",
            "git_url": "https://api.github.com/repos/owner/repo/git/blobs/abc123",
        }
        blob_result = {
            "encoding": "base64",
            "content": base64.b64encode(marker.encode()).decode(),
        }

        with mock.patch.object(ah, "api_request",
                               side_effect=[contents_result, blob_result]) as request:
            files = ah.get_remote_archive_files(
                "owner/repo", "main", manifest, "secret-token"
            )

        self.assertEqual(files["history/2026-08/24.md"], marker)
        self.assertIn("/repos/owner/repo/git/blobs/abc123", request.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
