"""DF1537-09: every scan appends its per-page outcomes to <store>/SCAN-LOG.csv; digest names pages down for N scans."""
import contextlib
import csv
import datetime
import fcntl
import hashlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("flock_monitor_scan_log", ROOT / "tools" / "flock_monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)
BODY = b'<html><a href="/agendas/2026-10-13">Council agenda</a></html>'
ALPHA, BETA, GAMMA = "https://www.alpha.example/agendas", "https://beta.example/news", "https://gamma.example/minutes"
WATCH = [("Alpha City", "agenda", ALPHA, "", "alpha.example", ""),
         ("Beta Town", "news", BETA, "", "beta.example", hashlib.sha256(BODY).hexdigest()),
         ("Gamma County", "minutes", GAMMA, "", "gamma.example", ""),
         ("Delta Village", "social", "", "Delta Village plate cameras", "delta.example", "")]
LOG_HEADER = "scan_date,agency,url,outcome,reason"

def day(n):  # 2026-10-01 + n days
    return (datetime.date(2026, 10, 1) + datetime.timedelta(days=n)).isoformat()

def fake_fetch(refuse=None):  # refuse: {url: reason}; counts every call, never opens a socket
    def answer(url, official_domain):
        if url in (refuse or {}):
            raise monitor.Refused(refuse[url])
        return url, "text/html; charset=utf-8", BODY
    return mock.Mock(side_effect=answer)

class ScanLogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.watchlist, self.store = self.tmp / "WATCHLIST.csv", self.tmp / "store"
        self.log = self.store / "SCAN-LOG.csv"
        self.write_watch(WATCH)

    def write_watch(self, rows):
        with self.watchlist.open("w", encoding="utf-8", newline="") as out:
            csv.writer(out, lineterminator="\n").writerows([monitor.WATCH_HEADER] + [list(r) for r in rows])

    def run_main(self, *args, fetch=None):
        out = io.StringIO()
        with mock.patch.object(monitor, "fetch", fetch or fake_fetch()), contextlib.redirect_stdout(out):
            code = monitor.main(list(args))
        return code, out.getvalue().splitlines()

    def scan(self, date, refuse=None, fetch=None):
        return self.run_main("scan", str(self.watchlist), "--store", str(self.store), "--date", date,
                             fetch=fetch or fake_fetch(refuse))

    def digest(self, date, *extra):
        code, out = self.run_main("digest", "--store", str(self.store), "--date", date, *extra)
        path = self.store / "digest" / (date + ".md")
        return code, out, path.read_text(encoding="utf-8").splitlines() if path.exists() else None

    def log_rows(self):
        with self.log.open(encoding="utf-8", newline="") as handle:
            return [tuple(r) for r in csv.reader(handle)]

    def only_gamma(self):
        self.write_watch([WATCH[2]])

    def test_one_row_per_non_social_row_with_exact_reasons(self):
        code, out = self.scan(day(0), {GAMMA: "http 503"})
        self.assertEqual(code, 0)
        self.assertEqual(out, ["row 4: refused http 503",
                               "scanned=3 unchanged=1 new_items=1 refused=1 offdomain_links=0 skipped_social=1"])
        rows = [(day(0), "Alpha City", ALPHA, "ok", ""), (day(0), "Beta Town", BETA, "unchanged", ""),
                (day(0), "Gamma County", GAMMA, "refused", "http 503")]
        self.assertEqual(self.log_rows(), [tuple(LOG_HEADER.split(","))] + rows)
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.scan(day(0), {GAMMA: "http 503"})[0], 0)  # a scan is an event: same date appends again
        self.assertEqual(self.log_rows()[1:], rows + [(day(0), "Alpha City", ALPHA, "unchanged", "")] + rows[1:])

    def test_three_refusals_listed_with_last_reason_and_streak_start(self):
        self.only_gamma()
        for n, why in enumerate(("network URLError", "http 503", "http 503")):
            self.assertEqual(self.scan(day(n), {GAMMA: why})[0], 0)
        code, out, text = self.digest(day(2), "--unreachable-after", "3")
        self.assertEqual((code, text), (0, ["Flock monitor %s: no new items" % day(2), "",
                                            "Unreachable (1 pages, last 3 scans refused)",
                                            "- Gamma County: %s (http 503, since %s)" % (GAMMA, day(0))]))
        code, out, text = self.digest(day(2), "--unreachable-after", "4")  # fewer than N rows: not listed
        self.assertEqual((code, text), (0, ["Flock monitor %s: no new items" % day(2)]))
        self.assertEqual(self.digest(day(2))[2][-1], "- Gamma County: %s (http 503, since %s)" % (GAMMA, day(0)))  # N=3

    def test_ok_breaks_the_streak_and_since_restarts_after_it(self):
        self.only_gamma()
        for n, refused in enumerate((True, True, False, True)):
            self.scan(day(n), {GAMMA: "http 503"} if refused else None)
        self.assertEqual(self.digest(day(3), "--unreachable-after", "2")[2], ["Flock monitor %s: no new items" % day(3)])
        self.scan(day(4), {GAMMA: "network URLError"})
        self.assertEqual(self.digest(day(4), "--unreachable-after", "2")[2][-1],
                         "- Gamma County: %s (network URLError, since %s)" % (GAMMA, day(3)))

    def test_unchanged_also_breaks_the_streak(self):
        self.write_watch([WATCH[1]])  # Beta's hash matches BODY: a reachable page is unchanged
        for n, refused in enumerate((True, True, False)):
            self.scan(day(n), {BETA: "http 503"} if refused else None)
        self.assertEqual(self.log_rows()[-1][3], "unchanged")
        self.assertEqual(len(self.digest(day(2), "--unreachable-after", "2")[2]), 1)

    def test_only_rows_on_or_before_the_digest_date_count(self):
        self.only_gamma()
        for n in range(3):
            self.scan(day(n), {GAMMA: "http 403"})
        self.scan(day(3))
        self.scan(day(4))
        self.assertEqual(self.digest(day(2), "--unreachable-after", "3")[2][-1],
                         "- Gamma County: %s (http 403, since %s)" % (GAMMA, day(0)))
        self.assertEqual(len(self.digest(day(4), "--unreachable-after", "2")[2]), 1)
        self.assertEqual(len(self.digest(day(1), "--unreachable-after", "3")[2]), 1)  # only 2 rows on or before D

    def test_logged_page_listed_after_it_leaves_the_watch_list(self):
        self.only_gamma()
        for n in range(2):
            self.scan(day(n), {GAMMA: "http 503"})
        self.write_watch([WATCH[0]])  # the log is the only source; Alpha has one ok row and is not listed
        self.scan(day(2))
        text = self.digest(day(2), "--unreachable-after", "2")[2]
        self.assertEqual(text[-2:], ["Unreachable (1 pages, last 2 scans refused)",
                                     "- Gamma County: %s (http 503, since %s)" % (GAMMA, day(0))])

    def test_sixty_rows_kept_per_page_other_pages_untouched(self):
        self.store.mkdir(mode=0o700)
        other = ["%s,Old Agency,https://old.example/news,refused,http 404" % day(n) for n in range(2)]
        self.log.write_text("\n".join([LOG_HEADER] + other) + "\n", encoding="utf-8")
        self.only_gamma()
        trimmed = []
        for n in range(61):
            code, out = self.scan(day(n))
            self.assertEqual(code, 0)
            trimmed += [line for line in out if "log_trimmed=" in line]
        self.assertEqual(trimmed, ["scanned=1 unchanged=1 new_items=0 refused=0 offdomain_links=0 skipped_social=0"
                                   " log_trimmed=1"])
        rows = self.log_rows()[1:]
        gamma = [r for r in rows if r[1] == "Gamma County"]
        self.assertEqual((len(gamma), gamma[0][0], gamma[-1][0]), (60, day(1), day(60)))
        self.assertEqual([",".join(r) for r in rows[:2]], other)

    def test_symlinked_or_folder_log_refused_before_any_fetch(self):
        self.store.mkdir(mode=0o700)
        target = self.tmp / "elsewhere.csv"
        target.write_text(LOG_HEADER + "\n", encoding="utf-8")
        self.log.symlink_to(target)
        fake = fake_fetch()
        self.assertEqual(self.scan(day(0), fetch=fake), (1, ["SCAN-LOG.csv is a symlink"]))
        self.assertEqual(fake.call_count, 0)
        self.assertEqual(self.digest(day(0)), (1, ["SCAN-LOG.csv is a symlink"], None))
        self.assertEqual(target.read_text(encoding="utf-8"), LOG_HEADER + "\n")
        self.assertFalse((self.store / "LEADS.csv").exists() or (self.store / "MONITOR.csv").exists())
        self.log.unlink()
        self.log.mkdir()
        self.assertEqual(self.scan(day(0), fetch=fake), (1, ["SCAN-LOG.csv is not a file"]))
        self.assertEqual((fake.call_count, self.digest(day(0))[:2]), (0, (1, ["SCAN-LOG.csv is not a file"])))

    def test_ragged_or_wrong_header_log_fails_with_nothing_written_or_fetched(self):
        self.scan(day(0))
        monitor_csv = self.store / "MONITOR.csv"
        before = (monitor_csv.read_bytes(), self.watchlist.read_bytes())
        for text, why in ((LOG_HEADER + "\n%s,Gamma County,%s,refused\n" % (day(0), GAMMA), "row 2: columns have the wrong count"),
                          ("scan_date,agency,url,outcome\n", "row 1: header is not the accepted header")):
            self.log.write_text(text, encoding="utf-8")
            fake = fake_fetch()
            self.assertEqual(self.scan(day(1), fetch=fake), (1, ["SCAN-LOG.csv " + why]))
            self.assertEqual((fake.call_count, monitor_csv.read_bytes(), self.watchlist.read_bytes()), (0,) + before)
            self.assertEqual(self.log.read_text(encoding="utf-8"), text)
            self.assertEqual(self.digest(day(1)), (1, ["SCAN-LOG.csv " + why], None))

    def test_held_store_lock_leaves_log_unchanged(self):
        self.scan(day(0), {GAMMA: "http 503"})
        before = self.log.read_bytes()
        fd = os.open(self.store / ".monitor.lock", os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fake = fake_fetch()
        self.assertEqual(self.scan(day(1), fetch=fake), (1, ["store busy: another scan is running"]))
        self.assertEqual((fake.call_count, self.log.read_bytes()), (0, before))

    def test_unreachable_after_outside_2_to_30_is_a_usage_error(self):
        for value in ("1", "31", "0", "x", "3.0", "-3"):
            err = io.StringIO()
            with self.subTest(value=value), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
                monitor.main(["digest", "--store", str(self.store), "--date", day(0), "--unreachable-after", value])
            self.assertEqual(stop.exception.code, 2)
            self.assertIn("--unreachable-after", err.getvalue())
        self.assertFalse(self.store.exists())
        for value in ("2", "30"):
            self.assertEqual(self.digest(day(0), "--unreachable-after", value)[0], 0)

if __name__ == "__main__":
    unittest.main()
