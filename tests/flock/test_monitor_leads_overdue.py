"""RF1300-24: flock_monitor leads --older-than N lists only new leads first seen more than N days before --date."""
import contextlib
import csv
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("_test_monitor_leads_overdue", ROOT / "tools" / "flock_monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)
TODAY = "2026-11-02"
N20, N15, N14, N3, R30, I30 = ("M-%012x" % n for n in (0x20, 0x15, 0x14, 0x03, 0x30A, 0x30B))
ROWS = [[N15, "Zed Town", "2026-10-18", "new", "2026-10-18", "digest"],
        [N20, "Sample City", "2026-10-13", "new", "2026-10-13", "digest"],
        [R30, "Sample City", "2026-10-03", "requested", "2026-10-05", "Organizer Quietname"],
        [N3, "Sample City", "2026-10-30", "new", "2026-10-30", "digest"],
        [I30, "Zed Town", "2026-10-03", "ignored", "2026-10-04", "Organizer Hiddenby"],
        [N14, "Zed Town", "2026-10-19", "new", "2026-10-19", "digest"]]
LINE = {N20: "lead | %s | sample-city | new | first_seen=2026-10-13 | age=20d" % N20,
        N15: "lead | %s | zed-town | new | first_seen=2026-10-18 | age=15d" % N15,
        N14: "lead | %s | zed-town | new | first_seen=2026-10-19 | age=14d" % N14,
        N3: "lead | %s | sample-city | new | first_seen=2026-10-30 | age=3d" % N3,
        R30: "lead | %s | sample-city | requested | first_seen=2026-10-03 | age=30d" % R30,
        I30: "lead | %s | zed-town | ignored | first_seen=2026-10-03 | age=30d" % I30}
ZERO = "leads=%d shown=0 new=0 ignored=0 requested=0"


class LeadsOverdueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.store.mkdir(mode=0o700)
        self.leads = self.store / "LEADS.csv"

    def write(self, rows):
        with open(self.leads, "w", encoding="utf-8", newline="") as out:
            csv.writer(out, lineterminator="\n").writerows([monitor.LEADS_HEADER] + rows)

    def run_main(self, *args, store=None, date=TODAY):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = monitor.main(["leads", "--store", str(store or self.store), "--date", date, *args])
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue()

    def lines(self, *args, **kw):
        code, out = self.run_main(*args, **kw)
        return code, out.splitlines()

    def test_only_leads_strictly_older_than_n_days(self):
        self.write(ROWS)
        self.assertEqual(self.lines("--older-than", "14"), (0, [LINE[N20], LINE[N15],
                         "leads=6 shown=2 new=2 ignored=0 requested=0 older_than=14 overdue=2"]))
        self.assertEqual(self.lines("--older-than", "13"), (0, [LINE[N20], LINE[N15], LINE[N14],
                         "leads=6 shown=3 new=3 ignored=0 requested=0 older_than=13 overdue=3"]))
        self.assertEqual(self.lines("--older-than", "20"), (0, [ZERO % 6 + " older_than=20 overdue=0"]))

    def test_requested_or_ignored_leads_are_never_overdue(self):
        self.write(ROWS)
        self.assertEqual(self.lines("--older-than", "1"), (0, [LINE[N20], LINE[N15], LINE[N14], LINE[N3],
                         "leads=6 shown=4 new=4 ignored=0 requested=0 older_than=1 overdue=4"]))
        self.write([ROWS[2], ROWS[4]])
        self.assertEqual(self.lines("--older-than", "7"), (0, [ZERO % 2 + " older_than=7 overdue=0"]))

    def test_agency_and_older_than_both_apply(self):
        self.write(ROWS)
        self.assertEqual(self.lines("--agency", "sample-city", "--older-than", "14"),
                         (0, [LINE[N20], "leads=6 shown=1 new=1 ignored=0 requested=0 older_than=14 overdue=1"]))
        self.assertEqual(self.lines("--older-than", "14", "--agency", "zed-town"),
                         (0, [LINE[N15], "leads=6 shown=1 new=1 ignored=0 requested=0 older_than=14 overdue=1"]))
        self.assertEqual(self.lines("--agency", "nowhere", "--older-than", "1"),
                         (0, [ZERO % 6 + " older_than=1 overdue=0"]))

    def test_bad_days_or_decided_state_is_usage_exit_2(self):
        self.write(ROWS)
        for args in (("--older-than", "0"), ("--older-than", "366"), ("--older-than", "abc"), ("--older-than", "1.5"),
                     ("--older-than", "-1"), ("--older-than", ""), ("--older-than", " 7"), ("--older-than", "1000"),
                     ("--older-than", "7", "--state", "ignored"), ("--state", "requested", "--older-than", "7"),
                     ("--json", "--state", "ignored", "--older-than", "7")):
            with self.subTest(args=args):
                self.assertEqual(self.run_main(*args), (2, ""))

    def test_state_new_with_older_than_is_allowed(self):
        self.write(ROWS)
        self.assertEqual(self.lines("--state", "new", "--older-than", "7"), (0, [LINE[N20], LINE[N15], LINE[N14],
                         "leads=6 shown=3 new=3 ignored=0 requested=0 older_than=7 overdue=3"]))
        self.assertEqual(self.lines("--older-than", "365"), (0, [ZERO % 6 + " older_than=365 overdue=0"]))

    def test_malformed_row_elsewhere_is_refused_and_nothing_listed(self):
        for n, (col, value) in enumerate(((2, "2026-11-03"), (3, "maybe"))):
            with self.subTest(col=col):
                rows = [list(r) for r in ROWS]
                rows[4][col] = value  # an ignored zed-town row: outside both the age and the agency filter
                self.write(rows)
                field = ("first_seen", "state")[n]
                self.assertEqual(self.lines("--older-than", "14", "--agency", "sample-city"),
                                 (1, ["LEADS.csv row 6: %s is invalid" % field]))

    def test_missing_leads_csv_is_zero_and_creates_nothing(self):
        self.assertEqual(self.lines("--older-than", "7"), (0, [ZERO % 0 + " older_than=7 overdue=0"]))
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), [])
        absent = self.store / "absent"
        self.assertEqual(self.lines("--older-than", "7", store=absent), (0, [ZERO % 0 + " older_than=7 overdue=0"]))
        self.assertFalse(absent.exists())

    def test_read_only_and_never_prints_who_or_when(self):
        self.write(ROWS)
        data = self.leads.read_bytes()
        with mock.patch.object(monitor, "store_lock", side_effect=AssertionError("lock taken")), \
                mock.patch.object(monitor, "write_atomic", side_effect=AssertionError("write")):
            code, out = self.run_main("--older-than", "1")
        self.assertEqual((code, self.leads.read_bytes(), sorted(p.name for p in self.store.iterdir())),
                         (0, data, ["LEADS.csv"]))
        for absent in ("Quietname", "Hiddenby", "digest", "2026-10-05", "Sample City", "Zed Town"):
            self.assertNotIn(absent, out)

    def test_json_carries_older_than_and_overdue_like_the_text(self):
        self.write(ROWS)
        code, out = self.run_main("--json", "--older-than", "14")
        doc = json.loads(out)
        self.assertEqual((code, [e["item_id"] for e in doc["leads"]]), (0, [N20, N15]))
        self.assertEqual(doc["counts"], {"leads": 6, "shown": 2, "new": 2, "ignored": 0, "requested": 0,
                                         "older_than": 14, "overdue": 2})
        self.assertNotIn("older_than", json.loads(self.run_main("--json")[1])["counts"])

    def test_no_flag_output_is_byte_identical_to_main(self):  # goldens captured from origin/main before RF1300-24
        everything = "\n".join([LINE[R30], LINE[I30], LINE[N20], LINE[N15], LINE[N14], LINE[N3],
                                "leads=6 shown=6 new=4 ignored=1 requested=1"]) + "\n"
        self.write(ROWS)
        self.assertEqual(self.run_main(), (0, everything))
        self.assertEqual(self.run_main("--state", "new"), (0, "\n".join(
            [LINE[N20], LINE[N15], LINE[N14], LINE[N3], "leads=6 shown=4 new=4 ignored=0 requested=0"]) + "\n"))
        self.assertEqual(self.run_main("--agency", "sample-city"), (0, "\n".join(
            [LINE[R30], LINE[N20], LINE[N3], "leads=6 shown=3 new=2 ignored=0 requested=1"]) + "\n"))
        self.assertEqual(self.run_main("--agency", "nowhere"), (0, ZERO % 6 + "\n"))
        self.write([ROWS[2], ROWS[4]])
        self.assertEqual(self.run_main(), (0, "\n".join([LINE[R30], LINE[I30],
                                                          "leads=2 shown=2 new=0 ignored=1 requested=1"]) + "\n"))
        rows = [list(r) for r in ROWS]
        rows[4][2] = "2026-11-03"
        self.write(rows)
        self.assertEqual(self.run_main(), (1, "LEADS.csv row 6: first_seen is invalid\n"))
        self.leads.unlink()
        self.assertEqual(self.run_main(), (0, ZERO % 0 + "\n"))


if __name__ == "__main__":
    unittest.main()
