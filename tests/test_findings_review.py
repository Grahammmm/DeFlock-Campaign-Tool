"""DF2237-01: `finding review` / `finding review-status` offline receipts. Synthetic fixtures only.

Run from the repository root: python3 -B -m unittest tests.test_findings_review -v
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
REVIEW_BLOCKERS = {"missing_factual_review", "missing_legal_review", "missing_privacy_review",
                   "need_two_independent_reviewers"}


def draft(**changes):
    base = {"id": "syn-finding-1", "title": "Synthetic title", "slug": "synthetic-title",
            "summary": "Synthetic summary.", "classification": "documented_fact", "confidence": "high",
            "event_date": "2026-01-02", "sources": [{"locator": "p. 1", "sha256": "a" * 64}],
            "published_at": None, "corrections": [], "state": "draft", "author": "Ada  Lovelace",
            "limitations": "Synthetic limits.", "counterevidence": "None found."}
    base.update(changes)
    return base


class FindingReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-review-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic County",
                 "--state", "ca")
        self.draft = self.base / "draft.json"
        self.write_draft(draft())
        self.receipts = self.root / "private" / "reviews" / "syn-finding-1.json"

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if returncode:
            self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        return result

    def write_draft(self, document, path=None):
        (path or self.draft).write_text(json.dumps(document), encoding="utf-8")

    def review(self, role="factual", decision="approve", reviewer="Rev One", rationale="Checked p. 1.",
               returncode=0, path=None):
        result = self.cli("finding", "review", path or self.draft, "--directory", self.root, "--role", role,
                          "--decision", decision, "--reviewer", reviewer, "--rationale", rationale,
                          returncode=returncode)
        return json.loads(result.stdout) if returncode == 0 else result

    def status(self, path=None, returncode=0):
        result = self.cli("finding", "review-status", path or self.draft, "--directory", self.root,
                          returncode=returncode)
        return json.loads(result.stdout) if returncode == 0 else result

    def approve_all(self):
        self.review("factual", reviewer="Rev One")
        self.review("legal", reviewer="Rev Two")
        self.review("privacy", reviewer="Rev One")

    def tree(self):
        return {str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() else None)
                for p in sorted(self.root.rglob("*"))}

    def test_two_reviewers_three_roles_clear_review_blockers(self):
        self.assertTrue(REVIEW_BLOCKERS <= set(self.status()["review_blockers"]))
        self.approve_all()
        report = self.status()
        self.assertEqual(report["review_blockers"], [])
        self.assertEqual((report["receipts_for_this_content"], report["stale_receipts"]), (3, 0))
        receipt = json.loads(self.receipts.read_text())[0]
        self.assertEqual(sorted(receipt), ["content_sha256", "decision", "rationale", "reviewed_at", "reviewer", "role"])
        self.assertRegex(receipt["reviewed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(receipt["content_sha256"], report["content_sha256"])

    def test_one_reviewer_for_all_roles_still_blocked_and_structural_blockers_remain(self):
        for role in ("factual", "legal", "privacy"):
            self.review(role, reviewer="Rev One")
        self.assertEqual(self.status()["review_blockers"], ["need_two_independent_reviewers"])
        self.write_draft(draft(sources=[]))
        self.assertIn("missing_evidence", self.status()["review_blockers"])

    def test_publication_fields_do_not_unbind_but_summary_edit_does(self):
        self.approve_all()
        self.write_draft(draft(state="published", published_at="2026-02-01", updated_at="2026-02-02",
                               corrections=[{"date": "2026-02-02", "note": "x"}]))
        report = self.status()
        self.assertEqual((report["review_blockers"], report["stale_receipts"]), ([], 0))
        self.write_draft(draft(summary="Edited synthetic summary."))
        report = self.status()
        self.assertEqual((report["receipts_for_this_content"], report["stale_receipts"]), (0, 3))
        self.assertTrue(REVIEW_BLOCKERS <= set(report["review_blockers"]))

    def test_author_cannot_review_in_any_case_or_spacing(self):
        for name in ("ada lovelace", " Ada  Lovelace ", "ADA\u00a0LOVELACE"):
            result = self.review(reviewer=name, returncode=1)
            self.assertIn("the author cannot review their own finding", result.stderr)
        self.assertFalse(os.path.lexists(self.root / "private" / "reviews"))

    def test_bad_arguments_stop_and_leave_file_unchanged(self):
        self.review()
        before = self.receipts.read_bytes()
        for kwargs in ({"role": "editorial"}, {"decision": "approved"}, {"rationale": "   "},
                       {"reviewer": ""}, {"rationale": "line\nbreak"}, {"reviewer": "Rev\x07Two"},
                       {"rationale": "x" * 2001}, {"reviewer": "r" * 121}):
            with self.subTest(kwargs=kwargs):
                self.review(**dict({"reviewer": "Rev Two"}, **kwargs), returncode=1)
                self.assertEqual(self.receipts.read_bytes(), before)
        self.review(reviewer="Rev Two", rationale="x" * 2000)

    def test_reject_or_changes_requested_on_current_content_is_unresolved_challenge(self):
        self.approve_all()
        self.review("legal", decision="reject", reviewer="Rev Three")
        self.assertEqual(self.status()["review_blockers"], ["unresolved_challenge"])
        self.write_draft(draft(summary="Revised synthetic summary."))
        self.review("legal", decision="changes_requested", reviewer="Rev Three")
        self.assertIn("unresolved_challenge", self.status()["review_blockers"])

    def test_duplicate_refused_but_new_content_can_be_reviewed_again(self):
        self.review()
        before = self.receipts.read_bytes()
        result = self.review(reviewer=" rev  ONE ", returncode=1)
        self.assertIn("already recorded", result.stderr)
        self.assertEqual(self.receipts.read_bytes(), before)
        self.review(decision="changes_requested")
        self.write_draft(draft(summary="Changed synthetic summary."))
        self.review()
        self.assertEqual(len(json.loads(self.receipts.read_text())), 3)

    def test_receipt_file_symlink_or_not_a_list_refused_never_followed(self):
        self.review()
        target = self.base / "outside.json"
        target.write_text("[]")
        self.receipts.unlink()
        self.receipts.symlink_to(target)
        for command in (self.review, self.status):
            self.assertIn("symlink", command(returncode=1).stderr)
        self.assertEqual(target.read_text(), "[]")
        self.assertTrue(self.receipts.is_symlink())
        self.receipts.unlink()
        for text in ('{"a": 1}', "not json", ""):
            self.receipts.write_text(text)
            with self.subTest(text=text):
                self.assertIn(str(self.receipts), self.review(returncode=1).stderr)
                self.assertEqual(self.receipts.read_text(), text)
        self.receipts.unlink()
        (self.receipts.parent / ".lock").unlink()
        self.receipts.parent.rmdir()
        self.receipts.parent.symlink_to(self.base)
        for command in (self.review, self.status):
            self.assertIn("real directory", command(returncode=1).stderr)
        self.assertFalse((self.base / "syn-finding-1.json").exists())

    def test_bad_drafts_refused_with_path_named(self):
        bad = self.base / "bad.json"
        link = self.base / "link.json"
        link.symlink_to(self.draft)
        cases = [(link, None), (bad, b"\xff\xfe{}"), (bad, b"[1, 2]"), (bad, b"{bad json"),
                 (bad, json.dumps(draft(stray_note="hidden")).encode()),
                 (bad, json.dumps(draft(id="../escape")).encode()), (bad, json.dumps(draft(id="x" * 65)).encode()),
                 (bad, json.dumps(draft(id=7)).encode()), (bad, b'{"id": "a", "summary": NaN}')]
        for path, data in cases:
            if data is not None:
                bad.write_bytes(data)
            with self.subTest(data=data):
                for command in (self.review, self.status):
                    stderr = command(path=path, returncode=1).stderr
                    self.assertIn(str(path), stderr)
                    self.assertNotIn("hidden", stderr)
        bad.write_text(json.dumps(draft(stray_note="hidden")))
        self.assertIn("'stray_note'", self.status(path=bad, returncode=1).stderr)
        self.assertFalse(os.path.lexists(self.root / "private" / "reviews"))

    def test_modes_and_no_write_outside_private_reviews(self):
        (self.root / "content").mkdir()
        (self.root / "content" / "keep.json").write_text("{}")
        (self.root / "public").mkdir()
        (self.root / "public" / "index.html").write_text("synthetic")
        before = self.tree()
        draft_bytes = self.draft.read_bytes()
        self.approve_all()
        after = self.tree()
        changed = {k for k in set(before) | set(after) if before.get(k, 0) != after.get(k, 0)}
        self.assertEqual(changed, {"private/reviews", "private/reviews/.lock", "private/reviews/syn-finding-1.json"})
        self.assertEqual(self.draft.read_bytes(), draft_bytes)
        self.assertEqual(self.receipts.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.receipts.parent / ".lock").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.receipts.parent.stat().st_mode & 0o777, 0o700)

    def test_review_status_never_writes(self):
        self.assertFalse(os.path.lexists(self.root / "private" / "reviews"))
        self.status()
        self.assertFalse(os.path.lexists(self.root / "private" / "reviews"))
        self.review()
        before = (self.receipts.read_bytes(), self.receipts.stat().st_mtime_ns)
        self.status()
        self.assertEqual((self.receipts.read_bytes(), self.receipts.stat().st_mtime_ns), before)

    def test_concurrent_reject_and_approve_both_survive(self):
        from campaign_tool import findings
        self.approve_all()
        document = json.loads(self.draft.read_text())
        original = findings._load_receipts

        def slow_load(path):  # widen the read-to-replace window so unserialized writers would collide
            receipts = original(path)
            time.sleep(0.5)
            return receipts

        errors = []

        def write(role, decision, reviewer):
            try:
                findings.add_receipt(self.root, document, role, decision, reviewer, "Concurrent synthetic check.")
            except Exception as exc:  # surfaced below
                errors.append(exc)

        with mock.patch.object(findings, "_load_receipts", slow_load):
            threads = [threading.Thread(target=write, args=("legal", "reject", "Rev Three")),
                       threading.Thread(target=write, args=("privacy", "approve", "Rev Four"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(errors, [])
        receipts = json.loads(self.receipts.read_text())
        self.assertEqual(len(receipts), 5)
        self.assertEqual({(r["reviewer"], r["decision"]) for r in receipts[3:]},
                         {("Rev Three", "reject"), ("Rev Four", "approve")})
        self.assertEqual(self.status()["review_blockers"], ["unresolved_challenge"])

    def test_requires_a_campaign_directory(self):
        self.assertIn("campaign.json", self.cli("finding", "review-status", self.draft, "--directory",
                                                self.base / "nowhere", returncode=1).stderr)


if __name__ == "__main__":
    unittest.main()
