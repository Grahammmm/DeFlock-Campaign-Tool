"""Build-time camera counts from community-mapped points inside each city's boundary.

Synthetic squares in the fictional Gulf-of-Guinea extent (lon/lat 0..2) only; no network.
Run from the repository root: python3 -B -m unittest tests.test_camera_counts -v
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import camera_counts
from campaign_tool.camera_counts import count_by_name, point_in_polygon
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TAIL = " inside city limits; ownership unconfirmed"


def square(west, south, east, north):
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


def feature(name, geometry_type, coordinates, key="NAME"):
    return {"type": "Feature", "properties": {key: name} if name else {},
            "geometry": {"type": geometry_type, "coordinates": coordinates}}


def collection(features):
    return {"type": "FeatureCollection", "features": features}


def points(*coords):
    return collection([{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": list(c)}}
                       for c in coords])


class CountTests(unittest.TestCase):
    def test_hole_excluded_and_multipolygon_parts_both_counted(self):
        holed = feature("Cedar", "Polygon", [square(0.5, 0.5, 1.5, 1.5), square(0.8, 0.8, 1.2, 1.2)])
        multi = feature("Pine", "MultiPolygon", [[square(0.1, 0.1, 0.3, 0.3)], [square(1.7, 1.7, 1.9, 1.9)]])
        cameras = points((1.0, 1.0), (0.6, 0.6), (1.4, 1.4), (0.2, 0.2), (1.8, 1.8), (1.0, 0.2))
        counts, problems = count_by_name(cameras, collection([holed, multi]))
        self.assertEqual(counts, {"Cedar": 2, "Pine": 2})
        self.assertEqual(problems, [])

    def test_point_on_shared_edge_counted_once(self):
        west = feature("Cedar", "Polygon", [square(0.5, 0.5, 1.0, 1.0)])
        east = feature("Pine", "Polygon", [square(1.0, 0.5, 1.5, 1.0)])
        north = feature("Elm", "Polygon", [square(0.5, 1.0, 1.0, 1.5)])
        corner = feature("Oak", "Polygon", [square(1.0, 1.0, 1.5, 1.5)])  # (1, 1) is a corner of all four
        for point in ((1.0, 0.75), (0.75, 1.0), (1.0, 1.0)):
            with self.subTest(point=point):
                counts, _ = count_by_name(points(point), collection([west, east, north, corner]))
                self.assertEqual(sum(counts.values()), 1)

    def test_same_name_features_count_each_point_once(self):
        first = feature("Cedar", "Polygon", [square(0.5, 0.5, 1.0, 1.0)])
        overlapping = feature("Cedar", "Polygon", [square(0.7, 0.5, 1.2, 1.0)], key="BASENAME")
        counts, _ = count_by_name(points((0.8, 0.7), (0.6, 0.7), (1.1, 0.7)), collection([first, overlapping]))
        self.assertEqual(counts, {"Cedar": 3})

    def test_bad_features_named_by_index_without_values(self):
        features = [
            feature(None, "Polygon", [square(0.5, 0.5, 1.5, 1.5)]),
            feature("Line", "LineString", [[0.123456, 0.654321], [1.123456, 1.654321]]),
            feature("Tri", "Polygon", [[[0.123456, 0.5], [1.5, 0.5], [0.123456, 0.5]]]),
            feature("Str", "Polygon", [[["0.123456", "0.5"], ["1.5", "0.5"], ["1.5", "1.5"], ["0.123456", "0.5"]]]),
            feature("Cedar", "Polygon", [square(0.5, 0.5, 1.5, 1.5)]),
            "not-a-feature",
        ]
        counts, problems = count_by_name(points((1.0, 1.0)), collection(features))
        self.assertEqual(counts, {"Cedar": 1})
        self.assertEqual([p.split(":")[0] for p in problems], [f"city boundary feature {i}" for i in (0, 1, 2, 3, 5)])
        self.assertIn("no BASENAME or NAME", problems[0])
        self.assertIn("unsupported geometry type", problems[1])
        self.assertTrue(all("invalid ring" in p for p in problems[2:4]))
        for text in problems:
            self.assertNotRegex(text, r"\d\.\d|LineString|Line\b|Tri\b|Str\b")
        self.assertEqual(count_by_name(points((1.0, 1.0)), {"type": "FeatureCollection"}),
                         ({}, ["city boundary features: expected a list"]))

    def test_non_finite_ring_skipped_with_indexed_problem_not_a_crash(self):
        # json.loads accepts NaN/Infinity, so a boundary file can carry them; they must not reach the band index.
        nan_ring = json.loads('[[0.5, 0.5], [1.5, NaN], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]')
        inf_ring = json.loads('[[0.5, 0.5], [Infinity, 0.5], [1.5, 1.5], [0.5, -Infinity], [0.5, 0.5]]')
        features = [feature("Nan", "Polygon", [nan_ring]),
                    feature("Inf", "MultiPolygon", [[square(0.1, 0.1, 0.3, 0.3)], [inf_ring]]),
                    feature("Hole", "Polygon", [square(0.5, 0.5, 1.5, 1.5), inf_ring]),
                    feature("Cedar", "Polygon", [square(0.5, 0.5, 1.5, 1.5)]),
                    feature("Huge", "Polygon", [square(-1e308, -1e308, 1e308, 1e308)]),  # span overflows to inf
                    feature("BigInt", "Polygon", [square(0, 0, 2, 10 ** 309)]),  # int beyond float range
                    feature("HugeInt", "Polygon", [square(-10 ** 308, -10 ** 308, 10 ** 308, 10 ** 308)])]
        cameras = json.loads('{"features": [{"geometry": {"coordinates": [1.0, 1.0]}},'
                             ' {"geometry": {"coordinates": [NaN, 1.0]}}, {"geometry": {"coordinates": [1.0, Infinity]}},'
                             ' {"geometry": {"coordinates": [1, 1' + "0" * 309 + ']}}]}')
        counts, problems = count_by_name(cameras, collection(features))
        self.assertEqual(counts, {"Cedar": 1, "Huge": 1, "HugeInt": 1})
        self.assertEqual(problems, [f"city boundary feature {i}: invalid ring (needs 4 or more finite numeric positions)"
                                    for i in (0, 1, 2, 5)])

    def test_twenty_thousand_points_in_a_two_thousand_vertex_boundary_under_five_seconds(self):
        side = 500  # 4 x 500 = 2000 vertices on a square with subdivided sides
        ring = ([[0.1 + 1.8 * i / side, 0.1] for i in range(side)] + [[1.9, 0.1 + 1.8 * i / side] for i in range(side)]
                + [[1.9 - 1.8 * i / side, 1.9] for i in range(side)] + [[0.1, 1.9 - 1.8 * i / side] for i in range(side)])
        ring.append(ring[0])
        self.assertEqual(len(ring), 2001)
        cameras = points(*((0.05 + 1.9 * (i % 200) / 199, 0.05 + 1.9 * (i // 200) / 99) for i in range(20000)))
        started = time.perf_counter()
        counts, problems = count_by_name(cameras, collection([feature("Cedar", "Polygon", [ring])]))
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertEqual(problems, [])
        expected = sum(0.1 < x < 1.9 and 0.1 < y < 1.9 for x, y in (f["geometry"]["coordinates"] for f in cameras["features"]))
        self.assertEqual(counts, {"Cedar": expected})
        sample = cameras["features"][::97]  # the banded index agrees with the plain ray cast
        self.assertEqual(count_by_name(collection(sample), collection([feature("Cedar", "Polygon", [ring])]))[0],
                         {"Cedar": sum(point_in_polygon(f["geometry"]["coordinates"], [ring]) for f in sample)})
        self.assertTrue(0 < expected < 20000)


class BuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-camera-counts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.map_dir = self.root / "content" / "map"

    def write_map(self, boundaries, cameras):
        (self.map_dir / "city-boundaries.geojson").write_text(json.dumps(collection(boundaries)))
        (self.map_dir / "cameras.geojson").write_text(json.dumps(cameras))

    def add_pine(self, profile=None):
        path = self.root / "content" / "agencies.json"
        agencies = json.loads(path.read_text())
        agencies.append({"agency_id": "ca-pine-police", "slug": "pine", "name": "Pine Police Department (fictional)",
                         "kind": "police", "jurisdiction_name": "Pine", **({"profile": profile} if profile else {})})
        path.write_text(json.dumps(agencies))
        cities = json.loads((self.map_dir / "cities.json").read_text())
        cities["pine"] = {"name": "Pine", "bounds": [[1.5, 0.5], [1.9, 1.5]]}
        (self.map_dir / "cities.json").write_text(json.dumps(cities))

    def build(self):
        notes = []
        build_site(self.root, notes=notes)
        data = (self.root / "public" / "site-data.js").read_text(encoding="utf-8")
        return json.loads(re.search(r"var CITIES=(\{.*\});", data).group(1)), notes, data

    def test_three_inside_two_outside_then_singular_then_zero(self):
        cedar = feature("Cedar", "Polygon", [square(0.5, 0.5, 1.5, 1.5)])
        for cameras, label in ((points((0.7, 0.7), (1.0, 1.0), (1.3, 1.3), (0.2, 0.2), (1.8, 1.8)), "3 community-mapped points"),
                               (points((1.0, 1.0), (0.2, 1.8)), "1 community-mapped point"),
                               (points((0.2, 1.8)), "0 community-mapped points")):
            with self.subTest(label=label):
                self.write_map([cedar], cameras)
                cities, notes, _ = self.build()
                self.assertEqual(cities["cedar"]["cameraCount"], label + TAIL)
                self.assertEqual(notes, [])

    def test_typed_count_kept_and_rural_entry_unchanged(self):
        self.add_pine({"cameraCount": "About 12 per records"})
        self.write_map([feature("Cedar", "Polygon", [square(0.5, 0.5, 1.5, 1.5)]),
                        feature("Pine", "Polygon", [square(1.5, 0.5, 1.9, 1.5)])], points((1.0, 1.0), (1.7, 1.0)))
        cities, notes, _ = self.build()
        self.assertEqual(cities["pine"]["cameraCount"], "About 12 per records")
        self.assertEqual(cities["cedar"]["cameraCount"], "1 community-mapped point" + TAIL)
        self.assertEqual(cities["rural-area"]["cameraCount"], "")
        self.assertEqual(cities["rural-area"]["status"], "Ownership unconfirmed")
        self.assertEqual(notes, [])

    def test_city_without_a_matching_boundary_keeps_an_empty_count(self):
        self.add_pine()
        self.write_map([feature("Cedar", "Polygon", [square(0.5, 0.5, 1.5, 1.5)])], points((1.7, 1.0)))
        cities, _, _ = self.build()
        self.assertEqual(cities["pine"]["cameraCount"], "")
        self.assertEqual(cities["cedar"]["cameraCount"], "0 community-mapped points" + TAIL)

    def test_unmatched_boundary_and_bad_features_are_cli_notes_and_exit_zero(self):
        self.write_map([feature("Cedar", "Polygon", [square(0.5, 0.5, 1.5, 1.5)]),
                        feature("Elm", "Polygon", [square(1.6, 1.6, 1.9, 1.9)]),
                        feature("Rural", "LineString", [[0.123456, 0.654321], [1.5, 1.5]], key="BASENAME")],
                       points((1.0, 1.0), (1.7, 1.7)))
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        notes = [line for line in result.stderr.splitlines() if line.startswith("note: ")]
        self.assertEqual(notes, ["note: boundary Elm matches no city agency",
                                 "note: city boundary feature 2: unsupported geometry type (Polygon or MultiPolygon only)"])
        self.assertNotIn("0.123456", result.stderr + result.stdout)

    def test_overflowing_json_boundary_number_with_no_cameras_is_a_note_and_the_build_succeeds(self):
        # Standard JSON syntax: 1e309 loads as infinity; a 310-digit integer loads as an int beyond float range.
        for literal in ("1e309", "1" + "0" * 309):
            with self.subTest(literal=literal[:8]):
                self.write_map([feature("Cedar", "Polygon", [square(0.5, 0.5, 1.5, 1.5)]),
                                feature("Elm", "Polygon", [[[0, 0], [1, 0], [1, 7.5], [0, 1], [0, 0]]])], points())
                path = self.map_dir / "city-boundaries.geojson"
                path.write_text(path.read_text().replace("7.5", literal))
                result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory",
                                         str(self.root)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr)
                notes = [line for line in result.stderr.splitlines() if line.startswith("note: ")]
                self.assertEqual(notes, ["note: city boundary feature 1: invalid ring"
                                         " (needs 4 or more finite numeric positions)"])
                self.assertNotIn(literal, result.stderr + result.stdout)
                cities, _, _ = self.build()
                self.assertEqual(cities["cedar"]["cameraCount"], "0 community-mapped points" + TAIL)

    def test_no_map_computes_nothing_and_site_data_is_unchanged(self):
        shutil.rmtree(self.map_dir)
        with mock.patch.object(camera_counts, "count_by_name", side_effect=AssertionError("computed")):
            cities, notes, data = self.build()
        self.assertEqual(notes, [])
        self.assertEqual(cities["cedar"]["cameraCount"], "")
        with mock.patch.object(Site, "mapped_counts", return_value=({"Cedar": 99}, ["x"])):
            _, _, again = self.build()
        self.assertEqual(again, data)


if __name__ == "__main__":
    unittest.main()
