"""Synthetic CLI acceptance at the report boundary (no pipeline or project)."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_stages, run_report

FIXTURE = Path("examples/run-report/records-run-report-v1.synthetic.json")
APPROVAL = "OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE"
CLOCK = "OUTPUTS_FOLLOWUP_CLOCK_UNAVAILABLE"
REGISTER = "OUTPUTS_REGISTER_UNAVAILABLE"
DIGEST = "OUTPUTS_DIGEST_UNRESOLVED"


class GOLiveStagesTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.reports = Path(scratch.name) / "reports"
        self.reports.mkdir()

    def report(self, hour, warning=None, *, mode="scheduled", name=None, ocr=None, stub=False,
               extracted=None, needs=(), outputs_status="warn"):
        report = json.loads(FIXTURE.read_text(encoding="utf-8"))
        report.update(run_id=f"run_20260101T{hour:02}0000Z_0a1b2c3d", mode=mode,
                      started_at=f"2026-01-01T{hour:02}:00:00Z", finished_at=f"2026-01-01T{hour:02}:07:30Z")
        if warning:
            next(stage for stage in report["stages"] if stage["name"] == "outputs").update(
                status=outputs_status, error_code=warning)
            if outputs_status == "fail":
                report["overall_status"] = "fail"
        report["needs_g"].extend(dict(code=code, stage=stage, severity=severity, count=1)
                                 for code, stage, severity in needs)
        if extracted is not None:
            next(stage for stage in report["stages"] if stage["name"] == "extract")["counts"].update(
                input=extracted, output=extracted)
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

    def test_unwritable_queue_warning_is_known_and_does_not_prove_the_queue_stage(self):
        # Contract (DF2222-02): OUTPUTS_APPROVAL_QUEUE_UNWRITABLE ranks with the other queue warning. Fails if stage 13
        # reads proven, stage 14 stops being masked by it, or the unrelated outputs stages stop proving.
        self.report(6, "OUTPUTS_APPROVAL_QUEUE_UNWRITABLE")
        rows = self.rows()
        self.assertIsNone(rows["13"]["last_proven_run"])
        self.assertIsNone(rows["13"]["masked_by"])
        self.assertEqual(rows["14"]["masked_by"], "OUTPUTS_APPROVAL_QUEUE_UNWRITABLE")
        for stage in ("8", "9", "10"):
            self.assertEqual(rows[stage]["state"], "proven")

    def test_full_list_unwritable_queue_is_stage_13_own_failure_not_proof(self):
        # Contract (DF2222-02): on the full-list path the unwritable-queue code is stage 13's own failure. Fails if stage
        # 13 reads proven with no warning although the queue write failed; stage 14 (clock) must stay independent.
        unwritable = "OUTPUTS_APPROVAL_QUEUE_UNWRITABLE"
        self.report(6, unwritable, needs=((unwritable, "outputs", "action"),))
        rows = self.rows()
        row = rows["13"]
        self.assertEqual((row["state"], row["last_warning"], row["last_proven_run"], row["basis"]),
                         ("not proven", unwritable, None, "full-list"))
        self.assertEqual((rows["14"]["state"], rows["14"]["masked_by"]), ("proven", None))

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

    def test_full_list_distinguishes_later_success_from_own_failure(self):
        # First-warning inference both hides a successful clock and misattributes a failed one.
        approval = (APPROVAL, "outputs", "action")
        success = self.report(6, APPROVAL, needs=(approval, (CLOCK, "extract", "action")))
        rows = self.rows()
        self.assertEqual((rows["14"]["state"], rows["14"]["last_proven_run"], rows["14"]["masked_by"]),
                         ("proven", success, None))
        self.assertEqual(rows["14"]["basis"], "full-list")
        self.assertEqual((rows["13"]["state"], rows["13"]["last_warning"], rows["13"]["masked_by"]),
                         ("not proven", APPROVAL, None))
        self.assertEqual(rows["15"]["state"], golive_stages.NO_RECEIPT)
        self.assertIn(" | Basis\n", self.invoke()[1])
        self.assertIn(" | full-list\n", self.invoke()[1])
        self.report(7, APPROVAL, needs=(approval, (CLOCK, "outputs", "action")))
        for number, code in (("13", APPROVAL), ("14", CLOCK)):
            row = self.rows()[number]
            self.assertEqual((row["state"], row["last_warning"], row["masked_by"]), ("not proven", code, None))
        self.assertEqual(self.rows()["14"]["last_proven_run"], success)

    def test_full_list_upstream_blockers_do_not_become_success(self):
        # No clock failure is emitted when register fails; digest overwrite still blocks positive proof.
        approval = (APPROVAL, "outputs", "action")
        self.report(6, APPROVAL, needs=(approval, (REGISTER, "outputs", "action")))
        for number in ("12", "14"):
            row = self.rows()[number]
            self.assertEqual((row["state"], row["last_warning"], row["masked_by"]), ("not proven", REGISTER, None))
        self.report(7, DIGEST, needs=(approval, (DIGEST, "outputs", "action")))
        rows = self.rows()
        for number in map(str, range(9, 16)):
            row = rows[number]
            self.assertEqual((row["state"], row["last_warning"], row["masked_by"], row["basis"]),
                             ("not proven", APPROVAL if number == "13" else DIGEST, None, "full-list"))

    def test_full_list_requires_matching_outputs_action_on_warn(self):
        # A blocking abort item or an action from another stage cannot qualify later outputs.
        for hour, status, item, state in (
            (6, "warn", (APPROVAL, "extract", "action"), "masked"),
            (7, "warn", (APPROVAL, "outputs", "blocking"), "masked"),
            (8, "warn", (CLOCK, "outputs", "action"), "masked"),
            (9, "fail", (APPROVAL, "outputs", "blocking"), "not proven"),
            (10, "fail", (APPROVAL, "outputs", "action"), "not proven"),
        ):
            with self.subTest(status=status, item=item):
                self.report(hour, APPROVAL, needs=(item,), outputs_status=status)
                row = self.rows()["14"]
                self.assertEqual((row["basis"], row["state"], row["last_proven_run"], row["masked_by"]),
                                 ("first-warning (inferred)", state, None, APPROVAL if state == "masked" else None))

    def test_unknown_and_overwriting_output_warnings_do_not_assume_success(self):
        # A new warning isn't silently allowlisted. Digest's overwrite can hide even the reply warning.
        for hour, warning, mask in ((6, "OUTPUTS_NEW_WARNING", None),
                                    (7, "OUTPUTS_DIGEST_UNRESOLVED", "OUTPUTS_DIGEST_UNRESOLVED")):
            with self.subTest(warning=warning):
                self.report(hour, warning)
                rows = self.rows()
                self.assertIsNone(rows["13"]["last_proven_run"])
                self.assertEqual(rows["13"]["masked_by"], mask)

    def test_full_list_unknown_outputs_code_is_never_proven(self):
        # Contract: an unrecognized outputs action code stays conservative on the full-list path, as on the legacy
        # path. Bug: treating it as "no own failure" proved stages 9-14 despite a future/unknown failure.
        unknown = "OUTPUTS_NEW_WARNING"
        for hour, first, needs in ((6, unknown, ((unknown, "outputs", "action"),)),
                                   (7, APPROVAL, ((APPROVAL, "outputs", "action"), (unknown, "outputs", "action")))):
            with self.subTest(first=first):
                self.report(hour, first, needs=needs)
                rows = self.rows()
                for number in map(str, range(9, 16)):
                    row = rows[number]
                    self.assertEqual((row["basis"], row["last_proven_run"]), ("full-list", None), number)
                    self.assertNotEqual(row["state"], "proven", number)
                    self.assertIsNone(row["masked_by"], number)

    def test_external_and_missing_evidence_are_never_proven(self):
        # An umbrella ok cannot promote external stages, OCR from a ledger snapshot, the engine step or the findings lane.
        self.report(6, ocr=0)
        rows = self.rows()
        external = "external: not provable from run reports"
        self.assertEqual({key for key, row in rows.items() if row["state"] == external},
                         set("1 3 4 17 18 19 20 A1 A2 A3 A4 A5 A6 A7".split()))
        for stage, row in rows.items():
            if row["state"] == external or stage in ("6", "15", "16"):
                self.assertIsNone(row["last_proven_run"], stage)
        self.assertEqual(rows["16"]["state"], "not reported")
        self.report(7, mode="test", ocr=3)
        self.report(8, stub=True, ocr=4)
        self.assertIsNone(self.rows()["6"]["last_proven_run"])

    def test_engine_followup_stage_has_no_receipt_in_v1(self):
        # Contract: stage 15 is never proven by a bare outputs ok (older engines without the step emit the same
        # status). Bug: marking it proven would claim an engine step ran that the report cannot show.
        self.report(6)
        row = self.rows()["15"]
        self.assertEqual((row["state"], row["last_proven_run"], row["runs_since_last_proof"]),
                         (golive_stages.NO_RECEIPT, None, None))
        self.assertEqual(self.rows()["14"]["state"], "proven")  # neighbours keep their mapped evidence
        self.report(7, "OUTPUTS_FOLLOWUP_ENGINE_UNAVAILABLE")  # a failure is still reported
        row = self.rows()["15"]
        self.assertEqual((row["last_warning"], row["last_proven_run"]), ("OUTPUTS_FOLLOWUP_ENGINE_UNAVAILABLE", None))

    def test_zero_work_extract_is_not_extraction_evidence(self):
        # Contract: stage 5 needs documents actually extracted. Bug: run_stages.extract_result returns ok for zero
        # inputs or all-deferred OCR, so an idle run would be "proven".
        real = self.report(6)
        idle = self.report(7, extracted=0)
        row = self.rows()["5"]
        self.assertEqual((row["state"], row["last_proven_run"], row["runs_since_last_proof"]), ("not proven", real, 1))
        self.assertEqual(self.rows()["8"]["last_proven_run"], idle)  # unrelated stages unaffected
        self.assertIsNone(row["masked_by"])
        self.assertEqual(self.rows()["6"]["last_proven_run"], None)

    def test_ocr_snapshot_is_historical_presence_not_per_run_proof(self):
        # Contract: pages_ocr is a cumulative ledger count, so repeated idle runs must neither prove OCR nor move a
        # last-proven run or proof age. Bug: each idle run would reset the last proof from the same snapshot.
        self.report(6, ocr=2)
        self.report(7, ocr=2, extracted=0)
        row = self.rows()["6"]
        self.assertEqual((row["state"], row["last_proven_run"], row["runs_since_last_proof"]),
                         (golive_stages.HISTORICAL_OCR, None, None))
        self.report(8, ocr=0)
        self.assertEqual(self.rows()["6"]["state"], "not proven")
        self.report(9, "OUTPUTS_FOLLOWUP_ENGINE_UNAVAILABLE", ocr=2)
        self.assertEqual(self.rows()["6"]["state"], golive_stages.HISTORICAL_OCR)

    def test_report_finish_cutoff_compares_instants(self):
        # A missing cutoff accepts the future; >= rejects equality; wall-time comparison misreads offsets.
        run_id = self.report(6)
        finished = datetime(2026, 1, 1, 6, 7, 30, tzinfo=timezone.utc)
        tick = timedelta(microseconds=1)
        for clock_zone in (timezone.utc, timezone(timedelta(hours=5, minutes=30)),
                           timezone(-timedelta(hours=8)), None):
            for delta in (-tick, timedelta(0), tick):
                with self.subTest(clock_zone=clock_zone, delta=delta):
                    now = finished + delta
                    now = now.astimezone(clock_zone) if clock_zone is not None else now.replace(tzinfo=None)
                    if delta < timedelta(0):
                        with self.assertRaisesRegex(ValueError, "invalid report"):
                            golive_stages.load_reports(self.reports, now=now)
                    else:
                        reports = golive_stages.load_reports(self.reports, now=now)
                        self.assertEqual([report["run_id"] for report in reports], [run_id])

    def test_future_report_refuses_cli_proof_even_with_a_valid_sibling(self):
        # A bad-clock run must fail the whole CLI read, not prove drafts or be silently skipped.
        self.report(6)
        self.report(7, name="future.json")
        fixed = datetime(2026, 1, 1, 7, 7, 29, tzinfo=timezone.utc)
        with patch.object(golive_stages, "datetime", create=True) as date_type:
            date_type.now.side_effect = lambda tz: fixed.astimezone(tz)
            code, out, err = self.invoke("--json")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("'future.json': invalid report", err)

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
