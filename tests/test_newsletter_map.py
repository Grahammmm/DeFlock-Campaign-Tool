"""DF1422-16: newsletter --from-content "Camera map" section from content/map/history.json. Synthetic; nothing is sent."""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.draft import MAX_MEETINGS, DraftError, build_draft  # noqa: E402
from campaign_tool.newsletter.from_content import manifest_from_content  # noqa: E402
from tests.test_newsletter import MANIFEST, NOW as WORKER_NOW  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
SINCE = "2026-10-01T00:00:00+00:00"
CFG = {"name": "Synthetic Campaign", "county": "Synthetic"}
BASE = "https://cedar-records.example.invalid"
HEADINGS = ("## Camera map", "<h3>Camera map</h3>")
LINK = f" [Cameras page]({BASE}/cameras.html)"
LABELLED = "community-mapped points (ownership unconfirmed)"


def record(day, total, added=0, removed=0, by_city=None, osm_base="same"):  # osm_base: "same" = the day, else str or None
    if osm_base == "same":
        osm_base = day + "T03:04:05Z"
    return {"recorded_on": day, "osm_base": osm_base, "source": "overpass", "total": total, "by_city": by_city,
            "added": added, "removed": removed}


def manifest(history, **changes):
    base = {"schema_version": 1, "campaign": {"name": "Synthetic Campaign", "base_url": BASE, "county_name": "Synthetic"},
            "since": SINCE, "findings": [], "meetings": [],
            "map": {"records": history, "label": "Unincorporated area (ownership unconfirmed)", "path": "/cameras.html"}}
    return {**base, **changes}


def section(text):
    return text.split("## Camera map\n", 1)[1].split("\n\n", 1)[0].splitlines()


OLD = record("2026-09-20", 100, 100, 0, {"Cedar": 60, "Elm": 40})
NEW = record("2026-10-15", 106, 8, 2, {"Cedar": 64, "Elm": 40, "Oak": 2})


class CameraMapSectionTests(unittest.TestCase):
    def test_two_records_newest_in_window_gives_totals_since_and_city_lines_in_html_and_text(self):
        draft = build_draft(manifest([OLD, NEW]), now=NOW)
        expected = [f"- 106 {LABELLED}, OpenStreetMap data of 2026-10-15.{LINK}",
                    "- Since 2026-09-20: 8 added, 2 removed.",
                    "- Cedar: 60 → 64", "- Oak: 0 → 2"]
        self.assertEqual(section(draft["text"]), expected)
        for item in ("<li>106 community-mapped points (ownership unconfirmed), OpenStreetMap data of 2026-10-15. "
                     f'<a href="{BASE}/cameras.html" rel="noopener noreferrer">Cameras page</a></li>',
                     "<li>Since 2026-09-20: 8 added, 2 removed.</li>", "<li>Cedar: 60 → 64</li>", "<li>Oak: 0 → 2</li>"):
            self.assertIn(item, draft["html"])
        self.assertIn(HEADINGS[1], draft["html"])
        self.assertEqual(draft["counts"]["map"], 1)
        self.assertNotIn("Elm", draft["text"])  # unchanged city

    def test_records_given_out_of_order_use_the_newest_and_the_one_before_it(self):
        self.assertEqual(build_draft(manifest([NEW, OLD]), now=NOW), build_draft(manifest([OLD, NEW]), now=NOW))

    def test_osm_data_date_comes_from_osm_base_else_the_recorded_day(self):
        draft = build_draft(manifest([record("2026-10-15", 5, 5, 0, None, osm_base="2026-10-15T23:59:59Z")]), now=NOW)
        self.assertIn("OpenStreetMap data of 2026-10-15.", draft["text"])
        draft = build_draft(manifest([record("2026-10-16", 5, 5, 0, None, osm_base=None)]), now=NOW)
        self.assertIn("OpenStreetMap data of 2026-10-16.", draft["text"])

    def test_only_one_record_ever_in_window_gives_the_totals_line_only(self):
        draft = build_draft(manifest([record("2026-10-15", 7, 7, 0, {"Cedar": 7})]), now=NOW)
        self.assertEqual(section(draft["text"]), [f"- 7 {LABELLED}, OpenStreetMap data of 2026-10-15.{LINK}"])
        self.assertNotIn("Since", draft["text"])

    def test_newest_before_since_is_no_section_and_a_record_after_now_is_ignored(self):
        plain = build_draft({k: v for k, v in manifest([]).items() if k != "map"}, now=NOW)
        self.assertEqual(build_draft(manifest([OLD]), now=NOW), plain)
        future = record("2026-10-31", 500, 394, 0, {"Cedar": 500})
        self.assertEqual(build_draft(manifest([OLD, future]), now=NOW), plain)
        # a future record does not replace the in-window one, which is still compared with the record before it
        both = build_draft(manifest([OLD, NEW, future]), now=NOW)
        self.assertEqual(both, build_draft(manifest([OLD, NEW]), now=NOW))
        # window edges: the since day and now day are inside
        for day in ("2026-10-01", "2026-10-30"):
            self.assertIn("## Camera map", build_draft(manifest([record(day, 3, 3)]), now=NOW)["text"])
        self.assertNotIn("## Camera map", build_draft(manifest([record("2026-09-30", 3, 3)]), now=NOW)["text"])
        self.assertIn("## Camera map", build_draft(manifest([record("2020-01-01", 3, 3)], since=None), now=NOW)["text"])

    def test_in_window_record_with_no_change_is_no_section_and_a_byte_identical_draft(self):
        quiet = record("2026-10-15", 100, 0, 0, {"Cedar": 60, "Elm": 40})
        base = manifest([OLD, quiet], findings=copy.deepcopy(MANIFEST["findings"]), meetings=copy.deepcopy(MANIFEST["meetings"]))
        without = {k: v for k, v in base.items() if k != "map"}
        draft = build_draft(base, now=NOW)
        self.assertEqual(draft, build_draft(without, now=NOW))
        self.assertNotIn("map", draft["counts"])
        self.assertNotIn("Camera map", draft["text"] + draft["html"])
        # both by_city null and nothing added or removed is also no change
        nulls = manifest([record("2026-09-20", 9, 9), record("2026-10-15", 9)])
        self.assertEqual(build_draft(nulls, now=NOW), build_draft({k: v for k, v in nulls.items() if k != "map"}, now=NOW))
        # a changed city with added == removed == 0 still counts as a change
        moved = record("2026-10-15", 100, 0, 0, {"Cedar": 59, "Elm": 41})
        self.assertEqual(section(build_draft(manifest([OLD, moved]), now=NOW)["text"])[2:], ["- Cedar: 60 → 59", "- Elm: 40 → 41"])

    def test_by_city_null_on_either_record_gives_totals_and_since_only(self):
        for old, new in ((record("2026-09-20", 100, 100, 0, None), NEW), (OLD, record("2026-10-15", 106, 8, 2, None))):
            lines = section(build_draft(manifest([old, new]), now=NOW)["text"])
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[1].startswith("- Since 2026-09-20:"))

    def test_ten_changed_cities_show_eight_lines_then_and_two_more_on_the_cameras_page(self):
        names = [f"City {n:02d}" for n in range(10)]
        old = record("2026-09-20", 10, 10, 0, {name: 1 for name in names})
        new = record("2026-10-15", 20, 10, 0, {name: 2 for name in names})
        draft = build_draft(manifest([old, new]), now=NOW)
        lines = section(draft["text"])[2:]
        self.assertEqual(MAX_MEETINGS, 8)
        self.assertEqual(lines, [f"- {name}: 1 → 2" for name in names[:8]] + ["- and 2 more on the cameras page."])
        self.assertNotIn("City 08", draft["html"])

    def test_link_only_with_base_url_never_a_relative_path(self):
        bare = build_draft(manifest([OLD, NEW], campaign={"name": "Synthetic Campaign", "base_url": None, "county_name": "S"}), now=NOW)
        for blob in (bare["text"], bare["html"]):
            self.assertIn("106 community-mapped points", blob)
            self.assertNotIn("Cameras page", blob)
            self.assertNotIn("cameras.html", blob)
        self.assertIn(LINK, build_draft(manifest([OLD, NEW]), now=NOW)["text"])

    def test_wording_never_claims_installed_or_names_an_owner(self):
        draft = build_draft(manifest([OLD, NEW]), now=NOW)
        lowered = (draft["text"] + draft["html"]).lower()
        self.assertNotIn("installed", lowered)
        self.assertNotIn("operated by", lowered)
        self.assertNotIn("owned by", lowered)
        self.assertIn("ownership unconfirmed", lowered)

    def test_section_sits_after_the_library_and_before_decisions(self):
        records = [{"title": "Doc", "agency": "Cedar PD", "received_at": "2026-10-10T00:00:00+00:00", "prefix": "abcdefabcdef",
                    "public_url": None, "path": "/sources.html#cedar"}]
        decisions = [{"body": "Synthetic Board", "on": "2026-10-06", "result": "denied", "vote": None,
                      "source_url": "https://records.example.invalid/minutes/1", "meeting_path": "/meetings/x.html"}]
        draft = build_draft(manifest([OLD, NEW], records=records, decisions=decisions), now=NOW)
        text = draft["text"]
        self.assertLess(text.index("## New in the records library"), text.index("## Camera map"))
        self.assertLess(text.index("## Camera map"), text.index("## Decisions"))
        self.assertEqual(draft["counts"], {"findings": 0, "meetings": 0, "records": 1, "map": 1, "decisions": 1})

    def test_worker_manifest_without_map_is_byte_identical_and_bad_map_is_refused(self):
        before = build_draft(MANIFEST, now=WORKER_NOW)
        self.assertEqual(build_draft({**MANIFEST, "map": None}, now=WORKER_NOW), before)
        for bad in ([], "text", 3, {}, {"records": "x"}, {"records": {}}):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(DraftError, r"map"):
                    build_draft({**MANIFEST, "map": bad}, now=WORKER_NOW)
        for change, expected in (({"total": -1}, r"map\.records\[0\]\.total"), ({"added": "3"}, r"map\.records\[0\]\.added"),
                                 ({"removed": True}, r"map\.records\[0\]\.removed"), ({"recorded_on": "10/15/2026"}, r"recorded_on"),
                                 ({"by_city": [1]}, r"by_city"), ({"by_city": {"Cedar": -1}}, r"by_city"),
                                 ({"by_city": {"a@b.example.invalid": 1}}, "email")):
            with self.subTest(change=change):
                with self.assertRaisesRegex(DraftError, expected):
                    build_draft(manifest([{**NEW, **change}]), now=NOW)
        with self.assertRaisesRegex(DraftError, r"map\.records\[0\]: must be an object"):
            build_draft(manifest(["x"]), now=NOW)
        # an out-of-window record is validated too
        with self.assertRaisesRegex(DraftError, r"map\.records\[0\]\.total"):
            build_draft(manifest([{**OLD, "total": -5}, NEW]), now=NOW)


class CameraMapFromContentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-map-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.path = self.root / "content" / "map" / "history.json"

    def write(self, records):
        self.path.write_text(json.dumps({"schema_version": 1, "records": records}))

    def draft(self, since=SINCE):
        value = manifest_from_content(self.root, CFG, since, now=NOW)
        return value, build_draft(value, now=NOW)

    def test_history_becomes_the_map_key_and_the_section(self):
        self.write([OLD, NEW])
        value, draft = self.draft()
        self.assertEqual(set(value["map"]), {"records", "label", "path"})
        self.assertEqual(value["map"]["records"], [OLD, NEW])
        self.assertEqual(value["map"]["path"], "/cameras.html")
        self.assertIn("(ownership unconfirmed)", value["map"]["label"])
        self.assertEqual(section(draft["text"])[1], "- Since 2026-09-20: 8 added, 2 removed.")
        self.assertEqual(draft["counts"]["map"], 1)

    def test_no_history_file_or_no_record_means_no_map_key_and_an_unchanged_draft(self):
        value, draft = self.draft()
        self.assertNotIn("map", value)
        self.assertNotIn("Camera map", draft["text"])
        self.write([])
        again, same = self.draft()
        self.assertNotIn("map", again)
        self.assertEqual(same, draft)

    def test_history_outside_the_window_or_without_change_leaves_the_draft_as_it_was(self):
        self.write([OLD])
        for since in (SINCE, "2026-10-25T00:00:00+00:00"):
            value, draft = self.draft(since)
            self.assertIn("map", value)
            self.assertEqual(draft, build_draft({k: v for k, v in value.items() if k != "map"}, now=NOW))
            self.assertNotIn("Camera map", draft["text"] + draft["html"])
        self.write([OLD, record("2026-10-15", 100, 0, 0, {"Cedar": 60, "Elm": 40})])
        value, quiet = self.draft()
        self.assertEqual(quiet, build_draft({k: v for k, v in value.items() if k != "map"}, now=NOW))


if __name__ == "__main__":
    unittest.main()
