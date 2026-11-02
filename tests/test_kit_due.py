"""kit-due lists open hand-sent requests by provisional determination date; exit 3 when any is past. Offline, synthetic."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track, law  # noqa: E402


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


class KitDueTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-due-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.sent = self.root / "kit" / "sent.json"
        drafts = sorted((self.root / "kit" / "requests").glob("*.md"))
        self.assertGreaterEqual(len(drafts), 3)
        self.a, self.b, self.c = (path.stem for path in drafts[:3])
        self.package = law.load_package("us-ca")

    def send(self, agency, on):
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", agency, "--date", on, "--channel", "email")
        self.assertEqual(code, 0, err)

    def step(self, agency, on, kind):
        code, _, err = run("kit-response", "--directory", self.root, "--agency", agency, "--date", on, "--kind", kind)
        self.assertEqual(code, 0, err)

    def due(self, *extra, expect=0):
        code, out, err = run("kit-due", "--directory", self.root, *extra)
        self.assertEqual(code, expect, err)
        return json.loads(out)

    def stopped(self, result, words):
        code, out, err = result
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(words, err)

    def deadline(self, sent_on, extension=False):
        return law.deadline(self.package, sent_on, extension=extension).isoformat()

    def three_sent(self):
        # id order a < b < c, due order c < b < a: the list must follow due dates.
        self.send(self.c, "2026-03-02")
        self.send(self.b, "2026-03-05")
        self.send(self.a, "2026-03-10")
        self.assertEqual(self.deadline("2026-03-05"), "2026-03-15")

    def test_past_due_today_upcoming_sorted_by_due_and_exit_3(self):
        self.three_sent()
        report = self.due("--as-of", "2026-03-15", expect=3)
        self.assertEqual(report["as_of"], "2026-03-15")
        self.assertEqual(report["law_package_status"], self.package["status"])
        self.assertEqual([row["agency_id"] for row in report["open"]], [self.c, self.b, self.a])
        self.assertEqual([row["state"] for row in report["open"]], ["past_due", "due_today", "upcoming"])
        self.assertEqual([row["days"] for row in report["open"]], [-3, 0, 5])
        self.assertEqual([row["due"] for row in report["open"]],
                         [self.deadline(d) for d in ("2026-03-02", "2026-03-05", "2026-03-10")])
        self.assertEqual(report["open"][0]["sent_on"], "2026-03-02")
        self.assertTrue(report["open"][0]["name"])
        self.assertEqual(report["past_due"], 1)
        self.assertEqual(report["answered"], [])
        self.assertEqual(report["note"], kit_track.NOTE)
        self.assertIn(law.HOLIDAY_NOTE, report["note"])
        # The day before nothing is past: exit 0.
        report = self.due("--as-of", "2026-03-12")
        self.assertEqual([row["state"] for row in report["open"]], ["due_today", "upcoming", "upcoming"])
        self.assertEqual(report["past_due"], 0)

    def test_extension_moves_due_and_can_turn_past_due_into_upcoming(self):
        self.send(self.a, "2026-03-02")
        self.assertEqual(self.due("--as-of", "2026-03-20", expect=3)["open"][0]["state"], "past_due")
        self.step(self.a, "2026-03-04", "acknowledged")
        self.assertEqual(self.due("--as-of", "2026-03-20", expect=3)["past_due"], 1)  # acknowledging is no answer
        self.step(self.a, "2026-03-11", "extension")
        [row] = self.due("--as-of", "2026-03-20")["open"]
        self.assertEqual(row["due"], self.deadline("2026-03-02", extension=True))
        self.assertEqual((row["days"], row["state"]), (6, "upcoming"))
        self.assertEqual(self.due("--as-of", "2026-04-01", expect=3)["open"][0]["days"], -6)

    def test_determination_produced_or_denied_is_answered_never_past_due(self):
        for agency, kind in ((self.a, "produced"), (self.b, "denied"), (self.c, "determination")):
            self.send(agency, "2026-03-02")
            self.step(agency, "2026-03-20", kind)
        report = self.due("--as-of", "2026-06-01")
        self.assertEqual((report["open"], report["past_due"]), ([], 0))
        self.assertEqual({row["agency_id"]: row["last_step"] for row in report["answered"]},
                         {self.a: {"on": "2026-03-20", "kind": "produced"},
                          self.b: {"on": "2026-03-20", "kind": "denied"},
                          self.c: {"on": "2026-03-20", "kind": "determination"}})
        # A later step after the answer still leaves it answered.
        self.step(self.c, "2026-03-25", "produced")
        report = self.due("--as-of", "2026-06-01")
        self.assertEqual(len(report["answered"]), 3)
        self.assertEqual(report["past_due"], 0)

    def test_closed_request_absent_from_both_lists(self):
        self.send(self.a, "2026-03-02")
        self.send(self.b, "2026-03-02")
        self.step(self.a, "2026-03-03", "acknowledged")
        self.step(self.a, "2026-03-04", "closed")
        self.step(self.b, "2026-03-05", "produced")
        self.step(self.b, "2026-03-06", "closed")
        report = self.due("--as-of", "2026-06-01")
        self.assertEqual((report["open"], report["answered"], report["past_due"]), ([], [], 0))

    def test_business_day_package_uses_law_deadline(self):
        base = self.tmp / "law"
        (base / "us-ca").mkdir(parents=True)
        package = json.loads((law.package_path("us-ca")).read_text(encoding="utf-8"))
        package["records_law"]["day_type"] = "business"
        (base / "us-ca" / "package.json").write_text(json.dumps(package), encoding="utf-8")
        business = law.load_package("us-ca", base)
        friday = "2026-03-06"
        self.assertEqual(date.fromisoformat(friday).weekday(), 4)
        self.send(self.a, friday)
        report = kit_track.due(self.root, "2026-03-16", law_base=base)
        [row] = report["open"]
        self.assertEqual(row["due"], law.deadline(business, friday).isoformat())
        self.assertNotEqual(row["due"], self.deadline(friday))
        self.assertEqual(row["days"], (law.deadline(business, friday) - date(2026, 3, 16)).days)

    def test_missing_law_package_gives_no_law_package_and_exit_0(self):
        self.send(self.a, "2026-03-02")
        self.send(self.b, "2026-03-03")
        self.send(self.c, "2026-03-04")
        self.step(self.c, "2026-03-05", "produced")
        empty = self.tmp / "no-law"
        empty.mkdir()
        with mock.patch.object(law, "JURISDICTIONS", empty):
            report = self.due("--as-of", "2027-01-01")
        self.assertEqual(report["law_package_status"], "missing")
        self.assertEqual([row["agency_id"] for row in report["open"]], [self.a, self.b])
        for row in report["open"]:
            self.assertEqual((row["due"], row["days"], row["state"]), (None, None, "no_law_package"))
        self.assertEqual([row["agency_id"] for row in report["answered"]], [self.c])
        self.assertEqual(report["past_due"], 0)
        self.assertIn("cannot be computed", report["note"])
        self.assertTrue(report["note"].endswith(kit_track.NOTE))

    def test_no_sent_json_is_empty_and_corrupt_stops(self):
        report = self.due("--as-of", "2026-03-15")
        self.assertEqual((report["open"], report["answered"], report["past_due"]), ([], [], 0))
        for text in ("{not json", json.dumps({"schema_version": 2, "requests": {}})):
            with self.subTest(text=text):
                self.sent.write_text(text)
                self.stopped(run("kit-due", "--directory", self.root, "--as-of", "2026-03-15"),
                             "kit/sent.json unreadable")
                self.assertEqual(self.sent.read_text(), text)

    def test_invalid_as_of_stops_and_default_is_today(self):
        self.send(self.a, "2026-03-02")
        for bad in ("2026-13-01", "2026-02-30", "2026-3-1", "today", ""):
            with self.subTest(bad=bad):
                self.stopped(run("kit-due", "--directory", self.root, "--as-of", bad), "--as-of must be a real")
        report = kit_track.due(self.root, today=date(2026, 3, 13))
        self.assertEqual(report["as_of"], "2026-03-13")
        self.assertEqual((report["open"][0]["days"], report["past_due"]), (-1, 1))
        self.assertEqual(report, kit_track.due(self.root, "2026-03-13"))
        before = date.today().isoformat()
        code, out, err = run("kit-due", "--directory", self.root)
        self.assertIn(json.loads(out)["as_of"], {before, date.today().isoformat()})
        self.assertEqual(code, 3 if json.loads(out)["past_due"] else 0, err)


if __name__ == "__main__":
    unittest.main()
