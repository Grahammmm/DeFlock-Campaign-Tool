"""RF1155-2: camera CSV rows skip the node-suffix integer conversion when no point checks exist (synthetic content only)."""
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool import camera_csv
from campaign_tool.site import build_site
from campaign_tool.site_data_catalogue import verify_data_package
from tests import test_site_cameras_csv as base
from tests.test_site_cameras_page import NODE

REPO_ROOT = Path(__file__).resolve().parents[1]
LONG = NODE + "0" * 4500 + "1"  # 4501 digits: over the default 4300-digit integer-conversion limit
CHILD = '''
import json, sys
sys.path.insert(0, sys.argv[1])
sys.set_int_max_str_digits(640)  # test-only, child process only
from campaign_tool import camera_csv
node = "https://www.openstreetmap.org/node/"
urls = [node + "0" * 700 + "5", node + "0" * 700, node + "10", node + "2", node + "10", None, "https://www.openstreetmap.org/way/7"]
cameras = {"features": [{"type": "Feature", "properties": {"url": u, "manufacturer": "m%d" % n},
                         "geometry": {"type": "Point", "coordinates": [0.5, 0.5]}} for n, u in enumerate(urls)]}


class Raising(dict):
    def get(self, *args):
        raise AssertionError("lookup on empty checks")


print(json.dumps({"empty": camera_csv.camera_rows(cameras, {"features": []}, {}),
                  "raising": camera_csv.camera_rows(cameras, {"features": []}, Raising())}))
'''


def snapshot(folder):
    result = {}
    for walked, dirs, names in os.walk(folder):
        for name in dirs + names:
            path = Path(walked) / name
            result[path.relative_to(folder).as_posix()] = (path.lstat().st_mode, path.read_bytes() if path.is_file() else None)
    return result


class OptionalChecksTests(unittest.TestCase):
    setUp, cameras, checks = base.CameraCsvTests.setUp, base.CameraCsvTests.cameras, base.CameraCsvTests.checks
    write = staticmethod(base.CameraCsvTests.write)

    def table(self):
        raw = (self.public / "data" / "cameras.csv").read_bytes()
        return list(csv.reader(io.StringIO(raw.decode("utf-8"), newline="")))

    def run_cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), *extra],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=180)

    def long_camera(self, **props):
        return base.at(0.6, 0.6, LONG, manufacturer="Flock Safety", **props)

    def test_empty_checks_skip_integer_conversion_for_long_node_suffix(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-child-") as folder:
            script = Path(folder) / "child.py"
            script.write_text(CHILD, encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", str(script), str(REPO_ROOT)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["empty"], out["raising"])
        rows = out["empty"]
        node = "https://www.openstreetmap.org/node/"
        self.assertEqual([r["osm_url"] for r in rows],
                         [node + "0" * 700, node + "0" * 700 + "5", node + "10", node + "10", node + "2",
                          "https://www.openstreetmap.org/way/7", ""])  # raw spelling, lexicographic, stable, blank last
        self.assertEqual([r["manufacturer"] for r in rows], ["m1", "m0", "m2", "m4", "m3", "m6", "m5"])
        self.assertTrue(all(list(r) == list(camera_csv.COLUMNS) and r["check_status"] == r["checked_on"] == "" for r in rows))

    def test_nonempty_check_mapping_keeps_latest_and_missing_rules(self):
        newest = {"status": "seen_in_person", "checked_on": "2026-10-30"}
        older = {"status": "duplicate", "checked_on": "2026-10-01"}
        cameras = {"features": [base.at(0.5, 0.5, NODE + "10"), base.at(0.5, 0.5, NODE + "0010"), base.at(0.5, 0.5, NODE + "11"),
                                base.at(0.5, 0.5, NODE + "12"), base.at(0.5, 0.5, "https://www.openstreetmap.org/way/10"), base.at(0.5, 0.5)]}
        rows = camera_csv.camera_rows(cameras, {"features": []}, {10: [newest, older], 12: []})
        cells = {r["osm_url"]: (r["check_status"], r["checked_on"]) for r in rows}
        self.assertEqual(cells, {NODE + "10": ("seen_in_person", "2026-10-30"), NODE + "0010": ("seen_in_person", "2026-10-30"),
                                 NODE + "11": ("", ""), NODE + "12": ("", ""),  # unmatched key; matched key with an empty list
                                 "https://www.openstreetmap.org/way/10": ("", ""), "": ("", "")})
        self.assertTrue(all(list(r) == list(camera_csv.COLUMNS) for r in rows))

    def test_real_build_without_point_check_file_keeps_long_url(self):
        self.cameras([self.long_camera(), base.at(0.7, 1.0, NODE + "10"), base.at(0.9, 1.4)])
        before = snapshot(self.content)
        for existing in (False, True):
            if existing:  # a second build over an established public root
                self.assertTrue(self.public.is_dir())
            build_site(self.root)
            table = self.table()
            self.assertEqual(table[0], list(camera_csv.COLUMNS))
            self.assertEqual(len(table), 4)
            long_row = next(r for r in table[1:] if r[0] == LONG)
            self.assertEqual((long_row[3], long_row[6:]), ("Flock Safety", ["Cedar", "", ""]))
            self.assertEqual(verify_data_package(self.public), [])
            self.assertFalse((self.map / "point-checks.json").exists())
            self.assertTrue(before == snapshot(self.content), "content/ changed")

    def test_cli_normal_and_checked_no_check_build_complete(self):
        self.cameras([self.long_camera(), base.at(0.7, 1.0, NODE + "10")])
        before = snapshot(self.content)
        for extra in ((), ("--check",)):  # empty public root first, then an established one
            result = self.run_cli(*extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Exceeds the limit", result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual([r[0] for r in self.table()[1:]], [LONG, NODE + "10"])  # lexicographic on the raw spelling
            self.assertEqual(verify_data_package(self.public), [])
            self.assertTrue(before == snapshot(self.content), "content/ changed")

    def test_preview_no_check_long_url_is_write_free(self):
        self.cameras([self.long_camera()])
        for build_first in (False, True):
            if build_first:
                build_site(self.root)
            before = snapshot(self.root)
            result = self.run_cli("--preview")
            self.assertEqual(result.returncode, 0 if build_first else 3, result.stderr)  # 3 = a rebuild would add files
            self.assertNotIn("Exceeds the limit", result.stdout + result.stderr)
            self.assertTrue(result.stdout.rstrip("\n").endswith("Preview only; public/ was not changed."))
            self.assertTrue(before == snapshot(self.root), "preview wrote something")
            self.assertEqual(self.public.exists(), build_first)

    def test_ordinary_export_bytes_unchanged_with_and_without_checks(self):
        features = [base.at(0.6, 0.6, NODE + "30", manufacturer="=cmd"), base.at(0.7, 1.0, NODE + "10", operator="Cedar PD"), base.at(0.9, 1.4)]
        self.cameras(features)
        build_site(self.root)
        plain = (self.public / "data" / "cameras.csv").read_bytes()
        build_site(self.root)
        self.assertEqual((self.public / "data" / "cameras.csv").read_bytes(), plain)
        self.checks([{"node": 10, "status": "seen_in_person", "checked_on": "2026-10-30", "source": "https://records.example.invalid/r",
                      "note": "SENTINEL-NOTE"}])
        build_site(self.root)
        checked = (self.public / "data" / "cameras.csv").read_bytes()
        self.assertNotIn(b"SENTINEL-NOTE", checked)
        self.assertNotIn(b"records.example.invalid", checked)
        self.assertEqual(plain.replace(b"Cedar PD,,Cedar,,", b"Cedar PD,,Cedar,seen_in_person,2026-10-30", 1), checked)
        self.assertEqual(verify_data_package(self.public), [])


if __name__ == "__main__":
    unittest.main()
