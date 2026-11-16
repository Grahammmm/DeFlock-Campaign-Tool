"""FLK-REC-4 integrated synthetic journey: denial log -> route -> case -> real drafter -> queue. Nothing is sent."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from campaign_tool import law
from campaign_tool.records import case_builder as cb, denial_log as dl, followup_engine as fe, followup_route as fr
from campaign_tool.records.outcomes import received_items
from tests.records.test_case_builder import ask, items
from tests.records import test_followup_engine
from tests.records.test_mail_drafts import Base, DOC

SHA = "a" * 64
QUOTE = "We deny this request under Gov. Code section 9999."
PAGES = {received_items.item_id(DOC): [(2, "Re: your request.\n" + QUOTE + "\nThank you.")]}
ITEM = received_items.item_id(DOC)
CA = Path(law.JURISDICTIONS) / "us-ca" / "package.json"


def request():
    return {"agency_key": "alpha-pd", "agency": "Alpha PD", "request_id": "OLD-1", "closed": False,
            "events": [{"type": "sent", "date": "2026-10-01"},
                       {"type": "denial", "date": "2026-11-02", "source": {"item_id": ITEM}}]}


class Journey(Base):
    fixture = test_followup_engine.EngineTests.fixture
    source = test_followup_engine.EngineTests.source
    settings = test_followup_engine.EngineTests.settings
    snapshot = test_followup_engine.EngineTests.snapshot
    queued = test_followup_engine.EngineTests.queued

    def setUp(self):
        super().setUp()
        self.pages = copy.deepcopy(PAGES)
        patcher = mock.patch.object(dl, "_extraction_loader", lambda project, sources: self.pages.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, run="r1", asks=None, received=None):
        if not self.ledger.exists():
            self.build_ledger()
        self.settings()
        data = items(*(asks or [ask(1, "ALPR search logs", "missing"), ask(2, "Vendor agreement", "missing")]))
        with mock.patch("tests.records.test_followup_engine.request", lambda closed: request()):
            return self.fixture(run=run, data=data, received=received or [self.source(original_sha256=SHA)])

    def test_whole_denial_with_citation_queues_one_evidence_bound_pending_reply(self):
        self.build()
        before = self.snapshot()
        route, = fr.route_run(self.project, "r1")
        self.assertEqual((route["pattern"], route["asks_for"], route["vehicle"]), ("vague_answer", [1, 2], "reply_in_thread"))
        self.assertNotIn("section 9999", json.dumps(route))
        self.assertEqual(fe.run(self.project, "r1")["would_queue"], 1)
        self.assertEqual(self.snapshot(), before)  # dry run changes nothing
        self.assertEqual(fe.run(self.project, "r1", apply=True)["queued"], 1)
        row, = self.queued()
        self.assertEqual((row["status"], row["kind"]), ("pending", fe.KIND))
        ev = row["evidence"]
        self.assertEqual((ev["pattern"], ev["vehicle"], ev["thread_item_id"]), ("vague_answer", "reply_in_thread", ITEM))
        entry, = ev["vague_answer"]["entries"]
        self.assertEqual({k: entry[k] for k in ("quote", "page", "doc_sha256", "item_id")},
                         {"quote": QUOTE, "page": 2, "doc_sha256": SHA, "item_id": ITEM})
        self.assertEqual(entry["agency_citation"], {"text": [QUOTE], "label": "agency's citation; UNVERIFIED by us"})
        law_ctx = ev["vague_answer"]["law_package"]
        self.assertEqual((law_ctx["status"], law_ctx["rule_id"], law_ctx["review"]),
                         ("draft", "ca-gov-7922.540-written-denial", "needs_attorney_review"))
        self.assertEqual(law_ctx["package_sha256"], hashlib.sha256(CA.read_bytes()).hexdigest())
        body = row["body"]
        self.assertNotIn("9999", body)
        self.assertNotIn(QUOTE, body)
        self.assertIn("DRAFT law-package wording \u2014 unreviewed; not a legal finding", body)
        self.assertIn("Gov. Code \u00a7 7922.540(a)-(c)", body)
        self.assertIn("state the justification for withholding each", body)
        self.assertIn("the cited exemption or search", body)
        self.assertEqual(fe.run(self.project, "r1", apply=True)["duplicate"], 1)  # replay: no second row
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.fake.calls, [])  # nothing placed or sent

    def test_denial_log_lifecycle_links_only_the_exact_same_run_draft(self):
        self.build()
        fe.run(self.project, "r1", apply=True)
        log = dl.load(self.project, "r1")["entries"]
        self.assertEqual([(e["kind"], e["reply"]) for e in log], [("denied_whole", "drafted")])
        entry = log[0]["agency_words"]
        self.assertEqual((entry["quote"], entry["page"], entry["doc_sha256"]), (QUOTE, 2, SHA))
        row, = self.queued()
        queue = [dict(row, source_run_id="other")]
        doc = json.loads((self.project / "run-outputs" / "r1" / "outcomes" / "response-items.json").read_text())
        states = fe.fs.classify_run(self.project, "r1")
        sources = [self.source(original_sha256=SHA)]
        again = dl.entries(states, dict(doc, sources=sources), queue, page_text=self.pages.get)
        self.assertEqual(again[0]["reply"], "not_drafted")  # different run
        forged = copy.deepcopy(row)
        forged["evidence"]["vague_answer"]["entries"][0]["doc_sha256"] = "b" * 64
        self.assertEqual(dl.entries(states, dict(doc, sources=sources), [forged], page_text=self.pages.get)[0]["reply"],
                         "not_drafted")

    def test_item_level_and_uncited_denials_keep_the_old_routes(self):
        for label, text in (("item", "Item 2 is withheld under Gov. Code section 9999."),
                            ("heading", "2.\nWithheld under Gov. Code section 9999."),
                            ("bare exempt", "These records are exempt."), ("search", "We conducted a search."),
                            ("two quotes", QUOTE + "\nWe also deny this under \u00a7 7923.600.")):
            with self.subTest(label):
                self.tearDown_project()
                self.pages = {ITEM: [(1, text)]}
                self.build()
                route, = fr.route_run(self.project, "r1")
                self.assertNotEqual(route["pattern"], "vague_answer")
                self.assertEqual(fe.run(self.project, "r1", apply=True)["queued"], 0)
        for label, pages in (("page 0", [(0, QUOTE)]), ("no extraction", [])):
            with self.subTest(label):
                self.tearDown_project()
                self.pages = {ITEM: pages}
                self.build()
                self.assertNotEqual(fr.route_run(self.project, "r1")[0]["pattern"], "vague_answer")

    def test_unreviewed_extraction_never_qualifies(self):
        self.build()
        report = self.project / "run-outputs" / "r1" / "outcomes" / "received-items.json"
        doc = json.loads(report.read_text())
        doc["items"][0]["needs_visual_review_unresolved"] = True
        report.write_text(json.dumps(doc))
        self.assertNotEqual(fr.route_run(self.project, "r1")[0]["pattern"], "vague_answer")

    def test_forged_locator_is_refused_before_any_queue_write(self):
        self.build()
        route, = fr.route_run(self.project, "r1")
        for field, value in (("quote_sha256", "0" * 64), ("doc_sha256", "b" * 64), ("page", 3), ("denial_id", "x")):
            forged = copy.deepcopy(route)
            forged["vague_answer_evidence"][0][field] = value
            with self.subTest(field), self.assertRaises(cb.CaseError):
                cb.vague_context(self.project, "r1", forged)
        context = cb.vague_context(self.project, "r1", route)
        forged = copy.deepcopy(route)
        forged["vague_answer_evidence"][0]["quote_sha256"] = "0" * 64
        with self.assertRaises(cb.CaseError):  # a direct build() call cannot trust an arbitrary route payload
            cb.build(forged, request(), items(ask(1, "a", "missing"), ask(2, "b", "missing")), vague=context)
        with self.assertRaises(cb.CaseError):
            cb.build(route, request(), items(ask(1, "a", "missing"), ask(2, "b", "missing")))
        self.assertEqual(self.queued(), [])

    def test_changed_source_evidence_is_a_new_draft_and_same_evidence_next_run_is_a_duplicate(self):
        self.build()
        fe.run(self.project, "r1", apply=True)
        self.build(run="r2")
        self.assertEqual(fe.run(self.project, "r2", apply=True)["duplicate"], 1)
        self.pages = {ITEM: [(2, QUOTE.replace("9999", "8888"))]}
        self.assertEqual(fe.run(self.project, "r2", apply=True)["queued"], 1)
        self.assertEqual(len(self.queued()), 2)

    def test_autonomy_off_and_unresolved_thread_queue_nothing(self):
        self.build()
        self.settings(autonomy="off")
        self.assertEqual(fe.run(self.project, "r1", apply=True)["autonomy_off"], 1)
        self.settings()
        self.build(received=[self.source(original_sha256=SHA), self.source(item_id="b" * 64)])  # equal receipt instants
        summary = fe.run(self.project, "r1", apply=True)
        self.assertEqual((summary["queued"], summary["provenance_unresolved"]), (0, 1))
        self.assertEqual(self.queued(), [])

    def write_package(self, **changes):
        base = self.root / "jurisdictions"
        target = base / "us-xx"
        target.mkdir(parents=True, exist_ok=True)
        package = json.loads(CA.read_text().replace('"ca-', '"xx-'))
        package["jurisdiction"] = "us-xx"
        for rule in package["rules"]:
            if rule["rule_id"].endswith("-written-denial"):
                rule.update(citation="Fictional Code 12.3", duty="ALT-DUTY wording.")
        package.update(changes)
        (target / "package.json").write_text(json.dumps(package))
        (self.project / "campaign.json").write_text(json.dumps({"schema_version": 1, "law_package": "us-xx"}))
        return base

    def test_selected_alternate_package_supplies_the_wording_and_reviewed_keeps_the_caveat(self):
        self.build()
        base = self.write_package()
        with mock.patch.object(law, "JURISDICTIONS", base):
            fe.run(self.project, "r1", apply=True)
        row, = self.queued()
        self.assertIn("ALT-DUTY wording.", row["body"])
        self.assertIn("Fictional Code 12.3", row["body"])
        self.assertNotIn("7922.540", row["body"].split("\n---\n")[0])  # the playbook's reviewer note is unchanged
        self.assertEqual(row["evidence"]["vague_answer"]["law_package"]["package"], "us-xx")
        self.tearDown_project()
        self.build()
        base = self.write_package(status="reviewed", reviewed_by=["Synthetic Reviewer"], reviewed_at="2026-11-01")
        before = (base / "us-xx" / "package.json").read_bytes()
        with mock.patch.object(law, "JURISDICTIONS", base):
            fe.run(self.project, "r1", apply=True)
        row, = self.queued()
        self.assertNotIn("DRAFT law-package wording", row["body"])
        self.assertIn("Law-package wording (reviewed package; rule review: needs_attorney_review; not a legal finding)",
                      row["body"])
        self.assertIn("DRAFT \u2014 NOT SENT", row["body"])
        self.assertEqual((base / "us-xx" / "package.json").read_bytes(), before)

    def test_missing_package_rule_or_bad_config_refuses_before_any_write(self):
        self.build()
        (self.project / "campaign.json").write_text(json.dumps({"schema_version": 1, "law_package": "us-zz"}))
        before = self.snapshot()
        with self.assertRaises(cb.CaseError):
            fe.run(self.project, "r1", apply=True)
        (self.project / "campaign.json").write_text("{not json")
        self.assertEqual(fe.run(self.project, "r1", apply=True)["queued"], 0)  # denial log unavailable: no route, no draft
        base = self.write_package()
        package = json.loads((base / "us-xx" / "package.json").read_text())
        package["rules"] = [r for r in package["rules"] if not r["rule_id"].endswith("-written-denial")]
        (base / "us-xx" / "package.json").write_text(json.dumps(package))
        (self.project / "campaign.json").write_text(json.dumps({"schema_version": 1, "law_package": "us-xx"}))
        with mock.patch.object(law, "JURISDICTIONS", base), self.assertRaises(cb.CaseError):
            fe.run(self.project, "r1", apply=True)
        self.assertEqual(self.queued(), [])
        self.assertEqual({k: v for k, v in self.snapshot().items() if not k.endswith("campaign.json")},
                         {k: v for k, v in before.items() if not k.endswith("campaign.json")})

    def test_cli_output_has_no_quote_text(self):
        self.build()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with mock.patch("sys.argv", ["x", "--project", str(self.project), "--run-id", "r1"]):
                self.assertEqual(fr.main(), 0)
        self.assertIn("vague_answer", out.getvalue())
        self.assertNotIn("9999", out.getvalue())
        self.assertNotIn(QUOTE, out.getvalue())



if __name__ == "__main__":
    unittest.main()
