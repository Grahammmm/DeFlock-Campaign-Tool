"""BF-df-e2e-rehearsal: rehearse_agency.py walks one agency through every chain stage, read-only, into one receipt."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.flock.approval_field import APPROVAL_LINE, trusted, write_field

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "rehearse_agency.py"
SLUG, AGENCY = "sample-city", "Sample City"
ADDR, PHONE, PLATE = "planted-clerk@city.example", "(805) 555-1234", "7ABC123"
QUOTE = "Planted verbatim quote words kept for ninety days"
REQ_HEAD = "request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,source_sha256\n"
DRAFT_HEAD = "DRAFT - not sent - G approval required"
UNSENT = "____ (operator, by hand, after G's reply)"
SRC, DOC = "a" * 64, "b" * 64
STAGES = ["agency-verified", "request-drafted", "request-approved", "request-sent", "response-received", "preserved",
          "extracted", "ocr", "catalogued", "detected", "reviewed", "published"]
STATES = {"done", "waiting-on-gate", "waiting-on-agency", "not-wired", "absent", "inconsistent", "unknown"}


def load_tool():
    spec = importlib.util.spec_from_file_location("_rehearse_bf", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = load_tool()


class RehearseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        self.folder = self.store / "agencies" / SLUG
        self.folder.mkdir(parents=True)

    # ---- synthetic store builders -------------------------------------------------------------------------------
    def contact(self):
        (self.folder / "CONTACT.csv").write_text(
            "agency,role,email,source_url,source_sha256,checked_at,added_by\n"
            f"{AGENCY},records,{ADDR},https://city.example/records,{SRC},2026-10-01,tester\n", encoding="utf-8")

    def email(self, date="2026-10-02", sent="2026-10-03 operator", signed=True, subject="planted subject words"):
        run = self.folder / "runs" / date
        run.mkdir(parents=True, exist_ok=True)
        text = [DRAFT_HEAD, f"To: {ADDR}", f"Subject: {subject}", "---", "planted body " + PHONE, "---", APPROVAL_LINE,
                f"Sent by: {sent}"]
        (run / "EMAIL-DRAFT.md").write_text("\n".join(text) + "\n", encoding="utf-8")
        if signed:
            write_field(run)

    def requests(self, status="open", ddate=""):
        (self.folder / "REQUESTS.csv").write_text(REQ_HEAD + ",".join(
            ["R-1", AGENCY, "2026-10-03", "synthetic ack", "2026-10-04", ddate, "", status, SRC]) + "\n", encoding="utf-8")

    def facts(self, status="checked", sha=DOC):
        head = MOD.facts_tool.V2
        r = dict(zip(head, [AGENCY, "R-1", "retention", "ninety days " + PLATE, sha, "1", QUOTE, "", "record", "", "",
                            "tester", "2026-10-05", status]))
        r["fact_id"] = MOD.facts_tool.fact_id(r)
        (self.folder / "FACTS.csv").write_text(",".join(head) + "\n" + ",".join(r[k] for k in head) + "\n", encoding="utf-8")

    def findings(self, date="2026-10-20", claim="The policy sets a retention period.", verify=True):
        run = self.folder / "runs" / date
        run.mkdir(parents=True, exist_ok=True)
        head = MOD.publish_tool.compare.HEADER
        cells = {"finding_id": "FN-1", "topic": "retention", "claim_class": "fact", "rating": "strong", "claim": claim,
                 "fact_ids": "F-1", "rule_ids": "", "policy_refs": "", "quote": QUOTE, "quote_fact_id": "F-1", "next_step": ""}
        (run / "FINDINGS.csv").write_text(",".join(head) + "\n" + ",".join(cells[k] for k in head) + "\n", encoding="utf-8")
        if verify:
            (run / "VERIFY.json").write_text("{}\n", encoding="utf-8")

    def full_chain(self):
        self.contact()
        self.email()
        self.requests("determined", "2026-10-10")
        self.facts()
        self.findings()

    # ---- runners ------------------------------------------------------------------------------------------------
    def run_tool(self, out=None, agency=SLUG, as_of="2026-11-03", store=None, publish=None):
        out = out or self.tmp / "out"
        argv = ["--store", str(store or self.store), "--agency", agency, "--out", str(out), "--as-of", as_of]
        buf = io.StringIO()
        patch = mock.patch.object(MOD.publish_tool, "stage", return_value=(publish, None)) if publish else contextlib.nullcontext()
        with contextlib.redirect_stdout(buf), trusted(), patch:
            code = MOD.main(argv)
        return code, buf.getvalue(), out

    def receipt(self, **kw):
        code, said, out = self.run_tool(**kw)
        doc = json.loads((out / "rehearsal.json").read_text(encoding="utf-8"))
        return code, doc, {r["stage"]: r for r in doc["stages"]}, out

    def tree_hash(self, root):
        h = hashlib.sha256()
        for p in sorted(root.rglob("*")):
            h.update(str(p.relative_to(root)).encode() + oct(p.lstat().st_mode).encode())
            if p.is_file() and not p.is_symlink():
                h.update(p.read_bytes())
        return h.hexdigest()

    # ---- acceptance behaviours ------------------------------------------------------------------------------------
    def test_mid_chain_first_break_and_later_stages_still_reported(self):
        self.contact()
        self.email()
        self.requests("open")
        self.facts()
        code, doc, rows, out = self.receipt()
        self.assertEqual(code, 0)
        self.assertEqual([r["stage"] for r in doc["stages"]], STAGES)
        self.assertTrue(all(r["state"] in STATES and set(r) == {"stage", "state", "source", "next", "gate"} for r in doc["stages"]))
        self.assertEqual([rows[s]["state"] for s in STAGES[:5]], ["done"] * 4 + ["waiting-on-agency"])
        self.assertEqual(doc["verdict"]["first_break"], "response-received")
        self.assertTrue(doc["verdict"]["text"].startswith("first break: response-received — waiting-on-agency — python3 tools/"))
        self.assertEqual((rows["preserved"]["state"], rows["extracted"]["state"], rows["catalogued"]["state"]), ("done",) * 3)
        self.assertEqual((rows["ocr"]["state"], rows["detected"]["state"]), ("not-wired", "absent"))
        self.assertEqual((rows["request-approved"]["gate"], rows["reviewed"]["gate"], rows["published"]["gate"]),
                         ("send", "law", "publish"))
        self.assertEqual((doc["schema"], doc["review_required"], doc["agency"], doc["source_errors"]),
                         ("records-rehearsal-v1", True, SLUG, []))
        md = (out / "REHEARSAL.md").read_text(encoding="utf-8")
        self.assertIn("Verdict: " + doc["verdict"]["text"], md)
        self.assertEqual(sum(ln.startswith("| ") and ln.split(" | ")[0][2:] in STAGES for ln in md.splitlines()), 12)

    def test_complete_chain_says_machine_valid_not_acceptance(self):
        self.full_chain()
        code, doc, rows, out = self.receipt(publish="approved")
        self.assertEqual(code, 0)
        self.assertEqual({s: r["state"] for s, r in rows.items() if r["state"] != "done"}, {"ocr": "not-wired"})
        # OCR is never a pass, so even a full store names it as the first break
        self.assertEqual(doc["verdict"]["first_break"], "ocr")
        with mock.patch.object(MOD, "OCR", ("done", "df-ocr-live (simulated wiring)", "none")):  # once OCR is wired
            code, doc, _, out = self.receipt(out=self.tmp / "out2", publish="approved")
        text = "chain complete for sample-city; machine-valid, not semantic acceptance"
        self.assertEqual((code, doc["verdict"]["text"], doc["verdict"]["first_break"]), (0, text, None))
        self.assertIn(text, (out / "REHEARSAL.md").read_text(encoding="utf-8"))

    def test_published_states_from_publish_stage(self):
        self.full_chain()
        for n, (stage, want) in enumerate((("pending", "waiting-on-gate"), ("partly-approved", "waiting-on-gate"),
                                           ("no-export", "absent"), ("drifted", "inconsistent"), ("invalid", "inconsistent"))):
            rows = self.receipt(out=self.tmp / ("p%d" % n), publish=stage)[2]
            self.assertEqual(rows["published"]["state"], want, stage)
        rows = self.receipt(out=self.tmp / "real")[2]  # the real publish stage: no PUBLISH.json in the run
        self.assertEqual(rows["published"]["state"], "absent")

    def test_empty_agency_every_row_absent_never_all_clear(self):
        code, doc, rows, out = self.receipt()
        self.assertEqual(code, 0)
        self.assertEqual({s: r["state"] for s, r in rows.items()}, {s: "not-wired" if s == "ocr" else "absent" for s in STAGES})
        self.assertEqual(doc["verdict"]["first_break"], "agency-verified")
        both = (out / "REHEARSAL.md").read_text(encoding="utf-8") + (out / "rehearsal.json").read_text(encoding="utf-8")
        self.assertNotRegex(both.lower(), r"all clear|chain complete")

    def test_corrupt_facts_is_unknown_with_source_error_exit_1(self):
        self.contact()
        (self.folder / "FACTS.csv").write_bytes(b"\xff\xfe\x00garbage")
        code, doc, rows, _ = self.receipt()
        self.assertEqual(code, 1)
        self.assertEqual([rows[s]["state"] for s in ("preserved", "extracted", "catalogued")], ["unknown"] * 3)
        self.assertEqual(doc["source_errors"], [{"source": "FACTS.csv", "error": "UnicodeDecodeError"}])
        self.assertEqual((rows["agency-verified"]["state"], rows["ocr"]["state"]), ("done", "not-wired"))
        (self.folder / "FACTS.csv").write_text("not,a,facts,header\n", encoding="utf-8")
        code, doc, rows, _ = self.receipt(out=self.tmp / "o2")
        self.assertEqual((code, rows["extracted"]["state"], doc["source_errors"][0]["source"]), (1, "unknown", "FACTS.csv"))

    def test_corrupt_requests_is_unknown(self):
        (self.folder / "REQUESTS.csv").write_text("bad header\n", encoding="utf-8")
        code, doc, rows, _ = self.receipt()
        self.assertEqual(code, 1)
        self.assertEqual([rows[s]["state"] for s in STAGES[1:5]], ["unknown"] * 4)
        self.assertEqual(doc["source_errors"], [{"source": "REQUESTS.csv", "error": "Refuse"}])

    def test_fact_missing_source_sha256_is_inconsistent(self):
        self.facts(sha="")
        rows = self.receipt()[2]
        self.assertEqual((rows["preserved"]["state"], rows["extracted"]["state"]), ("inconsistent", "inconsistent"))
        self.facts(status="unreviewed")
        rows = self.receipt(out=self.tmp / "o2")[2]
        self.assertEqual((rows["preserved"]["state"], rows["catalogued"]["state"]), ("done", "absent"))

    def test_prose_never_moves_a_state(self):
        self.contact()
        (self.folder / "POLICY.csv").write_text("note\nAPPROVED BY G\n", encoding="utf-8")
        self.email(sent=UNSENT, signed=False, subject="request sent and APPROVED BY G")
        rows = self.receipt()[2]
        self.assertEqual((rows["request-drafted"]["state"], rows["request-approved"]["state"]), ("done", "waiting-on-gate"))
        self.assertEqual(rows["request-sent"]["state"], "absent")
        self.email(sent=UNSENT, signed=True, subject="request sent")  # signed but not sent: waiting on the send gate
        rows = self.receipt(out=self.tmp / "o2")[2]
        self.assertEqual((rows["request-approved"]["state"], rows["request-sent"]["state"]), ("done", "waiting-on-gate"))

    def test_unverified_or_unrecorded_send_is_inconsistent(self):
        self.contact()
        self.email()
        code, _, out = self.run_tool()
        rows = {r["stage"]: r for r in json.loads((out / "rehearsal.json").read_text())["stages"]}
        self.assertEqual(rows["request-sent"]["state"], "inconsistent")  # sent recorded with no REQUESTS.csv row
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):  # the signer is not trusted: approval no longer verifies
            MOD.main(["--store", str(self.store), "--agency", SLUG, "--out", str(self.tmp / "o2"), "--as-of", "2026-11-03"])
        rows = {r["stage"]: r for r in json.loads((self.tmp / "o2" / "rehearsal.json").read_text())["stages"]}
        self.assertEqual((rows["request-approved"]["state"], rows["request-sent"]["state"]), ("inconsistent", "inconsistent"))

    def test_bad_slug_refused_nothing_created(self):
        (self.store / "agencies" / "linked").symlink_to(self.folder)
        for slug in ("no-such-agency", "sample-city/..", "../store", "linked", "Sample City"):
            code, said, out = self.run_tool(agency=slug)
            self.assertEqual((code, os.path.lexists(out)), (2, False), slug)
            self.assertTrue(said.startswith("rehearse | refused | ") and said.count("\n") == 1, said)

    def test_out_refusals_and_modes(self):
        (self.tmp / "exists").mkdir()
        (self.tmp / "real").mkdir()
        (self.tmp / "link").symlink_to(self.tmp / "real")
        for out in (self.tmp / "exists", ROOT / "rehearsal-out-test", self.store / "agencies" / "x", self.store / "x",
                    self.tmp / "link" / "o", self.tmp / "missing" / "o"):
            code, said, _ = self.run_tool(out=out)
            self.assertEqual(code, 2, out)
            self.assertFalse(os.path.lexists(out) and out != self.tmp / "exists", out)
        self.assertEqual(sorted(os.listdir(self.tmp / "exists")), [])
        code, said, _ = self.run_tool(out=Path("relative-out"))
        self.assertEqual((code, os.path.lexists("relative-out")), (2, False))
        code, _, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual(sorted(os.listdir(out)), ["REHEARSAL.md", "rehearsal.json"])
        self.assertTrue(all((out / n).stat().st_mode & 0o777 == 0o600 for n in os.listdir(out)))

    def test_privacy_and_legal_wording_never_leak(self):
        self.full_chain()
        self.findings(claim="Sample City violated SB 34 by sharing data.")
        code, doc, rows, out = self.receipt(publish="pending")
        self.assertIn("legal_claim_flags=1", rows["detected"]["source"])
        both = (out / "REHEARSAL.md").read_text(encoding="utf-8") + (out / "rehearsal.json").read_text(encoding="utf-8")
        for planted in (ADDR, PHONE, PLATE, QUOTE, "violated", "planted", "city.example", SRC, DOC, str(self.store)):
            self.assertNotIn(planted, both)
        self.assertEqual(MOD.privacy.categories(both), [])
        self.assertEqual(MOD.privacy.legal_claims(both), [])
        self.assertIsNone(re.search(r"[0-9a-f]{64}", both))

    def test_guard_withholds_a_flagged_sentence(self):
        self.assertEqual(MOD.guard("write to " + ADDR), "(withheld by guard)")
        self.assertEqual(MOD.guard("The agency violated the act."), "(withheld by guard)")
        self.assertEqual(MOD.guard("python3 tools/flock_state.py state STORE"), "python3 tools/flock_state.py state STORE")

    def test_store_unchanged_and_rerun_identical_but_date(self):
        self.contact()
        self.email()
        self.requests("open")
        self.facts()
        self.findings(verify=False)
        before = self.tree_hash(self.store)
        _, _, a = self.run_tool(out=self.tmp / "a", as_of="2026-11-03")
        _, _, b = self.run_tool(out=self.tmp / "b", as_of="2026-11-04")
        self.assertEqual(self.tree_hash(self.store), before)
        for name in ("REHEARSAL.md", "rehearsal.json"):
            self.assertEqual((a / name).read_text().replace("2026-11-03", "D"), (b / name).read_text().replace("2026-11-04", "D"))
        self.assertEqual(json.loads((a / "rehearsal.json").read_text())["verdict"]["first_break"], "response-received")

    def test_static_import_walk_has_no_network_or_subprocess(self):
        banned = {"smtplib", "imaplib", "socket", "ssl", "http", "urllib", "requests", "subprocess"}
        found = set()
        for node in ast.walk(ast.parse(TOOL.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                found.add((node.module or "").split(".")[0])
        self.assertEqual(found & banned, set())
        self.assertNotRegex(TOOL.read_text(encoding="utf-8"), r"\b(?:write_text|write_bytes|unlink|rmtree|os\.rename|os\.replace)\b")


if __name__ == "__main__":
    unittest.main()
