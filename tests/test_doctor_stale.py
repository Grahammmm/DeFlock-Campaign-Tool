"""doctor --stale-days N names inputs dated more than N days ago with their refresh command (read-only).

Offline: synthetic campaign, synthetic agency seed and law package bases, fixed today.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, discovery, freshness, law  # noqa: E402

TODAY = date(2026, 10, 31)
MEETINGS, MAP, SEED, LAW = "kit/meetings.json", "content/map/snapshot.json", "agency seed us-ca", "law package us-ca"


class FixedDate(date):
    @classmethod
    def today(cls):
        return TODAY


def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


class DoctorStaleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-doctor-stale-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "campaign"
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(self.root),
                                 "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.seed_dir, self.law_dir = base / "seeds", base / "jurisdictions"
        self.seed_dir.mkdir()
        (self.law_dir / "us-ca").mkdir(parents=True)
        self.seed = json.loads(discovery.seed_path("us-ca").read_text(encoding="utf-8"))
        self.write_seed(ago(10))
        self.package = json.loads(law.package_path("us-ca").read_text(encoding="utf-8"))
        for rule in [self.package["records_law"]] + self.package["rules"]:
            for source in rule["sources"]:
                source["accessed"] = ago(10)
        self.write_package()
        for patch in (mock.patch.object(discovery, "SEED_DIR", self.seed_dir),
                      mock.patch.object(law, "JURISDICTIONS", self.law_dir),
                      mock.patch.object(freshness, "date", FixedDate)):
            patch.start()
            self.addCleanup(patch.stop)

    def write_seed(self, generated):
        seed = dict(self.seed, generated=generated)
        (self.seed_dir / "us-ca.json").write_text(json.dumps(seed), encoding="utf-8")

    def write_package(self):
        (self.law_dir / "us-ca" / "package.json").write_text(json.dumps(self.package), encoding="utf-8")

    def write_meetings(self, text):
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(text, encoding="utf-8")

    def meetings_at(self, days, client="synthetic-county"):
        self.write_meetings(json.dumps({"schema_version": 1, "client": client, "meetings": [],
                                        "generated": ago(days) + "T17:00:00+00:00"}))

    def write_map(self, snapshot=None, cameras=True):
        map_dir = self.root / "content" / "map"
        map_dir.mkdir(parents=True, exist_ok=True)
        if cameras:
            (map_dir / "cameras.geojson").write_text('{"type": "FeatureCollection", "features": []}', encoding="utf-8")
        if snapshot is not None:
            (map_dir / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    def report(self, days=90):
        return cli.doctor_report(self.root, stale_days=days)

    def entries(self, days=90):
        return {e["input"]: e for e in self.report(days)["stale_inputs"]}

    def test_meetings_older_than_n_listed_with_command_and_fresh_not_listed(self):
        self.meetings_at(120)
        report = self.report()
        entry = {e["input"]: e for e in report["stale_inputs"]}[MEETINGS]
        self.assertEqual((entry["status"], entry["date"], entry["age_days"]), ("stale", ago(120), 120))
        self.assertEqual(entry["refresh"], f"python3 -m campaign_tool meetings --directory {self.root} --online "
                                           "--client synthetic-county")
        self.assertIn("Refresh stale inputs: " + MEETINGS, report["next_actions"])
        self.meetings_at(30)
        self.assertNotIn(MEETINGS, self.entries())

    def test_exactly_n_days_is_not_stale(self):
        self.meetings_at(90)
        self.write_seed(ago(90))
        self.write_map({"schema_version": 1, "osm_base": ago(90) + "T00:00:00Z", "source": "overpass"})
        entries = self.entries()
        for name in (MEETINGS, SEED, MAP):
            self.assertNotIn(name, entries)
        self.meetings_at(91)
        self.assertEqual(self.entries()[MEETINGS]["age_days"], 91)

    def test_meetings_symlink_invalid_or_undated_is_unknown_and_missing_is_absent(self):
        self.assertEqual(self.entries()[MEETINGS]["status"], "absent")
        self.assertFalse([a for a in self.report()["next_actions"] if a.startswith("Refresh stale inputs")])
        target = self.root / "old-meetings.json"
        target.write_text(json.dumps({"client": "x", "generated": ago(1) + "T00:00:00+00:00"}), encoding="utf-8")
        (self.root / "kit").mkdir(exist_ok=True)
        os.symlink(target, self.root / "kit" / "meetings.json")
        cases = [None, "{not json", json.dumps({"client": "x"}), json.dumps({"generated": "last week"}),
                 json.dumps({"generated": 20261001}), json.dumps(["generated"])]
        for text in cases:
            with self.subTest(text=text):
                if text is not None:
                    (self.root / "kit" / "meetings.json").unlink()
                    self.write_meetings(text)
                entry = self.entries()[MEETINGS]
                self.assertEqual((entry["status"], entry["date"]), ("unknown", None))
                self.assertIn("--online --client", entry["refresh"])
                self.assertIn("Refresh stale inputs: " + MEETINGS, self.report()["next_actions"])

    def test_map_snapshot_dates(self):
        self.assertEqual(self.entries()[MAP]["status"], "absent")
        self.write_map()  # cameras.geojson without snapshot.json: a pre-DF1507-09 map
        self.assertEqual((self.entries()[MAP]["status"], self.entries()[MAP]["date"]), ("unknown", None))
        self.write_map({"schema_version": 1, "osm_base": None, "source": "saved_file"})
        self.assertEqual(self.entries()[MAP]["status"], "unknown")
        self.write_map({"schema_version": 1, "osm_base": "yesterday", "source": "overpass"})
        self.assertEqual(self.entries()[MAP]["status"], "unknown")
        self.write_map({"schema_version": 1, "osm_base": ago(100) + "T03:04:05Z", "source": "overpass"})
        entry = self.entries()[MAP]
        self.assertEqual((entry["status"], entry["date"], entry["age_days"]), ("stale", ago(100), 100))
        self.assertTrue(entry["refresh"].startswith(f"python3 tools/fetch_osm_alprs.py --directory {self.root} "))
        self.write_map({"schema_version": 1, "osm_base": ago(5) + "T03:04:05Z", "source": "overpass"})
        self.assertNotIn(MAP, self.entries())

    def test_agency_seed_stale_unknown_and_absent(self):
        self.assertNotIn(SEED, self.entries())
        self.write_seed(ago(400))
        entry = self.entries()[SEED]
        self.assertEqual((entry["status"], entry["age_days"]), ("stale", 400))
        self.assertEqual(entry["refresh"], "update the shared agency seed (package release)")
        self.write_seed("2026-13-40")
        self.assertEqual(self.entries()[SEED]["status"], "unknown")
        (self.seed_dir / "us-ca.json").unlink()
        self.assertEqual(self.entries()[SEED]["status"], "absent")

    def test_law_source_older_than_n_and_package_missing_or_invalid(self):
        self.assertFalse([name for name in self.entries() if name.startswith("law")])
        rule = self.package["rules"][0]
        rule["sources"][0]["accessed"] = ago(200)
        self.write_package()
        law_entries = [e for e in self.report()["stale_inputs"] if e["input"].startswith("law")]
        self.assertEqual(len(law_entries), 1)
        self.assertEqual(law_entries[0], {
            "input": "law:" + rule["rule_id"], "status": "stale", "title": rule["sources"][0]["title"],
            "accessed": ago(200), "date": ago(200), "age_days": 200,
            "refresh": f"python3 -m campaign_tool.law review-sheet us-ca --as-of {TODAY} --out FILE.csv"})
        self.assertNotIn("law:" + rule["rule_id"], self.entries(200))
        (self.law_dir / "us-ca" / "package.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(self.entries()[LAW]["status"], "unknown")
        (self.law_dir / "us-ca" / "package.json").unlink()
        self.assertEqual(self.entries()[LAW]["status"], "absent")

    def test_sorted_and_capped_at_100(self):
        self.meetings_at(120)
        self.write_seed(ago(400))
        names = [e["input"] for e in self.report()["stale_inputs"]]
        self.assertEqual(names, sorted(names))
        many = [{"input": f"law:x-{i:03}", "status": "stale"} for i in range(103)]
        with mock.patch.object(freshness, "stale_inputs", return_value=many):
            report = self.report()
        self.assertEqual((len(report["stale_inputs"]), report["stale_inputs_more"]), (100, 3))

    def test_no_flag_unchanged_and_bad_values_exit_2(self):
        self.meetings_at(120)
        plain = cli.doctor_report(self.root)
        flagged = self.report()
        self.assertNotIn("stale_inputs", plain)
        self.assertIn(MEETINGS, [e["input"] for e in flagged.pop("stale_inputs")])
        self.assertEqual(flagged["next_actions"].pop(), "Refresh stale inputs: " + MEETINGS)
        self.assertEqual(plain, flagged)
        command = [sys.executable, "-B", "-m", "campaign_tool", "doctor", "--directory", str(self.root)]
        for value in ("0", "3651", "-5", "abc"):
            with self.subTest(value=value):
                result = subprocess.run(command + ["--stale-days", value], cwd=REPO_ROOT, capture_output=True,
                                        text=True, timeout=60)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--stale-days", result.stderr)
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("stale_inputs", json.loads(result.stdout))
        result = subprocess.run(command + ["--stale-days", "1"], cwd=REPO_ROOT, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stale_inputs", json.loads(result.stdout))

    def test_read_only(self):
        self.meetings_at(120)
        self.write_map({"schema_version": 1, "osm_base": None, "source": "overpass"})

        def listing():
            return sorted((str(p.relative_to(p.anchor)), p.lstat().st_mtime_ns, p.lstat().st_mode)
                          for top in (self.root, self.seed_dir, self.law_dir) for p in [top, *top.rglob("*")])
        before = listing()
        self.report()
        self.assertEqual(listing(), before)


if __name__ == "__main__":
    unittest.main()
