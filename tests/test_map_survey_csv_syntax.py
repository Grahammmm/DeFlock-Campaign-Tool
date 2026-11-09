"""RF0844-3: malformed CSV quoting is refused before any survey point check is imported (synthetic data, no network)."""
import csv
import io
import json
import shutil
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
from tests.test_map_survey import CAMERAS, EXAMPLE, NODE, run, square  # noqa: E402

GENERIC = "--csv: not valid CSV"
CANARY = "SYNTHCANARYVALUE"
SOURCE = "https://records.example.invalid/a?x=1"


def crlf(*lines):
    return "\r\n".join(lines)


class SurveyCsvSyntaxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-survey-syntax-")
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
        self.csv = Path(temporary.name) / "survey.csv"

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def sheet_file(self):
        """The one generated sheet (one per city and day, never overwritten), made on first use."""
        folder = self.root / "kit" / "survey"
        if not list(folder.glob("*.csv")):
            result = run("map", "survey-sheet", "--city", "Cedar", "--directory", str(self.root))
            self.assertEqual(result.returncode, 0, result.stderr)
        return next(folder.glob("*.csv"))

    def sheet_rows(self):
        raw = self.sheet_file().read_bytes()
        return list(csv.reader(io.StringIO(raw.decode("utf-8"), newline="")))

    def encode(self, cells):
        out = io.StringIO()
        csv.writer(out, lineterminator="").writerow(cells)
        return out.getvalue()

    def lines(self, node10="", node30=None):
        """Header + node 10 filled + node 30's row; node10/node30 are RAW final-note text (possibly malformed)."""
        table = self.sheet_rows()
        rows = {row[0].rsplit("/", 1)[1]: row for row in table[1:]}
        first = self.encode(rows["10"][:10] + ["seen_in_person", "2026-10-30", SOURCE]) + "," + node10
        out = [self.encode(table[0]), first]
        if node30 is not None:
            out.append(self.encode(rows["30"][:10] + ["not_found_on_site", "2026-10-29", SOURCE]) + "," + node30)
        return out

    def put(self, text):
        self.csv.write_bytes(text.encode("utf-8"))

    def listing(self):
        return sorted((p.name, p.stat().st_mode, p.read_bytes() if p.is_file() else None) for p in self.map.iterdir())

    def assert_generic(self, text):
        self.put(text)
        with self.assertRaises(map_survey.SurveyError) as caught:
            map_survey._read_sheet(self.csv)
        self.assertEqual(str(caught.exception), GENERIC)
        self.assertIsNone(caught.exception.__cause__)

    def test_unterminated_quoted_final_field_is_refused(self):
        for tail in ("\r\n", ""):
            with self.subTest(newline=bool(tail)):
                self.assert_generic(crlf(*self.lines(node30='"observed')) + tail)

    def test_junk_after_closing_quote_is_refused(self):
        self.assert_generic(crlf(*self.lines(node30='"obser"ved')))
        self.assert_generic(crlf(*self.lines(node10='"obser"ved', node30='"fine"')) + "\r\n")

    def test_late_malformed_record_refuses_entire_import(self):
        self.put(crlf(*self.lines(node30='"obser"ved')) + "\r\n")
        for existing in (False, True):
            if existing:
                self.write(self.path, {"schema_version": 1, "checks": []})
                self.path.chmod(0o640)
            before_csv = (self.csv.read_bytes(), self.csv.stat().st_mode)
            before = self.listing()
            for apply in (False, True):
                with self.subTest(existing=existing, apply=apply):
                    result = run("map", "survey-import", "--csv", str(self.csv), "--directory", str(self.root),
                                 *(["--apply"] if apply else []))
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(GENERIC, result.stderr)
                    self.assertEqual(self.listing(), before)  # target bytes/modes and directory entries untouched
                    self.assertEqual((self.csv.read_bytes(), self.csv.stat().st_mode), before_csv)
        self.assertTrue(self.path.exists())

    def test_cli_csv_syntax_refusal_does_not_leak_values(self):
        self.put(crlf(*self.lines(node30=f'"{CANARY}"junk')) + "\r\n")
        result = run("map", "survey-import", "--csv", str(self.csv), "--directory", str(self.root), "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn(GENERIC, result.stderr)
        for text in (CANARY, "records.example.invalid", str(self.csv), "Traceback"):
            self.assertNotIn(text, result.stdout + result.stderr)

    def test_valid_quoted_fields_and_eof_round_trip(self):
        cases = {"commas": "a, b, c", "doubled quotes": 'say "hi"', "quoted multiline": "line one\nline two",
                 "unicode": "caf\u00e9 \u2713"}
        for label, note in cases.items():
            for encoding, newline, final in (("utf-8", "\r\n", ""), ("utf-8-sig", "\n", "\n"), ("utf-8", "\n", "")):
                with self.subTest(label=label, encoding=encoding, newline=repr(newline), final=bool(final)):
                    table = self.sheet_rows()
                    rows = {row[0].rsplit("/", 1)[1]: row for row in table[1:]}
                    rows["10"][10:] = ["seen_in_person", "2026-10-30", SOURCE, note]
                    out = io.StringIO(newline="")
                    csv.writer(out, lineterminator=newline).writerows(table)
                    text = out.getvalue()
                    if not final:
                        text = text[:-len(newline)]
                    self.csv.write_bytes(text.encode(encoding))
                    self.assertEqual(map_survey._read_sheet(self.csv)[0][13], note)
        # preview, apply, then the real loader reads the exact intended check
        table = self.sheet_rows()
        row = next(row for row in table[1:] if row[0].endswith("/10"))
        row[10:] = ["seen_in_person", "2026-10-30", SOURCE, 'gone, "ok"\nsee map']
        with self.csv.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(table)
        args = ("map", "survey-import", "--csv", str(self.csv), "--directory", str(self.root))
        self.assertEqual(json.loads(run(*args).stdout)["added"], 1)
        applied = run(*args, "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        check = load_map(self.root / "content")["point_checks"][10][0]
        self.assertEqual((check["checked_on"], check["note"]), ("2026-10-30", 'gone, "ok"\nsee map'))

    def test_well_formed_csv_with_disallowed_note_keeps_content_refusal(self):
        self.put(crlf(*self.lines(node10=self.encode(["call 555-123-4567 " + CANARY]))) + "\r\n")
        result = run("map", "survey-import", "--csv", str(self.csv), "--directory", str(self.root))
        self.assertEqual(result.returncode, 1)
        self.assertIn("row 2 note:", result.stderr)
        self.assertNotIn(GENERIC, result.stderr)
        self.assertNotIn(CANARY, result.stderr)

    def test_generated_blank_sheet_round_trips_unchanged(self):
        table = self.sheet_rows()
        self.assertEqual(table[2][3], "'=Evil,Co")  # the sheet guards formula-like cells
        raw = self.sheet_file().read_bytes()
        self.csv.write_bytes(raw)
        self.assertEqual(map_survey._read_sheet(self.csv), table[1:])
        for apply in (False, True):
            result = run("map", "survey-import", "--csv", str(self.csv), "--directory", str(self.root),
                         *(["--apply"] if apply else []))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["added"], 0)
        self.assertEqual(self.csv.read_bytes(), raw)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
