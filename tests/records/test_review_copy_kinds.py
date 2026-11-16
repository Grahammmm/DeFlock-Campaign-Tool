"""Synthetic public-copy intake/editor acceptance; never public content or live mail."""

import json
import re
from html import unescape
from unittest import mock

from campaign_tool.records import approval_queue as aq, console_review as cr, draft_import as di
from campaign_tool.records import draft_templates as dt, mail_drafts as md, mail_preview, mail_send as ms, public_packets
from tests.records.test_draft_import import document, tree
from tests.records.test_mail_drafts import Base
from tests.records.test_admin_panel import ServerCase, journal


def document_copy(n=0):
    doc = document()
    doc.pop("envelope")
    doc.update(kind="draft-correction-notice" if n == 0 else "draft-agency-summary",
               draft_key="copy-%s" % n, body="Synthetic full review copy %s\nCafé Ω\nTAIL\n" % n,
               target={"website": "https://synthetic.example", "page": "/agencies", "section": "agency-%s" % n},
               citations=[{"source_type": "tracker", "source_id": "tracker-row-7", "locator": "row 7",
                           "as_of": "2026-11-15", "verification": "tracker-only"},
                          {"source_type": "original", "source_id": None, "locator": None,
                           "as_of": None, "verification": "missing-original"}])
    if n == 0:
        doc["correction"] = {"prior_statement": "Synthetic earlier wording", "prior_version": "version-1",
                             "linkage": "correction-of:synthetic-1"}
    return doc


class CopyImportTests(Base):
    def intake(self, doc, apply=True):
        path = self.root / "copy.json"
        path.write_text(json.dumps(doc))
        return di.import_draft(self.project, path, apply=apply)

    def test_six_distinct_pending_originals_caveats_and_local_decisions(self):
        self.build_ledger()
        original = tree(self.root / "blobs")
        ledger = self.ledger.read_bytes()
        for n in range(6):
            before = tree(self.project)
            self.assertEqual(self.intake(document_copy(n), False)["action"], "would_queue")
            self.assertEqual(tree(self.project), before)
            self.intake(document_copy(n))
        rows = aq.load_queue(self.project)
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({r["id"] for r in rows}), 6)
        before = {r["id"]: aq._canon(r) for r in rows}
        for row in rows:
            doc = document_copy(int(row["evidence"]["draft_key"].split("-")[1]))
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["body"], doc["body"])
            self.assertEqual(aq.history(self.project, row["id"])["original"]["body"], doc["body"])
            payload = aq.approval_payload(self.project, row["id"])
            for key in ("target", "citations", "source_attachment_sha256", "source_section"):
                self.assertEqual(row["evidence"][key], doc[key])
                self.assertIn(aq._canon(doc[key]).encode(), payload)
            if "correction" in doc:
                self.assertIn(aq._canon(doc["correction"]).encode(), payload)
        edited = rows[0]["id"]
        aq.edit(self.project, edited, "operator", "Edited full copy", expected_revision=0)
        aq.reject(self.project, edited, "operator", "Not ready")
        for row in aq.load_queue(self.project):
            if row["id"] != edited:
                self.assertEqual(aq._canon(row), before[row["id"]])
        self.assertEqual(tree(self.root / "blobs"), original)
        self.assertEqual(self.ledger.read_bytes(), ledger)

    def test_duplicate_conflict_and_rejected_preservation(self):
        for n in (0, 1):
            doc = document_copy(n)
            result = self.intake(doc)
            before = tree(self.project)
            self.assertEqual(self.intake(doc)["action"], "existing")
            self.assertEqual(tree(self.project), before)
            changes = {"body": "Other", "target": {**doc["target"], "section": "changed"},
                       "citations": [{**doc["citations"][0], "verification": "unverified"}]}
            if n == 0:
                changes["correction"] = {**doc["correction"], "linkage": "other"}
            for key, value in changes.items():
                with self.assertRaisesRegex(aq.ApprovalQueueError, "source_conflict"):
                    self.intake({**doc, key: value})
                self.assertEqual(tree(self.project), before)
            aq.reject(self.project, result["id"], "operator", "No")
            before = tree(self.project)
            self.assertEqual(self.intake(doc)["status"], "rejected")
            self.assertEqual(tree(self.project), before)

    def test_malformed_target_correction_and_citation_controls(self):
        doc = document_copy()
        cases = [{**doc, "envelope": document()["envelope"]}, {**doc, "target": []},
                 {**doc, "target": {"website": "x", "section": "y"}},
                 {k: v for k, v in doc.items() if k != "correction"}]
        for field in ("website", "page", "section"):
            for value in (None, 3, "", "a\rb", "a\nb"):
                cases.append({**doc, "target": {**doc["target"], field: value}})
        for field in ("prior_statement", "prior_version", "linkage"):
            for value in (None, 7, "", "x\ny"):
                cases.append({**doc, "correction": {**doc["correction"], field: value}})
            cases.append({**doc, "correction": {k: v for k, v in doc["correction"].items() if k != field}})
        citation = doc["citations"][0]
        for field, value in (("source_id", None), ("source_id", ""), ("verification", True),
                             ("verification", "invented"), ("source_type", []), ("locator", 1)):
            cases.append({**doc, "citations": [{**citation, "verification": "verified", field: value}]})
        cases.append({**doc, "citations": [{k: v for k, v in citation.items() if k != "source_id"}]})
        before = tree(self.project)
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(aq.ApprovalQueueError):
                self.intake(bad)
            self.assertEqual(tree(self.project), before)
        for verification in ("verified", "unverified", "tracker-only", "missing-original"):
            valid = {**doc, "citations": [{**citation, "verification": verification}]}
            self.assertEqual(di.validate(valid)["evidence"]["citations"], valid["citations"])

    def test_approved_copy_cannot_place_send_or_become_packet(self):
        for n in (0, 1):
            row = self.approve(self.intake(document_copy(n))["id"])
            self.assertIn(row["kind"], dt.NON_MAIL_KINDS)
            self.assertNotIn(row["kind"], dt.NEXT_STEP_KINDS)
            for can_send in (False, True):
                self.assertEqual(cr.approval_label(row, {"can_send": can_send}),
                                 "Approve copy (draft-copy decision only; nothing is posted, sent or published)")
                self.assertIn("draft-copy decision only", cr.approval_status(self.project, row, {"can_send": can_send}))
            with mock.patch.object(md, "_thread_for", side_effect=AssertionError("mail lookup")):
                self.assertIsNone(mail_preview.preview(self.project, row)["to"])
            with self.assertRaises(ms.MailSendError) as error:
                ms._context(self.project, {}, row["id"], page_fingerprint=None, form_to=None, imap_factory=None)
            self.assertEqual(error.exception.code, "review_copy_not_mail")
            with self.assertRaisesRegex(public_packets.PacketError, "queue_row_invalid"):
                public_packets.from_queue(self.project, row)
        self.assertEqual(self.place()["placed"], 0)
        self.assertEqual(self.fake.messages, [])
        self.assertEqual(md.placements(self.project), [])


class CopyReviewTests(ServerCase):
    def draft(self, n=1, body=None):
        doc = document_copy(0)
        path = self.project.parent / "copy.json"
        path.write_text(json.dumps(doc))
        result = di.import_draft(self.project, path, apply=True)
        return next(r for r in aq.load_queue(self.project) if r["id"] == result["id"])

    def post(self, action, **fields):
        return self.http.post("/approvals/" + action, {"csrf_token": self.tok, "id": self.draft_row["id"],
                                                       "return": "review", **fields})

    def test_full_editor_copy_label_caveats_and_stale_expired_cancelled(self):
        path = "/review?id=" + self.draft_row["id"]
        before = journal(self.project)
        status, _, page = self.http.send("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(unescape(re.search(r'<textarea id="body"[^>]*>\n(.*?)</textarea>', page, re.S)[1]),
                         document_copy()["body"])
        for text in ("tracker-only", "missing-original", "not supplied", "Target website", "prior_statement",
                     "Approve copy (draft-copy decision only; nothing is posted, sent or published)"):
            self.assertIn(text, page)
        self.assertNotIn("Approve and send", page)
        self.http.send("GET", "/")
        self.assertEqual(journal(self.project), before)
        self.assertEqual(self.post("edit", body="Expired", expected_revision="0", csrf_token="old-start-token")[0], 403)
        self.assertEqual(journal(self.project), before)
        sig = self.signer.sign(aq.approval_payload(self.project, self.draft_row["id"]))
        self.assertEqual(self.post("edit", body="New copy", expected_revision="0")[0], 303)
        before = journal(self.project)
        self.assertEqual(self.post("edit", body="Stale", expected_revision="0")[0], 409)
        self.assertEqual(self.post("approve", signature=sig)[0], 403)
        self.assertEqual(journal(self.project), before)
        sig = self.signer.sign(aq.approval_payload(self.project, self.draft_row["id"]))
        self.server.mail_drafts_config = self.project / "must-not-read-mail-config.json"
        with mock.patch.object(md, "place", side_effect=AssertionError("placement")):
            self.assertEqual(self.post("approve", signature=sig)[0], 303)
        page = self.http.send("GET", path)[2]
        self.assertIn("Approved copy; draft-copy decision only", page)
        self.assertNotIn('href="/send', page)
        self.assertEqual(cr.approval_label(self.draft_row, {"can_send": True}),
                         "Approve copy (draft-copy decision only; nothing is posted, sent or published)")
        self.assertIn("draft-copy decision only", cr.approval_status(self.project, self.row(), {"can_send": True}))
