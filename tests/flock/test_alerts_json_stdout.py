"""flock_alerts reminders --json: the flock-alerts-v1 document on stdout, the same bytes --json-out writes (RF1240-21)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.flock import test_alerts as base
from tests.flock.test_alerts import BETA, DATE, ROOT, TOOL, known, unknown

alerts = base.alerts
CITY = "Sample City"
REFUSED_REPO = "alert | refused | store inside repository\n"
LAW = " | law=draft | provisional; not a legal due date | law package is draft: dates unconfirmed\n"
SUMMARY = " | recorded fields only; not a legal status; the canonical ledger wins\n"
# Text output captured from origin/main (88edf506) on the same fixtures, before --json existed.
GOLDEN_MIXED = (
    "alert | remind:sample-alpha-pd:R-P:reminder-passed | reminder-passed | action | reminder date 2026-10-26"
    " | reminder date passed 5 days ago" + LAW
    + "alert | remind:sample-beta-sheriff:B-0:reminder-today | reminder-today | action | reminder date 2026-10-31"
    " | reminder date today" + LAW
    + "alert | remind:sample-alpha-pd:U-1:receipt-unknown | receipt-unknown | action | reminder date -"
    " | receipt unknown, sent 30 days ago" + LAW
    + "alert | open=3 alerts=3 passed=1 today=1 week=0 receipt_unknown=1 waiting=0 law=draft" + SUMMARY)
GOLDEN_NONE = "alert | open=0 alerts=0 passed=0 today=0 week=0 receipt_unknown=0 waiting=0 law=draft" + SUMMARY


def mixed():
    return [known("R-P", -5), unknown("U-1", 30), known("B-0", 0, agency=BETA)]


class JsonStdoutTest(unittest.TestCase):
    setUp = base.AlertsTest.setUp  # synthetic stores built with the test_alerts.py helpers
    write = base.AlertsTest.write

    def run_on(self, *extra, date=DATE, store=None):
        argv = ["reminders", "--store", str(store or self.store), "--date", date, "--receipt-days", "10"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = alerts.main(argv + list(extra))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_document_on_stdout_both_alerts_exit_3_no_lines(self):
        self.write([known("R-P", -5), unknown("U-1", 30)])
        code, out = self.run_on("--json")
        self.assertEqual(code, 3)
        doc = json.loads(out)  # the whole of stdout is one JSON object
        self.assertEqual((doc["schema"], doc["review_required"], doc["as_of"]), ("flock-alerts-v1", True, DATE))
        self.assertEqual([(a["kind"], a["request_id"]) for a in doc["alerts"]],
                         [("reminder-passed", "R-P"), ("receipt-unknown", "U-1")])
        self.assertNotIn("alert |", out)
        self.assertTrue(out.endswith("}\n"))
        self.assertNotIn("since", doc)
        self.assertNotIn("agencies", doc)

    def test_stdout_bytes_equal_json_out_file_and_mode_0600(self):
        self.write(mixed())
        target = self.tmp / "new.json"
        done = subprocess.run([sys.executable, "-B", str(TOOL), "reminders", "--store", str(self.store), "--date", DATE,
                               "--receipt-days", "10", "--json", "--json-out", str(target)],
                              capture_output=True, check=False)
        self.assertEqual((done.returncode, done.stderr), (3, b""))
        self.assertEqual(done.stdout, target.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        other = self.tmp / "other.json"  # --json-out alone writes the same bytes
        self.assertEqual(self.run_on("--json-out", str(other)), (3, GOLDEN_MIXED))
        self.assertEqual(other.read_bytes(), done.stdout)

    def test_prev_since_cleared_and_new_match_json_out(self):
        self.write([known("R-P", -5), unknown("GONE", 30)])
        old = self.tmp / "old.json"
        self.assertEqual(self.run_on("--json-out", str(old), date="2026-10-30")[0], 3)
        self.write([known("R-P", -5), unknown("U-1", 30)])
        ref = self.tmp / "ref.json"
        self.assertEqual(self.run_on("--prev", str(old), "--json-out", str(ref))[0], 3)
        both = self.tmp / "both.json"
        code, out = self.run_on("--json", "--prev", str(old), "--json-out", str(both))
        self.assertEqual(code, 3)
        self.assertEqual(out.encode("utf-8"), ref.read_bytes())
        self.assertEqual(out.encode("utf-8"), both.read_bytes())
        doc = json.loads(out)
        self.assertEqual(doc["since"], "2026-10-30")
        self.assertEqual(doc["cleared"], [{"id": "remind:sample-alpha-pd:GONE:receipt-unknown", "kind": "receipt-unknown"}])
        self.assertEqual({a["request_id"]: a["new"] for a in doc["alerts"]}, {"R-P": False, "U-1": True})
        self.assertNotIn("alert |", out)

    def test_agency_filter_names_agency_and_drops_others(self):
        self.write([known("C-1", -5, agency=CITY), unknown("A-1", 30), known("B-0", 0, agency=BETA)])
        ref = self.tmp / "ref.json"
        self.assertEqual(self.run_on("--agency", "sample-city", "--json-out", str(ref))[0], 3)
        code, out = self.run_on("--json", "--agency", "sample-city")
        self.assertEqual(code, 3)
        self.assertEqual(out.encode("utf-8"), ref.read_bytes())
        doc = json.loads(out)
        self.assertEqual(doc["agencies"], ["sample-city"])
        self.assertEqual([(a["agency"], a["request_id"]) for a in doc["alerts"]], [("sample-city", "C-1")])
        self.assertNotIn("sample-alpha-pd", out)
        self.assertNotIn("sample-beta-sheriff", out)

    def test_refusals_and_store_error_stay_one_plain_line(self):
        self.write(mixed())
        for extra in ([], ["--agency", "sample-nowhere"]):
            with self.subTest(extra=extra):
                text = self.run_on(*extra, store=ROOT / "tests")
                self.assertEqual(text, (1, REFUSED_REPO))
                self.assertEqual(self.run_on("--json", *extra, store=ROOT / "tests"), text)
        unknown_agency = (1, "alert | refused | unknown agency sample-nowhere\n")
        self.assertEqual(self.run_on("--agency", "sample-nowhere"), unknown_agency)
        self.assertEqual(self.run_on("--json", "--agency", "sample-nowhere"), unknown_agency)
        bad = unknown("U-2", 30)
        bad[3] = "received"  # wording without receipt_date + sha is a row error
        self.write([bad])
        self.assertEqual(self.run_on("--json"), (1, "alert | store-error | REQUESTS.csv\n"))
        self.assertEqual(self.run_on("--json", "--date", "2026-13-01")[0], 2)  # argparse usage stays exit 2
        self.assertEqual(self.run_on("--json=yes")[0], 2)

    def test_existing_or_failed_json_out_prints_only_the_refusal(self):
        self.write(mixed())
        taken = self.tmp / "taken.json"
        taken.write_bytes(b"keep")
        self.assertEqual(self.run_on("--json", "--json-out", str(taken)), (1, "alert | refused | json-out already exists\n"))
        self.assertEqual(taken.read_bytes(), b"keep")
        broken = mock.MagicMock()
        broken.return_value.__enter__.return_value.write.side_effect = OSError("disk full")
        failed = self.tmp / "failed.json"
        with mock.patch.object(alerts.os, "fdopen", broken):
            self.assertEqual(self.run_on("--json", "--json-out", str(failed)),
                             (1, "alert | refused | json-out not written (OSError)\n"))
        self.assertFalse(os.path.lexists(failed))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["store", "taken.json"])

    def test_no_open_requests_empty_alerts_exit_0_and_text_goldens(self):
        self.write([known("D-1", -30, status="determined")])
        code, out = self.run_on("--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["alerts"], [])
        self.assertEqual(self.run_on(), (0, GOLDEN_NONE))  # text mode byte-identical to origin/main
        self.write(mixed())
        self.assertEqual(self.run_on(), (3, GOLDEN_MIXED))
        self.assertEqual(sorted(p.name for p in self.store.rglob("*") if p.is_file()), ["REQUESTS.csv"] * 2)

    def test_json_writes_no_file(self):
        self.write(mixed())
        with tempfile.TemporaryDirectory() as cwd, contextlib.chdir(cwd):
            self.assertEqual(self.run_on("--json")[0], 3)
            self.assertEqual(os.listdir(cwd), [])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["store"])


if __name__ == "__main__":
    unittest.main()
