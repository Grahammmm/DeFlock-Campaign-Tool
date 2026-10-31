import contextlib
import csv
import hashlib
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
SPEC = importlib.util.spec_from_file_location("flock_monitor_links", ROOT / "tools" / "flock_monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)
DATE = "2026-10-30"
BODY = (FIXTURES / "index-sample.html").read_bytes()
SUMMARY = "scanned=2 unchanged=0 new_items=3 refused=0 offdomain_links=4 skipped_social=1"

class MonitorLinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.watchlist, self.store = Path(tmp.name), Path(tmp.name) / "WATCHLIST.csv", Path(tmp.name) / "store"
        shutil.copy(FIXTURES / "watchlist-sample.csv", self.watchlist)
        self.fetch = mock.Mock(side_effect=lambda url, official_domain: (url, "text/html; charset=utf-8", BODY))

    def run_main(self, *args):
        out = io.StringIO()
        with mock.patch.object(monitor, "fetch", self.fetch), contextlib.redirect_stdout(out):
            code = monitor.main(list(args))
        return code, out.getvalue().splitlines()

    def scan(self, watchlist=None, store=None):
        return self.run_main("scan", str(watchlist or self.watchlist), "--store", str(store or self.store), "--date", DATE)

    def digest(self, store=None):
        return self.run_main("digest", "--store", str(store or self.store), "--date", DATE)

    def link(self, name, target):
        os.symlink(target, self.tmp / name)
        return self.tmp / name

    def test_happy_path_scan_and_digest_unchanged(self):
        self.assertEqual(self.scan(), (0, [SUMMARY]))
        with self.watchlist.open(encoding="utf-8", newline="") as handle:
            hashes = {r["kind"]: r["last_seen_hash"] for r in csv.DictReader(handle)}
        self.assertEqual(hashes, {"agenda": hashlib.sha256(BODY).hexdigest(), "news": hashlib.sha256(BODY).hexdigest(), "social": ""})
        self.assertEqual(len((self.store / "MONITOR.csv").read_text(encoding="utf-8").splitlines()), 4)
        self.assertEqual(self.digest(), (0, [str((self.store / "digest" / (DATE + ".md")).resolve())]))
        self.assertTrue((self.store / "digest" / (DATE + ".md")).read_text(encoding="utf-8").startswith("Flock monitor"))

    def test_linked_watchlist_refused_before_fetch_link_and_target_unchanged(self):
        before, link = self.watchlist.read_bytes(), self.link("linked.csv", self.watchlist)
        self.assertEqual(self.scan(watchlist=link), (1, ["watchlist is a symlink"]))
        self.assertEqual((self.fetch.call_count, link.is_symlink(), self.watchlist.read_bytes()), (0, True, before))
        self.assertFalse(self.store.exists())

    def test_linked_monitor_csv_refused_before_fetch_target_unchanged(self):
        self.store.mkdir()
        target = self.tmp / "elsewhere.csv"
        target.write_text(",".join(monitor.MONITOR_HEADER) + "\n", encoding="utf-8")
        before = (target.read_bytes(), self.watchlist.read_bytes())
        for name, dest in (("real", target), ("dangling", self.tmp / "nowhere.csv")):
            with self.subTest(name):
                os.symlink(dest, self.store / "MONITOR.csv")
                self.assertEqual(self.scan(), (1, ["MONITOR.csv is a symlink"]))
                os.unlink(self.store / "MONITOR.csv")
        self.assertEqual((self.fetch.call_count, (target.read_bytes(), self.watchlist.read_bytes())), (0, before))
        self.assertFalse((self.tmp / "nowhere.csv").exists())

    def test_monitor_csv_folder_refused(self):
        (self.store / "MONITOR.csv").mkdir(parents=True)
        self.assertEqual((self.scan(), self.fetch.call_count), ((1, ["MONITOR.csv is not a file"]), 0))

    def test_linked_digest_folder_refused_target_empty(self):
        self.assertEqual(self.scan()[0], 0)
        other = self.tmp / "other"
        other.mkdir()
        for dest in (other, self.tmp / "absent"):
            with self.subTest(dest=dest.name):
                os.symlink(dest, self.store / "digest")
                self.assertEqual(self.digest(), (1, ["digest folder is a symlink"]))
                os.unlink(self.store / "digest")
        self.assertEqual((os.listdir(other), (self.tmp / "absent").exists()), ([], False))

    def test_digest_regular_file_refused_no_traceback(self):
        self.store.mkdir()
        (self.store / "digest").write_text("keep\n", encoding="utf-8")
        self.assertEqual(self.digest(), (1, ["digest folder is not a folder"]))
        self.assertEqual((self.store / "digest").read_text(encoding="utf-8"), "keep\n")

    def test_oversized_csv_field_ends_as_io_error(self):
        big = "x" * (csv.field_size_limit() + 1)
        bad = self.tmp / "BIG.csv"
        bad.write_text(self.watchlist.read_text(encoding="utf-8") + "Example City,agenda,https://www.city.example/" + big + ",,city.example,\n",
                       encoding="utf-8")
        self.assertEqual(self.scan(watchlist=bad), (1, ["io error: Error"]))
        self.assertEqual(self.run_main("check", str(bad)), (1, ["io error: Error"]))
        self.store.mkdir()
        (self.store / "MONITOR.csv").write_text(",".join(monitor.MONITOR_HEADER) + "\n" + big + "\n", encoding="utf-8")
        self.assertEqual(self.scan(), (1, ["io error: Error"]))
        self.assertEqual(self.digest(), (1, ["io error: Error"]))
        self.assertEqual((self.fetch.call_count, (self.store / "digest").exists()), (0, False))

    def test_linked_store_still_accepted(self):
        real = self.tmp / "real-store"
        real.mkdir()
        link = self.link("store-link", real)
        self.assertEqual(self.scan(store=link), (0, [SUMMARY]))
        self.assertEqual(self.digest(store=link), (0, [str(real.resolve() / "digest" / (DATE + ".md"))]))
        self.assertTrue((real / "MONITOR.csv").is_file() and link.is_symlink())

    def test_check_on_linked_watchlist_unchanged(self):
        link = self.link("linked.csv", self.watchlist)
        expected = self.run_main("check", str(self.watchlist))
        self.assertEqual(expected[0], 0)
        self.assertEqual(self.run_main("check", str(link)), expected)
        self.assertEqual(self.fetch.call_count, 0)

if __name__ == "__main__":
    unittest.main()
