"""RF1651-2: correction-request evidence dates are compared as calendar days, not strings (synthetic, offline).

The content loader admits unpadded dates such as 2026-3-5, so the acceptance gate and the private reply window must
treat them as the same days as 2026-03-05.
Run from the repository root: python3 -B -m unittest tests.test_correction_request_calendar_dates -v
"""
import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import correction_requests as cr, findings  # noqa: E402
from campaign_tool.content import load_content  # noqa: E402
from tests import test_correction_requests as base  # noqa: E402  (module import: not re-collected here)

FID, MARKER = base.FID, "Correction date: [EDIT ME: confirm the correction date]"
LATER = date(2026, 4, 20)


class CalendarDateTests(base.CorrectionRequestTests):
    def setUp(self):
        super().setUp()
        self.replies = self.root / "kit" / "corrections" / "replies"

    def finding_file(self):
        return next(p for p in (self.root / "content" / "findings").glob("*.json") if json.loads(p.read_text())["id"] == FID)

    def correct_as(self, *spellings):
        """Record real corrections (topping up to len(spellings)), then rewrite their stored date text in order.
        The real loader must still admit the rewritten finding."""
        path = self.finding_file()
        document = json.loads(path.read_text())
        if len(document["corrections"]) < len(spellings):
            for correction in document["corrections"]:  # findings.correct orders by text: restore canonical dates first
                correction["date"] = "2026-03-08"
            path.write_text(json.dumps(document), encoding="utf-8")
            while len(json.loads(path.read_text())["corrections"]) < len(spellings):
                self.correct("2026-03-08")
            document = json.loads(path.read_text())
        self.assertEqual(len(document["corrections"]), len(spellings))
        for correction, spelled in zip(document["corrections"], spellings):
            correction["date"] = spelled
        path.write_text(json.dumps(document), encoding="utf-8")
        loaded = next(f for f in load_content(self.root)["findings"] if f["id"] == FID)
        self.assertEqual([c["date"] for c in loaded["corrections"]], list(spellings))

    def reply_text(self, n=1):
        target = self.replies / f"{n}.md"
        if target.exists():
            target.unlink()
        cr.reply(self.root, n)
        return target.read_text(encoding="utf-8")

    def next_id(self):
        return len(cr.list_requests(self.root, LATER)) + 1

    def accepted(self, received="2026-03-06", on="2026-03-10"):
        self.add(received=received)
        n = self.next_id() - 1
        self.correct_as("2026-03-08")
        self.decide(request_id=n, decision="accepted", on=on, note="Quote fixed.")
        return n

    def content_bytes(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted((self.root / "content").rglob("*"))
                if p.is_file()}

    def log_identity(self):
        st = self.file.stat()
        return self.file.read_bytes(), st.st_ino, st.st_mtime_ns, st.st_mode

    def test_pre_receipt_correction_never_authorizes_acceptance(self):
        self.add(received="2026-03-06")
        self.correct_as("2026-03-05")
        self.refused("accept only after recording the correction", self.decide, decision="accepted")  # padded control
        for spelled in ("2026-3-5", "2026-03-5", "2026-3-05"):
            with self.subTest(spelled):
                self.correct_as(spelled)
                message = self.refused("accept only after recording the correction: finding correct syn-pub-1",
                                       self.decide, decision="accepted", on="2026-03-10")
                self.assertIn("2026-03-06 or later", message)
        # an earlier month spelled unpadded sorts after the receipt as text
        self.add(received="2026-04-02", today=LATER)
        self.correct_as("2026-3-10")
        self.refused("accept only after recording", self.decide, request_id=2, decision="accepted", on="2026-04-10",
                     today=LATER)
        self.assertEqual([r["state"] for r in cr.list_requests(self.root, LATER)], ["open", "open"])

    def test_same_and_later_calendar_days_authorize_equally(self):
        self.correct_as("2026-03-08")
        records = []
        for spelled in ("2026-03-06", "2026-3-6", "2026-03-6", "2026-3-06", "2026-03-08", "2026-3-8", "2026-3-10"):
            with self.subTest(spelled):
                self.correct_as(spelled)
                self.add(received="2026-03-06")
                records.append(self.decide(request_id=self.next_id() - 1, decision="accepted", on="2026-03-10",
                                           note="Quote fixed."))
        self.assertEqual({tuple(sorted(r)) for r in records},
                         {("decided_on", "finding_id", "id", "note", "received_on", "state", "summary")})
        self.assertEqual({(r["state"], r["decided_on"]) for r in records}, {("accepted", "2026-03-10")})
        # unpadded pre-receipt decoys mixed with one qualifying date, in any order
        for spellings in (("2026-3-5", "2026-3-5", "2026-3-7"), ("2026-3-7", "2026-3-5", "2026-3-5")):
            with self.subTest(spellings):
                self.correct_as(*spellings)
                self.add(received="2026-03-06")
                self.assertEqual(self.decide(request_id=self.next_id() - 1, decision="accepted",
                                             note="Fixed.")["state"], "accepted")
        self.refused("decisions are final", self.decide, request_id=1, decision="accepted")

    def test_real_cli_refuses_padding_bypass_without_success_prefix(self):
        self.add(received="2026-03-06")
        self.correct_as("2026-3-5")
        before, content = self.log_identity(), self.content_bytes()
        result = self.cli("correction-request", "decide", "accepted", "--directory", self.root, "--id", "1",
                          "--on", "2026-03-10", "--note", "Quote fixed.", returncode=3)
        self.assertEqual(result.stdout, "")
        self.assertIn("accept only after recording the correction", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.log_identity(), before)
        self.assertEqual(self.content_bytes(), content)
        self.assertFalse((self.root / "kit").exists())

    def test_reply_selects_one_unpadded_in_window_correction(self):
        site = self.root / "content" / "site.json"
        document = json.loads(site.read_text())
        document["base_url"] = "https://example.org"
        site.write_text(json.dumps(document))
        n = self.accepted()
        self.correct_as("2026-03-08")
        padded = self.reply_text(n)
        self.assertIn("Decision date: 2026-03-10\nCorrection date: 2026-03-08\n", padded)
        for spelled in ("2026-3-8", "2026-03-8", "2026-3-08"):
            with self.subTest(spelled):
                self.correct_as(spelled)
                log, content = self.log_identity(), self.content_bytes()
                text = self.reply_text(n)
                self.assertIn(f"Decision date: 2026-03-10\nCorrection date: {spelled}\n", text)  # stored string shown
                self.assertEqual(text.replace(spelled, "2026-03-08"), padded)  # nothing else differs
                for needle in ("> Quote fixed.", "[EDIT ME: confirm the correction addressed this request]",
                               "Finding: https://example.org/findings/synthetic-title.html"):
                    self.assertIn(needle, text)
                self.assertEqual((self.replies / f"{n}.md").stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.log_identity(), log)
                self.assertEqual(self.content_bytes(), content)

    def test_reply_calendar_window_is_inclusive_and_excludes_neighbors(self):
        n = self.accepted()  # received 03-06, decided 03-10
        self.correct_as("2026-03-08")
        for spelled, shown in (("2026-3-6", True), ("2026-3-10", True), ("2026-3-5", False), ("2026-3-11", False)):
            with self.subTest(spelled):
                self.correct_as(spelled)
                text = self.reply_text(n)
                self.assertEqual(f"Correction date: {spelled}\n" in text, shown)
                self.assertEqual(MARKER in text, not shown)
        # a post-decision correction still authorizes acceptance but cannot supply the reply date
        self.add(received="2026-03-06")
        self.correct_as("2026-3-11")
        self.assertEqual(self.decide(request_id=2, decision="accepted", on="2026-03-07", note="Later fix.")["state"],
                         "accepted")
        self.assertIn(MARKER, self.reply_text(2))

    def test_reply_counts_entries_not_unique_days(self):
        n = self.accepted()
        for spellings, shown in ((("2026-3-5", "2026-3-12", "2026-3-9"), "2026-3-9"),  # one entry, decoys outside
                                 (("2026-3-5", "2026-3-12", "2026-3-13"), None),  # none in the window
                                 (("2026-03-08", "2026-3-8", "2026-3-12"), None),  # one day twice: still two entries
                                 (("2026-3-7", "2026-3-9", "2026-3-12"), None)):
            with self.subTest(spellings):
                self.correct_as(*spellings)
                text = self.reply_text(n)
                if shown:
                    self.assertIn(f"Correction date: {shown}\n", text)
                else:
                    self.assertIn(MARKER, text)
                    self.assertNotIn("Correction date: 2026", text)

    def test_other_states_and_invalid_content_keep_contracts(self):
        self.add(received="2026-03-06")
        self.add(received="2026-03-06")
        self.decide(request_id=2, decision="declined", on="2026-03-09", note="Records support the text.")
        self.correct_as("2026-3-8")
        for n, word in ((1, "being reviewed"), (2, "did not accept it")):
            text = self.reply_text(n)
            self.assertIn(word, text)
            self.assertNotIn("Correction date", text)
            self.assertNotIn("2026-3-8", text)
        # present but invalid content refuses before any draft; an accepted decision on a withdrawn finding refuses
        (self.replies / "2.md").unlink()
        site = self.root / "content" / "site.json"
        good = site.read_bytes()
        site.write_text("{not json")
        with self.assertRaises(ValueError) as caught:
            cr.reply(self.root, 2)
        self.assertIn("public content is unreadable", str(caught.exception))
        self.assertFalse((self.replies / "2.md").exists())
        site.write_bytes(good)
        findings.withdraw(self.root, FID, "Synthetic withdrawal.")
        self.refused("no published finding syn-pub-1", self.decide, request_id=1, decision="accepted")
        self.refused("already declined; decisions are final", self.decide, request_id=2, decision="accepted")

    def test_refusal_and_drafting_preserve_campaign_state(self):
        self.add(received="2026-03-06")
        self.correct_as("2026-3-5")
        before, content = self.log_identity(), self.content_bytes()
        self.refused("accept only after recording", self.decide, decision="accepted")
        self.assertEqual(self.log_identity(), before)
        self.assertEqual(self.content_bytes(), content)
        self.assertFalse((self.root / "kit").exists())
        # a later qualifying correction accepts; one draft is written, never replaced, nothing touches the network
        self.correct_as("2026-3-5", "2026-3-8")
        self.decide(decision="accepted", on="2026-03-10", note="Quote fixed.")
        decided, content = self.log_identity(), self.content_bytes()
        marker = (self.private / ".correction-requests.lock").stat().st_ino
        with mock.patch("socket.socket", side_effect=AssertionError("network")):
            self.assertEqual(cr.reply(self.root, 1)["state"], "accepted")
            self.assertIn("Correction date: 2026-3-8\n", (self.replies / "1.md").read_text(encoding="utf-8"))
            with self.assertRaises(ValueError) as caught:
                cr.reply(self.root, 1)
        self.assertIn("edit it by hand", str(caught.exception))
        self.assertEqual(self.log_identity(), decided)
        self.assertEqual(self.content_bytes(), content)
        self.assertEqual((self.private / ".correction-requests.lock").stat().st_ino, marker)
        self.assertEqual([p.name for p in self.replies.iterdir()], ["1.md"])


for _name in [n for n in dir(base.CorrectionRequestTests) if n.startswith("test_")]:
    setattr(CalendarDateTests, _name, None)  # inherited tests belong to tests.test_correction_requests

if __name__ == "__main__":
    unittest.main()
