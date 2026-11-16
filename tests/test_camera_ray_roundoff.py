"""RF0511-3: ray-crossing membership survives ordinary finite rounding of the crossing onto the query.

Synthetic geometry only; the oracle is exact rational arithmetic on the ORIGINAL supplied coordinates and does not
touch the module's edge, crossing, band or exact helpers.
Run from the repository root: python3 -B -m unittest tests.test_camera_ray_roundoff -q
"""
import copy
import csv
import hashlib
import io
import json
import math
import random
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
REAL_EXACT = camera_counts._exact_left_of_crossing
RING = json.loads("[[0,1.5594925198665022],[1.1087103325015797,1.5594925198665022],"
                  "[0.648886272103919,0.2466354431177197],[0,0.2466354431177197],[0,1.5594925198665022]]")
Y = 0.903063981492111
XS = [0.8787983023027492, 0.8787983023027494, 0.8787983023027495, 0.2, 1.7]
EXPECTED = [True, True, False, True, False]  # exact rational membership of the five retained queries


def oracle_ring(x, y, ring):
    inside = False
    for a, b in zip(ring, ring[1:]):
        if (a[1] > y) != (b[1] > y):
            at = Fraction(a[0]) + (Fraction(y) - Fraction(a[1])) * (Fraction(b[0]) - Fraction(a[0])) / (
                Fraction(b[1]) - Fraction(a[1]))
            if Fraction(x) < at:
                inside = not inside
    return inside


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
    return mock.patch.object(camera_counts, "_exact_left_of_crossing", side_effect=REAL_EXACT)


def windings():
    return [copy.deepcopy(RING), copy.deepcopy(RING)[::-1]]


class RoundoffMembershipTests(unittest.TestCase):
    def test_retained_normal_range_interior_is_not_rounded_onto_boundary(self):
        x = XS[1]
        for ring in windings():
            self.assertTrue(oracle_ring(x, Y, ring))
            with spy_exact() as exact:
                self.assertTrue(plain((x, Y), ring))
                self.assertTrue(indexed((x, Y), ring))
            self.assertTrue(exact.called)  # the near-tie took the exact path
            self.assertEqual(count_by_name(cameras_at((x, Y)), collection([feature("Synthetic", "Polygon", [ring])])),
                             ({"Synthetic": 1}, []))
        # every intermediate of the old float formula was finite and normal: the defect is pure roundoff
        x1, y1, x2, y2 = RING[1][0], RING[1][1], RING[2][0], RING[2][1]
        for v in (Y - y1, x2 - x1, y2 - y1, (Y - y1) * (x2 - x1)):
            self.assertTrue(math.isfinite(v) and abs(v) > 1e-300)

    def test_adjacent_floats_and_well_separated_controls(self):
        for ring in windings():
            self.assertEqual([oracle_ring(x, Y, ring) for x in XS], EXPECTED)
            self.assertEqual([plain((x, Y), ring) for x in XS], EXPECTED)
            self.assertEqual([indexed((x, Y), ring) for x in XS], EXPECTED)
        # strict equality: a query exactly on an exactly representable slanted edge is not left of it
        slant = [[0.0, 0.0], [4.0, 4.0], [0.0, 4.0], [0.0, 0.0]]
        for ring in (slant, slant[::-1]):
            for p in [(2.0, 2.0), (math.nextafter(2.0, 0), 2.0), (math.nextafter(2.0, 9), 2.0), (-0.0, 2.0), (0.0, 0.0)]:
                self.assertEqual(plain(p, ring), oracle_ring(p[0], p[1], ring), p)
                self.assertEqual(indexed(p, ring), oracle_ring(p[0], p[1], ring), p)

    def test_finite_cancellation_matrix_uses_certified_sign_or_exact_fallback(self):
        rng = random.Random(511)
        checked = 0
        for scale in (1.0, 2.0 ** -20, 2.0 ** 30):
            for shift in (0.0, 1.0 * scale, 1e3 * scale):
                for _ in range(300):
                    def pt():
                        return [shift + rng.random() * scale, shift + rng.random() * scale]
                    p, q, r = pt(), pt(), pt()
                    ring = [p, q, r, list(p)]
                    for a, b in zip(ring, ring[1:]):
                        if a[1] == b[1]:
                            continue
                        qy = (a[1] + b[1]) / 2
                        at = Fraction(a[0]) + (Fraction(qy) - Fraction(a[1])) * (Fraction(b[0]) - Fraction(a[0])) / (
                            Fraction(b[1]) - Fraction(a[1]))
                        base = float(at)
                        for x in (base, math.nextafter(base, -math.inf), math.nextafter(base, math.inf)):
                            for rg in (ring, ring[::-1]):
                                self.assertEqual(plain((x, qy), rg), oracle_ring(x, qy, rg), (x, qy, rg))
                                self.assertEqual(indexed((x, qy), rg), oracle_ring(x, qy, rg), (x, qy, rg))
                                checked += 1
        self.assertGreater(checked, 10000)
        # integer and float mixtures
        mixed = [[0, 0], [7, 3.5], [1, 9], [0, 0]]
        for p in [(1, 2), (3.5, 1.75), (4, 4), (0.5, 8.5), (3.4999999999999996, 1.75)]:
            self.assertEqual(plain(p, mixed), oracle_ring(p[0], p[1], mixed), p)

    def test_half_open_edge_and_vertex_ownership_stays_single(self):
        # two shapes share the retained slanted edge; every query belongs to exactly the oracle's owners
        left = RING
        right = [[1.1087103325015797, 1.5594925198665022], [2.0, 1.5594925198665022], [2.0, 0.2466354431177197],
                 [0.648886272103919, 0.2466354431177197], [1.1087103325015797, 1.5594925198665022]]
        for xs in XS + [0.648886272103919, 1.1087103325015797]:
            for y in (Y, 0.2466354431177197, 1.5594925198665022):
                for a, b in ((left, right), (left[::-1], right[::-1])):
                    expect = [oracle_ring(xs, y, a), oracle_ring(xs, y, b)]
                    self.assertEqual([plain((xs, y), a), plain((xs, y), b)], expect, (xs, y))
                    self.assertEqual([indexed((xs, y), a), indexed((xs, y), b)], expect, (xs, y))
        # a representable vertex query is owned once by the half-open rule, not twice and not never
        counts, _ = count_by_name(cameras_at((1.0, 1.0)), collection([
            feature("A", "Polygon", [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]),
            feature("B", "Polygon", [[[1, 0], [3, 0], [3, 2], [1, 2], [1, 0]]])]))
        self.assertEqual(counts, {"A": 1, "B": 1})
        self.assertEqual(count_by_name(cameras_at((2.0, 1.0)), collection([
            feature("A", "Polygon", [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]),
            feature("C", "Polygon", [[[2, 0], [4, 0], [4, 2], [2, 2], [2, 0]]])]))[0], {"A": 0, "C": 1})

    def test_holes_components_and_same_name_overlap(self):
        outer = [[-1, -1], [3, -1], [3, 3], [-1, 3], [-1, -1]]
        points = [(x, Y) for x in XS]
        for hole in windings():
            doc = collection([feature("Synthetic", "Polygon", [outer, hole])])
            expected = [oracle_ring(x, y, outer) and not oracle_ring(x, y, hole) for x, y in points]
            self.assertEqual([point_in_polygon(p, [outer, hole]) for p in points], expected)
            self.assertEqual(count_by_name(cameras_at(*points), doc)[0], {"Synthetic": sum(expected)})
        for ring in windings():
            multi = feature("Synthetic", "MultiPolygon", [[ring], [[[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]]])
            overlap = feature("Synthetic", "Polygon", [ring])
            got, problems = count_by_name(cameras_at(*points, (5.5, 5.5)), collection([multi, overlap]))
            self.assertEqual((got, problems), ({"Synthetic": 4}, []))  # three retained interior points + the far square, each once

    def test_merged_underflow_overflow_integer_and_band_guards_remain(self):
        s = 1e-200
        tri = [[0, 0], [s, 0], [0, s], [0, 0]]
        with spy_exact() as exact:
            self.assertEqual([plain(p, tri) for p in [(s / 4, s / 4), (3 * s / 4, 3 * s / 4)]], [True, False])
        self.assertTrue(exact.called)
        opposite = [[-1e308, -1e308], [1e308, -1e308], [-1e308, 1e308], [-1e308, -1e308]]
        with spy_exact() as exact:
            self.assertTrue(plain((-1.0, -1.0), opposite))
            self.assertFalse(plain((1.0, 1.0), opposite))
        self.assertTrue(exact.called)
        huge = 10 ** 400  # representable integer, too large for a float: exact fallback, no crash
        self.assertFalse(camera_counts._number(huge))
        self.assertTrue(camera_counts._left_of_crossing(0, 5, 0, 0, huge, 10))  # crossing is huge / 2 > 0
        self.assertFalse(camera_counts._left_of_crossing(huge, 5, 0, 0, huge, 10))  # x == huge > huge / 2
        nan_ring = json.loads("[[0, 0], [1, NaN], [0, 1], [0, 0]]")
        self.assertFalse(camera_counts.valid_ring(nan_ring))
        thin = [[0, 0], [1, 0], [1, 1e-300], [0, 1e-300], [0, 0]]
        self.assertEqual(plain((0.5, 5e-301), thin), oracle_ring(0.5, 5e-301, thin))
        self.assertEqual(indexed((0.5, 5e-301), thin), oracle_ring(0.5, 5e-301, thin))

    def test_certified_ordinary_fast_path_and_existing_performance_budget(self):
        square = [[0.1, 0.1], [1.9, 0.1], [1.9, 1.9], [0.1, 1.9], [0.1, 0.1]]
        slanted = [[0.1, 0.1], [1.9, 0.3], [1.0, 1.9], [0.1, 0.1]]
        with mock.patch.object(camera_counts, "_exact_left_of_crossing", side_effect=AssertionError("fallback")):
            for ring in (square, slanted):
                for p in [(0.5, 0.5), (1.0, 1.0), (0.05, 0.5), (1.95, 1.0), (1.5, 0.2)]:
                    self.assertEqual(plain(p, ring), oracle_ring(p[0], p[1], ring), p)
                    self.assertEqual(indexed(p, ring), oracle_ring(p[0], p[1], ring), p)
        # an uncertain crossing must not be answered by the fast path
        with spy_exact() as exact:
            plain((XS[1], Y), RING)
        self.assertTrue(exact.called)


class RealBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-camera-roundoff-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))

    def snapshot(self):
        return {str(p.relative_to(self.root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode)
                for p in sorted(self.root.rglob("*")) if p.is_file() and "public" not in p.relative_to(self.root).parts}

    def test_real_count_csv_survey_and_build_correct_only_membership(self):
        for ring in windings():
            boundaries = collection([feature("Synthetic", "Polygon", [ring])])
            cameras = cameras_at(*[(x, Y) for x in XS])
            self.assertEqual(count_by_name(cameras, boundaries), ({"Synthetic": 3}, []))
            rows = camera_csv.camera_rows(cameras, boundaries, None)
            self.assertEqual(sorted(r["city"] for r in rows), ["", "", "Synthetic", "Synthetic", "Synthetic"])
            by_lon = {float(r["longitude"]): r["city"] for r in rows}
            self.assertEqual([by_lon[x] for x in XS], ["Synthetic" if e else "" for e in EXPECTED])
            self.assertEqual(map_survey._in_city(cameras, boundaries, "Synthetic"), [0, 1, 3])
        # real fictional-campaign build with a matching city label
        boundaries = collection([feature("Cedar", "Polygon", [RING])])
        cameras = cameras_at((XS[1], Y), url="https://www.openstreetmap.org/node/1")
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
        self.assertEqual(self.snapshot(), before)  # inputs untouched: bytes and modes
        self.assertEqual(boundaries["features"][0]["geometry"]["coordinates"][0], RING)


if __name__ == "__main__":
    unittest.main()
