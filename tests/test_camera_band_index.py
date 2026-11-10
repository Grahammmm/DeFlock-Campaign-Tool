"""Finite thin-hole band lookup: synthetic, offline geometry and real consumers."""
import copy
import csv
import io
import json
import math
import os
import re
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from unittest import mock

from campaign_tool import camera_counts as cc, camera_csv, cli, map_survey
from campaign_tool.site import build_site
from tests import test_camera_counts as fixtures

DAY = date(2026, 11, 10)


def thin_ring(low=0.0, high=1e-309):
    middle = low / 2 + high / 2
    return [[0.25, low], [0.75, low], [0.75, middle], [0.75, high],
            [0.25, high], [0.25, middle], [0.25, low]]


def boundaries(hole):
    return fixtures.collection([fixtures.feature(
        "Cedar", "Polygon", [fixtures.square(0, -2, 2, 2), hole])])


class BandTests(unittest.TestCase):
    def test_two_band_thin_hole_distant_latitudes_do_not_overflow(self):
        for hole in (thin_ring(), thin_ring(-1e-309, 0)):
            for y in (-1, 1):
                with self.subTest(hole=hole, y=y):
                    cameras = fixtures.points((1, y))
                    cities = boundaries(hole)
                    before = copy.deepcopy((cameras, cities))
                    index = cc._Ring(hole)
                    self.assertTrue(cc.valid_ring(hole))
                    self.assertEqual(len(cc._edges(hole)), 4)
                    self.assertEqual(index.count, 2)
                    self.assertFalse(index.contains(1, y))
                    self.assertTrue(cc.point_in_polygon((1, y), cities["features"][0]["geometry"]["coordinates"]))
                    self.assertEqual(cc.count_by_name(cameras, cities), ({"Cedar": 1}, []))
                    self.assertEqual(camera_csv.camera_rows(cameras, cities, {})[0]["city"], "Cedar")
                    self.assertEqual(map_survey._in_city(cameras, cities, "Cedar"), [0])
                    self.assertEqual((cameras, cities), before)

    def test_band_endpoint_selection_precedes_division(self):
        for low, high in ((0, 1e-309), (-1e-309, 0), (-2, 2)):
            index = cc._Ring(thin_ring(low, high))
            for y in (-1e308, low, math.nextafter(low, -math.inf)):
                self.assertEqual(index._band(y), 0)
            for y in (high, math.nextafter(high, math.inf), 1e308):
                self.assertEqual(index._band(y), index.count - 1)
            for y in (math.nextafter(low, high), (low + high) / 2, math.nextafter(high, low)):
                expected = min(index.count - 1, max(0, int((y - low) / index.step)))
                self.assertEqual(index._band(y), expected)

    def test_indexed_membership_matches_plain_ray_cast(self):
        for low, high in ((0, 1e-309), (-1e-309, 0), (-2, 2), (0, 4)):
            for reverse in (False, True):
                ring = thin_ring(low, high)
                if reverse:
                    ring.reverse()
                indexed = cc._Ring(ring)
                ys = [low - 1, low, math.nextafter(low, high), (low + high) / 2,
                      math.nextafter(high, low), high, high + 1]
                for x in (0, 0.25, 0.5, 0.75, 1):
                    for y in ys:
                        with self.subTest(low=low, high=high, reverse=reverse, x=x, y=y):
                            self.assertEqual(indexed.contains(x, y), cc.point_in_ring(x, y, ring))
                self.assertTrue(indexed.contains(0.5, (low + high) / 2))
                self.assertFalse(indexed.contains(1, (low + high) / 2))

    def test_endpoint_bands_do_not_clamp_membership_coordinates(self):
        ring = thin_ring()
        index = cc._Ring(ring)
        for y in (-1, 1):
            self.assertFalse(index.contains(0.5, y))
            self.assertEqual(cc.count_by_name(fixtures.points((0.5, y)), boundaries(ring)), ({"Cedar": 1}, []))
        self.assertTrue(index.contains(0.5, 0))
        self.assertFalse(index.contains(0.5, 1e-309))
        self.assertEqual(cc.count_by_name(fixtures.points((0.5, 5e-310)), boundaries(ring)), ({"Cedar": 0}, []))
        # An outer ring has the same half-open edge/corner contract.
        outer = cc._Ring(thin_ring(0, 2))
        east = cc._Ring([[x + 0.5, y] for x, y in thin_ring(0, 2)])
        north = cc._Ring([[x, y + 2] for x, y in thin_ring(0, 2)])
        corner = cc._Ring([[x + 0.5, y + 2] for x, y in thin_ring(0, 2)])
        for x, y in ((0.75, 1), (0.5, 2), (0.75, 2)):
            self.assertEqual(sum(r.contains(x, y) for r in (outer, east, north, corner)), 1)
        self.assertFalse(outer.contains(0.5, -1))
        self.assertFalse(outer.contains(0.5, 3))

    def test_degenerate_and_single_band_controls_preserve_behavior(self):
        for ring in (fixtures.square(0.25, 0, 0.75, 1e-309),
                     fixtures.square(0.25, 0, 0.75, 0), fixtures.square(0.25, -2, 0.75, 2),
                     thin_ring(-1e308, 1e308)):
            index = cc._Ring(ring)
            self.assertEqual(index.count, 1)
            for x in (0, 0.5, 1):
                for y in (-1, 0, 5e-310, 1):
                    self.assertEqual(index.contains(x, y), cc.point_in_ring(x, y, ring))

    def test_multipolygon_and_same_name_overlap_keep_counts(self):
        holed = fixtures.feature("Cedar", "MultiPolygon", [
            [fixtures.square(3, 0, 4, 2)], [fixtures.square(0, -2, 2, 2), thin_ring()]])
        overlap = fixtures.feature("Cedar", "Polygon", [fixtures.square(0.8, -2, 2, 2)])
        sibling = fixtures.feature("Pine", "Polygon", [fixtures.square(5, 0, 6, 2)])
        cameras = fixtures.points((1, 1), (1, -1), (3.5, 1), (5.5, 1), (0.5, 5e-310), (7, 1))
        for features in ([holed, overlap, sibling], [sibling, overlap, holed]):
            cities = fixtures.collection(features)
            self.assertEqual(cc.count_by_name(cameras, cities), ({"Cedar": 3, "Pine": 1}, []))
            self.assertEqual(map_survey._in_city(cameras, cities, "Cedar"), [0, 1, 2])


class ConsumerTests(unittest.TestCase):
    setUp = fixtures.BuildTests.setUp
    write_map = fixtures.BuildTests.write_map
    add_pine = fixtures.BuildTests.add_pine

    def prepare(self):
        cameras = fixtures.points((1, 1))
        cameras["features"][0]["properties"] = {"url": "https://www.openstreetmap.org/node/10"}
        self.write_map(boundaries(thin_ring())["features"], cameras)

    def snapshot_inputs(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in self.root.rglob("*") if p.is_file() and "public" not in p.relative_to(self.root).parts
                and "kit" not in p.relative_to(self.root).parts}

    def test_real_build_and_csv_handle_two_band_thin_hole(self):
        self.prepare()
        self.add_pine({"cameraCount": "About 12 per records"})
        before = self.snapshot_inputs()
        outputs = []
        for _ in range(2):
            notes = []
            build_site(self.root, DAY, notes=notes)
            self.assertEqual(notes, [])
            public = self.root / "public"
            data = (public / "site-data.js").read_text()
            cities = json.loads(re.search(r"var CITIES=(\{.*\});", data).group(1))
            self.assertEqual(cities["cedar"]["cameraCount"], "1 community-mapped point" + fixtures.TAIL)
            self.assertEqual(cities["pine"]["cameraCount"], "About 12 per records")
            with (public / "data" / "cameras.csv").open(newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle))[0]["city"], "Cedar")
            outputs.append({str(p.relative_to(public)): p.read_bytes() for p in public.rglob("*") if p.is_file()})
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(self.snapshot_inputs(), before)

    def test_survey_selection_and_sheet_handle_two_band_thin_hole(self):
        self.prepare()
        before = self.snapshot_inputs()
        rows, _, day = map_survey.survey_rows(self.root, "Cedar", today=DAY)
        self.assertEqual(day, DAY)
        self.assertEqual([r["node_url"] for r in rows], ["https://www.openstreetmap.org/node/10"])
        fixed_date = mock.Mock(wraps=date)
        fixed_date.today.return_value = DAY
        out, err = io.StringIO(), io.StringIO()
        old_umask = os.umask(0o022)
        try:
            with mock.patch.object(map_survey, "date", fixed_date), mock.patch("sys.argv", [
                    "campaign_tool", "map", "survey-sheet", "--city", "Cedar", "--directory", str(self.root)]), \
                    redirect_stdout(out), redirect_stderr(err):
                result = cli.main()
        finally:
            os.umask(old_umask)
        self.assertIn(result, (0, None), err.getvalue())
        folder = self.root / "kit" / "survey"
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["cedar-2026-11-10.csv", "cedar-2026-11-10.html"])
        with (folder / "cedar-2026-11-10.csv").open(newline="") as handle:
            table = list(csv.reader(handle))
        self.assertEqual(tuple(table[0]), map_survey.HEADER)
        self.assertEqual(len(table), 2)
        self.assertEqual(table[1][0], rows[0]["node_url"])
        self.assertEqual(table[1][10:], ["", "", "", ""])
        for p in (self.root / "kit", folder):
            self.assertEqual(p.stat().st_mode & 0o777, 0o700)
        for p in folder.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "public").exists())
        self.assertEqual(self.snapshot_inputs(), before)


if __name__ == "__main__":
    unittest.main()
