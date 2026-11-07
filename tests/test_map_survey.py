"""DF0237-13: map survey-sheet / survey-import on a temp copy of the fictional campaign (synthetic data, no network)."""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from campaign_tool import map_survey  # noqa: E402
from campaign_tool.content import load_map  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402
from tests.test_site_cameras_page import NODE, point, square  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
CANARY = "SECRETVALUE"
CAMERAS = [point(0.6, 0.6, manufacturer="=Evil,Co", operator="Cedar PD", url=NODE + "30"),
           point(0.7, 1.0, manufacturer="Smith & Co", operator="Cedar \"PD\" & Co", url=NODE + "10"),
           point(1.2, 0.8, operator="Pine PD", url=NODE + "20"),
           point(-0.7, 1.0, manufacturer="West Co", operator=None, url=NODE + "50")]


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


class SurveyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-survey-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.map = self.root / "content" / "map"
        self.write(self.map / "city-boundaries.geojson", {"type": "FeatureCollection", "features": [
            square("Cedar", 0.5, 1.0), square("Pine", 1.0, 1.5), square("West", -1.0, -0.5)]})
        self.write(self.map / "cameras.geojson", {"type": "FeatureCollection", "features": CAMERAS})
        config = json.loads((self.map / "map-config.json").read_text(encoding="utf-8"))
        self.write(self.map / "map-config.json", dict(config, initial_point_count=len(CAMERAS)))
        self.path = self.map / "point-checks.json"
        self.csv = Path(temporary.name) / "filled.csv"

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def sheet(self, city="Cedar"):
        return run("map", "survey-sheet", "--city", city, "--directory", str(self.root))

    def sheet_file(self, city="Cedar", suffix="csv"):
        """The one dated sheet for city, found by name: a run crossing midnight must not look for another day's."""
        folder = self.root / "kit" / "survey"
        found = sorted(folder.glob(f"{map_survey.stem(city)}-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].{suffix}"))
        self.assertLessEqual(len(found), 1, found)
        return found[0] if found else None

    def survey_rows(self, city="Cedar"):
        if self.sheet_file(city) is None:  # one sheet per city and day, never overwritten
            result = self.sheet(city)
            self.assertEqual(result.returncode, 0, result.stderr)
        raw = self.sheet_file(city).read_bytes()
        return list(csv.reader(io.StringIO(raw.decode("utf-8"), newline="")))

    def fill(self, rows):
        """Write the sheet CSV with the given {node: (status, checked_on, source, note)} filled in."""
        table = self.survey_rows()
        for row in table[1:]:
            node = row[0].rsplit("/", 1)[1]
            if node in rows:
                row[10:14] = rows[node]
        with self.csv.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(table)

    def importer(self, apply=False):
        args = ["map", "survey-import", "--csv", str(self.csv), "--directory", str(self.root)]
        return run(*args, *(["--apply"] if apply else []))

    def good(self, **extra):
        return dict({"10": ("seen_in_person", "2026-10-30", "https://records.example.invalid/a?x=1", "")}, **extra)

    def refused(self, rows, line, apply=True, before=None):
        self.fill(rows)
        before = self.path.read_bytes() if self.path.exists() else None
        result = self.importer(apply)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(line, result.stderr)
        for text in (CANARY, "records.example.invalid"):
            self.assertNotIn(text, result.stderr + result.stdout)
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)

    # --- survey-sheet
    def test_sheet_rows_csv_html_and_privacy(self):
        table = self.survey_rows()
        self.assertEqual(tuple(table[0]), map_survey.HEADER)
        self.assertEqual([row[0] for row in table[1:]], [NODE + "10", NODE + "30"])  # sorted; Pine and West excluded
        self.assertEqual(table[2][1:6], ["0.6", "0.6", "'=Evil,Co", "Cedar PD", ""])  # lat, lon, formula guard
        self.assertTrue(all(row[10:] == ["", "", "", ""] for row in table[1:]))
        folder = self.root / "kit" / "survey"
        raw = self.sheet_file().read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)
        page = self.sheet_file(suffix="html").read_text(encoding="utf-8")
        self.assertIn("Camera field survey: Cedar", page)
        self.assertIn("Cedar &quot;PD&quot; &amp; Co", page)
        self.assertNotIn("\"PD\" & Co", page)
        self.assertNotIn("<script", page)
        self.assertIn("Supply your own source for every new check.", page)
        self.assertEqual(oct((self.root / "kit").stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(folder.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(self.sheet_file().stat().st_mode & 0o777), "0o600")
        self.assertFalse((self.root / "public").exists())

    def test_negative_longitude_is_guarded_and_zero_points_is_a_sheet(self):
        table = self.survey_rows("West")
        self.assertEqual(table[1][2], "'-0.7")
        self.write(self.map / "city-boundaries.geojson", {"type": "FeatureCollection", "features": [square("Empty", 5.0, 6.0)]})
        table = self.survey_rows("Empty")
        self.assertEqual(len(table), 1)
        self.assertIn("No mapped points inside this city boundary.",
                      self.sheet_file("Empty", "html").read_text(encoding="utf-8"))

    def test_sheet_refusals_write_nothing(self):
        for city in ("Nowhere", "cedar"):
            result = self.sheet(city)
            self.assertEqual(result.returncode, 1)
            self.assertIn("--city: unknown city boundary name", result.stderr)
        self.assertFalse((self.root / "kit").exists())
        self.assertEqual(self.survey_rows()[0][0], "node_url")
        again = self.sheet()
        self.assertEqual(again.returncode, 1)
        self.assertIn("exists; nothing was overwritten", again.stderr)

    def test_sheet_refuses_a_symlinked_kit_and_a_non_node_point(self):
        outside = self.root.parent / "elsewhere"
        outside.mkdir()
        (self.root / "kit").symlink_to(outside)
        self.assertEqual(self.sheet().returncode, 1)
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / "kit").unlink()
        self.write(self.map / "cameras.geojson", {"type": "FeatureCollection", "features": [
            point(0.6, 0.6, url="https://www.openstreetmap.org/way/5")]})
        result = self.sheet()
        self.assertEqual(result.returncode, 1)
        self.assertIn("features[0].url", result.stderr)
        self.assertFalse((self.root / "kit").exists())

    def test_sheet_shows_latest_check_but_leaves_new_fields_blank(self):
        self.write(self.path, {"schema_version": 1, "checks": [
            {"node": 10, "status": "seen_in_person", "checked_on": "2026-10-01", "source": "cafecafecafe", "note": "older"},
            {"node": 10, "status": "duplicate", "checked_on": "2026-10-20", "source": "https://records.example.invalid/n"}]})
        row = self.survey_rows()[1]
        self.assertEqual(row[6:10], ["duplicate", "2026-10-20", "https://records.example.invalid/n", ""])
        self.assertEqual(row[10:], ["", "", "", ""])

    # --- survey-import
    def test_blank_sheet_reimported_adds_nothing(self):
        self.fill({})
        for apply in (False, True):
            result = self.importer(apply)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["added"], 0)
        self.assertFalse(self.path.exists())

    def test_filled_sheet_dry_run_then_apply_then_build_reads_back(self):
        existing = {"node": 10, "status": "seen_in_person", "checked_on": "2026-10-01", "source": "cafecafecafe"}
        self.write(self.path, {"schema_version": 1, "checks": [existing]})
        before = self.path.read_bytes()
        self.fill(self.good(**{"30": ("not_found_on_site", "2026-10-29", "cafecafecafe", "gone, ok")}))
        dry = self.importer()
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(json.loads(dry.stdout)["added"], 2)
        self.assertEqual(self.path.read_bytes(), before)  # dry run writes nothing
        applied = self.importer(apply=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["checks"][0], existing)
        self.assertEqual(document["checks"][1:], [
            {"node": 10, "status": "seen_in_person", "checked_on": "2026-10-30", "source": "https://records.example.invalid/a?x=1"},
            {"node": 30, "status": "not_found_on_site", "checked_on": "2026-10-29", "source": "cafecafecafe", "note": "gone, ok"}])
        self.assertEqual([p.name for p in self.map.iterdir() if p.name.startswith(".")], [])
        checks = load_map(self.root / "content")["point_checks"]
        self.assertEqual([c["checked_on"] for c in checks[10]], ["2026-10-30", "2026-10-01"])
        self.assertEqual(checks[30][0]["note"], "gone, ok")
        built = {p.name: p.read_text(encoding="utf-8") for p in build_site(self.root, date.today()) if p.name == "cameras.html"}
        self.assertIn("Not found on site", built["cameras.html"])
        again = self.importer(apply=True)  # the same sheet again: every row now duplicates an existing check
        self.assertEqual(again.returncode, 1)
        self.assertIn("row ", again.stderr)
        self.assertIn("duplicate of an existing check", again.stderr)

    def test_source_is_required_and_never_inferred(self):
        self.write(self.path, {"schema_version": 1, "checks": [
            {"node": 10, "status": "seen_in_person", "checked_on": "2026-10-01", "source": "https://records.example.invalid/old"}]})
        self.refused({"10": ("seen_in_person", "2026-10-30", "", "")}, "row 2 source: required")
        self.refused({"10": ("seen_in_person", "2026-10-30", "", CANARY)}, "row 2 source: required")

    def test_bad_sources_are_refused_by_row_and_field(self):
        for bad in ("http://records.example.invalid/a", "https://user" + ":pw@records.example.invalid/a",
                    "aaaaaaaaaaaa", "https://records.example.invalid/" + "x" * 500, "not a source"):
            with self.subTest(bad=bad[:30]):
                self.refused(self.good(**{"10": ("seen_in_person", "2026-10-30", bad, "")}), "row 2 source:")

    def test_other_invalid_rows_are_named_by_row_and_field(self):
        ok = "https://records.example.invalid/a"
        self.refused({"10": ("", "2026-10-30", ok, "")}, "row 2 status: required")
        self.refused({"10": ("seen_in_person", "", ok, "")}, "row 2 checked_on: required")
        self.refused({"10": ("looked", "2026-10-30", ok, "")}, "row 2 status:")
        self.refused({"10": ("seen_in_person", "2999-01-01", ok, "")}, "row 2 checked_on:")
        self.refused({"10": ("seen_in_person", "2026-10-30", ok, "call 555-123-4567 " + CANARY)}, "row 2 note:")

    def test_edited_or_unknown_node_and_header_and_duplicate_are_refused(self):
        ok = "https://records.example.invalid/a"
        self.fill({"10": ("seen_in_person", "2026-10-30", ok, "")})
        text = self.csv.read_text(encoding="utf-8")
        for edited, line in ((NODE + "10/", "row 2 node_url: edited"), (NODE + "99", "row 2 node_url: not a mapped point"),
                             ("https://www.openstreetmap.org/way/10", "row 2 node_url: edited")):
            self.csv.write_text(text.replace(NODE + "10,", edited + ","), encoding="utf-8")
            result = self.importer(apply=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn(line, result.stderr)
        self.csv.write_text(text.replace("node_url", "node"), encoding="utf-8")
        self.assertIn("header row does not match", self.importer(apply=True).stderr)
        self.write(self.path, {"schema_version": 1, "checks": [
            {"node": 10, "status": "seen_in_person", "checked_on": "2026-10-30", "source": ok}]})
        self.refused({"10": ("seen_in_person", "2026-10-30", ok, "")}, "row 2: duplicate of an existing check")
        self.write(self.path, {"schema_version": 1, "checks": [
            {"node": 10, "status": "duplicate", "checked_on": f"2026-10-0{n}", "source": ok} for n in range(1, 6)]})
        self.refused({"10": ("seen_in_person", "2026-10-30", ok, "")}, "row 2 node: more than 5 checks for one point")

    def test_one_bad_row_refuses_the_whole_import(self):
        ok = "https://records.example.invalid/a"
        self.refused({"10": ("seen_in_person", "2026-10-30", ok, ""), "30": ("seen_in_person", "2026-10-30", "", "")},
                     "row 3 source: required")
        self.assertFalse(self.path.exists())

    def test_size_limit_is_checked_on_the_bytes_that_would_be_installed(self):
        from unittest import mock
        from campaign_tool import content
        self.fill(self.good())
        entry = {"node": 10, "status": "seen_in_person", "checked_on": "2026-10-30",
                 "source": "https://records.example.invalid/a?x=1"}
        merged = {"schema_version": 1, "checks": [entry]}
        compact, installed = len(json.dumps(merged)), len(map_survey._serialize(merged))
        self.assertGreater(installed, compact)
        # Between the compact and the indented size: refused, nothing written.
        with mock.patch.object(content, "MAX_POINT_CHECKS_BYTES", (compact + installed) // 2):
            with self.assertRaises(map_survey.SurveyError):
                map_survey.survey_import(self.root, self.csv, apply=True)
        self.assertFalse(self.path.exists())
        # Exactly at the limit: applied, and the written file is within it and reads back through the build loader.
        with mock.patch.object(content, "MAX_POINT_CHECKS_BYTES", installed):
            report = map_survey.survey_import(self.root, self.csv, apply=True)
            self.assertTrue(report["applied"])
            self.assertEqual(self.path.stat().st_size, installed)
            self.assertEqual(map_survey._existing(self.root)[1], merged)
            self.assertEqual(load_map(self.root / "content")["point_checks"][10][0]["source"], entry["source"])


    def test_apply_refuses_a_symlinked_check_file_and_campaign_flag_excludes_directory(self):
        self.fill(self.good())
        target = self.root.parent / "other.json"
        target.write_text('{"schema_version": 1, "checks": []}', encoding="utf-8")
        self.path.symlink_to(target)
        result = self.importer(apply=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"schema_version": 1, "checks": []}')
        both = run("map", "survey-sheet", "--city", "Cedar", "--directory", str(self.root), "--campaign", "x")
        self.assertEqual(both.returncode, 2)
        self.assertEqual(run("map", "survey-sheet", "--help").returncode, 0)


if __name__ == "__main__":
    unittest.main()
