"""RF0420-7: flock_monitor leads is a read-only LEADS.csv triage list (age, per-state counts); no titles/URLs/state_by."""
import contextlib
import csv
import fcntl
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("_test_monitor_leads_list", ROOT / "tools" / "flock_monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)
TODAY = "2026-11-02"
A, B, C, D, E = ("M-%012x" % n for n in (0xA1, 0xB2, 0xC3, 0xD4, 0xE5))
FIVE = [[A, "Sample City", "2026-10-30", "new", "2026-10-30", "digest"],
        [B, "Zed Town", "2026-10-20", "requested", "2026-10-25", "Organizer Quietname"],
        [C, "Sample City", "2026-10-20", "ignored", "2026-10-21", "Organizer Hiddenby"],
        [D, "Zed Town", "2026-11-02", "new", "2026-11-02", "digest"],
        [E, "Sample City", "2026-10-25", "new", "2026-10-25", "digest"]]
LINE = {A: "lead | %s | sample-city | new | first_seen=2026-10-30 | age=3d" % A,
        B: "lead | %s | zed-town | requested | first_seen=2026-10-20 | age=13d" % B,
        C: "lead | %s | sample-city | ignored | first_seen=2026-10-20 | age=13d" % C,
        D: "lead | %s | zed-town | new | first_seen=2026-11-02 | age=0d" % D,
        E: "lead | %s | sample-city | new | first_seen=2026-10-25 | age=8d" % E}


class LeadsListTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.store.mkdir(mode=0o700)
        self.leads = self.store / "LEADS.csv"

    def write(self, rows, header=monitor.LEADS_HEADER):
        with open(self.leads, "w", encoding="utf-8", newline="") as out:
            csv.writer(out, lineterminator="\n").writerows([header] + rows)

    def run_main(self, *args, store=None, date=TODAY):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = monitor.main(["leads", "--store", str(store or self.store), "--date", date, *args])
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue().splitlines()

    def files(self):
        return sorted(p.name for p in self.store.iterdir())

    def test_five_leads_oldest_first_then_id_with_counts(self):
        self.write(FIVE)
        self.assertEqual(self.run_main(), (0, [LINE[B], LINE[C], LINE[E], LINE[A], LINE[D],
                                               "leads=5 shown=5 new=3 ignored=1 requested=1"]))

    def test_state_filter_alone(self):
        self.write(FIVE)
        self.assertEqual(self.run_main("--state", "new"), (0, [LINE[E], LINE[A], LINE[D],
                                                               "leads=5 shown=3 new=3 ignored=0 requested=0"]))
        self.assertEqual(self.run_main("--state", "requested"), (0, [LINE[B],
                                                                     "leads=5 shown=1 new=0 ignored=0 requested=1"]))

    def test_agency_filter_alone_matches_slug_of_agency(self):
        self.write(FIVE)
        self.assertEqual(self.run_main("--agency", "sample-city"), (0, [LINE[C], LINE[E], LINE[A],
                                                                        "leads=5 shown=3 new=2 ignored=1 requested=0"]))

    def test_state_and_agency_together(self):
        self.write(FIVE)
        self.assertEqual(self.run_main("--state", "new", "--agency", "sample-city"),
                         (0, [LINE[E], LINE[A], "leads=5 shown=2 new=2 ignored=0 requested=0"]))
        self.assertEqual(self.run_main("--state", "requested", "--agency", "sample-city"),
                         (0, ["leads=5 shown=0 new=0 ignored=0 requested=0"]))
        self.assertEqual(self.run_main("--agency", "nowhere"), (0, ["leads=5 shown=0 new=0 ignored=0 requested=0"]))

    def test_missing_leads_csv_prints_zero_counts_and_creates_nothing(self):
        before = self.files()
        self.assertEqual(self.run_main(), (0, ["leads=0 shown=0 new=0 ignored=0 requested=0"]))
        self.assertEqual(self.files(), before)
        absent = self.store / "absent"
        self.assertEqual(self.run_main(store=absent), (0, ["leads=0 shown=0 new=0 ignored=0 requested=0"]))
        self.assertFalse(absent.exists())

    def test_header_only_is_zero_leads(self):
        self.write([])
        self.assertEqual(self.run_main(), (0, ["leads=0 shown=0 new=0 ignored=0 requested=0"]))

    def test_symlinked_leads_csv_refused(self):
        real = self.store / "real.csv"
        self.write(FIVE)
        os.replace(self.leads, real)
        os.symlink(real, self.leads)
        self.assertEqual(self.run_main(), (1, ["LEADS.csv is a symlink"]))

    def test_folder_leads_csv_refused(self):
        self.leads.mkdir()
        self.assertEqual(self.run_main(), (1, ["LEADS.csv is not a file"]))

    def test_wrong_header_refused(self):
        self.write(FIVE, header=monitor.LEADS_HEADER[:-1] + ["who"])
        self.assertEqual(self.run_main(), (1, ["LEADS.csv row 1: header is not the accepted header"]))

    def test_short_row_refused_with_row_number(self):
        self.write(FIVE[:2] + [FIVE[2][:-1]] + FIVE[3:])
        self.assertEqual(self.run_main(), (1, ["LEADS.csv row 4: columns have the wrong count"]))

    def test_bad_row_fields_refused_before_any_lead_line(self):
        cases = (("state", 3, "maybe", "state"), ("item_id", 0, "M-XYZ", "item_id"),
                 ("item_id", 0, "M-%012X" % 0xAB, "item_id"), ("month 13", 2, "2026-13-01", "first_seen"),
                 ("after --date", 2, "2026-11-03", "first_seen"), ("empty date", 2, "", "first_seen"),
                 ("compact date", 2, "20261030", "first_seen"))
        for label, col, value, field in cases:
            with self.subTest(label):
                rows = [list(r) for r in FIVE]
                rows[3][col] = value  # data row 4 = file row 5
                self.write(rows)
                self.assertEqual(self.run_main(), (1, ["LEADS.csv row 5: %s is invalid" % field]))

    def test_bad_row_outside_filter_still_refused(self):
        rows = [list(r) for r in FIVE]
        rows[1][3] = "maybe"  # a zed-town row; the filter asks for sample-city
        self.write(rows)
        self.assertEqual(self.run_main("--agency", "sample-city"), (1, ["LEADS.csv row 3: state is invalid"]))

    def test_first_seen_on_date_is_age_zero(self):
        self.write([FIVE[3]])
        self.assertEqual(self.run_main(), (0, [LINE[D], "leads=1 shown=1 new=1 ignored=0 requested=0"]))
        self.assertEqual(self.run_main(date="2026-11-01"), (1, ["LEADS.csv row 2: first_seen is invalid"]))

    def test_store_inside_repository_refused(self):
        self.assertEqual(self.run_main(store=ROOT / "tests" / "flock"), (1, ["store inside repository"]))

    def test_bad_agency_or_state_is_usage_exit_2(self):
        self.write(FIVE)
        for args in (("--agency", "Sample City"), ("--agency", "../x"), ("--agency", "sample--city"),
                     ("--agency", "-x"), ("--agency", ""), ("--state", "done"), ("--state", "NEW")):
            with self.subTest(args=args):
                self.assertEqual(self.run_main(*args), (2, []))

    def test_never_prints_state_by_state_at_titles_or_urls(self):
        self.write(FIVE)
        with open(self.store / "MONITOR.csv", "w", encoding="utf-8", newline="") as out:
            writer = csv.writer(out, lineterminator="\n")
            writer.writerow(monitor.MONITOR_HEADER)
            writer.writerow([A, "Sample City", "agenda", "https://www.city.example/hidden-agenda.pdf",
                             "Flock camera contract renewal", "0" * 64, "2026-10-30", "public", "unreviewed"])
        code, out = self.run_main()
        self.assertEqual(code, 0)
        text = "\n".join(out)
        for absent in ("Quietname", "Hiddenby", "Organizer", "digest", "2026-10-21", "https://", "city.example",
                       "Flock camera", "agenda", "Sample City", "Zed Town"):
            self.assertNotIn(absent, text)

    def test_held_lock_does_not_block_and_nothing_is_written(self):
        self.write(FIVE)
        fd = os.open(self.store / ".monitor.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before, data = self.files(), self.leads.read_bytes()
        code, out = self.run_main()
        self.assertEqual((code, out[-1]), (0, "leads=5 shown=5 new=3 ignored=1 requested=1"))
        self.assertEqual((self.files(), self.leads.read_bytes()), (before, data))

    def test_never_takes_the_lock(self):
        self.write(FIVE)
        with mock.patch.object(monitor, "store_lock", side_effect=AssertionError("lock taken")), \
                mock.patch.object(monitor, "write_atomic", side_effect=AssertionError("write")):
            self.assertEqual(self.run_main()[0], 0)
        self.assertNotIn(".monitor.lock", self.files())


if __name__ == "__main__":
    unittest.main()
