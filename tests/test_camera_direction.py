"""DF0237-11: an OpenStreetMap camera facing tag kept as tagged, shown as ``Faces: NE (OpenStreetMap tag)``.

Synthetic saved answers and a temp copy of the fictional example only; the network is blocked and nothing is sent.
Run as a script with ``--popup-fixture`` it prints built controllers for tests/map-controller.test.mjs.
"""
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from campaign_tool.camera_direction import DIRECTION_PATTERN, valid_direction  # noqa: E402
from campaign_tool.cli import read_config  # noqa: E402
from campaign_tool.content import ContentError, load_content  # noqa: E402
from campaign_tool.content_check import content_problems  # noqa: E402
from campaign_tool.site import Site, build_site  # noqa: E402
from tests import test_map_point_checks as point_checks  # noqa: E402  (the module: its test classes are not re-collected here)
from tests.test_site_cameras_page import NODE, ROW, SECTION, SIX, point  # noqa: E402
from tools import fetch_osm_alprs as tool  # noqa: E402

TODAY = date(2026, 11, 3)
WORDS = "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split()
GOOD = ["0", "359", "7", "90", "100", "299", "300", "349", "350", "NE", "ne", "Nne", "nNw", "NE;SW", "SW;NE", "NE;NE",
        "0;90;180;270", "N;e;S;w", "359;359;359;359", "NNE;NNE;NNE;NNE"]
BAD = ["", "00", "01", "360", "400", "-1", "+1", "1.5", "90.0", "90°", "north", "NORTH", "NEE", "NS", "N;", ";N", "N;;E",
       " N", "N ", "N; E", "N;E;S;W;N", "0;1;2;3;4", "N\n", "\nN", "N\nE", "١٢", "９０", "ſ", "N;ſ", "K", "<b>N</b>",
       "1;2;3;4;5", "NE,SW", "NE/SW", "N" * 16, "359;359;359;359;"]
NOT_TEXT = [90, 90.5, True, False, None, ["N"], {"a": 1}, b"N"]


def node(node_id, lat=1.0, lon=1.0, **tags):
    return {"type": "node", "id": node_id, "lat": lat, "lon": lon, "tags": tags}


def url(node_id):
    return f"https://www.openstreetmap.org/node/{node_id}"


class GrammarTests(unittest.TestCase):
    def test_direction_grammar_and_verbatim(self):
        for word in WORDS:
            for form in (word, word.lower(), word.capitalize()):
                self.assertTrue(valid_direction(form), form)
        self.assertEqual([v for v in range(360) if not valid_direction(str(v))], [])
        for value in GOOD:
            with self.subTest(good=value):
                self.assertTrue(valid_direction(value))
                self.assertIsNotNone(re.fullmatch(DIRECTION_PATTERN, value))  # the portable source works with no flags
                collection, skipped = tool.to_geojson([node(1, direction=value)])
                self.assertEqual(collection["features"][0]["properties"]["direction"], value)  # case, order, duplicates
                self.assertEqual(skipped, {})
        for value in BAD + NOT_TEXT:
            with self.subTest(bad=value):
                self.assertFalse(valid_direction(value))
                counts = {}
                tags = {"direction": value}
                collection, _ = tool.to_geojson([{"type": "node", "id": 1, "lat": 1, "lon": 1, "tags": tags}],
                                                direction_counts=counts)
                props = collection["features"][0]["properties"]
                self.assertIn("direction", props)
                self.assertIsNone(props["direction"])  # explicit null, and the point is kept
                self.assertEqual(counts, {"invalid": 1})
        self.assertFalse(valid_direction(None))


class FetchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-direction-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.campaign = self.tmp / "campaign"
        self.cameras = self.campaign / "content" / "map" / "cameras.geojson"
        patcher = mock.patch.object(tool.urllib.request, "urlopen", side_effect=AssertionError("network used"))
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)

    def saved(self, elements):
        path = self.tmp / "saved.json"
        path.write_text(json.dumps({"elements": elements}), encoding="utf-8")
        return path

    def run_tool(self, elements, *extra):
        self.campaign.mkdir(exist_ok=True)
        (self.campaign / "campaign.json").touch()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["--directory", str(self.campaign), "--from-file", str(self.saved(elements)), *extra])
        self.urlopen.assert_not_called()
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        return sorted((str(p.relative_to(self.tmp)), p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else b"")
                      for p in [self.campaign, *self.campaign.rglob("*")])

    def test_tag_precedence_counts_and_legacy(self):
        elements = [node(1, direction="NE", **{"camera:direction": "SW"}),      # direction wins
                    node(2, direction="up", **{"camera:direction": "SW"}),      # invalid primary: no fallback
                    node(3, **{"camera:direction": "270"}),                      # fallback key
                    node(4, **{"camera:direction": "sideways"}),                 # invalid fallback
                    node(5, direction=""), node(6, direction=None),              # present but empty / null: invalid
                    node(7, manufacturer="Flock Safety"),                         # no tag at all
                    node(1, direction="bad"),                                     # duplicate: skipped, not counted
                    node(8, lat=999, direction="bad"),                            # bad coordinates: skipped, not counted
                    {"type": "way", "id": 9, "tags": {"direction": "bad"}}]       # not a node: skipped, not counted
        counts = {}
        collection, skipped = tool.to_geojson(elements, direction_counts=counts)
        by_node = {f["properties"]["url"]: f["properties"] for f in collection["features"]}
        self.assertEqual([by_node[url(i)].get("direction", "ABSENT") for i in range(1, 8)],
                         ["NE", None, "270", None, None, None, "ABSENT"])
        self.assertEqual(counts, {"invalid": 4})
        self.assertEqual((len(collection["features"]), skipped), (7, {"duplicate": 1, "bad-coordinates": 1, "not-node": 1}))
        self.assertEqual(tool.to_geojson(elements)[1], skipped)  # the counter is optional
        code, out, err = self.run_tool(elements)
        self.assertEqual(code, 0)
        self.assertIn("points=7 skipped=3\n", out)  # an invalid facing is never a skip
        self.assertEqual(err, "direction invalid=4\n")  # a count only: no tag text, coordinate or path
        self.assertEqual(json.loads(self.cameras.read_text(encoding="utf-8")), collection)
        # no facing tag anywhere: no property, no diagnostic, the pre-feature bytes
        plain = [node(1, manufacturer="A", operator="B"), node(2)]
        code, out, err = self.run_tool(plain, "--allow-empty")
        self.assertEqual((code, err), (0, ""))
        features = json.loads(self.cameras.read_text(encoding="utf-8"))["features"]
        self.assertEqual([sorted(f["properties"]) for f in features], [["manufacturer", "operator", "url"]] * 2)
        self.assertNotIn("direction", self.cameras.read_text(encoding="utf-8"))

    def old_map(self, features):
        self.cameras.parent.mkdir(parents=True, exist_ok=True)
        self.cameras.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")

    def feature(self, node_id, **extra):
        return {"type": "Feature", "properties": {"manufacturer": "", "operator": "", "url": url(node_id), **extra},
                "geometry": {"type": "Point", "coordinates": [1.0, 1.0]}}

    def check(self, elements, *extra):
        before = self.snapshot()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["--directory", str(self.campaign), "--from-file", str(self.saved(elements)), "--check", *extra])
        self.assertEqual(self.snapshot(), before, "--check must write nothing")  # bytes and mtimes of every file
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_check_direction_retag_and_no_writes(self):
        self.old_map([self.feature(1, direction="NE"), self.feature(2, direction="NE"), self.feature(3, direction="NE;SW"),
                      self.feature(4), self.feature(5, direction="90"), self.feature(6), self.feature(7, direction=None),
                      self.feature(8, direction="NE"), self.feature(9, direction="NE"), self.feature(10, direction="NE")])
        (self.campaign / "content" / "map" / "history.json").write_text('{"schema_version": 1, "records": []}\n',
                                                                        encoding="utf-8")
        (self.campaign / "content" / "map" / "snapshot.json").write_text("{}\n", encoding="utf-8")
        new = [node(1, direction="NE"),                      # same: unchanged
               node(2, direction="SW"),                      # changed
               node(3, direction="SW;NE"),                   # order changed
               node(4, direction="N"),                       # unknown -> valid: retagged
               node(5),                                      # valid -> unknown: retagged, not removed
               node(6, direction="bogus"),                   # old omitted, new null: still unknown, unchanged
               node(7),                                      # null -> absent: unchanged
               node(8, direction="ne"),                      # case changed
               node(9, lon=1.1, direction="SW"),             # moved and retagged
               node(10, direction="NE")]
        code, lines, err = self.check(new)
        self.assertEqual(err, "direction invalid=1\n")
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "added=0 removed=0 moved=1 retagged=6 unchanged=4 unkeyed_old=0")
        listed = {line.rstrip(":"): [] for line in lines if line.endswith(":")}
        kind = None
        for line in lines[1:-1]:
            kind = line[:-1] if line.endswith(":") else kind
            if line.startswith("  "):
                listed[kind].append(line.strip())
        self.assertEqual(listed["retagged"], [url(n) for n in (2, 3, 4, 5, 8, 9)])
        self.assertEqual(listed["moved"], [url(9)])
        self.assertEqual(lines[-1], "Check only: nothing was written.")
        # a valid facing that disappears is a retag, never a removal; unknown -> valid is retagged too
        self.old_map([self.feature(1), self.feature(2, direction=None)])
        code, lines, _ = self.check([node(1, direction="45"), node(2, direction="45")])
        self.assertEqual((code, lines[0]), (0, "added=0 removed=0 moved=0 retagged=2 unchanged=0 unkeyed_old=0"))
        # a removed point still exits 3 and the list cap and order are unchanged
        self.old_map([self.feature(i, direction="N") for i in range(1, 26)])
        code, lines, _ = self.check([node(i, direction="S") for i in range(1, 24)])
        self.assertEqual((code, lines[0]), (3, "added=0 removed=2 moved=0 retagged=23 unchanged=0 unkeyed_old=0"))
        self.assertIn("  … +3 more", lines)
        # a direction alone never adds or moves
        self.old_map([self.feature(1, direction="N")])
        self.assertEqual(self.check([node(1, direction="N")])[1][0], "added=0 removed=0 moved=0 retagged=0 unchanged=1 unkeyed_old=0")
        # no tag text in any output
        self.assertNotIn("bogus", "\n".join(self.check([node(1, direction="bogus")])[1]))


def tagged_six():
    props = {"30": "NE", "10": "90;270", "20": None}
    out = []
    for feature in SIX:
        feature = json.loads(json.dumps(feature))
        match = re.search(r"/node/(\d+)$", feature["properties"].get("url") or "")
        if match and match.group(1) in props:
            feature["properties"]["direction"] = props[match.group(1)]
        elif not match and feature["properties"].get("url") is None:
            feature["properties"]["direction"] = "ne;SW;NW;3"  # a row with no OSM link
        out.append(feature)
    return out


class SiteTests(unittest.TestCase):
    setUp, built, site, checks = point_checks.PointChecksTests.setUp, point_checks.PointChecksTests.built, point_checks.PointChecksTests.site, point_checks.PointChecksTests.checks
    write = staticmethod(point_checks.PointChecksTests.write)

    def cameras(self, features):
        self.write(self.map / "cameras.geojson", {"type": "FeatureCollection", "features": features})

    def rows(self, html):
        return [cells for _, _, body in SECTION.findall(html) for cells in ROW.findall(body)]

    def refused(self, features):
        self.cameras(features)
        with self.assertRaises(ContentError) as caught:
            load_content(self.root, TODAY)
        listed = [p for p in content_problems(self.root) if "direction" in p]
        return str(caught.exception), listed

    def test_build_and_check_share_direction_refusal(self):
        for value in (None, "NE", "90;270", "ne;SW;NW;3", "359;359;359;359"):  # absent is the default fixture
            with self.subTest(ok=value):
                self.cameras([point(0.6, 0.6, url=NODE + "1", direction=value)])
                load_content(self.root, TODAY)
                self.assertEqual([p for p in content_problems(self.root) if "direction" in p], [])
        self.cameras(SIX)
        load_content(self.root, TODAY)
        before = {p: p.read_bytes() for p in self.map.glob("*") if p.name != "cameras.geojson"}  # the other inputs
        for value in BAD + [90, 90.5, True, ["N"], {"a": 1}]:  # JSON values; null is allowed
            with self.subTest(bad=value):
                message, listed = self.refused([SIX[0], point(0.6, 0.6, url=NODE + "1", direction=value)])
                line = "map/cameras.geojson.features[1].direction: expected an OpenStreetMap facing tag or null"
                self.assertEqual(message, line)
                self.assertEqual(listed, [line])  # content-check lists the same line from the same validator
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        # unrelated scalar and URL rules are unchanged
        for props, needle in (({"manufacturer": "<b>"}, "manufacturer: plain text only"),
                              ({"operator": ["x"]}, "operator: scalar values only"),
                              ({"url": "http://example.invalid/node/1"}, "url: must be an openstreetmap.org object URL")):
            self.cameras([point(0.6, 0.6, **props)])
            with self.assertRaisesRegex(ContentError, needle):
                load_content(self.root, TODAY)
        self.cameras([point(0.6, 0.6, other=7, flag=True, note="plain")])
        load_content(self.root, TODAY)  # other scalar properties still pass

    def test_build_refuses_before_replacing_the_public_site(self):
        self.cameras(tagged_six())
        build_site(self.root, TODAY)
        before = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.cameras([point(0.6, 0.6, url=NODE + "1", direction="north")])
        with self.assertRaises(ContentError):
            build_site(self.root, TODAY)
        after = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_facing_list_popup_checks_and_labels(self):
        self.cameras(tagged_six())
        self.checks([point_checks.check(10, "seen_in_person", "2026-10-30")])
        built = self.built()
        page = built["cameras.html"].decode()
        self.assertEqual(page.count("Faces: "), 3)  # nodes 30, 10 and the row with no link; null and untagged nothing new
        thirds = [row[2] for row in self.rows(page)]
        self.assertEqual((len(thirds), sum("Faces: " in t for t in thirds)), (6, 3))  # one OSM cell per row, no new column
        self.assertIn('node 30</a><br>Faces: NE (OpenStreetMap tag)</td>', page)
        self.assertIn('node 10</a><br>Faces: 90;270 (OpenStreetMap tag)</td>', page)
        self.assertIn("<td>no OSM link<br>Faces: ne;SW;NW;3 (OpenStreetMap tag)</td>", page)
        self.assertNotIn("null", page)
        # popup wiring: facing first, then the check renderer, nothing new in site-data.js
        app = built["app.js"].decode()
        self.assertIn("Faces: {direction} (OpenStreetMap tag)", app)
        self.assertLess(app.index("Faces: {direction}"), app.index("pointChecks[m[1]]"))
        self.assertNotIn("direction", built["site-data.js"].decode())
        # an escaped custom label reaches the list and the popup; the placeholder is mandatory
        self.write(self.content / "labels.json", {"schema_version": 1, "labels": {
            "map.direction_line": "Mira {direction} & más"}})
        built = self.built()
        self.assertIn("node 30</a><br>Mira NE &amp; más</td>", built["cameras.html"].decode())
        self.assertIn(r"Mira {direction} & m\u00e1s", built["app.js"].decode())  # js_json escapes non-ASCII
        self.assertNotIn("Faces:", built["cameras.html"].decode())
        # Spanish draft: placeholder-complete and used
        es = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))
        self.write(self.content / "labels.json", es)
        self.assertIn("Orientación: NE (etiqueta de OpenStreetMap)", self.built()["cameras.html"].decode())

    def test_directionless_baseline_and_determinism(self):
        self.cameras(SIX)
        baseline = self.built()
        null_only = [json.loads(json.dumps(f)) for f in SIX]
        for feature in null_only:
            feature["properties"]["direction"] = None
        self.cameras(null_only)
        nulls = self.built()
        self.assertEqual(set(nulls), set(baseline))
        for name in baseline:
            if name != "data/cameras.geojson":
                self.assertEqual(nulls[name], baseline[name], name)  # list, popup script and data: byte-identical
        self.assertNotIn(b"Faces", b"".join(baseline.values()))
        self.assertNotIn(b"Faces", nulls["app.js"] + nulls["cameras.html"])
        self.cameras(tagged_six())
        first, second = self.built(), self.built()
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(baseline))
        self.assertNotEqual(first["cameras.html"], baseline["cameras.html"])
        self.assertIn(b"Faces", first["app.js"])
        for name in ("sources.html", "findings.html", "index.html"):
            if name in baseline:
                self.assertEqual(first[name], baseline[name], name)

    def test_invalid_runtime_property_suppressed_by_the_popup_renderer(self):
        from campaign_tool.site import point_direction_renderer
        site = self.site()
        renderer = point_direction_renderer(site.labels)
        self.assertIn("typeof d===\"string\"", renderer)
        self.assertNotIn("direction:", renderer.replace("{direction}", ""))
        self.assertEqual(site.direction_line({"direction": "<b>"}), "")
        self.assertEqual(site.direction_line({"direction": 90}), "")
        self.assertEqual(site.direction_line({}), "")
        self.assertEqual(site.direction_line({"direction": "NE"}), "Faces: NE (OpenStreetMap tag)")


def popup_fixture():
    """Built controllers (app.js without its page bootstrap) and site-data.js for node's popup cases."""
    out = {}
    for name, with_checks, labels in (("facing", False, None), ("both", True, None),
                                      ("custom", False, {"map.direction_line": "Mira {direction} & más"}),
                                      ("none", False, None)):
        with tempfile.TemporaryDirectory(prefix="synthetic-direction-popup-") as temporary:
            root = Path(temporary) / "campaign"
            shutil.copytree(REPO_ROOT / "examples" / "fictional-campaign", root, ignore=shutil.ignore_patterns("public"))
            map_dir = root / "content" / "map"
            features = SIX if name == "none" else tagged_six()
            (map_dir / "cameras.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}),
                                                     encoding="utf-8")
            if with_checks:
                (map_dir / "point-checks.json").write_text(json.dumps({"schema_version": 1, "checks": [
                    point_checks.check(10, "seen_in_person", "2026-10-30")]}), encoding="utf-8")
            if labels:
                (root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}),
                                                              encoding="utf-8")
            site = Site(root, read_config(root), load_content(root, TODAY), today=TODAY)
            app = site.app_js()
            boot = 'document.addEventListener("DOMContentLoaded",function(){document.querySelectorAll(".city-choice")'
            out[name] = {"controller": app[:app.rindex(boot)], "site_data": site.site_data_js()}
    return out


if __name__ == "__main__":
    if sys.argv[1:] == ["--popup-fixture"]:
        print(json.dumps(popup_fixture()))
    else:
        unittest.main()
