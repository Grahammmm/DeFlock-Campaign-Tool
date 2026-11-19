"""Synthetic CLI acceptance at the report boundary (no pipeline or project)."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_stages, run_report

FIXTURE = Path("examples/run-report/records-run-report-v1.synthetic.json")
APPROVAL = "OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE"
CLOCK = "OUTPUTS_FOLLOWUP_CLOCK_UNAVAILABLE"


class GOLiveStagesTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.reports = Path(scratch.name) / "reports"
        self.reports.mkdir()

    def report(self, hour, warning=None, *, mode="scheduled", name=None, ocr=None, stub=False):
        report = json.loads(FIXTURE.read_text(encoding="utf-8"))
        report.update(run_id=f"run_20260101T{hour:02}0000Z_0a1b2c3d", mode=mode,
                      started_at=f"2026-01-01T{hour:02}:00:00Z", finished_at=f"2026-01-01T{hour:02}:07:30Z")
        if warning:
            next(stage for stage in report["stages"] if stage["name"] == "outputs").update(
                status="warn", error_code=warning)
        if ocr is not None:
            report["ocr"] = dict(pages_ocr=ocr, pages_conf_ge80=ocr, pages_conf_lt80=0,
                                 pages_conf_unknown=0, pages_reviewed=0, awaiting_ocr_images=0)
        if stub:
            report["needs_g"].append(dict(code="STUB_STAGE_BACKEND", stage="sync", severity="info", count=1))
        path = self.reports / (name or f"{report['run_id']}.json")
        path.write_text(json.dumps(report), encoding="utf-8")
        return report["run_id"]

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = golive_stages.main(["--reports", str(self.reports), *args])
        return code, out.getvalue(), err.getvalue()

    def rows(self, *args):
        code, out, err = self.invoke("--json", *args)
        self.assertEqual((code, err), (0, ""))
        return {row["stage"]: row for row in json.loads(out)["stages"]}

    def test_warning_masks_later_stage_and_next_run_recovers(self):
        # A single overall warn flag would hide both upstream success and ambiguous downstream evidence.
        self.report(6, APPROVAL, name="z-earlier.json")
        rows = self.rows()
        self.assertIsNone(rows["13"]["last_proven_run"])
        self.assertIsNone(rows["13"]["masked_by"])
        self.assertIsNone(rows["14"]["last_proven_run"])
        self.assertEqual(rows["14"]["masked_by"], APPROVAL)
        for stage in ("8", "9", "10"):
            self.assertEqual(rows[stage]["state"], "proven")
        clean = self.report(7, name="a-later.json")
        rows = self.rows()
        self.assertEqual(rows["13"]["last_proven_run"], clean)
        self.assertEqual(rows["14"]["last_proven_run"], clean)
        self.assertIsNone(rows["14"]["masked_by"])
        self.assertEqual(rows["14"]["last_warning"], APPROVAL)

    def test_since_keeps_old_proof_but_limits_warning_history(self):
        # Filtering the entire history would erase the last proof; sorting by filename reverses the history.
        proof = self.report(6, name="z-proof.json")
        cutoff = self.report(7, APPROVAL, name="y-warn.json")
        self.report(8, CLOCK, name="x-latest.json")
        row = self.rows()["14"]
        self.assertEqual((row["first_warning"], row["last_warning"]), (APPROVAL, CLOCK))
        row = self.rows("--since", cutoff)["14"]
        self.assertEqual((row["last_proven_run"], row["runs_since_last_proof"]), (proof, 2))
        self.assertEqual((row["first_warning"], row["last_warning"], row["masked_by"]), (CLOCK, CLOCK, None))

    def test_unknown_and_overwriting_output_warnings_do_not_assume_success(self):
        # A new warning isn't silently allowlisted. Digest's overwrite can hide even the reply warning.
        for hour, warning, mask in ((6, "OUTPUTS_NEW_WARNING", None),
                                    (7, "OUTPUTS_DIGEST_UNRESOLVED", "OUTPUTS_DIGEST_UNRESOLVED")):
            with self.subTest(warning=warning):
                self.report(hour, warning)
                rows = self.rows()
                self.assertIsNone(rows["13"]["last_proven_run"])
                self.assertEqual(rows["13"]["masked_by"], mask)

    def test_external_and_missing_evidence_are_never_proven(self):
        # An umbrella ok cannot promote external stages, OCR without positive counts, or the findings lane.
        self.report(6, ocr=0)
        rows = self.rows()
        external = "external: not provable from run reports"
        self.assertEqual({key for key, row in rows.items() if row["state"] == external},
                         set("1 3 4 17 18 19 20 A1 A2 A3 A4 A5 A6 A7".split()))
        for stage, row in rows.items():
            if row["state"] == external or stage in ("6", "16"):
                self.assertIsNone(row["last_proven_run"], stage)
        self.assertEqual(rows["16"]["state"], "not reported")
        self.report(7, mode="test", ocr=3)
        self.report(8, stub=True, ocr=4)
        self.assertIsNone(self.rows()["6"]["last_proven_run"])
        actual = self.report(9, ocr=2)
        self.assertEqual(self.rows()["6"]["last_proven_run"], actual)

    def test_module_output_is_stable_and_names_invalid_report(self):
        # Exercise the actual module entry point, stable order, and no leak via validator exception text.
        self.report(6)
        cmd = [sys.executable, "-B", "-m", "campaign_tool.records.golive_stages", "--reports", str(self.reports)]
        first = subprocess.check_output(cmd, text=True)
        self.assertEqual(subprocess.check_output(cmd, text=True), first)
        self.assertEqual([line.split(" | ")[0] for line in first.splitlines()[2:]],
                         [str(i) for i in range(1, 21)] + [f"A{i}" for i in range(1, 8)])
        (self.reports / "broken.json").write_text('{"private_body":"not report metadata"}', encoding="utf-8")
        bad = subprocess.run(cmd, text=True, capture_output=True, check=False)
        self.assertEqual((bad.returncode, bad.stdout), (2, ""))
        self.assertIn("broken.json", bad.stderr)
        self.assertNotIn("Traceback", bad.stderr)
        self.assertNotIn("private_body", bad.stderr)

    def test_read_only_confined_report_opens(self):
        # Catch out-of-directory reads, writes, a symlink escape, or traversal into subdirectories.
        run_id = self.report(6)
        valid_name = f"{run_id}.json"
        outside = self.reports.parent / "outside.json"
        outside.write_bytes((self.reports / valid_name).read_bytes())
        (self.reports / "nested").mkdir()
        (self.reports / "nested" / "not-a-run.json").touch()
        (self.reports / "linked.json").symlink_to(outside)
        actual_open, actual_io_open = os.open, io.open
        seen = set()
        report_fds = set()

        def os_read_only(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if path == str(self.reports):
                self.assertTrue(flags & os.O_DIRECTORY)
                return actual_open(path, flags, *args, **kwargs)
            self.assertIn(path, (valid_name, "linked.json"))
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertIsNotNone(kwargs.get("dir_fd"))
            seen.add(path)
            fd = actual_open(path, flags, *args, **kwargs)
            report_fds.add(fd)
            return fd

        def path_check(path, mode, *args, **kwargs):
            if isinstance(path, int):
                self.assertEqual(mode, "rb")
                report_fds.remove(path)
            else:
                self.assertEqual((path, mode), (run_report.SCHEMA_PATH, "r"))
            return actual_io_open(path, mode, *args, **kwargs)

        with patch("os.open", side_effect=os_read_only), patch("io.open", side_effect=path_check), \
                patch("builtins.open", side_effect=AssertionError("unexpected file open")):
            code, out, err = self.invoke()
            self.assertEqual((code, out), (2, ""))
            self.assertIn("linked.json", err)
            (self.reports / "linked.json").unlink()
            self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(seen, {valid_name, "linked.json"})


if __name__ == "__main__":
    unittest.main()
