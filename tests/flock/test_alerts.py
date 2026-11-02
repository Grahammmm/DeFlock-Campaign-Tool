"""flock_alerts reminders: provisional reminder and receipt-unknown alerts as stdout and flock-alerts-v1 (BF-df-overdue-alerts)."""
import ast
import contextlib
import csv
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_alerts.py"
PACKAGE = ROOT / "jurisdictions" / "us-ca" / "package.json"
DATE = "2026-10-31"
AS_OF = dt.date.fromisoformat(DATE)
SHA = hashlib.sha256(b"synthetic alerts doc").hexdigest()
BANNED = re.compile(r"\b(late|overdue)\b|violat|unlawful|illegal|non-?complian|breach", re.I)
HEADER = ["request_id", "agency", "sent", "receipt_evidence", "receipt_date", "determination_date",
          "extension_notice_date", "status", "source_sha256"]
ALPHA, BETA = "Sample Alpha PD", "Sample Beta Sheriff"


def load():
    spec = importlib.util.spec_from_file_location("_alerts_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


alerts = load()
requests, law = alerts.requests_tool, alerts.law
PKG = law.load_package("us-ca")


def receipt_for(offset, extension=False):  # receipt date whose provisional reminder date is AS_OF + offset
    target = AS_OF + dt.timedelta(days=offset)
    return next(r for r in (target - dt.timedelta(days=b) for b in range(60))
                if law.deadline(PKG, r.isoformat(), extension=extension) == target)


def known(rid, offset, agency=ALPHA, status="open", evidence="receipt email", ext=False):
    receipt = receipt_for(offset, ext)
    det = (receipt + dt.timedelta(days=1)).isoformat() if status == "determined" else ""
    notice = (receipt + dt.timedelta(days=1)).isoformat() if ext else ""
    return [rid, agency, (receipt - dt.timedelta(days=1)).isoformat(), evidence, receipt.isoformat(), det, notice,
            status, SHA]


def unknown(rid, sent_days_ago, agency=ALPHA, status="open"):
    return [rid, agency, (AS_OF - dt.timedelta(days=sent_days_ago)).isoformat(), "", "", "", "", status, ""]


class AlertsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"
        self.store.mkdir()

    def write(self, rows, header=HEADER):
        groups = {}
        for row in rows:
            groups.setdefault(requests.slug(row[1]), []).append(row)
        for slug, rs in groups.items():
            folder = self.store / "agencies" / slug
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / "REQUESTS.csv", "w", encoding="utf-8", newline="") as fh:
                csv.writer(fh).writerows([header] + rs)

    def run_tool(self, *extra, days="10", store=None):
        argv = ["reminders", "--store", str(store or self.store), "--date", DATE] + (["--receipt-days", days] if days else [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = alerts.main(argv + list(extra))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue().splitlines()

    def alert_lines(self, lines):
        return [ln for ln in lines if not ln.startswith("alert | open=")]

    def kinds(self, lines):
        return {ln.split(" | ")[1]: ln.split(" | ")[2] for ln in self.alert_lines(lines)}

    def test_reminder_windows_today_week_far_passed(self):
        self.write([known("R-0", 0), known("R-3", 3), known("R-10", 10), known("R-P", -5)])
        code, lines = self.run_tool()
        self.assertEqual(code, 3)
        self.assertEqual(self.kinds(lines), {"remind:sample-alpha-pd:R-P:reminder-passed": "reminder-passed",
                                             "remind:sample-alpha-pd:R-0:reminder-today": "reminder-today",
                                             "remind:sample-alpha-pd:R-3:reminder-week": "reminder-week"})
        passed, today, week = self.alert_lines(lines)
        self.assertIn(" | action | reminder date %s | reminder date passed 5 days ago | law=" % (AS_OF - dt.timedelta(5)), passed)
        self.assertIn(" | action | reminder date %s | reminder date today | " % DATE, today)
        self.assertIn(" | info | reminder date %s | in 3 days | " % (AS_OF + dt.timedelta(3)), week)
        self.assertTrue(lines[-1].startswith("alert | open=4 alerts=3 passed=1 today=1 week=1 receipt_unknown=0 waiting=0 law=draft | "))
        self.write([known("R-3", 3), known("R-10", 10)])
        code, lines = self.run_tool()
        self.assertEqual((code, len(self.alert_lines(lines))), (0, 1))  # info only: exit 0

    def test_receipt_unknown_threshold_and_future_sent(self):
        self.write([unknown("U-9", 9), unknown("U-10", 10), unknown("U-F", -2)])
        code, lines = self.run_tool()
        self.assertEqual(code, 3)
        self.assertEqual(self.alert_lines(lines), [
            "alert | remind:sample-alpha-pd:U-10:receipt-unknown | receipt-unknown | action | reminder date - | "
            "receipt unknown, sent 10 days ago | law=draft | provisional; not a legal due date | law package is draft: dates unconfirmed"])
        self.assertIn("receipt_unknown=1 waiting=2 ", lines[-1])
        self.write([unknown("U-9", 9)])
        self.assertEqual(self.run_tool()[0], 0)

    def test_receipt_days_required_and_bounded(self):
        self.write([unknown("U-10", 10)])
        for days in (None, "0", "366", "abc", "-1", "1.5"):
            self.assertEqual(self.run_tool(days=days)[0], 2, days)
        self.assertEqual(self.run_tool("--date", "2026-13-01")[0], 2)
        self.assertEqual(self.run_tool(days="1")[0], 3)
        self.assertEqual(self.run_tool(days="365")[0], 0)

    def test_closed_statuses_never_alert_and_flip_clears(self):
        rows = [known("D-1", -30, status="determined")] + [unknown(s + "-1", 300, status=s)
                                                           for s in ("produced", "closed", "withdrawn")]
        self.write(rows + [known("O-1", -3)])
        code, lines = self.run_tool()
        self.assertEqual(list(self.kinds(lines)), ["remind:sample-alpha-pd:O-1:reminder-passed"])
        self.write(rows + [known("O-1", -3, status="determined")])
        code, lines = self.run_tool()
        self.assertEqual((code, self.alert_lines(lines)), (0, []))
        self.assertIn("open=0 alerts=0 ", lines[-1])
        self.assertEqual(sorted(p.name for p in self.store.rglob("*") if p.is_file()), ["REQUESTS.csv"])  # no snooze state

    def test_400_days_passed_still_alerts(self):
        self.write([known("OLD-1", -400)])
        code, lines = self.run_tool()
        self.assertEqual(code, 3)
        self.assertIn(" | reminder date passed 400 days ago | ", lines[0])

    def test_invalid_store_fails_closed_without_json(self):
        good = known("G-1", -3)
        bad_date = unknown("B-1", 3)
        bad_date[2] = "2026-02-30"
        before = unknown("B-2", 30)
        before[3:5], before[8] = ["receipt email", (AS_OF - dt.timedelta(40)).isoformat()], SHA
        cases = {"duplicate": ([good, list(good)], HEADER), "receipt-before-sent": ([good, before], HEADER),
                 "bad-date": ([good, bad_date], HEADER), "header": ([good], HEADER[:-1] + ["sha256"])}
        for name, (rows, header) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.write(rows, header)
                out = self.tmp / "alerts.json"
                self.assertEqual(self.run_tool("--json-out", str(out)), (1, ["alert | store-error | REQUESTS.csv"]))
                self.assertFalse(os.path.lexists(out))
        self.setUp()
        self.assertEqual(self.run_tool(), (1, ["alert | store-error | REQUESTS.csv"]))  # empty store
        self.assertEqual(self.run_tool(store=ROOT / "tests")[0], 1)
        self.assertEqual(self.run_tool(store=ROOT / "tests")[1], ["alert | refused | store inside repository"])

    def test_extension_uses_due_basis(self):
        row = known("E-1", 2, ext=True)
        self.write([row])
        _, lines = self.run_tool()
        expected = re.search(r"\| determination (\S+) \|", requests.due(requests.load(self.store)[0], AS_OF)[0])[1]
        self.assertIn("+ 14 extension notice", requests.due(requests.load(self.store)[0], AS_OF)[0])
        self.assertIn(" | reminder date %s | in 2 days | " % expected, lines[0])

    def test_draft_law_note_on_every_line_and_json(self):
        pinned = hashlib.sha256(PACKAGE.read_bytes()).hexdigest()
        self.write([known("R-0", 0), known("R-3", 3), unknown("U-20", 20)])
        out = self.tmp / "a.json"
        code, lines = self.run_tool("--json-out", str(out))
        self.assertEqual(len(self.alert_lines(lines)), 3)
        for ln in self.alert_lines(lines):
            self.assertTrue(ln.endswith(" | law=draft | provisional; not a legal due date | law package is draft: dates unconfirmed"), ln)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((doc["law_status"], doc["law_note"]), ("draft", "law package is draft: dates unconfirmed"))
        self.assertEqual(hashlib.sha256(PACKAGE.read_bytes()).hexdigest(), pinned)

    def test_wording_in_evidence_changes_nothing_and_banned_words(self):
        self.write([known("R-1", -2, evidence="x"), unknown("U-1", 30)])
        plain = self.run_tool()
        self.write([known("R-1", -2, evidence="received; no response; overdue"), unknown("U-1", 30)])
        self.assertEqual(self.run_tool(), plain)
        claimed = unknown("U-2", 30)
        claimed[3] = "received"  # wording without receipt_date + sha is still a row error
        self.write([claimed])
        self.assertEqual(self.run_tool(), (1, ["alert | store-error | REQUESTS.csv"]))
        self.write([known("R-0", 0), known("R-3", 3), known("R-P", -9), unknown("U-1", 30), unknown("U-2", 2)])
        _, lines = self.run_tool()
        for ln in lines:
            generated = ln.replace("sample-alpha-pd", "").replace("Sample Alpha PD", "")
            self.assertIsNone(BANNED.search(re.sub(r"R-\w+|U-\w+", "", generated)), ln)
        self.setUp()
        self.write([known("Overdue-REQ-1", -1, agency="Late Town Police")])
        _, lines = self.run_tool()
        self.assertTrue(lines[0].startswith("alert | remind:late-town-police:Overdue-REQ-1:reminder-passed | "))

    def json_ok(self, rows, path):
        self.write(rows)
        return self.run_tool("--json-out", str(path))

    def test_json_out_refusals_mode_and_identical_bytes(self):
        rows = [known("R-0", 0), unknown("U-1", 30)]
        a, b = self.tmp / "a.json", self.tmp / "b.json"
        self.assertEqual(self.json_ok(rows, a)[0], 3)
        self.assertEqual(self.json_ok(rows, b)[0], 3)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(a.stat().st_mode & 0o777, 0o600)
        (self.tmp / "real").mkdir()
        (self.tmp / "link").symlink_to(self.tmp / "real")
        refusals = {str(a): "json-out already exists", "rel.json": "json-out must be an absolute path",
                    str(ROOT / "tests" / "x-alerts.json"): "json-out inside repository",
                    str(self.tmp / "link" / "c.json"): "json-out parent is a symlink",
                    str(self.tmp / "missing" / "c.json"): "json-out parent is not a folder"}
        for path, why in refusals.items():
            with self.subTest(why):
                self.assertEqual(self.run_tool("--json-out", path), (1, ["alert | refused | " + why]))
        self.assertEqual(os.listdir(self.tmp / "real"), [])
        self.assertFalse(os.path.lexists(ROOT / "tests" / "x-alerts.json"))
        broken = mock.MagicMock()
        broken.return_value.__enter__.return_value.write.side_effect = OSError("disk full")
        c = self.tmp / "c.json"
        with mock.patch.object(alerts.os, "fdopen", broken):
            self.assertEqual(self.run_tool("--json-out", str(c)), (1, ["alert | refused | json-out not written (OSError)"]))
        self.assertFalse(os.path.lexists(c))

    def test_imports_no_clock_and_no_write_without_json_out(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse(names & {"smtplib", "imaplib", "socket", "ssl", "http", "urllib", "requests", "subprocess", "sqlite3"})
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(calls & {"today", "now", "time", "utcnow"})

        def boom(*_):
            raise AssertionError("clock read")

        class NoToday(dt.date):
            today = classmethod(boom)

        self.write([known("R-0", 0), unknown("U-1", 30)])
        snap = sorted((str(p), p.stat().st_mtime_ns) for p in self.tmp.rglob("*"))
        fake = types.SimpleNamespace(date=NoToday, timedelta=dt.timedelta)
        with mock.patch("time.time", boom), mock.patch.object(alerts, "datetime", fake), \
                mock.patch.object(alerts.today_tool, "datetime", fake), mock.patch.object(requests, "date", NoToday):
            self.assertEqual(self.run_tool()[0], 3)
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.tmp.rglob("*")), snap)

    def test_phrase_parity_with_today_and_dashboard_rules(self):
        self.write([known("R-0", 0), known("R-3", 3), known("R-P", -4), unknown("U-1", 30, agency=BETA)])
        out = self.tmp / "a.json"
        self.run_tool("--json-out", str(out))
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((doc["schema"], doc["review_required"], doc["as_of"]), ("flock-alerts-v1", True, DATE))
        today = {(s, r, k): t for _, k, _, s, r, t in alerts.today_tool.reminders(self.store, DATE)}
        self.assertEqual(len(doc["alerts"]), 4)
        self.assertEqual([a["kind"] for a in doc["alerts"]],
                         ["reminder-passed", "reminder-today", "reminder-week", "receipt-unknown"])
        for a in doc["alerts"]:
            self.assertTrue(all(isinstance(a[k], str) for k in ("id", "kind", "source")))
            self.assertEqual(set(a), {"id", "source", "kind", "severity", "agency", "request_id", "reminder_date", "phrase"})
            text = today[(a["agency"], a["request_id"], a["kind"])]
            sep = "; " if a["kind"] == "receipt-unknown" else " (provisional); "
            self.assertEqual(text.partition(sep)[0], a["phrase"])
        self.assertIsNone(doc["alerts"][-1]["reminder_date"])
        self.assertNotIn(SHA, out.read_text(encoding="utf-8"))
        self.assertNotIn("receipt email", out.read_text(encoding="utf-8"))

    def test_help_names_verb(self):
        done = subprocess.run([sys.executable, "-B", str(TOOL), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0)
        self.assertIn("reminders", done.stdout)


if __name__ == "__main__":
    unittest.main()
