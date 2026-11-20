"""OCR-REVIEW-ROUTE-1: the console page that confirms or rejects ONE OCR page, journaled like the other console decisions.

Synthetic only: the documents are made by the same fixtures the extraction tests use, with fixed OCR text and no real record.
The cycle is the real one: export (console-model/ocr-pages.json) -> the page served from the pinned export -> a POST that
journals one decision -> ``decisions-apply`` -> the existing ``ocr_review.mark`` -> the text state and the analysis export.
Nothing here is sent or published, and a person's decision is simulated only by a form post in the test.
"""
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import threading
import unittest
from contextlib import redirect_stderr
from unittest import mock

from campaign_tool.records import admin_panel as ap
from campaign_tool.records import console_apply, console_journal, console_ocr, ocr_review, run_stages
from campaign_tool.records import analysis_export as ae
from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import received_items as ri
from campaign_tool.records.outcomes import textstate as ts
from tests.records import test_outcome_factsheets as _fs
from tests.records.test_console_apply import Apply
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN
from tests.records.test_console_snapshot import RUN2
from tests.records.test_console_phone_journey import WIDTH, Phone, by_label, find_browser, link_to, submit_of
from tests.records.test_outcome_textstate import write_extract_list

AGENCY = "ag1"


class OcrBase(Apply):
    """A live project with two scanned documents (one page each; page 1 of A has OCR confidence 91, B's is not recorded)."""
    private, document, enroll = (_fs.FactSheetsTests.private, _fs.FactSheetsTests.document, _fs.FactSheetsTests.enroll)

    def setUp(self):
        super().setUp()
        self.project, self.processing = self.live, self.live / "processing-v1"
        self.database = self.processing / "ledger.sqlite"
        self.evidence = self.processing / "artifacts" / "evidence"
        self.evidence.parent.mkdir(mode=0o700)
        self.adapter = ExtractionLedgerAdapter(database=self.database, evidence_root=self.evidence)
        self.serial = 0
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO runs VALUES('fixture','fixture','2041-01-01',NULL,'fixture',NULL,NULL,NULL,'fixture','{}')")
            con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", (AGENCY, "Ag1"))
            con.commit()
        self.sha_a, _ = self.document(["Alpha scan text: 12 cameras"], AGENCY)
        self.sha_b, _ = self.document(["Bravo scan text: retention 30 days"], AGENCY)
        with store.ledger(self.database) as con:
            con.execute("UPDATE page_state SET confidence=91.0 WHERE original_sha256=?", (self.sha_a,))
            con.commit()
        self.id_a, self.id_b = ("%s-1" % sha for sha in (self.sha_a, self.sha_b))
        for run in (RUN, RUN2):
            self.write_items(run)
        self.reviews = self.live / "ocr-review-v1" / "reviews.jsonl"

    def write_items(self, run):
        outcomes = self.live / "run-outputs" / run / "outcomes"
        outcomes.mkdir(parents=True, exist_ok=True)
        items = [{"item_id": "item-alpha", "agency_id": AGENCY, "request_id": "rq1", "received_at": "2026-11-02T18:00:00Z",
                  "original_sha256": self.sha_a, "kind": "records"},
                 {"item_id": "item-bravo", "agency_id": AGENCY, "request_id": "rq1", "received_at": "2026-11-03T18:00:00Z",
                  "original_sha256": self.sha_b, "kind": "records"}]
        (outcomes / "received-items.json").write_text(json.dumps({"schema": ri.SCHEMA, "run_id": run, "items": items}))

    # ---- helpers ------------------------------------------------------------------------------------------------

    def write_citations(self, run, cited, root=None):
        """Producer-shaped same-run fact sheet and gap proposals that cite page 1 of the document `cited` three times: two
        statements, one document proposal (also named in review_list) and one ledger-only proposal that cites no page."""
        root = root or self.live
        outputs = root / "run-outputs" / run
        items = json.loads((outputs / "outcomes" / "received-items.json").read_text())
        received = (ri._canon(items) + "\n").encode()
        (outputs / "outcomes" / "received-items.json").write_bytes(received)  # the producer writes canonical JSON
        cite = lambda: {"citation": {"document_sha256": cited, "page": 1, "unit_id": "u1"}, "quote": "q"}
        sheets = {"schema": "records-agency-factsheets-v1", "run_id": run, "agencies": [{"agency_id": AGENCY, "fields": {
            "contract": {"status": "S", "statements": [cite()]}, "cost": {"status": "S", "statements": [cite()]},
            "vendor": {"status": "NOT FOUND", "statements": []}}}]}
        item = next(i["item_id"] for i in items["items"] if i["original_sha256"] == cited)
        gaps = {"schema": "records-gap-proposals-v1", "run_id": run, "received_items_sha256": hashlib.sha256(received).hexdigest(),
                "review_list": ["p-doc"], "agencies": [{"agency_id": AGENCY, "requests": [{"request_id": "rq1", "proposals": [
                    {"proposal_id": "p-doc", "evidence_kind": "document", "item_id": item, "page": 1},
                    {"proposal_id": "p-ledger", "evidence_kind": "ledger_field", "item_id": None, "page": None}]}]}]}
        (outputs / "agency-factsheets.json").write_text(json.dumps(sheets))
        (outputs / "outcomes" / "gap-proposals.json").write_text(json.dumps(gaps))

    def order(self, listing):
        return re.findall(r'/ocr-review\?id=([0-9a-f]{64}-1)"', listing)

    def model(self):
        return json.loads((self.root / "current" / "console-model" / "ocr-pages.json").read_text())

    def forms(self, page_id):
        """{action: {field: value}} of the two forms on the page, exactly as the browser would submit them."""
        page = self.get("/ocr-review?id=" + page_id)
        found = {}
        for action, body in re.findall(r'<form method="post" action="(/ocr-review/[a-z]+)">(.*?)</form>', page, re.S):
            fields = {n: html.unescape(v) for n, v in re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', body)}
            if 'type="checkbox" name="read"' in body:
                fields["read"] = "1"
            found[action] = fields
        return found

    def post(self, action, page_id, **changes):
        fields = self.forms(page_id)["/ocr-review/" + action]
        fields.update(changes)
        fields = {k: v for k, v in fields.items() if v is not None}
        return self.decide("/ocr-review/" + action, **{k: v for k, v in fields.items() if k != "csrf_token"})

    def state(self, sha, run):
        """The text state this document has in the next run, and what the analysis export would do with that state."""
        write_extract_list(self.live, run, [sha])
        ts.write_run_textstate(self.live, run)
        records = json.loads((self.live / "run-outputs" / run / "text-state.json").read_text())["records"]
        state = next(r["state"] for r in records if r["sha256"] == sha)
        with run_stages.ledger_ro(self.processing) as db:
            db.row_factory = sqlite3.Row
            body, pages, code = ae._text(db, self.processing, sha, state)
        return state, (body.decode() if body else None), code

    def review_lines(self):
        return [json.loads(line) for line in self.reviews.read_text().splitlines()] if self.reviews.exists() else []

    def apply_journal(self):
        return self.apply()


class ListAndPage(OcrBase):
    def test_the_list_and_one_page_show_text_confidence_and_source_item(self):
        self.make_export()
        self.up()
        model = self.model()
        self.assertEqual(model["counts"], {"pending": 2, "confirmed": 0, "rejected": 0})
        self.assertEqual({r["id"]: (r["state"], r["bucket"]) for r in model["rows"]},
                         {self.id_a: ("pending", "spot_check"), self.id_b: ("pending", "hold")})
        listing = self.get("/ocr-review")
        self.assertIn("2 awaiting confirmation, 0 confirmed, 0 rejected", listing)
        for text in ("item-alpha, page 1", "item-bravo, page 1", "91", "not recorded", '/ocr-review?id=' + self.id_a):
            self.assertIn(text, listing)
        self.assertNotIn("Alpha scan text", listing)  # the list names pages; the text is on the page itself
        page = self.get("/ocr-review?id=" + self.id_a)
        for text in ("Alpha scan text: 12 cameras", "item-alpha", AGENCY, "rq1", "2026-11-02T18:00:00Z", "Confidence</dt><dd>91",
                     "OCR confidence 80 or more: spot check", "Awaiting confirmation", self.sha_a[:12]):
            self.assertIn(text, page)
        self.assertIn("held until a person confirms", self.get("/ocr-review?id=" + self.id_b))
        self.assertEqual(sorted(self.forms(self.id_a)), ["/ocr-review/confirm", "/ocr-review/reject"])
        self.assertIn("Nothing is sent or published", page)

    def test_a_bad_id_is_refused_and_an_unknown_page_is_404(self):
        self.make_export()
        self.up()
        self.get("/ocr-review?id=zzz", 400)
        self.get("/ocr-review?id=%s&x=1" % self.id_a, 400)
        self.get("/ocr-review?id=" + "f" * 64 + "-1", 404)

    def test_an_export_without_the_model_says_so_and_never_shows_zero(self):
        self.make_export(mutate=lambda dest: (dest / "console-model" / "ocr-pages.json").unlink())
        self.up()
        page = self.get("/ocr-review")
        self.assertIn("console_model_missing", page)
        self.assertNotIn("0 awaiting", page)

    def test_the_live_console_lists_read_only_and_creates_nothing(self):
        server = ap.build_server(str(self.live), RUN, actor="operator", system=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        self.server = server
        before = tree_state(self.live)
        page = self.get("/ocr-review")
        self.assertIn("2 awaiting confirmation", page)
        self.assertNotIn("<form", self.get("/ocr-review?id=" + self.id_a))
        self.assertEqual(tree_state(self.live), before)

    def cited_and_uncited(self):
        """(cited, uncited) page ids: the cited page sorts second in today's hash order, so a pass proves the new order."""
        (uncited, cited) = sorted((self.id_a, self.id_b))
        self.write_citations(RUN, cited[:64])
        return cited, uncited

    def test_a_cited_page_is_listed_first_with_its_count_in_the_exported_console(self):
        # protects: pending pages are ranked by same-run citations (2 statements + 1 document proposal; the ledger-only
        # proposal and the review_list repeat add nothing); a hash-order-only list would put the uncited page first.
        cited, uncited = self.cited_and_uncited()
        self.make_export()
        self.up()
        listing = self.get("/ocr-review")
        self.assertEqual(self.order(listing), [cited, uncited])
        self.assertEqual(re.findall(r"cited by[:\w ]*?(?=<)", listing), ["cited by 3", "cited by 0"])
        self.assertIn("cited by 3", self.get("/ocr-review?id=" + cited))
        self.assertIn("cited by 0", self.get("/ocr-review?id=" + uncited))
        self.assertEqual(sorted(self.forms(cited)), ["/ocr-review/confirm", "/ocr-review/reject"])  # the decision forms are as before
        self.write_citations(RUN, uncited[:64])  # the live inputs change after sealing: the pinned export is what is shown
        self.assertEqual(self.order(self.get("/ocr-review")), [cited, uncited])
        self.assertIn("cited by 3", self.get("/ocr-review?id=" + cited))
        self.assertEqual(self.post("reject", cited)[:2], (303, "/ocr-review"))  # a decision and its journal entry are unchanged
        (entry,) = self.lines()
        self.assertEqual((entry["route"], entry["action"], entry["target_id"]), ("/ocr-review/reject", "ocr_reject", cited))

    def test_the_live_console_ranks_the_same_way_and_creates_nothing(self):
        cited, uncited = self.cited_and_uncited()
        server = ap.build_server(str(self.live), RUN, actor="operator", system=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        self.server = server
        before = tree_state(self.live)
        listing = self.get("/ocr-review")
        self.assertEqual(self.order(listing), [cited, uncited])
        self.assertIn("cited by 3", listing)
        self.assertIn("cited by 3", self.get("/ocr-review?id=" + cited))
        self.assertEqual(tree_state(self.live), before)
        victim = self.live / "run-outputs" / RUN / "agency-factsheets.json"  # a symlinked source is refused, never followed
        victim.rename(victim.with_name("real.json"))
        victim.symlink_to("real.json")
        listing = self.get("/ocr-review")
        self.assertEqual(self.order(listing), sorted((self.id_a, self.id_b)))
        self.assertEqual(listing.count("cited by: unknown"), 2)
        self.assertNotIn("cited by 0", listing)

    def test_unreadable_citation_source_keeps_old_list_order(self):
        # protects: a source that cannot be read shows unknown and today's order, never 0 (which would claim "uncited").
        cited, uncited = self.cited_and_uncited()
        self.make_export(mutate=lambda dest: (dest / "run-outputs" / RUN / "agency-factsheets.json").write_text("{not json"))
        self.up()
        listing = self.get("/ocr-review")
        self.assertEqual(self.order(listing), [uncited, cited])
        self.assertEqual(listing.count("cited by: unknown"), 2)
        self.assertNotRegex(listing, r"cited by \d")
        self.assertIn("2 awaiting confirmation", listing)
        page = self.get("/ocr-review?id=" + cited)
        self.assertIn("cited by: unknown", page)
        self.assertEqual(sorted(self.forms(cited)), ["/ocr-review/confirm", "/ocr-review/reject"])

    def test_every_unusable_citation_input_makes_the_whole_index_unknown(self):
        # protects: no partial counts; each way the inputs cannot be trusted gives None, a producer-shaped empty set gives zero.
        cited, _ = self.cited_and_uncited()
        outputs = self.live / "run-outputs" / RUN
        gaps_path, sheet_path = outputs / "outcomes" / "gap-proposals.json", outputs / "agency-factsheets.json"
        self.assertEqual(console_ocr._citations(self.live, RUN), {(cited[:64], 1): 3})

        def with_(path, change):
            doc = json.loads(path.read_text())
            change(doc)
            path.write_text(json.dumps(doc))
            return console_ocr._citations(self.live, RUN)
        proposal = lambda d: d["agencies"][0]["requests"][0]["proposals"][0]
        statement = lambda d: d["agencies"][0]["fields"]["cost"]["statements"][0]["citation"]
        cases = [("other run", sheet_path, lambda d: d.update(run_id="other")),
                 ("bound to other items", gaps_path, lambda d: d.update(received_items_sha256="0" * 64)),
                 ("unresolved item", gaps_path, lambda d: proposal(d).update(item_id="nope")),
                 ("boolean page", gaps_path, lambda d: proposal(d).update(page=True)),
                 ("bad hash", sheet_path, lambda d: statement(d).update(document_sha256="x")),
                 ("page zero", sheet_path, lambda d: statement(d).update(page=0)),
                 ("bad container", sheet_path, lambda d: d.update(agencies={}))]
        for name, path, change in cases:
            original = path.read_bytes()
            self.assertIsNone(with_(path, change), name)
            path.write_bytes(original)
        empty = with_(sheet_path, lambda d: d["agencies"][0]["fields"]["cost"].update(statements=[]))
        self.assertEqual(empty, {(cited[:64], 1): 2})
        gaps_path.unlink()  # an absent source is unknown too
        self.assertIsNone(console_ocr._citations(self.live, RUN))
        with mock.patch.object(console_ocr, "CITE_CAP", 10):  # oversize
            self.assertIsNone(console_ocr._citations(self.live, RUN))
        self.assertIsNone(console_ocr._citations(self.live, "../x"))

    def test_the_ranking_is_cited_count_then_lower_confidence_then_hash_and_page(self):
        # protects the golden order: pending first by count desc, equal count by confidence asc (unrecorded last), then hash/page;
        # decided rows keep hash/page order after the pending ones; unknown counts keep exactly today's order.
        def row(sha, page, conf, state="pending"):
            return {"id": "%s-%d" % (sha * 64, page), "original_sha256": sha * 64, "page_no": page, "confidence": conf,
                    "state": state}
        rows = [row("a", 1, None), row("b", 1, 90.0), row("b", 2, 40.0), row("c", 1, 40.0), row("d", 1, 10.0), row("1", 1, 99.0, "confirmed"),
                row("0", 1, 5.0, "rejected")]
        cites = {("d" * 64, 1): 1, ("c" * 64, 1): 2, ("b" * 64, 1): 2, ("a" * 64, 1): 2, ("1" * 64, 1): 9}
        got = [(r["original_sha256"][0], r["page_no"]) for r in console_ocr._ranked(rows, cites)]
        self.assertEqual(got, [("c", 1), ("b", 1), ("a", 1), ("d", 1), ("b", 2), ("0", 1), ("1", 1)])
        got = [(r["original_sha256"][0], r["page_no"]) for r in console_ocr._ranked(rows, None)]
        self.assertEqual(got, [("a", 1), ("b", 1), ("b", 2), ("c", 1), ("d", 1), ("0", 1), ("1", 1)])


class Decisions(OcrBase):
    def test_confirm_is_journaled_applied_and_the_page_becomes_reviewed(self):
        self.assertEqual(self.state(self.sha_a, "run_20261110T160000Z_aaaaaaaa")[0::2], ("ocr_flagged", None))  # 91, unreviewed: usable with a flag (DF2007-07)
        self.make_export()
        self.up()
        before_root, before_live = tree_state(self.root), tree_state(self.live)
        forms = self.forms(self.id_a)  # what the page offered before the decision
        status, location, _ = self.post("confirm", self.id_a)
        self.assertEqual((status, location), (303, "/ocr-review"))
        (entry,) = self.lines()
        self.assertEqual((entry["schema"], entry["route"], entry["action"], entry["target_id"], entry["actor"],
                          entry["revision_seen"], entry["source_project_id"], entry["export_run_id"]),
                         (console_journal.SCHEMA, "/ocr-review/confirm", "ocr_confirm", self.id_a, "operator", None,
                          self.pid, RUN))
        self.assertEqual(json.loads(entry["value"]), {"confidence": 91.0, "derivative_sha256": hashlib.sha256(
            b"Alpha scan text: 12 cameras").hexdigest()})
        self.assertEqual(tree_state(self.root), before_root)  # the export is never written
        self.assertEqual(tree_state(self.live), before_live)  # and neither is the live project, until the next apply
        self.assertFalse(self.reviews.exists())
        page = self.get("/ocr-review?id=" + self.id_a)
        self.assertIn("Confirm recorded; applies at the next records run", page)
        self.assertNotIn("<form", page)  # one decision per page
        for action, fields in forms.items():  # a second post from the same stale form is refused
            fields = {k: v for k, v in fields.items() if k != "csrf_token"}
            self.assertEqual(self.decide(action, **fields)[0], 409, action)
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(self.apply_journal(), ["APPLIED"])
        (review,) = self.review_lines()
        self.assertEqual((review["decision"], review["reviewer_label"], review["original_sha256"], review["page_no"]),
                         ("confirmed", "console-reviewer", self.sha_a, 1))
        self.assertNotIn("operator", json.dumps(review))  # the actor stays in the journal, never in the page review file
        state, body, code = self.state(self.sha_a, "run_20261110T160001Z_bbbbbbbb")
        self.assertEqual((state, code), ("text_reviewed", None))  # reviewed: the findings lane now reads the text
        self.assertEqual(body, "[[PAGE 1]]\nAlpha scan text: 12 cameras\n")
        self.make_export(RUN2)
        self.assertEqual(self.model()["counts"], {"pending": 1, "confirmed": 1, "rejected": 0})
        listing = self.get("/ocr-review")
        self.assertIn("1 awaiting confirmation, 1 confirmed, 0 rejected", listing)
        shown = self.get("/ocr-review?id=" + self.id_a)
        self.assertIn("Confirmed", shown)
        self.assertNotIn("Alpha scan text", shown)  # decided pages no longer carry their text

    def test_reject_is_journaled_applied_and_the_page_stays_text_not_ready(self):
        self.assertEqual(self.state(self.sha_b, "run_20261110T160000Z_aaaaaaaa")[0::2], ("ocr_unreviewed", "TEXT_NOT_READY"))
        self.make_export()
        self.up()
        self.assertEqual(self.post("reject", self.id_b)[:2], (303, "/ocr-review"))
        (entry,) = self.lines()
        self.assertEqual((entry["route"], entry["action"], entry["target_id"]), ("/ocr-review/reject", "ocr_reject", self.id_b))
        self.assertEqual(json.loads(entry["value"])["confidence"], None)
        self.assertIn("Reject recorded; applies at the next records run", self.get("/ocr-review?id=" + self.id_b))
        self.assertEqual(self.apply_journal(), ["APPLIED"])
        (review,) = self.review_lines()
        self.assertEqual((review["decision"], review["reviewer_label"]), ("rejected", "console-reviewer"))
        state, body, code = self.state(self.sha_b, "run_20261110T160001Z_bbbbbbbb")
        self.assertEqual((state, body, code), ("could_not_be_read", None, "TEXT_NOT_READY"))  # still not ready, no text exported
        self.make_export(RUN2)
        self.assertIn("Rejected (stays not ready)", self.get("/ocr-review"))

    def test_a_decision_on_one_page_leaves_the_other_waiting(self):
        self.make_export()
        self.up()
        self.post("confirm", self.id_a)
        self.apply_journal()
        self.assertEqual(self.state(self.sha_b, "run_20261110T160000Z_aaaaaaaa")[0::2], ("ocr_unreviewed", "TEXT_NOT_READY"))
        self.assertEqual([r["original_sha256"] for r in self.review_lines()], [self.sha_a])

    def test_refusals_journal_nothing(self):
        self.make_export()
        self.up()
        good = self.forms(self.id_a)["/ocr-review/confirm"]
        token = {k: v for k, v in good.items() if k != "csrf_token"}
        cases = [("/ocr-review/confirm", {**token, "read": None}, 400),                       # the attestation box was not ticked
                 ("/ocr-review/confirm", {**token, "read": "0"}, 400),
                 ("/ocr-review/confirm", {**token, "extra": "1"}, 400),                       # a field the form does not have
                 ("/ocr-review/confirm", {**token, "id": "nothex"}, 400),
                 ("/ocr-review/confirm", {**token, "id": "f" * 64 + "-1"}, 404),             # not an exported page
                 ("/ocr-review/confirm", {**token, "derivative_sha256": "0" * 64}, 409),     # the page the person saw has changed
                 ("/ocr-review/confirm", {**token, "confidence": "12.5"}, 409),
                 ("/ocr-review/reject", {**token, "read": "1"}, 400)]
        for path, fields, code in cases:
            fields = {k: v for k, v in fields.items() if v is not None}
            self.assertEqual(self.decide(path, **fields)[0], code, (path, fields))
        self.assertEqual(self.request("POST", "/ocr-review/confirm", token)[0], 400)  # no csrf token field at all
        self.assertEqual(self.request("POST", "/ocr-review/confirm", {**token, "csrf_token": "wrong"})[0], 403)
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.request("GET", "/ocr-review/confirm")[0], 404)

    def test_a_page_already_decided_in_the_export_is_refused(self):
        self.make_export()
        self.up()
        self.post("reject", self.id_b)
        self.apply_journal()
        self.make_export(RUN2)
        for action in ("confirm", "reject"):
            fields = {"id": self.id_b, "derivative_sha256": hashlib.sha256(b"Bravo scan text: retention 30 days").hexdigest(),
                      "confidence": "null", "read": "1"}
            if action == "reject":
                del fields["read"]
            self.assertEqual(self.decide("/ocr-review/" + action, **fields)[0], 409)
        self.assertEqual(len(self.lines()), 1)


class IncompleteText(OcrBase):
    """A page whose text was not exported whole (missing or cut short) can be rejected but never confirmed."""

    def confirm_fields(self):
        return {"id": self.id_a, "derivative_sha256": hashlib.sha256(b"Alpha scan text: 12 cameras").hexdigest(),
                "confidence": "91.0", "read": "1"}

    def check(self, shown):
        self.up()
        page = self.get("/ocr-review?id=" + self.id_a)
        self.assertIn(shown, page)
        self.assertIn("Do not confirm this page", page)
        self.assertEqual(sorted(self.forms(self.id_a)), ["/ocr-review/reject"])  # no Confirm form is offered
        self.assertNotIn("Confirm this page", page)
        self.assertEqual(self.decide("/ocr-review/confirm", **self.confirm_fields())[0], 409)  # nor taken when posted anyway
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.post("reject", self.id_a)[:2], (303, "/ocr-review"))  # reject stays available
        (entry,) = self.lines()
        self.assertEqual((entry["action"], entry["target_id"]), ("ocr_reject", self.id_a))

    def test_text_that_could_not_be_exported_is_never_confirmed(self):
        with mock.patch.object(console_ocr, "_page_text", lambda *a: ({}, "extraction_unavailable")):
            self.make_export()
        self.assertIsNone(next(r for r in self.model()["rows"] if r["id"] == self.id_a)["text"])
        self.check("extraction_unavailable")

    def test_text_cut_short_is_never_confirmed(self):
        with mock.patch.object(console_ocr, "MAX_PAGE_TEXT", 10):
            self.make_export()
        row = next(r for r in self.model()["rows"] if r["id"] == self.id_a)
        self.assertEqual((row["text"], row["text_truncated"]), ("Alpha scan", True))
        self.check("Text shortened after")

    def test_a_complete_page_is_still_confirmed(self):
        self.make_export()
        self.up()
        self.assertEqual(sorted(self.forms(self.id_a)), ["/ocr-review/confirm", "/ocr-review/reject"])
        self.assertEqual(self.post("confirm", self.id_a)[:2], (303, "/ocr-review"))


class WaitingCap(OcrBase):
    """The waiting-row cap counts waiting pages only: decided pages never push a waiting page out of every later export."""

    def test_a_waiting_page_follows_after_earlier_decisions(self):
        first, second = sorted((self.id_a, self.id_b))  # export order is by document hash
        with mock.patch.object(console_ocr, "MAX_ROWS", 1):
            self.make_export()
            self.up()
            model = self.model()
            self.assertEqual([r["id"] for r in model["rows"]], [first])  # one waiting page fits; the other follows
            self.assertTrue(model["truncated"])
            self.assertEqual(model["counts"], {"pending": 2, "confirmed": 0, "rejected": 0})
            self.assertEqual(self.post("reject", first)[:2], (303, "/ocr-review"))
            self.assertEqual(self.apply_journal(), ["APPLIED"])
            self.make_export(RUN2)
            model = self.model()
            self.assertEqual([(r["id"], r["state"]) for r in model["rows"]], [(first, "rejected"), (second, "pending")])
            self.assertFalse(model["truncated"])
            self.assertEqual(model["counts"], {"pending": 1, "confirmed": 0, "rejected": 1})
            self.assertIn("Awaiting confirmation", self.get("/ocr-review?id=" + second))

    def test_decided_pages_do_not_count_against_the_cap_at_scale(self):
        flagged = [{"original_sha256": "%064x" % n, "page_no": 1, "confidence": 91.0, "derivative_sha256": "%064x" % (n + 7)}
                   for n in range(console_ocr.MAX_ROWS + 3)]
        decided = [{"decision": "confirmed", **p} for p in flagged[:console_ocr.MAX_ROWS]]
        with mock.patch.object(console_ocr.ocr_review, "pages", lambda db: flagged), \
                mock.patch.object(console_ocr.ocr_review, "read_reviews", lambda path: decided), \
                mock.patch.object(console_ocr.ocr_review, "binding", lambda r: (r["original_sha256"], r["page_no"])), \
                mock.patch.object(console_ocr, "_page_text", lambda *a: ({1: "text"}, None)):
            with run_stages.ledger_ro(self.processing) as db:
                body = console_ocr._collect(db, self.processing, self.reviews)
        waiting = [r for r in body["rows"] if r["state"] == "pending"]
        self.assertEqual(len(waiting), 3)  # all waiting pages listed after 1,000 decided ones
        self.assertFalse(body["truncated"])
        self.assertEqual(body["counts"], {"pending": 3, "confirmed": console_ocr.MAX_ROWS, "rejected": 0})


class WithoutAPinnedExport(OcrBase):
    """A decision is taken only from a pinned export with a decision journal; anything else is refused before it is read."""

    def fields(self):
        return {"id": self.id_a, "derivative_sha256": hashlib.sha256(b"Alpha scan text: 12 cameras").hexdigest(),
                "confidence": "91.0", "read": "1"}

    def test_the_live_console_refuses_the_post_and_writes_nothing(self):
        server = ap.build_server(str(self.live), RUN, actor="operator", system=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        self.server = server
        before = tree_state(self.live)
        for action in ("confirm", "reject"):
            status, page = self.request("POST", "/ocr-review/" + action, {"csrf_token": server.csrf_token, **self.fields()})
            self.assertEqual(status, 503, action)
            self.assertIn("only from a pinned export", page)
        self.assertEqual(tree_state(self.live), before)
        self.assertFalse(self.reviews.exists())

    def test_an_export_console_without_a_journal_refuses_the_post(self):
        self.make_export()
        self.server = ap.build_server(export_root=str(self.root), run_id="auto", actor="operator", system=False)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join()))
        self.assertNotIn("<form", self.get("/ocr-review?id=" + self.id_a))
        status, page = self.request("POST", "/ocr-review/confirm", {"csrf_token": self.server.csrf_token, **self.fields()})
        self.assertEqual(status, 503)
        self.assertIn("read_only_export", page)
        self.assertEqual(self.lines(), [])

    def test_no_export_installed_yet_refuses_the_post_and_journals_nothing(self):
        self.up()  # an export console and a journal, but no export has been installed
        with redirect_stderr(io.StringIO()):
            status, page = self.request("POST", "/ocr-review/confirm", {"csrf_token": self.server.csrf_token, **self.fields()})
        self.assertEqual(status, 503)
        self.assertIn("export_unavailable", page)
        self.assertEqual(self.lines(), [])
        self.assertFalse(self.reviews.exists())


class ApplyRefusals(OcrBase):
    def journal_one(self, action="confirm", page_id=None):
        self.make_export()
        self.up()
        self.assertEqual(self.post(action, page_id or self.id_a)[0], 303)

    def test_a_page_re_read_since_the_decision_is_refused_not_confirmed(self):
        self.journal_one()
        with store.ledger(self.database) as con:
            con.execute("UPDATE page_state SET derivative_sha256=? WHERE original_sha256=?", ("e" * 64, self.sha_a))
            con.commit()
        self.assertEqual(self.apply_journal(), ["REFUSED ocr_page_changed"])
        self.assertEqual(self.review_lines(), [])
        self.assertEqual(self.state(self.sha_a, "run_20261110T160000Z_aaaaaaaa")[0], "ocr_flagged")

    def test_replay_is_a_noop_and_a_conflicting_decision_is_refused(self):
        self.journal_one()
        self.assertEqual(self.apply_journal(), ["APPLIED"])
        self.assertEqual(self.apply_journal(), ["ALREADY"])  # the receipt, not a second mark
        self.assertEqual(len(self.review_lines()), 1)
        # a crash between the mark and its receipt: the same decision is found already made and counts as applied
        (self.live / "approvals" / console_apply.FILE).unlink()
        self.assertEqual(self.apply_journal(), ["APPLIED"])
        self.assertEqual(len(self.review_lines()), 1)
        # somebody decided the other way meanwhile (the command on the box): refused, not overwritten
        (self.live / "approvals" / console_apply.FILE).unlink()
        self.reviews.unlink()
        self.assertEqual(ocr_review.main(["mark", "--project", str(self.live), "--original-sha256", self.sha_a, "--page", "1",
                                          "--decision", "rejected", "--reviewer-label", "organizer"]), 0)
        self.assertEqual(self.apply_journal(), ["REFUSED ocr_review_not_pending"])
        self.assertEqual([r["decision"] for r in self.review_lines()], ["rejected"])

    def test_a_malformed_or_unknown_entry_is_refused_with_a_code(self):
        self.journal_one()
        raw = self.journal_bytes()
        entry = json.loads(raw)
        for target, value, code in (("%s-1" % ("f" * 64), entry["value"], "REFUSED ocr_page_unknown"),
                                    (self.id_a, "not json", "REFUSED ocr_entry_invalid"),
                                    (self.id_a, json.dumps({"confidence": 91.0}), "REFUSED ocr_entry_invalid"),
                                    ("nothex-1", entry["value"], "REFUSED ocr_entry_invalid")):
            body = {k: v for k, v in entry.items() if k != "entry_id"}
            body.update(target_id=target, value=value)
            body["entry_id"] = hashlib.sha256(console_journal._canon({k: v for k, v in body.items()}).encode()).hexdigest()
            (self.j / console_journal.FILE).write_bytes((console_journal._canon(body) + "\n").encode())
            self.assertEqual(self.apply_journal(), [code], target)
            (self.live / "approvals" / console_apply.FILE).unlink()
        self.assertEqual(self.review_lines(), [])


@unittest.skipUnless(find_browser() or os.environ.get("DEFLOCK_REQUIRE_BROWSER") == "1", "no Chrome or Chromium on PATH")
class PhoneWidth(OcrBase):
    """The page in a real Chrome sized and touch-emulated as a 390 px phone: no sideways scroll, controls reachable by touch."""

    def setUp(self):
        super().setUp()
        binary = find_browser()
        if binary is None:
            self.fail("DEFLOCK_REQUIRE_BROWSER=1 but no Chrome or Chromium was found")
        self.make_export()
        self.up()
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        self.phone = Phone(binary)
        self.addCleanup(self.phone.close)

    def fits(self, where):
        width = self.phone.js("({view: innerWidth, page: document.documentElement.scrollWidth, "
                              "meta: !!document.querySelector('meta[name=viewport]')})")
        self.assertEqual(width["view"], WIDTH, where)
        self.assertTrue(width["meta"], where + ": no viewport meta tag")
        self.assertLessEqual(width["page"], WIDTH, "%s: the page is wider than the phone (%s px)" % (where, width["page"]))

    def test_list_page_confirm_and_reject_work_by_touch_at_390_px(self):
        self.phone.open(self.base + "/ocr-review")
        self.fits("list")
        self.assertIn("2 awaiting confirmation", self.phone.text())
        self.phone.tap(link_to("/ocr-review?id=" + self.id_a), settle=True)
        self.assertEqual(self.phone.js("location.pathname + location.search"), "/ocr-review?id=" + self.id_a)
        self.assertIn("Alpha scan text: 12 cameras", self.phone.text())
        self.fits("page")
        self.phone.tap(submit_of("/ocr-review/confirm"))  # the attestation box is required: the phone refuses by itself
        self.assertEqual(self.lines(), [])
        self.phone.tap(by_label("I compared this text"))
        self.assertTrue(self.phone.js("(%s).checked" % by_label("I compared this text")))
        self.phone.tap(submit_of("/ocr-review/confirm"), settle=True)
        (entry,) = self.lines()
        self.assertEqual((entry["action"], entry["target_id"]), ("ocr_confirm", self.id_a))
        self.assertEqual(self.phone.js("location.pathname"), "/ocr-review")
        self.assertIn("Confirm recorded", self.phone.text())
        self.fits("list after confirm")
        self.phone.tap(link_to("/ocr-review?id=" + self.id_b), settle=True)
        self.phone.tap(submit_of("/ocr-review/reject"), settle=True)
        self.assertEqual([e["action"] for e in self.lines()], ["ocr_confirm", "ocr_reject"])
        self.assertIn("Reject recorded", self.phone.text())
        self.fits("list after reject")


if __name__ == "__main__":
    unittest.main()
