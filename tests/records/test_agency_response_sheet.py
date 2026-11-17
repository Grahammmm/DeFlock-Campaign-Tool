"""DFAGB-08: agency response sheet projection (synthetic data only)."""
import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import agency_response_sheet as sheet, cli

CANARY = "CANARY-9f3a"
UNSET = object()


def ask(ask_id, rating=UNSET, flags=(), text="Produce the audit log " + CANARY):
    row = {"ask_id": ask_id, "text": text, "evidence": [CANARY + " exemption 6254"], "flags": list(flags)}
    if rating is not UNSET:
        row["rating"] = rating
    return row


class SheetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.out = self.root / "sheet.md"

    def write(self, requests, run="run1", schema="records-response-items-v1", raw=None):
        path = self.project / "run-outputs" / run / "outcomes"
        path.mkdir(parents=True, exist_ok=True)
        body = raw if raw is not None else json.dumps({"schema": schema, "run_id": run, "requests": requests})
        (path / "response-items.json").write_text(body)

    def run_cli(self, *extra, out=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = sheet.main(["--project", str(self.project), "--agency", "A1",
                               "--out", str(out or self.out), *extra])
        return code, err.getvalue()

    def text(self):
        return self.out.read_text()

    def one(self, asks, flags=()):
        self.write([{"agency_key": "A1", "request_id": "R-1", "flags": list(flags), "asks": asks}])
        self.assertEqual(self.run_cli()[0], 0)
        return self.text()

    def test_six_ratings_exact_mapping(self):
        pairs = [("answered", "produced"), ("withheld", "withheld"), ("exemption_cited", "exemption cited"),
                 ("no_records_stated", "no records stated"), ("missing", "not addressed"), ("unclear", "unclear")]
        out = self.one([ask(i + 1, r) for i, (r, _) in enumerate(pairs)])
        for i, (rating, state) in enumerate(pairs):
            self.assertIn("- ask %d | tier 1 | raw %s | %s | flags: none" % (i + 1, rating, state), out)
        self.assertIn("machine reading, review required; not a legal finding", out)

    def test_citation_only_stays_exemption_cited(self):
        out = self.one([ask(1, "exemption_cited")])
        self.assertIn("| exemption cited |", out)
        self.assertNotIn("| withheld |", out)
        self.assertIn("it is not a withholding and not a validity finding", out)

    def test_conflicting_cue_unclear_keeps_flag(self):
        out = self.one([ask(1, "unclear", ["conflicting_cues:exemption_cited+withheld"])])
        self.assertIn("raw unclear | unclear | flags: conflicting_cues:exemption_cited+withheld", out)

    def test_text_unreviewed_flags_stay_explicit(self):
        out = self.one([ask(1, "unclear", ["text_unreviewed", "unclear_reason:text_unreviewed"])],
                       flags=["text_unreviewed"])
        self.assertIn("request flags: text_unreviewed", out)
        self.assertIn("| unclear | flags: text_unreviewed, unclear_reason:text_unreviewed", out)
        self.assertNotIn("not addressed |", out)

    def test_null_absent_unknown_unrated(self):
        values = [None, UNSET, "banana", ["x"], 7]
        out = self.one([ask(i + 1, v) for i, v in enumerate(values)])
        for i, raw in enumerate(["null", "absent", "banana", "invalid:list", "invalid:int"]):
            self.assertIn("- ask %d | tier 1 | raw %s | not rated |" % (i + 1, raw), out)
        for word in ("| produced", "| withheld", "| not addressed"):
            self.assertNotIn(word, out)

    def test_no_run_data_marker(self):
        rated = self.one([ask(1, "banana")])
        self.out.unlink()
        for setup in ("no_file", "other_agency", "none"):
            with self.subTest(setup=setup):
                if setup == "no_file":
                    (self.project / "run-outputs" / "run1" / "outcomes" / "response-items.json").unlink()
                if setup == "other_agency":
                    self.write([{"agency_key": "ZZ", "request_id": "R-9", "asks": [ask(1, "answered")]}])
                if setup == "none":
                    self.project.mkdir(exist_ok=True)
                    (self.project / "run-outputs").rename(self.root / "moved")
                self.assertEqual(self.run_cli()[0], 0)
                body = self.text()
                self.assertIn("unknown: no run data", body)
                self.assertNotIn("not rated", body)
                self.assertNotEqual(body, rated)
                self.out.unlink()

    def test_no_text_leaks(self):
        out = self.one([ask(1, "exemption_cited", ["free text " + CANARY, "text_unreviewed"])], flags=[CANARY])
        self.assertNotIn(CANARY, out)
        self.assertNotIn("6254", out)
        self.assertIn("flags: other_flag, text_unreviewed", out)

    def test_inputs_unchanged_and_out_guards(self):
        self.one([ask(1, "answered")])

        def digest():
            return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.project.rglob("*") if p.is_file()}
        before = digest()
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o600)
        code, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("refused response_sheet_out_exists", err)
        code, err = self.run_cli(out=self.project / "inside.md")
        self.assertEqual(code, 2)
        self.assertIn("refused response_sheet_out_in_project", err)
        self.assertFalse((self.project / "inside.md").exists())
        self.assertEqual(digest(), before)

    def test_bad_input_refused_fixed_text(self):
        for case in ("schema", "json", "dup"):
            with self.subTest(case=case):
                if case == "schema":
                    self.write([], schema="other-v1")
                elif case == "json":
                    self.write([], raw="{not json")
                else:
                    self.write([], raw='{"schema": "records-response-items-v1", "schema": "x"}')
                code, err = self.run_cli()
                self.assertEqual(code, 2)
                self.assertIn("refused response_sheet_input", err)
                self.assertNotIn(str(self.root), err)
                self.assertFalse(self.out.exists())

    def test_malformed_containers_refused(self):
        for requests in (1, [{"agency_key": "A1", "asks": 1}]):
            with self.subTest(requests=requests):
                self.write(requests)
                code, err = self.run_cli("--run-id", "run1")
                self.assertEqual((code, err), (2, "records agency-response-sheet: refused response_sheet_input\n"))
                self.assertFalse(self.out.exists())

    def test_symlinked_input_directories_refused(self):
        for component in ("run-outputs", "run1", "outcomes"):
            with self.subTest(component=component):
                self.write([{"agency_key": "A1", "request_id": "outside", "asks": []}])
                path = {"run-outputs": self.project / "run-outputs",
                        "run1": self.project / "run-outputs" / "run1",
                        "outcomes": self.project / "run-outputs" / "run1" / "outcomes"}[component]
                moved = self.root / (component + "-outside")
                path.rename(moved)
                path.symlink_to(moved, target_is_directory=True)
                code, err = self.run_cli("--run-id", "run1")
                self.assertEqual((code, err), (2, "records agency-response-sheet: refused response_sheet_input\n"))
                self.assertFalse(self.out.exists())
                path.unlink()
                moved.rename(path)

    def test_run_replaced_by_symlink_after_selection_refused(self):
        self.write([{"agency_key": "A1", "request_id": "outside", "asks": []}])
        pick = sheet._pick_run

        def swap(project, run_id):
            chosen = pick(project, run_id)
            run = project / "run-outputs" / "run1"
            outside = self.root / "outside"
            run.rename(outside)
            run.symlink_to(outside, target_is_directory=True)
            return chosen

        with mock.patch.object(sheet, "_pick_run", side_effect=swap):
            code, err = self.run_cli("--run-id", "run1")
        self.assertEqual((code, err), (2, "records agency-response-sheet: refused response_sheet_input\n"))
        self.assertFalse(self.out.exists())

    def test_cli_registered(self):
        self.assertEqual(cli.COMMANDS["agency-response-sheet"], ("agency_response_sheet", "main"))


if __name__ == "__main__":
    unittest.main()
