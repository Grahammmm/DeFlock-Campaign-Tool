"""flock_region coverage: seed sheriff/police agencies of the county that the config does not list (RF1700-3)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_region.py"
FIXTURE_SEED = ROOT / "tests" / "fixtures" / "flock" / "seed" / "us-ca.json"
CITY = "ca-sample-city-police"


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def summary(listed, unlisted, other="chp:1,city_council:1", hand=0, seed=4):
    return "seed_agencies=%d listed=%d unlisted=%d other_unlisted=%s hand_added=%d" % (seed, listed, unlisted, other, hand)


class RegionCoverageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.seed_dir = self.tmp / "seed"
        self.seed_dir.mkdir()
        self.seed_file = self.seed_dir / "us-ca.json"
        self.seed_file.write_bytes(FIXTURE_SEED.read_bytes())
        self.cfg_path = self.tmp / "region.json"
        code, out, err = run("new", "--county-fips", "06999", "--region", "t", "--out", str(self.cfg_path),
                             "--seed-dir", str(self.seed_dir))
        self.assertEqual(code, 0, out + err)

    def coverage(self, *extra):
        return run("coverage", str(self.cfg_path), "--seed-dir", str(self.seed_dir), *extra)

    def edit_config(self, change):
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        change(cfg["agencies"])
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    def add_seed(self, *entries, fips="06999"):
        seed = json.loads(self.seed_file.read_text(encoding="utf-8"))
        template = seed["counties"][0]["agencies"][0]
        county = next((c for c in seed["counties"] if c["county_fips"] == fips), None)
        if county is None:
            county = dict(seed["counties"][0], county_fips=fips, name="Other", agencies=[])
            seed["counties"].append(county)
        for agency_id, kind, name in entries:
            county["agencies"].append(dict(template, agency_id=agency_id, kind=kind, name=name))
        self.seed_file.write_text(json.dumps(seed), encoding="utf-8")

    def assertCoverage(self, code, lines):
        got = self.coverage()
        self.assertEqual((got[0], got[1].splitlines()), (code, lines), got[2])
        self.assertNotIn("Traceback", got[1] + got[2])

    def test_unedited_draft_is_fully_listed(self):  # 1
        self.assertCoverage(0, [summary(2, 0)])

    def test_police_removed_by_hand_is_unlisted(self):  # 2
        self.edit_config(lambda a: a.pop(1))
        self.assertCoverage(3, ["unlisted %s police Sample City Police" % CITY, summary(1, 1)])
        self.edit_config(lambda a: a.pop(0))  # both gone: both named, in seed order
        self.assertCoverage(3, ["unlisted ca-sample-county-sheriff sheriff Sample County Sheriff",
                                "unlisted %s police Sample City Police" % CITY, summary(0, 2)])

    def test_out_of_scope_and_hold_still_listed(self):  # 3
        def mark(a):
            a[1]["in_scope"] = "no"
            a[0]["in_scope"] = "hold"
        self.edit_config(mark)
        self.assertCoverage(0, [summary(2, 0)])

    def test_duplicate_pointer_counts_once(self):
        def dup(a):
            a.append(dict(a[1], slug="sample-city-police-2", watch_agency="Sample City Police 2"))
        self.edit_config(dup)
        self.assertCoverage(0, [summary(2, 0)])

    def test_seed_gains_agencies_after_drafting(self):  # 4
        self.add_seed(("ca-sample-town-police", "police", "Sample Town Police"),
                      ("ca-sample-port-sheriff", "sheriff", "Sample Port Sheriff"))
        self.assertCoverage(3, ["unlisted ca-sample-town-police police Sample Town Police",
                                "unlisted ca-sample-port-sheriff sheriff Sample Port Sheriff", summary(2, 2, seed=6)])

    def test_new_agency_in_another_county_is_not_reported(self):
        self.add_seed(("ca-other-police", "police", "Other Police"), fips="06998")
        self.assertCoverage(0, [summary(2, 0)])

    def test_other_kinds_counted_not_failed(self):  # 5
        self.add_seed(("ca-sample-da", "district_attorney", "Sample DA"), ("ca-sample-board", "county_board", "Board"))
        other = "chp:1,city_council:1,county_board:1,district_attorney:1"
        self.assertCoverage(0, [summary(2, 0, other, seed=6)])
        self.edit_config(lambda a: a.append({  # a CHP office the organizer points at is listed, not counted
            "slug": "chp-sample", "watch_agency": "CHP Sample", "seed_agency_id": "ca-chp-sample-area", "in_scope": "no",
            "applicability": "", "official_domain": "", "site_slug": None, "watch": []}))
        self.assertCoverage(0, [summary(3, 0, "city_council:1,county_board:1,district_attorney:1", seed=6)])

    def test_all_other_kinds_listed_prints_zero(self):
        def point_all(a):
            for n, sid in enumerate(("ca-chp-sample-area", "ca-sample-city-council")):
                a.append({"slug": "x%d" % n, "watch_agency": "X%d" % n, "seed_agency_id": sid, "in_scope": "no",
                          "applicability": "", "official_domain": "", "site_slug": None, "watch": []})
        self.edit_config(point_all)
        self.assertCoverage(0, [summary(4, 0, "0")])

    def test_hand_added_agency_counted(self):  # 6
        self.edit_config(lambda a: a.append({
            "slug": "campus-police", "watch_agency": "Campus Police", "seed_agency_id": None,
            "seed_note": "campus department; not in the agency seed", "in_scope": "no", "applicability": "",
            "official_domain": "", "site_slug": None, "watch": []}))
        self.assertCoverage(0, [summary(2, 0, hand=1)])

    def test_config_failing_check_exits_1(self):  # 7
        self.edit_config(lambda a: a[1].update(seed_agency_id="ca-nowhere-police"))
        self.assertCoverage(1, ["agency 2: seed_agency_id is not an agency_id of this county in the seed",
                                "coverage: config has 1 error(s); run --check first"])
        self.add_seed(("ca-other-police", "police", "Other Police"), fips="06998")  # real seed id, wrong county
        self.edit_config(lambda a: a[1].update(seed_agency_id="ca-other-police"))
        self.assertCoverage(1, ["agency 2: seed_agency_id is not an agency_id of this county in the seed",
                                "coverage: config has 1 error(s); run --check first"])
        self.edit_config(lambda a: a[1].update(seed_agency_id=None))  # null without a seed_note is not hand-added
        self.assertCoverage(1, ["agency 2: seed_agency_id is null without a seed_note",
                                "coverage: config has 1 error(s); run --check first"])

    def test_seed_missing_or_unreadable_exits_1(self):  # 8
        named = ["config: jurisdiction is not cc-ss with a valid agency seed",
                 "agency 1: seed_agency_id is not an agency_id of this county in the seed",
                 "agency 2: seed_agency_id is not an agency_id of this county in the seed",
                 "coverage: config has 3 error(s); run --check first"]
        self.seed_file.write_text("{not json", encoding="utf-8")
        self.assertCoverage(1, named)
        self.seed_file.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")  # fails the seed schema
        self.assertCoverage(1, named)
        self.seed_file.rename(self.tmp / "gone.json")
        self.assertCoverage(1, named)
        self.cfg_path.write_text("[", encoding="utf-8")
        self.assertCoverage(1, ["config: cannot read (JSONDecodeError)"])
        self.cfg_path.unlink()
        self.assertCoverage(1, ["config: cannot read (FileNotFoundError)"])

    def test_seed_name_cleaned_and_cut(self):  # 9
        long = "Long " + "x" * 295
        self.add_seed(("ca-bell-police", "police", "Bell\x07 Police\nunlisted ca-fake police Fake\u202e"),
                      ("ca-long-police", "police", long))
        self.assertCoverage(3, ["unlisted ca-bell-police police Bell Policeunlisted ca-fake police Fake",
                                "unlisted ca-long-police police " + long[:120], summary(2, 2, seed=6)])

    def test_usage_errors_exit_2(self):  # 10
        cfg, seed = str(self.cfg_path), str(self.seed_dir)
        for args in (("coverage", cfg, "--check", cfg), ("coverage", cfg, "--dry-run"), ("coverage",),
                     ("coverage", "--seed-dir", seed), ("--check", cfg, "coverage")):
            with self.subTest(args=args):
                self.assertEqual(run(*args)[0], 2)

    def test_read_only(self):  # 11
        store = self.tmp / "store"
        code, out, err = run("init", str(self.cfg_path), "--store", str(store), "--seed-dir", str(self.seed_dir))
        self.assertEqual(code, 0, out + err)
        self.edit_config(lambda a: a.pop(1))
        before = {p: (p.read_bytes() if p.is_file() else None, os.lstat(p).st_mtime_ns, os.lstat(p).st_mode)
                  for p in sorted(self.tmp.rglob("*"))}
        self.assertEqual(self.coverage("--store", str(store))[0], 3)
        after = {p: (p.read_bytes() if p.is_file() else None, os.lstat(p).st_mtime_ns, os.lstat(p).st_mode)
                 for p in sorted(self.tmp.rglob("*"))}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
