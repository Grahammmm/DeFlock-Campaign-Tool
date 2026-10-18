"""Synthetic source gate for export provenance and collection expectations.

No live mailbox, private records, model, credential configuration or HTTP socket.
This suite is independent of the held containment qualification.
"""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.export_bridge import enroll_export
from campaign_tool.records.intake import eml_export, mail_delta
from campaign_tool.records.portal.collections import CollectionInventory, Scope
from campaign_tool.records.portal.engine import Limits, Response, fetch_queue
from campaign_tool.records.portal.ledger_adapter import WP1LedgerAdapter
from campaign_tool.records.portal.policy import Approval, PortalError
from campaign_tool.records.portal.store import Queue
from campaign_tool.records.run import Pipeline, RunError, root_lock
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend

HOST = "archive.example.invalid"
BASE = "https://" + HOST
PARSER = hashlib.sha256(b"synthetic-collection-parser-v1").hexdigest()
PDFS = {letter: ("%PDF-1.4\nSynthetic document " + letter + "\n%%EOF\n").encode()
        for letter in "ABCDEFGHI"}


def synthetic_message(body=None, html=None):
    msg = EmailMessage()
    msg["From"] = "records@agency.example.invalid"
    msg["To"] = "organizer@campaign.example.invalid"
    msg["Subject"] = "Synthetic collection delivery"
    msg["Message-ID"] = "<fixture-bridge@agency.example.invalid>"
    msg.set_content(body if body is not None else "Seven collections follow.\n")
    if html is not None:
        msg.add_alternative(html, subtype="html")
    msg.add_attachment(PDFS["A"], maintype="application", subtype="pdf", filename="A.pdf")
    for i, part in enumerate(msg.walk()):
        if part.is_multipart():
            part.set_boundary("synthetic-bridge-boundary-" + str(i))
    return msg, msg.as_bytes(policy=SMTP)


def legacy_export(root, *, body=None, html=None, uid=41, validity=900):
    """Independent legacy-shape fixture: CAS paths and integer walk indexes."""
    msg, raw = synthetic_message(body, html)

    def blob(data):
        sha = hashlib.sha256(data).hexdigest()
        path = Path("objects") / sha[:2] / sha
        eml_export._ensure_private(root / path, data)
        return {"path": str(path), "sha256": sha, "bytes": len(data)}

    attached = []
    for index, part in enumerate(msg.walk()):
        if part.get_filename():
            item = blob(part.get_payload(decode=True))
            item.update(part=index, filename=mail_delta.safe_filename(part.get_filename()),
                        original_filename=part.get_filename(), content_type=part.get_content_type())
            attached.append(item)
    receipt = {"complete": True, "account_id": "fixture-account", "folder": "Records",
               "uidvalidity": validity, "uid": uid, "bytes": len(raw),
               "internaldate": "2026-01-02T03:04:05Z", "original_eml": blob(raw), "attachments": attached}
    key = hashlib.sha256(json.dumps([receipt[k] for k in ("account_id", "folder", "uidvalidity", "uid")]).encode()).hexdigest()
    path = root / "receipts" / (key + ".json")
    eml_export._ensure_private(path, (json.dumps(receipt, sort_keys=True) + "\n").encode())
    return path, receipt


class ExportBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.pipeline = Pipeline(self.base / "canonical")
        self.root = self.pipeline.root
        self.export = self.base / "export"
        self.export.mkdir(mode=0o700)
        self.queue = Queue(self.root.path / "portal")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.queue.close)
        self.inventory = CollectionInventory(self.queue, Scope(frozenset({HOST}), frozenset({"social.example.invalid"})))
        self.backend = CanonicalMailBackend(self.export, self.root.intake, self.root.ledger)
        self.identity = self.pipeline._identity()
        self.backend.start_run(self.identity)

    def enroll(self, path):
        with root_lock(self.root.path):
            return enroll_export(path, export_root=self.export, backend=self.backend, inventory=self.inventory)

    def count(self, table):
        return self.pipeline._query("SELECT count(*) AS n FROM " + table)[0]["n"]

    def collection(self, suffix):
        url = BASE + "/WebLink/Browse.aspx?id=" + suffix
        return self.queue.db.execute("SELECT id FROM export_collections WHERE url_private=?", (url,)).fetchone()[0]

    def page(self, value, key, members, total, *, next_cursor=None, cursor=None, marker="page"):
        cursor = cursor or BASE + "/WebLink/Browse.aspx?id=" + key
        raw = ("<html>SYNTHETIC " + marker + " " + cursor + "</html>").encode()
        digest = hashlib.sha256(raw).hexdigest()
        temp = self.queue.objects / (".fixture-" + digest)
        temp.write_bytes(raw)
        os.chmod(temp, 0o600)
        self.queue.promote(temp, digest, len(raw))
        temp.unlink()
        data = {"cursor": cursor, "source_sha256": digest, "source_bytes": len(raw),
                "parser_sha256": PARSER, "members": members, "expected_total": total,
                "next_cursor": next_cursor, "event": value.message_id}
        with root_lock(self.root.path):
            self.inventory.accept_page(self.collection(key), **data)
        return data

    def members(self, *letters):
        return [{"id": letter, "kind": "document", "url": BASE + "/documents/" + letter} for letter in letters]

    def test_real_uid_provenance_and_no_configuration_loader(self):
        path, receipt = legacy_export(self.export)
        with patch("campaign_tool.records.intake.imap_intake.load_config", side_effect=AssertionError("no credentials")):
            value = self.enroll(path)
        row = self.pipeline._query("SELECT account,folder,uidvalidity,uid,eml_sha256,received_at FROM mail_messages")[0]
        self.assertEqual((row["account"], row["folder"], row["uidvalidity"], row["uid"]), ("fixture-account", "Records", 900, 41))
        self.assertEqual(row["eml_sha256"], receipt["original_eml"]["sha256"])
        self.assertEqual(row["received_at"], "2026-01-02T03:04:05Z")
        self.assertEqual(value.attachments[0][1], receipt["attachments"][0]["sha256"])
        self.assertFalse(self.inventory.readiness(value.message_id)["digestion_complete"])

    def test_replay_and_uidvalidity_reset_keep_originals_and_add_occurrence(self):
        path, _ = legacy_export(self.export)
        first = self.enroll(path)
        count = self.count("originals")
        self.assertEqual(self.enroll(path), first)
        self.assertEqual(self.count("mail_messages"), 1)
        new_path, _ = legacy_export(self.export, validity=901)
        second = self.enroll(new_path)
        self.assertNotEqual(first.message_id, second.message_id)
        self.assertEqual(first.eml_sha256, second.eml_sha256)
        self.assertEqual(self.count("originals"), count)
        self.assertEqual(self.count("mail_messages"), 2)

    def test_wrong_original_hash_and_missing_attachment_fail_closed(self):
        path, receipt = legacy_export(self.export)
        original = self.export / receipt["original_eml"]["path"]
        original.write_bytes(b"wrong bytes")
        with self.assertRaises(mail_delta.Rejected):
            self.enroll(path)
        self.assertEqual(self.count("mail_messages"), 0)
        # Separate valid occurrence, but falsely incomplete MIME declaration.
        other = self.base / "other"
        other.mkdir(mode=0o700)
        path2, receipt2 = legacy_export(other)
        receipt2["attachments"] = []
        path2.write_text(json.dumps(receipt2))
        backend = CanonicalMailBackend(other, self.root.intake, self.root.ledger)
        backend.start_run({**self.identity, "run_id": "other-fixture"})
        with root_lock(self.root.path), self.assertRaises(mail_delta.Rejected):
            enroll_export(path2, export_root=other, backend=backend, inventory=self.inventory)
        self.assertEqual(self.count("mail_messages"), 0)

    def test_body_alternatives_collection_without_extension_and_explicit_exclusions(self):
        url = BASE + "/WebLink/Browse.aspx?id=F1"
        path, _ = legacy_export(self.export, body=url + "\n", html='<a href="' + url + '">Records</a><a href="https://social.example.invalid/profile">Social</a>')
        value = self.enroll(path)
        rows = self.queue.db.execute("SELECT kind FROM export_body_links WHERE event=?", (value.message_id,)).fetchall()
        self.assertEqual(sorted(r[0] for r in rows), ["collection", "collection", "excluded"])
        self.assertEqual(len(self.inventory.pending_pages(value.message_id)), 1)
        self.assertFalse(self.inventory.readiness(value.message_id)["retrieval_complete"])

    def test_seven_collections_nine_documents_ten_occurrences(self):
        html = "".join('<a href="' + BASE + '/WebLink/Browse.aspx?id=F' + str(n) + '">collection</a>' for n in range(1, 8))
        path, _ = legacy_export(self.export, html=html)
        value = self.enroll(path)
        self.page(value, "F1", self.members("B", "C"), 2)
        self.page(value, "F2", self.members("C", "D"), 2)
        self.page(value, "F3", [], 0)
        next_url = BASE + "/WebLink/Browse.aspx?id=F4&page=2"
        self.page(value, "F4", self.members("E"), 2, next_cursor=next_url)
        self.page(value, "F4", self.members("F"), 2, cursor=next_url)
        for key, letter in (("F5", "G"), ("F6", "H"), ("F7", "I")):
            self.page(value, key, self.members(letter), 1)

        class Transport:
            def open(self, url, *, timeout):
                body = PDFS[url.rsplit("/", 1)[-1]]
                return Response(200, {"content-type": "application/pdf", "content-length": str(len(body))}, (body,))

        approval = Approval(datetime.now(timezone.utc) + timedelta(hours=1), frozenset({HOST}), frozenset(), {HOST: ("/",)})
        ledger = WP1LedgerAdapter(queue=self.queue, database=self.root.ledger, cas_root=self.root.intake / "blobs")
        fetched = fetch_queue(self.queue, apply=True, approval_loader=lambda: approval,
                              egress=lambda url: True, transport=Transport(), ledger=ledger)
        self.assertEqual(fetched["received"], 8)  # C downloaded once, two collection occurrences.
        self.assertEqual(fetched["ledger_delivered"], 8)
        result = self.inventory.readiness(value.message_id)
        self.assertTrue(result["retrieval_complete"])
        self.assertEqual((result["unique_preserved_documents"], result["attachment_occurrences"] + result["linked_document_occurrences"]), (9, 10))
        self.assertFalse(result["digestion_complete"])
        self.assertFalse(result["publication_authorized"])
        self.assertEqual(self.count("originals"), 10)  # Nine documents AND original EML.

    def test_unknown_denominator_not_empty_and_terminal_mismatch_rejected(self):
        path, _ = legacy_export(self.export, body=BASE + "/WebLink/Browse.aspx?id=F1\n")
        value = self.enroll(path)
        data = self.page(value, "F1", [], None)
        self.assertFalse(self.inventory.readiness(value.message_id)["retrieval_complete"])
        self.assertEqual(len(self.inventory.pending_pages(value.message_id)), 1)
        changed = dict(data, expected_total=0)
        with root_lock(self.root.path), self.assertRaisesRegex(PortalError, "collection_page_conflict"):
            self.inventory.accept_page(self.collection("F1"), **changed)

    def test_pagination_loop_preserves_partial_membership(self):
        url = BASE + "/WebLink/Browse.aspx?id=F1"
        path, _ = legacy_export(self.export, body=url + "\n")
        value = self.enroll(path)
        next_url = url + "&page=2"
        self.page(value, "F1", self.members("B"), 2, next_cursor=next_url)
        with self.assertRaisesRegex(PortalError, "pagination_loop_or_bound"):
            self.page(value, "F1", self.members("C"), 2, cursor=next_url, next_cursor=url)
        self.assertFalse(self.inventory.readiness(value.message_id)["retrieval_complete"])
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM export_collection_members").fetchone()[0], 1)

    def test_authorization_expiry_exceptions_and_exhaustion_do_not_reset(self):
        path, _ = legacy_export(self.export, body="\n".join(BASE + "/WebLink/Browse.aspx?id=F" + str(n) for n in (1, 2, 3)))
        value = self.enroll(path)
        self.page(value, "F1", self.members("B"), 1)
        class Transient:
            def open(self, url, *, timeout):
                return Response(503, {})
        approval = Approval(datetime.now(timezone.utc) + timedelta(hours=1), frozenset({HOST}), frozenset(), {HOST: ("/",)})
        for _ in range(3):
            fetch_queue(self.queue, apply=True, approval_loader=lambda: approval, egress=lambda url: True,
                        transport=Transient(), limits=Limits(backoff=0))
        before = tuple(self.queue.db.execute("SELECT state,tries FROM portal_items").fetchone())
        self.enroll(path)
        self.assertEqual(tuple(self.queue.db.execute("SELECT state,tries FROM portal_items").fetchone()), before)
        self.assertEqual(before, ("blocked", 3))
        class Denied:
            def __init__(self, status):
                self.status = status
            def open(self, url, *, timeout):
                return Response(self.status, {})
        for key, letter, status in (("F2", "G", 401), ("F3", "I", 410)):
            self.page(value, key, self.members(letter), 1)
            fetched = fetch_queue(self.queue, apply=True, approval_loader=lambda: approval,
                egress=lambda url: True, transport=Denied(status), limits=Limits(backoff=0))
            self.assertEqual(fetched["blocked"], 1)
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM portal_items WHERE reason='expired_or_denied_link'").fetchone()[0], 2)
        with root_lock(self.root.path):
            self.inventory.block(self.collection("F1"), "authorization_required")
        self.enroll(path)
        self.assertEqual(self.inventory.pending_pages(value.message_id), [])
        self.assertFalse(self.inventory.readiness(value.message_id)["retrieval_complete"])

    def test_nested_collection_depth_and_cycle_do_not_escape_worklist(self):
        path, _ = legacy_export(self.export, body=BASE + "/WebLink/Browse.aspx?id=F1\n")
        value = self.enroll(path)
        for left, right in (("F1", "F2"), ("F2", "F3"), ("F3", "F4")):
            self.page(value, left, [{"id": right, "kind": "collection", "url": BASE + "/WebLink/Browse.aspx?id=" + right}], 1)
        self.assertEqual(self.inventory.pending_pages(value.message_id), [])
        self.assertIn("collection_cycle_or_depth", self.inventory.readiness(value.message_id)["gaps"])
        with self.assertRaisesRegex(PortalError, "collection_not_in_bounded_worklist"):
            self.page(value, "F4", [], 0)

    def test_unapproved_and_unclassified_links_remain_visible(self):
        path, _ = legacy_export(self.export, body="https://unapproved.example.invalid/file.pdf\n" + BASE + "/unknown\n")
        value = self.enroll(path)
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM export_body_links WHERE kind='blocked'").fetchone()[0], 2)
        self.assertIn("link_unresolved", self.inventory.readiness(value.message_id)["gaps"])

    def test_scope_drift_and_page_source_corruption_rejected(self):
        path, _ = legacy_export(self.export, body=BASE + "/WebLink/Browse.aspx?id=F1\n")
        value = self.enroll(path)
        changed = CollectionInventory(self.queue, Scope(frozenset({HOST})))
        with root_lock(self.root.path), self.assertRaisesRegex(PortalError, "export_event_conflict"):
            changed.register(value, ("fixture-account", "Records", "900", "41"))
        with root_lock(self.root.path), self.assertRaises((PortalError, OSError)):
            self.inventory.accept_page(self.collection("F1"), cursor=BASE + "/WebLink/Browse.aspx?id=F1",
                source_sha256="f" * 64, source_bytes=1, parser_sha256=PARSER, members=[], expected_total=0,
                event=value.message_id)

    def test_pipeline_uses_same_advance_path_but_reports_collection_gaps(self):
        path, _ = legacy_export(self.export, body=BASE + "/WebLink/Browse.aspx?id=F1\n")
        # The stage implementation is already separately qualified. This control
        # verifies the new branch reaches it with the preserved real occurrence.
        def advance():
            self.assertEqual(self.count("mail_messages"), 1)
            return []
        with patch.object(self.pipeline, "advance_all", side_effect=advance) as stages:
            report = self.pipeline.run(exports={"root": self.export, "receipts": [path], "inventory": self.inventory})
        stages.assert_called_once()
        self.assertEqual(report["status"], "completed_with_gaps")
        self.assertEqual(report["intake"]["events"][0]["attachment_occurrences"], 1)
        with self.assertRaises(RunError):
            self.pipeline.run(inbox=self.export, exports={})


if __name__ == "__main__":
    unittest.main()
