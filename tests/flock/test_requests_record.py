"""NB-DF-04: flock_requests.py record sets one dated, sourced step on a request; regressions refused, nothing written."""
import contextlib
import importlib.util
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
HEADER = ("request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,"
          "source_sha256")
STEP_HEADER = "request_id,step,date,evidence,source_sha256,recorded_at"
SLUG, AGENCY = "sample-city-police-department", "Sample City Police Department"
HEX, EVIDENCE = "c" * 64, "synthetic acknowledgment message"
OPEN = f"SAMPLE-1,{AGENCY},2026-01-02,,,,,open,"
WITHDRAWN = f"SAMPLE-2,{AGENCY},2026-01-02,,,,,withdrawn,"


def load_tool():
    spec = importlib.util.spec_from_file_location("_requests_nb_df_04", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL_MODULE = load_tool()


class RecordTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.folder = self.store / "agencies" / SLUG
        self.folder.mkdir(parents=True)
        self.req, self.log = self.folder / "REQUESTS.csv", self.folder / "REQUEST-STEPS.csv"
        self.req.write_text("\n".join((HEADER, OPEN, WITHDRAWN)) + "\n", encoding="utf-8")

    def rec(self, step, when, rid="SAMPLE-1", ev=EVIDENCE, sha=HEX, today="2026-10-31", agency=SLUG, store=None,
            extra=()):
        argv = ["record", str(store or self.store), "--agency", agency, "--request", rid, "--step", step,
                "--date", when, "--evidence", ev, "--sha256", sha, *(("--today", today) if today else ()), *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = TOOL_MODULE.main(argv)
        self.assertNotIn(ev if len(ev) > 3 else "\0", out.getvalue())  # evidence text never on stdout
        return code, out.getvalue(), err.getvalue()

    def ok(self, step, when, word="recorded", **kw):
        code, out, err = self.rec(step, when, **kw)
        self.assertEqual((code, out, err), (0, f"{word} | {SLUG} | {kw.get('rid', 'SAMPLE-1')} | {step} | {when}\n", ""))

    def snap(self):
        return [p.read_bytes() if p.exists() else None for p in (self.req, self.log)]

    def refused(self, message, step, when, **kw):
        before = self.snap()
        code, out, err = self.rec(step, when, **kw)
        self.assertEqual((code, out), (1, ""), err)
        self.assertEqual(err, f"record: {message}\n")
        self.assertEqual(self.snap(), before, "files changed on a refusal")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir() if p.name.startswith(".")), [])

    def row(self):
        return self.req.read_text(encoding="utf-8").splitlines()[1]

    def test_full_lifecycle_in_order(self):
        steps = (("receipt", "2026-01-05"), ("extension", "2026-01-12"), ("determination", "2026-01-20"),
                 ("produced", "2026-02-01"), ("closed", "2026-02-10"))
        for step, when in steps:
            self.ok(step, when)
        self.assertEqual(self.req.read_text(encoding="utf-8").splitlines(), [
            HEADER, f"SAMPLE-1,{AGENCY},2026-01-02,{EVIDENCE},2026-01-05,2026-01-20,2026-01-12,closed,{HEX}", WITHDRAWN])
        self.assertEqual(self.log.read_text(encoding="utf-8").splitlines(),
                         [STEP_HEADER] + [f"SAMPLE-1,{s},{w},{EVIDENCE},{HEX},2026-10-31" for s, w in steps])
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        code, out, _ = self.rec("receipt", "2026-01-05")  # replay after close: unchanged, never a new line
        self.assertEqual((code, out), (0, f"unchanged | {SLUG} | SAMPLE-1 | receipt | 2026-01-05\n"))
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 6)

    def test_status_only_step_on_open_keeps_fields_and_today_defaults(self):
        self.ok("produced", "2026-01-09", today=None)
        self.assertEqual(self.row(), f"SAMPLE-1,{AGENCY},2026-01-02,,,,,produced,")
        self.assertTrue(self.log.read_text(encoding="utf-8").splitlines()[1].startswith("SAMPLE-1,produced,2026-01-09,"))

    def test_order_regression_refused(self):
        self.ok("receipt", "2026-01-10")
        self.refused("order regression (determination_date is before the date it must follow)", "determination",
                     "2026-01-08")
        self.refused("order regression (extension_notice_date is before the date it must follow)", "extension",
                     "2026-01-09")
        self.ok("extension", "2026-01-12")
        self.refused("order regression (--date is before the sent date or an earlier step)", "produced", "2026-01-11")
        self.req.write_text("\n".join((HEADER, OPEN)) + "\n", encoding="utf-8")
        self.log.unlink()
        self.refused("order regression (receipt_date is before the date it must follow)", "receipt", "2026-01-01")
        self.refused("order regression (--date is before the sent date or an earlier step)", "closed", "2026-01-01")

    def test_status_regression_and_terminal_status_refused(self):
        self.ok("produced", "2026-01-09")
        self.refused("status regression (produced to determined)", "determination", "2026-01-10")
        for step in ("receipt", "extension", "determination", "produced", "closed"):
            self.refused("request is withdrawn; no step can follow", step, "2026-01-10", rid="SAMPLE-2")
        self.ok("closed", "2026-01-11")
        self.refused("request is closed; no step can follow", "produced", "2026-01-12")
        self.refused("request is closed; no step can follow", "receipt", "2026-01-12")

    def test_same_values_unchanged_different_values_refused(self):
        self.ok("receipt", "2026-01-05")
        before = self.snap()
        self.ok("receipt", "2026-01-05", word="unchanged", today="2026-10-30")  # recorded_at alone is not a change
        self.assertEqual(self.snap(), before)
        self.refused("receipt_date is already set to a different value", "receipt", "2026-01-06")
        self.refused("receipt_evidence is already set to a different value", "receipt", "2026-01-05", ev="other text")
        self.refused("source_sha256 is already set to a different value", "receipt", "2026-01-05", sha="d" * 64)
        self.ok("determination", "2026-01-20")
        self.refused("determination_date is already set to a different value", "determination", "2026-01-21")
        self.ok("produced", "2026-02-01")
        self.ok("produced", "2026-02-01", word="unchanged")
        self.refused("step produced is already recorded with different values", "produced", "2026-02-02")
        self.refused("step produced is already recorded with different values", "produced", "2026-02-01", ev="other")

    def test_unknown_request_agency_and_check_error_named(self):
        self.refused(f"request NOPE-9 not found in {SLUG}/REQUESTS.csv", "receipt", "2026-01-05", rid="NOPE-9")
        self.refused("agency folder other-agency not found", "receipt", "2026-01-05", agency="other-agency")
        for agency in ("../agencies/" + SLUG, "Sample City", ""):
            self.refused("--agency must be a folder slug", "receipt", "2026-01-05", agency=agency)
        (self.store / "agencies" / "empty-agency").mkdir()
        self.refused("agency folder empty-agency has no REQUESTS.csv", "receipt", "2026-01-05", agency="empty-agency")
        self.req.write_text("\n".join((HEADER, OPEN.replace(",open,", ",pending,"))) + "\n", encoding="utf-8")
        self.refused(f"REQUESTS.csv fails --check ({SLUG}: row 2: status must be one of closed, determined, open, "
                     "produced, withdrawn)", "receipt", "2026-01-05")
        self.req.write_text("\n".join((HEADER, OPEN.replace(AGENCY, "Other Agency"))) + "\n", encoding="utf-8")
        self.refused(f"REQUESTS.csv fails --check ({SLUG}: row 2: agency is empty or does not match its folder)",
                     "receipt", "2026-01-05")

    def test_bad_steps_file_refused(self):
        self.log.write_text("request_id,step,date\n", encoding="utf-8")
        self.refused("REQUEST-STEPS.csv does not match REQUEST-STEPS.csv v1", "receipt", "2026-01-05")
        self.log.write_text(STEP_HEADER + "\nSAMPLE-1,receipt\n", encoding="utf-8")
        self.refused("REQUEST-STEPS.csv does not match REQUEST-STEPS.csv v1", "receipt", "2026-01-05")
        self.log.write_bytes(b"\xff\xfe")
        self.refused("REQUEST-STEPS.csv unreadable (UnicodeDecodeError)", "receipt", "2026-01-05")

    def test_steps_file_without_final_newline_is_appended_cleanly(self):
        self.log.write_text(STEP_HEADER + f"\nSAMPLE-2,receipt,2026-01-03,{EVIDENCE},{HEX},2026-01-04", encoding="utf-8")
        self.ok("receipt", "2026-01-05")
        self.assertEqual(self.log.read_text(encoding="utf-8").splitlines()[2],
                         f"SAMPLE-1,receipt,2026-01-05,{EVIDENCE},{HEX},2026-10-31")

    def test_symlinks_and_store_in_repo_refused(self):
        outside = self.store.parent / "outside"
        outside.mkdir()
        target = outside / "REQUESTS.csv"
        target.write_bytes(self.req.read_bytes())
        self.req.unlink()
        self.req.symlink_to(target)
        self.refused("REQUESTS.csv must not be a symlink", "receipt", "2026-01-05")
        self.assertEqual(target.read_text(encoding="utf-8").splitlines()[1], OPEN)
        self.req.unlink()
        self.req.write_bytes(target.read_bytes())
        steps = outside / "STEPS.csv"
        steps.write_text(STEP_HEADER + "\n", encoding="utf-8")
        self.log.symlink_to(steps)
        self.refused("REQUEST-STEPS.csv must not be a symlink", "receipt", "2026-01-05")
        self.assertEqual(steps.read_text(encoding="utf-8"), STEP_HEADER + "\n")
        self.log.unlink()
        linked = self.store / "agencies" / "linked-agency"
        linked.symlink_to(self.folder, target_is_directory=True)
        self.refused("linked-agency must not be a symlink", "receipt", "2026-01-05", agency="linked-agency")
        with mock.patch.object(TOOL_MODULE, "REPO", self.store.parent):
            self.refused("store is inside the repository", "receipt", "2026-01-05")
        self.refused("store is inside the repository", "receipt", "2026-01-05", store=ROOT)

    def test_input_validation_refused(self):
        for ev in ("line one\nline two", "line one\rline two", "x" * 121, "", "   "):
            self.refused("--evidence must be 1-120 characters on one line", "receipt", "2026-01-05", ev=ev)
        for sha in (HEX.upper(), "c" * 63, "g" * 64, ""):
            self.refused("--sha256 must be 64 lowercase hex", "receipt", "2026-01-05", sha=sha)
        for when in ("2026-1-5", "2026-02-30", "yesterday"):
            self.refused("--date must be YYYY-MM-DD", "receipt", when)
        self.refused("--date is after --today", "receipt", "2026-11-01")
        self.ok("receipt", "2026-01-05", ev="y" * 120)

    def test_usage_errors_exit_2(self):
        cases = [(("--today", "2026-10-31"), "2026-10-31", "receipt"), (("--note", "x"), None, "receipt"),
                 (("--agency",), None, "receipt"), ((), "31/10/2026", "receipt"), ((), "2026-10-31", "granted")]
        for extra, today, step in cases:
            code, out, err = self.rec(step, "2026-01-05", today=today, extra=extra)
            self.assertEqual((code, out, err.startswith("usage: flock_requests.py --check PATH")), (2, "", True), extra)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):  # a required flag missing
            code = TOOL_MODULE.main(["record", str(self.store), "--agency", SLUG, "--request", "SAMPLE-1",
                                     "--step", "receipt", "--date", "2026-01-05", "--evidence", EVIDENCE])
        self.assertEqual((code, err.getvalue().startswith("usage: ")), (2, True))
        self.assertEqual(self.snap()[1], None)

    def test_write_failure_leaves_both_files_unchanged(self):
        self.ok("receipt", "2026-01-05")
        real, calls = os.replace, []

        def second_fails(src, dst):
            calls.append(Path(dst).name)
            if len(calls) == 2:
                raise OSError("injected")
            return real(src, dst)

        for fake in (mock.Mock(side_effect=OSError("injected")), second_fails):
            with mock.patch.object(TOOL_MODULE.os, "replace", fake):
                self.refused("cannot write in agency folder (OSError)", "extension", "2026-01-12")
        self.assertEqual(calls, ["REQUESTS.csv", "REQUEST-STEPS.csv"])
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
