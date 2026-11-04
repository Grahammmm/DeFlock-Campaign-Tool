"""kit-verify-agency import --csv: many organizer-checked contacts, kit-verify-agency rules per row, one write or none.

Offline; fictional agencies; nothing is fetched or sent."""
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit  # noqa: E402

DAY = date(2026, 11, 1)
HEADER = "agency_id,source_url,checked_on,records_email,records_url,portal_vendor,portal_url\n"
A, B, C = "ca-fictional-a-police", "ca-fictional-b-sheriff", "ca-fictional-c-police"
GOOD = (HEADER + f"{A},https://a.example/records,2026-10-01,records@a.example,,,\n"
        f"{B},https://b.example/records,2026-10-02,,https://b.example/foia,,\n"
        f"{C},https://c.example/records,2026-10-03,,,govqa,https://c.example/portal\n")
ADDRESS = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
FICTIONAL = [{"agency_id": a, "name": f"Fictional {a}", "kind": "police", "selected": True, "verified": False}
             for a in (A, B, C)]


def cli(*arguments):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)


class VerifyImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-verify-import-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        result = cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                     "--state", "CA")
        self.assertEqual(result.returncode, 0, result.stderr)
        with contextlib.redirect_stdout(io.StringIO()):
            kit.build_kit(self.root)
        self.path = self.root / "kit" / "agencies.json"
        self.path.write_text(json.dumps(json.loads(self.path.read_text()) + FICTIONAL, indent=2) + "\n")

    def csv(self, text, name="contacts.csv"):
        path = self.tmp / name
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        return path

    def digest(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def agency(self, agency_id):
        return next(a for a in json.loads(self.path.read_text()) if a["agency_id"] == agency_id)

    def run_import(self, text, **kwargs):
        path = text if isinstance(text, Path) else self.csv(text)
        return kit.verify_agencies_csv(self.root, path, today=DAY, **kwargs)

    def refused(self, text, **kwargs):
        before = self.digest()
        with self.assertRaises(ValueError) as caught:
            self.run_import(text, **kwargs)
        self.assertEqual(self.digest(), before, "agencies.json must be byte-identical after a refusal")
        return str(caught.exception)

    def test_three_good_rows_one_write_each_with_own_verification(self):
        with mock.patch.object(kit, "_write_json", wraps=kit._write_json) as write:
            self.assertEqual(self.run_import(GOOD), {"verified": [A, B, C], "count": 3, "written": True})
        self.assertEqual(write.call_count, 1)
        a, b, c = self.agency(A), self.agency(B), self.agency(C)
        self.assertTrue(a["verified"] and b["verified"] and c["verified"])
        self.assertEqual(a["records_email"], "records@a.example")
        self.assertEqual(a["verification"], {"source_url": "https://a.example/records", "checked_on": "2026-10-01",
                                             "fields": ["records_email"]})
        self.assertEqual(b["records_url"], "https://b.example/foia")
        self.assertEqual(b["verification"]["fields"], ["records_url"])
        self.assertEqual(c["portal"], {"vendor": "govqa", "url": "https://c.example/portal"})
        self.assertEqual(c["verification"], {"source_url": "https://c.example/records", "checked_on": "2026-10-03",
                                             "fields": ["portal"]})

    def test_portal_url_without_vendor_keeps_entry_vendor_or_unknown(self):
        entries = json.loads(self.path.read_text())
        next(e for e in entries if e["agency_id"] == B)["portal"] = {"vendor": "nextrequest", "url": None}
        self.path.write_text(json.dumps(entries, indent=2) + "\n")
        self.run_import(HEADER + f"{A},https://a.example/r,2026-10-01,,,,https://a.example/p\n"
                        f"{B},https://b.example/r,2026-10-01,,,,https://b.example/p\n")
        self.assertEqual(self.agency(A)["portal"], {"vendor": "unknown", "url": "https://a.example/p"})
        self.assertEqual(self.agency(B)["portal"], {"vendor": "nextrequest", "url": "https://b.example/p"})

    def test_one_bad_row_among_three_names_its_line_and_writes_nothing(self):
        bad = GOOD.replace("https://b.example/records", "http://b.example/records")
        message = self.refused(bad)
        self.assertIn("line 3: source_url must be an https:// URL with a host", message)
        self.assertNotIn("line 2", message)
        self.assertNotIn("line 4", message)

    def test_each_bad_value_refused_by_line_with_single_agency_message(self):
        ok = "https://x.example/records"
        cases = [
            (f"ca-fictional-nobody,{ok},2026-10-01,records@x.example,,,\n", "line 2: agency_id 'ca-fictional-nobody' must appear exactly once"),
            (f"{A},{ok},2026-11-02,records@x.example,,,\n", "line 2: checked_on 2026-11-02 is after today"),
            (f"{A},{ok},2026-02-30,records@x.example,,,\n", "line 2: checked_on must be a real YYYY-MM-DD date"),
            (f"{A},{ok},2026-10-01,not-an-address,,,\n", "line 2: records_email must be a plain address"),
            (f"{A},{ok},2026-10-01,,,,\n", "line 2: give at least one of records_email, records_url, portal"),
            (f"{A},{ok},2026-10-01,,http://x.example/foia,,\n", "line 2: records_url must be an https:// URL"),
            (f"{A},{ok},2026-10-01,,,acmefoia,https://x.example/p\n", "line 2: portal_vendor must be one of nextrequest, govqa"),
            (f"{A},{ok},2026-10-01,,,govqa,\n", "line 2: portal_vendor needs a portal_url"),
            (f"{A},{ok},2026-10-01,records@x.example\n", "line 2: expected 7 cells, found 4"),
        ]
        for row, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, self.refused(HEADER + row))

    def test_single_agency_command_gives_the_same_message_for_the_same_value(self):
        result = cli("kit-verify-agency", "--directory", self.root, "--agency-id", A, "--source-url",
                     "http://x.example/r", "--checked-on", "2026-10-01", "--records-url", "https://x.example/f")
        self.assertIn("source_url must be an https:// URL with a host", result.stderr)
        self.assertIn("line 2: source_url must be an https:// URL with a host",
                      self.refused(HEADER + f"{A},http://x.example/r,2026-10-01,,https://x.example/f,,\n"))

    def test_repeated_agency_id_refused_on_second_line_even_if_first_is_fine(self):
        message = self.refused(GOOD + f"{A},https://a.example/other,2026-10-04,other@a.example,,,\n")
        self.assertIn("1 bad row(s)", message)
        self.assertIn("line 5: same agency as line 2", message)

    def test_bad_file_shapes_refused_before_any_row(self):
        wrong = HEADER.replace("source_url,checked_on", "checked_on,source_url")
        row = f"{A},https://a.example/r,2026-10-01,records@a.example,,,\n"
        many = HEADER + "".join(f"ca-fictional-{n}-police,https://x.example/r,2026-10-01,r@x.example,,,\n" for n in range(501))
        link = self.tmp / "link.csv"
        link.symlink_to(self.csv(GOOD, "real.csv"))
        cases = [
            (HEADER.rstrip().rsplit(",", 1)[0] + "\n" + row, "csv header must be exactly agency_id,source_url"),
            (wrong + row, "csv header must be exactly"),
            ("", "csv header must be exactly"),
            (HEADER, "csv must have 1-500 data rows (found 0)"),
            (many, "csv must have 1-500 data rows (found 501)"),
            (GOOD + "#" * (512 * 1024), "larger than 512 KiB"),
            (GOOD.encode() + b"ca-x,Caf\xe9\n", "not UTF-8"),
            (link, "symlinks are refused"),
            (self.tmp / "missing.csv", "cannot be opened"),
        ]
        for source, words in cases:
            with self.subTest(words=words, kind=type(source).__name__):
                self.assertIn(words, self.refused(source))

    def test_exactly_500_rows_and_bom_accepted(self):
        entries = json.loads(self.path.read_text())
        entries += [{"agency_id": f"ca-fictional-{n:03d}-police", "name": f"Fictional {n}", "kind": "police",
                     "selected": True, "verified": False} for n in range(500)]
        self.path.write_text(json.dumps(entries) + "\n")
        rows = "".join(f"ca-fictional-{n:03d}-police,https://x.example/r,2026-10-01,r@x.example,,,\n" for n in range(500))
        self.assertEqual(self.run_import("\ufeff" + HEADER + rows)["count"], 500)

    def test_twenty_five_bad_rows_list_twenty_then_count(self):
        rows = "".join(f"ca-fictional-nobody-{n},https://x.example/r,2026-10-01,r@x.example,,,\n" for n in range(25))
        message = self.refused(HEADER + rows)
        self.assertTrue(message.startswith("25 bad row(s)"), message)
        listed = [line for line in message.splitlines() if line.startswith("line ")]
        self.assertEqual(len(listed), 20)
        self.assertEqual((listed[0][:7], listed[-1][:8]), ("line 2:", "line 21:"))
        self.assertTrue(message.endswith("and 5 more"), message)

    def test_check_with_good_rows_changes_nothing(self):
        before = (self.digest(), self.path.stat().st_mtime_ns)
        with mock.patch.object(kit, "_write_json") as write:
            self.assertEqual(self.run_import(GOOD, check=True), {"verified": [A, B, C], "count": 3, "written": False})
        write.assert_not_called()
        self.assertEqual((self.digest(), self.path.stat().st_mtime_ns), before)
        self.assertIn("line 3:", self.refused(GOOD.replace("2026-10-02", "2026-13-02"), check=True))

    def test_write_failure_leaves_file_and_no_temp(self):
        before = self.digest()
        with mock.patch("os.replace", side_effect=OSError("injected")), self.assertRaises(OSError):
            self.run_import(GOOD)
        self.assertEqual(self.digest(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_never_fetches_or_sends(self):
        with mock.patch("socket.socket", side_effect=AssertionError("network used")), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            self.run_import(GOOD)
            self.refused(GOOD.replace("https://a.example/records", "http://a.example/records"))

    def test_cli_output_never_prints_an_address(self):
        good, before = self.csv(GOOD), self.digest()
        bad = self.csv(HEADER + f"ca-fictional-secret@leak.example,https://x.example/r,2026-10-01,jane.doe@leak.example,,,\n"
                       f"{A},https://x.example/r,2026-10-01,other@leak.example extra,,,\n"
                       f"{B},https://x.example/r,2026-10-01,bad@@leak.example,,,\n", "bad.csv")
        results = [cli("kit-verify-agency", "import", "--directory", self.root, "--csv", good, "--check"),
                   cli("kit-verify-agency", "import", "--directory", self.root, "--csv", bad)]
        self.assertEqual([r.returncode for r in results], [0, 2])
        self.assertEqual(self.digest(), before)
        for result in results:
            self.assertIsNone(ADDRESS.search(result.stdout + result.stderr), result.stdout + result.stderr)
        self.assertIn("line 2:", results[1].stderr)
        self.assertTrue(results[1].stderr.startswith("Stopped: 3 bad row(s)"), results[1].stderr)
        self.assertNotIn("Traceback", results[1].stderr)
        result = cli("kit-verify-agency", "import", "--directory", self.root, "--csv", good)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.split("Nothing was fetched", 1)[0]),
                         {"verified": [A, B, C], "count": 3, "written": True})
        self.assertIsNone(ADDRESS.search(result.stdout + result.stderr))
        self.assertTrue(self.agency(A)["verified"])

    def test_cli_import_refuses_single_agency_flags_and_missing_csv(self):
        base = ("kit-verify-agency", "import", "--directory", self.root)
        before = self.digest()
        for extra, words in (((), "--csv"), (("--csv", self.csv(GOOD), "--agency-id", A), "--agency-id"),
                             (("--csv", self.csv(GOOD), "--records-email", "r@x.example"), "--records-email")):
            with self.subTest(words=words):
                result = cli(*base, *extra)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(words, result.stderr)
        self.assertEqual(self.digest(), before)

    def test_cli_single_agency_still_requires_its_flags_and_refuses_import_flags(self):
        result = cli("kit-verify-agency", "--directory", self.root, "--records-email", "r@x.example")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("the following arguments are required: --agency-id, --source-url, --checked-on", result.stderr)
        result = cli("kit-verify-agency", "--directory", self.root, "--agency-id", A, "--source-url", "https://x.example/r",
                     "--checked-on", "2026-10-01", "--records-email", "r@x.example", "--check")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("only with 'import'", result.stderr)
        result = cli("kit-verify-agency", "--directory", self.root, "--agency-id", A, "--source-url", "https://x.example/r",
                     "--checked-on", "2026-10-01", "--records-email", "r@x.example")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.agency(A)["verified"])


if __name__ == "__main__":
    unittest.main()
