"""DF1537-07: flock_requests.py fees record appends sourced fee rows to private FEES.csv; fees check names requests over
the organizer's cap (required, no default) and never decides a payment."""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
HEADER = ("request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,"
          "source_sha256")
FEE_HEADER = "request_id,kind,amount_cents,date,evidence,source_sha256,recorded_at"
SLUG, AGENCY = "sample-town-police-department", "Sample Town Police Department"
HEX, SENTINEL = "e" * 64, "SENTINEL-fee-evidence-text"
ROWS = (f"SAMPLE-F1,{AGENCY},2026-03-02,,,,,open,", f"SAMPLE-F2,{AGENCY},2026-03-02,,,,,closed,",
        f"SAMPLE-F3,{AGENCY},2026-03-02,,,,,withdrawn,", f"SAMPLE-F4,{AGENCY},2026-03-02,,,2026-03-09,,determined,")
LAST = "| not a payment decision; G decides any spending"


def load_tool():
    spec = importlib.util.spec_from_file_location("_requests_df1537_07", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL_MODULE = load_tool()


class FeesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.folder = self.store / "agencies" / SLUG
        self.folder.mkdir(parents=True)
        self.req, self.fees = self.folder / "REQUESTS.csv", self.folder / "FEES.csv"
        self.req.write_text("\n".join((HEADER,) + ROWS) + "\n", encoding="utf-8")

    def run_tool(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = TOOL_MODULE.main(argv)
        self.assertNotIn(SENTINEL, out.getvalue() + err.getvalue())  # evidence text is never printed
        return code, out.getvalue(), err.getvalue()

    def rec(self, kind, amount, when, rid="SAMPLE-F1", sha=HEX, today="2026-10-31", agency=SLUG, store=None):
        return self.run_tool(["fees", "record", str(store or self.store), "--agency", agency, "--request", rid,
                              "--kind", kind, "--amount-cents", str(amount), "--date", when, "--evidence", SENTINEL,
                              "--sha256", sha, "--today", today])

    def ok(self, kind, amount, when, rid="SAMPLE-F1", word="fee", **kw):
        self.assertEqual(self.rec(kind, amount, when, rid=rid, **kw), (0, f"{word} | {SLUG} | {rid} | {kind} | {amount} | {when}\n", ""))

    def refused(self, message, kind, amount, when, **kw):
        before = self.fees.read_bytes() if self.fees.exists() else None
        self.assertEqual(self.rec(kind, amount, when, **kw), (1, "", f"fees: {message}\n"))
        self.assertEqual(self.fees.read_bytes() if self.fees.exists() else None, before, "FEES.csv changed on a refusal")
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.startswith(".")], [])

    def check(self, *cap):
        return self.run_tool(["fees", "check", str(self.store), *(("--cap-cents", *cap) if cap else ())])

    def test_estimate_invoice_paid_rows_and_cap_ok(self):  # edge 1
        for kind, amount, when in (("estimate", 2500, "2026-03-10"), ("invoice", 3000, "2026-03-20"), ("paid", 3000, "2026-03-25")):
            self.ok(kind, amount, when)
        self.assertEqual(self.fees.read_text(encoding="utf-8").splitlines(), [FEE_HEADER] + [
            f"SAMPLE-F1,{k},{a},{w},{SENTINEL},{HEX},2026-10-31" for k, a, w in
            (("estimate", 2500, "2026-03-10"), ("invoice", 3000, "2026-03-20"), ("paid", 3000, "2026-03-25"))])
        self.assertEqual(self.fees.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.check("5000"), (0, f"fees | {SLUG} | SAMPLE-F1 | estimate=2500 invoice=3000 paid=3000 | ok\n"
                                                 f"fees | requests=1 over_cap=0 cap_cents=5000 {LAST}\n", ""))

    def test_over_cap_named_exit_3_closed_not_counted(self):  # edge 2
        self.ok("estimate", 2500, "2026-03-10")
        self.ok("invoice", 3000, "2026-03-20")
        self.ok("invoice", 9000, "2026-03-20", rid="SAMPLE-F2")  # closed: shown, never counted
        self.ok("estimate", 2001, "2026-03-11", rid="SAMPLE-F4")  # determined: counted on its estimate
        self.assertEqual(self.check("2000"), (3, f"fees | {SLUG} | SAMPLE-F1 | estimate=2500 invoice=3000 paid=0 | over cap\n"
                                                 f"fees | {SLUG} | SAMPLE-F2 | estimate=- invoice=9000 paid=0 | ok\n"
                                                 f"fees | {SLUG} | SAMPLE-F4 | estimate=2001 invoice=- paid=0 | over cap\n"
                                                 f"fees | requests=3 over_cap=2 cap_cents=2000 {LAST}\n", ""))
        self.assertEqual(self.check("3000")[0], 0)  # equal to the cap is not over it

    def test_latest_by_date_then_file_order(self):  # edge 3
        self.ok("estimate", 4000, "2026-03-15")
        self.ok("estimate", 1000, "2026-03-10")  # earlier date, later row: not the latest
        self.assertIn("estimate=4000 invoice=-", self.check("5000")[1])
        self.ok("estimate", 1500, "2026-03-15")  # same date, later row: the latest
        self.assertIn("estimate=1500 invoice=-", self.check("5000")[1])
        self.ok("invoice", 6000, "2026-03-16")
        self.ok("invoice", 1200, "2026-03-16")  # the latest invoice (else estimate) is compared with the cap
        self.assertEqual(self.check("1300")[0], 0)

    def test_cap_required_no_default(self):  # edge 4
        self.ok("estimate", 999999, "2026-03-10")
        for cap in ((), ("-1",), ("12.5",), ("",), ("1e3",), (" 5",)):
            code, out, err = self.check(*cap)
            self.assertEqual((code, out, err.startswith("usage: flock_requests.py --check PATH")), (2, "", True), cap)
        for argv in (["fees", "check", str(self.store), "--cap", "5"], ["fees", "check", str(self.store), "--cap-cents", "5", "x"],
                     ["fees", "check"], ["fees"], ["fees", "pay", str(self.store)]):
            self.assertEqual(self.run_tool(argv)[0], 2, argv)
        self.assertEqual(self.check("0")[0], 3)

    def test_paid_needs_earlier_invoice_and_paid_over_invoice(self):  # edge 5
        self.refused("paid needs an earlier invoice row for request SAMPLE-F1", "paid", 100, "2026-03-25")
        self.ok("estimate", 3000, "2026-03-10")
        self.refused("paid needs an earlier invoice row for request SAMPLE-F1", "paid", 100, "2026-03-25")  # estimate only
        self.ok("invoice", 3000, "2026-03-20")
        self.refused("paid needs an earlier invoice row for request SAMPLE-F1", "paid", 100, "2026-03-19")  # dated before
        self.ok("paid", 4000, "2026-03-25")
        self.assertEqual(self.check("5000"), (0, f"fees | {SLUG} | SAMPLE-F1 | estimate=3000 invoice=3000 paid=4000 | paid over"
                                                 f" invoice\nfees | requests=1 over_cap=0 cap_cents=5000 {LAST}\n", ""))

    def test_amount_date_and_withdrawn_refused(self):  # edge 6
        for amount in ("12.50", "-5", "+5", "10000001", "$40", "40 USD", "007", "", "1,000"):
            self.refused("--amount-cents must be an integer 0-10000000", "estimate", amount, "2026-03-10")
        self.ok("estimate", 10000000, "2026-03-10")
        self.ok("estimate", 0, "2026-03-11")
        self.refused("--date is after --today", "estimate", 10, "2026-11-01")
        self.refused("--date is before the request's sent date", "estimate", 10, "2026-03-01")
        for when in ("2026-3-10", "2026-02-30", "soon"):
            self.refused("--date must be YYYY-MM-DD", "estimate", 10, when)
        self.refused("request SAMPLE-F3 is withdrawn", "estimate", 10, "2026-03-10", rid="SAMPLE-F3")
        self.refused("--sha256 must be 64 lowercase hex", "estimate", 10, "2026-03-10", sha=HEX.upper())
        self.ok("estimate", 10, "2026-03-10", rid="SAMPLE-F2")  # closed is not withdrawn
        code, _, err = self.run_tool(["fees", "record", str(self.store), "--agency", SLUG, "--request", "SAMPLE-F1",
                                      "--kind", "refund", "--amount-cents", "1", "--date", "2026-03-10",
                                      "--evidence", SENTINEL, "--sha256", HEX])
        self.assertEqual((code, err.startswith("usage: ")), (2, True))

    def test_identical_unchanged_and_unknown_request(self):  # edge 7
        self.ok("estimate", 2500, "2026-03-10")
        before = self.fees.read_bytes()
        self.ok("estimate", 2500, "2026-03-10", word="unchanged", today="2026-10-30")  # recorded_at alone is no change
        self.assertEqual(self.fees.read_bytes(), before)
        self.ok("estimate", 2500, "2026-03-10", sha="f" * 64)  # another source is a new row
        self.ok("invoice", 2500, "2026-03-10")  # another kind is a new row
        self.assertEqual(len(self.fees.read_text(encoding="utf-8").splitlines()), 4)
        self.refused(f"request NOPE-1 not found in {SLUG}/REQUESTS.csv", "estimate", 1, "2026-03-10", rid="NOPE-1")
        self.req.write_text("\n".join((HEADER, ROWS[0].replace(",open,", ",pending,"))) + "\n", encoding="utf-8")
        self.refused(f"REQUESTS.csv fails --check ({SLUG}: row 2: status must be one of closed, determined, open, produced,"
                     " withdrawn)", "estimate", 1, "2026-03-10")

    def test_symlinks_and_store_in_repo_refused(self):  # edge 8
        outside = self.store.parent / "outside"
        outside.mkdir()
        target = outside / "FEES.csv"
        target.write_text(FEE_HEADER + "\n", encoding="utf-8")
        self.fees.symlink_to(target)
        self.refused("FEES.csv must not be a symlink", "estimate", 1, "2026-03-10")
        self.assertEqual(target.read_text(encoding="utf-8"), FEE_HEADER + "\n")
        self.fees.unlink()
        (self.store / "agencies" / "linked-agency").symlink_to(self.folder, target_is_directory=True)
        self.refused("linked-agency must not be a symlink", "estimate", 1, "2026-03-10", agency="linked-agency")
        real = outside / "REQUESTS.csv"
        real.write_bytes(self.req.read_bytes())
        self.req.unlink()
        self.req.symlink_to(real)
        self.refused("REQUESTS.csv must not be a symlink", "estimate", 1, "2026-03-10")
        self.req.unlink()
        self.req.write_bytes(real.read_bytes())
        with mock.patch.object(TOOL_MODULE, "REPO", self.store.parent):
            self.refused("store is inside the repository", "estimate", 1, "2026-03-10")
        self.refused("store is inside the repository", "estimate", 1, "2026-03-10", store=ROOT)
        self.assertFalse(self.fees.exists())

    def test_bad_fees_file_named_by_check_and_record(self):  # edge 9
        good = f"SAMPLE-F1,estimate,10,2026-03-10,{SENTINEL},{HEX},2026-10-31"
        cases = ((["request_id,kind,amount"], "row 1: header does not match FEES.csv v1"),
                 ([FEE_HEADER, good, "SAMPLE-F1,estimate,10"], "row 3: columns expected 7, found 3"),
                 ([FEE_HEADER, good.replace("SAMPLE-F1", "GHOST-9")], "row 2: request GHOST-9 not in REQUESTS.csv"),
                 ([FEE_HEADER, good.replace(",estimate,", ",refund,")], "row 2: kind must be estimate, invoice or paid"),
                 ([FEE_HEADER, good.replace(",10,", ",1.5,")], "row 2: amount_cents must be an integer 0-10000000"),
                 ([FEE_HEADER, good.replace("2026-03-10", "2026-11-01")],
                  "row 2: date must be YYYY-MM-DD, not before sent or after recorded_at"),
                 ([FEE_HEADER, good.replace(",estimate,", ",paid,")], "row 2: paid has no earlier invoice row"))
        for lines, why in cases:
            self.fees.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertEqual(self.check("5000"), (1, "", f"{SLUG}: FEES.csv {why}\n"), why)
            self.refused(f"FEES.csv fails fees check (FEES.csv {why})", "estimate", 1, "2026-03-12")
        self.fees.write_bytes(b"\xff\xfe")
        self.assertEqual(self.check("5000"), (1, "", f"{SLUG}: FEES.csv unreadable (UnicodeDecodeError)\n"))
        self.req.write_text(HEADER + "\nbroken\n", encoding="utf-8")
        self.assertEqual(self.check("5000")[:2], (1, ""))

    def test_injected_write_failure_leaves_fees_unchanged(self):  # edge 10
        self.ok("estimate", 2500, "2026-03-10")
        with mock.patch.object(TOOL_MODULE.os, "replace", mock.Mock(side_effect=OSError("injected"))):
            self.refused("cannot write in agency folder (OSError)", "invoice", 3000, "2026-03-20")
        self.fees.unlink()
        with mock.patch.object(TOOL_MODULE.os, "replace", mock.Mock(side_effect=OSError("injected"))):
            self.refused("cannot write in agency folder (OSError)", "estimate", 1, "2026-03-10")

    def test_file_without_final_newline_and_store_without_fees(self):  # edge 11 (read-only check, clean append)
        self.assertEqual(self.check("0"), (0, f"fees | requests=0 over_cap=0 cap_cents=0 {LAST}\n", ""))
        self.fees.write_text(FEE_HEADER + f"\nSAMPLE-F1,estimate,10,2026-03-10,x,{HEX},2026-10-31", encoding="utf-8")
        before = self.fees.read_bytes()
        self.check("5")
        self.assertEqual(self.fees.read_bytes(), before)
        self.ok("invoice", 20, "2026-03-11")
        self.assertEqual(self.fees.read_text(encoding="utf-8").splitlines()[2],
                         f"SAMPLE-F1,invoice,20,2026-03-11,{SENTINEL},{HEX},2026-10-31")


if __name__ == "__main__":
    unittest.main()
