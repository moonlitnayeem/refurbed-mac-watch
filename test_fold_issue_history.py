#!/usr/bin/env python3

import unittest

import fold_issue_history as fh


def comment(comment_id, created_at, body="### Current standings\nreport", user="github-actions[bot]"):
    return {
        "id": comment_id,
        "created_at": created_at,
        "body": body,
        "user": {"login": user},
    }


class FoldIssueHistoryTest(unittest.TestCase):
    def test_keeps_latest_four_reports_expanded_and_folds_older_reports(self):
        comments = [
            comment(i, f"2026-08-19T0{i}:00:00Z")
            for i in range(1, 7)
        ]

        updates = fh.updates_to_fold(comments, keep=4)

        self.assertEqual([comment_id for comment_id, _ in updates], [1, 2])
        self.assertIn("<details>", updates[0][1])
        self.assertIn("Archived update — 2026-08-19 01:00 UTC", updates[0][1])
        self.assertIn("### Current standings", updates[0][1])

    def test_does_not_fold_unrelated_or_already_folded_comments(self):
        comments = [
            comment(1, "2026-08-19T01:00:00Z", user="moonlitnayeem"),
            comment(2, "2026-08-19T02:00:00Z", body="unrelated bot message"),
            comment(3, "2026-08-19T03:00:00Z",
                    body=f"{fh.FOLDED_MARKER}\n<details>old</details>"),
        ]

        self.assertEqual(fh.updates_to_fold(comments, keep=0), [])

    def test_report_marker_identifies_new_reports(self):
        marked = comment(1, "2026-08-19T01:00:00Z", body=fh.REPORT_MARKER + "\nreport")
        self.assertEqual([item[0] for item in fh.updates_to_fold([marked], keep=0)], [1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
