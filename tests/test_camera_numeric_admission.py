"""RF1925-9: oversized decoded JSON integers follow existing geometry refusal policies."""
import copy
import json
import re
import unittest

from campaign_tool import camera_counts, camera_csv, map_survey
from campaign_tool.site import build_site
from tests.test_camera_boundary_closure import CLOSED, HOLE, REASON, TAIL, _CampaignCase
from tests.test_camera_counts import collection, feature, points, square
from tests.test_site_cameras_page import NODE, point

HUGE = json.loads("1" + "0" * 400)


def bad_ring(value=HUGE, axis=0, position=1, ring=CLOSED):
    result = copy.deepcopy(ring)
    result[position][axis] = value
    if position in (0, len(result) - 1):
        result[0][axis] = result[-1][axis] = value
    return result


class NumericTests(unittest.TestCase):
    def test_json_oversized_integer_is_false_not_overflow(self):
        for value in (HUGE, json.loads("-1" + "0" * 400), True, False, None, "1", [], {},
                      float("nan"), float("inf"), float("-inf")):
            with self.subTest(kind=type(value).__name__):
                self.assertFalse(camera_counts._number(value))
        for value in (0, -3, 2.5, 1e300):
            self.assertTrue(camera_counts._number(value))

    def test_huge_outer_and_hole_ordinates_reject_whole_feature(self):
        for sign in (1, -1):
            for axis in (0, 1):
                for position in (0, 1, 4):
                    ring = bad_ring(sign * HUGE, axis, position)
                    hole = bad_ring(sign * HUGE, axis, position, HOLE)
                    for kind, polygons in (("Polygon", [ring]), ("Polygon", [CLOSED, hole]),
                                           ("MultiPolygon", [[CLOSED], [ring]]),
                                           ("MultiPolygon", [[CLOSED], [CLOSED, hole]])):
                        with self.subTest(sign=sign, axis=axis, position=position, kind=kind):
                            boundary = collection([feature("NumericMarker", kind, polygons)])
                            before = copy.deepcopy(boundary)
                            self.assertFalse(camera_counts.valid_ring(ring))
                            self.assertFalse(camera_counts.point_in_polygon((0.6, 0.6), [ring]))
                            self.assertFalse(camera_counts.point_in_polygon((0.6, 0.6), [CLOSED, hole]))
                            counts, problems = camera_counts.count_by_name(points((0.6, 0.6)), boundary)
                            self.assertEqual(counts, {})
                            self.assertEqual(problems, [f"city boundary feature 0: {REASON}"])
                            self.assertNotIn(str(HUGE), " ".join(problems))
                            self.assertNotIn("NumericMarker", " ".join(problems))
                            self.assertEqual(boundary, before)

    def test_huge_camera_ordinate_does_not_poison_valid_points(self):
        boundaries = collection([feature("Cedar", "Polygon", [CLOSED])])
        for sign in (1, -1):
            for axis in (0, 1):
                bad = [0.7, 0.7]
                bad[axis] = sign * HUGE
                cameras = points((0.6, 0.6), bad, (1.4, 1.4))
                before = copy.deepcopy(cameras)
                self.assertEqual(camera_counts._points(cameras), [(0.6, 0.6), (1.4, 1.4)])
                self.assertEqual(camera_counts.count_by_name(cameras, boundaries), ({"Cedar": 2}, []))
                self.assertEqual(cameras, before)

    def test_bad_feature_and_valid_same_name_sibling_are_order_independent(self):
        good = feature("Cedar", "Polygon", [square(0.5, 0.5, 1.0, 1.0)])
        overlap = copy.deepcopy(good)
        bad = feature("Cedar", "Polygon", [CLOSED, bad_ring(ring=HOLE)])
        cameras = points((0.6, 0.6), (1.4, 1.4))
        for features, index in (([bad, good, overlap], 0), ([good, overlap, bad], 2)):
            boundaries = collection(features)
            self.assertEqual(camera_counts.count_by_name(cameras, boundaries),
                             ({"Cedar": 1}, [f"city boundary feature {index}: {REASON}"]))
            self.assertEqual([r["city"] for r in camera_csv.camera_rows(cameras, boundaries, {})], ["Cedar", ""])
        alternate = feature("Elm", "Polygon", [CLOSED])
        self.assertEqual(camera_csv.camera_rows(points((1.4, 1.4)), collection([bad, alternate]), {})[0]["city"], "Elm")

    def test_float_infinity_and_unclosed_ring_contracts_remain(self):
        for ring in (bad_ring(float("inf")), bad_ring(float("-inf")), bad_ring(float("nan")), CLOSED[:-1]):
            self.assertFalse(camera_counts.valid_ring(ring))
            self.assertEqual(camera_counts.count_by_name(points((0.6, 0.6)),
                             collection([feature("Cedar", "Polygon", [ring])])),
                             ({}, [f"city boundary feature 0: {REASON}"]))
        self.assertTrue(camera_counts.valid_ring([[0, 0], [2, 0], [2, 2], [0.0, 0.0]]))
        self.assertTrue(camera_counts.point_in_polygon((0.6, 0.6), [CLOSED, HOLE]))
        self.assertFalse(camera_counts.point_in_polygon((1.0, 1.0), [CLOSED, HOLE]))


class ConsumerTests(_CampaignCase):
    def setUp(self):
        super().setUp()
        self.cameras = collection([point(0.6, 0.6, url=NODE + "30", manufacturer="X", operator="Y")])

    def build(self):
        notes = []
        build_site(self.root, notes=notes)
        public = self.root / "public"
        data = (public / "site-data.js").read_text(encoding="utf-8")
        match = re.search(r"var CITIES=(\{.*\});", data)
        self.assertIsNotNone(match)
        assert match is not None
        cities = json.loads(match.group(1))
        snapshot = {str(p.relative_to(public)): p.read_bytes() for p in public.rglob("*") if p.is_file()}
        return cities, notes, snapshot

    def test_build_and_csv_refuse_only_bad_boundary_membership(self):
        good = feature("Cedar", "Polygon", [CLOSED])
        self.write_map([good], self.cameras)
        control = self.build()
        self.assertEqual(control[0]["cedar"]["cameraCount"], "1 community-mapped point" + TAIL)
        self.assertEqual(control[1], [])
        self.assertIn(b",Cedar,", control[2]["data/cameras.csv"])
        self.assertEqual(self.build(), control)
        for bad in (feature("Cedar", "Polygon", [bad_ring()]),
                    feature("Cedar", "Polygon", [CLOSED, bad_ring(-HUGE, 1, 0, HOLE)])):
            self.write_map([bad], self.cameras)
            boundary_path = self.map_dir / "city-boundaries.geojson"
            raw = boundary_path.read_bytes()
            cities, notes, snapshot = self.build()
            self.assertEqual(cities["cedar"]["cameraCount"], "")
            self.assertEqual(notes, [f"city boundary feature 0: {REASON}"])
            self.assertNotIn(b",Cedar,", snapshot["data/cameras.csv"])
            self.assertEqual(snapshot["data/city-boundaries.geojson"], raw)
            self.assertEqual(boundary_path.read_bytes(), raw)
            self.assertEqual(self.build(), (cities, notes, snapshot))
        self.write_map([good], self.cameras)
        self.assertEqual(self.build(), control)

    def test_survey_huge_boundary_refuses_before_writes(self):
        checks = self.map_dir / "point-checks.json"
        checks.write_bytes(b'{"schema_version": 1, "checks": []}')
        checks_before = checks.read_bytes()
        good = feature("Cedar", "Polygon", [CLOSED])
        self.assertEqual(map_survey._in_city(self.cameras, collection([good]), "Cedar"), [0])
        for features, index in (([feature("Cedar", "Polygon", [bad_ring()])], 0),
                                ([good, feature("Elm", "Polygon", [CLOSED, bad_ring(-HUGE, 1, 0, HOLE)])], 1)):
            self.write_map(features, self.cameras)
            expected = f"city boundaries: city boundary feature {index}: {REASON}"
            with self.assertRaisesRegex(map_survey.SurveyError, re.escape(expected)):
                map_survey._in_city(self.cameras, collection(features), "Cedar")
            folder = self.root / "kit" / "survey"
            for existing in (False, True):
                if existing:
                    folder.mkdir(parents=True, mode=0o700)
                    (self.root / "kit").chmod(0o700)
                    (folder / "cedar-sentinel.csv").write_bytes(b"sentinel,worksheet\r\n")
                before = {str(p.relative_to(self.root)): p.read_bytes()
                          for p in self.root.rglob("*") if p.is_file()}
                result = self.run_cli("map", "survey-sheet", "--city", "Cedar", "--directory", str(self.root))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(expected, result.stderr)
                for forbidden in ("Traceback", "OverflowError", str(HUGE), "Cedar", "Elm"):
                    self.assertNotIn(forbidden, result.stdout + result.stderr)
                self.assertEqual({str(p.relative_to(self.root)): p.read_bytes()
                                  for p in self.root.rglob("*") if p.is_file()}, before)
                self.assertEqual(checks.read_bytes(), checks_before)
                if existing:
                    self.assertEqual([p.name for p in folder.iterdir()], ["cedar-sentinel.csv"])
                    (folder / "cedar-sentinel.csv").unlink()
                    folder.rmdir()
                    folder.parent.rmdir()
                else:
                    self.assertFalse(folder.parent.exists())
        self.write_map([good], self.cameras)
        result = self.run_cli("map", "survey-sheet", "--city", "Cedar", "--directory", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list((self.root / "kit" / "survey").glob("cedar-*.csv"))), 1)


if __name__ == "__main__":
    unittest.main()
