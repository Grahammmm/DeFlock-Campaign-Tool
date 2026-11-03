"""DF1537-06: flock_requests.py open --from-email RUNDIR adds one open REQUESTS row from a recorded-sent email run."""
import contextlib
import hashlib
import importlib.util
import io
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from tests.flock.approval_field import APPROVAL_LINE, trusted, write_field
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
HEADER = "request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,source_sha256\n"
STEP_HEADER = "request_id,step,date,evidence,source_sha256,recorded_at\n"
HEAD = "DRAFT - not sent - G approval required"
NAME, ADDR, SUBJ, BODY = "Opname Zedward", "planted-local@city.example", "Subject: planted subject words", "planted body words"
LEAKS = (NAME, "Opname", ADDR, "planted", "city.example", "To:", "@")
SLUG, AGENCY, OTHER = "sample-city", "Sample City", "R-OLD,Sample City,2026-09-01,,,,,open,\n"
SENT = f"{NAME} 2026-10-03"


def load_tool():
    spec = importlib.util.spec_from_file_location("_requests_df1537_06", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL_MODULE = load_tool()


class OpenFromEmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        self.folder = self.store / "agencies" / SLUG
        self.req, self.log = self.folder / "REQUESTS.csv", self.folder / "REQUEST-STEPS.csv"

    def email(self, date="2026-10-02", sent=SENT, approve="2026-10-02 ref G", head=HEAD, folder=None):
        """A run folder with a synthetic EMAIL-DRAFT.md; sent=None leaves only FOLLOWUP-DRAFT.md."""
        run = (folder or self.folder) / "runs" / date
        run.mkdir(parents=True, exist_ok=True)
        (run / "FOLLOWUP-DRAFT.md").write_text(HEAD + "\n", encoding="utf-8")
        if sent is not None:
            text = [head, f"To: {ADDR}", SUBJ, "---", BODY, "---", APPROVAL_LINE,
                    f"Sent by: {sent or '____ (operator, by hand, after G' + chr(39) + 's reply)'}"]
            (run / "EMAIL-DRAFT.md").write_text("\n".join(text) + "\n", encoding="utf-8")
            if approve and approve.strip():  # the state field, never a line
                write_field(run)
        return run

    def requests(self, *rows):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.req.write_text(HEADER + "".join(rows), encoding="utf-8")

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), trusted():
            code = TOOL_MODULE.main([str(a) for a in argv])
        for leaked in LEAKS:
            self.assertNotIn(leaked, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def open(self, run, rid="R-1", today="2026-10-31"):
        return self.main("open", "--from-email", run, "--request-id", rid, *(("--today", today) if today else ()))

    def snap(self):
        return sorted((str(p.relative_to(self.tmp)), p.read_bytes() if p.is_file() and not p.is_symlink() else None)
                      for p in self.tmp.rglob("*"))

    def refused(self, why, run, **kw):
        before = self.snap()
        self.assertEqual(self.open(run, **kw), (1, "", f"open: {why}\n"))
        self.assertEqual(self.snap(), before)

    def test_forged_approval_with_correct_hash_never_opens_a_request(self):
        """Round-1 P1: a hand-written field with the right text hash and a fake signature opened a ledger row."""
        self.requests(OTHER)
        run = self.email()
        write_field(run, forged=True)
        self.refused("email stage is sent unverified, not sent recorded", run)
        self.assertFalse(self.log.exists())
        code, out, err = self.main("--check", self.store)
        self.assertEqual(code, 1)
        self.assertIn(f"{SLUG}: run 2026-10-02: sent, but the approval signature does not verify", out + err)
        write_field(run)  # the genuine signed control opens
        self.assertEqual(self.open(run)[0], 0)

    def test_sent_run_opens_one_row_and_one_step_then_check_is_clean(self):
        self.requests(OTHER)
        run = self.email()
        old = self.req.read_bytes()
        self.assertEqual(self.open(run), (0, f"opened | {SLUG} | R-1 | sent 2026-10-03 | run 2026-10-02\n", ""))
        self.assertEqual(self.req.read_bytes(), old + b"R-1,Sample City,2026-10-03,,,,,open,\n")  # other rows byte-identical
        digest = hashlib.sha256((run / "EMAIL-DRAFT.md").read_bytes()).hexdigest()
        self.assertEqual(self.log.read_text(encoding="utf-8"),
                         STEP_HEADER + f"R-1,opened,2026-10-03,email run 2026-10-02,{digest},2026-10-31\n")
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertEqual(self.main("--check", self.store),
                         (0, "requests=2 errors=0 sent_runs=1 matched=1\n", ""))

    def test_folder_without_requests_file_gets_v1_header_0600_and_slug_label(self):
        run = self.email()
        self.assertEqual(self.open(run, rid="Ab.9_x-1")[0], 0)
        self.assertEqual(self.req.read_text(encoding="utf-8"), HEADER + f"Ab.9_x-1,{SLUG},2026-10-03,,,,,open,\n")
        self.assertEqual(stat.S_IMODE(self.req.stat().st_mode), 0o600)
        self.assertEqual(self.main("--check", self.store)[0], 0)

    def test_steps_log_keeps_earlier_rows_and_record_still_follows(self):
        self.requests(OTHER)
        earlier = "R-OLD,receipt,2026-09-02,synthetic ack," + "c" * 64 + ",2026-09-03"  # no trailing newline
        self.log.write_text(STEP_HEADER + earlier, encoding="utf-8")
        self.assertEqual(self.open(self.email())[0], 0)
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[:2], [STEP_HEADER.strip(), earlier])
        self.assertTrue(lines[2].startswith("R-1,opened,2026-10-03,email run 2026-10-02,"), lines[2])
        code, out, _ = self.main("record", self.store, "--agency", SLUG, "--request", "R-1", "--step", "receipt",
                                 "--date", "2026-10-04", "--evidence", "synthetic ack", "--sha256", "d" * 64,
                                 "--today", "2026-10-31")
        self.assertEqual((code, out), (0, f"recorded | {SLUG} | R-1 | receipt | 2026-10-04\n"))

    def test_opened_is_not_settable_through_record(self):
        self.requests(OTHER)
        code, _, err = self.main("record", self.store, "--agency", SLUG, "--request", "R-OLD", "--step", "opened",
                                 "--date", "2026-10-04", "--evidence", "x y", "--sha256", "d" * 64)
        self.assertEqual((code, err.startswith("usage:")), (2, True))

    def test_every_stage_other_than_sent_recorded_is_refused_by_name(self):
        self.requests(OTHER)
        cases = (("2026-10-01", {"sent": None}, "follow-up drafted"), ("2026-10-02", {"sent": "", "approve": ""}, "email drafted"),
                 ("2026-10-03", {"sent": "", "approve": "ok"}, "approval recorded"),
                 ("2026-10-04", {"approve": ""}, "inconsistent: sent without approval"),
                 ("2026-10-05", {"head": "not the draft header"}, "malformed"))
        for date, kw, stage in cases:
            with self.subTest(stage=stage):
                self.refused(f"email stage is {stage}, not sent recorded", self.email(date, **kw))
        empty = self.folder / "runs" / "2026-10-06"
        empty.mkdir()
        self.refused("email stage is none, not sent recorded", empty)
        self.refused("RUNDIR missing or not a folder", self.folder / "runs" / "2026-10-07")

    def test_sent_date_missing_before_run_or_after_today_is_refused(self):
        self.requests(OTHER)
        for n, value in enumerate((NAME, f"{NAME} 2026-02-30", f"{NAME} 3 Oct 2026"), 1):
            with self.subTest(value=value):
                self.refused("sent date missing on the Sent by line", self.email(f"2026-10-0{n}", sent=value))
        self.refused("sent date 2026-10-03 is before the run date", self.email("2026-10-05"))
        self.refused("sent date 2026-10-03 is after --today", self.email("2026-10-02"), today="2026-10-02")
        self.assertEqual(self.open(self.email("2026-10-02"), today="2026-10-03")[0], 0)  # same day is fine

    def test_same_run_twice_is_refused_naming_the_first_id(self):
        self.requests(OTHER)
        run = self.email()
        self.assertEqual(self.open(run)[0], 0)
        self.refused("run already opened as R-1", run, rid="R-2")
        self.refused("run already opened as R-1", run)
        self.assertEqual(self.open(self.email("2026-10-03"), rid="R-2")[0], 0)  # another run of that agency opens

    def test_request_id_in_use_is_refused_and_bad_ids_are_usage_errors(self):
        self.requests(OTHER)
        run = self.email()
        self.refused(f"request R-OLD already exists in {SLUG}/REQUESTS.csv", run, rid="R-OLD")
        before = self.snap()
        for rid in ("", "-R1", ".R1", "R 1", "R/1", "R,1", "R1\n", "x" * 41, "R\u00e91"):
            with self.subTest(rid=rid):
                self.assertEqual(self.open(run, rid=rid)[0], 2)
        for argv in (["open", "--from-email", run], ["open", "--from-email", run, "--request-id", "R-1", "--today", "x"],
                     ["open", "--from-email", run, "--request-id", "R-1", "--request-id", "R-2"],
                     ["open", run, "--request-id", "R-1"], ["open", "--from-email", run, "--request-id", "R-1", "--x"]):
            self.assertEqual(self.main(*argv)[0], 2, argv)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.open(run, rid="x" * 40)[0], 0)

    def test_requests_or_steps_files_failing_their_checks_are_refused(self):
        self.requests(OTHER, OTHER)
        run = self.email()
        self.refused(f"REQUESTS.csv fails --check ({SLUG}: row 3: request_id is empty or duplicates an earlier row)", run)
        self.requests(OTHER)
        self.log.write_text("request_id,step\n", encoding="utf-8")
        self.refused("REQUEST-STEPS.csv does not match REQUEST-STEPS.csv v1", run)

    def test_links_and_wrong_shapes_are_refused(self):
        self.requests(OTHER)
        run, outside = self.email(), self.tmp / "outside"
        self.email(folder=outside / SLUG)
        shutil.copy(self.req, outside / "REQUESTS.csv")
        (run / "EMAIL-DRAFT.md").unlink()
        (run / "EMAIL-DRAFT.md").symlink_to(outside / SLUG / "runs" / "2026-10-02" / "EMAIL-DRAFT.md")
        self.refused("email stage is refused: link, not sent recorded", run)
        (self.folder / "runs" / "2026-10-03").symlink_to(outside / SLUG / "runs" / "2026-10-02")
        self.refused("2026-10-03 must not be a symlink", self.folder / "runs" / "2026-10-03")
        linked = self.store / "agencies" / "linked-town"
        linked.symlink_to(outside / SLUG, target_is_directory=True)
        self.refused("linked-town must not be a symlink", linked / "runs" / "2026-10-02")
        other = self.store / "agencies" / "other-town"
        other.mkdir()
        (other / "runs").symlink_to(outside / SLUG / "runs", target_is_directory=True)
        self.refused("runs must not be a symlink", other / "runs" / "2026-10-02")
        self.req.rename(self.folder / "real.csv")
        self.req.symlink_to(outside / "REQUESTS.csv")
        good = self.email("2026-10-01")
        self.refused("REQUESTS.csv must not be a symlink", good)
        self.req.unlink()
        self.log.symlink_to(outside / "REQUESTS.csv")
        self.refused("REQUEST-STEPS.csv must not be a symlink", good)
        self.log.unlink()
        for bad in (self.folder / "runs", self.tmp / "x" / "runs" / "2026-10-01", self.folder / "runs" / "notes"):
            self.refused("RUNDIR must be <store>/agencies/<slug>/runs/<YYYY-MM-DD>", bad)
        upper = self.store / "agencies" / "Sample_City"
        self.refused("agency folder name must be a slug", self.email(folder=upper))
        with mock.patch.object(TOOL_MODULE, "REPO", self.tmp):
            self.refused("store is inside the repository", good)

    def test_agencies_folder_link_is_refused(self):
        self.email(folder=self.tmp / "real" / "agencies" / SLUG)
        self.store.mkdir()
        (self.store / "agencies").symlink_to(self.tmp / "real" / "agencies", target_is_directory=True)
        self.refused("agencies must not be a symlink", self.store / "agencies" / SLUG / "runs" / "2026-10-02")

    def test_crash_between_the_writes_rolls_the_requests_row_back(self):
        real = TOOL_MODULE.replace

        def broken(path, text):
            if path.name == "REQUEST-STEPS.csv":
                raise OSError("synthetic crash")
            return real(path, text)

        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.requests(OTHER)
                run, before = self.email(), self.snap()
                with mock.patch.object(TOOL_MODULE, "replace", broken):
                    code, out, err = self.open(run)
                self.assertEqual((code, out, err), (1, "", "open: cannot write in agency folder (OSError)\n"))
                self.assertEqual(self.snap(), before)
                self.assertEqual(self.req.exists(), existing)


if __name__ == "__main__":
    unittest.main()
