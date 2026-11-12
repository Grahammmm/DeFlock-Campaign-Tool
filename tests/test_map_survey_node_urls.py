"""RF1900-2: survey-import admits a filled row's node URL without converting its untrusted digits (synthetic data)."""
import csv
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
from tests import test_map_survey as survey_base  # noqa: E402  (fixtures only; its TestCase is not imported)
from tests.test_site_cameras_page import square  # noqa: E402

PREFIX = "https://www.openstreetmap.org/node/"
SOURCE = "https://records.example.invalid/a?x=1"
GOOD = ("seen_in_person", "2026-10-30", SOURCE, "")
TODAY = date(2026, 11, 11)
UNMAPPED = "node_url: not a mapped point"
EDITED = "node_url: edited; it must be a node URL exactly as the sheet wrote it"
LIMIT_CHILD = """\
import sys
from datetime import date
from pathlib import Path
sys.set_int_max_str_digits(640)
sys.path.insert(0, sys.argv[1])
from campaign_tool import map_survey
try:
    map_survey.survey_import(Path(sys.argv[2]), Path(sys.argv[3]), today=date(2026, 11, 11))
except map_survey.SurveyError as exc:
    print("SURVEY:" + str(exc))
"""


class NodeUrlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-survey-urls-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(survey_base.EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.map = self.root / "content" / "map"
        self.dump(self.map / "city-boundaries.geojson", {"type": "FeatureCollection", "features": [
            square("Cedar", 0.5, 1.0), square("Pine", 1.0, 1.5), square("West", -1.0, -0.5)]})
        self.dump(self.map / "cameras.geojson", {"type": "FeatureCollection", "features": survey_base.CAMERAS})
        config = json.loads((self.map / "map-config.json").read_text(encoding="utf-8"))
        self.dump(self.map / "map-config.json", dict(config, initial_point_count=len(survey_base.CAMERAS)))
        self.path = self.map / "point-checks.json"
        self.csv = self.tmp / "filled.csv"

    @staticmethod
    def dump(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def write_csv(self, rows):
        """rows: (node_url, status, checked_on, source, note); the ten sheet-owned columns are left empty."""
        with self.csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(map_survey.HEADER)
            for url, *filled in rows:
                writer.writerow([url] + [""] * 9 + list(filled))

    def cli(self, apply):
        return survey_base.run("map", "survey-import", "--csv", str(self.csv), "--directory", str(self.root),
                               *(["--apply"] if apply else []))

    def api_refusal(self, apply=False):
        with self.assertRaises(map_survey.SurveyError) as caught:
            map_survey.survey_import(self.root, self.csv, apply=apply, today=TODAY)
        return str(caught.exception)

    def assert_cli_refused(self, line, apply):
        result = self.cli(apply)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn(line, result.stderr)
        for text in ("Traceback", "int_max_str_digits", "Exceeds the limit", "integer string conversion", PREFIX):
            self.assertNotIn(text, result.stderr)
        return result.stderr

    def state(self):
        stat = self.path.stat() if self.path.exists() else None
        return (self.path.read_bytes() if stat else None, stat and stat.st_mode, stat and stat.st_mtime_ns,
                sorted(p.name for p in self.map.iterdir()), self.csv.read_bytes())

    # --- the overlong cases
    def test_overlong_positive_node_is_row_scoped_unmapped_in_api_and_cli(self):
        self.write_csv([(PREFIX + "7" * 4501, *GOOD)])
        before = self.state()
        self.assertEqual(self.api_refusal(apply=True), "row 2 " + UNMAPPED)
        for apply in (False, True):
            self.assertEqual(self.assert_cli_refused("row 2 " + UNMAPPED, apply).strip(), "Stopped: row 2 " + UNMAPPED)
        self.assertEqual(self.state(), before)

    def test_long_zero_padded_and_zero_only_nodes_are_edited_not_converted(self):
        for url in (PREFIX + "0" * 4500 + "10", PREFIX + "0" * 4501, PREFIX + "010", PREFIX + "0"):
            with self.subTest(digits=len(url) - len(PREFIX)):
                self.write_csv([(url, *GOOD)])
                before = self.state()
                self.assertEqual(self.api_refusal(), "row 2 " + EDITED)
                for apply in (False, True):
                    self.assert_cli_refused("row 2 " + EDITED, apply)
                self.assertEqual(self.state(), before)
                self.assertFalse(self.path.exists())

    def test_canonical_grammar_and_membership_keep_distinct_refusals(self):
        edited = [PREFIX + "٣٠", PREFIX + "+10", PREFIX + "-10", PREFIX + "010", PREFIX + " 10", PREFIX + "10 ",
                  PREFIX + "10\n", "http://www.openstreetmap.org/node/10", "https://openstreetmap.org/node/10",
                  "https://www.openstreetmap.org/way/10", PREFIX + "10/", PREFIX + "10?a=1", PREFIX + "10#x",
                  PREFIX, PREFIX + "1e1", PREFIX + "ten", " " + PREFIX + "10"]
        unmapped = [PREFIX + "99", PREFIX + "1" + "0" * 5000, PREFIX + "9" * 4400]
        self.write_csv([(url, *GOOD) for url in edited + unmapped + [PREFIX + "10"]])
        message = self.api_refusal()
        expected = [f"row {n} {EDITED}" for n in range(2, 2 + len(edited))]
        expected += [f"row {n} {UNMAPPED}" for n in range(2 + len(edited), 2 + len(edited) + len(unmapped))]
        self.assertEqual(message, "; ".join(expected[:40]) + ("; more not shown" if len(expected) > 40 else ""))
        # the known node alone, spelled exactly, is admitted
        self.write_csv([(PREFIX + "10", *GOOD)])
        report = map_survey.survey_import(self.root, self.csv, today=TODAY)
        self.assertEqual(report["added"], 1)
        # each canonical unknown node is "unmapped", each malformed spelling "edited", one row at a time
        for url in edited:
            self.write_csv([(url, *GOOD)])
            self.assertEqual(self.api_refusal(), "row 2 " + EDITED, repr(url))
        for url in unmapped:
            self.write_csv([(url, *GOOD)])
            self.assertEqual(self.api_refusal(), "row 2 " + UNMAPPED)

    # --- continuation, blanks, write preservation
    def test_bad_first_node_keeps_later_row_diagnostics_and_cap(self):
        rows = [(PREFIX + "5" * 4501, *GOOD), (PREFIX + "10", "seen_in_person", "2026-10-30", "", ""),
                (PREFIX + "30", "", "2026-10-30", SOURCE, ""), (PREFIX + "30", "seen_in_person", "", SOURCE, "")]
        self.write_csv(rows)
        self.assertEqual(self.api_refusal(), "; ".join([
            "row 2 " + UNMAPPED, "row 3 source: required", "row 4 status: required", "row 5 checked_on: required"]))
        self.assert_cli_refused("row 2 " + UNMAPPED + "; row 3 source: required; row 4 status: required", True)
        many = [(PREFIX + "6" * 4501, *GOOD)] + [(PREFIX + "99", *GOOD)] * 44 + [(PREFIX + "10", "", "", "", "x")]
        self.write_csv(many)
        message = self.api_refusal()
        parts = message.split("; ")
        self.assertEqual(parts[:2], ["row 2 " + UNMAPPED, "row 3 " + UNMAPPED])
        self.assertEqual(parts[39], "row 41 " + UNMAPPED)
        self.assertEqual(parts[40:], ["more not shown"])
        self.assertNotIn("row 47", message)

    def test_blank_observation_skips_bad_node_but_note_only_is_checked(self):
        long_node = PREFIX + "8" * 4501
        self.write_csv([(long_node, "", "", "", ""), (PREFIX + "0" * 4501, " ", "\t", "  ", " "), ("not a url", "", "", "", "")])
        for apply in (False, True):
            result = self.cli(apply)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual((report["applied"], report["added"], report["checks"]), (apply, 0, []))
        self.assertFalse(self.path.exists())
        self.write_csv([(long_node, "", "", "", "just a note")])
        self.assertEqual(self.api_refusal(), "row 2 " + UNMAPPED)
        self.write_csv([(PREFIX + "0" * 4501, "", "", "", "just a note")])
        self.assertEqual(self.api_refusal(), "row 2 " + EDITED)

    def test_mixed_valid_and_bad_nodes_never_partially_install_checks(self):
        existing = {"schema_version": 1, "checks": [
            {"node": 10, "status": "seen_in_person", "checked_on": "2026-10-01", "source": "https://records.example.invalid/old"}]}
        bad_rows = ([(PREFIX + "10", *GOOD), (PREFIX + "4" * 4501, *GOOD)],
                    [(PREFIX + "0" * 4501, *GOOD), (PREFIX + "30", *GOOD)],
                    [(PREFIX + "30", *GOOD), (PREFIX + "9" * 4501, *GOOD), (PREFIX + "10", *GOOD)])
        for present in (False, True):
            if present:
                self.dump(self.path, existing)
                os.utime(self.path, (1_700_000_000, 1_700_000_000))
            for rows in bad_rows:
                self.write_csv(rows)
                before = self.state()
                for apply in (False, True):
                    with self.subTest(present=present, apply=apply, first=rows[0][0][-3:]):
                        self.api_refusal(apply=apply)
                        result = self.cli(apply)
                        self.assertEqual((result.returncode, result.stdout), (1, ""))
                        self.assertEqual(self.state(), before)
        self.assertEqual([p.name for p in self.map.iterdir() if p.name.startswith(".")], [])

    # --- unchanged healthy behaviour
    def test_valid_roundtrip_duplicate_and_preview_bytes_are_unchanged(self):
        sheet = survey_base.run("map", "survey-sheet", "--city", "Cedar", "--directory", str(self.root))
        self.assertEqual(sheet.returncode, 0, sheet.stderr)
        generated = next((self.root / "kit" / "survey").glob("cedar-*.csv"))
        with generated.open(encoding="utf-8", newline="") as handle:
            table = list(csv.reader(handle))
        self.assertEqual([row[0] for row in table[1:]], [PREFIX + "10", PREFIX + "30"])
        table[1][10:14] = ["seen_in_person", "2026-10-30", SOURCE, "café – ok"]
        table[2][10:14] = ["not_found_on_site", "2026-10-29", "cafecafecafe", ""]
        with self.csv.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(table)
        expected_checks = [{"node": 10, "status": "seen_in_person", "checked_on": "2026-10-30", "source": SOURCE, "note": "café – ok"},
                           {"node": 30, "status": "not_found_on_site", "checked_on": "2026-10-29", "source": "cafecafecafe"}]
        expected_bytes = (
            '{\n  "schema_version": 1,\n  "checks": [\n    {\n      "node": 10,\n      "status": "seen_in_person",\n'
            '      "checked_on": "2026-10-30",\n      "source": "https://records.example.invalid/a?x=1",\n'
            '      "note": "café – ok"\n    },\n    {\n      "node": 30,\n      "status": "not_found_on_site",\n'
            '      "checked_on": "2026-10-29",\n      "source": "cafecafecafe"\n    }\n  ]\n}\n').encode("utf-8")
        dry = self.cli(False)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(json.loads(dry.stdout), {
            "applied": False, "added": 2, "checks": expected_checks,
            "note": "Dry run: nothing was written. Rerun with --apply; the checks reach the site at the next build."})
        self.assertFalse(self.path.exists())
        applied = self.cli(True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout)["note"],
                         "Checks written to content/map/point-checks.json; nothing is published until the next build.")
        self.assertEqual(self.path.read_bytes(), expected_bytes)
        again = self.cli(True)
        self.assertEqual(again.returncode, 1)
        self.assertEqual(again.stderr.strip(), "Stopped: row 2: duplicate of an existing check; row 3: duplicate of an existing check")
        self.assertEqual(self.path.read_bytes(), expected_bytes)
        loaded = survey_base.load_map(self.root / "content")["point_checks"]
        self.assertEqual(loaded[10][0]["note"], "café – ok")

    def test_digit_limit_independence_is_local_to_untrusted_csv_admission(self):
        child = self.tmp / "limit_child.py"
        child.write_text(LIMIT_CHILD, encoding="utf-8")
        self.write_csv([(PREFIX + "3" * 1000, *GOOD), (PREFIX + "0" * 1000 + "10", *GOOD), (PREFIX + "10", *GOOD)])
        result = subprocess.run([sys.executable, "-B", str(child), str(REPO_ROOT), str(self.root), str(self.csv)],
                                capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"SURVEY:row 2 {UNMAPPED}; row 3 {EDITED}")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
