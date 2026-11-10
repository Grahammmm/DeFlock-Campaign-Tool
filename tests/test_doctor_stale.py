"""doctor --stale-days N names inputs dated more than N days ago with their refresh command (read-only).

Offline: synthetic campaign, synthetic agency seed and law package bases, fixed today.
"""
import contextlib
import io
import json
import os
import socket
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


def ahead(days):
    return (TODAY + timedelta(days=days)).isoformat()


FUTURE = "Check the source date and local clock; then "


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

    # --- future-dated inputs (RF0200-3) ---

    def future_everywhere(self, days=3):
        self.meetings_at(-days)
        self.write_seed(ahead(days))
        self.write_map({"schema_version": 1, "osm_base": ahead(days) + "T00:00:00Z", "source": "overpass"})
        self.package["records_law"]["sources"][0]["accessed"] = ahead(days)
        self.package["rules"][0]["sources"][0]["accessed"] = ahead(days)
        self.write_package()

    def test_future_meetings_map_and_seed_are_reported(self):
        self.write_map({"schema_version": 1, "osm_base": ahead(1) + "T00:00:00Z", "source": "overpass"})
        self.write_seed(ahead(4000))
        self.meetings_at(-1)
        for days in (1, 90, 3650):
            with self.subTest(stale_days=days):
                entries = self.entries(days)
                for name, day, age in ((MEETINGS, ahead(1), -1), (MAP, ahead(1), -1), (SEED, ahead(4000), -4000)):
                    self.assertEqual((entries[name]["status"], entries[name]["date"], entries[name]["age_days"]),
                                     ("future", day, age), name)
        # Today is fresh, whatever the clock time or offset; only the calendar day counts.
        self.write_meetings(json.dumps({"generated": TODAY.isoformat() + "T23:59:59+00:00"}))
        self.write_seed(TODAY.isoformat())
        self.write_map({"schema_version": 1, "osm_base": TODAY.isoformat() + "T23:59:59Z", "source": "overpass"})
        entries = self.entries(1)
        for name in (MEETINGS, MAP, SEED):
            self.assertNotIn(name, entries)
        self.write_meetings(json.dumps({"generated": ahead(1) + "T00:30:00+14:00"}))
        self.assertEqual(self.entries(1)[MEETINGS]["date"], ahead(1))
        # A scheduled meeting's event date is not the scan date: only `generated` is aged.
        self.write_meetings(json.dumps({"generated": ago(1) + "T00:00:00+00:00",
                                        "meetings": [{"date": ahead(30), "title": "Synthetic council"}]}))
        self.assertNotIn(MEETINGS, self.entries(90))
        self.assertNotIn("future", {e["status"] for e in self.report(90)["stale_inputs"]})
        # Unreadable or undated stays unknown, never future.
        self.write_meetings(json.dumps({"generated": "2027-99-99T00:00:00+00:00"}))
        self.assertEqual(self.entries()[MEETINGS]["status"], "unknown")

    def test_future_law_sources_include_records_law_and_each_rule_source(self):
        rule = self.package["rules"][0]
        fresh, future_rule = dict(rule["sources"][0], title="Fresh"), dict(rule["sources"][0], title="Later")
        stale = dict(rule["sources"][0], title="Old")
        fresh["accessed"], future_rule["accessed"], stale["accessed"] = ago(1), ahead(2), ago(200)
        rule["sources"] = [fresh, future_rule, stale]
        self.package["records_law"]["sources"][0]["accessed"] = ahead(7)
        self.write_package()
        rows = [e for e in self.report()["stale_inputs"] if e["input"].startswith("law:")]
        key = lambda e: (e["input"], e["title"])  # noqa: E731
        self.assertEqual(sorted(rows, key=key), rows)
        worksheet = f"python3 -m campaign_tool.law review-sheet us-ca --as-of {TODAY} --out FILE.csv"
        self.assertEqual(rows, [
            {"input": "law:" + rule["rule_id"], "status": "future", "title": "Later", "accessed": ahead(2),
             "date": ahead(2), "age_days": -2, "refresh": FUTURE + worksheet},
            {"input": "law:" + rule["rule_id"], "status": "stale", "title": "Old", "accessed": ago(200),
             "date": ago(200), "age_days": 200, "refresh": worksheet},
            {"input": "law:records_law", "status": "future",
             "title": self.package["records_law"]["sources"][0]["title"], "accessed": ahead(7),
             "date": ahead(7), "age_days": -7, "refresh": FUTURE + worksheet}])
        # The legal worksheet is untouched: its stale list still counts only dates older than N.
        _rows, stale_list = law.review_sheet(law.load_package("us-ca"), TODAY, 90)
        self.assertEqual([s["title"] for s in stale_list], ["Old"])

    def test_future_refresh_instructions_keep_original_commands(self):
        self.future_everywhere()
        self.meetings_at(200)  # stale: refresh text unchanged
        stale = self.entries()[MEETINGS]
        self.assertEqual(stale["refresh"], f"python3 -m campaign_tool meetings --directory {self.root} --online "
                                           "--client synthetic-county")
        self.meetings_at(-3)
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("no command is run")), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("no network")):
            entries = self.entries()
        self.assertEqual(entries[MEETINGS]["refresh"], FUTURE + f"python3 -m campaign_tool meetings --directory "
                                                              f"{self.root} --online --client synthetic-county")
        self.assertEqual(entries[MAP]["refresh"], FUTURE + freshness.MAP_REFRESH.format(self.root))
        self.assertEqual(entries[SEED]["refresh"], FUTURE + "update the shared agency seed (package release)")
        worksheet = f"python3 -m campaign_tool.law review-sheet us-ca --as-of {TODAY} --out FILE.csv"
        for name in ("law:records_law", "law:" + self.package["rules"][0]["rule_id"]):
            self.assertEqual(entries[name]["refresh"], FUTURE + worksheet)
        self.assertEqual(FUTURE, "Check the source date and local clock; then ")
        # Unknown and absent rows never carry the prefix.
        self.write_meetings("{not json")
        self.assertFalse(self.entries()[MEETINGS]["refresh"].startswith(FUTURE))
        (self.root / "kit" / "meetings.json").unlink()
        self.assertEqual(self.entries()[MEETINGS]["status"], "absent")
        self.assertFalse(self.entries()[MEETINGS]["refresh"].startswith(FUTURE))

    def test_future_dates_share_sorting_cap_and_recovery(self):
        rule = self.package["rules"][0]
        template = rule["sources"][0]
        rule["sources"] = [dict(template, title=f"Source {i:03}", accessed=ahead(1 + i)) for i in range(105)]
        self.write_package()
        self.meetings_at(120)
        self.write_seed(ahead(2))
        (self.root / "content" / "map").mkdir(parents=True, exist_ok=True)
        (self.root / "content" / "map" / "snapshot.json").write_text("{not json", encoding="utf-8")
        report = self.report()
        total = 105 + 3  # sources + meetings + seed + unknown map
        self.assertEqual((len(report["stale_inputs"]), report["stale_inputs_more"]), (100, total - 100))
        keys = [(e["input"], e.get("title", "")) for e in report["stale_inputs"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual({e["status"] for e in report["stale_inputs"]}, {"future", "stale", "unknown"})
        # Fixing only some dates removes only the repaired entries.
        for source in rule["sources"][:100]:
            source["accessed"] = ago(5)
        self.write_seed(ago(5))
        self.write_package()
        remaining = self.report()["stale_inputs"]
        self.assertEqual(sorted(e["title"] for e in remaining if e["input"].startswith("law:")),
                         [f"Source {i:03}" for i in range(100, 105)])
        self.assertNotIn(SEED, {e["input"] for e in remaining})
        self.assertEqual({e["input"] for e in remaining} - {e["input"] for e in remaining if e["input"].startswith("law:")},
                         {MEETINGS, MAP})
        self.assertNotIn("stale_inputs_more", self.report())

    def test_future_report_is_read_only_and_opt_in(self):
        self.future_everywhere()

        def listing():
            return sorted((str(p.relative_to(p.anchor)), p.lstat().st_mtime_ns, p.lstat().st_mode,
                           p.read_bytes() if p.is_file() else None)
                          for top in (self.root, self.seed_dir, self.law_dir) for p in [top, *top.rglob("*")])
        before = listing()
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no network")):
            report = self.report()
            plain = cli.doctor_report(self.root)
        self.assertEqual(listing(), before)
        self.assertEqual({e["status"] for e in report["stale_inputs"]}, {"future"})
        self.assertNotIn("stale_inputs", plain)
        # The real command line: still exit 0 with a future-dated scan; no flag, no stale_inputs.
        self.write_meetings(json.dumps({"generated": (date.today() + timedelta(days=2)).isoformat() + "T00:00:00+00:00"}))
        command = [sys.executable, "-B", "-m", "campaign_tool", "doctor", "--directory", str(self.root)]
        result = subprocess.run(command + ["--stale-days", "90"], cwd=REPO_ROOT, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        meetings = {e["input"]: e for e in json.loads(result.stdout)["stale_inputs"]}[MEETINGS]
        self.assertEqual((meetings["status"], meetings["age_days"]), ("future", -2))
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("stale_inputs", json.loads(result.stdout))

    def test_future_inputs_project_into_local_readiness_counts(self):
        self.future_everywhere()
        config = json.loads((self.root / "campaign.json").read_text(encoding="utf-8"))
        (self.root / "campaign.json").write_text(json.dumps(dict(config, campaign_id="synthetic-campaign")),
                                                 encoding="utf-8")
        report = self.report()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), mock.patch.object(socket, "socket", side_effect=AssertionError):
            cli.readiness(self.root, campaign_id="synthetic-campaign", stale_days=90)
        snapshot = json.loads(buffer.getvalue())
        self.assertEqual(snapshot["counts"]["stale_inputs"], len(report["stale_inputs"]) + report.get("stale_inputs_more", 0))
        self.assertEqual(snapshot["counts"]["stale_inputs"], 5)
        action = [a for a in snapshot["next_actions"] if a.startswith("Refresh stale inputs: ")]
        self.assertEqual(len(action), 1)
        for name in (MEETINGS, MAP, SEED, "law:records_law"):
            self.assertIn(name, action[0])
        for detail in (self.package["records_law"]["sources"][0]["title"], FUTURE, "age_days"):
            self.assertNotIn(detail, buffer.getvalue())

    # --- DF1107-13: dated objects in the public content/agencies.json ---
    PREFIX = "content/agencies.json:ex-city."

    def write_agencies(self, *profiles, **extra):
        (self.root / "content").mkdir(exist_ok=True)
        rows = [dict({"agency_id": f"ex-city{'' if i == 0 else i}", "name": "Synthetic"}, profile=p, **extra)
                for i, p in enumerate(profiles)]
        (self.root / "content" / "agencies.json").write_text(json.dumps(rows), encoding="utf-8")

    def profile_entries(self, days=90):
        return {e["input"]: e for e in self.report(days)["stale_inputs"] if e["input"].startswith("content/agencies")}

    def test_every_family_boundary_future_and_hint(self):
        hint = "recheck the source and update checked_on"
        for singleton in ("alpr_policy", "contract", "policy_check", "contract_check", "pre_implementation_meeting",
                          "sharing"):
            for offset, status in ((89, None), (90, None), (91, "stale"), (-1, "future")):
                with self.subTest(field=singleton, offset=offset):
                    self.write_agencies({singleton: {"checked_on": ago(offset)}})
                    found = self.profile_entries()
                    if status is None:
                        self.assertEqual(found, {})
                        continue
                    entry = found[self.PREFIX + singleton]
                    self.assertEqual((entry["status"], entry["date"], entry["age_days"]), (status, ago(offset), offset))
                    self.assertEqual(entry["refresh"], hint if status == "stale" else FUTURE + hint)
        self.write_agencies({"funding": [{"checked_on": ago(5)}, {"checked_on": ago(120)}, {"checked_on": ahead(1)}]})
        found = self.profile_entries()
        self.assertEqual(sorted(found), [self.PREFIX + "funding[1]", self.PREFIX + "funding[2]"])
        self.assertEqual((found[self.PREFIX + "funding[1]"]["status"], found[self.PREFIX + "funding[2]"]["status"]),
                         ("stale", "future"))

    def test_only_newest_transparency_and_unknown_dates(self):
        self.write_agencies({"transparency": [{"checked_on": ago(3)}, {"checked_on": ago(400)}]})
        self.assertEqual(self.profile_entries(), {})  # old history under a fresh newest: silent
        self.write_agencies({"transparency": [{"checked_on": ago(400)}, {"checked_on": ago(200)}, {"checked_on": ago(200)}]})
        self.assertEqual(list(self.profile_entries()), [self.PREFIX + "transparency[1]"])  # unsorted, tie: first wins
        self.write_agencies({"transparency": [{"checked_on": ago(3)}, {"checked_on": "2026-02-30"}]})
        found = self.profile_entries()
        self.assertEqual(list(found), [self.PREFIX + "transparency[1]"])
        self.assertEqual((found[self.PREFIX + "transparency[1]"]["status"], found[self.PREFIX + "transparency[1]"]["date"]),
                         ("unknown", None))
        self.assertNotIn("age_days", found[self.PREFIX + "transparency[1]"])
        self.write_agencies({"contract": {"vendor": "x"}, "alpr_policy": {"checked_on": 20261001}})
        self.assertEqual({k: v["status"] for k, v in self.profile_entries().items()},
                         {self.PREFIX + "contract": "unknown", self.PREFIX + "alpr_policy": "unknown"})

    def test_nothing_added_without_profile_facts_or_for_unselected_or_private_kit(self):
        self.write_agencies({}, selected=False)
        self.assertEqual(self.profile_entries(), {})
        self.write_agencies({"funding": [], "transparency": [], "contract": None, "sharing": None})
        self.assertEqual(self.profile_entries(), {})
        self.write_agencies({"contract": {"checked_on": ago(500)}}, selected=False)
        self.assertEqual(self.profile_entries(), {})
        (self.root / "content" / "agencies.json").write_text("[]", encoding="utf-8")
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "agencies.json").write_text(json.dumps(
            [{"agency_id": "ex-city", "name": "S", "profile": {"contract": {"checked_on": ago(500)}}}]), encoding="utf-8")
        self.assertEqual(self.profile_entries(), {})
        (self.root / "content" / "agencies.json").unlink()
        self.assertEqual(self.profile_entries(), {})  # missing file: nothing, not a warning

    def test_unreadable_or_malformed_file_is_one_bounded_unknown(self):
        (self.root / "content").mkdir(exist_ok=True)
        path = self.root / "content" / "agencies.json"
        leak_marker = "LEAK-MARKER"
        for case in ("bytes", "text", "oversize", "dir", "dangling", "fifo", "wrongtype", "badid", "badshape", "null"):
            with self.subTest(case=case):
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                elif path.exists() or path.is_symlink():
                    path.unlink()
                if case == "bytes":
                    path.write_bytes(b"\xff\xfe" + leak_marker.encode())
                elif case == "text":
                    path.write_text("{" + leak_marker, encoding="utf-8")
                elif case == "oversize":
                    path.write_text('["' + "a" * (2 * 1024 * 1024) + '"]', encoding="utf-8")
                elif case == "dir":
                    path.mkdir()
                elif case == "dangling":
                    os.symlink(self.root / leak_marker, path)
                elif case == "fifo":
                    os.mkfifo(path)
                elif case == "null":  # a JSON null document is present-but-invalid, not a missing file
                    path.write_text("null", encoding="utf-8")
                elif case == "wrongtype":
                    path.write_text(json.dumps({"agency_id": leak_marker}), encoding="utf-8")
                elif case == "badid":
                    path.write_text(json.dumps([{"agency_id": leak_marker, "profile": {}}]), encoding="utf-8")
                else:
                    path.write_text(json.dumps([{"agency_id": "ex-city", "profile": {
                        "funding": [{"checked_on": ago(300)}, "x"], "contract": [1]}}]), encoding="utf-8")
                found = self.profile_entries()
                entry = found["content/agencies.json"]
                self.assertEqual((entry["status"], entry["date"], entry["refresh"]),
                                 ("unknown", None, "recheck the source and update checked_on"))
                self.assertNotIn(leak_marker, json.dumps(self.report()))
                if case == "badshape":  # the valid sibling is still diagnosed
                    self.assertEqual(found[self.PREFIX + "funding[0]"]["status"], "stale")

    def test_symlink_to_valid_file_is_not_followed(self):
        target = self.root / "real.json"
        target.write_text(json.dumps([{"agency_id": "ex-city", "profile": {"contract": {"checked_on": ago(500)}}}]),
                          encoding="utf-8")
        (self.root / "content").mkdir(exist_ok=True)
        os.symlink(target, self.root / "content" / "agencies.json")
        self.assertEqual(list(self.profile_entries()), ["content/agencies.json"])

    def test_merged_sorted_capped_unchanged_without_flag_and_read_only(self):
        self.write_agencies(*[{"contract": {"checked_on": ago(300)}, "funding": [{"checked_on": ago(300)}]}] * 60)
        self.meetings_at(120)
        report = self.report()
        total = len(report["stale_inputs"]) + report["stale_inputs_more"]
        self.assertEqual(len(report["stale_inputs"]), 100)
        names = [e["input"] for e in report["stale_inputs"]]
        self.assertEqual(names, sorted(names))
        self.assertEqual(total, 120 + 2)  # 120 profile rows + stale meetings + absent map
        buffer = io.StringIO()
        config = json.loads((self.root / "campaign.json").read_text(encoding="utf-8"))
        (self.root / "campaign.json").write_text(json.dumps(dict(config, campaign_id="synthetic-campaign")),
                                                 encoding="utf-8")
        with self.assertRaises(ValueError), contextlib.redirect_stdout(buffer):  # long next action: refused, no output
            cli.readiness(self.root, campaign_id="synthetic-campaign", stale_days=90)
        self.assertEqual(buffer.getvalue(), "")
        self.write_agencies({"contract": {"checked_on": ago(300)}, "funding": [{"checked_on": ago(300)}]})
        report = self.report()
        with contextlib.redirect_stdout(buffer):
            cli.readiness(self.root, campaign_id="synthetic-campaign", stale_days=90)
        self.assertEqual(json.loads(buffer.getvalue())["counts"]["stale_inputs"],
                         len(report["stale_inputs"]) + report.get("stale_inputs_more", 0))
        self.assertNotIn("stale_inputs", json.loads(buffer.getvalue()))  # counts only, no detailed array
        path = self.root / "content" / "agencies.json"
        command = [sys.executable, "-B", "-m", "campaign_tool", "doctor", "--directory", str(self.root)]
        self.write_agencies({"contract": {"checked_on": ago(500)}})  # old facts, no flag
        plain_old = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        path.unlink()
        plain_none = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(plain_old.stdout, plain_none.stdout)  # no flag: profile facts change no byte
        self.assertNotIn("stale_inputs", plain_old.stdout)
        self.write_agencies({"contract": {"checked_on": ago(500)}})
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no network")):
            self.report()
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    # --- DF1109B-01: profile reports, officials, body schedules and public-copy checks ---
    OFFICIAL, SCHEDULE, COPY = ("content/officials.json:bodies[0].officials[0]", "content/body-schedules.json:schedules[0]",
                                "content/sources.json:[0].public_copy")
    HINTS = {"officials": "re-check the official's page and update checked_on",
             "body-schedules": "re-check the body's calendar page and update checked_on",
             "sources": "re-download the public copy and run sources verify-copy"}
    NEW = ("content/officials.json", "content/body-schedules.json", "content/sources.json",
           "content/agencies.json:ex-city.reports")

    def write_content(self, name, document):
        (self.root / "content").mkdir(exist_ok=True)
        (self.root / "content" / name).write_text(json.dumps(document), encoding="utf-8")

    @staticmethod
    def dated(row, day):
        return row if day is NO_DATE else dict(row, checked_on=day)

    def write_officials(self, *bodies):
        self.write_content("officials.json", {"schema_version": 1, "bodies": [
            {"agency_id": f"ex-city{i or ''}", "officials": [self.dated(
                {"name": "Synthetic Member", "role": "Member", "source_url": "https://example.org/o"}, day) for day in days]}
            for i, days in enumerate(bodies)]})

    def write_schedules(self, *days):
        self.write_content("body-schedules.json", {"schema_version": 1, "schedules": [self.dated(
            {"body": f"Synthetic Council {i}", "weekday": "tuesday", "ordinals": [1], "local_start_time": "18:30",
             "time_zone": "America/Los_Angeles", "source_url": "https://example.org/c"}, day) for i, day in enumerate(days)]})

    def write_sources(self, *days, sha="0"):  # NO_COPY: a library entry without a public_copy check
        self.write_content("sources.json", [dict(
            {"sha256": sha * 64, "title": "Synthetic record", "agency_id": "ex-city", "received_at": "2026-01-02T00:00:00Z",
             "public_url": "https://example.org/r.pdf", "fragment": f"record-{i}"},
            **({} if day is NO_COPY else {"public_copy": self.dated({"matches": True}, day)})) for i, day in enumerate(days)])

    def write_all(self, day):
        self.write_officials([day])
        self.write_schedules(day)
        self.write_sources(day)
        self.write_agencies({"reports": [self.dated({"title": "Synthetic report", "period_start": "2025-01-01"}, day)]})

    def new_entries(self, days=90):
        return {e["input"]: e for e in self.report(days)["stale_inputs"] if e["input"].startswith(self.NEW)}

    def expected_hints(self):
        return ((self.OFFICIAL, self.HINTS["officials"]), (self.SCHEDULE, self.HINTS["body-schedules"]),
                (self.COPY, self.HINTS["sources"]), (self.PREFIX + "reports[0]", freshness.PROFILE_REFRESH))

    def test_new_families_all_checked_dates_and_hints(self):
        for offset, status in ((89, None), (90, None), (0, None), (91, "stale"), (400, "stale"), (-1, "future")):
            with self.subTest(offset=offset):
                self.write_all(ago(offset))
                found = self.new_entries()
                if status is None:
                    self.assertEqual(found, {})
                    continue
                self.assertEqual(len(found), 4)
                for name, hint in self.expected_hints():
                    self.assertEqual(found[name], {"input": name, "status": status, "date": ago(offset), "age_days": offset,
                                                   "refresh": hint if status == "stale" else FUTURE + hint})
        for bad in ("2026-1-05", "2026-02-30", "2026-10-01T00:00:00Z", 20261001, None, NO_DATE):
            with self.subTest(bad=bad):
                self.write_all(bad)
                found = self.new_entries()
                for name, hint in self.expected_hints():
                    self.assertEqual(found[name], {"input": name, "status": "unknown", "date": None, "refresh": hint})
                self.assertNotIn("content/officials.json", found)  # a bad date is a row unknown, not a file warning
        self.assertIn("Refresh stale inputs: ", " ".join(self.report()["next_actions"]))
        self.assertIn(self.OFFICIAL, [a for a in self.report()["next_actions"] if a.startswith("Refresh")][0])

    def test_report_checks_not_periods_and_historical_exclusions(self):
        old = {"period_start": ago(2000), "period_end": ago(1500), "published_on": ago(1400), "date": ago(1400)}
        self.write_agencies({"reports": [dict(old, checked_on=ago(5)), dict(old, checked_on=ago(400))],
                             "transparency": [{"checked_on": ago(3)}, {"checked_on": ago(400)}]})
        for name in ("agency-actions.json", "statements.json", "coverage.json", "elsewhere.json"):
            self.write_content(name, {"schema_version": 1, "entries": [{"checked_on": ago(900), "date": ago(900)}],
                                      "actions": [{"checked_on": ago(900)}], "statements": [{"checked_on": ago(900)}]})
        found = {e["input"]: e["status"] for e in self.report()["stale_inputs"]
                 if e["input"].startswith("content/") and e["input"] != MAP}  # history files and older snapshots stay silent
        self.assertEqual(found, {self.PREFIX + "reports[1]": "stale"})

    def test_optional_absent_empty_and_legacy_bytes(self):
        command = [sys.executable, "-B", "-m", "campaign_tool", "doctor", "--directory", str(self.root)]

        def outputs():
            return [subprocess.run(command + extra, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60).stdout
                    for extra in ([], ["--checklist", "--json"])]
        baseline = outputs()
        # A feature-absent campaign: exactly the entries main reported (pinned, not recomputed).
        self.assertEqual(self.report()["stale_inputs"], [
            {"input": MAP, "status": "absent", "date": None, "refresh": freshness.MAP_REFRESH.format(self.root)},
            {"input": MEETINGS, "status": "absent", "date": None, "refresh": f"python3 -m campaign_tool meetings "
             f"--directory {self.root} --online --client <legistar client>"}])
        absent = self.report()
        self.write_officials()
        self.write_content("body-schedules.json", {"schema_version": 1, "schedules": []})
        self.write_sources(NO_COPY, NO_COPY)
        self.write_content("agencies.json", [{"agency_id": "ex-city", "selected": False,
                                              "profile": {"reports": [{"checked_on": ago(900)}]}},
                                             {"agency_id": "ex-city2", "profile": {"reports": []}}])
        self.assertEqual(self.report(), absent)
        self.write_all(ago(900))  # stale facts change no byte without the flag
        self.assertEqual(outputs()[0], baseline[0])
        self.assertNotIn("stale_inputs", outputs()[0])

    def test_each_copy_alias_and_official_position_is_independent(self):
        self.write_officials([ago(5), ago(400), ago(5)], [ago(400)])
        self.write_sources(ago(400), NO_COPY, ago(5), ago(400), sha="1")  # one digest, several fragments
        self.write_schedules(ago(5), ago(400))
        found = self.new_entries()
        self.assertEqual(sorted(found), [
            "content/body-schedules.json:schedules[1]", "content/officials.json:bodies[0].officials[1]",
            "content/officials.json:bodies[1].officials[0]", self.COPY, "content/sources.json:[3].public_copy"])
        self.write_sources(ago(400), NO_COPY, ago(5), ago(5), sha="1")  # re-checking one alias clears only that one
        self.assertEqual(sorted(set(found) - set(self.new_entries())), ["content/sources.json:[3].public_copy"])
        for prose in ("Synthetic Member", "Synthetic Council", "Synthetic record"):  # never part of an id
            self.assertNotIn(prose, json.dumps(self.report()))

    def test_new_files_unreadable_one_unknown_no_leak(self):
        leak = "LEAK-MARKER"
        bad_envelopes = {"officials.json": [{"bodies": []}, {"schema_version": 2, "bodies": []},
                                            {"schema_version": 1, "bodies": {}}],
                         "body-schedules.json": [{"schema_version": True, "schedules": []},
                                                 {"schema_version": 1, "schedules": leak}],
                         "sources.json": [{"0": leak}, leak]}
        for name, envelopes in bad_envelopes.items():
            label, path = "content/" + name, self.root / "content" / name
            for case in ["bytes", "text", "null", "oversize", "dir", "dangling", "fifo", "symlink"] + envelopes:
                with self.subTest(file=name, case=case):
                    if path.is_dir() and not path.is_symlink():
                        path.rmdir()
                    elif path.exists() or path.is_symlink():
                        path.unlink()
                    self.write_all(ago(400))
                    path.unlink()
                    if case == "bytes":
                        path.write_bytes(b"\xff\xfe" + leak.encode())
                    elif case == "text":
                        path.write_text("{" + leak, encoding="utf-8")
                    elif case == "null":
                        path.write_text("null", encoding="utf-8")
                    elif case == "oversize":
                        path.write_text('["' + "a" * (2 * 1024 * 1024) + '"]', encoding="utf-8")
                    elif case == "dir":
                        path.mkdir()
                    elif case == "dangling":
                        os.symlink(self.root / leak, path)
                    elif case == "fifo":
                        os.mkfifo(path)
                    elif case == "symlink":  # a valid target is still never followed
                        target = self.root / "content" / (leak + name)
                        target.write_text(json.dumps({"schema_version": 1, "bodies": [], "schedules": []}), "utf-8")
                        os.symlink(target, path)
                    else:
                        self.write_content(name, case)
                    found = {e["input"]: e for e in self.report()["stale_inputs"] if e["input"].startswith(self.NEW)}
                    family = [key for key in found if key.startswith(label)]
                    self.assertEqual(family, [label])
                    self.assertEqual(found[label], {"input": label, "status": "unknown", "date": None,
                                                    "refresh": self.HINTS[name.removesuffix(".json")]})
                    self.assertEqual(len(found), 4)  # the three valid sibling families are still dated
                    self.assertNotIn(leak, json.dumps(self.report()))
        # A bad row shape adds one bounded warning; valid rows are kept.
        self.write_content("officials.json", {"schema_version": 1, "bodies": [
            {"agency_id": "ex-city", "officials": [{"checked_on": ago(400)}, leak, [leak]]}, {"officials": leak}, leak]})
        self.write_content("sources.json", [leak, {"public_copy": leak}, {"public_copy": {"checked_on": ago(400)}}])
        self.write_content("body-schedules.json", {"schema_version": 1, "schedules": [7, {"checked_on": ago(400)}]})
        found = {e["input"]: e["status"] for e in self.report()["stale_inputs"] if e["input"].startswith(self.NEW[:3])}
        self.assertEqual(found, {"content/officials.json": "unknown", self.OFFICIAL: "stale",
                                 "content/sources.json": "unknown", "content/sources.json:[2].public_copy": "stale",
                                 "content/body-schedules.json": "unknown", "content/body-schedules.json:schedules[1]": "stale"})
        self.assertNotIn(leak, json.dumps(self.report()))

    def test_large_scan_merged_cap_and_readiness_parity(self):
        self.write_officials(*[[ago(400)] * 30] * 3)
        self.write_schedules(*[ago(400)] * 20)
        self.write_sources(*[ahead(2)] * 20)
        self.write_agencies({"reports": [{"checked_on": ago(400)}] * 10, "contract": {"checked_on": ago(400)}})
        report = self.report()
        names = [e["input"] for e in report["stale_inputs"]]
        total = 90 + 20 + 20 + 11 + 2  # + absent map and meetings
        self.assertEqual((len(names), report["stale_inputs_more"]), (100, total - 100))
        self.assertEqual(names, sorted(names))
        full = freshness.stale_inputs(self.root, cli.read_config(self.root), 90)
        self.assertEqual(report["stale_inputs"], full[:100])
        config = json.loads((self.root / "campaign.json").read_text(encoding="utf-8"))
        (self.root / "campaign.json").write_text(json.dumps(dict(config, campaign_id="synthetic-campaign")),
                                                 encoding="utf-8")
        buffer = io.StringIO()
        with self.assertRaises(ValueError), contextlib.redirect_stdout(buffer):  # long next action: refused
            cli.readiness(self.root, campaign_id="synthetic-campaign", stale_days=90)
        self.assertEqual(buffer.getvalue(), "")
        # A short scan: readiness counts exactly the doctor entries; doctor JSON and --checklist --json agree.
        self.write_all(ago(400))
        report = self.report()
        with contextlib.redirect_stdout(buffer):
            cli.readiness(self.root, campaign_id="synthetic-campaign", stale_days=90)
        snapshot = json.loads(buffer.getvalue())
        self.assertEqual(snapshot["counts"]["stale_inputs"], len(report["stale_inputs"]))
        self.assertEqual(len(report["stale_inputs"]), 4 + 2)  # four new rows + absent map and meetings
        self.assertNotIn("stale_inputs", snapshot)
        command = [sys.executable, "-B", "-m", "campaign_tool", "doctor", "--directory", str(self.root),
                   "--stale-days", "90"]
        plain = json.loads(subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60).stdout)
        rows = json.loads(subprocess.run(command + ["--checklist", "--json"], cwd=REPO_ROOT, capture_output=True,
                                         text=True, timeout=60).stdout)
        new = {e["input"]: e["status"] for e in plain["stale_inputs"] if e["input"].startswith(self.NEW)}
        self.assertEqual(new, {name: "stale" for name, _hint in self.expected_hints()})
        from campaign_tool.doctor_checklist import _short
        self.assertIn("Other: stale_inputs = " + _short(plain["stale_inputs"]), [row["label"] for row in rows])

    def test_read_only_no_flag_no_optional_reads(self):
        self.write_all(ago(400))

        def listing():
            return sorted((str(p.relative_to(p.anchor)), p.lstat().st_mtime_ns, p.lstat().st_mode,
                           p.read_bytes() if p.is_file() else None) for p in [self.root, *self.root.rglob("*")])
        before = listing()
        with mock.patch.object(freshness, "_public_document", side_effect=AssertionError("read without the flag")):
            plain = cli.doctor_report(self.root)
        self.assertNotIn("stale_inputs", plain)
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no network")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("no command is run")):
            self.assertEqual(len(self.new_entries()), 4)
        self.assertEqual(listing(), before)

    def test_build_still_refuses_an_official_checked_366_days_ago(self):
        from campaign_tool.content import ContentError, load_officials
        document = {"schema_version": 1, "bodies": [{"agency_id": "ex-city", "officials": [
            {"name": "Synthetic Member", "role": "Member", "source_url": "https://example.org/o", "checked_on": ago(365)}]}]}
        self.assertIn("ex-city", load_officials(document, [{"agency_id": "ex-city"}], TODAY))
        document["bodies"][0]["officials"][0]["checked_on"] = ago(366)
        with self.assertRaisesRegex(ContentError, "older than 365 days"):
            load_officials(document, [{"agency_id": "ex-city"}], TODAY)
        self.write_officials([ago(366)])
        self.assertEqual(self.new_entries(365)[self.OFFICIAL]["status"], "stale")
        self.write_officials([ago(365)])
        self.assertEqual(self.new_entries(365), {})


NO_DATE, NO_COPY = object(), object()  # a row with no checked_on key; a source with no public_copy

if __name__ == "__main__":
    unittest.main()
