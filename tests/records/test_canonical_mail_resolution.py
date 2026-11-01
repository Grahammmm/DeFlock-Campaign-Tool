"""D16-23: real schema, verified synthetic MIME, sole writer and recovery.

D24 file re-read belongs to S3b. This slice tests its immutable source-pin
contract only, not classification/apply integration or owner authorization.
"""
import copy
from email.policy import SMTP
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
import multiprocessing
from unittest.mock import patch

from campaign_tool.records.intake import eml_export, local_capture
from campaign_tool.records.ledger import store, stages, original_resolution as resolution
from campaign_tool.records.ledger.mail_resolution_context import MailResolutionReadContext
from campaign_tool.records.ledger.mail_resolution_policy import ApprovedMailResolutionPolicy
from campaign_tool.records.ledger.occurrence_projection import project_occurrences
from campaign_tool.records.run import Pipeline, root_lock

from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from campaign_tool.records.runner.canonical_reconciliation import CanonicalOriginalReconciler
from campaign_tool.records.runner.contracts import Folder
from campaign_tool.records.runner.core import hid
from tests.records.test_export_bridge import synthetic_message, PDFS
from tests.records import test_ledger_occurrences as projection_fixtures
from tests.records.test_mail_resolution_policy import approved, value
from tests.records.test_original_reconciliation import fictional_policy
from types import SimpleNamespace
import os
from contextlib import closing


def export(root, uid, duplicate=False):
    """Independent legacy-export vocabulary, not output from the writer under test."""
    msg, _ = synthetic_message(body=f"Synthetic receipt {uid}.\n")
    if duplicate:
        msg.add_attachment(PDFS["A"], maintype="application", subtype="pdf", filename="again.pdf")
    raw = msg.as_bytes(policy=SMTP)

    def blob(data):
        sha = hashlib.sha256(data).hexdigest()
        path = Path("objects") / sha[:2] / sha
        eml_export._ensure_private(root / path, data)
        return {"path": str(path), "sha256": sha, "bytes": len(data)}

    attachments = []
    for index, part in enumerate(msg.walk()):
        if part.get_filename():
            item = blob(part.get_payload(decode=True))
            item.update(part=index, filename=part.get_filename(), original_filename=part.get_filename(),
                        content_type=part.get_content_type())
            attachments.append(item)
    receipt = {"complete": True, "account_id": "fixture-account", "folder": "Records",
               "uidvalidity": 900, "uid": uid, "bytes": len(raw),
               "internaldate": "2026-01-02T03:04:05Z", "original_eml": blob(raw), "attachments": attachments}
    path = root / "receipts" / f"synthetic-{uid}.json"
    eml_export._ensure_private(path, (json.dumps(receipt, sort_keys=True) + "\n").encode())
    return path, receipt


class World:
    def __init__(self, owner, *, eml=True, attachment=False, duplicate=False, larger=False,
                 first_seen="2000-01-01T00:00:00Z"):
        self.helper = projection_fixtures.OccurrenceProjectionTests()
        self.helper.setUp()
        owner.addCleanup(self.helper.doCleanups)
        self.database = self.helper.database
        self.base = self.database.parent.resolve()
        self.export = self.base.parent / "frozen-export"
        self.export.mkdir(mode=0o700)
        self.path, self.receipt = export(self.export, 41, duplicate)
        self.other_path, self.other = export(self.export, 42)
        self.third_path, self.third = export(self.export, 43)
        self.eml = self.receipt["original_eml"]["sha256"]
        self.attachment = self.receipt["attachments"][0]["sha256"]
        self.subject = self.attachment if attachment else self.eml
        declared = []
        if eml: declared.append((self.eml, self.receipt["original_eml"]["bytes"], "eml"))
        if attachment: declared.append((self.attachment, self.receipt["attachments"][0]["bytes"], "pdf"))
        with closing(sqlite3.connect(self.helper.snapshot / "intake.sqlite")) as con, con:
            for sha, size, form in declared:
                con.execute("INSERT INTO docs VALUES(?,?,?,?,?,?,?,?)", (sha, size, form, "complete",
                            first_seen, "full", "synthetic", "{}"))
        for sha, _, _ in declared:
            self.helper.add_occurrence("declared-" + sha, sha)
        import_id = self.helper.capture()
        project_occurrences(self.database, import_id)
        self.pipeline = Pipeline(self.base)
        self.output = self.pipeline.root.intake
        self.identity = self.pipeline._identity()
        self.identity["run_id"] = "synthetic-s2-first"
        self.entries = {}
        for sha, size, _ in declared:
            legacy = self.rows("SELECT payload_sha256 FROM legacy_rows WHERE source_table='docs' AND identity_json=?",
                               (store.canonical({"sha": sha}),))[0][0]
            oid = self.mid(41) if sha == self.eml else sorted(self.attachment_ids(41, duplicate))[-1 if larger else 0]
            self.entries[sha] = value(sha, size, self.digest(self.path), oid, legacy)["originals"][sha]
        self.policy_value = dict(value(), originals=self.entries)
        self.policy = approved(self.policy_value)
        self.backend = self.make_backend(self.policy)
        self.backend.start_run(self.identity)

    @staticmethod
    def mid(uid):
        return hid(["mail", "fixture-account", "Records", 900, uid])

    @classmethod
    def attachment_ids(cls, uid, duplicate=False):
        return [hid(["attachment", cls.mid(uid), loc]) for loc in (("1.2", "1.3") if duplicate else ("1.2",))]

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def rows(self, sql, args=()):
        with store.ledger(self.database, readonly=True) as con:
            return [tuple(row) for row in con.execute(sql, args)]

    def snapshot(self):
        with store.ledger(self.database, readonly=True) as con:
            return list(con.iterdump())

    def make_backend(self, policy, context=None):
        backend = CanonicalMailBackend(self.export, self.output, self.database,
                                       resolution_policy=policy, resolution_context=context)
        backend.run_identity = self.identity
        return backend

    def preserve(self, path=None, backend=None):
        path = self.path if path is None else path
        declaration = json.loads(path.read_bytes())
        with root_lock(self.base):
            return (backend or self.backend).preserve(path, "fixture-account", Folder("Records", 900), declaration["uid"])

    def historical(self, policy=None):
        return MailResolutionReadContext(self.output, policy=policy,
            historical_policies={sha: (self.policy.sha256, self.policy) for sha in self.entries})

    def next_run(self, backend):
        self.backend.finish_run(self.identity, "slice_completed", {"synthetic": True})
        identity = copy.deepcopy(self.identity)
        identity["run_id"] = "synthetic-s2-next"
        backend.start_run(identity)


class CanonicalMailResolutionTests(unittest.TestCase):
    def world(self, **kw):
        return World(self, **kw)

    def test_d16_d17_d18_real_preservation_immutable_baseline_and_replay(self):
        w = self.world()
        tables = ("originals", "legacy_rows", "occurrence_projection_rows")
        before = {table: w.rows("SELECT * FROM " + table + " ORDER BY 1,2") for table in tables}
        declared = w.rows("SELECT * FROM occurrences WHERE acquisition_method LIKE 'captured_legacy_%' ORDER BY id")
        result = w.preserve()
        self.assertEqual(result.eml_sha256, w.eml)
        self.assertEqual(w.rows("SELECT count(*) FROM original_resolutions"), [(1,)])
        self.assertEqual(w.rows("SELECT count(*) FROM mail_messages"), [(1,)])
        self.assertEqual(w.rows("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'", (w.eml,)), [("done",)])
        self.assertEqual(w.rows("SELECT DISTINCT status FROM stage_state WHERE original_sha256=? AND stage!='preserve'", (w.eml,)), [("pending",)])
        for table in tables:
            # New originals are allowed, all existing declarations are immutable.
            after = w.rows("SELECT * FROM " + table + " ORDER BY 1,2")
            self.assertTrue(all(row in after for row in before[table]))
            if table != "originals": self.assertEqual(after, before[table])
        self.assertEqual(w.rows("SELECT * FROM occurrences WHERE acquisition_method LIKE 'captured_legacy_%' ORDER BY id"), declared)
        context = w.backend._existing_preservation_context(w.eml)
        bound = w.rows("SELECT binding_sha256 FROM original_resolutions")[0][0]
        self.assertEqual(context["preservation_evidence"]["resolution_sha256"], bound)
        self.assertEqual(context["preservation_evidence"]["occurrence_ids"], [w.mid(41)])
        self.assertTrue(w.backend._validator(context))
        before = w.snapshot()
        w.preserve()
        self.assertEqual(w.snapshot(), before)
        self.assertEqual(w.rows("PRAGMA foreign_key_check"), [])
        self.assertEqual(w.rows("PRAGMA quick_check"), [("ok",)])

    def test_legacy_declaration_without_first_seen_preserves_with_occurrence_time(self):
        # Real legacy declarations can lack first_seen; the preserve receipt then
        # needs a valid timezone-aware time instead of NULL (invalid_timestamp).
        w = self.world(first_seen=None)
        self.assertEqual(w.rows("SELECT first_seen_at FROM originals WHERE sha256=?", (w.eml,)), [(None,)])
        w.preserve()
        self.assertEqual(w.rows("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'", (w.eml,)), [("done",)])
        acquired = w.rows("SELECT acquired_at FROM occurrences WHERE id=?", (w.mid(41),))[0][0]
        created = w.rows("SELECT r.created_at_tz FROM receipts r JOIN stage_state s ON s.receipt_sha256=r.sha256 "
                         "WHERE s.original_sha256=? AND s.stage='preserve'", (w.eml,))
        self.assertEqual(created, [(acquired,)])
        self.assertEqual(w.rows("SELECT first_seen_at FROM originals WHERE sha256=?", (w.eml,)), [(None,)])
        before = w.snapshot()
        w.preserve()
        self.assertEqual(w.snapshot(), before)

    def test_d19_missing_attachment_policy_rolls_back_whole_receipt(self):
        w = self.world(attachment=True)
        v = copy.deepcopy(w.policy_value)
        del v["originals"][w.attachment]
        backend = w.make_backend(approved(v))
        before = w.snapshot()
        with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
            w.preserve(backend=backend)
        self.assertEqual(w.snapshot(), before)

    def test_d20_no_policy_unresolved_still_refused(self):
        w = self.world()
        before = w.snapshot()
        with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
            w.preserve(backend=w.make_backend(None))
        self.assertEqual(w.snapshot(), before)

    def test_d20_no_policy_new_receipt_is_unchanged(self):
        w = self.world(eml=False)
        backend = w.make_backend(None)
        w.preserve(backend=backend)
        self.assertFalse(resolution.MAIL_SCHEMA in str(w.snapshot()))
        before = w.snapshot()
        w.preserve(backend=backend)
        self.assertEqual(w.snapshot(), before)

    def test_d21_duplicate_parts_each_policy_binding_and_replay(self):
        for larger in (False, True):
            with self.subTest(larger=larger):
                w = self.world(eml=False, attachment=True, duplicate=True, larger=larger)
                with patch.object(resolution, "append_mail_export", wraps=resolution.append_mail_export) as append:
                    w.preserve()
                    self.assertEqual(append.call_count, 1)
                expected = w.entries[w.attachment]["binding_occurrence_id"]
                self.assertEqual(w.rows("SELECT capture_occurrence_id FROM original_resolutions"), [(expected,)])
                self.assertEqual(w.rows("SELECT id FROM occurrences WHERE original_sha256=? AND acquisition_method='verified_mail_export' ORDER BY id", (w.attachment,)),
                                 [(oid,) for oid in sorted(w.attachment_ids(41, True))])
                self.assertEqual(w.rows("SELECT count(*) FROM stage_events WHERE original_sha256=? AND stage='preserve' AND status='done'", (w.attachment,)), [(1,)])
                before = w.snapshot()
                w.preserve()
                self.assertEqual(w.snapshot(), before)

    def test_d21_nonmember_binding_occurrence_refused_without_writes(self):
        w = self.world(eml=False, attachment=True, duplicate=True)
        v = copy.deepcopy(w.policy_value)
        v["originals"][w.attachment]["binding_occurrence_id"] = w.mid(41)  # Genuine id, WRONG original.
        before = w.snapshot()
        with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
            w.preserve(backend=w.make_backend(approved(v)))
        self.assertEqual(w.snapshot(), before)

    def test_d22_dependent_before_binding_then_after_committed_binding(self):
        w = self.world(eml=False, attachment=True)
        v = copy.deepcopy(w.policy_value)
        v["originals"][w.attachment]["dependent_receipts"] = [w.digest(w.other_path)]
        w.policy = approved(v)
        w.backend = w.make_backend(w.policy)
        before = w.snapshot()
        with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
            w.preserve(w.other_path)
        self.assertEqual(w.snapshot(), before)
        w.preserve()
        row = w.rows("SELECT * FROM original_resolutions")
        context = w.backend._existing_preservation_context(w.attachment)
        w.preserve(w.other_path)
        self.assertEqual(w.rows("SELECT * FROM original_resolutions"), row)
        self.assertEqual(w.backend._existing_preservation_context(w.attachment), context)
        self.assertTrue(w.backend._validator(context))
        self.assertEqual(w.rows("SELECT count(*) FROM mail_messages"), [(2,)])

    def test_resolved_unlisted_receipt_still_refused(self):
        w = self.world(eml=False, attachment=True)
        w.preserve()
        before = w.snapshot()
        with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
            w.preserve(w.third_path)
        self.assertEqual(w.snapshot(), before)

    def test_successor_with_explicit_historical_pin_never_rebinds(self):
        w = self.world(eml=False, attachment=True)
        w.preserve()
        row = w.rows("SELECT * FROM original_resolutions")
        v = copy.deepcopy(w.policy_value)
        v["approval_sha256"] = "1" * 64
        v["originals"][w.attachment]["dependent_receipts"] = [w.digest(w.third_path)]
        successor = approved(v)
        backend = w.make_backend(successor, w.historical(successor))
        w.next_run(backend)
        with patch.object(resolution, "append_mail_export", wraps=resolution.append_mail_export) as append:
            w.preserve(w.third_path, backend=backend)
            self.assertEqual(append.call_count, 0)
        self.assertEqual(w.rows("SELECT * FROM original_resolutions"), row)
        self.assertEqual(w.rows("SELECT policy_sha256 FROM original_resolutions"), [(w.policy.sha256,)])

    def test_successor_missing_historical_pin_cannot_select_trust_from_row(self):
        w = self.world(eml=False, attachment=True)
        w.preserve()
        v = copy.deepcopy(w.policy_value)
        v["approval_sha256"] = "1" * 64
        v["originals"][w.attachment]["dependent_receipts"] = [w.digest(w.third_path)]
        successor = approved(v)
        before = w.snapshot()
        with self.assertRaisesRegex(ValueError, "^resolution_policy_mismatch$"):
            w.preserve(w.third_path, backend=w.make_backend(successor))
        with self.assertRaisesRegex(ValueError, "^resolution_policy_mismatch$"):
            MailResolutionReadContext(w.output, historical_policies={w.attachment: (w.policy.sha256, successor)})
        with store.ledger(w.database, readonly=True) as con:
            with self.assertRaisesRegex(ValueError, "^resolution_policy_invalid$"):
                store.effective_original(con, w.attachment)
        self.assertEqual(w.snapshot(), before)

    def test_unpin_requires_explicit_historical_trust(self):
        w = self.world(eml=False, attachment=True)
        w.preserve()
        before = w.snapshot()
        with self.assertRaisesRegex(ValueError, "^resolution_policy_mismatch$"):
            w.preserve(w.third_path, backend=w.make_backend(None, MailResolutionReadContext(w.output)))
        self.assertEqual(w.snapshot(), before)
        backend = w.make_backend(None, w.historical())
        w.next_run(backend)
        row = w.rows("SELECT * FROM original_resolutions")
        w.preserve(w.third_path, backend=backend)
        self.assertEqual(w.rows("SELECT * FROM original_resolutions"), row)

    def test_changed_binding_omitted_and_held_successor_refused(self):
        for field in ("verification_receipt_sha256", "binding_occurrence_id", "legacy_binding_sha256", "omitted", "held"):
            with self.subTest(field=field):
                w = self.world(eml=False, attachment=True)
                w.preserve()
                v = copy.deepcopy(w.policy_value)
                v["approval_sha256"] = "1" * 64
                if field in ("omitted", "held"):
                    del v["originals"][w.attachment]
                    if field == "held": v["held"] = [w.attachment]
                else: v["originals"][w.attachment][field] = "0" * 64
                successor = approved(v)
                backend = w.make_backend(successor, w.historical(successor))
                before = w.snapshot()
                with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
                    w.preserve(w.third_path, backend=backend)
                self.assertEqual(w.snapshot(), before)

    def test_d23_raise_after_append_rolls_back_mail_occurrences_and_resolution(self):
        w = self.world(attachment=True)
        before = w.snapshot()
        actual = resolution.append_mail_export
        def fail(con, **kw):
            self.assertTrue(con.in_transaction)
            self.assertTrue(con.execute("SELECT 1 FROM occurrences WHERE id=?", (kw["occurrence_id"],)).fetchone())
            actual(con, **kw)
            raise RuntimeError("synthetic-after-append")
        with patch.object(resolution, "append_mail_export", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "^synthetic-after-append$"): w.preserve()
        self.assertEqual(w.snapshot(), before)
        w.preserve()
        self.assertEqual(w.rows("SELECT count(*) FROM original_resolutions"), [(2,)])

    @unittest.skipUnless(hasattr(os, "fork"), "crash fixture requires fork")
    def test_d23_postcommit_prestage_recover_dead_run_and_resume(self):
        w = self.world()
        def crash():
            # Inherited synthetic paths and offline guards; NO new interpreter,
            # shell, private defaults, or inline command in this real dead run.
            with patch("campaign_tool.records.runner.canonical_mail.installed_preservation_runner",
                       side_effect=lambda *args, **kw: os._exit(73)):
                w.preserve()
            os._exit(74)
        child = multiprocessing.get_context("fork").Process(target=crash)
        child.start()
        try:
            child.join(30)
            self.assertEqual(child.exitcode, 73)
        finally:
            if child.is_alive():
                child.kill()
                child.join(10)
            child.close()
        row = w.rows("SELECT * FROM original_resolutions")
        self.assertEqual(len(row), 1)
        self.assertEqual(w.rows("SELECT count(*) FROM mail_messages"), [(1,)])
        self.assertEqual(w.rows("SELECT DISTINCT status FROM stage_state"), [("pending",)])
        # This is the backend's committed/incomplete cut, not S3 apply classification.
        with root_lock(w.base): self.assertEqual(w.backend.recover_run(w.identity), "finalized")
        self.assertEqual(w.rows("SELECT status FROM runs WHERE run_id=?", (w.identity["run_id"],)), [("interrupted",)])
        identity = copy.deepcopy(w.identity)
        identity["run_id"] = "synthetic-recovery"
        backend = w.make_backend(w.policy)
        backend.start_run(identity)
        with patch.object(resolution, "append_mail_export", wraps=resolution.append_mail_export) as append:
            w.preserve(backend=backend)
            self.assertEqual(append.call_count, 0)
        self.assertEqual(w.rows("SELECT * FROM original_resolutions"), row)
        self.assertEqual(w.rows("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'", (w.eml,)), [("done",)])
        self.assertTrue(backend._validator(backend._existing_preservation_context(w.eml)))

    def test_accepted_or_cached_original_is_not_eligible(self):
        for cached in (False, True):
            with self.subTest(cached=cached):
                w = self.world()
                runner = stages.testing_runner(w.database, run_id=w.identity["run_id"], owner="synthetic",
                    engine_version=w.identity["runtime"]["version"], config_sha256=w.identity["config_sha256"], validators={})
                original_bytes = (w.export / w.receipt["original_eml"]["path"]).read_bytes()
                runner.set_content(w.eml, "preserve", original_bytes, author_id="synthetic", tier="A")
                if not cached: runner.claim(w.eml, "preserve")
                before = w.snapshot()
                with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"): w.preserve()
                self.assertEqual(w.snapshot(), before)

    def test_unrelated_v1_resolution_is_byte_identical(self):
        w = self.world()
        source = w.base / "fictional.txt"
        source.write_bytes(b"synthetic original")
        source.chmod(0o400)
        reconciler = CanonicalOriginalReconciler(w.export, w.output, w.database,
            policy=fictional_policy(w.helper.sha, 18))
        reconciler.run_identity = w.identity
        with patch.object(local_capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            reconciler.enroll_local_capture(source, original_sha256=w.helper.sha)
        before = w.rows("SELECT * FROM original_resolutions WHERE original_sha256=?", (w.helper.sha,))
        w.preserve()
        self.assertEqual(w.rows("SELECT * FROM original_resolutions WHERE original_sha256=?", (w.helper.sha,)), before)

    def test_context_is_preservation_only_and_evidence_cannot_choose_it(self):
        w = self.world()
        w.preserve()
        with self.assertRaisesRegex(ValueError, "^resolution_policy_invalid$"):
            w.backend.stage_runner.set_content(w.eml, "extract", b"synthetic", author_id="synthetic", tier="A")
        forged = copy.deepcopy(w.backend._existing_preservation_context(w.eml))
        forged["preservation_evidence"]["resolution_sha256"] = "0" * 64
        self.assertFalse(w.backend._validator(forged))
        forged = copy.deepcopy(w.backend._existing_preservation_context(w.eml))
        declared = w.rows("SELECT id FROM occurrences WHERE original_sha256=? AND acquisition_method LIKE 'captured_legacy_%'", (w.eml,))[0][0]
        forged["preservation_evidence"]["occurrence_ids"].append(declared)
        self.assertFalse(w.backend._validator(forged))

    def test_s3b_source_pin_contract_only_not_d24_apply(self):
        w = self.world()
        directory = w.base / "resolution-policies"
        directory.mkdir(mode=0o700)
        path = directory / w.policy.sha256
        path.write_bytes(w.policy.raw)
        path.chmod(0o600)
        loaded = ApprovedMailResolutionPolicy.load(w.base, expected_sha256=w.policy.sha256)
        self.assertEqual((loaded.source_path, loaded.source_sha256), (str(path), w.policy.sha256))
        for name in ("source_path", "source_sha256", "_source_path", "_source_sha256"):
            with self.assertRaises(AttributeError): setattr(loaded, name, "changed")
        with self.assertRaisesRegex(ValueError, "^resolution_policy_mismatch$"):
            ApprovedMailResolutionPolicy(loaded.raw, expected_sha256=loaded.sha256,
                                         source_path=path, source_sha256="0" * 64)
        self.assertIsNone(w.policy.source_path)
        self.assertIsNone(w.policy.source_sha256)


if __name__ == "__main__":
    unittest.main()
