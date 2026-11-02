"""Camera map snapshot date (DF1507-09): fetch_osm_alprs records Overpass osm_base; the site shows it, never the build date.

Synthetic points and saved answers written here; no network.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.content import load_content
from campaign_tool.content_check import content_problems
from campaign_tool.site import build_site
from tools import fetch_osm_alprs as tool

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
POINTS = [{"type": "node", "id": 9001, "lat": 37.5, "lon": -122.1, "tags": {"manufacturer": "Synthetic"}},
          {"type": "node", "id": 9002, "lat": 37.6, "lon": -122.2, "tags": {}}]
BASE = "2026-10-30T07:15:02Z"
KNOWN = "Map data as of 2026-10-30 (OpenStreetMap)"
UNKNOWN = "Map data date unknown (OpenStreetMap)"
SUFFIX = " (the new cameras.geojson was written)"


def answer(osm3s=None, elements=POINTS):
    document = {"version": 0.6, "elements": elements}
    if osm3s is not None:
        document["osm3s"] = osm3s
    return document


class MapSnapshotDateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-map-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.map_dir = self.root / "content" / "map"
        self.snapshot = self.map_dir / "snapshot.json"
        patcher = mock.patch.object(tool.urllib.request, "urlopen", side_effect=AssertionError("network used"))
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)

    def run_tool(self, document, *extra):
        saved = self.tmp / "saved.json"
        saved.write_text(json.dumps(document), encoding="utf-8")
        os.utime(saved, (1700000000, 1700000000))  # 2023-11-14: the file time must never become the map date
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["--directory", str(self.root), "--from-file", str(saved), *extra])
        return code, out.getvalue(), err.getvalue()

    def read_snapshot(self):
        return json.loads(self.snapshot.read_text(encoding="utf-8"))

    def date_lines(self, today=date(2031, 1, 2)):
        build_site(self.root, today=today)
        public = self.root / "public"
        return {page: re.findall(r'<p class="map-date">(.*?)</p>', (public / page).read_text(encoding="utf-8"))
                for page in ("index.html", "about.html")}

    def tree(self):
        return sorted((str(p.relative_to(self.root)), p.read_bytes() if p.is_file() else b"")
                      for p in self.root.rglob("*"))

    # 1. saved answer with osm3s.timestamp_osm_base
    def test_saved_answer_timestamp_written_and_shown_on_index_and_about(self):
        code, out, err = self.run_tool(answer({"timestamp_osm_base": BASE, "copyright": "synthetic"}))
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"snapshot osm_base={BASE}\n", out)
        self.assertEqual(self.read_snapshot(), {"schema_version": 1, "osm_base": BASE, "source": "saved_file"})
        self.assertEqual(oct(self.snapshot.stat().st_mode & 0o777), "0o644")
        self.assertEqual(load_content(self.root)["map"]["snapshot_date"], "2026-10-30")
        self.assertEqual(self.date_lines(), {"index.html": [KNOWN], "about.html": [KNOWN]})
        index = (self.root / "public" / "index.html").read_text(encoding="utf-8")
        self.assertLess(index.index('class="map-credit"'), index.index('class="map-date"'))

    # 2. no osm3s, or a malformed timestamp -> null, "date unknown"
    def test_missing_or_malformed_timestamp_is_null_and_date_unknown(self):
        cases = {"no osm3s": None, "yesterday": {"timestamp_osm_base": "yesterday"},
                 "date only": {"timestamp_osm_base": "2026-10-30"}, "offset": {"timestamp_osm_base": BASE[:-1] + "+00:00"},
                 "trailing": {"timestamp_osm_base": BASE + "x"}, "number": {"timestamp_osm_base": 1793000000},
                 "osm3s list": [BASE], "osm3s text": BASE}
        for label, osm3s in cases.items():
            with self.subTest(label):
                code, out, _ = self.run_tool(answer(osm3s))
                self.assertEqual(code, 0)
                self.assertIn("snapshot osm_base=unknown\n", out)
                self.assertEqual(self.read_snapshot(), {"schema_version": 1, "osm_base": None, "source": "saved_file"})
        lines = self.date_lines()
        self.assertEqual(lines, {"index.html": [UNKNOWN], "about.html": [UNKNOWN]})
        for page in ("index.html", "about.html"):
            self.assertNotIn("2023-11-14", (self.root / "public" / page).read_text(encoding="utf-8"))

    # 3. online path -> source overpass
    def test_online_answer_records_overpass_source(self):
        fetched = answer({"timestamp_osm_base": BASE})
        with mock.patch.object(tool, "fetch", return_value=fetched) as fetch, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = tool.main(["--directory", str(self.root), "--bbox", "37", "-123", "38", "-122"])
        self.assertEqual(code, 0)
        fetch.assert_called_once()
        self.assertIn(f"snapshot osm_base={BASE}\n", out.getvalue())
        self.assertEqual(self.read_snapshot(), {"schema_version": 1, "osm_base": BASE, "source": "overpass"})
        with mock.patch.object(tool, "fetch", return_value={"elements": POINTS}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(["--directory", str(self.root), "--relation", "1"]), 0)
        self.assertEqual(self.read_snapshot(), {"schema_version": 1, "osm_base": None, "source": "overpass"})

    # 4. empty answer refused by the empty guard -> snapshot not written, not changed
    def test_empty_guard_refusal_writes_no_snapshot(self):
        empty = answer({"timestamp_osm_base": "2026-11-01T00:00:00Z"}, elements=[])
        cameras = (self.map_dir / "cameras.geojson").read_bytes()
        code, _, err = self.run_tool(empty)
        self.assertEqual((code, err.strip()), (3, tool.EMPTY_KEPT))
        self.assertFalse(self.snapshot.exists())
        self.assertEqual(self.run_tool(answer({"timestamp_osm_base": BASE}))[0], 0)
        before = (self.snapshot.read_bytes(), (self.map_dir / "cameras.geojson").read_bytes())
        self.assertNotEqual(before[1], cameras)
        self.assertEqual(self.run_tool(empty)[0], 3)
        self.assertEqual((self.snapshot.read_bytes(), (self.map_dir / "cameras.geojson").read_bytes()), before)
        self.assertFalse([p.name for p in self.map_dir.iterdir() if p.name.startswith(".snapshot.json.")])

    # 5. symlinked snapshot.json -> refused, cameras written, exit 2
    def test_symlinked_snapshot_refused_after_cameras_written(self):
        target = self.tmp / "elsewhere.json"
        target.write_text("untouched\n", encoding="utf-8")
        self.snapshot.symlink_to(target)
        code, out, err = self.run_tool(answer({"timestamp_osm_base": BASE}))
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "fetch_osm_alprs: snapshot.json is a symlink; not written" + SUFFIX)
        self.assertEqual(out, "")
        self.assertTrue(self.snapshot.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "untouched\n")
        written = json.loads((self.map_dir / "cameras.geojson").read_text(encoding="utf-8"))
        self.assertEqual([f["properties"]["url"][-4:] for f in written["features"]], ["9001", "9002"])
        self.assertEqual((self.map_dir / "attribution.txt").read_text(encoding="utf-8"), tool.ATTRIBUTION + "\n")

    def test_write_map_without_snapshot_keeps_old_behaviour(self):
        collection = tool.to_geojson(POINTS)[0]
        tool.write_map(self.root, collection, False)
        self.assertFalse(self.snapshot.exists())

    # 6. no snapshot.json passes; an invalid one stops the build and content-check names it (parity)
    def test_map_without_snapshot_builds_with_date_unknown(self):
        self.assertFalse(self.snapshot.exists())
        self.assertEqual(content_problems(self.root), [])
        self.assertIsNone(load_content(self.root)["map"]["snapshot_date"])
        self.assertEqual(self.date_lines(), {"index.html": [UNKNOWN], "about.html": [UNKNOWN]})

    def test_invalid_snapshot_stops_build_and_content_check_names_it(self):
        good = {"schema_version": 1, "osm_base": BASE, "source": "saved_file"}
        cases = {"extra key": ({**good, "fetched_at": BASE}, "map/snapshot.json: unknown field(s) ['fetched_at']"),
                 "missing key": ({"schema_version": 1, "osm_base": BASE},
                                 "map/snapshot.json: missing field(s) ['source']"),
                 "version": ({**good, "schema_version": 2}, "map/snapshot.json.schema_version: must be 1"),
                 "version bool": ({**good, "schema_version": True}, "map/snapshot.json.schema_version: must be 1"),
                 "osm_base": ({**good, "osm_base": "yesterday"}, "map/snapshot.json.osm_base: must be a UTC "
                              "timestamp YYYY-MM-DDTHH:MM:SSZ or null"),
                 "source": ({**good, "source": "build"}, "map/snapshot.json.source: must be 'overpass' or 'saved_file'"),
                 "not object": ([good], "map/snapshot.json: expected an object"),
                 "too large": ({**good, "pad": "x" * 5000}, "map/snapshot.json: file too large")}
        for label, (document, expected) in cases.items():
            with self.subTest(label):
                self.snapshot.write_text(json.dumps(document), encoding="utf-8")
                self.assertEqual(content_problems(self.root), [expected])
                with self.assertRaises(ValueError) as caught:
                    load_content(self.root)
                self.assertTrue(expected.endswith(str(caught.exception).removeprefix("map/")), caught.exception)
        self.snapshot.write_text(json.dumps({**good, "fetched_at": BASE}), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Stopped: map/snapshot.json: unknown field(s) ['fetched_at']", result.stderr)
        self.assertFalse((self.root / "public").exists())

    def test_snapshot_named_beside_another_broken_map_file(self):
        self.snapshot.write_text(json.dumps({"schema_version": 1, "osm_base": None, "source": "x"}), encoding="utf-8")
        (self.map_dir / "cameras.geojson").write_text("{", encoding="utf-8")
        problems = content_problems(self.root)
        self.assertIn("map/snapshot.json.source: must be 'overpass' or 'saved_file'", problems)
        self.assertTrue(any(p.startswith("map/cameras.geojson: not valid UTF-8 JSON") for p in problems), problems)
        self.snapshot.unlink()
        self.snapshot.symlink_to(self.tmp / "absent.json")  # dangling link: absent, as for cities.json
        self.assertTrue(all("snapshot" not in p for p in content_problems(self.root)))

    def test_symlinked_snapshot_is_not_read_by_build(self):
        target = self.tmp / "real.json"
        target.write_text(json.dumps({"schema_version": 1, "osm_base": BASE, "source": "overpass"}), encoding="utf-8")
        self.snapshot.symlink_to(target)
        self.assertEqual(content_problems(self.root), ["map/snapshot.json: symlinks are not read"])

    # 7. --check writes nothing
    def test_check_writes_nothing(self):
        self.assertEqual(self.run_tool(answer({"timestamp_osm_base": BASE}))[0], 0)
        before = self.tree()
        code, out, _ = self.run_tool(answer({"timestamp_osm_base": "2026-11-01T00:00:00Z"}), "--check")
        self.assertEqual(code, 0)
        self.assertIn("Check only: nothing was written.", out)
        self.assertNotIn("snapshot osm_base", out)
        self.assertEqual(self.tree(), before)

    # 8. the build date is never the map date
    def test_build_date_never_used_as_map_date(self):
        self.assertEqual(self.run_tool(answer({"timestamp_osm_base": BASE}))[0], 0)
        for today in (date(2031, 1, 2), date(2026, 10, 31)):
            with self.subTest(today=today):
                self.assertEqual(self.date_lines(today), {"index.html": [KNOWN], "about.html": [KNOWN]})
        self.snapshot.unlink()
        lines = self.date_lines(date(2031, 1, 2))
        self.assertEqual(lines, {"index.html": [UNKNOWN], "about.html": [UNKNOWN]})
        self.assertNotIn("2031-01-02", json.dumps(lines))

    def test_no_map_shows_no_date_anywhere(self):
        shutil.rmtree(self.map_dir)
        build_site(self.root, today=date(2031, 1, 2))
        for page in ("index.html", "about.html"):
            self.assertNotIn("Map data", (self.root / "public" / page).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
