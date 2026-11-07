"""DF0237-12: data/cameras.csv, the spreadsheet copy of the community camera map (synthetic content only, no network)."""
import csv
import hashlib
import io
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool import camera_csv
from campaign_tool.content import ContentError
from campaign_tool.site import build_site
from campaign_tool.site_data_catalogue import verify_data_package
from tests.test_site_cameras_page import NODE, SIX, point, square

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
ATTRIBUTION = "\u00a9 OpenStreetMap contributors. ODbL 1.0; attribution and share-alike apply."
SOURCE = "https://records.example.invalid/r"


def shape(name, rings, key="NAME", kind="Polygon"):
    return {"type": "Feature", "properties": {key: name}, "geometry": {"type": kind, "coordinates": rings}}


def box(west, east, south=0.0, north=2.0):
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


def at(x, y, url=None, **props):
    return {"type": "Feature", "properties": dict(props, url=url), "geometry": {"type": "Point", "coordinates": [x, y]}}


class CameraCsvTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-cameras-csv-")
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

    def checks(self, entries):
        self.write(self.map / "point-checks.json", {"schema_version": 1, "checks": entries})

    def build(self):
        build_site(self.root)
        return (self.public / "data" / "cameras.csv").read_bytes()

    def table(self):
        return list(csv.reader(io.StringIO(self.build().decode("utf-8"), newline="")))

    def test_exact_headers_rows_and_latest_check(self):
        features = [dict(f, properties=dict(f["properties"])) for f in SIX]
        features[0]["properties"]["direction"] = "NE"
        self.cameras(features)
        self.checks([{"node": 10, "status": "duplicate", "checked_on": "2026-10-01", "source": SOURCE},
                     {"node": 10, "status": "seen_in_person", "checked_on": "2026-10-30", "source": SOURCE},
                     {"node": 10, "status": "in_agency_record", "checked_on": "2026-10-30", "source": SOURCE},  # same-day tie
                     {"node": 20, "status": "not_found_on_site", "checked_on": "2026-10-02", "source": SOURCE}])
        raw = self.build()
        self.assertTrue(raw.startswith(b"osm_url,latitude,longitude,manufacturer,operator,direction,city,check_status,checked_on\r\n"))
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        rows = {row[0]: row for row in self.table()[1:]}
        self.assertEqual(rows[NODE + "30"], [NODE + "30", "0.6", "0.6", "Flock Safety", "Cedar PD", "NE", "Cedar", "", ""])
        self.assertEqual(rows[NODE + "10"][3:], ["Smith & Co", "Cedar PD", "", "Cedar", "in_agency_record", "2026-10-30"])
        self.assertEqual(rows[NODE + "20"][7:], ["not_found_on_site", "2026-10-02"])
        self.assertEqual(rows[NODE + "40"][6:], ["", "", ""])  # outside every boundary: blank city, no check
        self.assertEqual(rows["https://www.openstreetmap.org/way/5"][6:], ["Pine", "", ""])  # a way has no node check
        self.assertEqual(rows[""][1:3], ["1.4", "0.9"])  # latitude is coordinates[1]
        self.assertEqual([row[0] for row in self.table()[1:]], sorted(r[0] for r in self.table()[1:] if r[0]) + [""])

    def test_boundary_city_rule(self):
        outer = box(0.0, 2.0)
        boundaries = {"features": [
            shape("Zed", [outer]), shape("Alpha", [box(0.0, 1.0)]), shape("Alpha", [box(0.0, 1.0)]),  # lexicographic, deduplicated
            shape("Ring", [box(3.0, 5.0), box(3.5, 4.0, 0.5, 1.5)], kind="Polygon"),
            shape("Multi", [[box(6.0, 7.0)], [box(8.0, 9.0)]], kind="MultiPolygon"),
            shape("Shown as base", [box(10.0, 11.0)], key="BASENAME"),
            {"type": "Feature", "properties": {"NAME": "Skipped", "BASENAME": ""}, "geometry": {"type": "Point", "coordinates": [0, 0]}},
            {"type": "Feature", "properties": {"NAME": "Broken"}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0]]]}}]}
        boundaries["features"].append({"type": "Feature", "properties": {"BASENAME": "Base", "NAME": "Name"},
                                       "geometry": {"type": "Polygon", "coordinates": [box(20.0, 21.0)]}})
        cameras = {"features": [at(0.5, 1.0, "u1"), at(1.5, 1.0, "u2"), at(3.2, 1.0, "u3"), at(3.7, 1.0, "u4"), at(8.5, 1.0, "u5"),
                                at(10.5, 1.0, "u6"), at(20.5, 1.0, "u7"), at(50.0, 50.0, "u8"), at(1.0, 1.0, "u9")]}
        city = {row["osm_url"]: row["city"] for row in camera_csv.camera_rows(cameras, boundaries, {})}
        self.assertEqual([city["u" + str(n)] for n in range(1, 10)],
                         ["Alpha", "Zed", "Ring", "", "Multi", "Shown as base", "Base", "", "Zed"])
        # u4 sits in Ring's hole; u9 is on Alpha's east edge, which the half-open test gives to Zed only
        self.assertEqual(camera_csv.camera_rows({"features": [at(1.0, 1.0)]}, {"features": []}, {})[0]["city"], "")

    def test_csv_encoding_and_no_private_fields(self):
        features = [at(-0.5, -1.5, NODE + "1", manufacturer="=cmd", operator="a,b \"q\" \u00e9\u4e2d", private_tag="SENTINEL-PROP"),
                    at(0.7, 0.6, NODE + "2", manufacturer="+1", operator="-2", model="@x", direction="N;NE"),
                    at(0.8, 0.6, NODE + "3", manufacturer="a=b", operator="x+y-z@w", tagged=True),
                    at(0.9, 0.6, NODE + "4", manufacturer=12, operator=False),
                    at(0.95, 0.6, NODE + "5", manufacturer="@x")]
        self.cameras(features)
        self.checks([{"node": 1, "status": "seen_in_person", "checked_on": "2026-10-30", "source": SOURCE,
                      "note": "SENTINEL-NOTE"}])
        raw = self.build()
        for sentinel in (b"SENTINEL-PROP", b"SENTINEL-NOTE", b"records.example.invalid"):
            self.assertNotIn(sentinel, raw)
        self.assertTrue(raw.endswith(b"\r\n"))
        rows = {row[0]: row for row in self.table()[1:]}
        self.assertEqual(rows[NODE + "1"][1:5], ["'-1.5", "'-0.5", "'=cmd", "a,b \"q\" \u00e9\u4e2d"])
        self.assertEqual(rows[NODE + "2"][3:6], ["'+1", "'-2", "N;NE"])
        self.assertEqual(rows[NODE + "2"][2], "0.7")
        self.assertEqual(rows[NODE + "3"][3:5], ["a=b", "x+y-z@w"])  # interior characters are untouched
        self.assertEqual(rows[NODE + "4"][3:5], ["12", "false"])
        self.assertEqual(rows[NODE + "5"][3], "'@x")
        self.assertTrue(rows[NODE + "1"][8].startswith("2026"))
        self.assertIn(b"\r\n", raw)

    def test_legacy_points_and_order(self):
        urls = [NODE + "2", NODE + "10", "https://www.openstreetmap.org/way/7", None, NODE + "10", None]
        features = [at(0.6, 0.6, url, manufacturer=f"m{n}") for n, url in enumerate(urls)]
        features.append({"type": "Feature", "properties": None, "geometry": {"type": "Point", "coordinates": [0.6, 0.7]}})
        self.cameras(features)
        rows = self.table()[1:]
        self.assertEqual([r[0] for r in rows], [NODE + "10", NODE + "10", NODE + "2", "https://www.openstreetmap.org/way/7", "", "", ""])
        self.assertEqual([r[3] for r in rows[:2]], ["m1", "m4"])  # stable input order for equal URLs
        self.assertEqual([r[3] for r in rows[4:]], ["m3", "m5", ""])
        with self.assertRaises(ContentError) as caught:
            camera_csv.camera_rows({"features": [at(0, 0), at(math.nan, 1.0)]}, {"features": []}, {})
        self.assertEqual(str(caught.exception),
                         "map/cameras.geojson.features[1].geometry.coordinates: finite coordinates required for CSV")
        self.cameras([])
        self.assertEqual(self.build(), b"osm_url,latitude,longitude,manufacturer,operator,direction,city,check_status,checked_on\r\n")
        self.cameras([at(0.6 + n / 100000, 0.6, f"{NODE}{n}") for n in range(2105)])
        self.assertEqual(len(self.table()), 2106)  # the HTML list is capped, the spreadsheet is not

    def test_catalogue_matches_final_csv(self):
        raw = self.build()
        resources = {r["path"]: r for r in json.loads((self.public / "data" / "datapackage.json").read_text(encoding="utf-8"))["resources"]}
        resource = resources["cameras.csv"]
        self.assertEqual((resource["name"], resource["mediatype"], resource["bytes"], resource["hash"]),
                         ("cameras-csv", "text/csv", len(raw), "sha256:" + hashlib.sha256(raw).hexdigest()))
        self.assertEqual(resource["description"], "Community-mapped camera points; ownership unconfirmed.")
        self.assertEqual(resource["schema"]["fields"], [{"name": n, "type": "string"} for n in camera_csv.COLUMNS])
        self.assertEqual(resource["licenses"], [{"name": "ODbL-1.0", "path": "https://opendatacommons.org/licenses/odbl/1-0/"}])
        self.assertEqual(resource["attribution"], ATTRIBUTION)
        page = (self.public / "data.html").read_text(encoding="utf-8")
        row = page[page.index('data-path="data/cameras.csv"'):].split("</tr>")[0]
        self.assertIn(ATTRIBUTION, row)
        self.assertIn('href="https://opendatacommons.org/licenses/odbl/1-0/"', row)
        self.assertEqual(verify_data_package(self.public), [])
        target = self.public / "data" / "cameras.csv"
        target.write_bytes(raw + b"x\r\n")  # a changed file is caught offline
        self.assertTrue(any(path == "data/cameras.csv" for path, _ in verify_data_package(self.public)))
        target.write_bytes(raw.replace(b"latitude", b"lat", 1))
        self.assertTrue(any(path == "data/cameras.csv" for path, _ in verify_data_package(self.public)))
        target.unlink()
        self.assertTrue(any(path == "data/cameras.csv" for path, _ in verify_data_package(self.public)))

    def test_repeat_no_map_and_stale_cleanup(self):
        first = self.build()
        before = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertEqual(self.build(), first)
        again = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertTrue(before == again, "two builds differ")
        shutil.rmtree(self.map)
        build_site(self.root)
        self.assertFalse((self.public / "data" / "cameras.csv").exists())
        self.assertNotIn("cameras.csv", (self.public / "data" / "datapackage.json").read_text(encoding="utf-8"))
        self.assertNotIn("cameras.csv", (self.public / "data.html").read_text(encoding="utf-8"))

    def test_private_value_in_a_camera_tag_refuses_the_build(self):
        build_site(self.root)
        kept = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.cameras([at(0.6, 0.6, NODE + "1", operator="call 805-555-0142")])
        with self.assertRaises(ContentError):
            build_site(self.root)
        now = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertTrue(kept == now, "public/ changed after a refused build")


if __name__ == "__main__":
    unittest.main()
