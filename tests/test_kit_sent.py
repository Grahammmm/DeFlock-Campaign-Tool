"""kit-sent records a hand-sent request with its draft hash; kit-status lists provisional due dates. Offline, synthetic."""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit, kit_track, law  # noqa: E402


def run(*arguments):
    """Run the CLI in-process (so the urlopen patch applies); return (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class KitSentTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-sent-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.kit = self.root / "kit"
        self.sent = self.kit / "sent.json"
        drafts = sorted((self.kit / "requests").glob("*.md"))
        self.assertGreaterEqual(len(drafts), 2)
        self.agency, self.other = drafts[0].stem, drafts[1].stem
        self.draft = drafts[0]

    def send(self, agency=None, sent_on="2026-03-02", channel="email", *extra):
        return run("kit-sent", "--directory", self.root, "--agency", agency or self.agency, "--date", sent_on,
                   "--channel", channel, *extra)

    def kit_status(self):
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def assert_refused(self, result, *words):
        code, out, err = result
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)

    def test_happy_path_binds_draft_hash_and_shows_law_deadlines(self):
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        self.assertIn("Recorded only. Nothing was sent.", out)
        printed = json.loads(out.split("Recorded only.")[0])
        digest = hashlib.sha256(self.draft.read_bytes()).hexdigest()
        document = json.loads(self.sent.read_text())
        self.assertEqual(document["schema_version"], 1)
        entry = document["requests"][self.agency]
        self.assertEqual(entry["draft_sha256"], digest)
        self.assertEqual(printed["draft_sha256"], digest)
        self.assertEqual((entry["sent_on"], entry["channel"], entry["draft"], entry["steps"]),
                         ("2026-03-02", "email", f"requests/{self.agency}.md", []))
        report = self.kit_status()
        package = law.load_package("us-ca")
        self.assertEqual(report["law_package_status"], package["status"])
        self.assertTrue(report["note"].startswith("Provisional reminders only;"))
        self.assertIn(law.HOLIDAY_NOTE, report["note"])
        [row] = report["requests"]
        self.assertEqual(row["agency_id"], self.agency)
        self.assertIsInstance(row["name"], str)
        self.assertEqual(row["determination_due"], law.deadline(package, date(2026, 3, 2)).isoformat())
        self.assertEqual(row["with_extension"], law.deadline(package, date(2026, 3, 2), extension=True).isoformat())
        self.assertFalse(row["draft_changed_since_sent"])
        self.assertEqual((row["channel"], row["steps"]), ("email", 0))

    def test_status_sorted_by_sent_on_then_id_and_empty_without_file(self):
        self.assertEqual(self.kit_status()["requests"], [])
        self.assertFalse(self.sent.exists())
        self.assertEqual(self.send(self.agency, "2026-04-01", "portal")[0], 0)
        self.assertEqual(self.send(self.other, "2026-03-01", "mail")[0], 0)
        ids = [row["agency_id"] for row in self.kit_status()["requests"]]
        self.assertEqual(ids, [self.other, self.agency])

    def test_unknown_agency_missing_or_symlinked_draft_refused_without_file(self):
        self.assert_refused(self.send("ca-nowhere-police"), "not in kit/agencies.json")
        self.assertFalse(self.sent.exists())
        (self.kit / "requests" / (self.other + ".md")).unlink()
        self.assert_refused(self.send(self.other), "regular file")
        self.assertFalse(self.sent.exists())
        target = self.kit / "elsewhere.md"
        target.write_bytes(self.draft.read_bytes())
        self.draft.unlink()
        self.draft.symlink_to(target)
        self.assert_refused(self.send(), "regular file")
        self.assertFalse(self.sent.exists())

    def test_kit_not_built_refused(self):
        (self.kit / "agencies.json").unlink()
        self.assert_refused(self.send(), "run kit first")
        self.assertFalse(self.sent.exists())

    def test_bad_dates_refused(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        for value in (tomorrow, "1999-12-31", "2026-02-30", "2026-3-02", "yesterday"):
            with self.subTest(value=value):
                self.assert_refused(self.send(sent_on=value), "--date")
                self.assertFalse(self.sent.exists())
        self.assertEqual(self.send(sent_on="2000-01-01")[0], 0)
        self.assertEqual(self.send(self.other, sent_on=date.today().isoformat())[0], 0)

    def test_bad_channel_is_argparse_error(self):
        code, _, err = self.send(channel="fax")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)
        self.assertFalse(self.sent.exists())
        with self.assertRaisesRegex(ValueError, "--channel"):
            kit_track.record_sent(self.root, self.agency, "2026-03-02", "fax")

    def test_duplicate_refused_replace_allowed_only_without_steps(self):
        self.assertEqual(self.send()[0], 0)
        before = self.sent.read_bytes()
        self.assert_refused(self.send(sent_on="2026-03-05"), "already recorded", "2026-03-02")
        self.assertEqual(self.sent.read_bytes(), before)
        code, _, err = self.send(self.agency, "2026-03-05", "portal", "--replace")
        self.assertEqual(code, 0, err)
        entry = json.loads(self.sent.read_text())["requests"][self.agency]
        self.assertEqual((entry["sent_on"], entry["channel"]), ("2026-03-05", "portal"))
        document = json.loads(self.sent.read_text())
        document["requests"][self.agency]["steps"].append({"kind": "acknowledged", "on": "2026-03-06"})
        self.sent.write_text(json.dumps(document))
        before = self.sent.read_bytes()
        self.assert_refused(self.send(self.agency, "2026-03-07", "mail", "--replace"), "steps", "2026-03-05")
        self.assertEqual(self.sent.read_bytes(), before)
        self.assertEqual(self.kit_status()["requests"][0]["steps"], 1)

    def test_draft_edited_after_recording_is_flagged(self):
        self.assertEqual(self.send()[0], 0)
        self.draft.write_text(self.draft.read_text() + "\nEdited after sending.\n")
        self.assertTrue(self.kit_status()["requests"][0]["draft_changed_since_sent"])
        self.draft.unlink()
        self.assertTrue(self.kit_status()["requests"][0]["draft_changed_since_sent"])

    def test_missing_law_package_gives_null_dates(self):
        self.assertEqual(self.send()[0], 0)
        with tempfile.TemporaryDirectory(prefix="synthetic-no-law-") as empty:
            report = kit_track.status(self.root, law_base=Path(empty))
        self.assertEqual(report["law_package_status"], "missing")
        [row] = report["requests"]
        self.assertIsNone(row["determination_due"])
        self.assertIsNone(row["with_extension"])
        self.assertTrue(report["note"].startswith("Provisional reminders only;"))

    def test_unreadable_sent_json_stops_and_is_untouched(self):
        good = json.dumps({"schema_version": 1, "requests": {}})
        entry = {"sent_on": "2026-03-02", "channel": "email", "draft": f"requests/{self.agency}.md",
                 "draft_sha256": "0" * 64, "recorded_at": "2026-03-02T00:00:00+00:00", "steps": []}
        cases = {
            "corrupt": "{not json",
            "wrong schema": json.dumps({"schema_version": 2, "requests": {}}),
            "not an object": json.dumps([]),
            "bad channel": json.dumps({"schema_version": 1, "requests": {self.agency: {**entry, "channel": "fax"}}}),
            "bad date": json.dumps({"schema_version": 1, "requests": {self.agency: {**entry, "sent_on": "2026-02-30"}}}),
            "other draft": json.dumps({"schema_version": 1, "requests": {self.agency: {**entry, "draft": "../x.md"}}}),
            "steps missing": json.dumps({"schema_version": 1, "requests": {
                self.agency: {k: v for k, v in entry.items() if k != "steps"}}}),
            "oversize": good + " " * (1 << 20),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.sent.write_text(text)
                before = self.sent.read_bytes()
                self.assert_refused(run("kit-status", "--directory", self.root), "kit/sent.json unreadable")
                self.assert_refused(self.send(), "kit/sent.json unreadable")
                self.assertEqual(self.sent.read_bytes(), before)
        self.sent.unlink()
        target = self.kit / "real-sent.json"
        target.write_text(good)
        self.sent.symlink_to(target)
        self.assert_refused(self.send(), "kit/sent.json unreadable")
        self.assert_refused(run("kit-status", "--directory", self.root), "kit/sent.json unreadable")
        self.assertEqual(target.read_text(), good)
        self.assertTrue(self.sent.is_symlink())

    def test_kit_rerun_leaves_sent_json_byte_identical(self):
        self.assertEqual(self.send()[0], 0)
        before = self.sent.read_bytes()
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent.read_bytes(), before)
        self.assertFalse(json.loads((self.kit / "summary.json").read_text())["sent"])

    def test_record_sent_today_parameter(self):
        with self.assertRaisesRegex(ValueError, "between 2000-01-01 and today"):
            kit_track.record_sent(self.root, self.agency, "2026-03-02", "email", today=date(2026, 3, 1))
        kit_track.record_sent(self.root, self.agency, "2026-03-02", "email", today=date(2026, 3, 2))
        self.assertIn(self.agency, kit_track.read_sent(self.root))


if __name__ == "__main__":
    unittest.main()
