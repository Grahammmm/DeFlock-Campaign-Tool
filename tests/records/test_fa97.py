"""FA97: persisted body-discovery replay, using synthetic producer receipts.

No mailbox, model, OCR or network is used. All roots are explicit temporary
roots. The acceptance launcher installs pre-import process-local boundaries;
ordinary unittest execution uses the same fixture and temporary-directory API.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests.records import test_export_bridge as producer
from campaign_tool.records.export_bridge import enroll_export
from campaign_tool.records.intake import mail_delta
from campaign_tool.records.portal.collections import CollectionInventory, Scope
from campaign_tool.records.portal.policy import PortalError
from campaign_tool.records.portal.store import Queue
from campaign_tool.records.run import Pipeline, root_lock
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend

HOST = producer.HOST
BASE = producer.BASE
EMPTY = [("1.1", "Synthetic empty delivery.\n", "text/plain")]
DOCUMENT = [("1.1", BASE + "/documents/B", "text/plain")]
COLLECTION = [("1.1", BASE + "/WebLink/Browse.aspx?id=F1", "text/plain")]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fa97-fixture-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.pipeline = Pipeline(self.base / "canonical")
        self.root = self.pipeline.root
        self.export = self.base / "export"
        self.export.mkdir(mode=0o700)
        self.queue = Queue(self.root.path / "portal")
        self.addCleanup(lambda: self.queue.close())
        self.scope = Scope(frozenset({HOST}), frozenset({"social.example.invalid"}))
        self.inventory = CollectionInventory(self.queue, self.scope)
        self.backend = CanonicalMailBackend(self.export, self.root.intake, self.root.ledger)
        self.backend.start_run(self.pipeline._identity())
        self.receipt, _ = producer.legacy_export(self.export, body=EMPTY[0][1])

    def create(self, *, pending=False):
        with root_lock(self.root.path):
            if pending:
                _, identity, _ = mail_delta.load_receipt(self.receipt)
                account, folder, validity, uid = identity
                from campaign_tool.records.runner.contracts import Folder
                value = self.backend.preserve(self.receipt, account, Folder(folder, int(validity)), int(uid))
                self.inventory.register(value, identity)
            else:
                value = enroll_export(self.receipt, export_root=self.export,
                                      backend=self.backend, inventory=self.inventory)
        self.event = value.message_id
        return value

    def discover(self, bodies):
        with root_lock(self.root.path):
            return self.inventory.discover(self.event, bodies)

    def status(self):
        return self.queue.db.execute("SELECT discovery FROM export_events WHERE id=?",
                                     (self.event,)).fetchone()[0]

    def snapshot(self):
        # Every portal table includes notice counts, generations, attempts and
        # versions; every canonical table and immutable source byte is compared.
        portal = tuple(self.queue.db.iterdump())
        with sqlite3.connect(self.root.ledger) as con:
            canonical = tuple(con.iterdump())
        with sqlite3.connect(self.root.intake_db) as con:
            intake = tuple(con.iterdump())
        files = {}
        for root in (self.export, self.root.intake / "blobs", self.queue.objects):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    files[str(path.relative_to(self.base))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return portal, canonical, intake, files, self.inventory.readiness(self.event)

    def refuse(self, bodies, code="body_discovery_conflict"):
        before = self.snapshot()
        with self.assertRaises(PortalError) as caught:
            self.discover(bodies)
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(self.snapshot(), before)

    def reopen(self):
        self.queue.close()
        self.queue = Queue(self.root.path / "portal")
        self.inventory = CollectionInventory(self.queue, self.scope)

    def pending(self, bodies):
        self.create(pending=True)
        self.assertEqual(self.status(), "pending")
        self.assertEqual(self.inventory.readiness(self.event)["gaps"], ["body_discovery_incomplete"])
        self.assertIsNone(self.discover(bodies))
        self.assertEqual(self.status(), "enumerated")


class Behavior(Fixture):
    def test_completed_empty_reopen_same_empty_is_noop(self):
        self.create()
        self.assertEqual(self.status(), "enumerated")
        self.reopen()
        before = self.snapshot()
        self.assertIsNone(self.discover(EMPTY))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.inventory.readiness(self.event), {
            "event": self.event, "retrieval_complete": True, "attachment_occurrences": 1,
            "linked_document_occurrences": 0, "unique_preserved_documents": 1,
            "gaps": [], "digestion_complete": False, "publication_authorized": False})

    def test_completed_empty_reopen_nonempty_refuses_before_targets(self):
        self.create()
        self.reopen()
        self.assertEqual(self.status(), "enumerated")
        for bodies in (DOCUMENT, COLLECTION):
            with self.subTest(kind=bodies):
                self.refuse(bodies)

    def test_enroll_export_conflicting_replays_preserve_completed_empty(self):
        self.create()
        self.reopen()
        discover = self.inventory.discover
        before = self.snapshot()
        for changed in (DOCUMENT, COLLECTION):
            # Simulate changed body-link extraction from the same preserved
            # receipt, while running the real discover and enrollment handler.
            with patch.object(self.inventory, "discover",
                              side_effect=lambda event, bodies: discover(event, changed)):
                for attempt in range(2):
                    with self.subTest(bodies=changed, attempt=attempt):
                        with root_lock(self.root.path), self.assertRaises(PortalError) as caught:
                            enroll_export(self.receipt, export_root=self.export,
                                          backend=self.backend, inventory=self.inventory)
                        self.assertEqual(str(caught.exception), "body_discovery_conflict")
                        self.assertEqual(self.status(), "enumerated")
                        self.assertEqual(self.snapshot(), before)

    def test_nonempty_to_empty_refuses(self):
        self.pending(DOCUMENT)
        self.reopen()
        self.refuse(EMPTY)

    def test_changed_nonempty_declarations_refuse_before_targets(self):
        self.pending(DOCUMENT)
        self.reopen()
        alternatives = [COLLECTION,
            [("1.1", BASE + "/documents/C", "text/plain")],
            [("1.2", DOCUMENT[0][1], "text/plain")],
            [("1.1", BASE + "/documents/C " + DOCUMENT[0][1], "text/plain")]]
        for bodies in alternatives:
            with self.subTest(bodies=bodies):
                self.refuse(bodies)

    def test_canonical_distinct_part_reorder_is_noop(self):
        bodies = [*DOCUMENT, ("1.2", '<a href="' + COLLECTION[0][1] + '">Records</a>', "text/html"),
                  ("1.3", "https://social.example.invalid/profile", "text/plain")]
        self.pending(bodies)
        self.reopen()
        before = self.snapshot()
        self.assertIsNone(self.discover(list(reversed(bodies))))
        self.assertEqual(self.snapshot(), before)

    def test_pending_empty_first_discovery_is_valid(self):
        self.pending(EMPTY)
        self.assertTrue(self.inventory.readiness(self.event)["retrieval_complete"])

    def test_pending_nonempty_first_discovery_is_valid(self):
        self.pending([*DOCUMENT, ("1.2", COLLECTION[0][1], "text/plain")])
        rows = self.queue.db.execute("SELECT kind FROM export_body_links ORDER BY part").fetchall()
        self.assertEqual([r[0] for r in rows], ["document", "collection"])
        self.assertEqual(self.inventory.readiness(self.event)["gaps"],
                         ["collection_membership_unknown_or_exception", "document_pending_or_exception"])

    def test_scope_drift_precedes_empty_and_nonempty_replay(self):
        self.create()
        self.inventory = CollectionInventory(self.queue, Scope(frozenset({HOST}), max_links=99))
        before = tuple(self.queue.db.iterdump())
        for bodies in (EMPTY, DOCUMENT):
            with root_lock(self.root.path), self.assertRaises(PortalError) as caught:
                self.inventory.discover(self.event, bodies)
            self.assertEqual(str(caught.exception), "export_event_scope_mismatch")
            self.assertEqual(tuple(self.queue.db.iterdump()), before)

    def test_blocked_empty_recovery_remains_valid(self):
        self.create(pending=True)
        with root_lock(self.root.path):
            self.inventory.discovery_failed(self.event)
        self.assertEqual(self.status(), "blocked")
        self.assertIsNone(self.discover(DOCUMENT))
        self.assertEqual(self.status(), "enumerated")
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM portal_items").fetchone()[0], 1)

    def test_blocked_nonempty_same_recovery_preserves_targets(self):
        self.pending(DOCUMENT)
        with root_lock(self.root.path):
            self.inventory.discovery_failed(self.event)
        self.assertEqual(self.status(), "blocked")
        self.refuse(COLLECTION)
        portal_before = tuple(self.queue.db.execute("SELECT * FROM portal_items").fetchone())
        notices_before = [tuple(r) for r in self.queue.db.execute("SELECT * FROM portal_notices")]
        self.assertIsNone(self.discover(DOCUMENT))
        self.assertEqual(self.status(), "enumerated")
        self.assertEqual(tuple(self.queue.db.execute("SELECT * FROM portal_items").fetchone()), portal_before)
        self.assertEqual([tuple(r) for r in self.queue.db.execute("SELECT * FROM portal_notices")], notices_before)

    def test_generation_and_attempt_history_survives_replay(self):
        from datetime import datetime, timedelta, timezone
        from campaign_tool.records.portal.engine import Limits, Response, fetch_queue
        from campaign_tool.records.portal.policy import Approval
        self.pending(DOCUMENT)
        item = self.queue.db.execute("SELECT * FROM portal_items").fetchone()
        self.queue.inventory(HOST, item["request_id"], item["item_id"],
                             DOCUMENT[0][1] + "?revision=2", item["id"],
                             refresh=True, expected_generation=1)

        class Unavailable:
            def open(self, url, *, timeout):
                return Response(503, {})

        approval = Approval(datetime.now(timezone.utc) + timedelta(hours=1),
                            frozenset({HOST}), frozenset(), {HOST: ("/",)})
        for _ in range(3):
            fetch_queue(self.queue, apply=True, approval_loader=lambda: approval,
                        egress=lambda url: True, transport=Unavailable(), limits=Limits(backoff=0))
        self.reopen()
        item = self.queue.db.execute("SELECT generation,tries,state FROM portal_items").fetchone()
        self.assertEqual(tuple(item), (2, 3, "blocked"))
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM portal_attempts").fetchone()[0], 3)
        before = self.snapshot()
        self.assertIsNone(self.discover(DOCUMENT))
        self.assertEqual(self.snapshot(), before)
        self.refuse(COLLECTION)

    def test_changed_kind_and_reason_declarations_refuse(self):
        self.pending(DOCUMENT)
        for classified in ((DOCUMENT[0][1], "blocked", "unapproved_origin"),
                           (DOCUMENT[0][1], "document", "synthetic_changed_reason")):
            with patch.object(self.inventory, "_classify", return_value=classified):
                self.refuse(DOCUMENT)


class GateControl(unittest.TestCase):
    def test_launcher_identity_still_requires_manifest(self):
        from tools import accept_fa97
        with tempfile.TemporaryDirectory(prefix="fa97-missing-manifest-") as directory:
            with self.assertRaises(FileNotFoundError):
                accept_fa97.identity(accept_fa97.REPO, Path(directory) / "absent.json")

    def test_disposable_truthiness_mutant_and_restored_positive(self):
        # The launcher owns copying, file-launched children and raw receipts.
        # Ordinary unittest uses the identical driver under its temporary root.
        from tools import accept_fa97
        directory = Path(tempfile.mkdtemp(prefix="fa97-control-"))
        # A normal checkout has no admission-time SOURCE-MANIFEST.json beside
        # it. Supply an explicitly synthetic manifest only for this unit test;
        # the standalone launcher still requires its real admission manifest.
        manifest = directory / "synthetic-source-manifest.json"
        manifest.write_text(json.dumps({"fixture": "fa97-unittest-only"}) + "\n")
        with patch.object(accept_fa97, "SOURCE_MANIFEST", manifest):
            result = accept_fa97.negative_control(directory)
        expected_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.assertEqual(result["parent_identity"]["source_manifest_sha256"], expected_hash)
        for case, name in (("mutant", "targeted-defect"),
                           ("mutant-same-empty", "same-empty"),
                           ("restored", "positive")):
            receipt = json.loads((directory / case / (name + ".json")).read_text())
            self.assertEqual(receipt["identity"]["source_manifest_sha256"], expected_hash)
            self.assertEqual(receipt["candidate_identity"]["source_manifest_sha256"], expected_hash)
        self.assertTrue(result["detected"])
        self.assertEqual(result["mutant_failed_tests"], [
            "tests.records.test_fa97.Behavior.test_completed_empty_reopen_nonempty_refuses_before_targets"])
        self.assertEqual(result["mutant_same_empty_exit"], 0)
        self.assertEqual(result["restored_positive_exit"], 0)


if __name__ == "__main__":
    unittest.main()
