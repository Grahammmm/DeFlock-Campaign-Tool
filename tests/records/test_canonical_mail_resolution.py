"""T4B slice 2: CanonicalMailBackend resolves legacy unresolved originals (synthetic only).

Fictional EML and attachment bytes, a fictional legacy import, no mailbox, no network.
Covers test group D (16-23) of docs/RECORDS-LEGACY-MAIL-RESOLUTION.md; 24 belongs to the
adopt-mail slice, which owns policy loading and re-checking.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import mail_adoption
from campaign_tool.records.intake.mail_resolution_policy import ApprovedMailResolutionPolicy
from campaign_tool.records.ledger import occurrence_classification, original_resolution, store
from campaign_tool.records.ledger.occurrence_projection import project_occurrences
from campaign_tool.records.run import Pipeline
from campaign_tool.records.runner.adapters import LegacyIntakeBackend
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from campaign_tool.records.runner.contracts import Folder
from campaign_tool.records.runner.core import hid
from tests.records import test_ledger_occurrences as projection_fixtures
from tests.records.mail_resolution_fixtures import policy_bytes, sha
from tests.records.test_mail_adoption_regression import synthetic_receipt

ACCOUNT, FOLDER, VALIDITY = "synthetic-adopt-account", "Adoption", 900


class BackendResolutionCase(unittest.TestCase):
    """Legacy ledger declares the receipts' EML (and chosen attachments) as unresolved originals."""

    def setUp(self):
        self.helper = projection_fixtures.OccurrenceProjectionTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.export = self.base / "mail-history"
        self.export.mkdir(mode=0o700)
        self.root = Pipeline(self.base / "processing").root.path
        self.database = self.helper.database = self.root / "ledger.sqlite"
        self.output = self.root / "intake"
        self.legacy = []

    # -- fixture helpers -------------------------------------------------------
    def receipt(self, uid, attachments=0, body=None):
        path, receipt = synthetic_receipt(self.export, uid=uid, attachments=attachments, body=body)
        return path, receipt, hashlib.sha256(path.read_bytes()).hexdigest()

    def custom_receipt(self, uid, payloads):
        """A synthetic receipt whose attachments carry exactly `payloads` (repeats allowed)."""
        from email.message import EmailMessage
        from email.policy import SMTP
        from campaign_tool.records.intake import eml_export, mail_delta
        from tests.records.test_mail_adoption_regression import _blob
        msg = EmailMessage()
        msg["From"], msg["To"] = "records@adopt.example.invalid", "organizer@campaign.example.invalid"
        msg["Subject"], msg["Message-ID"] = "Synthetic custom %d" % uid, "<custom-%d@adopt.example.invalid>" % uid
        msg.set_content("Synthetic custom body %d.\n" % uid)
        for n, payload in enumerate(payloads):
            msg.add_attachment(payload, maintype="application", subtype="pdf", filename="part-%d.pdf" % n)
        for index, part in enumerate(msg.walk()):
            if part.is_multipart():
                part.set_boundary("synthetic-custom-%d-boundary-%d" % (uid, index))
        raw = msg.as_bytes(policy=SMTP)
        attached = []
        for index, part in enumerate(msg.walk()):
            if part.get_filename():
                item = _blob(self.export, part.get_payload(decode=True))
                item.update(part=index, filename=mail_delta.safe_filename(part.get_filename()),
                            original_filename=part.get_filename(), content_type=part.get_content_type())
                attached.append(item)
        receipt = {"complete": True, "account_id": ACCOUNT, "folder": FOLDER, "uidvalidity": VALIDITY, "uid": uid,
                   "bytes": len(raw), "internaldate": "2026-01-02T03:04:05Z",
                   "original_eml": _blob(self.export, raw), "attachments": attached}
        key = hashlib.sha256(json.dumps([receipt[k] for k in ("account_id", "folder", "uidvalidity", "uid")])
                             .encode()).hexdigest()
        path = self.export / "receipts" / (key + ".json")
        eml_export._ensure_private(path, (json.dumps(receipt, sort_keys=True) + "\n").encode())
        return path, receipt, hashlib.sha256(path.read_bytes()).hexdigest()

    def declare(self, size, digest, form, uid=None):
        with sqlite3.connect(self.helper.snapshot / "intake.sqlite") as con:
            con.execute("INSERT INTO docs(sha,bytes,format,stage,first_seen,review_status,version,digest) "
                        "VALUES(?,?,?,?,?,?,?,?)", (digest, size, form, "complete", "2000-01-01T00:00:00Z",
                                                     "full", "synthetic", "{}"))
        if form == "eml":  # the legacy mail occurrence the resolution must leave untouched
            self.helper.add_occurrence("legacy-" + digest[:12], digest, receipt={
                "mail_identity": {"account": ACCOUNT, "folder": FOLDER, "uidvalidity": VALIDITY, "uid": uid},
                "eml_sha256": digest})

    def build(self):
        """Import the declared legacy docs, project them and open the backend."""
        import_id = self.helper.capture()
        project_occurrences(self.database, import_id)
        return import_id

    def probe(self):
        """A throwaway intake output, so planning ids never touches the real one."""
        return LegacyIntakeBackend(self.export, Pipeline(self.base / "probe").root.path / "intake")

    def backend(self, policy=None):
        backend = CanonicalMailBackend(self.export, self.output, self.database, resolution_policy=policy)
        backend.start_run(mail_adoption._identity(None))
        return backend

    def binding(self, digest):
        with store.ledger(self.database, readonly=True) as con:
            for payload_sha, identity in con.execute("SELECT payload_sha256,identity_json FROM legacy_rows "
                                                     "WHERE source_table='docs'"):
                if json.loads(identity) == {"sha": digest}:
                    return payload_sha
        raise AssertionError("no legacy docs row")

    @staticmethod
    def message_id(uid):
        return mail_adoption.message_id((ACCOUNT, FOLDER, str(VALIDITY), str(uid)))

    def entry(self, digest, size, receipt, oid, dependents=()):
        return {"bytes": size, "role": "original", "scope": "in_scope", "verification_receipt_sha256": receipt,
                "binding_occurrence_id": oid, "dependent_receipts": sorted(dependents),
                "legacy_binding_sha256": self.binding(digest)}

    @staticmethod
    def policy(originals):
        raw = policy_bytes(originals)
        return ApprovedMailResolutionPolicy(raw, expected_sha256=sha(raw))

    def preserve(self, backend, path, uid):
        return backend.preserve(path, ACCOUNT, Folder(FOLDER, VALIDITY), uid)

    def rows(self, sql, args=()):
        with store.ledger(self.database, readonly=True) as con:
            return [tuple(row) for row in con.execute(sql, args)]

    def counts(self):
        with store.ledger(self.database, readonly=True) as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: self.rows("SELECT count(*) FROM %s" % name)[0][0] if name in tables else 0
                for name in ("original_resolutions", "occurrences", "mail_messages")}

    def frozen(self):
        """Rows that slice A promises a resolution never touches."""
        return {name: self.rows(sql) for name, sql in (
            ("legacy_rows", "SELECT * FROM legacy_rows ORDER BY 1,2,3"),
            ("projection", "SELECT * FROM occurrence_projection_rows ORDER BY 1,2,3"),
            ("legacy_occ", "SELECT * FROM occurrences WHERE acquisition_method LIKE 'captured_legacy_%' ORDER BY id"),
            ("originals", "SELECT * FROM originals ORDER BY sha256"))}

    def simple(self, uid=31):
        """One receipt whose EML is a legacy unresolved original; returns what the tests need."""
        path, receipt, digest = self.receipt(uid)
        eml = receipt["original_eml"]
        self.declare(eml["bytes"], eml["sha256"], "eml", uid)
        self.build()
        return path, eml, digest

    def resolve_policy(self, eml, digest, uid=31, dependents=()):
        return self.policy({eml["sha256"]: self.entry(eml["sha256"], eml["bytes"], digest, self.message_id(uid),
                                                      dependents)})


class SingleReceiptTests(BackendResolutionCase):
    def test_16_17_18_legacy_eml_is_resolved_and_adopted(self):
        path, eml, digest = self.simple()
        before = self.frozen()
        backend = self.backend(self.resolve_policy(eml, digest))
        self.preserve(backend, path, 31)
        self.assertEqual(self.counts()["original_resolutions"], 1)
        self.assertEqual(self.counts()["mail_messages"], 1)
        self.assertEqual(self.rows("SELECT acquisition_method FROM occurrences WHERE original_sha256=? "
                                   "ORDER BY acquisition_method", (eml["sha256"],)),
                         [("captured_legacy_mail",), ("verified_mail_export",)])
        self.assertEqual(self.rows("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                                   (eml["sha256"],)), [("done",)])
        with store.ledger(self.database, readonly=True) as con:
            effective = original_resolution.effective_original(con, eml["sha256"])
        self.assertEqual((effective["scope"], effective["storage_path"]),
                         ("in_scope", str(self.output / "blobs" / eml["sha256"])))
        self.assertTrue(effective["resolution_sha256"])
        # 17: legacy occurrence, legacy_rows, projection rows and the originals row are untouched.
        self.assertEqual(self.frozen(), before)
        # 18: the replay validator accepts the accepted evidence.
        self.assertTrue(backend._validator(backend._existing_preservation_context(eml["sha256"])))
        envelope = backend._existing_preservation_context(eml["sha256"])["preservation_evidence"]
        self.assertEqual(envelope["resolution_sha256"], effective["resolution_sha256"])

    def test_19_unlisted_unresolved_attachment_refuses_whole_receipt(self):
        path, receipt, digest = self.receipt(32, attachments=1)
        eml, att = receipt["original_eml"], receipt["attachments"][0]
        self.declare(eml["bytes"], eml["sha256"], "eml", 32)
        self.declare(att["bytes"], att["sha256"], "pdf")
        self.build()
        policy = self.resolve_policy(eml, digest, uid=32)  # attachment is not in the policy
        before = self.counts()
        with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
            self.preserve(self.backend(policy), path, 32)
        self.assertEqual(self.counts(), before)

    def test_20_without_policy_behaviour_is_unchanged(self):
        path, eml, _ = self.simple(33)
        before = self.counts()
        with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
            self.preserve(self.backend(), path, 33)
        self.assertEqual(self.counts(), before)

    def test_entry_for_other_receipt_or_wrong_bytes_is_refused(self):
        path, eml, digest = self.simple(34)
        wrong_receipt = self.policy({eml["sha256"]: self.entry(eml["sha256"], eml["bytes"], "e" * 64,
                                                               self.message_id(34))})
        wrong_bytes = self.policy({eml["sha256"]: self.entry(eml["sha256"], eml["bytes"] + 1, digest,
                                                             self.message_id(34))})
        before = self.counts()
        for policy in (wrong_receipt, wrong_bytes):
            with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
                self.preserve(self.backend(policy), path, 34)
            self.assertEqual(self.counts(), before)


class DuplicatePartTests(BackendResolutionCase):
    def setUp(self):
        super().setUp()
        # Two byte-identical attachment parts: same bytes O, two occurrence ids.
        self.path, self.receipt_data, self.digest = self.receipt(35, attachments=0)
        self.dup = self.duplicate_receipt()
        eml = self.dup["original_eml"]
        self.declare(eml["bytes"], eml["sha256"], "eml", 36)
        self.att = self.dup["attachments"][0]
        self.declare(self.att["bytes"], self.att["sha256"], "pdf")
        self.build()
        probe = self.probe()
        value = probe.preserve(self.dup_path, ACCOUNT, Folder(FOLDER, VALIDITY), 36)
        self.ids = sorted(hid(["attachment", value.message_id, loc]) for loc, sha_ in value.attachments
                          if sha_ == self.att["sha256"])
        self.assertEqual(len(self.ids), 2)

    def duplicate_receipt(self):
        payload = b"%PDF-1.4\nSynthetic duplicate attachment\n%%EOF\n"
        self.dup_path, receipt, self.dup_digest = self.custom_receipt(36, [payload, payload])
        return receipt

    def policy_for(self, binding_id):
        eml, att = self.dup["original_eml"], self.att
        return self.policy({
            eml["sha256"]: self.entry(eml["sha256"], eml["bytes"], self.dup_digest, self.message_id(36)),
            att["sha256"]: self.entry(att["sha256"], att["bytes"], self.dup_digest, binding_id)})

    def test_21_one_resolution_per_distinct_original(self):
        backend = self.backend(self.policy_for(self.ids[0]))
        self.preserve(backend, self.dup_path, 36)
        self.assertEqual(self.rows("SELECT capture_occurrence_id FROM original_resolutions WHERE "
                                   "original_sha256=?", (self.att["sha256"],)), [(self.ids[0],)])
        self.assertEqual(self.counts()["original_resolutions"], 2)
        self.assertEqual(self.rows("SELECT count(*) FROM occurrences WHERE original_sha256=? AND "
                                   "acquisition_method='verified_mail_export'", (self.att["sha256"],)), [(2,)])
        self.assertEqual(self.rows("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                                   (self.att["sha256"],)), [("done",)])
        # 21c: preserve again; no new row and no conflict.
        before = self.counts()
        self.preserve(self.backend(self.policy_for(self.ids[0])), self.dup_path, 36)
        self.assertEqual(self.counts(), before)

    def test_21a_policy_decides_which_occurrence_is_named(self):
        self.preserve(self.backend(self.policy_for(self.ids[1])), self.dup_path, 36)
        self.assertEqual(self.rows("SELECT capture_occurrence_id FROM original_resolutions WHERE "
                                   "original_sha256=?", (self.att["sha256"],)), [(self.ids[1],)])

    def test_21b_occurrence_outside_the_receipt_is_refused(self):
        before = self.counts()
        with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
            self.preserve(self.backend(self.policy_for("d" * 64)), self.dup_path, 36)
        self.assertEqual(self.counts(), before)


class DependentReceiptTests(BackendResolutionCase):
    def test_22_dependent_receipt_waits_for_binding_receipt(self):
        # Two receipts carry the same legacy attachment bytes O.
        payload = b"%PDF-1.4\nSynthetic shared attachment\n%%EOF\n"
        p1, r1, d1 = self.custom_receipt(41, [payload])
        p2, r2, d2 = self.custom_receipt(42, [payload])
        att = r1["attachments"][0]
        self.assertEqual(att["sha256"], r2["attachments"][0]["sha256"])
        for uid, item in ((41, r1["original_eml"]), (42, r2["original_eml"])):
            self.declare(item["bytes"], item["sha256"], "eml", uid)
        self.declare(att["bytes"], att["sha256"], "pdf")
        self.build()
        probe = self.probe()
        v1 = probe.preserve(p1, ACCOUNT, Folder(FOLDER, VALIDITY), 41)
        oid = hid(["attachment", v1.message_id, [loc for loc, s in v1.attachments if s == att["sha256"]][0]])
        origs = {r1["original_eml"]["sha256"]: self.entry(r1["original_eml"]["sha256"], r1["original_eml"]["bytes"],
                                                          d1, self.message_id(41)),
                 att["sha256"]: self.entry(att["sha256"], att["bytes"], d1, oid, dependents=[d2])}
        origs[r2["original_eml"]["sha256"]] = self.entry(r2["original_eml"]["sha256"], r2["original_eml"]["bytes"],
                                                         d2, self.message_id(42))
        policy = self.policy(origs)
        backend = self.backend(policy)
        before = self.counts()
        with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
            self.preserve(backend, p2, 42)
        self.assertEqual(self.counts(), before)
        self.preserve(backend, p1, 41)
        row = self.rows("SELECT * FROM original_resolutions WHERE original_sha256=?", (att["sha256"],))
        resolutions = self.counts()["original_resolutions"]
        self.preserve(backend, p2, 42)
        self.assertEqual(self.rows("SELECT * FROM original_resolutions WHERE original_sha256=?", (att["sha256"],)), row)
        self.assertEqual(self.counts()["original_resolutions"], resolutions + 1)  # only R2's own EML
        self.assertEqual(self.rows("SELECT count(*) FROM occurrences WHERE original_sha256=? AND "
                                   "acquisition_method='verified_mail_export'", (att["sha256"],)), [(2,)])
        self.assertTrue(backend._validator(backend._existing_preservation_context(att["sha256"])))


class FaultTests(BackendResolutionCase):
    def test_23_raise_after_insert_before_commit_leaves_nothing(self):
        path, eml, digest = self.simple(51)
        policy = self.resolve_policy(eml, digest, uid=51)
        before = self.counts()
        real = original_resolution.append_mail_export

        def boom(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("synthetic fault after resolution insert")
        with mock.patch.object(original_resolution, "append_mail_export", boom):
            with self.assertRaises(RuntimeError):
                self.preserve(self.backend(policy), path, 51)
        self.assertEqual(self.counts(), before)

    def test_23_crash_after_commit_resumes(self):
        path, eml, digest = self.simple(52)
        policy = self.resolve_policy(eml, digest, uid=52)

        def factory(*args):
            raise RuntimeError("synthetic crash before preservation stage")
        backend = CanonicalMailBackend(self.export, self.output, self.database, resolution_policy=policy,
                                       stage_runner_factory=factory)
        backend.start_run(mail_adoption._identity(None))
        with self.assertRaises(RuntimeError):
            self.preserve(backend, path, 52)
        self.assertEqual(self.counts()["original_resolutions"], 1)
        self.assertEqual(self.rows("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                                   (eml["sha256"],)), [("pending",)])
        self.preserve(self.backend(policy), path, 52)
        self.assertEqual(self.rows("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                                   (eml["sha256"],)), [("done",)])
        self.assertEqual(self.counts()["original_resolutions"], 1)

    def test_23_dead_run_is_recovered(self):
        path, eml, digest = self.simple(53)
        backend = self.backend(self.resolve_policy(eml, digest, uid=53))
        identity = backend.run_identity
        self.assertEqual(backend.recover_run(identity), "finalized")


if __name__ == "__main__":
    unittest.main()
