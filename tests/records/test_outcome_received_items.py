"""Received-item classification and cited comparison projection (synthetic only)."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import run_stages
from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import received_items as ri
from tests.records import test_outcome_factsheets as _fs

FactSheetsTests = _fs.FactSheetsTests
del _fs

CALTRANS = ["Caltrans District 5\nWe have received your public records request.\n"
            "Caltrans is extending the response period. We are extending the determination period.\n"
            "We will respond to your request by November 30."]
ATASCADERO = ["Page %d scanned text, nothing actionable." % n for n in range(1, 26)]
ATASCADERO[2] = "The duplication fee is $12.00 for these pages."


class ReceivedItemsTests(unittest.TestCase):
    # Reuse the producer-shaped synthetic document/extraction helpers without their tests.
    private, document, enroll, tree = (FactSheetsTests.private, FactSheetsTests.document,
                                       FactSheetsTests.enroll, FactSheetsTests.tree)

    def setUp(self):
        workspaces = Path(__file__).resolve().parents[2] / "build" / "received-items" / "test-workspaces"
        workspaces.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="received-", dir=workspaces)
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.processing = self.project / "processing-v1"
        self.processing.mkdir(mode=0o700)
        self.database = self.processing / "ledger.sqlite"
        store.initialize(self.database)
        self.evidence = self.processing / "artifacts" / "evidence"
        self.evidence.parent.mkdir(mode=0o700)
        self.adapter = ExtractionLedgerAdapter(database=self.database, evidence_root=self.evidence)
        self.serial = 0
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO runs VALUES('fixture','fixture','2041-01-01',NULL,'fixture',NULL,NULL,NULL,'fixture','{}')")
            for name in ("caltrans", "atascadero", "other"):
                con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", (name, name.title()))
            con.commit()
        portal = self.project / "portal-queue-v1"
        portal.mkdir(mode=0o700)
        with contextlib.closing(sqlite3.connect(portal / "portal.sqlite")) as con, con:
            con.execute("CREATE TABLE portal_items(id TEXT)")

    def collect(self, comparison=None):
        with run_stages.snapshot_ro(self.database) as con:
            return ri.collect(con, self.processing, comparison, run_id="test-run")

    def item(self, report, sha):
        return next(i for i in report["items"] if i["original_sha256"] == sha)

    def mail(self, sha, received_at):
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO mail_messages VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("m-" + sha[:8], "acct", "INBOX", 1, self.serial, "<x@example.test>", sha, "{}",
                         received_at, "verified"))
            con.commit()

    def stage(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = run_stages.main(["outputs", "--project", str(self.project), "--run-id", "test-run"])
        return code, json.loads(stdout.getvalue())

    def artifact(self):
        return self.project / "run-outputs" / "test-run" / "outcomes" / "received-items.json"

    def test_caltrans_and_atascadero_classified_with_quote_page_and_deadline(self):
        cal, _ = self.document(CALTRANS, "caltrans")
        ata, _ = self.document(ATASCADERO, "atascadero")
        self.mail(cal, "2026-11-02T10:00:00-08:00")
        report = self.collect()
        c, a = self.item(report, cal), self.item(report, ata)
        self.assertEqual((c["kind"], c["kind_basis"]["page"], c["agency_id"]), ("extension", 1, "caltrans"))
        self.assertEqual(c["kind_basis"]["quote"], "Caltrans is extending the response period. We are extending the determination period.")
        self.assertIn(c["kind_basis"]["quote"], CALTRANS[0])
        self.assertEqual(c["kit_equivalent"], "extension")
        self.assertEqual([d["date"] for d in c["deadlines"]], ["2026-11-30"])
        self.assertEqual((c["deadlines"][0]["page"], c["deadlines"][0]["year_inferred"]), (1, True))
        self.assertEqual(c["received_at"], "2026-11-02T18:00:00Z")
        self.assertEqual((a["kind"], a["kind_basis"]["page"]), ("fee", 3))
        self.assertEqual(a["deadlines"], [])
        for item in (c, a):
            self.assertTrue(item["review_required"] and not item["legal_conclusion"])
            self.assertEqual(item["decision_source"], "rules")
            self.assertEqual(item["findings"], [])
            self.assertEqual(item["comparison_status"], "unresolved")
        self.assertEqual(c["item_id"], ri.item_id(cal))

    def test_all_kinds_and_non_actions_stay_other(self):
        cases = {"response": "We acknowledge receipt of your request.", "denial": "We deny your request.",
                 "records": "Enclosed are responsive records.", "extension": "We invoke an extension.",
                 "fee": "The estimated duplication cost is $4.00."}
        docs = {k: self.document([v], "other")[0] for k, v in cases.items()}
        title = self.document(["Automated License Plate Reader Policy\nSection 1 text"], "other")[0]
        noise = {name: self.document([text], "other")[0] for name, text in {
            "negated": "We do not deny your request.", "question": "Will you deny your request?",
            "history": "Thanks.\n-----Original Message-----\nWe deny your request.",
            "quoted": "> We deny your request.", "bare": "The vendor annual fee is $27,000 in the contract.",
            "asking": "Please provide the responsive records and waive any fee."}.items()}
        report = self.collect()
        for kind, sha in docs.items():
            self.assertEqual(self.item(report, sha)["kind"], kind)
            basis = self.item(report, sha)["kind_basis"]
            self.assertEqual(basis["page"], 1)
            self.assertEqual(basis["quote"], cases[kind])
        self.assertEqual(self.item(report, title)["kind"], "records")
        for name, sha in noise.items():
            item = self.item(report, sha)
            self.assertEqual((item["kind"], item["kind_basis"]), ("other", {"page": None, "quote": None}), name)

    def test_mixed_specific_kinds_and_generic_ack_precedence(self):
        mixed = self.document(["We deny your request in part.\nWe deny your request.\nThe duplication fee is $5."], "other")[0]
        ack = self.document(["We have received your request.\nWe invoke an extension."], "other")[0]
        report = self.collect()
        item = self.item(report, mixed)
        self.assertEqual(item["kind"], "other")
        self.assertIn("mixed_kind_candidates", item["diagnostics"])
        self.assertEqual({c["kind"] for c in item["candidates"]}, {"denial", "fee"})
        self.assertEqual(self.item(report, ack)["kind"], "extension")

    def test_unpaginated_and_missing_extraction_preserved_as_other(self):
        unpaged = self.document(["We deny your request."], "other", locator={"line": 1})[0]
        with store.ledger(self.database) as con:  # admitted original with no extraction at all
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("a" * 64, 1, None, None, "agency_original", "in_scope", "/x", "pending", None, "{}"))
            con.commit()
        report = self.collect()
        item = self.item(report, unpaged)
        self.assertEqual((item["kind"], item["kind_basis"]["page"]), ("other", None))
        self.assertIn("page_locator_unavailable", item["diagnostics"])
        missing = self.item(report, "a" * 64)
        self.assertEqual(missing["kind"], "other")
        self.assertIn("current_extraction_unavailable", missing["diagnostics"])
        self.assertIn("received_at_unavailable", missing["diagnostics"])

    def test_unpaginated_mail_body_is_not_given_an_invented_page(self):
        # Live Caltrans mail shape: one HTML block, locator {"mime","part"}, no page number.
        body = ("<div>Caltrans is exercising its authority under Government Code section 7922.535 "
                "to extend the time. You will receive a response no later than November 30, 2026</div>")
        sha, _ = self.document([body], "caltrans", locator={"mime": "text/html", "part": "1"})
        item = self.item(self.collect(), sha)
        self.assertEqual((item["kind"], item["kind_basis"]), ("other", {"page": None, "quote": None}))
        self.assertIn("page_locator_unavailable", item["diagnostics"])
        self.assertEqual(item["deadlines"], [])

    def test_admission_that_cannot_be_resolved_omits_the_original_unread(self):
        sha, _ = self.document(CALTRANS, "caltrans")
        with patch.object(ri.store, "effective_original", side_effect=ValueError("bad_binding")), \
                patch.object(ri, "_current_units", side_effect=AssertionError("text was read")):
            report = self.collect()
        self.assertEqual(report["items"], [])
        self.assertIn("effective_original_unavailable", report["diagnostics"])

    def test_reply_and_forward_history_never_yields_a_deadline(self):
        history = "Thanks.\n-----Original Message-----\nWe will respond by November 30, 2026."
        self.assertEqual(ri.deadlines([(1, 1, "u1", history)], None), [])
        # The block runs on into later units of the same message.
        spanning = [(1, 1, "u1", "Thanks.\nOn Mon, Nov 2, 2026 at 9:00 AM Clerk <c@example.test> wrote:"),
                    (2, 2, "u2", "We will respond by November 30, 2026.")]
        self.assertEqual(ri.deadlines(spanning, None), [])
        current = [(1, 1, "u1", "We will respond by November 30, 2026.\n-----Original Message-----\n"
                                "We will respond by December 5, 2026.")]
        self.assertEqual([d["date"] for d in ri.deadlines(current, None)], ["2026-11-30"])

    def test_ambiguous_page_evidence_is_refused_not_first_unit(self):
        def unit(uid, text):
            return {"page": 1, "ordinal": 1, "unit_id": uid, "text": text, "text_sha256": "t", "payload_sha256": "p"}
        digest = "b" * 64
        entry = {"source_id": digest, "locator": "page 1"}
        one = {digest: {"units": [unit("u1", "Only line")]}}
        two = {digest: {"units": [unit("u1", "First line"), unit("u2", "Second line")]}}
        self.assertEqual(ri._resolve_evidence(entry, one)[2], "Only line")
        self.assertIsNone(ri._resolve_evidence(entry, two))

    def test_policy_ref_only_from_a_uniquely_established_policy_source(self):
        policy, _ = self.document(["Retention Policy\nRecords are kept 30 days."], "caltrans")
        minutes, _ = self.document(["Council Minutes\nPublic comment was heard."], "caltrans")
        rows = [self.row("caltrans", policy, "page 1", field="retention"),
                self.row("caltrans", minutes, "page 1", field="public_comment"),
                self.row("caltrans", policy, "page 1", field="access_logs"),
                self.row("caltrans", minutes, "page 1", field="safeguards")]
        rows[0]["evidence"].append({"source_id": minutes, "locator": "page 1"})  # two sources: not unique
        rows.append(self.row("caltrans", policy, "page 1", field="audit_process"))
        report = self.collect(self.comparison(rows))
        refs = {f["comparison_ref"]["field"]: f["policy_ref"]
                for i in report["items"] for f in i["findings"]}
        self.assertEqual(refs["audit_process"]["document_sha256"], policy)
        for field in ("public_comment", "access_logs", "safeguards", "retention"):
            self.assertNotIn(field, refs, field)
        for i in report["items"]:
            self.assertIn("policy_source_unestablished", i["diagnostics"])
            self.assertNotEqual(i["comparison_status"], "mapped")
        self.assertIn("policy_source_unestablished", report["diagnostics"])

    def test_inventory_identity_exclusion_and_ambiguous_joins(self):
        kept, _ = self.document(["We deny your request."], "caltrans")
        excluded, _ = self.document(["We deny your request."], "caltrans", scope="out_of_scope")
        both, _ = self.document(["We deny your request."], "caltrans")
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO joins VALUES('second',?,?,?,?,?,?,?)", (both, "atascadero", None, "fixture", "{}", "typed", None))
            con.commit()
        report = self.collect()
        shas = [i["original_sha256"] for i in report["items"]]
        self.assertIn(kept, shas)
        self.assertNotIn(excluded, shas)
        self.assertEqual(len(shas), len(set(shas)))
        item = self.item(report, both)
        self.assertIsNone(item["agency_id"])
        self.assertIn("ambiguous_agency_attribution", item["diagnostics"])
        self.assertEqual(self.item(report, kept)["item_id"], ri.item_id(kept))

    def comparison(self, rows):
        return {"schema": ri.COMPARISON_SCHEMA, "run_id": "test-run", "rows": rows}

    def row(self, agency, source, locator, field="usage_policy_published", status="gap", **extra):
        return {"agency_id": agency, "field": field, "status": status, "reason": "documentary_criterion_discrepancy",
                "expected": "Written policy publicly available.", "needs_attorney_review": True,
                "citation": {"label": "Civ. Code § 1798.90.51(b)", "url": "https://example.test/x", "baseline": "draft"},
                "evidence": [{"source_id": source, "locator": locator}], **extra}

    def test_comparison_projection_exact_rating_and_only_resolvable_rows(self):
        policy, _ = self.document(["Policy Title Line\nWe publish this policy online."], "caltrans")
        other, _ = self.document(["We deny your request."], "atascadero")
        rows = [self.row("caltrans", policy, "page 1"),
                self.row("caltrans", "sha256:" + policy, "page 1", field="retention", status="unknown"),
                self.row("atascadero", policy, "page 1", field="custodian"),      # other agency: never sprayed
                self.row("caltrans", "doc-alias", "section 4"),                   # unresolved alias
                self.row("caltrans", policy, "page 1", field="retention_practice"),  # no separate policy citation
                self.row("caltrans", policy, "page 9", field="safeguards"),       # page not extracted
                self.row("caltrans", policy, "page 1", field="cpra_response_deadline", status="meets")]
        report = self.collect(self.comparison(rows))
        item = self.item(report, policy)
        self.assertEqual({f["comparison_ref"]["field"]: f["rating"] for f in item["findings"]},
                         {"usage_policy_published": "gap", "retention": "unknown", "cpra_response_deadline": "meets"})
        for finding in item["findings"]:
            self.assertEqual((finding["page"], finding["quote"]), (1, "Policy Title Line"))
            self.assertTrue(finding["needs_attorney_review"] and not finding["legal_conclusion"])
            self.assertEqual(finding["law_package_status"], "draft")
            self.assertEqual(finding["rule_ref"]["label"], "Civ. Code § 1798.90.51(b)")
        by_field = {f["comparison_ref"]["field"]: f for f in item["findings"]}
        self.assertEqual(by_field["usage_policy_published"]["policy_ref"]["document_sha256"], policy)
        self.assertIsNone(by_field["cpra_response_deadline"]["policy_ref"])
        self.assertEqual(self.item(report, other)["findings"], [])
        self.assertEqual(item["comparison_status"], "partial")
        self.assertTrue({"comparison_evidence_unresolved", "practice_policy_citation_missing"} <= set(report["diagnostics"]))

    def test_stage_artifact_modes_replay_conflict_and_no_coordinator_input(self):
        doc, _ = self.document(CALTRANS, "caltrans")
        (self.processing / "runs").mkdir(mode=0o700)
        (self.processing / "runs" / "extract-test-run.json").write_text(json.dumps(
            {"schema": "records-extract-outcomes-v1", "run_id": "test-run", "documents": []}))
        before = self.tree()
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
            code, result = self.stage()
        self.assertEqual((code, result["status"]), (0, "ok"))
        path = self.artifact()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        report = json.loads(path.read_text())
        self.assertEqual(report["schema"], "records-received-item-v1")
        self.assertEqual(len(report["comparison_sha256"]), 64)
        self.assertEqual(report["items"][0]["original_sha256"], doc)
        self.assertEqual(report["items"][0]["kind"], "extension")
        self.assertTrue(all(f["rating"] == "unknown" for i in report["items"] for f in i["findings"]))
        self.assertEqual(report["items"][0]["comparison_status"], "unresolved")
        self.assertEqual(self.tree(), before)
        inode = path.stat().st_ino
        self.assertEqual(self.stage()[0], 0)
        self.assertEqual(path.stat().st_ino, inode)
        os.chmod(path, 0o600)
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        code, result = self.stage()
        self.assertEqual(code, 1)
        self.assertEqual(result["error_code"], "OUTPUTS_WRITE_FAILED")
        self.assertEqual(path.read_bytes(), original + b" ")

    def test_corrupt_current_text_is_a_refusal_not_empty_success(self):
        self.document(CALTRANS, "caltrans")
        for payload in self.evidence.iterdir():
            payload.chmod(0o600)
            payload.write_bytes(b"{}")
        with self.assertRaises(ValueError):
            self.collect()
        code, _ = self.stage()
        self.assertEqual(code, 1)
        self.assertFalse(self.artifact().exists())

    def test_stale_comparison_run_refused_and_symlink_output_refused(self):
        self.document(CALTRANS, "caltrans")
        run = self.project / "run-outputs" / "test-run"
        run.mkdir(parents=True, mode=0o700)
        (run / "comparison.json").write_text(json.dumps(self.comparison([]) | {"run_id": "older-run"}))
        with self.assertRaises(OSError):
            ri.write_run_received_items(self.project, "test-run")
        (run / "comparison.json").unlink()
        (run / "outcomes").symlink_to(self.project)
        with self.assertRaises(OSError):
            ri.write_run_received_items(self.project, "test-run")


del FactSheetsTests  # keep the producer test class from being collected here


if __name__ == "__main__":
    unittest.main()
