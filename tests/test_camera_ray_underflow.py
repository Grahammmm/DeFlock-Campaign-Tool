"""RF2340-5: ray-crossing membership survives a finite edge product that underflows.

Synthetic geometry only; the oracle is exact rational arithmetic on the ORIGINAL supplied coordinates and does not
touch the module's edge, crossing or ring helpers.
Run from the repository root: python3 -B -m unittest tests.test_camera_ray_underflow -q
"""
import copy
import unittest
from fractions import Fraction
from unittest import mock

from campaign_tool import camera_counts, camera_csv, map_survey
from campaign_tool.camera_counts import count_by_name, point_in_polygon, point_in_ring


def oracle_ring(x, y, ring):
    """Even-odd, half-open membership computed with exact Fractions straight from the supplied positions."""
    inside = False
    for a, b in zip(ring, ring[1:]):
        if (a[1] > y) != (b[1] > y):
            at = Fraction(a[0]) + (Fraction(y) - Fraction(a[1])) * (Fraction(b[0]) - Fraction(a[0])) / (
                Fraction(b[1]) - Fraction(a[1]))
            if Fraction(x) < at:
                inside = not inside
    return inside


def triangle(s, reverse=False):
    ring = [[0, 0], [s, 0], [0, s], [0, 0]]
    return ring[::-1] if reverse else ring


def feature(name, kind, coordinates):
    return {"type": "Feature", "properties": {"NAME": name}, "geometry": {"type": kind, "coordinates": coordinates}}


def collection(features):
    return {"type": "FeatureCollection", "features": features}


def cameras_at(*points):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": list(p)}} for p in points]}


def plain(point, ring):
    return point_in_ring(point[0], point[1], ring)


def indexed(point, ring):
    return camera_counts._Ring(ring).contains(point[0], point[1])


class UnderflowMembershipTests(unittest.TestCase):
    def test_underflow_triangle_keeps_inside_and_outside_in_both_windings(self):
        s = 1e-200
        points = [(s / 4, s / 4), (3 * s / 4, 3 * s / 4)]
        for reverse in (False, True):
            ring = triangle(s, reverse)
            self.assertTrue(camera_counts.valid_ring(ring))
            self.assertEqual([plain(p, ring) for p in points], [True, False])
            self.assertEqual([indexed(p, ring) for p in points], [True, False])
            self.assertEqual([oracle_ring(p[0], p[1], ring) for p in points], [True, False])

    def test_subnormal_product_uses_original_number_oracle(self):
        for s in (1e-160, 2.0 ** -520, 2.0 ** -540, 2.0 ** -600, 1e-200, 1e-300):
            for reverse in (False, True):
                ring = triangle(s, reverse)
                for i in range(1, 8):
                    for j in range(0, 9):
                        point = (s * j / 8, s * i / 8)
                        for dx in (0.0, s * 2.0 ** -40, -s * 2.0 ** -40):  # near, not just on, the diagonal
                            p = (point[0] + dx, point[1])
                            want = oracle_ring(p[0], p[1], ring)
                            self.assertEqual(plain(p, ring), want, (s, reverse, p))
                            self.assertEqual(indexed(p, ring), want, (s, reverse, p))

    def test_legitimate_zero_displacements_keep_fast_path(self):
        rings = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]], [[0.0, -0.0], [1, 0], [1, 1], [-0.0, 1], [0, 0]],
                 [[0, 0], [0, 0], [1, 0], [1, 1], [0, 1], [0, 0]], [[0, 0], [1, 0], [2, 0], [0, 0]]]
        with mock.patch.object(camera_counts, "_exact_left_of_crossing", side_effect=AssertionError("fallback")):
            for ring in rings:
                for p in [(0.5, 0.5), (0, 0), (1, 1), (0.5, 0), (0.5, 1), (1, 0.5), (0, 0.5), (2, 2), (-1, 0.5)]:
                    self.assertEqual(plain(p, ring), oracle_ring(p[0], p[1], ring), (ring, p))
                    self.assertEqual(indexed(p, ring), oracle_ring(p[0], p[1], ring), (ring, p))

    def test_half_open_shared_slanted_edge_has_single_owner(self):
        s = 2.0 ** -600
        lower = [[0, 0], [s, 0], [0, s], [0, 0]]
        upper = [[s, 0], [s, s], [0, s], [s, 0]]
        for flip in (False, True):
            a, b = (lower[::-1], upper[::-1]) if flip else (lower, upper)
            for i in range(0, 9):
                for j in range(0, 9):
                    p = (s * i / 8, s * j / 8)
                    owners = [plain(p, a), plain(p, b)]
                    self.assertEqual(owners, [oracle_ring(p[0], p[1], a), oracle_ring(p[0], p[1], b)], (flip, p))
                    self.assertEqual([indexed(p, a), indexed(p, b)], owners)
            self.assertEqual(plain((s / 2, s / 2), a) + plain((s / 2, s / 2), b), 1)

    def test_tiny_hole_excludes_only_its_actual_interior(self):
        s = 1e-200
        outer = [[-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0], [-1.0, -1.0]]
        for rev_outer in (False, True):
            for rev_hole in (False, True):
                rings = [outer[::-1] if rev_outer else outer, triangle(s, rev_hole)]
                self.assertTrue(point_in_polygon((s / 4, s / 4), rings) is False)       # inside the hole
                self.assertTrue(point_in_polygon((3 * s / 4, 3 * s / 4), rings))       # hole bbox, outside diagonal
                self.assertTrue(point_in_polygon((0.5, 0.5), rings))
                self.assertFalse(point_in_polygon((2.0, 0.5), rings))
                feat = collection([feature("Cedar", "Polygon", rings)])
                counts, problems = count_by_name(cameras_at((s / 4, s / 4), (3 * s / 4, 3 * s / 4), (0.5, 0.5)), feat)
                self.assertEqual((counts, problems), ({"Cedar": 2}, []))

    def test_count_csv_and_survey_agree_with_independent_membership(self):
        s = 1e-200
        for reverse in (False, True):
            boundaries = collection([feature("Cedar", "Polygon", [triangle(s, reverse)])])
            cameras = cameras_at((s / 4, s / 4), (3 * s / 4, 3 * s / 4))
            before = copy.deepcopy((cameras, boundaries))
            self.assertEqual(count_by_name(cameras, boundaries), ({"Cedar": 1}, []))
            self.assertEqual([r["city"] for r in camera_csv.camera_rows(cameras, boundaries, {})], ["Cedar", ""])
            self.assertEqual(list(map_survey._in_city(cameras, boundaries, "Cedar")), [0])
            self.assertEqual((cameras, boundaries), before)

    def test_multipolygon_same_name_overlap_and_multiband_slants(self):
        s = 2.0 ** -600
        n = 8  # staircase-free slanted edge subdivided exactly (powers of two), so several bands are used
        ring = [[0, 0], [s, 0]] + [[s - s * k / n, s * k / n] for k in range(1, n + 1)] + [[0, 0]]
        points = [(s * i / 16, s * j / 16) for i in range(0, 17) for j in range(1, 16)]
        for reverse in (False, True):
            r = ring[::-1] if reverse else ring
            self.assertGreater(camera_counts._Ring(r).count, 1)
            for p in points:
                want = oracle_ring(p[0], p[1], r)
                self.assertEqual(plain(p, r), want, p)
                self.assertEqual(indexed(p, r), want, p)
            # overlapping same-name polygons count each camera once
            tri = triangle(s, reverse)
            boundaries = collection([feature("Cedar", "MultiPolygon", [[tri], [r]]), feature("Cedar", "Polygon", [tri])])
            cams = cameras_at(*points)
            want_count = sum(oracle_ring(p[0], p[1], tri) or oracle_ring(p[0], p[1], r) for p in points)
            self.assertEqual(count_by_name(cams, boundaries), ({"Cedar": want_count}, []))

    def test_normal_float_path_does_not_use_exact_fallback(self):
        square = [[0.1, 0.1], [1.9, 0.1], [1.9, 1.9], [0.1, 1.9], [0.1, 0.1]]
        slanted = [[0.1, 0.1], [1.9, 0.3], [1.0, 1.9], [0.1, 0.1]]
        with mock.patch.object(camera_counts, "_exact_left_of_crossing", side_effect=AssertionError("fallback")):
            for ring in (square, slanted):
                for p in [(0.5, 0.5), (1.0, 1.0), (0.05, 0.5), (1.95, 1.0), (1.5, 0.2)]:
                    self.assertEqual(plain(p, ring), oracle_ring(p[0], p[1], ring), p)
                    self.assertEqual(indexed(p, ring), oracle_ring(p[0], p[1], ring), p)


if __name__ == "__main__":
    unittest.main()
