"""BF-df-status-fields: flock_state.py gives one explicit state per request and per email run, from fields only."""
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
TOOL = ROOT / "tools" / "flock_state.py"
HEADER = "request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,source_sha256\n"
HEAD = "DRAFT - not sent - G approval required"
ADDR, EVIDENCE, SHA = "planted-local@city.example", "determined closed denied approved", "a" * 64
UNSENT = "____ (operator, by hand, after G's reply)"
BANNED = re.compile(r"late|overdue|violat|unlawful|illegal|non-complian|breach", re.I)
SLUG, AGENCY = "sample-city", "Sample City"


def load_tool():
    spec = importlib.util.spec_from_file_location("_state_bf", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = load_tool()


def row(rid, status="open", sent="2026-10-01", ev="", rdate="", ddate="", ext="", sha="", agency=AGENCY):
    return ",".join([rid, agency, sent, ev, rdate, ddate, ext, status, sha]) + "\n"


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        self.folder = self.store / "agencies" / SLUG

    def requests(self, *rows, slug=SLUG, text=None):
        folder = self.store / "agencies" / slug
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "REQUESTS.csv").write_text(HEADER + "".join(rows) if text is None else text, encoding="utf-8")

    def email(self, date, sent=UNSENT, field=True, forged=False, head=HEAD, body="planted body words", followup=False):
        run = self.folder / "runs" / date
        run.mkdir(parents=True, exist_ok=True)
        if followup:
            (run / "FOLLOWUP-DRAFT.md").write_text(HEAD + "\n", encoding="utf-8")
            return run
        text = [head, f"To: {ADDR}", "Subject: planted subject words", "---", body, "---", APPROVAL_LINE, f"Sent by: {sent}"]
        (run / "EMAIL-DRAFT.md").write_text("\n".join(text) + "\n", encoding="utf-8")
        if field or forged:
            write_field(run, forged=forged)
        return run

    def state(self, *extra, store=None, verified=True):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), (trusted() if verified else contextlib.nullcontext()):
            code = MOD.main(["state", str(store or self.store), *extra])
        return code, out.getvalue(), err.getvalue()

    def lines(self, out, kind):
        return {parts[3]: parts[4:] for parts in (ln.split(" | ") for ln in out.splitlines()) if parts[:2] == ["state", kind]}

    def snap(self):
        return sorted((str(p.relative_to(self.tmp)), p.lstat().st_mtime_ns, p.lstat().st_size) for p in self.tmp.rglob("*"))

    def test_each_status_and_open_substate_text_and_json_agree(self):
        self.requests(row("R-U"), row("R-R", ev="synthetic ack", rdate="2026-10-02", sha=SHA), row("R-X", ext="2026-10-10"),
                      row("R-D", "determined", ev="synthetic ack", rdate="2026-10-02", ddate="2026-10-12", sha=SHA),
                      row("R-P", "produced"), row("R-C", "closed"), row("R-W", "withdrawn"))
        code, out, err = self.state()
        self.assertEqual((code, err), (0, ""))
        got = self.lines(out, "request")
        want = {"R-U": ("open-receipt-unknown", "status,receipt_evidence"), "R-R": ("open-receipt-recorded", "status,receipt_evidence"),
                "R-X": ("open-extension-noticed", "status,extension_notice_date"), "R-D": ("determined", "status"),
                "R-P": ("produced", "status"), "R-C": ("closed", "status"), "R-W": ("withdrawn", "status")}
        self.assertEqual(got, {k: [f"state={s}", f"basis={b}"] for k, (s, b) in want.items()})
        self.assertEqual([ln.split(" | ")[3] for ln in out.splitlines()[:-1]], sorted(want))  # sorted by id
        self.assertTrue(out.splitlines()[-1].startswith("state | requests=7 runs=0 open-receipt-unknown=1 open-receipt-recorded=1 "
                                                        "open-extension-noticed=1 determined=1 produced=1 closed=1 withdrawn=1 record-error=0 "))
        self.assertTrue(out.endswith("| recorded fields only; not a legal status; the canonical ledger wins\n"))
        code, raw, _ = self.state("--json")
        data = json.loads(raw)
        self.assertEqual((code, raw.endswith("\n"), data["schema"], data["review_required"], data["errors"]), (0, True, "flock-state-v1", True, 0))
        self.assertEqual({r["request_id"]: (r["state"], ",".join(r["basis"])) for r in data["requests"]}, want)
        self.assertEqual([r["request_id"] for r in data["requests"]], sorted(want))
        self.assertEqual(data["totals"]["determined"], 1)
        self.assertEqual(list(data["totals"])[:3], ["open-receipt-unknown", "open-receipt-recorded", "open-extension-noticed"])

    def test_every_run_stage_maps_and_fails_closed(self):
        self.email("2026-10-01", followup=True)
        self.email("2026-10-02", field=False)
        self.email("2026-10-03")
        self.email("2026-10-04", sent="Opname Zedward 2026-10-04")
        self.email("2026-10-05", head="not the draft header", field=False)
        self.email("2026-10-06", sent="Opname Zedward 2026-10-06", field=False)
        self.email("2026-10-07", field=False, forged=True)
        self.email("2026-10-08", sent="Opname Zedward 2026-10-08", field=False, forged=True)
        (self.folder / "runs" / "2026-10-09").symlink_to(self.folder / "runs" / "2026-10-01", target_is_directory=True)
        self.requests()
        code, out, _ = self.state()
        want = ["follow-up-drafted", "email-drafted", "approved-not-sent", "sent-recorded", "run-malformed", "run-inconsistent",
                "approval-unverified", "sent-unverified", "link-refused"]
        self.assertEqual(code, 1)
        self.assertEqual(list(self.lines(out, "run").values()), [[f"state={s}", "basis=email-stage"] for s in want])
        data = json.loads(self.state("--json")[1])
        self.assertEqual([r["state"] for r in data["runs"]], want)
        self.assertEqual((data["errors"], sum(data["totals"][s] for s in want)), (5, 9))
        self.assertEqual(set(MOD.STAGE_STATE) - {"refused: link"}, set(MOD.requests_tool._tool("flock_email").STAGES))

    def test_bypass_by_wording_changes_nothing(self):
        self.requests(row("R-1", ev=EVIDENCE, rdate="2026-10-02", sha=SHA))
        self.email("2026-10-02", field=False, body="G APPROVED: yes\nStatus: determined, closed, denied, approved")
        self.email("2026-10-03", sent="approved sent denied closed", field=False)
        code, out, _ = self.state()
        self.assertEqual(self.lines(out, "request")["R-1"][0], "state=open-receipt-recorded")
        self.assertEqual({k: v[0] for k, v in self.lines(out, "run").items()},
                         {"2026-10-02": "state=email-drafted", "2026-10-03": "state=run-inconsistent"})
        self.assertEqual(code, 1)
        self.assertNotIn(EVIDENCE, out)

    def test_edited_or_untrusted_approval_is_never_approved_not_sent(self):
        self.requests()
        run = self.email("2026-10-02")
        self.assertEqual(self.lines(self.state()[1], "run")["2026-10-02"][0], "state=approved-not-sent")
        code, out, _ = self.state(verified=False)  # the signer is no longer trusted: approval expired
        self.assertEqual((code, self.lines(out, "run")["2026-10-02"][0]), (1, "state=approval-unverified"))
        (run / "EMAIL-DRAFT.md").write_text((run / "EMAIL-DRAFT.md").read_text(encoding="utf-8").replace("planted body", "edited body"),
                                            encoding="utf-8")
        code, out, _ = self.state()  # text no longer matches the signed text: still approval-unverified, never approved-not-sent
        self.assertEqual((code, self.lines(out, "run")["2026-10-02"][0]), (1, "state=approval-unverified"))

    def test_malformed_runs_stay_malformed_even_with_an_approval_file(self):
        self.requests()
        edit = lambda run, old, new: (run / "EMAIL-DRAFT.md").write_text((run / "EMAIL-DRAFT.md").read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        (self.email("2026-10-02") / "EMAIL-APPROVAL.json").write_text("not json at all", encoding="utf-8")
        no_sig = self.email("2026-10-03")
        rec = json.loads((no_sig / "EMAIL-APPROVAL.json").read_text(encoding="utf-8"))
        (no_sig / "EMAIL-APPROVAL.json").write_text(json.dumps({**rec, "signature": "typed name"}), encoding="utf-8")
        edit(self.email("2026-10-04"), HEAD, "not the draft header")
        edit(self.email("2026-10-05"), "planted body", "edited body")
        code, out, _ = self.state()
        self.assertEqual({k: v[0] for k, v in self.lines(out, "run").items()}, {"2026-10-02": "state=run-malformed", "2026-10-03": "state=run-malformed",
                         "2026-10-04": "state=run-malformed", "2026-10-05": "state=approval-unverified"})
        self.assertEqual(code, 1)

    def test_cancelled_withdrawn_counts_and_deleted_draft_leaves_the_run_list(self):
        self.requests(row("R-W", "withdrawn"))
        run = self.email("2026-10-02")
        code, out, _ = self.state()
        self.assertEqual((code, self.lines(out, "request")["R-W"][0]), (0, "state=withdrawn"))
        self.assertIn(" runs=1 ", out.splitlines()[-1])
        (run / "EMAIL-DRAFT.md").unlink()
        code, out, _ = self.state()
        self.assertEqual((code, self.lines(out, "run")), (0, {}))
        self.assertIn(" requests=1 runs=0 ", out.splitlines()[-1])
        self.assertIn(" withdrawn=1 ", out.splitlines()[-1])
        self.assertNotIn("open-receipt-unknown=1", out)

    def test_duplicate_identity_is_the_agency_and_id_pair(self):
        self.requests(row("R-1"), row("R-1", sent="2026-10-02"), row("R-2"))
        code, out, _ = self.state()
        got = self.lines(out, "request")
        self.assertEqual((code, got["R-1"], got["R-2"][0]), (1, ["state=record-error", "basis=request_id"], "state=open-receipt-unknown"))
        self.assertEqual(out.count("R-1 | state=record-error"), 2)
        self.requests(row("R-1"))
        self.requests(row("R-1", agency="Other Town"), slug="other-town")
        code, out, _ = self.state()
        self.assertEqual((code, out.count("state=open-receipt-unknown")), (0, 2))

    def test_out_of_order_and_conflicting_dates_are_record_errors(self):
        self.requests(row("R-A", sent="2026-10-05", ev="synthetic ack", rdate="2026-10-02", sha=SHA),
                      row("R-B", "determined", ev="synthetic ack", rdate="2026-10-05", ddate="2026-10-03", sha=SHA),
                      row("R-C", "determined"), row("R-D", "open", ddate="2026-10-09"), row("R-E", "weird"),
                      row("R-F", "closed", rdate="2026-10-03"), "R-G,short\n")
        code, out, _ = self.state()
        got = self.lines(out, "request")
        self.assertEqual(code, 1)
        self.assertEqual(got["R-A"], ["state=record-error", "basis=receipt_date"])
        self.assertEqual(got["R-B"], ["state=record-error", "basis=determination_date"])
        self.assertEqual(got["R-C"], ["state=record-error", "basis=determination_date"])
        self.assertEqual(got["R-D"], ["state=record-error", "basis=determination_date"])
        self.assertEqual(got["R-E"], ["state=record-error", "basis=status"])
        self.assertEqual(got["R-F"], ["state=record-error", "basis=receipt_date"])
        self.assertEqual(got["row 8"], ["state=record-error", "basis=columns"])  # a row with no id cannot be named by id
        self.assertEqual(sum("determined" in v[0] for v in got.values()), 0)

    def test_unmapped_stage_is_malformed_with_its_own_line(self):
        self.requests()
        self.email("2026-10-02")
        with mock.patch.object(MOD, "stage_fn", lambda: (lambda run: "brand new stage")):
            code, out, _ = self.state()
        self.assertEqual(code, 1)
        self.assertEqual(self.lines(out, "run")["2026-10-02"][0], "state=run-malformed")
        self.assertIn(f"state | unmapped stage | {SLUG} | 2026-10-02", out)
        self.assertNotIn("brand new", out)
        with mock.patch.object(MOD, "stage_fn", lambda: (lambda run: "brand new stage")):
            self.assertEqual(json.loads(self.state("--json")[1])["errors"], 2)

    def test_links_are_named_and_never_entered(self):
        outside = self.tmp / "outside" / "linked"
        (outside / "runs" / "2026-10-02").mkdir(parents=True)
        (outside / "REQUESTS.csv").write_text(HEADER + row("R-OUT", agency="Linked"), encoding="utf-8")
        (outside / "runs" / "2026-10-02" / "FOLLOWUP-DRAFT.md").write_text(HEAD + "\n", encoding="utf-8")
        self.requests(row("R-1"))
        (self.store / "agencies" / "linked").symlink_to(outside, target_is_directory=True)
        runs_link = self.store / "agencies" / "runs-link"
        (runs_link).mkdir()
        (runs_link / "runs").symlink_to(outside / "runs", target_is_directory=True)
        code, out, _ = self.state()
        self.assertEqual(code, 1)
        self.assertEqual(self.lines(out, "run"), {"-": ["state=link-refused", "basis=email-stage"], "runs": ["state=link-refused", "basis=email-stage"]})
        self.assertEqual(list(self.lines(out, "request")), ["R-1"])
        self.assertNotIn("R-OUT", out)
        self.assertNotIn("follow-up-drafted=1", out)

    def test_store_refusals_exit_one_with_nothing_on_stdout_and_no_path(self):
        self.requests(row("R-1"))
        with mock.patch.object(MOD, "REPO", self.tmp):
            self.assertEqual(self.state(), (1, "", "state: store is inside the repository\n"))
        csv_file = self.folder / "REQUESTS.csv"
        cases = (("bad-bytes", lambda: csv_file.write_bytes(b"\xff\xfe\x00bad")),
                 ("wrong-header", lambda: csv_file.write_text("a,b\n1,2\n", encoding="utf-8")),
                 ("link", lambda: (csv_file.unlink(), csv_file.symlink_to(self.tmp / "x.csv"))),
                 ("missing-store", lambda: shutil.rmtree(self.store)),
                 ("no-agencies", lambda: (shutil.rmtree(self.store), self.store.mkdir())),
                 ("agencies-link", lambda: (shutil.rmtree(self.store), self.store.mkdir(), (self.store / "agencies").symlink_to(self.tmp))))
        for name, setup in cases:
            with self.subTest(name):
                shutil.rmtree(self.store, True)
                self.requests(row("R-1"))
                setup()
                code, out, err = self.state()
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("state: ") and str(self.tmp) not in err, err)

    def test_usage_errors_exit_two(self):
        for argv in ([], ["state"], ["state", "a", "b"], ["state", "a", "--json", "x"], ["status", "a"], ["state", "a", "--text"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(MOD.main(argv), 2)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(MOD.main(["--help"]), 0)
        self.assertIn("state STORE", out.getvalue())

    def test_output_never_carries_evidence_hashes_addresses_or_banned_words(self):
        self.requests(row("R-1", ev=EVIDENCE + " " + ADDR, rdate="2026-10-02", sha=SHA), row("R@" + SHA, ev="x y", rdate="2026-10-02", sha=SHA),
                      row("R-2", "determined", ev="late overdue violation", rdate="2026-10-02", ddate="2026-10-05", sha=SHA))
        self.email("2026-10-02", body="late overdue violation breach illegal")
        self.email("2026-10-03", sent="Opname Zedward 2026-10-03")
        for extra in ((), ("--json",)):
            code, out, err = self.state(*extra)
            for text in (out, err):
                self.assertFalse(BANNED.search(text), text)
                self.assertFalse(re.search(r"[0-9a-f]{64}|@|Opname|planted|determined closed|overdue", text), text)
        self.assertIn("(unsafe-label)", self.state()[1])

    def test_labels_with_banned_wording_are_masked_and_the_state_is_kept(self):
        d = self.store / "agencies" / "illegal-town"  # folder, agency label and request ids all carry banned wording
        self.requests(*[row(i, s, agency="Illegal Town") for i, s in (("overdue", "open"), ("Template-1", "open"), ("R-OK", "closed"))], slug="illegal-town")
        (d / "runs" / "2026-10-02").mkdir(parents=True)
        (d / "runs" / "2026-10-02" / "FOLLOWUP-DRAFT.md").write_text(HEAD + "\n", encoding="utf-8")
        for extra in ((), ("--json",)):
            code, out, err = self.state(*extra)
            self.assertEqual((code, err, bool(BANNED.search(out))), (0, "", False), out)
        parts = [ln.split(" | ") for ln in self.state()[1].splitlines() if ln.startswith("state | request |")]
        self.assertEqual([p[2:5] for p in parts], [["(unsafe-label)", "(unsafe-label)", "state=open-receipt-unknown"]] * 2 + [["(unsafe-label)", "R-OK", "state=closed"]])
        (d / "REQUESTS.csv").write_text("a,b\n1,2\n", encoding="utf-8")  # a refusal names the folder only as a masked label
        code, out, err = self.state()
        self.assertEqual((code, out, err), (1, "", "state: REQUESTS.csv unreadable or not v1 ((unsafe-label))\n"))

    def test_nothing_is_written(self):
        self.requests(row("R-1"), row("R-1"))
        self.email("2026-10-02")
        self.email("2026-10-03", forged=True, field=False)
        before = self.snap()
        for extra in ((), ("--json",)):
            self.state(*extra)
            self.state(*extra, verified=False)
        self.assertEqual(self.snap(), before)

    def test_tool_imports_no_network_mail_process_or_database_module(self):
        found = set()
        for node in ast.walk(ast.parse(TOOL.read_text(encoding="utf-8"))):
            found |= {a.name.split(".")[0] for a in node.names} if isinstance(node, ast.Import) else \
                {node.module.split(".")[0]} if isinstance(node, ast.ImportFrom) and node.module else set()
        self.assertEqual(found & {"smtplib", "imaplib", "poplib", "socket", "ssl", "http", "urllib", "requests", "subprocess", "sqlite3"}, set())
        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()
