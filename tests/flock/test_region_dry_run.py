"""flock_region init --dry-run reports what init would change and writes nothing (RF1210-2)."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from tests.flock.test_region import ROOT, SAMPLE, run

R = "regions/sample-county/"
NEW = ["would create: agencies/sample-city-police/", "would create: agencies/sample-county-sheriff/",
       "would write: %sWATCHLIST.csv (new)" % R, "would write: %sROSTER.csv (new)" % R, "would write: %sPLAN.md (new)" % R,
       "agencies=3 folders_to_create=2 watch_rows=3 watch_to_drop=0 roster_rows=3 dry_run=1"]


class RegionDryRunTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"

    def tree(self):  # every path under the store with size, mtime and mode; never followed through links
        return {p.relative_to(self.tmp).as_posix(): (s.st_size, s.st_mtime_ns, s.st_mode)
                for p in sorted(self.tmp.rglob("*")) for s in [os.lstat(p)]}

    def config(self, change=lambda a: None, region="sample-county"):
        cfg = json.loads(SAMPLE.read_text(encoding="utf-8"))
        cfg["region"] = region
        change(cfg["agencies"])
        path = self.tmp / ("cfg-%d.json" % len(list(self.tmp.glob("cfg-*.json"))))
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return str(path)

    def init(self, config=str(SAMPLE), dry=True):
        return run("init", config, "--store", str(self.store), *(["--dry-run"] if dry else []))

    def dry_unchanged(self, config):  # dry run output, asserting the whole tree is untouched
        before = self.tree()
        code, out = self.init(config)
        self.assertEqual(self.tree(), before)
        return code, out.splitlines()

    def test_fresh_store_missing_and_existing(self):
        self.assertEqual(self.dry_unchanged(str(SAMPLE)), (0, NEW))
        self.assertFalse(self.store.exists())
        self.store.mkdir()
        (self.store / "note.txt").write_text("keep\n", encoding="utf-8")
        self.assertEqual(self.dry_unchanged(str(SAMPLE)), (0, NEW))
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), ["note.txt"])

    def test_after_init_unchanged_and_kept_hash_is_unchanged(self):
        self.assertEqual(self.init(dry=False)[0], 0)
        same = ["would write: %s%s (unchanged)" % (R, n) for n in ("WATCHLIST.csv", "ROSTER.csv", "PLAN.md")]
        tail = "agencies=3 folders_to_create=0 watch_rows=3 watch_to_drop=0 roster_rows=3 dry_run=1"
        self.assertEqual(self.dry_unchanged(str(SAMPLE)), (0, same + [tail]))
        watch = self.store / R / "WATCHLIST.csv"
        lines = watch.read_text(encoding="utf-8").splitlines()
        watch.write_text("\n".join(lines[:2] + [lines[2] + "b" * 64] + lines[3:]) + "\n", encoding="utf-8")
        self.assertEqual(self.dry_unchanged(str(SAMPLE)), (0, same + [tail]))  # init keeps the hash, so no change

    def test_dropped_rows_named_without_url_query_or_hash_then_real_init_matches(self):
        self.assertEqual(self.init(dry=False)[0], 0)
        watch = self.store / R / "WATCHLIST.csv"
        lines = watch.read_text(encoding="utf-8").splitlines()
        watch.write_text("\n".join(lines[:2] + [lines[2] + "c" * 64] + lines[3:]) + "\n", encoding="utf-8")
        old = watch.read_bytes()
        cfg = self.config(lambda a: a[0].update(watch=a[0]["watch"][:1]))
        code, out = self.dry_unchanged(cfg)
        self.assertEqual((code, out), (0, [
            "would write: %sWATCHLIST.csv (changed)" % R, "would write: %sROSTER.csv (unchanged)" % R,
            "would write: %sPLAN.md (unchanged)" % R, "would drop watch row: Sample City Police news",
            "would drop watch row: Sample City Police social",
            "agencies=3 folders_to_create=0 watch_rows=1 watch_to_drop=2 roster_rows=3 dry_run=1"]))
        text = "\n".join(out)
        for leak in ("https://", "city.example", "Sample City ALPR", "c" * 64):
            self.assertNotIn(leak, text)
        self.assertEqual(watch.read_bytes(), old)
        self.assertEqual(self.init(cfg, dry=False),
                         (0, "agencies=3 folders_created=0 watch_rows=1 watch_dropped=2 roster_rows=3\n"))
        self.assertNotIn("c" * 64, watch.read_text(encoding="utf-8"))

    def test_hold_flipped_to_no_creates_nothing_for_it(self):
        self.assertEqual(self.init(dry=False)[0], 0)
        (self.store / "agencies" / "sample-county-sheriff").rmdir()
        self.assertEqual(self.dry_unchanged(str(SAMPLE))[1][0], "would create: agencies/sample-county-sheriff/")
        code, out = self.dry_unchanged(self.config(lambda a: a[1].update(in_scope="no")))
        self.assertEqual((code, out), (0, [
            "would write: %sWATCHLIST.csv (unchanged)" % R, "would write: %sROSTER.csv (changed)" % R,
            "would write: %sPLAN.md (changed)" % R,
            "agencies=3 folders_to_create=0 watch_rows=3 watch_to_drop=0 roster_rows=3 dry_run=1"]))

    def assert_refused_like_init(self, config, line):
        code, out = self.dry_unchanged(config)
        self.assertEqual(code, 1)
        self.assertIn(line, out)
        self.assertTrue(out[-1].startswith("errors=") or len(out) == 1, out)
        before = self.tree()
        self.assertEqual(self.init(config, dry=False), (1, "\n".join(out) + "\n"))
        self.assertEqual(self.tree(), before)

    def test_refusals_unchanged(self):
        (self.store / "regions").mkdir(parents=True)
        (self.tmp / "elsewhere").mkdir()
        os.symlink(self.tmp / "elsewhere", self.store / "regions" / "sample-county")
        self.assert_refused_like_init(str(SAMPLE), "regions/sample-county: is a symlink; refusing")
        self.assertEqual(list((self.tmp / "elsewhere").iterdir()), [])
        os.unlink(self.store / "regions" / "sample-county")
        self.assertEqual(self.init(dry=False)[0], 0)
        other = self.config(lambda a: a.pop(1), region="other-county")
        self.assert_refused_like_init(other, "agency 1: slug already belongs to region sample-county")
        self.assertFalse((self.store / "regions" / "other-county").exists())

    def test_unreadable_watchlist_refused_like_init(self):
        self.assertEqual(self.init(dry=False)[0], 0)
        (self.store / R / "WATCHLIST.csv").write_text("not,the,header\n", encoding="utf-8")
        self.assert_refused_like_init(str(SAMPLE), "row 1: header is not the accepted header")

    def test_config_error(self):
        bad = self.config(lambda a: a[0].update(slug="Bad Slug"))
        self.assert_refused_like_init(bad, "agency 1: slug is not a lowercase slug")
        self.assertFalse(self.store.exists())

    def test_usage(self):
        self.assertEqual(run("new", "--county-fips", "06999", "--region", "t", "--out", str(self.tmp / "d.json"),
                             "--dry-run")[0], 2)
        self.assertFalse((self.tmp / "d.json").exists())
        self.assertEqual(run("--check", str(SAMPLE), "--dry-run")[0], 2)
        code, out = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("--dry-run", out)

    def test_store_inside_repository(self):
        code, out = run("init", str(SAMPLE), "--store", str(ROOT / "tests" / "store"), "--dry-run")
        self.assertEqual((code, out), (1, "store inside repository\n"))
        self.assertFalse((ROOT / "tests" / "store").exists())


if __name__ == "__main__":
    unittest.main()
