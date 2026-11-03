"""DF0137-13: each writing fetch_osm_alprs run records one dated entry in content/map/history.json.

Synthetic --from-file answers only (made-up node ids and coordinates); urlopen is patched to fail. No network.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.camera_counts import count_by_name
from tools import fetch_osm_alprs as tool

OCT, NOV = "2026-10-01T06:00:00Z", "2026-11-01T06:00:00Z"


def node(number, lon=0.75, lat=1.0):
    return {"type": "node", "id": number, "lat": lat, "lon": lon, "tags": {"man_made": "surveillance"}}


def answer(nodes, base=OCT):
    document = {"elements": nodes}
    if base is not None:
        document["osm3s"] = {"timestamp_osm_base": base}
    return document


def square(name, west, east):
    ring = [[west, 0.5], [east, 0.5], [east, 1.5], [west, 1.5], [west, 0.5]]
    return {"type": "Feature", "properties": {"NAME": name}, "geometry": {"type": "Polygon", "coordinates": [ring]}}


def record(day, total=1, **extra):
    return dict({"recorded_on": day, "osm_base": None, "source": "saved_file", "total": total, "by_city": None,
                 "added": 0, "removed": 0}, **extra)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-osm-history-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.campaign = self.tmp / "campaign"
        self.campaign.mkdir()
        (self.campaign / "campaign.json").write_text("{}\n", encoding="utf-8")
        self.map_dir = self.campaign / "content" / "map"
        self.history = self.map_dir / "history.json"
        patcher = mock.patch.object(tool.urllib.request, "urlopen", side_effect=AssertionError("network used"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tool(self, document, *extra):
        saved = self.tmp / "saved.json"
        saved.write_text(json.dumps(document), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["--directory", str(self.campaign), "--from-file", str(saved), *extra])
        return code, out.getvalue(), err.getvalue()

    def records(self):
        document = json.loads(self.history.read_text(encoding="utf-8"))
        self.assertEqual(sorted(document), ["records", "schema_version"])
        return document["records"]

    def seed(self, records):
        self.map_dir.mkdir(parents=True, exist_ok=True)
        self.history.write_text(json.dumps({"schema_version": 1, "records": records}), encoding="utf-8")
        return self.history.read_bytes()

    # 1. two writing runs with different osm_base -> 2 records, added/removed as diff(); --check changes nothing
    def test_two_runs_two_records_added_removed_match_diff_and_check_writes_nothing(self):
        first, second = [node(1), node(2), node(3)], [node(2), node(3), node(4), node(5)]
        code, out, _ = self.run_tool(answer(first, OCT))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "history: 1 records")
        old = json.loads((self.map_dir / "cameras.geojson").read_text(encoding="utf-8"))
        code, out, _ = self.run_tool(answer(second, NOV))
        self.assertEqual((code, out.splitlines()[-1]), (0, "history: 2 records"))
        changes = tool.diff(old, tool.to_geojson(second)[0])
        self.assertEqual(self.records(), [
            {"recorded_on": "2026-10-01", "osm_base": OCT, "source": "saved_file", "total": 3, "by_city": None,
             "added": 3, "removed": 0},
            {"recorded_on": "2026-11-01", "osm_base": NOV, "source": "saved_file", "total": 4, "by_city": None,
             "added": len(changes["added"]), "removed": len(changes["removed"])}])
        self.assertEqual((len(changes["added"]), len(changes["removed"])), (2, 1))
        before = self.history.read_bytes()
        code, out, _ = self.run_tool(answer([node(9)], "2026-12-01T00:00:00Z"), "--check")
        self.assertEqual(code, 3)  # points would be removed; still nothing written
        self.assertIn("Check only: nothing was written.", out)
        self.assertEqual(self.history.read_bytes(), before)

    # 2. the same osm_base twice -> one record, replaced (so is a second run on the same map day)
    def test_same_osm_base_replaces_last_record(self):
        self.assertEqual(self.run_tool(answer([node(1)], OCT))[0], 0)
        self.assertEqual(self.run_tool(answer([node(1), node(2)], OCT))[0], 0)
        self.assertEqual([(r["osm_base"], r["total"], r["added"]) for r in self.records()], [(OCT, 2, 1)])
        self.assertEqual(self.run_tool(answer([node(1)], "2026-10-01T18:00:00Z"))[0], 0)
        self.assertEqual([(r["osm_base"], r["total"], r["removed"]) for r in self.records()],
                         [("2026-10-01T18:00:00Z", 1, 1)])

    #    ... and a refused write (empty answer without --allow-empty) leaves no record
    def test_refused_empty_write_records_nothing(self):
        self.assertEqual(self.run_tool(answer([node(1)], OCT))[0], 0)
        before = self.history.read_bytes()
        code, out, err = self.run_tool(answer([], NOV))
        self.assertEqual((code, out, err.strip()), (3, "", tool.EMPTY_KEPT))
        self.assertEqual(self.history.read_bytes(), before)
        code, _, _ = self.run_tool(answer([], NOV), "--allow-empty")
        self.assertEqual(code, 0)
        self.assertEqual([(r["total"], r["removed"]) for r in self.records()], [(1, 0), (0, 1)])

    # 3. the 121st record drops the oldest; 120 kept in order
    def test_121st_record_drops_oldest_and_keeps_order(self):
        days = [f"2026-{month:02d}-{day:02d}" for month in range(1, 7) for day in range(1, 21)]
        self.seed([record(day, total=index) for index, day in enumerate(days)])
        self.assertEqual(self.run_tool(answer([node(1)], NOV))[0], 0)
        kept = self.records()
        self.assertEqual(len(kept), 120)
        self.assertEqual([r["recorded_on"] for r in kept], days[1:] + ["2026-11-01"])
        self.assertEqual([r["total"] for r in kept[:-1]], list(range(1, 120)))

    # 4. city-boundaries.geojson present -> by_city by the build's own rule; absent -> null
    def test_by_city_uses_boundaries_when_present(self):
        self.map_dir.mkdir(parents=True)
        boundaries = {"type": "FeatureCollection", "features": [square("Pine", 1.0, 1.5), square("Cedar", 0.5, 1.0)]}
        (self.map_dir / "city-boundaries.geojson").write_text(json.dumps(boundaries), encoding="utf-8")
        nodes = [node(1, 0.6), node(2, 1.0), node(3, 1.2), node(4, 1.8)]  # node 2 on the shared edge: Pine only
        self.assertEqual(self.run_tool(answer(nodes, OCT))[0], 0)
        expected = count_by_name(tool.to_geojson(nodes)[0], boundaries)[0]
        self.assertEqual(self.records()[0]["by_city"], {"Cedar": 1, "Pine": 2})
        self.assertEqual(self.records()[0]["by_city"], expected)
        (self.map_dir / "city-boundaries.geojson").unlink()
        self.assertEqual(self.run_tool(answer(nodes, NOV))[0], 0)
        self.assertIsNone(self.records()[1]["by_city"])

    # 5. a hand-edited history the build would refuse is never overwritten; an older map day is not recorded
    def test_invalid_history_or_older_map_day_refused_map_still_written(self):
        cases = {"unknown key": ({"schema_version": 1, "records": [], "note": "SECRETVALUE"}, r"unknown field"),
                 "schema 2": ({"schema_version": 2, "records": []}, r"schema_version: must be 1"),
                 "decreasing": ({"schema_version": 1, "records": [record("2026-09-02"), record("2026-09-01")]},
                                r"records\[1\]\.recorded_on: dates must be strictly increasing"),
                 "negative": ({"schema_version": 1, "records": [record("2026-09-01", total=-1)]},
                              r"records\[0\]\.total: must be a non-negative integer")}
        for name, (document, message) in cases.items():
            with self.subTest(name):
                self.map_dir.mkdir(parents=True, exist_ok=True)
                self.history.write_text(json.dumps(document), encoding="utf-8")
                before = self.history.read_bytes()
                code, out, err = self.run_tool(answer([node(len(name))], OCT))
                self.assertEqual((code, out), (2, ""))
                self.assertRegex(err, "fetch_osm_alprs: history not updated: map/history.json" + r".*" + message)
                self.assertTrue(err.strip().endswith("(the new cameras.geojson was written)"))
                self.assertNotIn("SECRETVALUE", err)
                self.assertEqual(self.history.read_bytes(), before)
        self.seed([record("2026-11-01")])
        before = self.history.read_bytes()
        code, _, err = self.run_tool(answer([node(7)], OCT))
        self.assertEqual(code, 2)
        self.assertIn("the map day is older than its last record", err)
        self.assertEqual(self.history.read_bytes(), before)
        self.assertIn("node/7", (self.map_dir / "cameras.geojson").read_text(encoding="utf-8"))

    # 6. a symlinked history.json is never followed; no osm_base -> the run's UTC day and null
    def test_symlinked_history_refused_target_untouched(self):
        self.map_dir.mkdir(parents=True)
        target = self.tmp / "elsewhere.json"
        target.write_text(json.dumps({"schema_version": 1, "records": []}), encoding="utf-8")
        os.symlink(target, self.history)
        code, _, err = self.run_tool(answer([node(1)], OCT))
        self.assertEqual(code, 2)
        self.assertIn("map/history.json: symlinks are not read", err)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"schema_version": 1, "records": []})
        self.assertTrue(self.history.is_symlink())

    def test_missing_osm_base_uses_utc_run_day(self):
        with mock.patch.object(tool, "_utc_day", return_value="2026-11-03"):
            self.assertEqual(self.run_tool(answer([node(1)], None))[0], 0)
        self.assertEqual(self.records(), [record("2026-11-03", added=1)])

    def test_oversize_history_trimmed_to_byte_cap(self):
        big = {f"City {index:03d} " + "x" * 100: index for index in range(500)}  # ~60 KiB per record
        self.seed([record(f"2026-01-{day:02d}", by_city=big) for day in range(1, 5)])
        self.assertEqual(self.run_tool(answer([node(1)], NOV))[0], 0)
        self.assertLessEqual(self.history.stat().st_size, tool.HISTORY_BYTES)
        self.assertEqual(self.records()[-1]["recorded_on"], "2026-11-01")


if __name__ == "__main__":
    unittest.main()
