"""fetch_osm_alprs --check: added/removed/moved/retagged points by node URL vs the current map; writes nothing (RF1730-10)."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import fetch_osm_alprs as tool

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "osm"
SMALL = FIXTURES / "overpass-small.json"
CHANGED = FIXTURES / "overpass-small-changed.json"
ELEMENTS = json.loads(SMALL.read_text(encoding="utf-8"))["elements"]
MESSAGE = "fetch_osm_alprs: current cameras.geojson is not readable GeoJSON"
LAST = "Check only: nothing was written."
STEP = 1 / 111195.08  # degrees of latitude per metre on the haversine sphere


def url(node_id):
    return f"https://www.openstreetmap.org/node/{node_id}"


def node(node_id, lat=1.0, lon=1.0, **tags):
    return {"type": "node", "id": node_id, "lat": lat, "lon": lon, "tags": tags}


def counts(added=0, removed=0, moved=0, retagged=0, unchanged=0, unkeyed_old=0):
    return (f"added={added} removed={removed} moved={moved} retagged={retagged} "
            f"unchanged={unchanged} unkeyed_old={unkeyed_old}")


class CheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-osm-check-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.campaign = self.tmp / "campaign"
        self.map_dir = self.campaign / "content" / "map"
        self.cameras = self.map_dir / "cameras.geojson"
        patcher = mock.patch.object(tool.urllib.request, "urlopen", side_effect=AssertionError("network used"))
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)

    def write_old(self, elements=None, raw=None):
        self.map_dir.mkdir(parents=True, exist_ok=True)
        if raw is None:
            raw = json.dumps(tool.to_geojson(elements)[0], indent=1) + "\n"
        self.cameras.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))

    def saved(self, elements):
        path = self.tmp / "saved.json"
        path.write_text(json.dumps({"elements": elements}), encoding="utf-8")
        return path

    def snapshot(self):
        if not self.campaign.exists():
            return None
        return sorted((str(p.relative_to(self.tmp)), p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else b"")
                      for p in [self.campaign, *self.campaign.rglob("*")])

    def check(self, *extra, source=None, elements=None, scope=None):
        before = self.snapshot()
        scope = scope or ["--from-file", str(source or self.saved(elements))]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["--directory", str(self.campaign), *scope, "--check", *extra])
        self.assertEqual(self.snapshot(), before, "--check must write nothing")
        self.urlopen.assert_not_called()
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_same_answer_twice_all_unchanged(self):
        self.campaign.mkdir()  # the write run needs a campaign directory (RF1730-11); --check does not
        (self.campaign / "campaign.json").write_text("{}\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(["--directory", str(self.campaign), "--from-file", str(SMALL)]), 0)
        for _ in range(2):
            self.assertEqual(self.check(source=SMALL), (0, [counts(unchanged=3), LAST], ""))

    def test_one_removed_exits_3_and_names_it(self):
        self.write_old(ELEMENTS)
        kept = [e for e in ELEMENTS if e["id"] != 9000002]
        lines = [counts(removed=1, unchanged=2), "removed:", "  " + url(9000002), LAST]
        self.assertEqual(self.check(elements=kept), (3, lines, ""))

    def test_changed_fixture_every_kind_and_no_coordinates_or_tag_text(self):
        self.write_old(ELEMENTS)
        code, lines, err = self.check(source=CHANGED)
        self.assertEqual((code, err), (3, ""))
        self.assertEqual(lines, [counts(1, 1, 1, 1, 1), "added:", "  " + url(9000004), "removed:", "  " + url(9000002),
                                 "moved:", "  " + url(9000003), "retagged:", "  " + url(9000003), LAST])
        text = "\n".join(lines)
        for needle in ("Example", "Agency", "County Office", "Vendor", "1.25", "1.5", "1.75", "0.25", "0.75",
                       "1.000045", "0.5"):
            self.assertNotIn(needle, text)

    def test_moved_threshold_and_move_metres(self):
        self.write_old([node(1), node(2), node(3)])
        new = [node(1, lat=1 + 5 * STEP), node(2, lat=1 + 50 * STEP), node(3)]
        self.assertEqual(self.check(elements=new),
                         (0, [counts(moved=1, unchanged=2), "moved:", "  " + url(2), LAST], ""))
        self.assertEqual(self.check("--move-metres", "0", elements=new),
                         (0, [counts(moved=2, unchanged=1), "moved:", "  " + url(1), "  " + url(2), LAST], ""))
        self.assertEqual(self.check("--move-metres", "60", elements=new), (0, [counts(unchanged=3), LAST], ""))
        self.assertAlmostEqual(tool._metres([0, 0], [0, 1]), 111195.08, delta=1)
        self.assertAlmostEqual(tool._metres([0, 60], [1, 60]), 55597, delta=10)

    def test_unusable_old_point_counts_as_moved(self):
        old = tool.to_geojson([node(1), node(2), node(3)])[0]
        old["features"][0]["geometry"] = None
        old["features"][1]["geometry"]["coordinates"] = ["1.0", "1.0"]
        old["features"][2]["geometry"]["coordinates"] = [1.0, 91.0]
        changes = tool.diff(old, tool.to_geojson([node(1), node(2), node(3)])[0])
        self.assertEqual((changes["moved"], changes["unchanged"]), ([url(1), url(2), url(3)], []))

    def test_retagged_and_moved_retagged_counted_in_both(self):
        self.write_old([node(1, operator="Old Op"), node(2, manufacturer="Old Make"), node(3, operator="X"), node(4)])
        new = [node(1, operator="New Op"), node(2, manufacturer="New Make"),
               node(3, lat=1 + 50 * STEP, operator="Y"), node(4)]
        self.assertEqual(self.check(elements=new), (0, [counts(moved=1, retagged=3, unchanged=1), "moved:",
                                                        "  " + url(3), "retagged:", "  " + url(1), "  " + url(2),
                                                        "  " + url(3), LAST], ""))

    def test_missing_tag_keys_in_hand_supplied_file_are_not_retagged(self):
        feature = {"type": "Feature", "properties": {"url": url(7)},
                   "geometry": {"type": "Point", "coordinates": [1.0, 1.0]}}
        self.write_old(raw=json.dumps({"type": "FeatureCollection", "features": [feature]}))
        self.assertEqual(self.check(elements=[node(7)]), (0, [counts(unchanged=1), LAST], ""))

    def test_list_cut_at_20_then_more(self):
        self.write_old([])
        for total, tail in ((20, []), (21, ["  … +1 more"]), (25, ["  … +5 more"])):
            with self.subTest(total=total):
                new = [node(i) for i in range(100 + total, 100, -1)]
                listed = ["  " + url(i) for i in range(101, 121)]
                self.assertEqual(self.check(elements=new), (0, [counts(added=total), "added:", *listed, *tail, LAST], ""))

    def test_old_feature_without_valid_node_url_is_unkeyed_not_removed(self):
        point = {"type": "Point", "coordinates": [1.0, 1.0]}
        features = [{"type": "Feature", "properties": {"url": url(1)}, "geometry": point}]
        bad_urls = [None, 5, "http://www.openstreetmap.org/node/5", url(0), url("05"), url("5x"), url(5) + " ",
                    url(5) + "\n", "https://www.openstreetmap.org/way/5", url(1)]
        features += [{"type": "Feature", "properties": {"url": u}, "geometry": point} for u in bad_urls]
        features += [{"type": "Feature", "properties": {}, "geometry": point},
                     {"type": "Feature", "geometry": point}, "text", None]
        self.write_old(raw=json.dumps({"type": "FeatureCollection", "features": features}))
        self.assertEqual(self.check(elements=[node(1)]), (0, [counts(unchanged=1, unkeyed_old=14), LAST], ""))

    def test_no_old_file_all_added_and_nothing_created(self):
        lines = [counts(added=3), "added:", *("  " + url(i) for i in (9000001, 9000002, 9000003)), LAST]
        self.assertEqual(self.check(source=SMALL), (0, lines, ""))
        self.assertFalse(self.campaign.exists())
        self.campaign.mkdir()
        self.assertEqual(self.check(source=SMALL), (0, lines, ""))
        self.assertFalse((self.campaign / "content").exists())

    def test_unreadable_old_file_exit_2_fixed_message(self):
        cases = {"list": "[]", "invalid json": "{", "empty": "", "invalid utf-8": b"\xff\xfe", "null": "null",
                 "feature": '{"type": "Feature"}', "no type": '{"features": []}',
                 "features not list": '{"type": "FeatureCollection", "features": {}}',
                 "nan": '{"type": "FeatureCollection", "features": [{"x": NaN}]}'}
        for label, raw in cases.items():
            with self.subTest(label):
                self.write_old(raw=raw)
                self.assertEqual(self.check(source=SMALL), (2, [], MESSAGE + "\n"))
        self.cameras.unlink()
        self.cameras.mkdir()
        self.assertEqual(self.check(source=SMALL), (2, [], MESSAGE + "\n"))

    def test_unreadable_old_file_refused_before_network(self):
        self.write_old(raw="[]")
        with mock.patch.object(tool, "fetch") as fetch:
            self.assertEqual(self.check(scope=["--bbox", "0", "0", "1", "1"]), (2, [], MESSAGE + "\n"))
        fetch.assert_not_called()

    def test_bad_move_metres_is_usage_error(self):
        argv = ["--directory", str(self.campaign), "--from-file", str(SMALL), "--check", "--move-metres"]
        for value in ("-1", "nan", "inf", "-inf", "ten"):
            with self.subTest(value), contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as caught:
                    tool.main([*argv, value])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("--move-metres", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            tool.main(["--directory", str(self.campaign), "--from-file", str(SMALL), "--move-metres", "5"])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.campaign.exists())

    def test_check_works_with_bbox_and_relation(self):
        self.write_old(ELEMENTS)
        for scope in (["--bbox", "0", "0", "2", "2"], ["--relation", "396490"]):
            with self.subTest(scope[0]), mock.patch.object(tool, "fetch", return_value={"elements": ELEMENTS[:2]}) \
                    as fetch:
                lines = [counts(removed=1, unchanged=2), "removed:", "  " + url(9000002), LAST]
                self.assertEqual(self.check(scope=scope), (3, lines, ""))
                fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
