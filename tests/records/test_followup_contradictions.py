"""FLK-REC-2: a signed, reviewed contradiction becomes a cited reply-in-thread draft; nothing else does.

Real signed-review adapter -> route -> case -> real playbook -> engine, all synthetic: a throwaway key trusted in-process
and the analysis fixture of the finding-review tests. Nothing is sent, approved or published.
"""
import contextlib
import csv
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq, followup_engine as fe, followup_findings as ff
from campaign_tool.records import followup_route as fr, finding_review
from campaign_tool.records.outcomes import received_items, timeline
from tests.approval_signing import Signer
from tests.records import test_analysis_packet_outbox as to, test_analysis_tick as tt
from tests.records.test_case_builder import DAY
from tests.records import test_followup_engine as tfe, test_mail_drafts as tmd

MARKER = "UNRELATED-ASK-MARKER-5521"


DOC = tmd.DOC


class ContradictionTests(tmd.Base):
    settings = tfe.EngineTests.settings
    queued = tfe.EngineTests.queued

    def setUp(self):
        super().setUp()
        self.o = to.PacketOutboxTests("test_empty_received_at_holds_the_finding_as_date_unavailable")
        self.o.start()
        self.addCleanup(self.o.doCleanups)
        self.analysis = self.o.root
        os.chmod(self.analysis / ".analysis", 0o700)
        profile = json.loads((self.analysis / "profiles" / (tt.SLUG + ".json")).read_text())
        self.agency = profile["records_agency_id"]
        self.o.review("N1", approve=False)  # the facts are checked; no human reviewed the finding yet
        config = json.loads(self.o.t.config().read_text())
        self.findings = (Path(config["compare"]["f3"]["store"]) / "agencies" / tt.SLUG / "runs" / config["review"]["run_date"]
                         / "FINDINGS.csv")
        self.build_ledger()
        self.settings()

    # ---- fixtures
    def contradiction(self, topic="contradiction"):
        with open(self.findings, newline="") as handle:
            reader = csv.DictReader(handle)
            names, rows = list(reader.fieldnames), list(reader)
        with open(self.findings, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=names, lineterminator="\n")
            writer.writeheader()
            writer.writerows(dict(r, topic=topic) if r["finding_id"] == "N1" else r for r in rows)

    def sign_off(self):
        self.contradiction()
        self.o.approve_finding("N1")

    def project_fixture(self, sha=None, answered=True):
        out = self.project / "run-outputs" / "r1" / "outcomes"
        out.mkdir(parents=True, exist_ok=True)
        item = received_items.item_id(DOC)
        req = {"agency_key": self.agency, "agency": "Agency Y", "request_id": "OLD-1", "closed": False,
               "events": [{"type": "sent", "date": "2026-10-01"}, {"type": "denial", "date": "2026-11-02"}]}
        asks = [{"ask_id": "ask-1", "text": MARKER + " vendor agreement", "rating": "answered" if answered else "missing",
                 "evidence": [{"item_id": item, "page": 1, "quote": "synthetic produced text"}]}]
        data = {"agency_key": self.agency, "request_id": "OLD-1", "asks": asks}
        source = {"agency_id": self.agency, "request_id": "OLD-1", "received_at": DAY + "T12:00:00Z", "item_id": item,
                  "original_sha256": sha or self.o.digest}
        docs = {"timeline.json": {"schema": timeline.SCHEMA, "run_id": "r1", "as_of": DAY, "requests": [req]},
                "response-items.json": {"schema": "records-response-items-v1", "run_id": "r1", "requests": [data]},
                "received-items.json": {"schema": received_items.SCHEMA, "run_id": "r1", "items": [source]}}
        for name, doc in docs.items():
            (out / name).write_text(json.dumps(doc))
        (out.parent / "CURRENT-REQUEST-REGISTER.json").write_text(json.dumps(
            {"schema": "records-request-register-v1", "as_of": DAY, "requests": [req]}))

    def snapshot(self):
        tree = {str(p): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for base in (self.project, self.analysis) for p in base.rglob("*")
                if p.is_file() and not p.name.endswith(("-wal", "-shm"))}
        return tree

    def routed(self, root=True):
        (row,) = fr.route_run(self.project, "r1", analysis_root=self.analysis if root else None)
        return row

    def assert_not_routed(self, root=True):
        row = self.routed(root)
        self.assertNotEqual(row["pattern"], "records_contradict_policy")
        self.assertNotIn("finding", row)
        before = self.snapshot()
        summary = fe.run(self.project, "r1", apply=True, analysis_root=self.analysis)
        self.assertEqual((summary["queued"], self.queued()), (0, []))
        self.assertEqual(before, self.snapshot())
        return row

    # ---- acceptance
    def test_reviewed_contradiction_routes_and_drafts_cited_facts_only(self):
        self.sign_off()
        self.project_fixture()
        legacy = self.routed(root=False)
        self.assertEqual((legacy["pattern"], legacy["reason"][:6]), (None, "rule 9"))
        row = self.routed()
        facts = ff.load_reviewed_contradictions(self.project, "r1", self.analysis)
        (descriptor,) = facts
        self.assertTrue(descriptor["valid"])
        self.assertEqual((row["pattern"], row["vehicle"], row["handled_by"], row["reason"], row["asks_for"], row["extra_patterns"]),
                         ("records_contradict_policy", "reply_in_thread", "engine", "reviewed_contradiction", [], []))
        self.assertEqual(row["finding"], ff.locator(descriptor))
        self.assertEqual(row["finding"]["fact_ids"], [self.o.cited["N1"]])
        quote = "The search log lists date, user and plate only."
        self.assertEqual([f["quote"] for f in descriptor["facts"]], [quote])
        before = self.snapshot()
        self.assertEqual(fe.run(self.project, "r1", analysis_root=self.analysis)["would_queue"], 1)
        self.assertEqual(before, self.snapshot())  # dry run writes nothing, in the project or the analysis root
        self.assertEqual(fe.run(self.project, "r1", apply=True, analysis_root=self.analysis)["queued"], 1)
        (queued,) = self.queued()
        self.assertEqual((queued["kind"], queued["status"]), ("followup-playbook", "pending"))
        self.assertEqual(queued["evidence"]["finding"], row["finding"])
        self.assertEqual(queued["evidence"]["pattern"], "records_contradict_policy")
        body = queued["body"]
        letter = body.split("\n---\n")[0]
        self.assertIn(quote, letter)
        self.assertIn(self.o.digest, letter)
        self.assertIn("page 2", letter)
        questions = letter.split("Please:")[1]
        self.assertNotIn(MARKER, questions)  # the produced ask is never re-requested
        self.assertIn("Items recorded as received; not requested again: Item 1: " + MARKER, letter)
        self.assertIn(self.o.cited["N1"][:12], questions)  # slot n: only the cited fact locator
        again = fe.run(self.project, "r1", apply=True, analysis_root=self.analysis)
        self.assertEqual((again["queued"], again["duplicate"], len(self.queued())), (0, 1, 1))
        aq.reject(self.project, queued["id"], "operator", "no")
        self.assertEqual(fe.run(self.project, "r1", apply=True, analysis_root=self.analysis)["queued"], 0)

    def test_unreviewed_finding_never_routes(self):
        self.contradiction()
        self.project_fixture()
        self.assertEqual(self.assert_not_routed()["reason"][:6], "rule 9")

    def test_rejected_finding_never_routes_and_reason_stays_private(self):
        self.contradiction()
        self.project_fixture()
        finding_review.reject(self.analysis, tt.SLUG, "N1", "PRIVATE-REJECT-REASON-77")
        row = self.assert_not_routed()
        self.assertNotIn("PRIVATE-REJECT-REASON-77", json.dumps(row))

    def test_revoked_or_unverifiable_signature_never_routes(self):
        self.sign_off()
        self.project_fixture()
        self.assertEqual(self.routed()["pattern"], "records_contradict_policy")
        with Signer(trusted=False).trust():  # the approving key is no longer trusted: fresh read refuses it
            self.assert_not_routed()
        with mock.patch.object(finding_review.approval, "TRUSTED_SIGNERS", Path("/nonexistent/trust/approvers")):
            self.assert_not_routed()  # trust unavailable is a deferral, never an approval
        self.assertEqual(self.routed()["pattern"], "records_contradict_policy")  # nothing was cached or rewritten

    def test_changed_text_after_signing_needs_a_new_review(self):
        self.sign_off()
        self.project_fixture()
        self.contradiction(topic="contradiction ")  # a different finding hash: the old signature does not apply
        self.assert_not_routed()

    def test_non_contradiction_topic_never_routes(self):
        self.contradiction(topic="record-gap")
        self.o.approve_finding("N1")
        self.project_fixture()
        self.assert_not_routed()

    def test_approved_but_uncited_or_unmapped_source_is_manual_not_drafted(self):
        self.sign_off()
        self.project_fixture(sha="c" * 64)  # the cited record is not a received item of this agency
        row = self.routed()
        self.assertEqual((row["handled_by"], row["reason"], row["pattern"]), ("manual", "contradiction_evidence_unavailable", None))
        before = self.snapshot()
        summary = fe.run(self.project, "r1", apply=True, analysis_root=self.analysis)
        self.assertEqual((summary["queued"], summary["manual_reasons"]), (0, {"contradiction_evidence_unavailable": 1}))
        self.assertEqual(before, self.snapshot())

    def test_cited_record_not_answered_is_not_eligible(self):
        self.sign_off()
        self.project_fixture(answered=False)
        self.assertNotEqual(self.routed()["pattern"], "records_contradict_policy")

    def test_no_root_is_legacy_and_bad_root_is_content_free(self):
        self.sign_off()
        self.project_fixture()
        self.assertEqual(self.routed(root=False)["reason"][:6], "rule 9")
        for bad in (self.root / "missing", self.pw):
            with self.subTest(root=bad.name):
                with self.assertRaises(ff.FindingsUnavailable) as caught:
                    fr.route_run(self.project, "r1", analysis_root=bad)
                self.assertEqual(str(caught.exception), ff.UNAVAILABLE)
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["x", "--project", str(self.project), "--run-id", "r1",
                                             "--analysis-root", str(self.root / "missing")]), contextlib.redirect_stderr(err):
            self.assertEqual(fe.main(), 2)
        self.assertEqual(err.getvalue().strip(), ff.UNAVAILABLE)

    def test_cli_dry_then_apply_and_repeat_is_a_no_op(self):
        self.sign_off()
        self.project_fixture()
        for apply, key, count in ((False, "would_queue", 1), (True, "queued", 1), (True, "queued", 0)):
            out = io.StringIO()
            argv = ["x", "--project", str(self.project), "--run-id", "r1", "--analysis-root", str(self.analysis)]
            with mock.patch.object(sys, "argv", argv + (["--apply"] if apply else [])), contextlib.redirect_stdout(out):
                self.assertEqual(fe.main(), 0)
            self.assertEqual(json.loads(out.getvalue())[key], count)
        self.assertEqual(len(self.queued()), 1)
