"""kit-response records dated agency steps on a hand-sent request; impossible histories are refused. Offline, synthetic."""
import contextlib
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
from campaign_tool import cli, kit_track, law  # noqa: E402

SENT_ON = "2026-03-02"


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


class KitResponseTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-response-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.sent = self.root / "kit" / "sent.json"
        drafts = sorted((self.root / "kit" / "requests").glob("*.md"))
        self.agency, self.other = drafts[0].stem, drafts[1].stem

    def send(self, agency=None):
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", agency or self.agency,
                           "--date", SENT_ON, "--channel", "email")
        self.assertEqual(code, 0, err)

    def step(self, on, kind, *extra, agency=None):
        return run("kit-response", "--directory", self.root, "--agency", agency or self.agency, "--date", on,
                   "--kind", kind, *extra)

    def ok(self, on, kind, *extra):
        code, out, err = self.step(on, kind, *extra)
        self.assertEqual(code, 0, err)
        self.assertIn("Recorded only. Nothing was sent.", out)
        return json.loads(out.split("Recorded only.")[0])

    def refused(self, result, *words):
        """Assert a Stopped: refusal naming the rule and an unchanged sent.json."""
        code, out, err = result
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)

    def refused_untouched(self, on, kind, *words, extra=()):
        before = self.sent.read_bytes()
        self.refused(self.step(on, kind, *extra), *words)
        self.assertEqual(self.sent.read_bytes(), before)

    def row(self):
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        return {row["agency_id"]: row for row in json.loads(out)["requests"]}[self.agency]

    def test_full_history_accepted_and_status_shows_extension_and_closed(self):
        self.send()
        history = [("2026-03-05", "acknowledged"), ("2026-03-10", "extension"), ("2026-03-20", "produced"),
                   ("2026-03-27", "produced"), ("2026-04-01", "closed")]
        for on, kind in history:
            printed = self.ok(on, kind)
        self.assertEqual([(s["on"], s["kind"]) for s in printed["steps"]], history)
        self.assertEqual((printed["agency_id"], printed["sent_on"]), (self.agency, SENT_ON))
        stored = json.loads(self.sent.read_text())["requests"][self.agency]["steps"]
        self.assertEqual([(s["on"], s["kind"]) for s in stored], history)
        self.assertTrue(all(kit_track.STAMP.fullmatch(s["recorded_at"]) and "note" not in s for s in stored))
        row = self.row()
        package = law.load_package("us-ca")
        extended = law.deadline(package, date(2026, 3, 2), extension=True).isoformat()
        self.assertEqual((row["extended"], row["closed"], row["steps"]), (True, True, 5))
        self.assertEqual(row["last_step"], {"on": "2026-04-01", "kind": "closed"})
        self.assertEqual(row["determination_due"], extended)
        self.assertEqual(row["with_extension"], extended)

    def test_step_free_entry_reports_unextended_and_open(self):
        self.send()
        row = self.row()
        package = law.load_package("us-ca")
        self.assertEqual((row["extended"], row["closed"], row["last_step"], row["steps"]), (False, False, None, 0))
        self.assertEqual(row["determination_due"], law.deadline(package, date(2026, 3, 2)).isoformat())
        self.ok("2026-03-04", "acknowledged")
        row = self.row()
        self.assertEqual((row["extended"], row["closed"]), (False, False))
        self.assertEqual(row["last_step"], {"on": "2026-03-04", "kind": "acknowledged"})
        self.assertEqual(row["determination_due"], law.deadline(package, date(2026, 3, 2)).isoformat())

    def test_step_before_kit_sent_refused_and_no_file_created(self):
        self.refused(self.step("2026-03-05", "acknowledged"), "no hand-sent request recorded for", self.agency,
                     "run kit-sent first")
        self.assertFalse(self.sent.exists())
        self.send()
        before = self.sent.read_bytes()
        self.refused(self.step("2026-03-05", "acknowledged", agency=self.other), "run kit-sent first", self.other)
        self.assertEqual(self.sent.read_bytes(), before)

    def test_bad_dates_refused_file_byte_identical(self):
        self.send()
        self.ok("2026-03-10", "acknowledged")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        cases = {"2026-03-01": "before the request was sent", "2026-03-09": "before the previous step",
                 tomorrow: "in the future", "2026-02-30": "real YYYY-MM-DD", "2026-3-11": "real YYYY-MM-DD",
                 "soon": "real YYYY-MM-DD"}
        for value, words in cases.items():
            with self.subTest(value=value):
                self.refused_untouched(value, "produced", words)
        self.ok(date.today().isoformat(), "produced")

    def test_first_step_before_sent_on_refused(self):
        self.send()
        self.refused_untouched("2026-03-01", "acknowledged", "before the request was sent on 2026-03-02")

    def test_acknowledged_and_extension_order_rules(self):
        self.send()
        self.ok("2026-03-05", "produced")
        self.refused_untouched("2026-03-06", "acknowledged", "acknowledged is allowed only as the first step")
        self.refused_untouched("2026-03-06", "extension", "not allowed after a determination, produced or denied")
        self.send(self.other)
        for kind in ("acknowledged", "extension"):
            code, _, err = self.step("2026-03-05", kind, agency=self.other)
            self.assertEqual(code, 0, err)
        before = self.sent.read_bytes()
        self.refused(self.step("2026-03-06", "acknowledged", agency=self.other), "only as the first step")
        self.refused(self.step("2026-03-06", "extension", agency=self.other), "extension is allowed only once")
        self.assertEqual(self.sent.read_bytes(), before)

    def test_extension_after_determination_or_denied_refused(self):
        self.send()
        self.ok("2026-03-05", "determination")
        self.refused_untouched("2026-03-06", "extension", "not allowed after a determination")
        self.send(self.other)
        code, _, err = self.step("2026-03-05", "denied", agency=self.other)
        self.assertEqual(code, 0, err)
        before = self.sent.read_bytes()
        self.refused(self.step("2026-03-06", "extension", agency=self.other), "not allowed after")
        self.assertEqual(self.sent.read_bytes(), before)

    def test_nothing_after_closed_and_closed_not_first(self):
        self.send()
        self.refused_untouched("2026-03-05", "closed", "closed needs at least one prior step")
        self.assertEqual(json.loads(self.sent.read_text())["requests"][self.agency]["steps"], [])
        self.ok("2026-03-05", "denied")
        self.ok("2026-03-06", "closed")
        for kind in kit_track.KINDS:
            with self.subTest(kind=kind):
                self.refused_untouched("2026-03-07", kind, "no step can follow closed")

    def test_determination_and_denied_once_produced_repeats(self):
        self.send()
        self.ok("2026-03-05", "determination")
        self.refused_untouched("2026-03-06", "determination", "determination is allowed at most once")
        self.ok("2026-03-06", "denied")
        self.refused_untouched("2026-03-07", "denied", "denied is allowed at most once")
        for day in ("2026-03-08", "2026-03-08", "2026-03-09"):
            printed = self.ok(day, "produced")
        self.assertEqual([s["kind"] for s in printed["steps"]].count("produced"), 3)

    def test_same_day_steps_in_order_accepted(self):
        self.send()
        self.ok(SENT_ON, "acknowledged")
        self.ok(SENT_ON, "determination")
        printed = self.ok(SENT_ON, "closed")
        self.assertEqual([s["on"] for s in printed["steps"]], [SENT_ON] * 3)
        self.assertTrue(self.row()["closed"])

    def test_note_rules(self):
        self.send()
        for note, words in (("see <b>letter</b>", "plain text"), ("bell\x07", "control characters"),
                            ("line\nbreak", "control characters"), ("x" * 201, "1-200 characters"),
                            ("   ", "1-200 characters")):
            with self.subTest(note=note[:12]):
                self.refused_untouched("2026-03-05", "acknowledged", words, extra=("--note", note))
        printed = self.ok("2026-03-05", "acknowledged", "--note", "  emailed   the   clerk  ")
        self.assertEqual(printed["steps"][-1]["note"], "emailed the clerk")
        self.assertEqual(json.loads(self.sent.read_text())["requests"][self.agency]["steps"][-1]["note"],
                         "emailed the clerk")
        printed = self.ok("2026-03-06", "produced", "--note", "y" * 200)
        self.assertEqual(len(printed["steps"][-1]["note"]), 200)

    def test_unknown_kind_is_argparse_error(self):
        self.send()
        before = self.sent.read_bytes()
        code, _, err = self.step("2026-03-05", "other")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)
        self.assertEqual(self.sent.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "--kind must be one of"):
            kit_track.record_step(self.root, self.agency, "2026-03-05", "other")
        self.assertEqual(self.sent.read_bytes(), before)

    def test_corrupt_or_malformed_sent_json_stops_untouched(self):
        self.send()
        entry = json.loads(self.sent.read_text())["requests"][self.agency]
        cases = {"corrupt": "{not json",
                 "bad step kind": json.dumps({"schema_version": 1, "requests": {
                     self.agency: {**entry, "steps": [{"on": "2026-03-05", "kind": "other"}]}}}),
                 "bad step date": json.dumps({"schema_version": 1, "requests": {
                     self.agency: {**entry, "steps": [{"on": "2026-02-30", "kind": "produced"}]}}})}
        for label, text in cases.items():
            with self.subTest(label=label):
                self.sent.write_text(text)
                self.refused_untouched("2026-03-06", "produced", "kit/sent.json unreadable")

    def test_today_parameter_bounds_the_date(self):
        self.send()
        with self.assertRaisesRegex(ValueError, "in the future"):
            kit_track.record_step(self.root, self.agency, "2026-03-05", "acknowledged", today=date(2026, 3, 4))
        entry = kit_track.record_step(self.root, self.agency, "2026-03-05", "acknowledged", today=date(2026, 3, 5))
        self.assertEqual(entry["steps"][-1]["kind"], "acknowledged")


if __name__ == "__main__":
    unittest.main()
