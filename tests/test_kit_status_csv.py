"""kit-status --csv writes the request tracker as a new private CSV file (0600, formula-safe). Offline, synthetic."""
import contextlib
import csv
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_export, kit_track, law  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

HEADER = ["agency_id", "name", "sent_on", "channel", "provisional_determination_due", "provisional_with_extension",
          "extended", "closed", "steps", "last_step_on", "last_step_kind", "linked_records", "draft_changed_since_sent",
          "fee_estimate", "fee_state", "fee_paid"]
TRAILER = "Private file; nothing was sent or published.\n"


def run(*arguments):
    """Run the CLI in-process; return (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class KitStatusCsvTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-status-csv-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        drafts = sorted((self.root / "kit" / "requests").glob("*.md"))
        self.a, self.b = (path.stem for path in drafts[:2])
        ready_drafts(self.root)
        self.private = self.tmp / "private"
        self.private.mkdir()

    def cmd(self, *arguments):
        code, out, err = run(*arguments)
        self.assertEqual(code, 0, err)
        return out

    def send(self, agency, on):
        self.cmd("kit-sent", "--directory", self.root, "--agency", agency, "--date", on, "--channel", "email")

    def step(self, agency, on, kind):
        self.cmd("kit-response", "--directory", self.root, "--agency", agency, "--date", on, "--kind", kind)

    def export(self, out):
        output = self.cmd("kit-status", "--directory", self.root, "--csv", out)
        self.assertTrue(output.endswith(TRAILER), output)
        return json.loads(output[:-len(TRAILER)])

    def rows(self, path):
        data = Path(path).read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        with open(path, encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], HEADER)
        self.assertEqual(data.count(b"\r\n"), len(rows))
        return rows[1:]

    def rename(self, agency, name):
        path = self.root / "kit" / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        next(entry for entry in agencies if entry["agency_id"] == agency)["name"] = name
        path.write_text(json.dumps(agencies), encoding="utf-8")

    def refused(self, out, words):
        before = sorted(p.name for p in self.private.iterdir())
        code, stdout, err = run("kit-status", "--directory", self.root, "--csv", out)
        self.assertEqual(code, 1, stdout)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(words, err)
        self.assertEqual(stdout, "")
        self.assertEqual(sorted(p.name for p in self.private.iterdir()), before)

    def test_two_requests_in_status_order_with_due_dates_flags_and_mode_0600(self):
        self.send(self.b, "2026-03-02")
        self.send(self.a, "2026-03-05")
        self.step(self.b, "2026-03-03", "acknowledged")
        self.step(self.b, "2026-03-04", "extension")
        self.step(self.a, "2026-03-06", "acknowledged")
        self.step(self.a, "2026-03-07", "closed")
        report = json.loads(self.cmd("kit-status", "--directory", self.root))
        out = self.private / "tracker.csv"
        summary = self.export(out)
        package = law.load_package("us-ca")
        self.assertEqual(summary, {"written": str(out.resolve()), "rows": 2,
                                   "law_package_status": package["status"], "note": kit_track.NOTE})
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        rows = self.rows(out)
        self.assertEqual([row[0] for row in rows], [r["agency_id"] for r in report["requests"]])
        self.assertEqual([row[0] for row in rows], [self.b, self.a])
        for row, source in zip(rows, report["requests"]):
            self.assertEqual(row[1:4], [source["name"], source["sent_on"], "email"])
            self.assertEqual(row[4:6], [source["determination_due"], source["with_extension"]])
            self.assertEqual(row[8:], [str(source["steps"]), source["last_step"]["on"], source["last_step"]["kind"],
                                       "0", "no", "", "", ""])
        self.assertEqual(rows[0][4], rows[0][5])  # extended: due is the extended date
        self.assertEqual([row[6:8] for row in rows], [["yes", "no"], ["no", "yes"]])
        self.assertNotIn("record_prefixes", out.read_text(encoding="utf-8"))

    def test_no_requests_gives_header_only(self):
        out = self.private / "empty.csv"
        self.assertEqual(self.export(out)["rows"], 0)
        self.assertEqual(self.rows(out), [])

    def test_missing_law_package_leaves_due_columns_empty(self):
        self.send(self.a, "2026-03-02")
        empty = self.tmp / "no-jurisdictions"
        empty.mkdir()
        out = self.private / "nolaw.csv"
        with mock.patch.object(law, "JURISDICTIONS", empty):
            summary = self.export(out)
        self.assertEqual((summary["law_package_status"], summary["rows"]), ("missing", 1))
        [row] = self.rows(out)
        self.assertEqual(row[4:6], ["", ""])
        self.assertEqual(row[9:11], ["", ""])  # no steps: last step cells empty

    def test_formula_names_get_a_leading_quote_and_commas_and_quotes_round_trip(self):
        self.send(self.a, "2026-03-02")
        cases = {'=HYPERLINK("x")': '\'=HYPERLINK("x")', "+1": "'+1", "-2": "'-2", "@SUM(A1)": "'@SUM(A1)",
                 "\tTab": "'\tTab", "\rCR": "'\rCR", 'Police, "North" Unit': 'Police, "North" Unit',
                 "Plain = name": "Plain = name"}
        for number, (name, written) in enumerate(cases.items()):
            with self.subTest(name=name):
                self.rename(self.a, name)
                out = self.private / f"names-{number}.csv"
                self.export(out)
                [row] = self.rows(out)
                self.assertEqual(row[1], written)

    def test_every_string_cell_is_guarded(self):
        self.assertEqual(kit_export._cell("-x"), "'-x")
        self.assertEqual(kit_export._cell(None), "")
        self.assertEqual((kit_export._cell(True), kit_export._cell(False)), ("yes", "no"))
        self.assertEqual(kit_export._cell(3), "3")

    def test_bad_paths_are_refused_and_nothing_is_created(self):
        (self.root / "public").mkdir(exist_ok=True)
        (self.root / "content" / "sub").mkdir(parents=True, exist_ok=True)
        self.refused(self.root / "public" / "t.csv", "inside the campaign's public/ folder")
        self.assertFalse((self.root / "public" / "t.csv").exists())
        self.refused(self.root / "content" / "sub" / "t.csv", "inside the campaign's content/ folder")
        self.assertFalse((self.root / "content" / "sub" / "t.csv").exists())
        into_public = self.private / "into-public"
        into_public.symlink_to(self.root / "public")
        self.refused(into_public / "t.csv", "inside the campaign's public/ folder")
        self.assertFalse((self.root / "public" / "t.csv").exists())
        existing = self.private / "existing.csv"
        existing.write_text("keep\n", encoding="utf-8")
        self.refused(existing, "exists; choose a new file name")
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep\n")
        link = self.private / "link.csv"
        link.symlink_to(self.private / "new-target.csv")
        self.refused(link, "exists; choose a new file name")
        self.assertFalse((self.private / "new-target.csv").exists())
        self.refused(self.private / "out.txt", "must end in .csv")
        self.refused(self.private / "missing" / "t.csv", "does not exist")
        self.assertFalse((self.private / "missing").exists())

    def test_second_export_to_same_file_is_refused(self):
        out = self.private / "once.csv"
        self.export(out)
        first = out.read_bytes()
        self.send(self.a, "2026-03-02")
        self.refused(out, "exists; choose a new file name")
        self.assertEqual(out.read_bytes(), first)

    def test_write_failure_leaves_no_partial_file(self):
        self.send(self.a, "2026-03-02")
        out = self.private / "fail.csv"
        with mock.patch.object(os, "fsync", side_effect=OSError("disk failed")):
            code, stdout, err = run("kit-status", "--directory", self.root, "--csv", out)
        self.assertEqual(code, 1, stdout)
        self.assertIn("disk failed", err)
        self.assertFalse(os.path.lexists(out))
        self.assertEqual(list(self.private.iterdir()), [])

    def test_bad_sent_json_writes_nothing(self):
        (self.root / "kit" / "sent.json").write_text("not json", encoding="utf-8")
        self.refused(self.private / "bad.csv", "kit/sent.json unreadable")

    def test_without_csv_json_output_is_unchanged(self):
        self.send(self.a, "2026-03-02")
        self.step(self.a, "2026-03-03", "acknowledged")
        self.rename(self.a, "=Formula Name")
        expected = json.dumps(kit_track.status(self.root), indent=2) + "\n"
        self.assertEqual(self.cmd("kit-status", "--directory", self.root), expected)
        self.assertIn('"name": "=Formula Name"', expected)
        self.assertEqual(list(self.private.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
