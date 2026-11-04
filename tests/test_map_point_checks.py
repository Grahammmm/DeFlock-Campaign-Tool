"""DF1422-11: content/map/point-checks.json, organizer checks of single mapped points (synthetic content only).

A temp copy of the fictional example with a two-city boundary file (Cedar | Pine) and the six points of
tests/test_site_cameras_page.py. No network; nothing is written to OpenStreetMap by any code path.

Run as a script with ``--popup-fixture`` it prints the built controller and site-data.js as JSON for
tests/map-controller.test.mjs.
"""
import json
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from campaign_tool import map_controller  # noqa: E402
from campaign_tool.cli import read_config  # noqa: E402
from campaign_tool.content import ContentError, load_content, load_map  # noqa: E402
from campaign_tool.content_check import content_problems  # noqa: E402
from campaign_tool.site import Site, build_site  # noqa: E402
from tests.test_site_cameras_page import NODE, ROW, SECTION, SIX, square  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 3)
LABEL = "map/point-checks.json"
FILE_LABEL = "map/point-checks.json.checks[0]"
CEDAR_RECORD = "cafecafecafe"  # sha256[:12] of the fixture's first sources.json entry
STATUS_LINE = "must be a 12-hex library prefix or an https:// URL of at most 500 characters"


def check(node=10, status="seen_in_person", day="2026-10-30", source=CEDAR_RECORD, **extra):
    return dict({"node": node, "status": status, "checked_on": day, "source": source}, **extra)


class PointChecksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-point-checks-")
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
        self.write(self.map / "cameras.geojson", {"type": "FeatureCollection", "features": SIX})
        config = json.loads((self.map / "map-config.json").read_text(encoding="utf-8"))
        self.write(self.map / "map-config.json", dict(config, initial_point_count=len(SIX)))
        self.path = self.map / "point-checks.json"

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def checks(self, entries, **document):
        self.write(self.path, dict({"schema_version": 1, "checks": entries}, **document))

    def sources(self, extra=()):
        entries = json.loads((self.content / "sources.json").read_text(encoding="utf-8"))
        self.write(self.content / "sources.json", entries + list(extra))

    def built(self, today=TODAY):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in build_site(self.root, today)}

    def site(self, today=TODAY):
        return Site(self.root, read_config(self.root), load_content(self.root, today), today=today)

    def sections(self, html):
        return [(name, int(count), ROW_CHECK.findall(body)) for name, count, body in SECTION.findall(html)]

    def refused(self, entries, line, listed=True, **document):
        """build refuses with exactly ``line`` first; content-check lists it too (it reads the real clock, so a line
        that depends on the injected build day passes ``listed=False``); the value is never echoed."""
        self.checks(entries, **document)
        with self.assertRaises(ContentError) as caught:
            load_content(self.root, TODAY)
        self.assertEqual(str(caught.exception), line)
        if listed:
            self.assertIn(line, content_problems(self.root))
        self.assertNotIn("SECRETVALUE", line)

    # 1. one check per status: four rows with exact labels; library prefix -> sources fragment; URL escaped
    def test_one_check_per_status_four_rows(self):
        self.checks([check(10, "seen_in_person"),
                     check(20, "in_agency_record", "2026-10-29", "https://records.example.invalid/r?a=1&b=2"),
                     check(30, "not_found_on_site", "2026-10-28", "https://records.example.invalid/two"),
                     check(40, "duplicate", "2026-10-27", "beefbeefbeef")])
        self.built()
        html = (self.public / "cameras.html").read_text(encoding="utf-8")
        self.assertIn('<th scope="col">Organizer check</th>', html)
        rows = {int(re.search(r"node (\d+)</a>", cells[2]).group(1)): cells[3]
                for _, _, found in self.sections(html) for cells in found if "node" in cells[2]}
        self.assertEqual(rows[10], 'Seen in person · <time datetime="2026-10-30">2026-10-30</time> · '
                                   '<a href="/sources.html#cedar-police-records">Synthetic Cedar Police ALPR policy</a>')
        self.assertEqual(rows[20], 'Listed in an agency record · <time datetime="2026-10-29">2026-10-29</time> · '
                                   '<a href="https://records.example.invalid/r?a=1&amp;b=2" rel="noopener noreferrer">'
                                   'records.example.invalid</a>')
        self.assertEqual(rows[30], 'Not found on site · <time datetime="2026-10-28">2026-10-28</time> · '
                                   '<a href="https://records.example.invalid/two" rel="noopener noreferrer">'
                                   'records.example.invalid</a>')
        self.assertEqual(rows[40], 'Duplicate of another point · <time datetime="2026-10-27">2026-10-27</time> · '
                                   '<a href="/sources.html#cedar-police-records">'
                                   'Synthetic Cedar Police vendor agreement excerpt</a>')
        dashes = [cells[3] for _, _, found in self.sections(html) for cells in found if "node" not in cells[2]]
        self.assertEqual(dashes, ["—", "—"])  # the two points without an OSM node id have no check
        self.assertIn("<h2>Community-mapped points; ownership unconfirmed.</h2>", html)

    def test_newest_check_shown_with_count_of_earlier_ones(self):
        self.checks([check(10, "duplicate", "2026-09-01"), check(10, "seen_in_person", "2026-10-30"),
                     check(10, "in_agency_record", "2026-10-01")])
        html = self.site().cameras_html()["html"]
        self.assertIn("Seen in person · <time datetime=\"2026-10-30\">2026-10-30</time> · "
                      "<a href=\"/sources.html#cedar-police-records\">Synthetic Cedar Police ALPR policy</a> · and 2 earlier",
                      html)
        self.assertEqual(html.count("earlier"), 1)
        self.assertEqual(self.site().map["point_checks"][10][0]["checked_on"], "2026-10-30")

    # 2. limits: 6 on one node, 2001 in all, a node that is not a mapped point
    def test_more_than_five_checks_on_one_node_refused(self):
        entries = [check(10, day=f"2026-10-{day:02d}") for day in range(1, 7)]
        self.refused(entries, f"{LABEL}.checks[5].node: more than 5 checks for one point")
        self.checks(entries[:5])
        self.assertEqual(len(load_content(self.root, TODAY)["map"]["point_checks"][10]), 5)

    def test_more_than_two_thousand_checks_refused(self):
        self.refused([check(10)] * 2001, f"{LABEL}.checks: expected a list of at most 2000 checks")
        self.checks([check(10)] * 5 + [check(20)] * 5)
        self.assertEqual(sorted(load_content(self.root, TODAY)["map"]["point_checks"]), [10, 20])

    def test_unknown_node_named_by_entry_not_value(self):
        self.refused([check(10), check(99999)], f"{LABEL}.checks[1].node: not a mapped point")
        self.refused([check("10")], f"{LABEL}.checks[0].node: must be a positive integer")
        self.refused([check(True)], f"{LABEL}.checks[0].node: must be a positive integer")
        self.refused([check(5)], f"{LABEL}.checks[0].node: not a mapped point")  # 5 is a way, not a node

    # 3. dates and library prefixes
    def test_future_or_impossible_date_refused(self):
        self.refused([check(day="2026-11-04")], f"{FILE_LABEL}.checked_on: must not be after the build day", listed=False)
        self.refused([check(day="2999-01-01")], f"{FILE_LABEL}.checked_on: must not be after the build day")
        self.checks([check(day="2026-11-03")])
        self.assertEqual(len(load_content(self.root, TODAY)["map"]["point_checks"][10]), 1)  # build day itself is fine
        for bad in ("2026-02-30", "2026-13-01", "26-10-30", "SECRETVALUE", 20261030, None):
            with self.subTest(bad=bad):
                self.refused([check(day=bad)], f"{FILE_LABEL}.checked_on: must be an ISO date YYYY-MM-DD")

    def test_library_prefix_must_match_exactly_one_entry(self):
        line = f"{FILE_LABEL}.source: library prefix must match exactly one sources.json entry"
        self.refused([check(source="123412341234")], line)  # matches none
        self.sources([dict(json.loads((self.content / "sources.json").read_text(encoding="utf-8"))[0],
                           sha256=CEDAR_RECORD + "0" * 52, fragment="cedar-extra")])
        self.refused([check(source=CEDAR_RECORD)], line)  # matches two
        self.checks([check(source="beefbeefbeef")])
        self.assertEqual(load_content(self.root, TODAY)["map"]["point_checks"][10][0]["fragment"], "cedar-police-records")

    # 4. unsafe values: refused by field label, the value is never in the message
    def test_unsafe_source_note_and_keys_refused_without_the_value(self):
        self.refused([check(source="http://records.example.invalid/SECRETVALUE")], f"{FILE_LABEL}.source: {STATUS_LINE}")
        self.refused([check(source="SECRETVALUE")], f"{FILE_LABEL}.source: {STATUS_LINE}")
        self.refused([check(source="https://" + "a" * 500 + ".example.invalid/")], f"{FILE_LABEL}.source: {STATUS_LINE}")
        self.refused([check(note="call 555 123 4567 SECRETVALUE")],
                     f"{FILE_LABEL}.note: must be plain text of at most 200 characters, without an address or phone-like number")
        self.refused([check(note="write to a@b.example.invalid")],
                     f"{FILE_LABEL}.note: must be plain text of at most 200 characters, without an address or phone-like number")
        self.refused([check(note="x" * 201)],
                     f"{FILE_LABEL}.note: must be plain text of at most 200 characters, without an address or phone-like number")
        self.refused([check(status="owned_by_agency")],
                     f"{FILE_LABEL}.status: must be one of seen_in_person, in_agency_record, not_found_on_site, duplicate")
        self.refused([check(extra="SECRETVALUE")], f"{FILE_LABEL}: unknown field(s) ['extra']")
        self.refused([{"node": 10, "status": "duplicate", "checked_on": "2026-10-30"}],
                     f"{FILE_LABEL}: missing field(s) ['source']")
        self.refused([check()], f"{LABEL}.schema_version: must be 1", schema_version=2)
        self.refused([check()], f"{LABEL}: unknown field(s) ['extra']", extra="SECRETVALUE")
        userinfo_url = "https://" + ":".join(("user", "pw")) + "@records.example.invalid/SECRETVALUE"
        self.refused([check(source=userinfo_url)], f"{FILE_LABEL}.source: must be an https:// URL")

    def test_content_check_lists_every_refused_entry(self):
        self.checks([check(99999), check(status="nope"), check(), check(day="2999-01-01"), check(source="123412341234")])
        problems = [line for line in content_problems(self.root) if line.startswith(LABEL)]
        self.assertEqual(problems, sorted([
            f"{LABEL}.checks[0].node: not a mapped point",
            f"{LABEL}.checks[1].status: must be one of seen_in_person, in_agency_record, not_found_on_site, duplicate",
            f"{LABEL}.checks[3].checked_on: must not be after the build day",
            f"{LABEL}.checks[4].source: library prefix must match exactly one sources.json entry"]))
        with self.assertRaises(ContentError) as caught:  # build stops at the first of the same lines
            load_content(self.root, TODAY)
        self.assertEqual(str(caught.exception), f"{LABEL}.checks[0].node: not a mapped point")

    def test_content_check_clean_with_good_file(self):
        self.checks([check(10), check(20, "duplicate", source="https://records.example.invalid/x")])
        self.assertEqual(content_problems(self.root), [])

    def test_content_check_skips_prefix_match_when_sources_json_is_unreadable(self):
        (self.content / "sources.json").write_text("not json", encoding="utf-8")
        self.checks([check(10), check(99999)])
        problems = content_problems(self.root)
        self.assertIn(f"{LABEL}.checks[1].node: not a mapped point", problems)
        self.assertFalse([line for line in problems if "library prefix" in line], problems)

    # 5. duplicate / not_found_on_site: points, numeric counts and count sentences stay unchanged
    def test_duplicate_and_not_found_points_and_count_sentences_stay_unchanged(self):
        plain = self.built()
        self.checks([check(10, "duplicate"), check(30, "not_found_on_site"), check(20, "duplicate")])
        with_checks = self.built()
        self.assertEqual(with_checks["data/cameras.geojson"], plain["data/cameras.geojson"])  # every point still there
        sections = self.sections(with_checks["cameras.html"].decode())
        self.assertEqual([(name, count, len(rows)) for name, count, rows in sections],
                         [("Cedar", 3, 3), ("Pine", 2, 2), ("Unincorporated Cedar County (ownership unconfirmed)", 1, 1)])
        cities = json.loads(re.search(r"var CITIES=(\{.*?\});\nvar COUNTY", with_checks["site-data.js"].decode(), re.S).group(1))
        plain_cities = json.loads(re.search(r"var CITIES=(\{.*?\});\nvar COUNTY", plain["site-data.js"].decode(), re.S).group(1))
        self.assertEqual(cities, plain_cities)
        self.assertEqual(cities["cedar"]["cameraCount"], "3 community-mapped points inside city limits; ownership unconfirmed")
        self.assertEqual(cities["pine"]["cameraCount"], "2 community-mapped points inside city limits; ownership unconfirmed")
        counts, problems = self.site().mapped_counts()
        self.assertEqual((counts, problems), ({"Cedar": 3, "Pine": 2}, []))  # camera_counts itself is unchanged

    def test_count_sentence_stays_unchanged_regardless_of_newest_check(self):
        self.checks([check(10, "duplicate", "2026-09-01"), check(10, "seen_in_person", "2026-10-30"),
                     check(30, "in_agency_record"), check(20, "seen_in_person")])
        cedar = self.site().cities()["cedar"]["cameraCount"]
        self.assertEqual(cedar, "3 community-mapped points inside city limits; ownership unconfirmed")
        self.checks([check(10, "seen_in_person", "2026-09-01"), check(10, "duplicate", "2026-10-30")])
        self.assertEqual(self.site().cities()["cedar"]["cameraCount"], cedar)

    def test_an_organizer_typed_count_is_never_extended(self):
        agencies = json.loads((self.content / "agencies.json").read_text(encoding="utf-8"))
        for agency in agencies:
            if agency["agency_id"] == "ca-cedar-police":
                agency["profile"] = dict(agency.get("profile") or {}, cameraCount="TYPED")
        self.write(self.content / "agencies.json", agencies)
        self.checks([check(10, "duplicate")])
        self.assertEqual(self.site().cities()["cedar"]["cameraCount"], "TYPED")

    def test_in_agency_record_is_never_worded_as_ownership(self):
        self.checks([check(10, "in_agency_record")])
        html = self.site().cameras_html()["html"]
        self.assertIn("Listed in an agency record", html)
        self.assertNotRegex(html.lower(), r"owned by|belongs to|operated by (the )?agency")
        self.assertIn("Points are community reports from OpenStreetMap, not confirmed agency ownership.", html)

    # 6. symlink / over 512 KiB refused; no file (or no check) -> every built file identical
    def test_symlinked_or_oversize_file_refused(self):
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps({"schema_version": 1, "checks": []}), encoding="utf-8")
        for pointed in (target, self.root / "missing.json"):  # a dangling link is refused too, never skipped
            with self.subTest(pointed=pointed.name):
                self.path.unlink(missing_ok=True)
                self.path.symlink_to(pointed)
                with self.assertRaisesRegex(ContentError, rf"^{re.escape(LABEL)}: symlinks are not read$"):
                    load_content(self.root, TODAY)
                self.assertIn(f"{LABEL}: symlinks are not read", content_problems(self.root))
        self.path.unlink()
        self.path.write_text(json.dumps({"schema_version": 1, "checks": []}) + " " * (513 * 1024), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, rf"^{re.escape(LABEL)}: file too large \(over 512 KiB\)$"):
            load_content(self.root, TODAY)
        self.assertIn(f"{LABEL}: file too large (over 512 KiB)", content_problems(self.root))
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaisesRegex(ContentError, rf"^{re.escape(LABEL)}: not a regular file$"):
            load_content(self.root, TODAY)

    def test_no_file_or_no_checks_leaves_every_built_file_identical(self):
        plain = self.built()
        self.assertNotIn(b"pointChecks", plain["site-data.js"])
        self.assertNotIn(b"Organizer check", b"".join(plain.values()))
        self.assertTrue(plain["app.js"].decode().startswith(  # the controller is rendered with no popup renderer
            "// Generated by campaign_tool build. Map controller plus page bootstrap.\n"
            + map_controller.render(self.site().map["controller"], "")))
        self.checks([])
        self.assertEqual(self.built(), plain)
        self.assertEqual(load_map(self.content, today=TODAY)["point_checks"], {})
        self.assertEqual(content_problems(self.root), [])
        self.assertEqual(self.site().cameras_html()["html"].count("<th "), 3 * 3)  # three sections, three columns

    # 7. site-data.js and the popup renderer exist only with checks; only the newest status and day ship
    def test_site_data_carries_only_status_and_day_of_the_newest_check(self):
        self.checks([check(10, "duplicate", "2026-09-01", note="NOTE-SHOULD-STAY-HOME"),
                     check(10, "seen_in_person", "2026-10-30", "https://records.example.invalid/SOURCE-STAYS-HOME",
                           note="NOTE-SHOULD-STAY-HOME"), check(40, "not_found_on_site", "2026-10-02")])
        built = self.built()
        data = built["site-data.js"].decode()
        self.assertIn('var pointChecks={"10":{"status":"seen_in_person","checked_on":"2026-10-30"},'
                      '"40":{"status":"not_found_on_site","checked_on":"2026-10-02"}};', data)
        everything = b"".join(data for name, data in built.items() if name != "cameras.html").decode("utf-8", "replace")
        for private in ("NOTE-SHOULD-STAY-HOME", "SOURCE-STAYS-HOME"):
            self.assertNotIn(private, everything)
        self.assertNotIn("point-checks", everything)  # the file itself is not published
        self.assertFalse((self.public / "data" / "point-checks.json").exists())
        app = built["app.js"].decode()
        self.assertIn("Organizer check", app)
        self.assertIn("pointChecks[m[1]]", app)

    def test_cameras_page_never_prints_the_note(self):
        self.checks([check(10, note="NOTE-SHOULD-STAY-HOME")])
        self.assertNotIn("NOTE-SHOULD-STAY-HOME", self.site().cameras_html()["html"])


ROW_CHECK = re.compile(r"<tr><td>([^<]*)</td><td>([^<]*)</td><td>(.*?)</td><td>(.*?)</td></tr>")


def popup_fixture():
    """The built controller (app.js without its page bootstrap) and site-data.js for node's popup case."""
    with tempfile.TemporaryDirectory(prefix="synthetic-point-checks-popup-") as temporary:
        root = Path(temporary) / "campaign"
        shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
        map_dir = root / "content" / "map"
        (map_dir / "cameras.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": SIX}), encoding="utf-8")
        (map_dir / "point-checks.json").write_text(json.dumps({"schema_version": 1, "checks": [
            check(10, "seen_in_person", "2026-10-30"), check(20, "duplicate", "2026-10-02")]}), encoding="utf-8")
        site = Site(root, read_config(root), load_content(root, TODAY), today=TODAY)
        app = site.app_js()
        boot = 'document.addEventListener("DOMContentLoaded",function(){document.querySelectorAll(".city-choice")'
        return {"controller": app[:app.rindex(boot)], "site_data": site.site_data_js()}


if __name__ == "__main__":
    if sys.argv[1:] == ["--popup-fixture"]:
        print(json.dumps(popup_fixture()))
    else:
        unittest.main()
