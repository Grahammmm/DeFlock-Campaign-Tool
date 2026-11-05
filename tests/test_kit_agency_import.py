"""kit-agency import --csv: many organizer agencies, add rules per row, one atomic write or none. Fictional agencies."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_agencies  # noqa: E402

DAY = date(2026, 3, 4)
HEADER = "agency_id,name,kind,note\n"
GOOD = (HEADER + "ca-fictional-north-police,Fictional North Police,police,\n"
        "ca-fictional-county-sheriff,Fictional County Sheriff,sheriff,Seen at a meeting\n"
        "ca-fictional-county-board,Fictional County Board,county_board,\n")
GOOD_IDS = ["ca-fictional-north-police", "ca-fictional-county-sheriff", "ca-fictional-county-board"]


def cli(*arguments):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)


class KitAgencyImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-import-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        result = cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                     "--state", "CA")
        self.assertEqual(result.returncode, 0, result.stderr)
        with contextlib.redirect_stdout(io.StringIO()):
            kit.build_kit(self.root)
        self.path = self.root / "kit" / "agencies.json"
        os.chmod(self.path, 0o640)

    def csv(self, text, name="agencies.csv"):
        path = self.tmp / name
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        return path

    def digest(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def run_import(self, text, **kwargs):
        path = text if isinstance(text, Path) else self.csv(text)
        return kit_agencies.import_csv(self.root, path, today=DAY, **kwargs)

    def refused(self, text, **kwargs):
        before = self.digest()
        with self.assertRaises(ValueError) as caught:
            self.run_import(text, **kwargs)
        self.assertEqual(self.digest(), before, "agencies.json must be byte-identical after a refusal")
        return str(caught.exception)

    def test_good_rows_appended_in_order_mode_kept_then_repeat_refused(self):
        seed = json.loads(self.path.read_text())
        self.assertEqual(self.run_import(GOOD), {"added": GOOD_IDS, "count": 3, "written": True})
        agencies = json.loads(self.path.read_text())
        self.assertEqual(agencies[:len(seed)], seed)
        added = agencies[len(seed):]
        self.assertEqual([a["agency_id"] for a in added], GOOD_IDS)
        common = {"selected": True, "verified": False, "added_by": "organizer", "added_on": "2026-03-04"}
        self.assertEqual(added[1], {"agency_id": GOOD_IDS[1], "name": "Fictional County Sheriff", "kind": "sheriff",
                                    **common, "note": "Seen at a meeting"})
        self.assertNotIn("note", added[0])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        message = self.refused(GOOD)
        for line in (2, 3, 4):
            self.assertIn(f"line {line}: already in kit/agencies.json", message)

    def test_bad_rows_named_by_line_nothing_written(self):
        text = (HEADER + "ca-fictional-north-police,Fictional North Police,police,\n"
                "nv-fictional-south-police,Fictional South Police,police,\n"
                "ca-fictional-county-sheriff,Fictional County Sheriff,sheriff,\n"
                "ca-fictional-town-mayor,Fictional Town Mayor,mayor,\n")
        message = self.refused(text)
        self.assertIn("2 bad row(s)", message)
        self.assertIn("line 3: agency_id must start with 'ca-'", message)
        self.assertIn("line 5: kind must be one of", message)
        self.assertNotIn("line 2:", message)
        self.assertNotIn("line 4:", message)

    def test_blank_lines_ignored_line_numbers_physical(self):
        text = (HEADER + "\nca-fictional-north-police,Fictional North Police,police,\n\n"
                "ca-fictional-north-police,Fictional North Police,police,\n")
        self.assertIn("line 5: repeats line 3 of this file", self.refused(text))

    def test_same_id_twice_names_first_line(self):
        text = (HEADER + "ca-fictional-north-police,Fictional North Police,police,\n"
                "ca-fictional-county-board,Fictional County Board,county_board,\n"
                "ca-fictional-north-police,Fictional North Police Again,police,\n")
        message = self.refused(text)
        self.assertIn("line 4: repeats line 2 of this file", message)
        self.assertIn("1 bad row(s)", message)

    def test_shape_refused_before_any_row_check(self):
        bad_row = "nv-fictional-south-police,Fictional South Police,police\n"
        cases = [("id,name,kind\n" + bad_row, "header must be exactly"),
                 ("agency_id,name,kind,note,extra\n" + bad_row, "header must be exactly"),
                 ("Agency_id,name,kind\n" + bad_row, "header must be exactly"),
                 ("agency_id,name,kind\n" + bad_row + "ca-fictional-x,Fictional X,police,extra\n",
                  "line 3: expected 3 cells, found 4"),
                 ("agency_id,name,kind\n" + bad_row + "ca-fictional-x,Fictional X\n", "line 3: expected 3 cells, found 2"),
                 ("", "header must be exactly"),
                 ("\n\n", "header must be exactly"),
                 ("agency_id,name,kind\n\n", "1-500 data rows (found 0)"),
                 ("agency_id,name,kind\n" + bad_row * 501, "1-500 data rows (found 501)")]
        for text, words in cases:
            with self.subTest(words=words, text=text[:30]):
                message = self.refused(text)
                self.assertIn(words, message)
                self.assertNotIn("must start with", message, "row checks must not run on a malformed file")

    def test_name_and_note_rules_each_named(self):
        rows = ["ca-fictional-a,Fictional <script> Police,police,",
                "ca-fictional-b,\"Fictional\tB Police\",police,",
                "ca-fictional-c,Fx,police,",
                "ca-fictional-d," + "N" * 121 + ",police,",
                "ca-fictional-e,Fictional E Police,police," + "x" * 301,
                "ca-fictional-f,Fictional F Police,police," + "x" * 300]
        message = self.refused(HEADER + "\n".join(rows) + "\n")
        self.assertIn("5 bad row(s)", message)
        self.assertIn("line 2: name must be plain text without angle brackets", message)
        self.assertIn("line 3: name must be 3-120 characters with no control characters", message)
        self.assertIn("line 4: name must be 3-120 characters", message)
        self.assertIn("line 5: name must be 3-120 characters", message)
        self.assertIn("line 6: note must be at most 300 characters", message)
        self.assertNotIn("line 7:", message)

    def test_more_than_twenty_errors_listed_then_counted(self):
        rows = "".join(f"nv-fictional-p{n},Fictional P{n} Police,police,\n" for n in range(25))
        message = self.refused(HEADER + rows)
        self.assertIn("25 bad row(s)", message)
        self.assertIn("line 21:", message)
        self.assertNotIn("line 22:", message)
        self.assertTrue(message.endswith("(+5 more)"), message)

    def test_csv_file_kinds_refused_bom_header_works(self):
        real = self.csv(GOOD, "real.csv")
        link = self.tmp / "link.csv"
        link.symlink_to(real)
        fifo = self.tmp / "pipe.csv"
        os.mkfifo(fifo)
        big = self.csv(GOOD + "#" * (256 * 1024), "big.csv")
        cases = [(link, "symbolic links"), (fifo, "regular file"), (self.tmp, "regular file"),
                 (self.tmp / "missing.csv", "cannot be opened"), (big, "larger than 256 KiB"),
                 (self.csv(GOOD.encode() + b"ca-fictional-x,Caf\xe9 Police,police,\n", "latin.csv"), "not UTF-8")]
        for path, words in cases:
            with self.subTest(words=words):
                self.assertIn(words, self.refused(path))
        self.assertEqual(self.run_import(self.csv("\ufeff" + GOOD, "bom.csv"))["count"], 3)

    def test_exactly_256_kib_accepted(self):
        text = GOOD.replace("Seen at a meeting", "")
        padded = text + "\n" * (256 * 1024 - len(text.encode()))
        self.assertEqual(self.run_import(padded)["added"], GOOD_IDS)

    def test_unparseable_csv_refused_without_traceback(self):
        huge_cell = HEADER + "ca-fictional-a,Fictional A,police," + "x" * 140000 + "\n"
        self.assertIn("csv line 2: field larger than field limit", self.refused(huge_cell))
        result = cli("kit-agency", "import", "--directory", self.root, "--csv", self.csv(huge_cell))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stderr.startswith("Stopped: csv line 2"), result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_check_reports_without_writing(self):
        before = self.digest()
        self.assertEqual(self.run_import(GOOD, check=True), {"added": GOOD_IDS, "count": 3, "written": False})
        self.assertEqual(self.digest(), before)
        self.assertIn("line 3: kind must be one of",
                      self.refused(HEADER + "ca-fictional-a,Fictional A,police,\nca-fictional-b,Fictional B,mayor,\n",
                                   check=True))

    def test_write_failure_leaves_file_and_no_temp(self):
        before = self.digest()
        with mock.patch("os.replace", side_effect=OSError("injected")), self.assertRaises(OSError):
            self.run_import(GOOD)
        self.assertEqual(self.digest(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_cli_ok_check_and_stopped(self):
        good = self.csv(GOOD)
        before = self.digest()
        result = cli("kit-agency", "import", "--directory", self.root, "--csv", good, "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.rstrip().endswith("Nothing was sent."), result.stdout)
        self.assertEqual(json.loads(result.stdout.rsplit("Nothing was sent.", 1)[0])["written"], False)
        self.assertEqual(self.digest(), before)
        result = cli("kit-agency", "import", "--directory", self.root, "--csv", good)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.rsplit("Nothing was sent.", 1)[0]),
                         {"added": GOOD_IDS, "count": 3, "written": True})
        after = self.digest()
        result = cli("kit-agency", "import", "--directory", self.root, "--csv", good)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stderr.startswith("Stopped: 3 bad row(s)"), result.stderr)
        self.assertIn("line 2: already in kit/agencies.json", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.digest(), after)

    def snapshot(self):
        """Roster bytes + mode, the rest of kit/ and any leftover temp file."""
        kit_dir = self.path.parent
        tree = {str(p.relative_to(kit_dir)): (p.read_bytes() if p.is_file() else None)
                for p in sorted(kit_dir.rglob("*"))}
        return (self.path.read_bytes(), self.path.stat().st_mode & 0o777, tree)

    def assert_refused_everywhere(self, text, words):
        """API apply, API check and CLI apply/check all refuse; nothing in kit/ changes."""
        before = self.snapshot()
        for check in (False, True):
            with self.subTest(surface="api", check=check):
                self.assertIn(words, self.refused(text, check=check))
                self.assertEqual(self.snapshot(), before)
            with self.subTest(surface="cli", check=check):
                extra = ["--check"] if check else []
                result = cli("kit-agency", "import", "--directory", self.root, "--csv", self.csv(text), *extra)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
                self.assertIn(words, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.snapshot(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_unterminated_quoted_note_refused_all_or_nothing(self):
        row = "ca-fictional-a,Fictional A,police,\"unfinished"
        for tail in ("", "\n"):
            with self.subTest(tail=tail):
                text = HEADER + "ca-fictional-good,Fictional Good,police,\n" + row + tail
                self.assert_refused_everywhere(text, "csv line 3:")

    def test_text_after_closing_quote_refused_all_or_nothing(self):
        text = HEADER + "ca-fictional-good,Fictional Good,police,\nca-fictional-a,Fictional A,police,\"note\"junk\n"
        self.assert_refused_everywhere(text, "csv line 3:")
        self.assert_refused_everywhere(HEADER + "ca-fictional-a,Fictional A,police,\"note\"junk\n", "csv line 2:")

    def test_quoted_newline_id_is_not_a_valid_literal_id(self):
        good = "ca-fictional-good,Fictional Good,police,\n"
        for suffix in ("\n", "\r\n", " ", "\t", "\x00", "\x1f"):
            with self.subTest(suffix=repr(suffix)):
                text = HEADER + good + f"\"ca-fictional-a{suffix}\",Fictional A,police,\n"
                self.assert_refused_everywhere(text, "line 3: agency_id must be lowercase")

    def test_valid_csv_quoting_round_trips(self):
        seed = json.loads(self.path.read_text())
        text = ("\ufeff" + HEADER.replace("\n", "\r\n")
                + "ca-fictional-a,\"Fictional, A \"\"North\"\" Police\",police,\r\n"
                + "ca-fictional-b,Fictional B,sheriff,\"one, \"\"two\"\"\nthree\"")
        self.assertEqual(self.run_import(text)["added"], ["ca-fictional-a", "ca-fictional-b"])
        added = json.loads(self.path.read_text())[len(seed):]
        self.assertEqual(added[0]["name"], 'Fictional, A "North" Police')
        self.assertNotIn("note", added[0])
        self.assertEqual(added[1]["note"], 'one, "two"\nthree')
        self.assertEqual(added[1]["added_by"], "organizer")
        self.assertIs(added[1]["selected"], True)

    def test_multiline_note_preserves_next_row_start_line(self):
        good = "ca-fictional-a,Fictional A,police,\"line one\nline two\"\n"
        message = self.refused(HEADER + good + "ca-fictional-b,Fictional B,mayor,\n")
        self.assertIn("line 4: kind must be one of", message)
        self.assertNotIn("line 2:", message)
        message = self.refused(HEADER + good + "ca-fictional-b,Fictional B,police,\"open\nmore\nmore\n")
        self.assertIn("csv line 4:", message)


if __name__ == "__main__":
    unittest.main()
