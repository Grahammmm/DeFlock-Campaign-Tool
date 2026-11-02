"""DF2237-04: `finding withdraw ID --reason T`. Synthetic campaign published via findings.publish.

Run from the repository root: python3 -B -m unittest tests.test_findings_withdraw -v
"""
import json
import os
import shutil
import stat
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings  # noqa: E402
from campaign_tool.content import load_content  # noqa: E402
from tests import test_findings_publish as publish_tests  # noqa: E402  (module import: not re-collected here)

NOW, draft = publish_tests.NOW, publish_tests.draft
LATER = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
REASON = "Synthetic reason: the cited page was misread."


class FindingWithdrawTests(unittest.TestCase):
    # Reuse the DF2237-02 synthetic campaign helpers without re-running its tests (no TestCase bound here).
    _p = vars(publish_tests.FindingPublishTests)
    setUp_campaign, cli, register, write_draft, receipt, approve_all, log_lines, public_tree = (
        _p["setUp"], _p["cli"], _p["register"], _p["write_draft"], _p["receipt"], _p["approve_all"], _p["log_lines"],
        _p["public_tree"])

    def setUp(self):
        self.setUp_campaign()
        self.approve_all()
        findings.publish(self.root, self.draft, "Pat Organizer", now=NOW)
        self.path = self.content / "findings" / "synthetic-title.json"
        self.original = self.path.read_bytes()
        self.withdrawn = self.root / "private" / "withdrawn"
        self.copy = self.withdrawn / "synthetic-title.json"

    def withdraw(self, finding_id="syn-pub-1", reason=REASON):
        return findings.withdraw(self.root, finding_id, reason, now=LATER)

    def refused(self, needle, finding_id="syn-pub-1", reason=REASON):
        lines = self.log_lines()
        before = sorted(p.name for p in self.withdrawn.iterdir()) if self.withdrawn.is_dir() else []
        with self.assertRaises(ValueError) as caught:
            self.withdraw(finding_id, reason)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.path.read_bytes(), self.original)  # nothing moved
        self.assertEqual(sorted(p.name for p in self.withdrawn.iterdir()) if self.withdrawn.is_dir() else [], before)
        self.assertEqual(self.log_lines(), lines)
        return str(caught.exception)

    def publish_draft(self, returncode, **changes):
        self.write_draft(draft(**changes))
        self.approve_all()
        return self.cli("finding", "publish", self.draft, "--directory", self.root, "--approved-by", "Pat Organizer",
                        returncode=returncode)

    def test_withdraw_via_cli_moves_file_privately_and_logs_without_reason(self):
        result = self.cli("finding", "withdraw", "syn-pub-1", "--directory", self.root, "--reason", REASON)
        lines = result.stdout.splitlines()
        self.assertEqual(json.loads(lines[0]), {"withdrawn": "synthetic-title",
                                                "path": "private/withdrawn/synthetic-title.json"})
        self.assertEqual(lines[1], "Run build to remove it from public/. Nothing was deployed.")
        self.assertFalse(os.path.lexists(self.path))
        kept = json.loads(self.copy.read_text(encoding="utf-8"))
        self.assertEqual(set(kept["withdrawn"]), {"at", "reason"})
        self.assertEqual(kept["withdrawn"]["reason"], REASON)
        self.assertRegex(kept["withdrawn"]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual({k: v for k, v in kept.items() if k != "withdrawn"}, json.loads(self.original))
        self.assertEqual(stat.S_IMODE(self.withdrawn.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.copy.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.withdrawn.iterdir()], ["synthetic-title.json"])  # no temp left
        self.assertEqual(len(self.log_lines()), 2)
        self.assertEqual(json.loads(self.log_lines()[-1]), {"at": kept["withdrawn"]["at"], "action": "withdraw",
                                                            "id": "syn-pub-1", "slug": "synthetic-title"})
        self.assertNotIn("misread", self.log.read_text(encoding="utf-8"))
        self.assertEqual(load_content(self.root)["findings"], [])
        self.assertEqual(findings.withdrawn_names(self.root), ({"syn-pub-1"}, {"synthetic-title"}))

    def test_next_build_omits_the_finding_and_adds_no_notice_page(self):
        self.cli("build", "--directory", self.root)
        before = self.public_tree()
        self.assertIn("findings/synthetic-title.html", before)  # control: it was public
        self.assertIn(b"synthetic-title", before["sitemap.xml"])
        self.assertIn(b"synthetic-title", before["findings/index.html"])
        self.withdraw()
        self.cli("build", "--directory", self.root)
        after = self.public_tree()
        self.assertNotIn("findings/synthetic-title.html", after)
        self.assertLessEqual(set(after), set(before))  # no notice page or any other new file
        for name, data in after.items():
            for needle in (b"synthetic-title", b"Synthetic title", b"misread", b"withdrawn"):
                self.assertNotIn(needle, data, name)
        self.cli("build", "--directory", self.root, "--check")

    def test_later_publish_of_withdrawn_id_or_slug_refuses(self):
        self.withdraw()
        lines = self.log_lines()
        for changes in ({"slug": "fresh-slug", "title": "Fresh title"}, {"id": "syn-pub-2"}):
            with self.subTest(changes=changes):
                result = self.publish_draft(1, **changes)
                self.assertIn("was withdrawn and stays taken", result.stderr)
                self.assertEqual(sorted(p.name for p in (self.content / "findings").iterdir()), [])
                self.assertEqual(self.log_lines(), lines)
        self.publish_draft(0, id="syn-pub-2", slug="fresh-slug", title="Fresh title")  # control: new id and slug
        self.assertEqual([f["slug"] for f in load_content(self.root)["findings"]], ["fresh-slug"])

    def test_unknown_duplicate_or_symlinked_finding_refuses(self):
        self.refused("0 published findings have it", finding_id="syn-missing")
        duplicate = self.content / "findings" / "copy.json"
        shutil.copyfile(self.path, duplicate)
        self.refused("2 published findings have it")
        duplicate.unlink()
        outside = self.base / "outside.json"
        outside.write_bytes(self.original)
        self.path.unlink()
        os.symlink(outside, self.path)
        self.original = self.path.read_bytes()
        self.refused("symlink")
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(outside.read_bytes(), self.original)

    def test_bad_reason_refuses(self):
        for reason, needle in (("", "must not be empty"), ("   ", "must not be empty"),
                               ("x" * 1001, "longer than 1000"), ("bad\x07", "control characters"),
                               ("two\nlines", "control characters")):
            with self.subTest(reason=reason[:10]):
                self.refused(needle, reason=reason)
        self.assertFalse(self.withdrawn.exists())
        result = self.cli("finding", "withdraw", "syn-pub-1", "--directory", self.root, "--reason", "", returncode=1)
        self.assertIn("--reason must not be empty", result.stderr)

    def test_existing_private_copy_refuses_and_is_untouched(self):
        self.withdrawn.mkdir(mode=0o700)
        self.copy.write_text('{"id": "older"}', encoding="utf-8")
        self.refused("private/withdrawn/synthetic-title.json already exists")
        self.assertEqual(self.copy.read_text(encoding="utf-8"), '{"id": "older"}')
        self.copy.unlink()
        os.symlink(self.base / "nowhere.json", self.copy)  # a dangling symlink in its place also refuses
        self.refused("private/withdrawn/synthetic-title.json already exists")
        self.assertFalse(os.path.exists(self.base / "nowhere.json"))

    def test_bad_publication_log_refuses_before_anything_is_written(self):
        outside = self.base / "outside-log.jsonl"
        outside.write_bytes(self.log.read_bytes())
        self.log.unlink()
        os.symlink(outside, self.log)
        self.assertIn("cannot open publication log", self.refused("publication-log.jsonl"))
        self.assertFalse(os.path.lexists(self.copy))

    def test_unlink_failure_keeps_both_copies_and_names_the_leftover(self):
        lines = self.log_lines()
        with mock.patch.object(findings.os, "unlink", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(ValueError) as caught:
                self.withdraw()
        error = str(caught.exception)
        self.assertIn("leftover content/findings/synthetic-title.json", error)
        self.assertIn("both copies remain", error)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(json.loads(self.copy.read_text(encoding="utf-8"))["withdrawn"]["reason"], REASON)
        self.assertEqual(self.log_lines(), lines)
        self.assertEqual(sorted(p.name for p in self.withdrawn.iterdir()), ["synthetic-title.json"])

    def test_withdrawn_names_names_unreadable_files(self):
        self.assertEqual(findings.withdrawn_names(self.root), (set(), set()))
        self.withdrawn.mkdir(mode=0o700)
        bad = self.withdrawn / "broken.json"
        bad.write_text("not json", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            findings.withdrawn_names(self.root)
        self.assertIn(f"{bad}: withdrawn finding is not valid JSON", str(caught.exception))
        bad.unlink()
        (self.withdrawn / "folder.json").mkdir()
        with self.assertRaises(ValueError) as caught:
            findings.withdrawn_names(self.root)
        self.assertIn("folder.json: withdrawn finding is not a regular file", str(caught.exception))
        result = self.publish_draft(1, id="syn-pub-2", slug="fresh-slug", title="Fresh title")
        self.assertIn("folder.json", result.stderr)  # publish refuses instead of skipping it


if __name__ == "__main__":
    unittest.main()
