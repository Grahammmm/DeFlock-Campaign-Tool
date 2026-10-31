import contextlib
import csv
import fcntl
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
SPEC = importlib.util.spec_from_file_location("flock_monitor_lock", ROOT / "tools" / "flock_monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)
DATE = "2026-10-30"
BODY = (FIXTURES / "index-sample.html").read_bytes()
SUMMARY = "scanned=2 unchanged=0 new_items=3 refused=0 offdomain_links=4 skipped_social=1"

def fake_fetch(error=None):  # records every fetch; never opens a socket
    return mock.Mock(side_effect=error or (lambda url, official_domain: (url, "text/html; charset=utf-8", BODY)))

class MonitorLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.watchlist, self.store = Path(tmp.name), Path(tmp.name) / "WATCHLIST.csv", Path(tmp.name) / "store"
        self.lock = self.store / ".monitor.lock"
        shutil.copy(FIXTURES / "watchlist-sample.csv", self.watchlist)

    def run_main(self, *args, fetch=None):
        out = io.StringIO()
        with mock.patch.object(monitor, "fetch", fetch or fake_fetch()), contextlib.redirect_stdout(out):
            code = monitor.main(list(args))
        return code, out.getvalue().splitlines()

    def scan_main(self, fetch):
        return self.run_main("scan", str(self.watchlist), "--store", str(self.store), "--date", DATE, fetch=fetch)

    def flock(self, unlock=True):  # lock from a separate open file description, as another scan would
        self.store.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # BlockingIOError if a scan still holds it
        return fcntl.flock(fd, fcntl.LOCK_UN) if unlock else fd

    def snapshot(self):
        return [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.watchlist, self.store / "MONITOR.csv")]

    def test_happy_path_creates_private_lock_and_releases_it(self):
        fake = fake_fetch()
        self.assertEqual((monitor.scan(self.watchlist, self.store, DATE, fetch=fake), fake.call_count), ([SUMMARY], 2))
        with (self.store / "MONITOR.csv").open(encoding="utf-8", newline="") as handle:
            self.assertEqual(sorted(r["url"] for r in csv.DictReader(handle)), [
                "https://www.city.example/agendas/2026-10-13", "https://www.city.example/files/packet.pdf",
                "https://www.city.example/news/cameras"])
        self.assertEqual(self.lock.stat().st_mode & 0o777, 0o600)
        self.flock()

    def test_held_lock_refuses_before_reading_or_fetching(self):
        monitor.scan(self.watchlist, self.store, DATE, fetch=fake_fetch())
        before, fd, fake = self.snapshot(), self.flock(unlock=False), fake_fetch()
        with mock.patch.object(monitor, "read_csv", wraps=monitor.read_csv) as read:
            self.assertEqual(self.scan_main(fake), (1, ["store busy: another scan is running"]))
        self.assertEqual([c.args[0] for c in read.call_args_list], [str(self.watchlist)])  # MONITOR.csv never read
        self.assertEqual((fake.call_count, self.snapshot()), (0, before))
        fcntl.flock(fd, fcntl.LOCK_UN)  # the other scan finishes: a retried scan now runs
        self.assertEqual(self.scan_main(fake_fetch())[1], ["scanned=2 unchanged=2 new_items=0 refused=0 offdomain_links=0 "
                                                           "skipped_social=1"])

    def test_lock_released_when_fetch_fails(self):
        fake = fake_fetch(OSError("disk"))
        self.assertEqual((self.scan_main(fake), fake.call_count), ((1, ["io error: OSError"]), 1))
        self.assertFalse((self.store / "MONITOR.csv").exists())
        self.flock()

    def test_stale_lock_file_from_dead_process_is_reused(self):
        self.store.mkdir(mode=0o700)
        self.lock.write_bytes(b"4242\n")
        self.assertEqual(self.scan_main(fake_fetch()), (0, [SUMMARY]))
        self.assertEqual(self.lock.read_bytes(), b"4242\n")  # reused as-is, never deleted or rewritten
        self.flock()

    def test_symlinked_lock_is_refused_target_untouched(self):
        target = self.tmp / "TARGET"
        for exists in (True, False):  # a symlink to a file outside the store, then a dangling one
            with self.subTest(exists=exists):
                target.write_bytes(b"target\n") if exists else target.unlink()
                self.store.mkdir(mode=0o700, exist_ok=True)
                self.lock.unlink(missing_ok=True)
                self.lock.symlink_to(target)
                fake = fake_fetch()
                self.assertEqual((self.scan_main(fake), fake.call_count), ((1, ["io error: OSError"]), 0))
                self.assertEqual(target.read_bytes() if exists else target.exists(), b"target\n" if exists else False)
                self.assertFalse((self.store / "MONITOR.csv").exists())

    def test_missing_store_is_created_private(self):
        self.store, self.lock = self.tmp / "a" / "store", self.tmp / "a" / "store" / ".monitor.lock"
        self.assertEqual(self.scan_main(fake_fetch()), (0, [SUMMARY]))
        self.assertEqual((self.store.stat().st_mode & 0o777, self.lock.stat().st_mode & 0o777), (0o700, 0o600))

    def test_bad_watchlist_fails_before_any_lock(self):
        text = self.watchlist.read_text(encoding="utf-8")
        self.watchlist.write_text(text.replace("www.city.example/news", "other.example/news"), encoding="utf-8")
        fake = fake_fetch()
        self.assertEqual(self.scan_main(fake), (1, ["row 3: url host is outside official_domain"]))
        self.assertEqual((fake.call_count, self.store.exists()), (0, False))

    def test_check_and_digest_take_no_lock(self):
        monitor.scan(self.watchlist, self.store, DATE, fetch=fake_fetch())
        self.flock(unlock=False)
        self.assertEqual(self.run_main("check", str(self.watchlist))[0], 0)
        code, out = self.run_main("digest", "--store", str(self.store), "--date", DATE)
        self.assertEqual((code, Path(out[0]).read_text(encoding="utf-8").splitlines()[0]),
                         (0, "Flock monitor 2026-10-30: 3 new items (public, unreviewed)"))

if __name__ == "__main__":
    unittest.main()
