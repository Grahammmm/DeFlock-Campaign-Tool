"""Synthetic-only tests for records agency-events --validate; no live file, mail or send."""
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records.agency_run import events_ingest, events_validate

HEADER = "\t".join(events_ingest.COLUMNS)
CANARY = "CANARY-CELL-TEXT"


def row(eid="T-1", date="2026-11-02", time="09:00 PST", ref="26-901", kind="request_sent", what="Sent",
        source="Sent:1", basis="DATED", note="receipt basis: recorded", who="DeFlock SLO", agency="Test PD"):
    return "\t".join((eid, date, time, agency, ref, kind, what, source, who, basis, note))


def clean_rows():
    return [row("T-1", kind="request_sent"), row("T-2", kind="clock_start", time="", basis="INFERRED"),
            row("T-3", date="2026-11-12", kind="clock_due", time="", basis="INFERRED"),
            row("T-4", date="2026-11-13", kind="follow_up_sent"), row("T-5", date="2026-11-20", kind="determination")]


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, rows, header=HEADER):
        path = self.dir / "events.tsv"
        path.write_text("\n".join([header] + rows) + "\n", "utf-8")
        return path

    def run_cli(self, argv):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            code = events_ingest.main(argv)
        return code, o.getvalue(), e.getvalue()

    def codes(self, rows, **kw):
        return [(p["line"], p["column"], p["code"]) for p in events_validate.validate(self.write(rows, **kw))]


class Tests(Case):
    def test_clean_file_exits_zero(self):
        path = self.write(clean_rows())
        code, out, _ = self.run_cli(["--validate", str(path), "--json"])
        self.assertEqual((code, json.loads(out)), (0, {"ok": True, "problems": []}))
        self.assertEqual(self.run_cli(["--validate", str(path)])[:2], (0, ""))

    def test_ingest_output_validates_clean(self):
        store = self.dir / "mail"
        (store / "receipts").mkdir(parents=True)
        (store / "SYNC-STATUS.json").write_text(json.dumps({"failures": []}))
        (store / "receipts" / "1.json").write_text(json.dumps({
            "folder": "Sent", "uid": 1, "internaldate": "25-Aug-2026 04:11:00 +0000",
            "headers": {"From": ["a@pismotest.example"], "Subject": ["Records request 26-901"]}}))
        aliases = self.dir / "aliases.json"
        aliases.write_text(json.dumps({"agency": "Pismo Test PD", "agency_id": "pismo-test", "event_prefix": "PTP",
                                       "aliases": ["Pismo Test PD", "pismotest.example"],
                                       "requests": {"26-901": "req-901"}}))
        out = self.dir / "out"
        self.assertEqual(self.run_cli(["--receipts", str(store), "--agency-aliases", str(aliases),
                                       "--out", str(out)])[0], 0)
        self.assertEqual(events_validate.validate(out / "events.tsv"), [])

    def test_each_rule_gives_one_named_hit(self):
        rows = clean_rows() + [
            "a\tb",
            row("", kind="request_sent"),
            row("T-1"),
            row("T-9", kind="bogus"),
            row("T-10", date="2026-02-30"),
            row("T-11", time="9am"),
            row("T-12", basis="GUESS"),
            row("T-13", ref=""),
            row("T-14", what="a <b> c"),
            row("T-15", source="Sent:2 2026-11-02T00:20Z"),
            row("T-16", what="response overdue", note="on our records"),
        ]
        expected = [(7, "row", "row_columns"), (8, "event_id", "event_id_missing"),
                    (9, "event_id", "event_id_duplicate"), (10, "event_type", "event_type_unknown"),
                    (11, "date", "date_invalid"), (12, "time_local", "time_invalid"),
                    (13, "basis", "basis_invalid"), (14, "request_ref", "request_ref_missing"),
                    (15, "what_happened", "angle_bracket"), (16, "date", "date_is_utc_date"),
                    (17, "clock_note", "overdue_wording")]
        self.assertEqual(self.codes(rows), expected)

    def test_header_invalid_stops_early(self):
        swapped = "\t".join(reversed(events_ingest.COLUMNS))
        self.assertEqual(self.codes(clean_rows() + ["x"], header=swapped), [(1, "header", "header_invalid")])

    def test_utc_date_rule(self):
        def hits(date, stamp):
            return self.codes([row(date=date, source="Sent:1 " + stamp)])
        self.assertEqual(hits("2026-11-02", "2026-11-02T00:20Z"), [(2, "date", "date_is_utc_date")])
        self.assertEqual(hits("2026-11-01", "2026-11-02T00:20Z"), [])
        self.assertEqual(hits("2026-11-05", "2026-11-05T21:00Z"), [])

    def test_overdue_wording_rule(self):
        def hits(what, note):
            return self.codes([row(what=what, note=note)])
        self.assertEqual(hits("Reply overdue", "on our records"), [(2, "clock_note", "overdue_wording")])
        self.assertEqual(hits("Reply overdue", "on our records; not a legal finding"), [])
        self.assertEqual(hits("Reply is past due", "x"), [(2, "clock_note", "overdue_wording")])
        self.assertEqual(hits("The latest reply", "x"), [])

    def test_real_event_types_accepted(self):
        rows = [row("T-1", kind="request_received"), row("T-2", kind="reconsideration_sent")]
        self.assertEqual(self.codes(rows), [])
        self.assertEqual(self.codes([row(kind="bogus_type")]), [(2, "event_type", "event_type_unknown")])

    def test_output_never_prints_cell_text(self):
        path = self.write([row(kind="bogus", what=CANARY + " <x>")])
        for argv in (["--validate", str(path)], ["--validate", str(path), "--json"]):
            code, out, err = self.run_cli(argv)
            self.assertEqual(code, 1)
            self.assertNotIn(CANARY, out + err)
            self.assertIn("event_type_unknown", out)

    def test_file_guards(self):
        good = self.write(clean_rows())
        link = self.dir / "link.tsv"
        link.symlink_to(good)
        big = self.dir / "big.tsv"
        big.write_bytes(b"x" * (3 * 1024 * 1024))
        bad = self.dir / "bad.tsv"
        bad.write_bytes(b"\xff\xfe\t\n")
        blank = self.dir / "blank.tsv"
        blank.write_bytes(b"")
        for path in (link, big, bad, self.dir / "missing.tsv", self.dir):
            self.assertEqual(events_validate.validate(path), [{"line": 0, "column": "file", "code": "file_invalid"}])
        self.assertEqual([p["code"] for p in events_validate.validate(blank)], ["header_invalid"])
        before = (good.read_bytes(), os.stat(good).st_mtime_ns)
        self.assertEqual(self.run_cli(["--validate", str(good)])[0], 0)
        self.assertEqual((good.read_bytes(), os.stat(good).st_mtime_ns), before)

    def test_ingest_flags_still_required(self):
        code, out, err = self.run_cli([])
        self.assertEqual((code, out, err.strip()), (1, "", "agency-events: arguments_missing"))
        self.assertEqual(self.run_cli(["--receipts", "x"])[0], 1)


if __name__ == "__main__":
    unittest.main()
