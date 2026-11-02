"""flock_requests.py due PATH --ics OUT: a new private iCalendar of open requests' provisional reminder dates (RF1520-3)."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
SHA = hashlib.sha256(b"synthetic receipt").hexdigest()
NAME, EVIDENCE = "Marker Agency Qz Office", "EVIDENCE-MARKER-7QX acknowledgment"
spec = importlib.util.spec_from_file_location("_requests_rf1520_3", TOOL)
requests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(requests)
law = requests.law
PKG = law.load_package("us-ca")

EXPECTED = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//DeFlock//flock-requests//EN\r\nCALSCALE:GREGORIAN\r\nBEGIN:VEVENT\r\n"
    "UID:R\\;1\\,A-sample-city-police-department@flock-requests.invalid\r\nDTSTAMP:20261102T000000Z\r\n"
    "DTSTART;VALUE=DATE:20260312\r\nDTEND;VALUE=DATE:20260313\r\n"
    "SUMMARY:Provisional reminder: R\\;1\\,A (sample-city-police-department)\r\n"
    "DESCRIPTION:receipt 2026-03-02 + 10 calendar days\\nNot a legal due date.\\nP\r\n"
    " rovisional reminders only\\, not legal due dates. Business-day counts skip \r\n"
    " Saturday and Sunday only\\; public holidays are not modeled. Confirm actual\r\n"
    "  receipt\\, applicable last-day holiday exclusions and lawful extension not\r\n"
    " ice before any lateness conclusion.\r\nTRANSP:TRANSPARENT\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def row(rid, receipt="", ext="", status="open", agency=NAME, sent="2026-03-01"):
    return [rid, agency, sent, EVIDENCE if receipt else "", receipt, "", ext, status, SHA if receipt else ""]


def as_dict(cells):
    return dict(zip(requests.HEADER, cells))


def run(*args):
    return subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)


def unfold(text):  # RFC 5545 3.1: CRLF followed by one space is removed
    return text.replace("\r\n ", "")


def prop(text, name):
    return [ln.split(":", 1)[1] for ln in unfold(text).split("\r\n") if ln.split(":", 1)[0].split(";")[0] == name]


def unescape(value):
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


class IcsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / "out"
        self.out.mkdir()

    def write(self, rows, header=None):
        path = self.dir / "REQUESTS.csv"
        path.write_text("\n".join([",".join(header or requests.HEADER)] + [",".join(r) for r in rows]) + "\n",
                        encoding="utf-8")
        return path

    def refused(self, done, code, text, path=None):
        self.assertEqual(done.returncode, code, done.stderr)
        self.assertIn(text, done.stderr)
        self.assertEqual(done.stdout, "")
        self.assertEqual(sorted(os.listdir(self.out)), [])  # no file and no leftover temp
        if path is not None:
            self.assertFalse(os.path.lexists(path))

    def test_open_requests_with_receipts_become_events_on_the_due_dates(self):
        rows = [row("R-B", "2026-03-02"), row("R-A", "2026-03-05", ext="2026-03-09"), row("R-U"),
                row("R-CLOSED", "2026-03-02", status="closed")]
        path, out = self.write(rows), self.out / "cal.ics"
        done = run("due", str(path), "--ics", str(out), "--today", "2026-11-02")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        lines = done.stdout.splitlines()
        slugged = [dict(as_dict(r), agency=requests.slug(NAME)) for r in rows]
        self.assertEqual(lines[:-1], requests.due(slugged))
        self.assertEqual(lines[-1], "ics | events=2 receipt_unknown=1 | cal.ics")
        due = {x.split(" | ")[1]: x.split(" | ")[3].split()[1] for x in lines[:-2] if "determination" in x}
        self.assertEqual(due, {"R-B": "2026-03-12", "R-A": "2026-03-29"})
        text = out.read_bytes().decode("utf-8")
        starts = [date.fromisoformat(f"{v[:4]}-{v[4:6]}-{v[6:]}").isoformat() for v in prop(text, "DTSTART")]
        self.assertEqual(starts, [due["R-B"], due["R-A"]])  # sorted by (date, request_id)
        self.assertEqual(prop(text, "UID"), ["R-B-marker-agency-qz-office@flock-requests.invalid",
                                             "R-A-marker-agency-qz-office@flock-requests.invalid"])
        self.assertNotIn("R-CLOSED", text + done.stdout)
        self.assertNotIn("R-U", text)
        self.assertIn("+ 14 extension notice", unescape(prop(text, "DESCRIPTION")[1]))
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(os.listdir(self.out)), ["cal.ics"])

    def test_exact_bytes_escaping_folding_and_unfolding(self):
        cells = as_dict(row("R;1,A", "2026-03-02", agency="Sample City Police Department"))
        text, events, unknown = requests.ics_text([cells], date(2026, 11, 2))
        self.assertEqual((text, events, unknown), (EXPECTED, 1, 0))
        physical = text.split("\r\n")
        self.assertEqual(physical[-1], "")
        self.assertNotIn("\n", "".join(physical))  # every line ends in CRLF, no bare LF
        self.assertTrue(all(len(p.encode("utf-8")) <= 75 for p in physical))
        self.assertGreater(sum(p.startswith(" ") for p in physical), 1)
        want = f"receipt 2026-03-02 + 10 calendar days\nNot a legal due date.\n{law.HOLIDAY_NOTE}"
        self.assertEqual([unescape(v) for v in prop(text, "DESCRIPTION")], [want])
        self.assertEqual(unescape(prop(text, "SUMMARY")[0]), "Provisional reminder: R;1,A (sample-city-police-department)")
        wide = requests.fold("X" * 74 + "\u00e9" * 3)  # a two-octet character is never split across a fold
        self.assertTrue(all(len(p.encode("utf-8")) <= 75 for p in wide.split("\r\n")))
        self.assertEqual(unfold(wide), "X" * 74 + "\u00e9" * 3)

    def test_business_day_count_across_month_end_matches_law_deadline(self):
        biz = copy.deepcopy(PKG)
        biz["records_law"]["day_type"] = "business"
        cells = as_dict(row("R-BIZ", "2026-01-23", sent="2026-01-22"))  # a Friday; ten weekdays later is Feb 6
        with mock.patch.object(requests.law, "load_package", return_value=biz):
            text, events, _ = requests.ics_text([cells], date(2026, 11, 2))
        self.assertEqual(law.deadline(biz, "2026-01-23"), date(2026, 2, 6))
        self.assertEqual((prop(text, "DTSTART"), prop(text, "DTEND"), events), (["20260206"], ["20260207"], 1))
        self.assertIn("+ 10 business days", unescape(prop(text, "DESCRIPTION")[0]))

    def test_zero_events_is_header_and_footer_only(self):
        path, out = self.write([row("R-U"), row("R-C", "2026-03-02", status="closed")]), self.out / "empty.ics"
        done = run("due", str(path), "--ics", str(out), "--today", "2026-11-02")
        self.assertEqual(done.stdout.splitlines()[-1], "ics | events=0 receipt_unknown=1 | empty.ics")
        self.assertEqual(out.read_bytes(), b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//DeFlock//flock-requests//EN\r\n"
                                           b"CALSCALE:GREGORIAN\r\nEND:VCALENDAR\r\n")

    def test_write_refusals_leave_no_file_and_no_temp(self):
        path = self.write([row("R-B", "2026-03-02")])
        inside = ROOT / "tests" / "rf1520-3-must-not-exist.ics"
        self.refused(run("due", str(path), "--ics", str(inside)), 2, "due: --ics must be outside the repository", inside)
        link = self.dir / "repo-link"
        link.symlink_to(ROOT / "tests", target_is_directory=True)
        self.refused(run("due", str(path), "--ics", str(link / inside.name)), 2, "outside the repository", inside)
        missing = self.out / "no-such-dir" / "cal.ics"
        self.refused(run("due", str(path), "--ics", str(missing)), 2, "due: --ics parent folder is missing")
        for name, make in (("kept.ics", lambda p: p.write_bytes(b"organizer calendar")),
                           ("dangling.ics", lambda p: p.symlink_to(self.dir / "nowhere.ics"))):
            existing = self.out / name
            make(existing)
            done = run("due", str(path), "--ics", str(existing))
            self.assertEqual((done.returncode, done.stdout), (2, ""))
            self.assertIn("due: --ics already exists", done.stderr)
            self.assertEqual(sorted(os.listdir(self.out)), [name])
            existing.unlink()
        self.assertFalse((self.dir / "nowhere.ics").exists())
        self.assertFalse(inside.exists())

    def test_ics_appearing_mid_write_is_never_replaced(self):
        path, out = self.write([row("R-B", "2026-03-02")]), self.out / "race.ics"
        err, stdout = io.StringIO(), io.StringIO()
        with mock.patch.object(requests.os, "link", side_effect=FileExistsError), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(stdout):
            code = requests.main(["due", str(path), "--ics", str(out), "--today", "2026-11-02"])
        self.assertEqual((code, stdout.getvalue()), (2, ""))
        self.assertIn("due: --ics already exists", err.getvalue())
        self.assertEqual(sorted(os.listdir(self.out)), [])

    def test_flags_any_order_as_of_and_default_today(self):
        path, out = self.write([row("R-B", "2026-03-02")]), self.out / "a.ics"
        done = run("due", str(path), "--today", "2026-11-02", "--as-of", "2026-03-10", "--ics", str(out))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.splitlines()[0].endswith(" | as of 2026-03-10: in 2 days"))
        self.assertEqual(prop(out.read_bytes().decode("utf-8"), "DTSTAMP"), ["20261102T000000Z"])
        before, plain = date.today(), self.out / "b.ics"
        self.assertEqual(run("due", str(path), "--ics", str(plain)).returncode, 0)
        stamps = {f"{d:%Y%m%d}T000000Z" for d in (before, before + timedelta(days=1))}
        self.assertIn(prop(plain.read_bytes().decode("utf-8"), "DTSTAMP")[0], stamps)

    def test_bad_arguments_print_usage_and_write_nothing(self):
        path, out = self.write([row("R-B", "2026-03-02")]), str(self.out / "x.ics")
        for args in (["--ics", out, "--today", "2026-02-30"], ["--ics", out, "--ics", out + "2"], ["--ics"],
                     ["--ics", out, "--as-of", "2026-13-01"], ["--ics", out, "--bogus", "1"], ["--today", "2026-11-02",
                                                                                              "--ics"]):
            with self.subTest(args=args):
                self.refused(run("due", str(path), *args), 2, "usage: flock_requests.py")

    def test_due_without_ics_is_unchanged(self):
        path = self.write([row("R-B", "2026-03-02"), row("R-U")])
        rows = [as_dict(r) for r in (row("R-B", "2026-03-02"), row("R-U"))]
        plain, dated = run("due", str(path)), run("due", str(path), "--as-of", "2026-03-10")
        self.assertEqual((plain.returncode, plain.stdout), (0, "\n".join(requests.due(rows)) + "\n"))
        self.assertEqual(dated.stdout, "\n".join(requests.due(rows, date(2026, 3, 10))) + "\n")
        self.assertEqual(plain.stdout.splitlines()[0], f"provisional reminder | R-B | {NAME} | determination 2026-03-12 | "
                         "receipt 2026-03-02 + 10 calendar days | Gov. Code 7922.535 | not a legal due date")
        self.assertNotIn("ics |", plain.stdout + dated.stdout)

    def test_requests_failing_check_exit_1_and_write_nothing(self):
        out = self.out / "bad.ics"
        bad = self.write([row("R-B", "2026-03-02")[:7] + ["pending", SHA]])
        done = run("due", str(bad), "--ics", str(out))
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("row 2: status must be one of", done.stderr)
        self.assertEqual(sorted(os.listdir(self.out)), [])
        self.refused(run("due", str(self.write([row("R-B")], header=["x"])), "--ics", str(out)), 1, "header", out)

    def test_free_text_name_and_evidence_never_leave_and_store_is_unchanged(self):
        path, out = self.write([row("R-B", "2026-03-02"), row("R-A", "2026-03-05", ext="2026-03-09")]), self.out / "p.ics"
        before = path.read_bytes()
        done = run("due", str(path), "--ics", str(out), "--today", "2026-11-02")
        self.assertEqual(done.returncode, 0, done.stderr)
        both = out.read_bytes().decode("utf-8") + done.stdout
        for marker in (NAME, EVIDENCE, "EVIDENCE-MARKER", "Qz Office"):
            self.assertNotIn(marker, unfold(both))
        self.assertIn("marker-agency-qz-office", both)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
