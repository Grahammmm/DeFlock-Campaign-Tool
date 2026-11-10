"""RF2135-4: oversized decoded JSON integers in a camera ordinate refuse the CSV export with the indexed ContentError.

Synthetic content only (a temporary copy of the fictional example); no network.
"""
import copy
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool import camera_csv
from campaign_tool.content import ContentError
from campaign_tool.site import build_site
from tests.test_site_cameras_page import NODE, SIX, point, square

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
HUGE = json.loads("1" + "0" * 400)
TODAY = datetime.date(2026, 11, 1)
SENTINEL = "SENTINEL-PROPERTY-VALUE"


def message(index):
    return f"map/cameras.geojson.features[{index}].geometry.coordinates: finite coordinates required for CSV"


def camera(x, y, extra=(), url=NODE + "7"):
    return {"type": "Feature", "properties": {"url": url, "operator": SENTINEL},
            "geometry": {"type": "Point", "coordinates": [x, y, *extra]}}


def snapshot(folder):
    """Every entry under folder with its mode and bytes (symlinks by target, never followed)."""
    result = {}
    for base, dirs, names in os.walk(folder):
        for name in dirs + names:
            path = Path(base) / name
            data = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
            result[path.relative_to(folder).as_posix()] = (path.lstat().st_mode, data)
    return result


class DirectTests(unittest.TestCase):
    def test_oversized_json_integer_ordinates_refuse_with_index(self):
        good = camera(0.6, 0.6)
        for sign in (1, -1):
            for axis in (0, 1):
                for index in (0, 2):
                    bad = [0.7, 0.7]
                    bad[axis] = sign * HUGE
                    features = [copy.deepcopy(good) for _ in range(index)] + [camera(*bad)]
                    cameras = {"features": features}
                    before = copy.deepcopy(cameras)
                    with self.subTest(sign=sign, axis=axis, index=index):
                        with self.assertRaises(ContentError) as caught:
                            camera_csv.camera_rows(cameras, {}, {})
                        self.assertEqual(str(caught.exception), message(index))
                        self.assertEqual(cameras, before)

    def test_numeric_admission_matches_existing_safe_predicate(self):
        for value in (0, -0.0, -3, 2.5, 1e300, -1e300):
            with self.subTest(admitted=value):
                rows = camera_csv.camera_rows({"features": [camera(value, 1)]}, {}, {})
                self.assertEqual(len(rows), 1)
        for value in (True, False, None, "1", [], {}, float("nan"), float("inf"), float("-inf"), HUGE, -HUGE):
            for axis in (0, 1):
                bad = [1, 1]
                bad[axis] = value
                with self.subTest(rejected=type(value).__name__, axis=axis):
                    with self.assertRaises(ContentError) as caught:
                        camera_csv.camera_rows({"features": [camera(*bad)]}, {}, {})
                    self.assertEqual(str(caught.exception), message(0))

    def test_first_two_ordinates_remain_verbatim(self):
        rows = camera_csv.camera_rows({"features": [camera(3, 4.5, extra=("unused-third",))]}, {}, {})
        self.assertEqual((rows[0]["longitude"], rows[0]["latitude"]), ("3", "4.5"))
        # only the first two ordinates are consumed; an oversized third is not examined
        rows = camera_csv.camera_rows({"features": [camera(3, 4.5, extra=(HUGE,))]}, {}, {})
        self.assertEqual((rows[0]["longitude"], rows[0]["latitude"]), ("3", "4.5"))


class CampaignCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-camera-csv-admission-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public, self.map = self.root / "content", self.root / "public", self.root / "content" / "map"
        agencies = json.loads((self.content / "agencies.json").read_text(encoding="utf-8"))
        agencies.append({"agency_id": "ca-pine-police", "slug": "pine", "name": "Pine PD (fictional)", "kind": "police",
                         "jurisdiction_name": "Pine"})
        self.write(self.content / "agencies.json", agencies)
        self.write(self.map / "cities.json", {"cedar": {"name": "Cedar", "bounds": [[0.5, 0.5], [1.0, 1.5]]},
                                              "pine": {"name": "Pine", "bounds": [[1.0, 0.5], [1.5, 1.5]]}})
        self.write(self.map / "city-boundaries.geojson",
                   {"type": "FeatureCollection", "features": [square("Pine", 1.0, 1.5), square("Cedar", 0.5, 1.0)]})
        self.cameras(SIX)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def cameras(self, features):
        self.write(self.map / "cameras.geojson", {"type": "FeatureCollection", "features": features})
        config = json.loads((self.map / "map-config.json").read_text(encoding="utf-8"))
        self.write(self.map / "map-config.json", dict(config, initial_point_count=len(features)))

    def malformed(self, sign, axis):
        bad = [0.7, 0.7]
        bad[axis] = sign * HUGE
        # the loader's type-only admission accepts the decoded integer; the bad camera follows two good ones
        self.cameras([SIX[0], SIX[1], camera(*bad)])
        return 2


class BuildRefusalTests(CampaignCase):
    def test_build_refusal_preserves_absent_and_existing_public_tree(self):
        for existing in (False, True):
            for sign in (1, -1):
                for axis in (0, 1):
                    with self.subTest(existing=existing, sign=sign, axis=axis):
                        shutil.rmtree(self.public, ignore_errors=True)
                        self.cameras(SIX)
                        if existing:
                            build_site(self.root, today=TODAY)
                        index = self.malformed(sign, axis)
                        before_public = snapshot(self.public) if existing else None
                        before_inputs = snapshot(self.content)
                        before_root = sorted(p.name for p in self.root.iterdir())
                        with self.assertRaises(ContentError) as caught:
                            build_site(self.root, today=TODAY)
                        self.assertEqual(str(caught.exception), message(index))
                        self.assertEqual(snapshot(self.content), before_inputs)
                        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before_root)  # no staging residue
                        if existing:
                            self.assertEqual(snapshot(self.public), before_public)
                        else:
                            self.assertFalse(self.public.exists())

    def test_valid_build_csv_is_repeatable(self):
        build_site(self.root, today=TODAY)
        first = (self.public / "data" / "cameras.csv").read_bytes()
        shutil.rmtree(self.public)
        build_site(self.root, today=TODAY)
        self.assertEqual((self.public / "data" / "cameras.csv").read_bytes(), first)
        self.assertTrue(first.startswith(b"osm_url,latitude,longitude,"))
        self.assertIn(b"\r\n", first)


class CliRefusalTests(CampaignCase):
    def run_cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), *extra],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def test_cli_build_preview_and_check_have_named_nontraceback_refusal(self):
        for existing in (False, True):
            for mode in ((), ("--preview",), ("--check",)):
                for sign, axis in ((1, 0), (-1, 1)):
                    with self.subTest(existing=existing, mode=mode, sign=sign, axis=axis):
                        shutil.rmtree(self.public, ignore_errors=True)
                        self.cameras(SIX)
                        if existing:
                            build_site(self.root, today=TODAY)
                        index = self.malformed(sign, axis)
                        before_public = snapshot(self.public) if existing else None
                        before_inputs = snapshot(self.content)
                        before_root = sorted(p.name for p in self.root.iterdir())
                        result = self.run_cli(*mode)
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn("Stopped: " + message(index), result.stderr)
                        for text in ("Traceback", "OverflowError", str(HUGE)[:40], SENTINEL):
                            self.assertNotIn(text, result.stderr)
                            self.assertNotIn(text, result.stdout)
                        self.assertEqual(result.stdout, "")
                        self.assertEqual(snapshot(self.content), before_inputs)
                        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before_root)
                        if existing:
                            self.assertEqual(snapshot(self.public), before_public)
                        else:
                            self.assertFalse(self.public.exists())


if __name__ == "__main__":
    unittest.main()
