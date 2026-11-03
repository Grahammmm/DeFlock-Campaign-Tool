"""RF1100-10: `flock_monitor.py check WATCHLIST --json` (flock-monitor-check-v1). Synthetic watch lists, no network."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from tests.flock.test_monitor import FIXTURES, monitor

HEADER = "agency,kind,url,query,official_domain,last_seen_hash\n"
HASH = "ab" * 32

class CheckJsonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.watchlist = self.dir / "WATCHLIST.csv"
        shutil.copy(FIXTURES / "watchlist-sample.csv", self.watchlist)
        no_net = (mock.patch.object(urllib.request, "build_opener", side_effect=AssertionError("network")),
                  mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")),
                  mock.patch.object(monitor, "fetch", side_effect=AssertionError("fetch")))
        for patcher in no_net:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = monitor.main(list(args))
        return code, out.getvalue()

    def check_json(self, path=None):
        code, out = self.run_main("check", str(path or self.watchlist), "--json")
        return code, json.loads(out), out

    def test_valid_list_rows_in_file_order_and_counts(self):
        before = self.watchlist.read_bytes()
        code, doc, out = self.check_json()
        self.assertEqual(code, 0)
        self.assertEqual(doc, {
            "schema": "flock-monitor-check-v1", "review_required": True, "network": False, "valid": True,
            "rows": [{"row": 2, "agency_slug": "example-city", "kind": "agenda", "action": "fetch",
                      "url": "https://www.city.example/agendas"},
                     {"row": 3, "agency_slug": "example-city", "kind": "news", "action": "fetch",
                      "url": "https://www.city.example/news"},
                     {"row": 4, "agency_slug": "example-city", "kind": "social", "action": "social", "url": None}],
            "errors": [], "counts": {"rows": 3, "fetch": 2, "social": 1, "errors": 0}})
        self.assertEqual(out, json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n")
        self.assertEqual(self.watchlist.read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["WATCHLIST.csv"])  # no store, no temp file
        _, text = self.run_main("check", str(self.watchlist))  # url is exactly the text-mode url
        self.assertEqual([r["url"] for r in doc["rows"] if r["url"]],
                         [line.split("would fetch ")[1] for line in text.splitlines() if "would fetch " in line])

    def bad_list(self):
        text = self.watchlist.read_text(encoding="utf-8")
        self.watchlist.write_text(text.replace("www.city.example/agendas", "www.city.example/agendas/a.pdf")
                                  .replace("www.city.example/news", "other.example/news"), encoding="utf-8")

    def test_two_bad_rows_are_errors_with_rows_absent_exit_1(self):
        self.bad_list()
        code, doc, _ = self.check_json()
        self.assertEqual(code, 1)
        self.assertNotIn("rows", doc)
        self.assertFalse(doc["valid"])
        self.assertEqual(doc["errors"], [{"row": 2, "field": "url", "why": "is a pdf"},
                                         {"row": 3, "field": "url", "why": "host is outside official_domain"}])
        self.assertEqual(doc["counts"], {"rows": 3, "fetch": 2, "social": 1, "errors": 2})
        self.assertEqual(doc["schema"], "flock-monitor-check-v1")

    def test_every_row_error_why_matches_text_mode(self):
        self.watchlist.write_text(HEADER + " ,podcast,http://x.example/a,q,X.example,zz\n", encoding="utf-8")
        code, doc, _ = self.check_json()
        _, text = self.run_main("check", str(self.watchlist))
        self.assertEqual(code, 1)
        self.assertEqual(["row %d: %s %s" % (e["row"], e["field"], e["why"]) for e in doc["errors"]], text.splitlines())
        self.assertEqual(len(doc["errors"]), 7)
        self.assertEqual(doc["counts"], {"rows": 1, "fetch": 0, "social": 0, "errors": 7})  # unknown kind: not fetch

    def test_bad_list_text_mode_is_unchanged(self):
        self.bad_list()
        self.assertEqual(self.run_main("check", str(self.watchlist)),
                         (1, "row 2: url is a pdf\nrow 3: url host is outside official_domain\n"))
        shutil.copy(FIXTURES / "watchlist-sample.csv", self.watchlist)
        self.assertEqual(self.run_main("check", str(self.watchlist)), (0, (
            "row 2: would fetch https://www.city.example/agendas\nrow 3: would fetch https://www.city.example/news\n"
            "row 4: social, not fetched\nrows=3 fetch=2 social=1 errors=0\n")))

    def test_file_level_refusals_stay_one_text_line(self):
        link = self.dir / "LINK.csv"
        link.symlink_to(self.watchlist)
        wrong = self.dir / "WRONG.csv"
        wrong.write_text(self.watchlist.read_text(encoding="utf-8").replace("agency,", "agent,", 1), encoding="utf-8")
        short = self.dir / "SHORT.csv"
        short.write_text(HEADER + "Example City,agenda\n", encoding="utf-8")
        folder = self.dir / "FOLDER.csv"
        folder.mkdir()
        for path, line in ((link, "watchlist is a symlink"), (wrong, "row 1: header is not the accepted header"),
                           (self.dir / "missing.csv", "io error: FileNotFoundError"),
                           (short, "row 2: columns have the wrong count"), (folder, "watchlist is not a file")):
            with self.subTest(path.name):
                self.assertEqual(self.run_main("check", str(path), "--json"), (1, line + "\n"))

    def test_only_slug_never_agency_text_query_or_hash(self):
        self.watchlist.write_text(HEADER + "Canary PD Q7,agenda,https://www.city.example/a,,city.example,%s\n"
                                  "Canary PD Q7,social,,QueryCanary Z9,city.example,\n" % HASH, encoding="utf-8")
        code, doc, out = self.check_json()
        self.assertEqual((code, [r["agency_slug"] for r in doc["rows"]]), (0, ["canary-pd-q7", "canary-pd-q7"]))
        for canary in ("Canary PD", "QueryCanary", HASH, "query", "last_seen_hash", "agency\""):
            self.assertNotIn(canary, out)
        self.bad_list_with_canaries()
        code, doc, out = self.check_json()
        self.assertEqual(code, 1)
        for canary in ("Canary PD", "QueryCanary", "A" * 64):
            self.assertNotIn(canary, out)

    def bad_list_with_canaries(self):
        self.watchlist.write_text(HEADER + "Canary PD Q7,agenda,https://other.example/a,,city.example,\n"
                                  "Canary PD Q7,social,,QueryCanary Z9,city.example,%s\n" % ("A" * 64), encoding="utf-8")

    def test_json_is_check_only(self):
        store = self.dir / "store"
        for args in (("scan", str(self.watchlist), "--store", str(store), "--json"),
                     ("digest", "--store", str(store), "--date", "2026-10-30", "--json"),
                     ("leads", "--store", str(store), "--json")):
            with self.subTest(args[0]), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                self.run_main(*args)
            self.assertEqual(caught.exception.code, 2)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["WATCHLIST.csv"])

if __name__ == "__main__":
    unittest.main()
