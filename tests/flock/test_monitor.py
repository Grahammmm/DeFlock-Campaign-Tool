import contextlib
import csv
import hashlib
import importlib.util
import io
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
SPEC = importlib.util.spec_from_file_location("flock_monitor", ROOT / "tools" / "flock_monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)
DATE = "2026-10-30"
BODY = (FIXTURES / "index-sample.html").read_bytes()

class FakeFetch:  # records every fetch; never opens a socket
    def __init__(self, ctype="text/html; charset=utf-8", body=BODY):
        self.calls, self.ctype, self.body = [], ctype, body

    def __call__(self, url, official_domain):
        self.calls.append(url)
        return url, self.ctype, self.body

def rows(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))

class MonitorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.watchlist = Path(tmp.name) / "WATCHLIST.csv"
        self.store = Path(tmp.name) / "store"
        shutil.copy(FIXTURES / "watchlist-sample.csv", self.watchlist)

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = monitor.main(list(args))
        return code, out.getvalue().splitlines()

    def test_happy_path_records_links_dedupes_and_digests(self):
        fake = FakeFetch()
        lines = monitor.scan(self.watchlist, self.store, DATE, fetch=fake)
        self.assertEqual(lines, ["scanned=2 unchanged=0 new_items=3 refused=0 offdomain_links=4 skipped_social=1"])
        items = rows(self.store / "MONITOR.csv")
        self.assertEqual(sorted(r["url"] for r in items), ["https://www.city.example/agendas/2026-10-13",
                                                            "https://www.city.example/files/packet.pdf",
                                                            "https://www.city.example/news/cameras"])
        self.assertTrue(all(r["kind"] == "public" and r["status"] == "unreviewed" and r["seen_at"] == DATE for r in items))
        self.assertIn("Council agenda 2026-10-13", [r["title"] for r in items])
        page = hashlib.sha256(BODY).hexdigest()
        self.assertEqual([r["last_seen_hash"] for r in rows(self.watchlist)], [page, page, ""])
        again = monitor.scan(self.watchlist, self.store, "2026-10-31", fetch=fake)
        self.assertEqual(again, ["scanned=2 unchanged=2 new_items=0 refused=0 offdomain_links=0 skipped_social=1"])
        code, out = self.run_main("digest", "--store", str(self.store), "--date", DATE)
        self.assertEqual(code, 0)
        digest = Path(out[0]).read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(digest), 40)
        self.assertEqual(digest[0], "Flock monitor 2026-10-30: 3 new items (public, unreviewed), 0 relevant")
        self.assertEqual(len(digest), 4)
        code, out = self.run_main("digest", "--store", str(self.store), "--date", "2026-10-31")
        self.assertEqual(Path(out[0]).read_text(encoding="utf-8"), "Flock monitor 2026-10-31: no new items\n")

    def test_scope_fails_closed(self):
        text = self.watchlist.read_text(encoding="utf-8")
        self.watchlist.write_text(text.replace("www.city.example/news", "other.example/news"), encoding="utf-8")
        monitor.fetch, original = FakeFetch(), monitor.fetch
        self.addCleanup(setattr, monitor, "fetch", original)
        code, out = self.run_main("scan", str(self.watchlist), "--store", str(self.store), "--date", DATE)
        self.assertEqual((code, out, monitor.fetch.calls), (1, ["row 3: url host is outside official_domain"], []))
        self.watchlist.write_text(text.replace("www.city.example/news", "[invalid"), encoding="utf-8")
        code, out = self.run_main("scan", str(self.watchlist), "--store", str(self.store), "--date", DATE)
        self.assertEqual((code, out, monitor.fetch.calls), (1, ["row 3: url is not a parseable URL"], []))
        shutil.copy(FIXTURES / "watchlist-sample.csv", self.watchlist)
        lines = monitor.scan(self.watchlist, self.store, DATE, fetch=FakeFetch("application/pdf"))
        self.assertEqual(lines[:2], ["row %d: refused content-type not text/html" % n for n in (2, 3)])
        self.assertIn("new_items=0 refused=2", lines[2])
        self.assertFalse((self.store / "MONITOR.csv").exists())
        lines = monitor.scan(self.watchlist, self.store, DATE, fetch=FakeFetch(body=BODY + b'<a href="https://[x">Bad</a>'))
        self.assertEqual(lines, ["scanned=2 unchanged=0 new_items=3 refused=0 offdomain_links=6 skipped_social=1"])
        self.assertRaises(monitor.Refused, monitor.require, "city.example", "https://[x", "text/html")
        guard = monitor.RedirectGuard("city.example")
        for newurl in ("https://other.example/x", "http://www.city.example/x", "https://evilcity.example/x", "https://[x"):
            with self.subTest(newurl=newurl), self.assertRaises(monitor.Refused):
                guard.redirect_request(None, None, 302, "Found", {}, newurl)

    def test_pdf_link_is_recorded_but_never_fetched(self):
        fake = FakeFetch()
        monitor.scan(self.watchlist, self.store, DATE, fetch=fake)
        self.assertEqual(fake.calls, ["https://www.city.example/agendas", "https://www.city.example/news"])
        self.assertIn("https://www.city.example/files/packet.pdf", [r["url"] for r in rows(self.store / "MONITOR.csv")])

    def test_check_is_a_zero_network_dry_run_and_fails_closed(self):
        with mock.patch.object(urllib.request, "build_opener", side_effect=AssertionError), \
                mock.patch.object(monitor, "fetch", side_effect=AssertionError):
            before, stat = self.watchlist.read_bytes(), self.watchlist.stat().st_mtime_ns
            code, out = self.run_main("check", str(self.watchlist))
            self.assertEqual((code, out), (0, ["row 2: would fetch https://www.city.example/agendas",
                                               "row 3: would fetch https://www.city.example/news",
                                               "row 4: social, not fetched", "rows=3 fetch=2 social=1 errors=0"]))
            self.assertEqual((self.watchlist.read_bytes(), self.watchlist.stat().st_mtime_ns), (before, stat))
            self.assertEqual([p.name for p in self.watchlist.parent.iterdir()], ["WATCHLIST.csv"])  # no store, MONITOR.csv or .tmp
            text = before.decode("utf-8")
            for label, content, expect in (("pdf", text.replace("agendas", "agendas/a.pdf"), "row 2: url is a pdf"),
                                           ("header", text.replace("agency,", "agent,", 1), "row 1: header is not the accepted header")):
                with self.subTest(label):
                    self.watchlist.write_text(content, encoding="utf-8")
                    self.assertEqual(self.run_main("check", str(self.watchlist)), (1, [expect]))
            code, out = self.run_main("check", str(self.watchlist.with_name("missing.csv")))
            self.assertEqual((code, out), (1, ["io error: FileNotFoundError"]))

    def test_fetch_error_mapping(self):
        with mock.patch.object(urllib.request, "build_opener") as build:
            for error, text in ((ValueError("bad"), "malformed url"), (urllib.error.URLError("down"), "network URLError")):
                build.return_value.open.side_effect = error
                with self.subTest(text), self.assertRaisesRegex(monitor.Refused, "^%s$" % text):
                    monitor.fetch("https://www.city.example/agendas", "city.example")
            build.reset_mock()
            with self.assertRaises(monitor.Refused):
                monitor.fetch("http://www.city.example/agendas", "city.example")
            build.assert_not_called()

    def test_write_atomic_ignores_planted_fixed_temp(self):
        import os
        out = self.store / "OUT.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        old_tmp, sentinel = out.with_name("OUT.csv.tmp"), self.watchlist.parent / "SENTINEL"
        sentinel.write_bytes(b"sentinel\n")
        old_tmp.symlink_to(sentinel)
        monitor.write_atomic(out, header=["a", "b"], rows=[{"a": "1", "b": "2"}])
        self.assertEqual(sentinel.read_bytes(), b"sentinel\n")
        self.assertTrue(old_tmp.is_symlink())
        self.assertEqual(os.readlink(old_tmp), str(sentinel))
        self.assertFalse(out.is_symlink())
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(out.read_bytes(), b"a,b\n1,2\n")
        old_tmp.unlink()
        old_tmp.write_bytes(b"stale\n")
        old_tmp.chmod(0o644)
        monitor.write_atomic(out, "c\n")
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(out.stat().st_ino, old_tmp.stat().st_ino)
        self.assertEqual((old_tmp.read_bytes(), old_tmp.stat().st_mode & 0o777), (b"stale\n", 0o644))
        before, data, ino = sorted(os.listdir(out.parent)), out.read_bytes(), out.stat().st_ino
        with mock.patch.object(monitor.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                monitor.write_atomic(out, "d\n")
        self.assertEqual((out.read_bytes(), out.stat().st_ino), (data, ino))
        self.assertEqual(sorted(os.listdir(out.parent)), before)

if __name__ == "__main__":
    unittest.main()
