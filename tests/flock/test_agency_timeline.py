"""BF-df-agency-timeline: flock_agency.py timeline prints one agency's recorded history oldest first and its open requests."""
import ast
import contextlib
import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.flock.approval_field import APPROVAL_LINE, trusted, write_field

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_agency.py"
HEADER = "request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,source_sha256\n"
STEPS = "request_id,step,date,evidence,source_sha256,recorded_at\n"
FEES = "request_id,kind,amount_cents,date,evidence,source_sha256,recorded_at\n"
INV = "request,doc_id,sha256,analysis_only,date,title\n"
HEAD, UNSENT = "DRAFT - not sent - G approval required", "____ (operator, by hand, after G's reply)"
SHA, SLUG, AGENCY = "a" * 64, "sample-city", "Sample City"
BANNED = re.compile(r"late|overdue|violat|unlawful|illegal|non-complian|breach", re.I)


def load_tool():
    spec = importlib.util.spec_from_file_location("_agency_bf", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = load_tool()


def row(rid, status="open", sent="2026-10-01", ev="", rdate="", ddate="", ext="", sha="", agency=AGENCY):
    return ",".join([rid, agency, sent, ev, rdate, ddate, ext, status, sha]) + "\n"


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        self.folder = self.store / "agencies" / SLUG
        self.folder.mkdir(parents=True)

    def put(self, name, text):
        (self.folder / name).write_text(text, encoding="utf-8")

    def email(self, date, sent=UNSENT):
        run = self.folder / "runs" / date
        run.mkdir(parents=True)
        text = [HEAD, "To: planted-local@city.example", "Subject: planted subject", "---", "planted body", "---", APPROVAL_LINE,
                f"Sent by: {sent}"]
        (run / "EMAIL-DRAFT.md").write_text("\n".join(text) + "\n", encoding="utf-8")
        write_field(run)
        return run

    def run_tool(self, *extra, store=None, agency=SLUG):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), trusted():
            code = MOD.main(["timeline", str(store or self.store), "--agency", agency, "--as-of", "2026-10-20", *extra])
        return code, out.getvalue(), err.getvalue()

    def events(self, out):
        return [ln.split(" | ") for ln in out.splitlines()[1:] if re.match(r"([0-9-]{10}|undated) \| ", ln)]

    def full_store(self):
        self.put("REQUESTS.csv", HEADER + row("R-2", sent="2026-10-03", ev="synthetic ack", rdate="2026-10-05", sha=SHA)
                 + row("R-1", "determined", ev="synthetic ack", rdate="2026-10-02", ddate="2026-10-09", sha=SHA))
        self.put("REQUEST-STEPS.csv", STEPS + f"R-1,determination,2026-10-09,ack,{SHA},2026-10-10\n"
                 + f"R-1,receipt,2026-10-02,ack,{SHA},2026-10-03\n")
        self.put("FEES.csv", FEES + f"R-1,invoice,1250,2026-10-11,inv,{SHA},2026-10-12\nR-1,paid,1250,2026-10-12,rcpt,{SHA},2026-10-13\n")
        (self.tmp / "inv.csv").write_text(INV + f"R-1,D1,{'1' * 64},no,2026-10-15,Planted Title\nR-1,D2,{'2' * 64},yes,2026-10-15,T\n"
                                          f"R-1,D3,{'3' * 64},no,,T\nR-9,D4,{'4' * 64},no,2026-10-15,T\n", encoding="utf-8")
        self.email("2026-10-04", sent="Opname Zedward 2026-10-04")

    def test_full_history_in_date_order_with_still_open_reminder(self):
        self.full_store()
        code, out, err = self.run_tool("--inventory", str(self.tmp / "inv.csv"))
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines()[0], f"agency {SLUG} | as of 2026-10-20 | recorded fields only; not a legal status; the canonical ledger wins")
        self.assertEqual(self.events(out), [
            ["2026-10-01", "sent", "R-1", "-"], ["2026-10-02", "receipt", "R-1", "(recorded twice)"],
            ["2026-10-03", "sent", "R-2", "-"], ["2026-10-04", "email-sent-recorded", "-", "-"],
            ["2026-10-05", "receipt", "R-2", "-"], ["2026-10-09", "determination", "R-1", "(recorded twice)"],
            ["2026-10-11", "fee-invoice", "R-1", "USD 12.50"], ["2026-10-12", "fee-paid", "R-1", "USD 12.50"],
            ["2026-10-15", "documents", "R-1", "2 received, 1 analysis-only"], ["undated", "documents", "R-1", "1 received, 0 analysis-only"]])
        opened = [ln for ln in out.splitlines() if ln.startswith("still open")]
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("still open | R-2 | open-receipt-recorded | reminder 2026-"), opened)
        self.assertRegex(opened[0], r"reminder [0-9-]{10} \(provisional\), (in [0-9]+ days?|today|passed [0-9]+ days? ago)$")
        self.assertEqual(out.splitlines()[-1], "totals | events=10 open=1 errors=0 failing_runs=0")
        self.assertNotRegex(out, r"Planted|planted|Opname|ack|D1|1111")

    def test_out_of_order_steps_sort_and_same_date_ties_are_fixed(self):
        self.put("REQUESTS.csv", HEADER + row("R-B", "closed") + row("R-A", "produced", sent="2026-10-02"))
        self.put("REQUEST-STEPS.csv", STEPS + f"R-B,closed,2026-10-08,x,{SHA},2026-10-09\nR-A,opened,2026-10-02,x,{SHA},2026-10-02\n"
                 f"R-B,opened,2026-10-01,x,{SHA},2026-10-01\nR-A,produced,2026-10-08,x,{SHA},2026-10-09\n")
        code, out, _ = self.run_tool()
        self.assertEqual(code, 0)
        self.assertEqual(self.events(out), [
            ["2026-10-01", "sent", "R-B", "-"], ["2026-10-01", "step", "R-B", "opened"], ["2026-10-02", "sent", "R-A", "-"],
            ["2026-10-02", "step", "R-A", "opened"], ["2026-10-08", "produced", "R-A", "-"], ["2026-10-08", "closed", "R-B", "-"]])
        self.assertNotIn("undated", out)  # the dated step stands for the status; no second undated line
        self.assertNotIn("still open", out)

    def test_cancelled_withdrawn_request_is_an_event_and_not_open(self):
        self.put("REQUESTS.csv", HEADER + row("R-W", "withdrawn") + row("R-O", sent="2026-10-02"))
        code, out, _ = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn(["undated", "withdrawn", "R-W", "-"], self.events(out))
        self.assertEqual([ln for ln in out.splitlines() if ln.startswith("still open")],
                         ["still open | R-O | open-receipt-unknown | reminder - (provisional)"])

    def test_expired_approval_edited_after_signing_fails(self):
        self.put("REQUESTS.csv", HEADER + row("R-1"))
        run = self.email("2026-10-02")
        self.assertIn(["2026-10-02", "email-approved-not-sent", "-", "-"], self.events(self.run_tool()[1]))
        draft = run / "EMAIL-DRAFT.md"
        draft.write_text(draft.read_text(encoding="utf-8").replace("planted body", "edited body"), encoding="utf-8")
        code, out, _ = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn(["2026-10-02", "email-approval-unverified", "-", "-"], self.events(out))
        self.assertNotIn("approved-not-sent", out)
        self.assertTrue(out.splitlines()[-1].endswith("failing_runs=1"))

    def test_failing_source_is_named_and_other_sources_still_shown(self):
        self.full_store()
        for name, setup in (("FEES.csv", lambda: self.put("FEES.csv", "not,a,fee,file\n")),
                            ("FEES.csv", lambda: (self.folder / "FEES.csv").write_bytes(b"\xff\xfe\x00bad")),
                            ("inventory", lambda: (self.tmp / "inv.csv").write_bytes(b"\xff\xfe\x00bad")),
                            ("inventory", lambda: (self.tmp / "inv.csv").write_text("a,b\n1,2\n", encoding="utf-8")),
                            ("REQUEST-STEPS.csv", lambda: self.put("REQUEST-STEPS.csv", STEPS + "R-1,receipt,someday,x,y,z\n"))):
            with self.subTest(name):
                shutil.rmtree(self.store)
                self.folder.mkdir(parents=True)
                (self.tmp / "inv.csv").unlink(missing_ok=True)
                self.full_store()
                setup()
                code, out, err = self.run_tool("--inventory", str(self.tmp / "inv.csv"))
                self.assertEqual((code, err), (1, ""))
                self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith(f"source-error | {name} | ")]), 1, out)
                self.assertIn(["2026-10-03", "sent", "R-2", "-"], self.events(out))
                self.assertIn("still open | R-2 |", out)
        self.put("REQUESTS.csv", HEADER + row("R-1") + row("R-1", sent="2026-10-02") + row("R-3", "withdrawn"))
        code, out, _ = self.run_tool("--json")
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(data["errors"][:2], [{"file": "REQUESTS.csv", "reason": "record-error R-1 (request_id)"}] * 2)
        self.assertIn({"date": "undated", "kind": "withdrawn", "request": "R-3", "detail": "-"}, data["events"])

    def test_bypass_by_wording_is_never_printed(self):
        self.put("REQUESTS.csv", HEADER + row("R-1", ev="agency is late and broke the law", rdate="2026-10-02", sha=SHA)
                 + row("R@" + SHA, ev="planted-local@city.example", rdate="2026-10-02", sha=SHA)
                 + row("overdue", "withdrawn"))
        self.put("FEES.csv", FEES + f"R@x{SHA},invoice,10,2026-10-03,illegal breach,{SHA},2026-10-04\n")
        for extra in ((), ("--json",)):
            code, out, err = self.run_tool(*extra)
            self.assertEqual(code, 1)
            self.assertFalse(BANNED.search(out + err), out)
            self.assertNotRegex(out + err, r"@|[0-9a-f]{64}|broke|law\b")
            self.assertEqual(len(re.findall(r"reminder(?!\")", out)), len(re.findall(r"reminder \S+ \(provisional\)", out)))
        self.assertIn("(unsafe-label)", self.run_tool()[1])
        for slug in ("../x", "Sample", "a" * 61, "a/b", ""):
            with self.subTest(slug=slug):
                self.assertEqual(self.run_tool(agency=slug)[:2], (2, ""))

    def test_store_refusals_print_only_the_refusal(self):
        self.put("REQUESTS.csv", HEADER + row("R-1"))
        with mock.patch.object(MOD, "REPO", self.tmp):
            self.assertEqual(self.run_tool(), (1, "", "timeline: store is inside the repository\n"))
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "REQUESTS.csv").write_text(HEADER + row("R-OUT", agency="Linked"), encoding="utf-8")
        (self.store / "agencies" / "linked").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_tool(agency="linked"), (1, "", "timeline: agency folder is missing or a link\n"))
        self.assertEqual(self.run_tool(agency="nowhere"), (1, "", "timeline: agency folder is missing or a link\n"))
        self.assertEqual(self.run_tool(store=self.tmp / "none")[:2], (1, ""))
        (self.tmp / "s2").mkdir()
        (self.tmp / "s2" / "agencies").symlink_to(self.store / "agencies", target_is_directory=True)
        self.assertEqual(self.run_tool(store=self.tmp / "s2"), (1, "", "timeline: store is not a folder with an agencies folder\n"))

    def test_json_and_text_agree_and_bad_dates_are_usage(self):
        self.full_store()
        _, text, _ = self.run_tool("--inventory", str(self.tmp / "inv.csv"))
        code, raw, _ = self.run_tool("--inventory", str(self.tmp / "inv.csv"), "--json")
        data = json.loads(raw)
        self.assertEqual((code, data["schema"], data["review_required"], data["agency"]), (0, "flock-agency-timeline-v1", True, SLUG))
        self.assertEqual([[e["date"], e["kind"], e["request"], e["detail"]] for e in data["events"]], self.events(text))
        self.assertEqual(text.splitlines()[-1], "totals | events=%d open=%d errors=%d failing_runs=%d"
                         % (len(data["events"]), len(data["open"]), len(data["errors"]), data["failing_runs"]))
        for argv in ([], ["timeline", "s"], ["timeline", "s", "--agency"], ["timeline", "s", "--agency", SLUG, "--as-of", "2026-02-30"],
                     ["timeline", "s", "--agency", SLUG, "--as-of", "10/20/2026"], ["timeline", "s", "--agency", SLUG, "--agency", SLUG],
                     ["timeline", "s", "--agency", SLUG, "--out", "x"], ["show", "s", "--agency", SLUG]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual((MOD.main(argv), out.getvalue()), (2, ""))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(MOD.main(["--help"]), 0)
        self.assertIn("timeline STORE --agency SLUG", out.getvalue())

    def test_nothing_is_written(self):
        self.full_store()
        before = sorted((str(p), p.lstat().st_mtime_ns, p.lstat().st_size) for p in self.tmp.rglob("*"))
        for extra in ((), ("--json",)):
            self.run_tool("--inventory", str(self.tmp / "inv.csv"), *extra)
        self.assertEqual(sorted((str(p), p.lstat().st_mtime_ns, p.lstat().st_size) for p in self.tmp.rglob("*")), before)

    def test_static_no_network_process_database_or_write_open(self):
        text, found = TOOL.read_text(encoding="utf-8"), set()
        for node in ast.walk(ast.parse(text)):
            found |= {a.name.split(".")[0] for a in node.names} if isinstance(node, ast.Import) else \
                {node.module.split(".")[0]} if isinstance(node, ast.ImportFrom) and node.module else set()
        self.assertEqual(found & {"smtplib", "imaplib", "socket", "http", "urllib", "requests", "subprocess", "sqlite3"}, set())
        self.assertTrue(found)
        self.assertNotRegex(text, r"\bopen\(|write_text|write_bytes|\.write\(|os\.(replace|unlink|link|rename)")


if __name__ == "__main__":
    unittest.main()
