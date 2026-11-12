"""RF1530-11: ray-crossing membership survives finite coordinates whose derived arithmetic overflows.

Synthetic geometry only; the oracle is exact rational arithmetic on the ORIGINAL supplied coordinates and does not
touch the module's edge, crossing or ring helpers. Scales such as 1e308 are extreme-input hardening, not geography.
Run from the repository root: python3 -B -m unittest tests.test_camera_ray_overflow -q
"""
import copy
import csv
import hashlib
import io
import json
import math
import re
import shutil
import socket
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

from campaign_tool import camera_counts, camera_csv, map_survey
from campaign_tool.camera_counts import count_by_name, point_in_polygon, point_in_ring
from campaign_tool.content import load_content
from campaign_tool.site import build_site

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
TAIL = " inside city limits; ownership unconfirmed"
OPPOSITE = [[-1e308, -1e308], [1e308, -1e308], [-1e308, 1e308], [-1e308, -1e308]]  # interior is x + y < 0
REAL_EXACT = camera_counts._exact_left_of_crossing


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


def oracle_polygon(point, rings):
    return oracle_ring(point[0], point[1], rings[0]) and not any(oracle_ring(point[0], point[1], h) for h in rings[1:])


def triangle(s, reverse=False):
    ring = [[0, 0], [s, 0], [0, s], [0, 0]]
    return ring[::-1] if reverse else ring


def feature(name, kind, coordinates, **properties):
    return {"type": "Feature", "properties": {"NAME": name, **properties},
            "geometry": {"type": kind, "coordinates": coordinates}}


def collection(features):
    return {"type": "FeatureCollection", "features": features}


def cameras_at(*points, **properties):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": dict(properties), "geometry": {"type": "Point", "coordinates": list(p)}}
        for p in points]}


def plain(point, ring):
    return point_in_ring(point[0], point[1], ring)


def indexed(point, ring):
    return camera_counts._Ring(ring).contains(point[0], point[1])


def spy_exact():
    """Patch the exact helper with a recording wrapper around the real one."""
    return mock.patch.object(camera_counts, "_exact_left_of_crossing", side_effect=REAL_EXACT)


class OverflowMembershipTests(unittest.TestCase):
    def test_product_overflow_triangle_keeps_membership_in_both_windings(self):
        for s in (1e200, 1e308):
            points = [(0.25, 0.25), (s / 4, s / 4), (0.75 * s, 0.75 * s)]
            for reverse in (False, True):
                ring = triangle(s, reverse)
                boundaries = collection([feature("Cedar", "Polygon", [ring])])
                cameras = cameras_at(*points)
                before = copy.deepcopy((cameras, boundaries))
                self.assertTrue(camera_counts.valid_ring(ring))
                with spy_exact() as exact:
                    self.assertEqual([plain(p, ring) for p in points], [True, True, False], (s, reverse))
                    self.assertEqual([indexed(p, ring) for p in points], [True, True, False], (s, reverse))
                    self.assertEqual(count_by_name(cameras, boundaries), ({"Cedar": 2}, []), (s, reverse))
                self.assertTrue(exact.called)  # the overflowing edges reached the exact comparison
                self.assertEqual([oracle_ring(p[0], p[1], ring) for p in points], [True, True, False])
                self.assertEqual([r["city"] for r in camera_csv.camera_rows(cameras, boundaries, {})],
                                 ["Cedar", "Cedar", ""])
                self.assertEqual(list(map_survey._in_city(cameras, boundaries, "Cedar")), [0, 1])
                self.assertEqual((cameras, boundaries), before)

    def test_opposite_extreme_endpoints_keep_normal_camera_inside(self):
        points = [(-1.0, -1.0), (1.0, 1.0)]  # x + y < 0 is the interior of OPPOSITE
        for reverse in (False, True):
            ring = OPPOSITE[::-1] if reverse else OPPOSITE
            self.assertTrue(camera_counts.valid_ring(ring))
            self.assertEqual(camera_counts._Ring(ring).count, 1)  # the span overflows: the existing one-band path
            self.assertEqual([plain(p, ring) for p in points], [True, False], reverse)
            self.assertEqual([indexed(p, ring) for p in points], [True, False], reverse)
            self.assertEqual([oracle_ring(p[0], p[1], ring) for p in points], [True, False], reverse)
            self.assertEqual(count_by_name(cameras_at(*points), collection([feature("Cedar", "Polygon", [ring])])),
                             ({"Cedar": 1}, []))

    def test_finite_true_crossing_survives_overflowed_intermediates(self):
        # The slanted edge (1e308, -1e308) -> (-1e308, 1e308) crosses y = -1 at the finite x = 1.
        edge = (1e308, -1e308, -1e308, 1e308)
        for reverse in (False, True):
            ring = OPPOSITE[::-1] if reverse else OPPOSITE
            cases = [((0.5, -1.0), True), ((1.0, -1.0), False), ((1.0000001, -1.0), False), ((-5.0, -1.0), True)]
            for point, want in cases:
                with spy_exact() as exact:
                    self.assertEqual(plain(point, ring), want, (reverse, point))
                    self.assertEqual(indexed(point, ring), want, (reverse, point))
                self.assertEqual(oracle_ring(point[0], point[1], ring), want)
                self.assertTrue(exact.called)
                for call in exact.call_args_list:
                    x, y, x1, y1, x2, y2 = call.args
                    self.assertEqual((x, y), point)  # original operands, never inf, NaN or rounded differences
                    self.assertTrue(all(math.isfinite(v) for v in call.args), call.args)
                    self.assertIn((x1, y1, x2, y2), [edge, edge[2:] + edge[:2], (-1e308, 1e308, -1e308, -1e308),
                                                     (-1e308, -1e308, -1e308, 1e308)])

    def test_representable_integer_inputs_with_oversized_products_do_not_crash(self):
        rings = {
            "oversized product": ([[0, 0], [10 ** 200, 0], [0, 10 ** 200], [0, 0]], [(1, 1), (10 ** 199, 10 ** 199),
                                                                                    (10 ** 200, 10 ** 200)]),
            "oversized differences": (json.loads("[[-%s,-%s],[%s,-%s],[-%s,%s],[-%s,-%s]]" % ((10 ** 308,) * 8)),
                                      [(-1, -1), (1, 1), (-10 ** 307, -10 ** 307)]),
            "int difference with float factor": ([[-10 ** 308, 0.0], [10 ** 308, 1.0], [-10 ** 308, 1.0],
                                                  [-10 ** 308, 0.0]], [(-1, 0.5), (1, 0.5), (-1.0, 0.25)]),
            "mixed int and float": ([[-10 ** 308, -10 ** 308], [1e308, -1e308], [-1e308, 10 ** 308],
                                     [-10 ** 308, -10 ** 308]], [(-1, -1), (1.0, 1), (0, 0),
                                                                 (0.0, -10 ** 308 - 10 ** 291),  # int/float rounded together: dn == 0.0
                                                                 (-10 ** 308, -10 ** 308 - 10 ** 291)]),
        }
        for label, (ring, points) in rings.items():
            self.assertTrue(camera_counts.valid_ring(ring), label)
            for reverse in (False, True):
                r = ring[::-1] if reverse else ring
                for point in points:
                    want = oracle_ring(point[0], point[1], r)
                    self.assertEqual(plain(point, r), want, (label, reverse, point))
                    self.assertEqual(indexed(point, r), want, (label, reverse, point))
        self.assertEqual([oracle_ring(*p, rings["int difference with float factor"][0]) for p in ((-1, 0.5), (1, 0.5))],
                         [True, False])
        # Input admission is unchanged: an integer no float can represent is still refused, never computed.
        huge = [[0, 0], [10 ** 400, 0], [0, 10 ** 400], [0, 0]]
        self.assertFalse(camera_counts.valid_ring(huge))
        self.assertFalse(plain((1, 1), huge))
        self.assertEqual(count_by_name(cameras_at((1, 1)), collection([feature("Cedar", "Polygon", [huge])])),
                         ({}, ["city boundary feature 0: invalid ring (needs a closed ring of 4 or more finite numeric positions)"]))

    def test_overflow_holes_multipolygons_and_overlap(self):
        s, h = 1e308, 1e200
        inside_hole, in_hole_bbox, in_outer, beyond, other, other_beyond = (
            (h / 4, h / 4), (0.75 * h, 0.75 * h), (s / 4, s / 4), (0.75 * s, 0.75 * s), (-s / 4, -s / 4),
            (-0.75 * s, -0.75 * s))
        points = [inside_hole, in_hole_bbox, in_outer, beyond, other, other_beyond]
        for rev_outer in (False, True):
            for rev_hole in (False, True):
                outer, hole = triangle(s, rev_outer), triangle(h, rev_hole)
                second = [[0, 0], [-s, 0], [0, -s], [0, 0]]
                polygons = [[outer, hole], [second[::-1] if rev_outer else second]]
                expected = [any(oracle_polygon(p, rings) for rings in polygons) for p in points]
                self.assertEqual(expected, [False, True, True, False, True, False])
                for rings in polygons:
                    self.assertEqual([point_in_polygon(p, rings) for p in points], [oracle_polygon(p, rings) for p in points])
                boundaries = collection([
                    feature("Cedar", "MultiPolygon", polygons),
                    feature("Cedar", "Polygon", [outer, hole]),  # same name, overlaps the first part: counted once
                    feature("Pine", "Polygon", [outer, hole]),
                ])
                cameras = cameras_at(*points)
                with spy_exact() as exact:
                    counts, problems = count_by_name(cameras, boundaries)
                self.assertTrue(exact.called)
                self.assertEqual((counts, problems), ({"Cedar": 3, "Pine": 2}, []), (rev_outer, rev_hole))
                rows = camera_csv.camera_rows(cameras, boundaries, {})
                self.assertEqual([r["city"] for r in rows], ["", "Cedar", "Cedar", "", "Cedar", ""])

    def test_shared_slanted_edge_keeps_half_open_ownership(self):
        s = 2.0 ** 1000  # s * s overflows; every query below is exactly representable
        lower = [[0, 0], [s, 0], [0, s], [0, 0]]
        upper = [[s, 0], [s, s], [0, s], [s, 0]]
        calls = 0
        for flip in (False, True):
            a, b = (lower[::-1], upper[::-1]) if flip else (lower, upper)
            for i in range(0, 9):
                for j in range(0, 9):
                    p = (s * i / 8, s * j / 8)
                    with spy_exact() as exact:
                        owners = [plain(p, a), plain(p, b)]
                        self.assertEqual([indexed(p, a), indexed(p, b)], owners)
                    calls += exact.call_count
                    self.assertEqual(owners, [oracle_ring(p[0], p[1], a), oracle_ring(p[0], p[1], b)], (flip, p))
            mid = (s / 2, s / 2)  # on the shared diagonal: owned once, never twice, never by neither
            self.assertEqual(plain(mid, a) + plain(mid, b), 1)
            self.assertEqual(plain((s / 8, s / 8), a) + plain((s / 8, s / 8), b), 1)  # strictly below the diagonal
            self.assertEqual(plain((s, s), a) + plain((s, s), b), 0)  # the far vertex is outside both
        self.assertGreater(calls, 0)
        # Zero, vertical and horizontal displacement controls stay on the float path at the same extreme scale.
        squares = [[[0, 0], [s, 0], [s, s], [0, s], [0, 0]], [[s, 0], [2 * s, 0], [2 * s, s], [s, s], [s, 0]]]
        with mock.patch.object(camera_counts, "_exact_left_of_crossing", side_effect=AssertionError("fallback")):
            for p in [(s / 2, s / 2), (s, s / 2), (0, 0), (s, s), (s / 2, 0), (s / 2, s), (3 * s, s / 2)]:
                self.assertEqual([plain(p, r) for r in squares], [oracle_ring(p[0], p[1], r) for r in squares], p)
                self.assertEqual([indexed(p, r) for r in squares], [oracle_ring(p[0], p[1], r) for r in squares], p)

    def test_ordinary_fast_path_and_merged_underflow_remain(self):
        square = [[0.1, 0.1], [1.9, 0.1], [1.9, 1.9], [0.1, 1.9], [0.1, 0.1]]
        slanted = [[0.1, 0.1], [1.9, 0.3], [1.0, 1.9], [0.1, 0.1]]
        with mock.patch.object(camera_counts, "_exact_left_of_crossing", side_effect=AssertionError("fallback")):
            for ring in (square, slanted):
                for p in [(0.5, 0.5), (1.0, 1.0), (0.05, 0.5), (1.95, 1.0), (1.5, 0.2)]:
                    self.assertEqual(plain(p, ring), oracle_ring(p[0], p[1], ring), p)
                    self.assertEqual(indexed(p, ring), oracle_ring(p[0], p[1], ring), p)
        s = 1e-200  # the product underflows: still the exact comparison, still the exact answer
        points = [(s / 4, s / 4), (3 * s / 4, 3 * s / 4)]
        for reverse in (False, True):
            with spy_exact() as exact:
                self.assertEqual([plain(p, triangle(s, reverse)) for p in points], [True, False])
            self.assertTrue(exact.called)
        # Existing contract: the finite +-1e308 square (span overflows) is still counted, vertical edges and all.
        big = [[-1e308, -1e308], [1e308, -1e308], [1e308, 1e308], [-1e308, 1e308], [-1e308, -1e308]]
        self.assertEqual(count_by_name(cameras_at((0.5, 0.5), (1.0, 1.0)), collection([feature("Huge", "Polygon", [big])])),
                         ({"Huge": 2}, []))
        # Malformed scalars and unclosed rings are still refused before any arithmetic.
        nan_ring = json.loads("[[0, 0], [1, NaN], [0, 1], [0, 0]]")
        self.assertFalse(camera_counts.valid_ring(nan_ring))
        self.assertFalse(camera_counts.valid_ring([[0, 0], [1, 0], [0, 1], [0, 0.5]]))


class RealBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-camera-overflow-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))

    def snapshot(self):
        return {str(p.relative_to(self.root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode)
                for p in sorted(self.root.rglob("*")) if p.is_file() and "public" not in p.relative_to(self.root).parts}

    def test_real_loader_build_csv_and_survey_keep_normal_camera(self):
        boundaries = collection([feature("Cedar", "Polygon", [OPPOSITE])])
        cameras = cameras_at((-1.0, -1.0), url="https://www.openstreetmap.org/node/1")
        map_dir = self.root / "content" / "map"
        (map_dir / "city-boundaries.geojson").write_text(json.dumps(boundaries))
        (map_dir / "cameras.geojson").write_text(json.dumps(cameras))
        before = self.snapshot()
        notes = []
        refused = AssertionError("network is forbidden")
        with mock.patch.object(socket, "socket", side_effect=refused), \
                mock.patch.object(socket, "create_connection", side_effect=refused):
            self.assertTrue(load_content(self.root))
            build_site(self.root, notes=notes)
        data = (self.root / "public" / "site-data.js").read_text(encoding="utf-8")
        cities = json.loads(re.search(r"var CITIES=(\{.*\});", data).group(1))
        self.assertEqual(cities["cedar"]["cameraCount"], "1 community-mapped point" + TAIL)
        self.assertEqual(notes, [])
        rows = list(csv.DictReader(io.StringIO((self.root / "public" / "data" / "cameras.csv").read_text(encoding="utf-8"))))
        self.assertEqual([r["city"] for r in rows], ["Cedar"])
        self.assertEqual(list(map_survey._in_city(cameras, boundaries, "Cedar")), [0])
        self.assertEqual(self.snapshot(), before)  # inputs untouched: bytes and modes


if __name__ == "__main__":
    unittest.main()
