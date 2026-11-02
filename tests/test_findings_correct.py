"""DF2237-03: `finding correct ID --date D --note T [--from DRAFT]`. Synthetic campaign published via findings.publish.

Run from the repository root: python3 -B -m unittest tests.test_findings_correct -v
"""
import json
import os
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings  # noqa: E402
from campaign_tool.content import load_content  # noqa: E402
from tests import test_findings_publish as publish_tests  # noqa: E402  (module import: not re-collected here)

NOW, RECORDS, draft, sha = publish_tests.NOW, publish_tests.RECORDS, publish_tests.draft, publish_tests.sha
LATER = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)


class FindingCorrectTests(unittest.TestCase):
    # Reuse the DF2237-02 synthetic campaign helpers without re-running its tests (no TestCase bound here).
    _p = vars(publish_tests.FindingPublishTests)
    setUp_campaign, cli, register, write_draft, receipt, approve_all, log_lines = (
        _p["setUp"], _p["cli"], _p["register"], _p["write_draft"], _p["receipt"], _p["approve_all"], _p["log_lines"])

    def setUp(self):
        self.setUp_campaign()
        self.approve_all()
        findings.publish(self.root, self.draft, "Pat Organizer", now=NOW)
        self.path = self.content / "findings" / "synthetic-title.json"
        self.original = json.loads(self.path.read_text(encoding="utf-8"))

    def correct(self, date="2026-03-06", note="Fixed the page number.", from_draft=None, now=LATER):
        return findings.correct(self.root, "syn-pub-1", date, note, from_draft, now=now)

    def document(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def refused(self, needle, finding_id="syn-pub-1", **kwargs):
        before, lines = self.path.read_bytes() if self.path.exists() else None, self.log_lines()
        with self.assertRaises(ValueError) as caught:
            if finding_id == "syn-pub-1":
                self.correct(**kwargs)
            else:
                findings.correct(self.root, finding_id, "2026-03-06", "Note.", now=LATER)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
        self.assertEqual(self.log_lines(), lines)
        return str(caught.exception)

    def new_text(self, **changes):
        document = draft(summary="Corrected synthetic summary.", **changes)
        path = self.base / "corrected.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path, document

    def test_note_only_correction_via_cli_keeps_everything_else(self):
        result = self.cli("finding", "correct", "syn-pub-1", "--directory", self.root, "--date", "2026-03-04",
                          "--note", "  Fixed the page number.  ")
        lines = result.stdout.splitlines()
        self.assertEqual(json.loads(lines[0]), {"corrected": "synthetic-title", "corrections": 1})
        self.assertIn("Not built or deployed.", lines[1])
        document = self.document()
        self.assertEqual(document["corrections"], [{"date": "2026-03-04", "note": "Fixed the page number."}])
        self.assertRegex(document["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        for key in ("id", "slug", "published_at", "state"):
            self.assertEqual(document[key], self.original[key])
        self.assertEqual(findings.reviewed_view(document), findings.reviewed_view(self.original))
        self.assertEqual(json.loads(self.log_lines()[-1]), {
            "at": document["updated_at"], "action": "correct", "id": "syn-pub-1", "slug": "synthetic-title",
            "date": "2026-03-04", "content_sha256": findings.reviewed_hash(self.original), "text_changed": False})
        self.assertEqual(len(self.log_lines()), 2)
        [loaded] = load_content(self.root)["findings"]
        self.assertEqual(loaded["corrections"], document["corrections"])
        self.cli("build", "--directory", self.root, "--check")
        refusal = self.cli("finding", "correct", "syn-pub-1", "--directory", self.root, "--date", "2026-13-01",
                           "--note", "x", returncode=1)
        self.assertIn("not a real calendar date", refusal.stderr)

    def test_date_order_rules(self):
        self.refused("is in the future", date="2026-03-11")
        self.refused("before the publication date 2026-03-04", date="2026-03-03")
        self.assertEqual(self.correct(date="2026-03-04"), {"corrected": "synthetic-title", "corrections": 1})
        self.correct(date="2026-03-06", now=datetime(2026, 3, 7, tzinfo=timezone.utc))
        self.refused("before the last correction date 2026-03-06", date="2026-03-05")
        self.assertEqual(self.correct(date="2026-03-06")["corrections"], 3)  # equal to the last one is fine
        self.correct(date="2026-03-10")  # today (UTC) is fine
        document = self.document()
        self.assertEqual([c["date"] for c in document["corrections"]],
                         ["2026-03-04", "2026-03-06", "2026-03-06", "2026-03-10"])
        self.assertEqual(document["updated_at"], "2026-03-10T12:00:00Z")
        self.assertEqual(len(self.log_lines()), 5)

    def test_future_is_judged_by_utc_date(self):
        tokyo = datetime(2026, 3, 11, 1, 0, 0, tzinfo=timezone(timedelta(hours=9)))  # 2026-03-10 16:00 UTC
        self.refused("is in the future (today is 2026-03-10 UTC)", date="2026-03-11", now=tokyo)
        pacific = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone(timedelta(hours=-8)))  # 2026-03-11 04:00 UTC
        self.correct(date="2026-03-11", now=pacific)
        self.assertEqual(self.document()["updated_at"], "2026-03-11T04:00:00Z")

    def test_bad_date_shapes_and_notes_refuse(self):
        for date, needle in (("2026-13-01", "not a real calendar date"), ("2026-02-30", "not a real calendar date"),
                             ("2026-11-1", "must be YYYY-MM-DD"), ("20260305", "must be YYYY-MM-DD"),
                             ("", "must be YYYY-MM-DD"), ("2026-03-06T00:00", "must be YYYY-MM-DD")):
            with self.subTest(date=date):
                self.refused(needle, date=date)
        for note, needle in (("", "must not be empty"), ("   ", "must not be empty"),
                             ("x" * 1001, "longer than 1000"), ("bad\x07", "control characters"),
                             ("two\nlines", "control characters")):
            with self.subTest(note=note[:10]):
                self.refused(needle, note=note)
        self.assertEqual(self.document(), self.original)

    def test_from_draft_with_full_approvals_replaces_text_and_extends_corrections(self):
        self.correct(date="2026-03-05", note="First note.")
        path, document = self.new_text()
        self.approve_all(document=document)
        self.assertEqual(self.correct(note="Summary corrected.", from_draft=path)["corrections"], 2)
        result = self.document()
        self.assertEqual(findings.reviewed_view(result), findings.reviewed_view(document))
        self.assertEqual(result["summary"], "Corrected synthetic summary.")
        self.assertEqual(result["corrections"], [{"date": "2026-03-05", "note": "First note."},
                                                 {"date": "2026-03-06", "note": "Summary corrected."}])
        for key in ("id", "slug", "published_at", "state"):
            self.assertEqual(result[key], self.original[key])
        entry = json.loads(self.log_lines()[-1])
        self.assertEqual((entry["text_changed"], entry["content_sha256"]), (True, findings.reviewed_hash(document)))
        self.assertEqual(len(load_content(self.root)["findings"]), 1)

    def test_from_draft_approved_only_on_old_text_refuses(self):
        path, _ = self.new_text()
        self.refused("review blockers", from_draft=path)
        error = self.refused("missing_factual_review", from_draft=path)
        self.assertIn("need_two_independent_reviewers", error)
        self.assertEqual(self.document(), self.original)

    def test_from_draft_with_a_challenge_on_new_text_refuses(self):
        path, document = self.new_text()
        self.approve_all(document=document)
        self.receipt("legal", "Rev Three", decision="changes_requested", document=document)
        self.refused("unresolved_challenge", from_draft=path)

    def test_from_draft_with_other_id_or_slug_or_bad_citation_refuses(self):
        for changes in ({"id": "syn-other"}, {"slug": "other-slug"}):
            with self.subTest(changes=changes):
                path, document = self.new_text(**changes)
                self.approve_all(document=document)
                self.refused("draft id and slug must be 'syn-pub-1' and 'synthetic-title'", from_draft=path)
        path, document = self.new_text(sources=[{"locator": "page:1", "sha256": sha(RECORDS[2])}])
        self.approve_all(document=document)
        self.refused(f"{sha(RECORDS[2])[:12]} unregistered", from_draft=path)
        self.register(*RECORDS)
        self.refused(f"{sha(RECORDS[2])[:12]} not_ingested", from_draft=path)
        path, document = self.new_text()
        self.approve_all(document=document)
        (self.objects / sha(RECORDS[0])).unlink()
        self.refused(f"{sha(RECORDS[0])[:12]} object_missing", from_draft=path)

    def test_unknown_duplicate_or_symlinked_finding_refuses(self):
        self.refused("0 published findings have it", finding_id="syn-missing")
        duplicate = self.content / "findings" / "copy.json"
        shutil.copyfile(self.path, duplicate)
        self.refused("2 published findings have it")
        duplicate.unlink()
        outside = self.base / "outside.json"
        shutil.copyfile(self.path, outside)
        self.path.unlink()
        os.symlink(outside, self.path)
        before = outside.read_bytes()
        self.refused("symlink")
        self.assertEqual(outside.read_bytes(), before)
        self.assertTrue(self.path.is_symlink())

    def test_withdrawn_style_state_is_not_corrected(self):
        self.path.write_text(json.dumps(dict(self.original, state="withdrawn")), encoding="utf-8")
        self.refused("only published findings are rendered")


if __name__ == "__main__":
    unittest.main()
