"""RF1520-2: flock_requests.py show STORE --agency SLUG --request ID [--as-of D] prints one request's row, logged steps,
fees and provisional reminder, names log/row disagreements (exit 3) and writes nothing."""
import contextlib
import hashlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
HEADER = ("request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,"
          "source_sha256")
STEP_HEADER = "request_id,step,date,evidence,source_sha256,recorded_at"
SLUG, AGENCY = "sample-city-police-department", "Sample City Police Department"
SENTINEL = "SENTINEL-show-evidence-text"
HEX = hashlib.sha256(b"synthetic receipt source").hexdigest()
FEE_HEX = hashlib.sha256(b"synthetic fee source").hexdigest()
ROWS = (f"SAMPLE-S1,{AGENCY},2026-03-02,,,,,open,", f"SAMPLE-S2,{AGENCY},2026-03-02,,,,,open,")


def load_tool():
    spec = importlib.util.spec_from_file_location("_requests_rf1520_2", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL_MODULE = load_tool()


class ShowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.folder = self.store / "agencies" / SLUG
        self.folder.mkdir(parents=True)
        self.req, self.log = self.folder / "REQUESTS.csv", self.folder / "REQUEST-STEPS.csv"
        self.req.write_text("\n".join((HEADER,) + ROWS) + "\n", encoding="utf-8")

    def run_tool(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = TOOL_MODULE.main(argv)
        self.assertNotIn(SENTINEL, out.getvalue() + err.getvalue())  # evidence text is never printed
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        return {p: (p.is_symlink(), p.read_bytes() if p.is_file() else None) for p in sorted(self.store.rglob("*"))}

    def show(self, *extra, rid="SAMPLE-S1", agency=SLUG, store=None):
        before = self.snapshot()
        got = self.run_tool(["show", str(store or self.store), "--agency", agency, "--request", rid, *extra])
        self.assertEqual(self.snapshot(), before, "show changed the store")  # read-only, no temp files left
        return got

    def record(self, step, when, rid="SAMPLE-S1"):
        code, _, err = self.run_tool(["record", str(self.store), "--agency", SLUG, "--request", rid, "--step", step,
                                      "--date", when, "--evidence", SENTINEL, "--sha256", HEX, "--today", "2026-10-31"])
        self.assertEqual((code, err), (0, ""))

    def fee(self, kind, amount, when):
        code, _, err = self.run_tool(["fees", "record", str(self.store), "--agency", SLUG, "--request", "SAMPLE-S1",
                                      "--kind", kind, "--amount-cents", str(amount), "--date", when,
                                      "--evidence", SENTINEL, "--sha256", FEE_HEX, "--today", "2026-10-31"])
        self.assertEqual((code, err), (0, ""))

    def edit_row(self, old, new):
        self.req.write_text(self.req.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")

    def row(self, rid="SAMPLE-S1"):
        rows, errs = TOOL_MODULE.load(self.req, SLUG)
        self.assertEqual(errs, [])
        return next(r for r in rows if r["request_id"] == rid)

    def test_recorded_request_steps_fees_reminder(self):  # edge 1
        self.record("receipt", "2026-03-04")
        self.record("extension", "2026-03-20")
        self.fee("estimate", 2500, "2026-03-10")
        self.fee("invoice", 3000, "2026-03-21")
        reminder = TOOL_MODULE.due([self.row()])[:-1]
        self.assertEqual(len(reminder), 1)
        self.assertIn("extension notice", reminder[0])
        code, out, err = self.show()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            f"request | {SLUG} | SAMPLE-S1 | status=open sent=2026-03-02 receipt=2026-03-04 determination=- "
            f"extension_notice=2026-03-20 source={HEX[:12]}",
            f"step | SAMPLE-S1 | receipt | 2026-03-04 | source={HEX[:12]} | recorded 2026-10-31",
            f"step | SAMPLE-S1 | extension | 2026-03-20 | source={HEX[:12]} | recorded 2026-10-31",
            "fee | SAMPLE-S1 | estimate | 2500 | 2026-03-10",
            "fee | SAMPLE-S1 | invoice | 3000 | 2026-03-21",
            "fees | SAMPLE-S1 | estimate=2500 invoice=3000 paid=0",
            *reminder,
            "show | log_mismatch=0 unlogged=0 | read-only"])

    def test_sent_only_no_log_no_fees(self):  # edge 2
        code, out, err = self.show(rid="SAMPLE-S2")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            f"request | {SLUG} | SAMPLE-S2 | status=open sent=2026-03-02 receipt=- determination=- extension_notice=- "
            "source=-",
            "fees | SAMPLE-S2 | none",
            f"provisional reminder | SAMPLE-S2 | {AGENCY} | receipt unknown | no date computed",
            "show | log_mismatch=0 unlogged=0 | read-only"])

    def test_closed_request_has_no_reminder(self):
        self.req.write_text(f"{HEADER}\nSAMPLE-S1,{AGENCY},2026-03-02,,,,,closed,\n", encoding="utf-8")
        code, out, _ = self.show()
        self.assertEqual(code, 0)
        self.assertNotIn("provisional reminder", out)

    def assert_mismatch(self, code_name, field):
        code, out, err = self.show()
        self.assertEqual((code, err), (3, ""))
        lines = out.splitlines()
        self.assertIn(f"mismatch | SAMPLE-S1 | {code_name} | {field}", lines)
        self.assertEqual(lines[-1], "show | log_mismatch=1 unlogged=0 | read-only")

    def test_hand_cleared_receipt_is_step_not_on_row(self):  # edge 3
        self.record("receipt", "2026-03-04")
        self.edit_row(f"{SENTINEL},2026-03-04,", ",,")
        self.edit_row(f",open,{HEX}", ",open,")
        self.assert_mismatch("step_not_on_row", "receipt_date")

    def test_hand_changed_date_is_row_differs(self):  # edge 3
        self.record("receipt", "2026-03-04")
        self.edit_row(f"{SENTINEL},2026-03-04,", f"{SENTINEL},2026-03-05,")
        self.assert_mismatch("row_differs", "receipt_date")

    def test_status_regressed_after_produced_is_status_behind(self):  # edge 3
        self.record("produced", "2026-03-30")
        self.edit_row(",produced,", ",open,")
        self.assert_mismatch("status_behind", "status")

    def test_status_ahead_of_log_is_not_behind(self):
        self.record("produced", "2026-03-30")
        self.edit_row(",produced,", ",closed,")
        code, out, _ = self.show()
        self.assertEqual(code, 0)
        self.assertNotIn("mismatch |", out)

    def test_hand_set_determination_is_unlogged_only(self):  # edge 3
        self.edit_row("SAMPLE-S1,{0},2026-03-02,,,,,open,".format(AGENCY),
                      "SAMPLE-S1,{0},2026-03-02,,,2026-03-12,,determined,".format(AGENCY))
        code, out, err = self.show()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-2:], ["unlogged | SAMPLE-S1 | determination_date",
                                                 "show | log_mismatch=0 unlogged=1 | read-only"])

    def test_other_requests_log_lines_ignored(self):
        self.record("receipt", "2026-03-04", rid="SAMPLE-S2")
        code, out, _ = self.show()
        self.assertEqual(code, 0)
        self.assertNotIn("step |", out)

    def test_steps_sorted_by_date_then_file_order(self):
        self.log.write_text("\n".join((STEP_HEADER, f"SAMPLE-S2,produced,2026-03-30,{SENTINEL},{HEX},2026-10-02",
                                       f"SAMPLE-S2,closed,2026-03-30,{SENTINEL},{HEX},2026-10-03",
                                       f"SAMPLE-S2,opened,2026-03-02,email run 2026-03-02,{HEX},2026-10-01")) + "\n",
                            encoding="utf-8")
        code, out, _ = self.show(rid="SAMPLE-S2")
        self.assertEqual(code, 3)  # the row is still open: status_behind
        self.assertEqual([ln.split(" | ")[2] for ln in out.splitlines() if ln.startswith("step |")],
                         ["opened", "produced", "closed"])
        self.assertEqual(out.splitlines()[-2:], ["mismatch | SAMPLE-S2 | status_behind | status",
                                                 "show | log_mismatch=1 unlogged=0 | read-only"])

    def test_as_of_adds_distance_cell(self):  # edge 4
        code, out, _ = self.show("--as-of", "2026-03-12", rid="SAMPLE-S2")
        self.assertEqual(code, 0)
        self.assertIn(f"provisional reminder | SAMPLE-S2 | {AGENCY} | receipt unknown | no date computed"
                      " | as of 2026-03-12: sent 10 days ago", out.splitlines())

    def test_bad_usage_exit_2(self):  # edge 4
        base = ["show", str(self.store)]
        for argv in (base + ["--agency", SLUG, "--request", "SAMPLE-S1", "--as-of", "2026-02-30"],
                     base + ["--agency", SLUG, "--agency", SLUG, "--request", "SAMPLE-S1"],
                     base + ["--agency", SLUG],
                     base + ["--agency", SLUG, "--request", "SAMPLE-S1", "--today", "2026-03-12"],
                     base + ["--agency", SLUG, "--request"],
                     ["show"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_tool(argv), (2, "", TOOL_MODULE.USAGE + "\n"))

    def refused(self, message, **kw):
        self.assertEqual(self.show(**kw), (1, "", f"show: {message}\n"))

    def test_refusals_named_exit_1(self):  # edge 5
        self.refused(f"request SAMPLE-NOPE not found in {SLUG}/REQUESTS.csv", rid="SAMPLE-NOPE")
        self.refused("agency folder unknown-town not found", agency="unknown-town")
        self.refused("--agency must be a folder slug", agency="Sample City")
        self.refused("store is inside the repository", store=ROOT)

    def test_symlinked_agency_folder_refused(self):  # edge 5
        os.symlink(self.folder, self.store / "agencies" / "linked-town")
        self.refused("linked-town must not be a symlink", agency="linked-town")

    def test_symlinked_steps_log_refused(self):  # edge 5
        real = self.store / "elsewhere.csv"
        real.write_text(STEP_HEADER + "\n", encoding="utf-8")
        os.symlink(real, self.log)
        self.refused("REQUEST-STEPS.csv must not be a symlink")

    def test_bad_log_or_fees_header_refused(self):
        self.log.write_text("request_id,step\n", encoding="utf-8")
        self.refused("REQUEST-STEPS.csv unreadable")
        self.log.write_text(STEP_HEADER + "\n", encoding="utf-8")
        (self.folder / "FEES.csv").write_text("kind,amount\n", encoding="utf-8")
        self.refused("FEES.csv unreadable")
        (self.folder / "FEES.csv").write_text(
            f"request_id,kind,amount_cents,date,evidence,source_sha256,recorded_at\nSAMPLE-S1,estimate,1.50,2026-03-10,"
            f"{SENTINEL},{FEE_HEX},2026-10-31\n", encoding="utf-8")
        self.refused("FEES.csv fails fees check (FEES.csv row 2: amount_cents must be an integer 0-10000000)")


if __name__ == "__main__":
    unittest.main()
