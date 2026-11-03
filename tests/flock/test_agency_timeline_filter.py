"""RF0550-11: flock_agency.py timeline --kind LIST --request LIST narrows one agency's history; the window applies first."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests.flock import test_agency_timeline as base
from tests.flock.approval_field import trusted
from tests.flock.test_agency_timeline import BANNED, FEES, HEADER, MOD, SHA, SLUG, STEPS, row

R1_SENT, R1_RECEIPT, R1_DET = ["2026-09-01", "sent", "R-1", "-"], ["2026-09-05", "receipt", "R-1", "-"], \
    ["2026-09-20", "determination", "R-1", "-"]
R2_SENT, R2_FEE = ["2026-10-01", "sent", "R-2", "-"], ["2026-10-03", "fee-invoice", "R-2", "USD 12.50"]
ALL_KINDS = ["sent", "receipt", "extension-notice", "determination", "produced", "closed", "withdrawn", "step", "fee-invoice",
             "fee-paid", "email", "documents"]


class TimelineFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        self.folder = self.store / "agencies" / SLUG
        self.folder.mkdir(parents=True)
        # R-1: sent, receipt, determination (not open); R-2: sent, fee-invoice (open); one email run on 10-02
        self.put("REQUESTS.csv", HEADER + row("R-1", "determined", sent="2026-09-01", ev="synthetic ack", rdate="2026-09-05",
                                              ddate="2026-09-20", sha=SHA) + row("R-2", sent="2026-10-01"))
        self.put("FEES.csv", FEES + f"R-2,invoice,1250,2026-10-03,inv,{SHA},2026-10-04\n")
        base.TimelineTests.email(self, "2026-10-02")  # the base test's run-folder helper, reused

    def put(self, name, text):
        (self.folder / name).write_text(text, encoding="utf-8")

    def run_tool(self, *extra, today=date(2026, 10, 31)):
        out, err = io.StringIO(), io.StringIO()
        fake = mock.Mock(today=mock.Mock(return_value=today))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), trusted(), mock.patch.object(MOD, "date", fake):
            code = MOD.main(["timeline", str(self.store), "--agency", SLUG, *extra])
        return code, out.getvalue(), err.getvalue()

    def events(self, out):
        return [ln.split(" | ") for ln in out.splitlines()[1:] if ln[:1] in "0123456789u" and " | " in ln]

    def opened(self, out):
        return [ln.split(" | ")[1] for ln in out.splitlines() if ln.startswith("still open | ")]

    def email_lines(self, out):
        return [e for e in self.events(out) if e[1].startswith("email-")]

    def test_fixture_unfiltered_has_every_event(self):
        code, out, _ = self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertEqual([e for e in self.events(out) if not e[1].startswith("email-")], [R1_SENT, R1_RECEIPT, R1_DET, R2_SENT, R2_FEE])
        self.assertEqual(len(self.email_lines(out)), 1, out)
        self.assertEqual(self.opened(out), ["R-2"])

    def test_kind_determination_keeps_one_line_and_the_open_block(self):
        code, out, err = self.run_tool("--kind", "determination")
        self.assertEqual((code, err, self.events(out)), (0, "", [R1_DET]))
        self.assertEqual(self.opened(out), ["R-2"])  # --kind never narrows the still-open block
        self.assertEqual(out.splitlines()[-1], "totals | events=1 open=1 errors=0 failing_runs=0 filtered=kind")
        data = json.loads(self.run_tool("--kind", "determination", "--json")[1])
        self.assertEqual((data["filter"], len(data["events"]), len(data["open"])), ({"kind": ["determination"], "request": None}, 1, 1))

    def test_request_keeps_its_events_and_its_open_row_and_drops_email_runs(self):
        code, out, _ = self.run_tool("--request", "R-2")
        self.assertEqual((code, self.events(out), self.opened(out)), (0, [R2_SENT, R2_FEE], ["R-2"]))
        self.assertEqual(self.email_lines(out), [])
        self.assertEqual(out.splitlines()[-1], "totals | events=2 open=1 errors=0 failing_runs=0 filtered=request")
        code, out, _ = self.run_tool("--request", "R-1")  # R-1 is not open: the still-open block is empty
        self.assertEqual((code, self.events(out), self.opened(out)), (0, [R1_SENT, R1_RECEIPT, R1_DET], []))
        self.assertTrue(out.splitlines()[-1].startswith("totals | events=3 open=0 "), out)
        data = json.loads(self.run_tool("--request", "R-2,R-1", "--json")[1])
        self.assertEqual((data["filter"], data["open"][0]["request"], len(data["events"])), ({"kind": None, "request": ["R-2", "R-1"]}, "R-2", 5))

    def test_kind_email_and_kind_with_request(self):
        code, out, _ = self.run_tool("--kind", "email")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.events(out), self.email_lines(self.run_tool()[1]))
        self.assertEqual(len(self.events(out)), 1)
        code, out, _ = self.run_tool("--kind", "email,sent", "--request", "R-1")
        self.assertEqual((code, self.events(out), self.opened(out)), (0, [R1_SENT], []))
        self.assertTrue(out.splitlines()[-1].endswith(" filtered=kind,request"), out)
        data = json.loads(self.run_tool("--request", "R-1", "--kind", "email,sent", "--json")[1])
        self.assertEqual(data["filter"], {"kind": ["email", "sent"], "request": ["R-1"]})
        code, out, _ = self.run_tool("--kind", ",".join(ALL_KINDS))  # all 12 words: every event, still marked filtered
        self.assertEqual(self.events(out), self.events(self.run_tool()[1]))
        self.assertTrue(out.splitlines()[-1].endswith(" filtered=kind"), out)

    def test_bad_kind_is_usage_and_prints_nothing(self):
        for value, why in (("banana", "timeline: unknown kind banana\n"), ("sent,sent", "timeline: unknown kind sent\n"),
                           (",".join(ALL_KINDS + ["sent"]), "timeline: unknown kind sent\n"), ("", "timeline: unknown kind\n"),
                           ("sent,", "timeline: unknown kind\n"), ("email-", "timeline: unknown kind email-\n"),
                           ("email-approved-not-sent", "timeline: unknown kind email-approved-not-sent\n"),
                           ("Sent", "timeline: unknown kind\n"), ("a@b.example", "timeline: unknown kind\n"),
                           ("overdue", "timeline: unknown kind\n"), ("x" * 25, "timeline: unknown kind\n")):
            with self.subTest(value=value):
                self.assertEqual(self.run_tool("--kind", value, "--json"), (2, "", why))
        for extra in (("--kind", "sent", "--kind", "receipt"), ("--kind",), ("--request", "R-1", "--request", "R-2")):
            with self.subTest(extra=extra):
                code, out, err = self.run_tool(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("usage: "), err)

    def test_unknown_request_is_refused_before_any_output(self):
        for value, label in (("R-9", "R-9"), ("R-1,R-9", "R-9"), ("-", "(unsafe-label)"), ("", "-"), ("R-1,", "-"),
                             ("planted@city.example", "(unsafe-label)")):
            for extra in ((), ("--json",), ("--kind", "sent")):
                with self.subTest(value=value, extra=extra):
                    self.assertEqual(self.run_tool("--request", value, *extra), (1, "", f"timeline: unknown request {label}\n"))
        code, out, err = self.run_tool("--request", ",".join(f"R-{n}" for n in range(21)))
        self.assertEqual((code, out, err), (2, "", "timeline: --request takes 1 to 20 ids\n"))

    def test_window_first_then_filter(self):
        code, out, _ = self.run_tool("--since", "2026-09-10", "--as-of", "2026-10-02", "--kind", "sent")
        self.assertEqual((code, self.events(out)), (0, [R2_SENT]))
        # before = 09-01 sent and 09-05 receipt; after = 10-03 fee: counts are of the window only, not of the kind filter
        self.assertEqual(out.splitlines()[-1], "totals | events=1 open=1 errors=0 failing_runs=0 before=2 after=1 filtered=kind")
        data = json.loads(self.run_tool("--since", "2026-09-10", "--as-of", "2026-10-02", "--kind", "sent", "--json")[1])
        self.assertEqual((data["since"], data["before"], data["after"], data["filter"]["kind"]), ("2026-09-10", 2, 1, ["sent"]))
        # a request whose events all fall outside the window is still this agency's request: empty, not refused
        code, out, err = self.run_tool("--since", "2026-10-01", "--request", "R-1")
        self.assertEqual((code, err, self.events(out)), (0, "", []))
        self.assertTrue(out.splitlines()[-1].endswith(" before=3 after=0 filtered=request"), out)

    def test_source_error_keeps_exit_one_whatever_is_shown(self):
        self.put("REQUEST-STEPS.csv", STEPS + "R-1,receipt,not-a-date,x,x,x\n")
        code, out, err = self.run_tool("--kind", "determination")
        self.assertEqual((code, err, self.events(out)), (1, "", [R1_DET]))
        self.assertEqual(out.splitlines()[-1], "totals | events=1 open=1 errors=1 failing_runs=0 filtered=kind")

    def test_no_filter_flags_output_is_unchanged(self):
        for extra in ((), ("--json",)):
            with self.subTest(extra=extra):
                code, out, err = self.run_tool(*extra)
                golden = io.StringIO()
                with contextlib.redirect_stdout(golden), trusted():  # the unfiltered render path, called directly
                    want = MOD.render(SLUG, date(2026, 10, 31), *MOD.timeline(str(self.store), SLUG, date(2026, 10, 31), None),
                                      bool(extra))
                self.assertEqual((code, out, err), (want, golden.getvalue(), ""))
        self.assertNotIn("filtered", self.run_tool()[1])
        self.assertNotIn("filter", json.loads(self.run_tool("--json")[1]))
        for extra in ((), ("--kind", "sent"), ("--request", "R-2")):
            with self.subTest(json_extra=extra):
                data = json.loads(self.run_tool("--json", *extra)[1])
                self.assertEqual((data["schema"], data["review_required"], data["agency"]), ("flock-agency-timeline-v1", True, SLUG))
                self.assertFalse(BANNED.search(json.dumps(data)), data)


if __name__ == "__main__":
    unittest.main()
