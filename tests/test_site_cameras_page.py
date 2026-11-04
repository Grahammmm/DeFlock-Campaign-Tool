"""DF1507-13: cameras.html, a no-JS list of community-mapped points grouped by city boundary.

Synthetic content only: a temp copy of the fictional example with a two-city boundary file (Cedar | Pine, sharing
the edge x = 1.0) and six points. No network.
"""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from campaign_tool.cli import check_public_tree, read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
GROUP = "Unincorporated Cedar County (ownership unconfirmed)"
SECTION = re.compile(r'<section class="camera-group"><h3>([^<]*)</h3><p>(\d+) mapped points?</p>(.*?)</section>')
ROW = re.compile(r"<tr><td>([^<]*)</td><td>([^<]*)</td><td>(.*?)</td></tr>")
NODE = "https://www.openstreetmap.org/node/"


def square(name, west, east):
    ring = [[west, 0.5], [east, 0.5], [east, 1.5], [west, 1.5], [west, 0.5]]
    return {"type": "Feature", "properties": {"NAME": name}, "geometry": {"type": "Polygon", "coordinates": [ring]}}


def point(x, y, **properties):
    return {"type": "Feature", "properties": properties, "geometry": {"type": "Point", "coordinates": [x, y]}}


SIX = [point(0.6, 0.6, manufacturer="Flock Safety", operator="Cedar PD", url=NODE + "30"),
       point(0.7, 1.0, manufacturer="Smith & Co", operator="Cedar PD", url=NODE + "10"),
       point(0.9, 1.4, operator=None, url=None),  # no manufacturer key, null operator and url
       point(1.0, 1.0, manufacturer="Flock Safety", operator="", url=NODE + "20"),  # on the shared edge
       point(1.2, 0.8, manufacturer="  ", operator="Pine PD", url="https://www.openstreetmap.org/way/5"),
       point(1.8, 1.8, manufacturer="Flock Safety", operator="Unknown", url=NODE + "40")]


class CamerasPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-cameras-page-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"
        self.map = self.content / "map"
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

    def base_url(self, value):
        site = json.loads((self.content / "site.json").read_text(encoding="utf-8"))
        self.write(self.content / "site.json", dict(site, base_url=value))

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def page(self):  # rendered without writing public/
        return Site(self.root, read_config(self.root), load_content(self.root)).cameras_html()["html"]

    def sections(self, html):
        return [(name, int(count), ROW.findall(body)) for name, count, body in SECTION.findall(html)]

    # 1. two cities + one outside point: Cedar 3, Pine 2, then the group label section with 1
    def test_sections_by_city_then_group_with_counts(self):
        build_site(self.root)
        html = self.read("cameras.html")
        found = self.sections(html)
        self.assertEqual([(name, count, len(rows)) for name, count, rows in found],
                         [("Cedar", 3, 3), ("Pine", 2, 2), (GROUP, 1, 1)])
        self.assertIn("<h2>Community-mapped points; ownership unconfirmed.</h2>", html)
        self.assertIn("Points are community reports from OpenStreetMap, not confirmed agency ownership.", html)
        self.assertIn("Map data date unknown (OpenStreetMap)", html)
        self.assertIn("Gulf of Guinea near 0,0", html)  # the attribution text
        self.assertIn("<p>1 mapped point</p>", html)

    # 2. the shared-edge point is listed once (in Pine, by the counts' half-open rule); rows == features
    def test_shared_edge_point_listed_once(self):
        found = self.sections(self.page())
        links = [cells[2] for _, _, rows in found for cells in rows]
        self.assertEqual(len(links), len(SIX))
        self.assertEqual(sum(NODE + "20" in link for link in links), 1)
        self.assertIn(NODE + "20", "".join(cells[2] for cells in found[1][2]))  # Pine
        counts = json.loads(re.search(r"var CITIES=(\{.*\});", Site(
            self.root, read_config(self.root), load_content(self.root)).site_data_js()).group(1))
        self.assertTrue(counts["cedar"]["cameraCount"].startswith("3 "), counts["cedar"]["cameraCount"])

    # 3. missing/blank manufacturer or operator -> "not tagged"; null or way url -> "no OSM link"; rows by URL; escaped
    def test_untagged_values_links_order_and_escaping(self):
        cedar, pine, group = self.sections(self.page())
        self.assertEqual(cedar[2], [
            ("Smith &amp; Co", "Cedar PD", f'<a href="{NODE}10" rel="noopener noreferrer">node 10</a>'),
            ("Flock Safety", "Cedar PD", f'<a href="{NODE}30" rel="noopener noreferrer">node 30</a>'),
            ("not tagged", "not tagged", "no OSM link")])
        self.assertEqual(pine[2], [
            ("Flock Safety", "not tagged", f'<a href="{NODE}20" rel="noopener noreferrer">node 20</a>'),
            ("not tagged", "Pine PD", "no OSM link")])  # a way URL sorts after node URLs and is not linked
        self.assertEqual(group[2][0][:2], ("Flock Safety", "Unknown"))

    # 4. no map: no page, no footer/fallback links, sitemap the same page set as before
    def test_no_map_no_page_links_or_sitemap_entry(self):
        shutil.rmtree(self.map)
        self.base_url(BASE)
        written = {p.relative_to(self.public).as_posix() for p in build_site(self.root)}
        self.assertNotIn("cameras.html", written)
        for name in ("index.html", "about.html", "findings/index.html"):
            self.assertNotIn("/cameras.html", self.read(name), name)
        locs = [loc.text for loc in ET.fromstring(self.read("sitemap.xml")).iter(
            "{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
        self.assertEqual(locs, [BASE + p for p in ("/", "/agencies.html", "/findings/", "/corrections.html", "/sources.html",
                                                   "/meetings.html", "/about.html", "/methods.html", "/privacy.html", "/search.html", "/findings/packet-all.html", "/timeline.html",  # DF2037-14
                                                   "/agencies/cedar.html",  # DF2237-08 agency page
                                                   "/findings/cedar-policy-posted.html",
                                                   "/meetings/cedar-council-2026-10.html")])

    def test_with_map_linked_from_fallback_footer_and_sitemap(self):
        self.base_url(BASE)
        build_site(self.root)
        self.assertIn('rel="noopener noreferrer">open the DeFlock source map</a>. '
                      '<a href="/cameras.html">Read the list of mapped points</a>.</p>', self.read("index.html"))
        for name in ("index.html", "about.html", "cameras.html", "404.html"):
            footer = self.read(name).split('<footer class="site-footer">', 1)[1]
            self.assertIn('<a href="/cameras.html">Mapped points</a>', footer, name)
        locs = [loc.text for loc in ET.fromstring(self.read("sitemap.xml")).iter(
            "{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
        self.assertEqual(locs.count(BASE + "/cameras.html"), 1)
        self.assertIn('<link rel="canonical" href="https://cedar-records.example.invalid/cameras.html">',
                      self.read("cameras.html"))

    # 5. the name is reserved: a public-assets/cameras.html entry is refused (as a folder: collision; as a file:
    #    .html is not an asset extension) and nothing is written
    def test_public_assets_cameras_html_refused(self):
        folder = self.content / "public-assets" / "cameras.html"
        folder.mkdir(parents=True)
        (folder / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        with self.assertRaisesRegex(ContentError, r"public-assets/cameras\.html/logo\.svg: collides with a generated path"):
            build_site(self.root)
        self.assertFalse(self.public.exists())
        shutil.rmtree(folder)
        folder.write_text("<p>x</p>", encoding="utf-8")
        with self.assertRaisesRegex(ContentError, r"public-assets/cameras\.html: extension not allowed"):
            build_site(self.root)
        self.assertFalse(self.public.exists())

    #    ... and a content page content/pages/cameras.json is refused with a map (never silently replaced), kept without
    def test_content_page_named_cameras(self):
        (self.content / "pages").mkdir(exist_ok=True)
        self.write(self.content / "pages" / "cameras.json", {"title": "Our cameras", "description": "", "body_md": "Hi."})
        with self.assertRaisesRegex(ContentError, r"content/pages/cameras\.json: collides with the generated cameras\.html"):
            build_site(self.root)
        self.assertFalse(self.public.exists())
        shutil.rmtree(self.map)
        build_site(self.root)
        self.assertIn("<h2>Our cameras</h2>", self.read("cameras.html"))

    # 6. over 2 000 points: each section shows its full count and 500 rows, then "and N more on the map"
    def test_large_map_caps_rows_per_section(self):
        points = [point(0.6 + (i % 300) / 1000, 0.6 + (i // 300) / 100, url=NODE + str(100000 + i)) for i in range(1200)]
        points += [point(1.8, 1.8, url=NODE + str(200000 + i)) for i in range(900)]
        self.cameras(points)
        html = self.page()
        self.assertEqual([(name, count, len(rows)) for name, count, rows in self.sections(html)],
                         [("Cedar", 1200, 500), ("Pine", 0, 0), (GROUP, 900, 500)])
        self.assertEqual(re.findall(r"<p>and (\d+) more on the map</p>", html), ["700", "400"])
        self.assertIn(f'href="{NODE}100000"', html)
        self.assertNotIn(f'href="{NODE}100500"', html)  # rows by URL: the first 500 only

    def test_exactly_2000_points_are_all_listed(self):
        self.cameras([point(1.8, 1.8, url=NODE + str(100000 + i)) for i in range(2000)])
        html = self.page()
        self.assertEqual(self.sections(html)[-1][1:2], (2000,))
        self.assertEqual(len(ROW.findall(html)), 2000)
        self.assertNotIn("more on the map", html)

    # 7. passes the public-tree scan; no script; every external link has rel="noopener noreferrer"
    def test_public_tree_clean_no_script_external_links_rel(self):
        build_site(self.root)
        self.assertEqual(check_public_tree(self.public), [])
        html = self.read("cameras.html")
        self.assertNotIn("<script", html)
        external = re.findall(r'<a href="https?://[^"]*"([^>]*)>', html)
        self.assertTrue(external)
        self.assertTrue(all(attrs == ' rel="noopener noreferrer"' for attrs in external), external)

    # DF0137-13: map/history.json -> "Mapped points over time"
    def history(self, records, **document):
        self.write(self.map / "history.json", dict({"schema_version": 1, "records": records}, **document))

    @staticmethod
    def entry(day, total, by_city=None, **extra):
        return dict({"recorded_on": day, "osm_base": None, "source": "saved_file", "total": total, "by_city": by_city,
                     "added": total, "removed": 0}, **extra)

    def built(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in build_site(self.root)}

    def test_history_table_with_city_columns(self):
        self.history([self.entry("2026-10-01", 5, {"Pine": 2, "Cedar": 3}, osm_base="2026-10-01T06:00:00Z"),
                      self.entry("2026-11-01", 6, {"Cedar": 3, "Pine": 2, "Oak & Elm": 1}, added=2, removed=1)])
        self.built()
        html = self.read("cameras.html")
        section = re.search(r'<section class="camera-history">(.*?)</section>', html).group(1)
        self.assertIn("<h3>Mapped points over time</h3>", section)
        self.assertEqual(re.findall(r'<th scope="col">([^<]*)</th>', section),
                         ["Date", "Mapped points", "Added", "Removed", "Cedar", "Oak &amp; Elm", "Pine"])
        self.assertEqual(re.findall(r"<tr><td><time[^>]*>([^<]*)</time></td>((?:<td>[^<]*</td>)*)</tr>", section), [
            ("2026-10-01", "<td>5</td><td>5</td><td>0</td><td>3</td><td>—</td><td>2</td>"),
            ("2026-11-01", "<td>6</td><td>2</td><td>1</td><td>3</td><td>1</td><td>2</td>")])
        self.assertNotIn("<script", html)
        self.assertEqual(check_public_tree(self.public), [])
        self.assertEqual([(name, count) for name, count, _ in self.sections(html)],
                         [("Cedar", 3), ("Pine", 2), (GROUP, 1)])  # the point list is unchanged

    # 4. a record without by_city (no boundary file at that run) -> totals only
    def test_history_totals_only_when_a_record_lacks_by_city(self):
        self.history([self.entry("2026-10-01", 5), self.entry("2026-11-01", 6, {"Cedar": 3})])
        section = re.search(r'<section class="camera-history">(.*?)</section>', self.page()).group(1)
        self.assertEqual(re.findall(r'<th scope="col">([^<]*)</th>', section), ["Date", "Mapped points", "Added", "Removed"])
        self.assertEqual(len(re.findall(r"<tr>", section)), 3)  # header + 2 rows

    # 7. no history, an empty one or a single record -> every built file byte-identical
    def test_no_or_one_record_every_built_file_identical(self):
        plain = self.built()
        for records in ([], [self.entry("2026-10-01", 6)]):
            with self.subTest(records=len(records)):
                self.history(records)
                self.assertEqual([name for name, data in self.built().items() if plain.get(name) != data], [])
                self.assertEqual(sorted(self.built()), sorted(plain))
        self.history([self.entry("2026-10-01", 6), self.entry("2026-11-01", 6)])
        changed = [name for name, data in self.built().items() if plain.get(name) != data]
        self.assertEqual(changed, ["cameras.html"])

    # 5./6. a hand-edited or unsafe history.json: build refuses naming the field, content-check lists the same line
    def test_invalid_history_refused_by_build_and_content_check(self):
        from campaign_tool.content_check import content_problems
        good = [self.entry("2026-10-01", 5), self.entry("2026-11-01", 6)]
        cases = [
            ({"note": "SECRETVALUE"}, good, "map/history.json: unknown field(s) ['note']"),
            ({"schema_version": 2}, good, "map/history.json.schema_version: must be 1"),
            ({}, [dict(good[0], extra="SECRETVALUE"), good[1]], "map/history.json.records[0]: unknown field(s) ['extra']"),
            ({}, [good[1], good[0]], "map/history.json.records[1].recorded_on: dates must be strictly increasing"),
            ({}, [good[0], good[0]], "map/history.json.records[1].recorded_on: dates must be strictly increasing"),
            ({}, [dict(good[0], removed=-3)], "map/history.json.records[0].removed: must be a non-negative integer"),
            ({}, [dict(good[0], total=True)], "map/history.json.records[0].total: must be a non-negative integer"),
            ({}, [dict(good[0], added=1.5)], "map/history.json.records[0].added: must be a non-negative integer"),
            ({}, [dict(good[0], recorded_on="2026-13-01")], "map/history.json.records[0].recorded_on: must be an ISO date YYYY-MM-DD"),
            ({}, [dict(good[0], recorded_on="SECRETVALUE")], "map/history.json.records[0].recorded_on: must be an ISO date YYYY-MM-DD"),
            ({}, [dict(good[0], osm_base="2026-09-30T00:00:00Z")],
             "map/history.json.records[0].osm_base: must be null or a UTC timestamp on the recorded_on day"),
            ({}, [dict(good[0], by_city={"Cedar": -1})],
             "map/history.json.records[0].by_city: must be null or {city name: non-negative integer} (at most 500 names of plain text)"),
            ({}, [self.entry(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", 1) for i in range(121)],
             "map/history.json.records: expected a list of at most 120 records"),
        ]
        for extra, records, line in cases:
            with self.subTest(line=line):
                self.history(records, **extra)
                with self.assertRaises(ContentError) as caught:
                    build_site(self.root)
                self.assertEqual(str(caught.exception), line)
                self.assertFalse(self.public.exists())
                self.assertEqual(content_problems(self.root), [line])
                self.assertNotIn("SECRETVALUE", line)

    def test_symlinked_or_oversize_history_refused(self):
        from campaign_tool.content_check import content_problems
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps({"schema_version": 1, "records": []}), encoding="utf-8")
        link = self.map / "history.json"
        for pointed in (target, self.root / "missing.json"):  # a dangling link is refused too, never skipped
            with self.subTest(pointed=pointed.name):
                link.unlink(missing_ok=True)
                link.symlink_to(pointed)
                with self.assertRaisesRegex(ContentError, r"^map/history\.json: symlinks are not read$"):
                    build_site(self.root)
                self.assertEqual(content_problems(self.root), ["map/history.json: symlinks are not read"])
        link.unlink()
        link.write_text(json.dumps({"schema_version": 1, "records": []}) + " " * (256 * 1024), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, r"^map/history\.json: file too large \(over 256 KiB\)$"):
            build_site(self.root)
        self.assertEqual(content_problems(self.root), ["map/history.json: file too large (over 256 KiB)"])
        self.assertFalse(self.public.exists())


if __name__ == "__main__":
    unittest.main()
