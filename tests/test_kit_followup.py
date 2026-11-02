"""kit-followup writes a private, never-sent follow-up draft for a past-due hand-sent request. Offline, synthetic."""
import contextlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit, kit_track, law  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

DONE = "Draft only. Nothing was sent.\n"


def run(*arguments):
    """Run the CLI in-process; return (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}


class KitFollowupTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-followup-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.ids = [path.stem for path in sorted((self.root / "kit" / "requests").glob("*.md"))]
        self.assertGreaterEqual(len(self.ids), 4)
        self.a, self.b = self.ids[:2]
        ready_drafts(self.root)  # kit-sent refuses unfilled placeholders and unverified recipients (DF1507-07)
        self.package = law.load_package("us-ca")
        self.folder = self.root / "kit" / "followups"

    def send(self, agency, on="2026-03-02"):
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", agency, "--date", on, "--channel", "email")
        self.assertEqual(code, 0, err)

    def step(self, agency, on, kind):
        code, _, err = run("kit-response", "--directory", self.root, "--agency", agency, "--date", on, "--kind", kind)
        self.assertEqual(code, 0, err)

    def followup(self, agency, as_of, *extra):
        code, out, err = run("kit-followup", "--directory", self.root, "--agency", agency, "--as-of", as_of, *extra)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith(DONE), out)
        return json.loads(out[:-len(DONE)])

    def stopped(self, words, agency, as_of, *extra):
        code, out, err = run("kit-followup", "--directory", self.root, "--agency", agency, "--as-of", as_of, *extra)
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(words, err)

    def deadline(self, sent_on, extension=False):
        return law.deadline(self.package, sent_on, extension=extension).isoformat()

    def law_copy(self, **changes):
        base = self.tmp / "law"
        (base / "us-ca").mkdir(parents=True, exist_ok=True)
        package = json.loads(law.package_path("us-ca").read_text(encoding="utf-8"))
        package.update(changes)
        (base / "us-ca" / "package.json").write_text(json.dumps(package), encoding="utf-8")
        return base

    def test_past_due_draft_cites_sent_date_due_law_and_draft_status(self):
        self.send(self.a)
        due = self.deadline("2026-03-02")
        self.assertEqual(due, "2026-03-12")
        report = self.followup(self.a, "2026-04-01")  # 30 days after sending, no steps
        name = f"{self.a}-2026-04-01.md"
        self.assertEqual(report, {"written": f"kit/followups/{name}", "due": due, "days_past": 20,
                                  "note": kit_track.NOTE})
        text = (self.folder / name).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Follow-up draft: "))
        self.assertIn("DRAFT — NOT SENT. Review before sending it yourself.", text)
        self.assertIn("On 2026-03-02 we sent a records request by email", text)
        self.assertIn(f"determination date was {due}; the period runs from the agency's receipt", text)
        records_law = self.package["records_law"]
        self.assertIn(f"{records_law['name']}, {records_law['citation']}", text)
        self.assertEqual(self.package["status"], "draft")
        self.assertIn("Law package status: draft (not independently reviewed)", text)
        self.assertIn(law.HOLIDAY_NOTE, text)
        self.assertIn(kit.DEFAULT_REQUESTER, text)
        names = {e["agency_id"]: e["name"] for e in json.loads((self.root / "kit" / "agencies.json").read_text())}
        self.assertIn(names[self.a], text)

    def test_due_today_before_due_or_before_sending_is_not_past_due(self):
        self.send(self.a)
        for as_of in ("2026-03-12", "2026-03-05", "2026-03-01"):
            with self.subTest(as_of=as_of):
                self.stopped("not past due: provisional determination date 2026-03-12", self.a, as_of)
        self.assertFalse(os.path.lexists(self.folder))

    def test_acknowledged_drafts_extension_moves_due_answered_and_closed_refused(self):
        self.send(self.a)
        self.step(self.a, "2026-03-04", "acknowledged")
        self.assertEqual(self.followup(self.a, "2026-03-13")["days_past"], 1)  # acknowledging is no answer
        self.step(self.a, "2026-03-11", "extension")
        extended = self.deadline("2026-03-02", extension=True)
        self.stopped(f"not past due: provisional determination date {extended}", self.a, "2026-03-20")
        report = self.followup(self.a, "2026-04-01")
        self.assertEqual(report["due"], extended)
        self.assertIn(f"was {extended};", (self.folder / f"{self.a}-2026-04-01.md").read_text(encoding="utf-8"))
        for agency, kind in zip(self.ids[1:4], ("determination", "produced", "denied")):
            with self.subTest(kind=kind):
                self.send(agency)
                self.step(agency, "2026-03-20", kind)
                self.stopped(f"request for {agency} was answered ({kind} on 2026-03-20); no follow-up drafted",
                             agency, "2026-04-01")
        self.step(self.a, "2026-04-02", "closed")
        self.stopped(f"request for {self.a} is closed", self.a, "2026-04-10")
        self.step(self.b, "2026-04-02", "closed")
        self.stopped(f"request for {self.b} is closed", self.b, "2026-04-10")

    def test_no_request_unknown_agency_future_or_malformed_as_of_stop(self):
        self.stopped(f"no hand-sent request recorded for {self.b}", self.b, "2026-04-01")
        self.stopped("no hand-sent request recorded for ca-not-an-agency", "ca-not-an-agency", "2026-04-01")
        self.stopped("no hand-sent request recorded for ../escape", "../escape", "2026-04-01")
        self.send(self.a)
        self.stopped("--as-of 2999-01-01 is in the future", self.a, "2999-01-01")
        for bad in ("2026-02-30", "2026-3-1", "today", ""):
            with self.subTest(bad=bad):
                self.stopped("--as-of must be a real YYYY-MM-DD date", self.a, bad)
        self.assertFalse(os.path.lexists(self.folder))

    def test_missing_law_package_refused_and_reviewed_package_has_no_draft_line(self):
        self.send(self.a)
        empty = self.tmp / "no-law"
        empty.mkdir()
        with mock.patch.object(law, "JURISDICTIONS", empty):
            self.stopped("no law package: dates cannot be computed", self.a, "2026-04-01")
        self.assertFalse(os.path.lexists(self.folder))
        base = self.law_copy(status="reviewed", reviewed_by=["Synthetic Reviewer"], reviewed_at="2026-01-05")
        report = kit_track.followup(self.root, self.a, "2026-04-01", law_base=base)
        text = (self.root / report["written"]).read_text(encoding="utf-8")
        self.assertNotIn("not independently reviewed", text)
        self.assertNotIn("Law package status", text)
        self.assertIn(self.package["records_law"]["citation"], text)

    def test_existing_target_needs_replace_and_symlinks_are_refused(self):
        self.send(self.a)
        self.followup(self.a, "2026-04-01")
        target = self.folder / f"{self.a}-2026-04-01.md"
        target.write_text("organizer edits\n", encoding="utf-8")
        self.stopped("already exists; use --replace", self.a, "2026-04-01")
        self.assertEqual(target.read_bytes(), b"organizer edits\n")
        self.followup(self.a, "2026-04-01", "--replace")
        self.assertIn("DRAFT — NOT SENT", target.read_text(encoding="utf-8"))
        outside = self.tmp / "outside.md"
        outside.write_text("keep\n", encoding="utf-8")
        link = self.folder / f"{self.a}-2026-04-02.md"
        link.symlink_to(outside)
        for extra in ((), ("--replace",)):
            with self.subTest(extra=extra):
                self.stopped("must not be a symlink", self.a, "2026-04-02", *extra)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep\n")
        self.assertTrue(link.is_symlink())
        for path in self.folder.iterdir():
            path.unlink()
        self.folder.rmdir()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.folder.symlink_to(elsewhere, target_is_directory=True)
        self.stopped("followups/ must not be a symlink", self.a, "2026-04-03")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_private_modes_sent_json_unchanged_and_only_followup_written(self):
        self.send(self.a)
        before = snapshot(self.root)
        report = self.followup(self.a, "2026-04-01")
        after = snapshot(self.root)
        self.assertEqual(set(after) - set(before), {report["written"]})
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)  # sent.json bytes included
        self.assertIn("kit/sent.json", before)
        self.assertEqual(stat.S_IMODE((self.root / report["written"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertEqual(json.loads((self.root / "kit" / "sent.json").read_text())["requests"][self.a]["steps"], [])

    def test_draft_has_no_foreign_address_and_no_full_hash(self):
        agencies_path = self.root / "kit" / "agencies.json"
        agencies = json.loads(agencies_path.read_text(encoding="utf-8"))
        entry = next(e for e in agencies if e["agency_id"] == self.a)
        entry["records_email"] = "records@example.org"
        agencies_path.write_text(json.dumps(agencies), encoding="utf-8")
        self.send(self.a)
        report = self.followup(self.a, "2026-04-01")
        text = (self.root / report["written"]).read_text(encoding="utf-8")
        allowed = json.dumps(entry)
        for token in re.findall(r"\S*@\S*", text):
            self.assertIn(token, allowed)
        self.assertIsNone(re.search(r"[0-9a-f]{64}", text))
        sent = json.loads((self.root / "kit" / "sent.json").read_text())["requests"][self.a]
        self.assertNotIn(sent["draft_sha256"], text)


if __name__ == "__main__":
    unittest.main()
