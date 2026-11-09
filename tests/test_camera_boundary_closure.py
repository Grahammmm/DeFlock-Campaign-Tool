"""RF1355-4: an unclosed city boundary ring is refused, never closed implicitly (synthetic data, no network).

Squares live in the fictional lon/lat 0..2 extent. Run from the repository root:
python3 -B -m unittest tests.test_camera_boundary_closure -v
"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from campaign_tool import camera_counts, camera_csv  # noqa: E402
from campaign_tool.camera_counts import count_by_name, point_in_polygon, valid_ring  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402
from tests.test_camera_counts import collection, feature, points, square  # noqa: E402
from tests.test_site_cameras_page import NODE, point  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TAIL = " inside city limits; ownership unconfirmed"
CLOSED = square(0.5, 0.5, 1.5, 1.5)
OPEN = CLOSED[:-1]  # the closing position removed: four distinct positions
HOLE = square(0.8, 0.8, 1.2, 1.2)
OPEN_HOLE = HOLE[:-1]
REASON = "invalid ring (needs a closed ring of 4 or more finite numeric positions)"


def cameras_at(*coords):
    return points(*coords)


class RingTests(unittest.TestCase):
    def test_open_outer_ring_is_not_implicitly_closed(self):
        triangle = [[0.5, 0.5], [1.5, 0.5], [1.0, 1.5], [0.5, 0.5]]
        self.assertTrue(valid_ring(CLOSED))
        self.assertTrue(valid_ring(triangle))
        self.assertFalse(valid_ring(OPEN))
        before = copy.deepcopy(OPEN)
        self.assertFalse(point_in_polygon((1.0, 1.0), [OPEN]))
        counts, problems = count_by_name(cameras_at((1.0, 1.0)), collection([feature("Cedar", "Polygon", [OPEN])]))
        self.assertEqual(counts, {})
        self.assertEqual(problems, [f"city boundary feature 0: {REASON}"])
        self.assertEqual(OPEN, before)
        self.assertEqual(len(OPEN), 4)
        controls, none = count_by_name(cameras_at((1.0, 1.0)), collection([feature("Cedar", "Polygon", [CLOSED])]))
        self.assertEqual((controls, none), ({"Cedar": 1}, []))

    def test_open_hole_invalidates_the_whole_polygon(self):
        geometry = [CLOSED, OPEN_HOLE]
        for where in ((1.0, 1.0), (0.6, 0.6)):  # inside the hole, outside the hole
            with self.subTest(where=where):
                self.assertFalse(point_in_polygon(where, geometry))
        counts, problems = count_by_name(cameras_at((1.0, 1.0), (0.6, 0.6)),
                                         collection([feature("Cedar", "Polygon", geometry)]))
        self.assertEqual(counts, {})
        self.assertEqual(problems, [f"city boundary feature 0: {REASON}"])
        self.assertEqual(camera_csv.camera_rows(cameras_at((0.6, 0.6)), collection([feature("Cedar", "Polygon", geometry)]),
                                                {})[0]["city"], "")
        self.assertTrue(point_in_polygon((0.6, 0.6), [CLOSED, HOLE]))
        self.assertFalse(point_in_polygon((1.0, 1.0), [CLOSED, HOLE]))

    def test_later_open_multipolygon_ring_invalidates_all_parts(self):
        later_outer = feature("Cedar", "MultiPolygon", [[CLOSED], [square(1.6, 1.6, 1.9, 1.9)[:-1]]])
        later_hole = feature("Cedar", "MultiPolygon", [[CLOSED], [square(1.6, 1.6, 1.9, 1.9), OPEN_HOLE]])
        for bad in (later_outer, later_hole):
            counts, problems = count_by_name(cameras_at((1.0, 1.0)), collection([bad]))
            self.assertEqual(counts, {})  # the earlier valid part, which holds the point, is not kept
            self.assertEqual(problems, [f"city boundary feature 0: {REASON}"])
            self.assertEqual(camera_csv.camera_rows(cameras_at((1.0, 1.0)), collection([bad]), {})[0]["city"], "")

    def test_endpoint_equality_is_exact_and_dimension_preserving(self):
        self.assertTrue(valid_ring([[0, 0], [1, 0], [1, 1], [0.0, 0.0]]))  # int and float spellings are equal
        self.assertTrue(valid_ring([[0, 0, 5], [1, 0, 5], [1, 1, 5], [0.0, 0, 5.0]]))
        self.assertFalse(valid_ring([[0, 0], [1, 0], [1, 1], [0, 1e-12]]))  # no tolerance
        self.assertFalse(valid_ring([[0, 0], [1, 0], [1, 1], [0, 0, 0]]))  # differing endpoint lengths
        self.assertFalse(valid_ring([[0, 0, 1], [1, 0], [1, 1], [0, 0, 2]]))  # differing trailing ordinate
        self.assertFalse(valid_ring([[0, 0, 1], [1, 0], [1, 1], [0, 0, 1.5]]))

    def test_invalid_feature_does_not_poison_valid_same_name_siblings(self):
        good = feature("Cedar", "Polygon", [square(0.5, 0.5, 1.0, 1.0)])
        overlapping = feature("Cedar", "Polygon", [square(0.7, 0.5, 1.2, 1.0)])
        bad = feature("Cedar", "Polygon", [OPEN])
        cameras = cameras_at((0.8, 0.7), (1.1, 0.7), (1.4, 1.4))
        for order, bad_index in (([good, overlapping, bad], 2), ([bad, good, overlapping], 0)):
            counts, problems = count_by_name(cameras, collection(order))
            self.assertEqual(counts, {"Cedar": 2})  # the overlap is counted once; the open feature adds nothing
            self.assertEqual(problems, [f"city boundary feature {bad_index}: {REASON}"])
        # CSV: the invalid containing feature is ignored, so a valid alternate name still attributes the point
        alternate = feature("Elm", "Polygon", [square(1.0, 1.0, 1.9, 1.9)])
        rows = camera_csv.camera_rows(cameras_at((1.4, 1.4)), collection([bad, alternate]), {})
        self.assertEqual(rows[0]["city"], "Elm")
        rows = camera_csv.camera_rows(cameras_at((1.4, 1.4)), collection([bad]), {})
        self.assertEqual(rows[0]["city"], "")


class _CampaignCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-boundary-closure-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.map_dir = self.root / "content" / "map"

    def write_map(self, boundaries, cameras):
        (self.map_dir / "city-boundaries.geojson").write_text(json.dumps(collection(boundaries)))
        (self.map_dir / "cameras.geojson").write_text(json.dumps(cameras))
        config = json.loads((self.map_dir / "map-config.json").read_text(encoding="utf-8"))
        config["initial_point_count"] = len(cameras["features"])
        (self.map_dir / "map-config.json").write_text(json.dumps(config))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


class BuildTests(_CampaignCase):
    def build(self):
        notes = []
        build_site(self.root, notes=notes)
        data = (self.root / "public" / "site-data.js").read_text(encoding="utf-8")
        csv_bytes = (self.root / "public" / "data" / "cameras.csv").read_bytes()
        match = re.search(r"var CITIES=(\{.*\});", data)
        assert match is not None
        return json.loads(match.group(1)), notes, data, csv_bytes

    def test_build_omits_inferred_count_and_csv_city_for_open_boundary(self):
        cameras = collection([point(1.0, 1.0, url=NODE + "10", manufacturer="X", operator="Y")])
        self.write_map([feature("Cedar", "Polygon", [CLOSED])], cameras)
        cities, notes, _, raw = self.build()
        self.assertEqual(cities["cedar"]["cameraCount"], "1 community-mapped point" + TAIL)
        self.assertEqual(notes, [])
        self.assertIn(b",Cedar,", raw)
        self.write_map([feature("Cedar", "Polygon", [OPEN])], cameras)
        cities, notes, data, raw = self.build()
        self.assertEqual(cities["cedar"]["cameraCount"], "")  # an empty inferred count, not "0 community-mapped points"
        self.assertEqual(notes, [f"city boundary feature 0: {REASON}"])
        self.assertNotIn(b",Cedar,", raw)
        self.assertEqual(self.build(), (cities, notes, data, raw))  # a repeat build is deterministic

    def test_explicit_organizer_count_is_not_overwritten_by_open_boundary(self):
        path = self.root / "content" / "agencies.json"
        agencies = json.loads(path.read_text())
        agencies.append({"agency_id": "ca-pine-police", "slug": "pine", "name": "Pine Police Department (fictional)",
                         "kind": "police", "jurisdiction_name": "Pine", "profile": {"cameraCount": "About 12 per records"}})
        path.write_text(json.dumps(agencies))
        cities_path = self.map_dir / "cities.json"
        cities = json.loads(cities_path.read_text())
        cities["pine"] = {"name": "Pine", "bounds": [[1.5, 0.5], [1.9, 1.5]]}
        cities_path.write_text(json.dumps(cities))
        self.write_map([feature("Pine", "Polygon", [square(1.5, 0.5, 1.9, 1.5)[:-1]])],
                       collection([point(1.7, 1.0, url=NODE + "10")]))
        built, notes, _, _ = self.build()
        self.assertEqual(built["pine"]["cameraCount"], "About 12 per records")
        self.assertEqual(notes, [f"city boundary feature 0: {REASON}"])


class SurveySheetTests(_CampaignCase):
    def setUp(self):
        super().setUp()
        self.cameras = collection([point(0.6, 0.6, manufacturer="X", operator="Cedar PD", url=NODE + "30")])
        self.checks = self.map_dir / "point-checks.json"
        self.checks.write_bytes(b'{"schema_version": 1, "checks": []}')

    def sheet(self):
        return self.run_cli("map", "survey-sheet", "--city", "Cedar", "--directory", str(self.root))

    def test_closed_control_succeeds(self):
        self.write_map([feature("Cedar", "Polygon", [CLOSED])], self.cameras)
        result = self.sheet()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list((self.root / "kit" / "survey").glob("cedar-*.csv"))), 1)

    def test_survey_sheet_refuses_open_boundary_before_writes(self):
        self.write_map([feature("Cedar", "Polygon", [OPEN])], self.cameras)
        checks_before = self.checks.read_bytes()
        result = self.sheet()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"city boundaries: city boundary feature 0: {REASON}", result.stderr)
        for secret in ("0.6", "1.5", "Cedar PD"):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertFalse((self.root / "kit").exists())  # absent private output root: nothing created
        self.assertEqual(self.checks.read_bytes(), checks_before)
        # An existing private output root keeps its sentinel worksheet byte for byte and gains nothing.
        folder = self.root / "kit" / "survey"
        folder.mkdir(parents=True, mode=0o700)
        (self.root / "kit").chmod(0o700)
        sentinel = folder / "cedar-sentinel.csv"
        sentinel.write_bytes(b"sentinel,worksheet\r\n")
        result = self.sheet()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["cedar-sentinel.csv"])
        self.assertEqual(sentinel.read_bytes(), b"sentinel,worksheet\r\n")
        self.assertEqual(self.checks.read_bytes(), checks_before)

    def test_unrelated_open_boundary_still_refuses_the_whole_survey(self):
        self.write_map([feature("Cedar", "Polygon", [CLOSED]), feature("Elm", "Polygon", [OPEN])], self.cameras)
        result = self.sheet()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"city boundary feature 1: {REASON}", result.stderr)
        self.assertFalse((self.root / "kit").exists())


if __name__ == "__main__":
    unittest.main()
