"""flock_alerts reminders --agency SLUG: alerts, counts and flock-alerts-v1 scoped to the chosen agencies (RF0510-11)."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.flock import test_alerts as base
from tests.flock.test_alerts import ALPHA, BETA, DATE, known, unknown

alerts = base.alerts
A, B = "sample-alpha-pd", "sample-beta-sheriff"
TAIL = " | law=draft | provisional; not a legal due date | law package is draft: dates unconfirmed"
SUMMARY_TAIL = " | recorded fields only; not a legal status; the canonical ledger wins"
GOLDEN_LINES = [
    "alert | remind:sample-alpha-pd:R-P:reminder-passed | reminder-passed | action | reminder date 2026-10-26 | "
    "reminder date passed 5 days ago" + TAIL,
    "alert | remind:sample-beta-sheriff:U-1:receipt-unknown | receipt-unknown | action | reminder date - | "
    "receipt unknown, sent 30 days ago" + TAIL,
    "alert | open=2 alerts=2 passed=1 today=0 week=0 receipt_unknown=1 waiting=0 law=draft" + SUMMARY_TAIL]
GOLDEN_JSON = """{
  "alerts": [
    {
      "agency": "sample-alpha-pd",
      "id": "remind:sample-alpha-pd:R-P:reminder-passed",
      "kind": "reminder-passed",
      "phrase": "reminder date passed 5 days ago",
      "reminder_date": "2026-10-26",
      "request_id": "R-P",
      "severity": "action",
      "source": "request-reminders"
    },
    {
      "agency": "sample-beta-sheriff",
      "id": "remind:sample-beta-sheriff:U-1:receipt-unknown",
      "kind": "receipt-unknown",
      "phrase": "receipt unknown, sent 30 days ago",
      "reminder_date": null,
      "request_id": "U-1",
      "severity": "action",
      "source": "request-reminders"
    }
  ],
  "as_of": "2026-10-31",
  "law_note": "law package is draft: dates unconfirmed",
  "law_status": "draft",
  "review_required": true,
  "schema": "flock-alerts-v1"
}
"""


class AgencyTest(unittest.TestCase):
    setUp = base.AlertsTest.setUp  # synthetic stores built with the test_alerts.py helpers
    write = base.AlertsTest.write

    def run_on(self, *extra, days="10", date=DATE):
        argv = ["reminders", "--store", str(self.store), "--date", date, "--receipt-days", days] + list(extra)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = alerts.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue().splitlines()

    def two_agencies(self):  # a: one passed reminder, one open row with no alert, one waiting; b: receipt unknown
        self.write([known("R-P", -5), known("R-10", 10), unknown("U-A", 3),
                    unknown("U-1", 30, agency=BETA), unknown("U-2", 5, agency=BETA), known("D-1", -9, BETA, "determined")])

    def test_one_agency_scopes_lines_and_counts(self):
        self.two_agencies()
        code, lines = self.run_on("--agency", A)
        self.assertEqual(code, 3)
        self.assertEqual(lines, [GOLDEN_LINES[0], "alert | open=3 alerts=1 passed=1 today=0 week=0 receipt_unknown=0 "
                                 "waiting=1 agencies=%s law=draft%s" % (A, SUMMARY_TAIL)])
        code, lines = self.run_on("--agency", B, days="30")
        self.assertEqual(code, 3)
        self.assertEqual(lines, [GOLDEN_LINES[1], "alert | open=2 alerts=1 passed=0 today=0 week=0 receipt_unknown=1 "
                                 "waiting=1 agencies=%s law=draft%s" % (B, SUMMARY_TAIL)])

    def test_both_agencies_same_lines_plus_agencies_in_summary(self):
        self.two_agencies()
        plain = self.run_on()
        for order in ((A, B), (B, A)):
            code, lines = self.run_on("--agency", order[0], "--agency", order[1])
            self.assertEqual(code, plain[0])
            self.assertEqual(lines[:-1], plain[1][:-1])
            self.assertEqual(lines[-1], plain[1][-1].replace(" law=draft", " agencies=%s,%s law=draft" % (A, B)))

    def test_no_agency_stdout_and_json_identical_to_main(self):
        self.write([known("R-P", -5), unknown("U-1", 30, agency=BETA)])
        out = self.tmp / "plain.json"
        self.assertEqual(self.run_on("--json-out", str(out)), (3, GOLDEN_LINES))
        self.assertEqual(out.read_text(encoding="utf-8"), GOLDEN_JSON)
        self.assertNotIn("agencies", out.read_text(encoding="utf-8"))

    def test_malformed_repeated_or_too_many_slugs_are_usage_errors(self):
        self.two_agencies()
        many = [x for i in range(21) for x in ("--agency", "ag-%d" % i)]
        cases = {"case": ["--agency", "Bad_Slug"], "repeat": ["--agency", A, "--agency", A], "21": many,
                 "dash-edge": ["--agency", "-a"], "double-dash": ["--agency", "a--b"], "empty": ["--agency", ""],
                 "traversal": ["--agency", "../" + A], "trailing-newline": ["--agency", A + "\n"]}
        for name, extra in cases.items():
            with self.subTest(name):
                self.assertEqual(self.run_on(*extra), (2, []))
        many20 = many[:40]
        self.assertEqual(self.run_on(*many20), (1, ["alert | refused | unknown agency ag-0"]))  # 20 is allowed

    def test_unknown_agency_refused_before_store_read_and_no_json(self):
        self.two_agencies()
        (self.tmp / "real").mkdir()
        (self.store / "agencies" / "zz-link").symlink_to(self.tmp / "real")
        (self.store / "agencies" / "zz-file").write_text("x", encoding="utf-8")
        (self.store / "agencies" / "zz-alpha").symlink_to(self.store / "agencies" / A)
        out = self.tmp / "a.json"
        for slug in ("zz", "zz-link", "zz-file", "zz-alpha"):
            with self.subTest(slug), mock.patch.object(alerts.requests_tool, "load", side_effect=AssertionError("read")):
                self.assertEqual(self.run_on("--agency", A, "--agency", slug, "--json-out", str(out)),
                                 (1, ["alert | refused | unknown agency %s" % slug]))
                self.assertFalse(os.path.lexists(out))
        out.write_text("{}", encoding="utf-8")  # refused first: before the json-out and prev checks
        self.assertEqual(self.run_on("--agency", "zz", "--json-out", str(out), "--prev", "rel.json"),
                         (1, ["alert | refused | unknown agency zz"]))

    def test_json_out_scoped_with_agencies_and_mode(self):
        self.two_agencies()
        out = self.tmp / "a.json"
        self.assertEqual(self.run_on("--agency", A, "--json-out", str(out))[0], 3)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["agencies"], [A])
        self.assertEqual([x["id"] for x in doc["alerts"]], ["remind:sample-alpha-pd:R-P:reminder-passed"])
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        both = self.tmp / "b.json"
        self.assertEqual(self.run_on("--agency", B, "--agency", A, "--json-out", str(both))[0], 3)
        self.assertEqual(json.loads(both.read_text(encoding="utf-8"))["agencies"], [A, B])

    def test_filtered_agency_without_alerts_exits_zero(self):
        self.write([known("R-P", -5), known("R-10", 10, agency=BETA)])
        self.assertEqual(self.run_on("--agency", B), (0, [
            "alert | open=1 alerts=0 passed=0 today=0 week=0 receipt_unknown=0 waiting=0 agencies=%s law=draft%s"
            % (B, SUMMARY_TAIL)]))
        self.write([known("R-P", -5, status="determined"), known("R-10", 10, agency=BETA)])
        code, lines = self.run_on("--agency", A)
        self.assertEqual((code, lines[-1][:23]), (0, "alert | open=0 alerts=0"))

    def test_prev_compares_only_chosen_agencies(self):
        self.write([known("R-P", -5), known("R-Q", -2), unknown("U-1", 30, agency=BETA)])
        old = self.tmp / "old.json"
        self.assertEqual(self.run_on("--json-out", str(old), date="2026-10-30")[0], 3)
        self.write([known("R-P", -5), known("R-Q", -2, status="determined"), known("D-1", -9, BETA, "determined")])
        code, lines = self.run_on("--agency", A, "--prev", str(old))  # b's U-1 closed too, but b is not chosen
        self.assertEqual(code, 3)
        self.assertEqual(lines[:-1], [GOLDEN_LINES[0] + " | still",
                                      "alert | cleared | remind:sample-alpha-pd:R-Q:reminder-passed | reminder-passed"])
        self.assertIn(" new=0 cleared=1 since=2026-10-30 ", lines[-1])
        self.assertNotIn(B, "\n".join(lines[:-1]))


if __name__ == "__main__":
    unittest.main()
