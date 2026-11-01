"""fetch_osm_alprs --from-file: offline camera map build; bad and duplicate points skipped and counted (RF1730-9)."""
import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.content import load_map
from tools import fetch_osm_alprs as tool

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "osm" / "overpass-small.json"
EXAMPLE = ROOT / "examples" / "fictional-campaign"
ELEMENTS = json.loads(FIXTURE.read_text(encoding="utf-8"))["elements"]
MESSAGE = "fetch_osm_alprs: saved answer is not valid Overpass JSON"


def golden(elements):
    """The pre-RF1730-9 to_geojson, frozen here as the byte-identity reference for valid points."""
    features = []
    for element in elements:
        if element.get("type") != "node" or "lat" not in element or "lon" not in element:
            continue
        tags = element.get("tags") or {}
        features.append({
            "type": "Feature",
            "properties": {
                "manufacturer": tool.clean(tags.get("manufacturer")),
                "operator": tool.clean(tags.get("operator")),
                "url": f"https://www.openstreetmap.org/node/{int(element['id'])}",
            },
            "geometry": {"type": "Point", "coordinates": [float(element["lon"]), float(element["lat"])]},
        })
    features.sort(key=lambda f: f["properties"]["url"])
    return json.dumps({"type": "FeatureCollection", "features": features}, indent=1) + "\n"


def node(node_id, lat=1.0, lon=1.0, **extra):
    return {"type": "node", "id": node_id, "lat": lat, "lon": lon, **extra}


class OfflineBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-osm-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.campaign = self.tmp / "campaign"
        self.map_dir = self.campaign / "content" / "map"
        patcher = mock.patch.object(tool.urllib.request, "urlopen", side_effect=AssertionError("network used"))
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)

    def saved(self, document=None, raw=None):
        path = self.tmp / "saved.json"
        if raw is None:
            raw = json.dumps(document if document is not None else {"elements": ELEMENTS}).encode()
        path.write_bytes(raw)
        return path

    def run_tool(self, *extra, path=None):
        argv = ["--directory", str(self.campaign), "--from-file", str(path or self.saved()), *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(argv)
        return code, out.getvalue(), err.getvalue()

    def assert_refused(self, path):
        code, out, err = self.run_tool(path=path)
        self.assertEqual((code, out, err.strip()), (2, "", MESSAGE))
        self.assertFalse((self.campaign / "content").exists(), "map dir must not be created")
        self.urlopen.assert_not_called()

    def skipped_for(self, elements):
        collection, skipped = tool.to_geojson(elements)
        return [f["properties"]["url"] for f in collection["features"]], skipped

    def test_fixture_matches_golden_bytes_offline(self):
        code, out, err = self.run_tool()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((self.map_dir / "cameras.geojson").read_text(encoding="utf-8"), golden(ELEMENTS))
        self.assertEqual((self.map_dir / "attribution.txt").read_text(encoding="utf-8"), tool.ATTRIBUTION + "\n")
        self.assertIn("points=3 skipped=0\n", out)
        self.assertNotIn("skipped not-node", out)
        self.urlopen.assert_not_called()

    def test_non_finite_literals_refuse_whole_file(self):
        good = json.dumps({"elements": ELEMENTS})
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                raw = good.replace('"lat": 0.5', f'"lat": {literal}')
                self.assertIn(literal, raw)
                self.assert_refused(self.saved(raw=raw.encode()))

    def test_bad_top_level_and_shape_refused(self):
        cases = {"top-level list": json.dumps(ELEMENTS).encode(),
                 "elements missing": b'{"version": 0.6}',
                 "elements not a list": b'{"elements": {"0": 1}}',
                 "invalid utf-8": b'{"elements": [], "x": "\xff\xfe"}',
                 "not json": b'{"elements": [',
                 "empty": b""}
        for label, raw in cases.items():
            with self.subTest(label):
                self.assert_refused(self.saved(raw=raw))
        self.assert_refused(self.tmp / "missing.json")

    def test_file_over_cap_refused(self):
        path = self.saved()
        with mock.patch.object(tool, "MAX_ANSWER_BYTES", path.stat().st_size - 1):
            self.assert_refused(path)
        with mock.patch.object(tool, "MAX_ANSWER_BYTES", path.stat().st_size):
            self.assertEqual(self.run_tool(path=path)[0], 0)

    def test_bad_coordinates_skipped_and_counted(self):
        bad = [node(1, lat=200), node(2, lon=-181), node(3, lat=True), node(4, lat="35.3"),
               node(5, lon=None), node(6, lat=-90.0001), node(7, lat=float("inf"))]
        urls, skipped = self.skipped_for(bad + [node(8, lat=-90, lon=180)])
        self.assertEqual(urls, ["https://www.openstreetmap.org/node/8"])
        self.assertEqual(skipped, {"bad-coordinates": 7})

    def test_bad_ids_and_duplicates(self):
        missing = node(1)
        del missing["id"]
        elements = [node("12"), node(-1), node(0), node(True), node(1.5), missing,
                    node(20, tags={"operator": "First"}), node(20, tags={"operator": "Second"}),
                    node(21, lat=500), node(21)]
        collection, skipped = tool.to_geojson(elements)
        self.assertEqual(skipped, {"bad-id": 6, "duplicate": 1, "bad-coordinates": 1})
        operators = {f["properties"]["url"][-2:]: f["properties"]["operator"] for f in collection["features"]}
        self.assertEqual(operators, {"20": "First", "21": ""})

    def test_not_node_and_no_coordinates(self):
        no_lat = node(5)
        del no_lat["lat"]
        no_lon = node(6)
        del no_lon["lon"]
        elements = [{"type": "way", "id": 1}, {"type": "relation", "id": 2}, "text", 7, None,
                    {"id": 3, "lat": 1, "lon": 1}, no_lat, no_lon]
        self.assertEqual(self.skipped_for(elements), ([], {"not-node": 6, "no-coordinates": 2}))

    def test_summary_lines_sorted_and_free_of_data(self):
        no_lat = node(9000010, tags={"operator": "SecretTagText"})
        del no_lat["lat"]
        extra = [{"type": "way", "id": 1}, {"type": "relation", "id": 2}, no_lat,
                 node(9000011, lat=200, lon=-120.123456), copy.deepcopy(ELEMENTS[0])]
        code, out, _ = self.run_tool(path=self.saved({"elements": ELEMENTS + extra}))
        self.assertEqual(code, 0)
        lines = out.splitlines()
        start = lines.index("points=3 skipped=5")
        self.assertEqual(lines[start:start + 5], ["points=3 skipped=5", "skipped bad-coordinates=1",
                                                  "skipped duplicate=1", "skipped no-coordinates=1",
                                                  "skipped not-node=2"])
        self.assertTrue(lines[start + 5].startswith("ATTRIBUTION REQUIRED: "))
        self.assertTrue(lines[0].startswith("Wrote 3 points to "))
        summary = "\n".join(lines[1:])  # line 0 names the output path, which is random here
        for needle in ("200", "120.123456", "SecretTagText", "Example", "1.25", "9000"):
            self.assertNotIn(needle, summary)
        self.assertEqual((self.map_dir / "cameras.geojson").read_text(encoding="utf-8"), golden(ELEMENTS))

    def test_from_file_and_bbox_mutually_exclusive(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            tool.main(["--directory", str(self.campaign), "--from-file", str(self.saved()),
                       "--bbox", "0", "0", "1", "1"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("not allowed with argument", err.getvalue())
        self.assertFalse(self.campaign.exists())
        self.urlopen.assert_not_called()

    def test_written_map_passes_content_validation(self):
        shutil.copytree(EXAMPLE, self.campaign, ignore=shutil.ignore_patterns("public"))
        (self.map_dir / "cameras.geojson").unlink()
        code, _, _ = self.run_tool()
        self.assertEqual(code, 0)
        loaded = load_map(self.campaign / "content")
        self.assertEqual(loaded["camera_count"], 3)
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
