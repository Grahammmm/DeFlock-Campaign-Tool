"""flock_region --check CONFIG --json: the flock-region-check-v1 document (RF0740-12)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.flock.test_region import SAMPLE, SEED, TOOL

SAMPLE_TOTALS = "agencies=3 in_scope=1 hold=1 watch_rows=3 errors=0\n"
BAD_ERRORS = [{"where": "agency 1", "field": "slug", "why": "is not a lowercase slug"},
              {"where": "agency 1", "field": "watch 1", "why": "url host is outside official_domain"},
              {"where": "agency 3", "field": "watch_agency", "why": "is duplicated"}]


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args, "--seed-dir", SEED], capture_output=True, text=True,
                          check=False)
    return done.returncode, done.stdout, done.stderr


class RegionCheckJsonTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def config(self, name, change=None, raw=None):
        cfg = json.loads(SAMPLE.read_text(encoding="utf-8"))
        if change: change(cfg)
        path = self.tmp / name
        path.write_text(raw if raw is not None else json.dumps(cfg), encoding="utf-8")
        return str(path)

    def bad_config(self):
        def change(cfg):
            a = cfg["agencies"]
            a[0]["slug"], a[0]["watch"][0]["url"] = "Bad Slug", "https://other.example/agendas"
            a[2]["watch_agency"] = a[0]["watch_agency"]
        return self.config("bad.json", change)

    def check_json(self, path):
        code, out, err = run("--check", path, "--json")
        self.assertEqual(err, "")
        self.assertTrue(out.endswith("}\n") and out.isascii(), out)
        return code, json.loads(out), out

    def assert_matches_text(self, path, doc):  # each error object is one text line, in order; counts = totals line
        code, out, _ = run("--check", path)
        lines, c = out.splitlines(), doc["counts"]
        self.assertEqual(["%(where)s: %(field)s %(why)s" % e for e in doc["errors"]], lines[:-1])
        self.assertEqual(lines[-1], "agencies=%d in_scope=%d hold=%d watch_rows=%d errors=%d" % (
            c["agencies"], c["in_scope_yes"], c["hold"], c["watch_rows"], c["errors"]))
        return code

    def test_valid_sample(self):  # 1
        code, doc, out = self.check_json(str(SAMPLE))
        self.assertEqual((code, doc), (0, {"schema": "flock-region-check-v1", "errors": [], "counts": {
            "agencies": 3, "in_scope_yes": 1, "hold": 1, "no": 1, "watch_rows": 3, "errors": 0}}))
        self.assertEqual(out, json.dumps(doc, indent=2, ensure_ascii=True) + "\n")
        self.assertEqual(list(doc), ["schema", "errors", "counts"])
        self.assertEqual(self.assert_matches_text(str(SAMPLE), doc), 0)

    def test_three_errors_one_for_one(self):  # 2
        path = self.bad_config()
        code, doc, _ = self.check_json(path)
        self.assertEqual((code, doc["errors"]), (1, BAD_ERRORS))
        self.assertEqual(doc["counts"], {"agencies": 3, "in_scope_yes": 1, "hold": 1, "no": 1, "watch_rows": 3,
                                         "errors": 3})
        self.assertEqual(self.assert_matches_text(path, doc), 1)

    def test_no_counted_only_in_json(self):  # 3
        def all_no(cfg):
            for a in cfg["agencies"]: a["in_scope"] = "no"
        path = self.config("no.json", all_no)
        code, doc, _ = self.check_json(path)
        self.assertEqual((code, doc["errors"]), (0, []))
        self.assertEqual((doc["counts"]["no"], doc["counts"]["in_scope_yes"], doc["counts"]["hold"]), (3, 0, 0))
        self.assertEqual(run("--check", path)[:2], (0, "agencies=3 in_scope=0 hold=0 watch_rows=3 errors=0\n"))

    def test_unreadable_config_no_json(self):  # 4
        for name, raw, text in (("broken.json", "{", "config: cannot read (JSONDecodeError)\n"),
                                ("list.json", '{"agencies": {}}', "config: agencies is not a list\n"),
                                ("array.json", "[]", "config: agencies is not a list\n")):
            with self.subTest(name):
                path = self.config(name, raw=raw)
                self.assertEqual(run("--check", path, "--json"), (1, text, ""))
                self.assertEqual(run("--check", path), (1, text, ""))
        self.assertEqual(run("--check", str(self.tmp / "missing.json"), "--json"),
                         (1, "config: cannot read (FileNotFoundError)\n", ""))

    def test_non_object_agency(self):  # 5
        path = self.config("x.json", lambda cfg: cfg["agencies"].append("x"))
        code, doc, _ = self.check_json(path)
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", json.dumps(doc))
        self.assertTrue(doc["errors"] and all(e["where"] == "agency 4" for e in doc["errors"]), doc)
        self.assertEqual(doc["counts"]["agencies"], 4)
        self.assertEqual(doc["counts"]["errors"], len(doc["errors"]))
        self.assertEqual(self.assert_matches_text(path, doc), 1)

    def test_usage_errors(self):  # 6
        cfg, out = str(SAMPLE), str(self.tmp / "n.json")
        for args, why in ((("new", "--county-fips", "06999", "--region", "t", "--out", out, "--json"), "--json is valid"),
                          (("init", cfg, "--store", str(self.tmp / "s"), "--json"), "--json is valid"),
                          (("init", cfg, "--store", str(self.tmp / "s"), "--json", "--dry-run"), "--json is valid"),
                          (("--check", cfg, "--json", "--dry-run"), "--dry-run is valid only with init")):
            with self.subTest(args=args):
                code, stdout, err = run(*args)
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn(why, err)
        self.assertIn("--json is valid only with --check or coverage", run("init", cfg, "--store", "s", "--json")[2])
        self.assertEqual(sorted(self.tmp.iterdir()), [])

    def test_text_output_unchanged(self):  # 7 byte-identical to main for the sample and the error config
        self.assertEqual(run("--check", str(SAMPLE)), (0, SAMPLE_TOTALS, ""))
        self.assertEqual(run("--check", self.bad_config()), (1, "".join(
            "%(where)s: %(field)s %(why)s\n" % e for e in BAD_ERRORS) + "agencies=3 in_scope=1 hold=1 watch_rows=3 "
            "errors=3\n", ""))


if __name__ == "__main__":
    unittest.main()
