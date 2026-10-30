import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_region.py"
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
SEED, SAMPLE = str(FIXTURES / "seed"), FIXTURES / "region-sample.json"
OWNED = {"FACTS.csv", "POLICY.csv", "REQUESTS.csv", "LAW.csv"}


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args, "--seed-dir", SEED], capture_output=True, text=True,
                          check=False)
    return done.returncode, done.stdout


def load_monitor():  # F7 helpers, loaded by file path like the tool does
    import importlib.util
    spec = importlib.util.spec_from_file_location("_test_region_monitor", ROOT / "tools" / "flock_monitor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FlockRegionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"
        self.region = self.store / "regions" / "sample-county"

    def files(self):
        return {p.relative_to(self.store).as_posix(): p.read_bytes() for p in sorted(self.store.rglob("*")) if p.is_file()}

    def mutated(self, name, change, region="sample-county"):
        cfg = json.loads(SAMPLE.read_text(encoding="utf-8"))
        cfg["region"] = region
        change(cfg["agencies"])
        path = self.tmp / name
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return str(path)

    def test_happy_path_new_check_init_idempotent(self):
        draft = str(self.tmp / "draft.json")
        code, out = run("new", "--county-fips", "06999", "--region", "t", "--out", draft)
        self.assertEqual(code, 0, out)
        agencies = json.loads(Path(draft).read_text(encoding="utf-8"))["agencies"]
        self.assertEqual([(a["seed_agency_id"], a["in_scope"]) for a in agencies],
                         [("ca-sample-county-sheriff", "hold"), ("ca-sample-city-police", "hold")])
        self.assertEqual(run("--check", draft), (0, "agencies=2 in_scope=0 hold=2 watch_rows=0 errors=0\n"))
        self.assertEqual(run("new", "--county-fips", "06999", "--region", "t", "--out", draft)[0], 1)  # no overwrite

        code, out = run("init", str(SAMPLE), "--store", str(self.store))
        self.assertEqual(code, 0, out)
        self.assertEqual(out, "agencies=3 folders_created=2 watch_rows=3 watch_dropped=0 roster_rows=3\n")
        monitor = load_monitor()
        watch = monitor.read_csv(self.region / "WATCHLIST.csv", monitor.WATCH_HEADER)
        self.assertEqual((len(watch), [e for _, row in watch for e in monitor.row_errors(row)]), (3, []))
        roster = (self.region / "ROSTER.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual((roster[0], len(roster)), ("watch_agency,slug,in_scope,applicability,site_slug", 4))
        plan = (self.region / "PLAN.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(plan[0], "Region plan - sample-county - private working checklist, not a publication")
        self.assertIn("HOLD: operator applicability line required before step 7", plan)

        before = self.files()
        self.assertEqual(run("init", str(SAMPLE), "--store", str(self.store))[1],
                         "agencies=3 folders_created=0 watch_rows=3 watch_dropped=0 roster_rows=3\n")
        self.assertEqual(self.files(), before)

        path = self.region / "WATCHLIST.csv"
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[2] = lines[2] + "a" * 64  # row 2 ends with the empty last_seen_hash column
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(run("init", str(SAMPLE), "--store", str(self.store))[0], 0)
        self.assertTrue(path.read_text(encoding="utf-8").splitlines()[2].endswith("," + "a" * 64))

    def test_fail_closed_mutations(self):
        def offdomain(a): a[0]["watch"][0]["url"] = "https://other.example/agendas"
        def nowhere(a): a[1]["seed_agency_id"] = "ca-nowhere-police"
        def no_applicability(a): a[0]["applicability"] = ""
        def dup_slug(a): a[2]["slug"] = "sample-county-sheriff"
        for change, agency in ((offdomain, 1), (nowhere, 2), (no_applicability, 1), (dup_slug, 3)):
            with self.subTest(change.__name__):
                bad = self.mutated(change.__name__ + ".json", change)
                code, out = run("--check", bad)
                self.assertEqual(code, 1)
                self.assertIn("agency %d: " % agency, out)
                self.assertEqual(run("init", bad, "--store", str(self.store))[0], 1)
        self.assertFalse(self.store.exists())

        self.assertEqual(run("init", str(SAMPLE), "--store", str(self.store))[0], 0)
        other = self.mutated("other.json", lambda a: a.pop(1), region="other-county")
        code, out = run("init", other, "--store", str(self.store))
        self.assertEqual(code, 1)
        self.assertIn("agency 1: slug already belongs to region sample-county", out)
        self.assertFalse((self.store / "regions" / "other-county").exists())

        code, out = run("init", str(SAMPLE), "--store", str(ROOT / "tests" / "store"))
        self.assertEqual((code, out), (1, "store inside repository\n"))
        self.assertFalse((ROOT / "tests" / "store").exists())

    def test_ownership_boundary(self):
        self.assertEqual(run("init", str(SAMPLE), "--store", str(self.store))[0], 0)
        created = [p.relative_to(self.store).as_posix() for p in self.store.rglob("*")]
        self.assertFalse(OWNED & {Path(p).name for p in created})
        allowed = ("regions", "regions/sample-county", "agencies", "agencies/sample-city-police",
                   "agencies/sample-county-sheriff")
        for path in created:
            self.assertTrue(path in allowed or path.startswith("regions/sample-county/"), path)
        self.assertEqual(oct(os.stat(self.region / "ROSTER.csv").st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
