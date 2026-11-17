"""RF1201-3: ray-crossing membership survives a finite crossing quotient that underflows.

Synthetic geometry only; the oracle is exact rational arithmetic on the ORIGINAL supplied coordinates.
Run from the repository root: python3 -B -m unittest tests.test_camera_ray_quotient_underflow -q
"""
import copy
import unittest
from fractions import Fraction
from unittest import mock

from campaign_tool import camera_counts, camera_csv, map_survey
from campaign_tool.camera_counts import count_by_name, point_in_polygon, point_in_ring

RING = [[0, 0], [1e-200, 1e200], [0, 1e200], [0, 0]]
CAMERA = (0, 1e-100)


def oracle_ring(x, y, ring):
    inside = False
    for a, b in zip(ring, ring[1:]):
        if (a[1] > y) != (b[1] > y):
            at = Fraction(a[0]) + (Fraction(y) - Fraction(a[1])) * (Fraction(b[0]) - Fraction(a[0])) / (
                Fraction(b[1]) - Fraction(a[1]))
            if Fraction(x) < at:
                inside = not inside
    return inside


def feature(name, kind, coordinates):
    return {"type": "Feature", "properties": {"NAME": name}, "geometry": {"type": kind, "coordinates": coordinates}}


def collection(features):
    return {"type": "FeatureCollection", "features": features}


def cameras_at(*points):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": list(p)}} for p in points]}


def plain(p, ring):
    return point_in_ring(p[0], p[1], ring)


def indexed(p, ring):
    return camera_counts._Ring(ring).contains(p[0], p[1])


class QuotientUnderflowTests(unittest.TestCase):
    def test_zero_quotient_preserves_positive_crossing(self):
        width, height, y = RING[1][0], RING[1][1], CAMERA[1]
        self.assertTrue(camera_counts.valid_ring(RING))
        self.assertNotEqual(y * width, 0.0)                       # the product itself is a normal nonzero value
        self.assertEqual(y * width / height, 0.0)                 # ...but the quotient underflows to zero
        self.assertGreater(Fraction(y) * Fraction(width) / Fraction(height), 0)
        self.assertTrue(oracle_ring(CAMERA[0], y, RING))
        self.assertTrue(plain(CAMERA, RING))
        self.assertTrue(indexed(CAMERA, RING))

    def test_subnormal_quotient_uses_original_operands(self):
        for sign_w in (1, -1):
            for sign_h in (1, -1):
                for query_x in (0.0, -2.0 ** -1070, 2.0 ** -1070, -2.0 ** -1000, 2.0 ** -1000):
                    width, height, y = sign_w * 2.0 ** -300, sign_h * 2.0 ** 450, 2.0 ** -300
                    # product 2**-600 is normal and above the guard floor; the quotient 2**-1050 is subnormal
                    self.assertNotEqual(y * width / height, 0.0)
                    self.assertLess(abs(y * width / height), 2.0 ** -1022)
                    ring = [[0, 0], [width, height], [0, height], [0, 0]]
                    want = oracle_ring(query_x, y, ring)
                    self.assertEqual(plain((query_x, y), ring), want, (sign_w, sign_h, query_x))
                    self.assertEqual(indexed((query_x, y), ring), want, (sign_w, sign_h, query_x))
                    self.assertEqual(
                        camera_counts._left_of_crossing(query_x, y, 0, 0, width, height),
                        Fraction(query_x) < Fraction(y) * Fraction(width) / Fraction(height))

    def test_true_zero_and_normal_quotients_keep_fast_path(self):
        rings = [[[0.1, 0.1], [1.9, 0.3], [1.0, 1.9], [0.1, 0.1]],
                 [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]], [[0.0, -0.0], [1, 0], [1, 1], [-0.0, 1], [0, 0]]]
        with mock.patch.object(camera_counts, "_exact_left_of_crossing", side_effect=AssertionError("fallback")):
            for ring in rings:
                for p in [(0.5, 0.5), (0, 0), (1, 1), (0.5, 0), (0.5, 1), (1, 0.5), (0, 0.5), (2, 2), (-1, 0.5), (0.25, 0.25)]:
                    self.assertEqual(plain(p, ring), oracle_ring(p[0], p[1], ring), (ring, p))
                    self.assertEqual(indexed(p, ring), oracle_ring(p[0], p[1], ring), (ring, p))

    def test_count_csv_survey_propagate_underflow_fix(self):
        other = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        hole = [[0, 0], [1e-200, 1e200], [1e-200, 0], [0, 0]]
        outer = [[-1, -1], [1, -1], [1, 1e201], [-1, 1e201], [-1, -1]]
        cameras = cameras_at(CAMERA, (5, 5), (50, 50))
        boundaries = collection([feature("Cedar", "Polygon", [RING]), feature("Cedar", "Polygon", [RING]),
                                 feature("Elm", "MultiPolygon", [[other], [RING]]),
                                 feature("Fir", "Polygon", [outer, hole])])
        before = copy.deepcopy((cameras, boundaries))
        expected = {"Cedar": 1, "Elm": 2, "Fir": 1}
        self.assertEqual(count_by_name(cameras, boundaries), (expected, []))
        self.assertEqual([r["city"] for r in camera_csv.camera_rows(cameras, boundaries, {})][0], "Cedar")
        self.assertEqual(list(map_survey._in_city(cameras, boundaries, "Cedar")), [0])
        self.assertEqual(list(map_survey._in_city(cameras, boundaries, "Elm")), [0, 1])
        self.assertEqual((cameras, boundaries), before)
        # the camera lies inside the triangular hole's side of the Fir outer ring, so it is excluded there
        self.assertFalse(point_in_polygon(CAMERA, [outer, [[0, 0], [1e-200, 1e200], [0, 1e200], [0, 0]]]))

    def test_existing_exceptional_arithmetic_remains_compatible(self):
        s = 1e-200  # product underflow
        tri = [[0, 0], [s, 0], [0, s], [0, 0]]
        for p in [(s / 4, s / 4), (3 * s / 4, 3 * s / 4)]:
            self.assertEqual(plain(p, tri), oracle_ring(p[0], p[1], tri))
        big = 1e308  # finite overflow of differences
        sq = [[-big, -big], [big, -big], [big, big], [-big, big], [-big, -big]]
        for p in [(0, 0), (big / 2, -big / 2), (2e307, 3e307)]:
            self.assertEqual(plain(p, sq), oracle_ring(p[0], p[1], sq))
        self.assertFalse(point_in_ring(0, 0, [[0, 0], [float("inf"), 1], [1, 1], [0, 0]]))
        self.assertFalse(point_in_ring(0, 0, [[0, 0], [1, 1], [0, 0]]))


if __name__ == "__main__":
    unittest.main()
