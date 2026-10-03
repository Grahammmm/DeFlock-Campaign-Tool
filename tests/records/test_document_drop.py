"""Synthetic genuine document acquisition, canonical receipts and extraction tests."""
import contextlib
from email.message import EmailMessage
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from campaign_tool.records import run, unattended
from campaign_tool.records.intake import document_drop as drop, eml_export
from campaign_tool.records.ledger import store, stages
from campaign_tool.records.runner.canonical_documents import CanonicalDocumentsBackend
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from campaign_tool.records.runner.contracts import Folder
from tests.records.ocr_fixtures import mixed_pdf


def xlsx_bytes():
    from openpyxl import Workbook
    book = Workbook()
    book.active.title = "Synthetic"
    book.active.append(["Item", "Count"])
    book.active.append(["Example", 3])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


class DocumentDropTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        os.chmod(self.base, 0o700)
        self.input = self.base / "documents"
        self.input.mkdir(mode=0o700)
        self.root = self.base / "records"

    def add(self, name="sample.xlsx", raw=None):
        path = self.input / name
        path.write_bytes(xlsx_bytes() if raw is None else raw)
        path.chmod(0o600)
        return path

    def readonly(self):
        return patch.object(drop.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY))

    def manifest(self):
        return drop.snapshot(str(self.input))

    def backend(self, suffix):
        pipeline = run.Pipeline(str(self.root))
        backend = CanonicalDocumentsBackend(pipeline.root.sub("mail"), pipeline.root.intake, pipeline.root.ledger)
        identity = pipeline._identity()
        identity["run_id"] = "document-test-" + suffix
        backend.start_run(identity)
        return pipeline, backend, identity

    def test_manifest_is_bounded_exact_bytes(self):
        path = self.add()
        manifest = self.manifest()
        self.assertEqual(manifest["documents"], 1)
        self.assertEqual(manifest["entries"][0]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaises(Exception):
            drop.snapshot(str(self.input), expected="0" * 64)

    def test_no_readonly_exemption(self):
        self.add()
        with patch.object(drop.os, "fstatvfs", return_value=SimpleNamespace(f_flag=0)):
            with self.assertRaises(Exception):
                drop.snapshot(str(self.input), readonly=True)

    def test_file_readonly_checked_not_only_directory(self):
        self.add()
        with patch.object(drop.os, "fstatvfs", side_effect=[
                SimpleNamespace(f_flag=os.ST_RDONLY), SimpleNamespace(f_flag=0)]):
            with self.assertRaises(Exception):
                drop.snapshot(str(self.input), readonly=True)

    def test_mutated_source_rejected(self):
        path = self.add()
        manifest = self.manifest()
        path.write_bytes(b"changed")
        with self.readonly(), self.assertRaises(Exception):
            drop.read_entry(str(self.input), manifest, manifest["entries"][0])

    def test_symlink_rejected(self):
        source = self.base / "outside.pdf"
        source.write_bytes(b"%PDF-synthetic")
        (self.input / "linked.pdf").symlink_to(source)
        with self.assertRaises(Exception):
            self.manifest()

    def test_hardlink_rejected(self):
        path = self.add()
        os.link(path, self.input / "other.xlsx")
        with self.assertRaises(Exception):
            self.manifest()

    def test_wrong_mode_rejected(self):
        self.add().chmod(0o644)
        with self.assertRaises(Exception):
            self.manifest()

    def test_nested_and_unknown_format_rejected(self):
        (self.input / "nested").mkdir(mode=0o700)
        with self.assertRaises(Exception):
            self.manifest()

    def test_png_not_claimed_supported(self):
        self.add("sample.png", b"synthetic")
        with self.assertRaises(Exception):
            self.manifest()

    def test_empty_drop_valid(self):
        manifest = self.manifest()
        self.assertEqual((manifest["documents"], manifest["bytes"]), (0, 0))

    def test_canonical_preserve_and_replay(self):
        self.add()
        manifest = self.manifest()
        pipeline, backend, identity = self.backend("first")
        with self.readonly():
            self.assertFalse(backend.preserve_document(str(self.input), manifest, manifest["entries"][0]))
        backend.finish_run(identity, "slice_completed", {"documents": 1})
        with store.ledger(pipeline.root.ledger, readonly=True) as con:
            rows = con.execute("SELECT * FROM occurrences").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "local")
            self.assertIsNone(rows[0]["parent_occurrence_id"])
            self.assertEqual(con.execute("SELECT count(*) FROM mail_messages").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT status FROM stage_state WHERE stage='preserve'").fetchone()[0], "done")
            source = json.loads(rows[0]["source_ref"])
            self.assertNotIn("agency", source)
            self.assertNotIn("request", source)
            self.assertEqual(drop.verify_local_occurrence(rows[0], pipeline.root.intake, manifest["entries"][0]["sha256"])[1], "xlsx")
        _, replay, identity = self.backend("second")
        with self.readonly():
            self.assertTrue(replay.preserve_document(str(self.input), manifest, manifest["entries"][0]))
        replay.finish_run(identity, "slice_completed", {"documents": 0})
        stages.counts(pipeline.root.ledger)

    def test_receipt_tamper_fails_closed(self):
        self.add()
        manifest = self.manifest()
        pipeline, backend, _ = self.backend("tamper")
        with self.readonly():
            backend.preserve_document(str(self.input), manifest, manifest["entries"][0])
        with store.ledger(pipeline.root.ledger, readonly=True) as con:
            row = con.execute("SELECT * FROM occurrences").fetchone()
            path = Path(json.loads(row["evidence"])["verification_receipt_path"])
            path.write_bytes(b"{}")
            with self.assertRaises(Exception):
                drop.verify_local_occurrence(row, pipeline.root.intake, manifest["entries"][0]["sha256"])

    def test_bad_format_retained_but_not_enrolled(self):
        path = self.add("broken.xlsx", b"not a workbook")
        manifest = self.manifest()
        pipeline, backend, _ = self.backend("bad")
        with self.readonly(), self.assertRaises(Exception):
            backend.preserve_document(str(self.input), manifest, manifest["entries"][0])
        subject = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual((pipeline.root.intake / "blobs" / subject).read_bytes(), path.read_bytes())
        with store.ledger(pipeline.root.ledger, readonly=True) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM originals").fetchone()[0], 0)

    def test_failed_attempt_consumes_budget(self):
        self.add("broken.pdf", b"not pdf")
        manifest = self.manifest()
        pipeline = unattended.UnattendedPipeline(str(self.root))
        with self.readonly():
            report = pipeline.run(documents=str(self.input), document_manifest_sha256=manifest["sha256"])
        self.assertEqual(len(report["intake"]["failures"]), 1)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["substantive_complete"])
        self.assertEqual(pipeline.safety.phases["intake"]["attempted"], 1)

    def test_genuine_cli_xlsx_extraction(self):
        self.add()
        manifest = self.manifest()
        output = io.StringIO()
        with self.readonly(), patch("campaign_tool.records.runtime.require", return_value=True), \
                patch.dict(os.environ, {key: os.environ[key] for key in ("PYTHONPATH", "TMPDIR", "PATH")
                                        if key in os.environ}, clear=True), contextlib.redirect_stdout(output):
            code = run.main(["--root", str(self.root), "--documents", str(self.input),
                             "--document-manifest-sha256", manifest["sha256"], "--unattended", "--json"])
        report = json.loads(output.getvalue())
        self.assertIn(code, (0, 3))
        self.assertEqual(report["intake"]["documents"], 1)
        self.assertEqual(report["intake"]["messages"], 0)
        self.assertEqual(len(report["intake"]["preserved"]), 1)
        self.assertFalse(report["substantive_complete"])
        self.assertEqual(report["substantive_review_status"], "queued")
        with store.ledger(run.Root(str(self.root)).ledger, readonly=True) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM originals").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT status FROM stage_state WHERE stage='extract'").fetchone()[0], "pending")
            self.assertEqual(con.execute("SELECT count(*) FROM units").fetchone()[0], 0)
        self.assertEqual(code, 3)
        self.assertEqual(report["subjects"][0]["stages"]["extract"], "blocked")
        receipts = list((self.root / "extract").glob("extract-*/extraction.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["parser"], "legacy-intake:xlsx")
        self.assertEqual(receipt["original_sha256"], manifest["entries"][0]["sha256"])
        warnings = [issue for issue in receipt["issues"]
                    if issue["code"] == "workbook_cached_values_and_objects_not_reviewed"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("no recalculation", warnings[0]["detail"])
        self.assertIn("object rendering", warnings[0]["detail"])
        self.assertIn("cached-value comparison", warnings[0]["detail"])
        self.assertTrue(any(unit["kind"] == "workbook_sheet" for unit in receipt["units"]))
        self.assertTrue(any(unit["kind"] == "workbook_row" and unit["locator"]["sheet"] == "Synthetic"
                            for unit in receipt["units"]))

    def test_pdf_partial_is_not_full_review(self):
        self.add("scan.pdf", mixed_pdf())
        manifest = self.manifest()
        pipeline = unattended.UnattendedPipeline(str(self.root))
        with self.readonly():
            report = pipeline.run(documents=str(self.input), document_manifest_sha256=manifest["sha256"])
        self.assertEqual(len(report["intake"]["preserved"]), 1)
        self.assertFalse(report["substantive_complete"])
        self.assertNotEqual(report["counts"]["extract"]["done"], 1)

    def test_ambiguous_cli_selectors_rejected(self):
        with self.assertRaises(SystemExit):
            unattended.main(["--root", str(self.root), "--documents", str(self.input),
                             "--document-manifest-sha256", "a" * 64, "--inbox", str(self.input)])

    def test_missing_manifest_rejected(self):
        pipeline = unattended.UnattendedPipeline(str(self.root))
        with self.assertRaises(run.RunError):
            pipeline.run(documents=str(self.input))

    def test_mixed_mail_and_local_shared_bytes_both_orders(self):
        raw = xlsx_bytes()
        self.add(raw=raw)
        manifest = self.manifest()
        for mail_first in (True, False):
            with self.subTest(mail_first=mail_first):
                root = self.base / ("mail-first" if mail_first else "local-first")
                pipeline = run.Pipeline(str(root))
                message = EmailMessage()
                message["Subject"] = "Synthetic attachment"
                message.set_content("Synthetic")
                message.add_attachment(raw, maintype="application", subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename="sample.xlsx")
                receipt = eml_export.export_message(message.as_bytes(), mail_root=pipeline.root.sub("mail"),
                                                     account="local", mailbox="inbox", uidvalidity=1, uid=1)
                mail = CanonicalMailBackend(pipeline.root.sub("mail"), pipeline.root.intake, pipeline.root.ledger)
                docs = CanonicalDocumentsBackend(pipeline.root.sub("mail"), pipeline.root.intake, pipeline.root.ledger)
                for backend, label in ((mail, "mail"), (docs, "docs")):
                    identity = pipeline._identity()
                    identity["run_id"] = label + "-" + pipeline.run_id
                    backend.start_run(identity)
                def local():
                    with self.readonly():
                        docs.preserve_document(str(self.input), manifest, manifest["entries"][0])
                def mailed():
                    mail.preserve(receipt, "local", Folder("inbox", 1), 1)
                for acquire in ((mailed, local) if mail_first else (local, mailed)):
                    acquire()
                summary = stages.counts(pipeline.root.ledger)
                self.assertEqual(summary["originals"], 2)
                subject = hashlib.sha256(raw).hexdigest()
                with store.ledger(pipeline.root.ledger, readonly=True) as con:
                    kinds = {row[0] for row in con.execute("SELECT kind FROM occurrences WHERE original_sha256=?", (subject,))}
                self.assertEqual(kinds, {"local", "attachment"})
                self.assertEqual(pipeline._form(subject, pipeline._original(subject)), "xlsx")

    def test_cap_one_manifest_bound_progress_prevents_starvation(self):
        first = self.add("first.xlsx")
        self.add("second.xlsx", xlsx_bytes() + b"synthetic ZIP trailing bytes")
        manifest = self.manifest()
        from campaign_tool.records.run_safety import RunSafety, RunSafetyPolicy
        policy = RunSafetyPolicy(max_fetches=1)
        def intake():
            pipeline = unattended.UnattendedPipeline(str(self.root), policy=policy)
            pipeline.safety = RunSafety(pipeline.root.ledger, policy)
            with self.readonly():
                report = pipeline.ingest_documents(str(self.input), manifest["sha256"])
            self.assertEqual(pipeline.safety.phases["intake"]["attempted"], 1)
            return report
        one, two = intake(), intake()
        self.assertEqual((one["cursor_start"], one["cursor_next"], one["deferred"]), (0, 1, 1))
        self.assertEqual((two["cursor_start"], two["cursor_next"], two["deferred"]), (1, 0, 0))
        self.assertEqual(len(one["preserved"]), 1)
        self.assertEqual(len(two["preserved"]), 1)
        with store.ledger(run.Root(str(self.root)).ledger, readonly=True) as con:
            subjects = {row[0] for row in con.execute("SELECT sha256 FROM originals")}
        self.assertEqual(subjects, {entry["sha256"] for entry in manifest["entries"]})
        three, four = intake(), intake()
        self.assertEqual(len(three["replayed"]), 1)
        self.assertEqual(len(four["replayed"]), 1)
        self.assertNotEqual(three["replayed"][0], four["replayed"][0])

    def test_progress_never_reused_for_changed_manifest(self):
        self.add("first.xlsx")
        self.add("second.xlsx", xlsx_bytes() + b"synthetic ZIP trailing bytes")
        manifest = self.manifest()
        from campaign_tool.records.run_safety import RunSafety, RunSafetyPolicy
        policy = RunSafetyPolicy(max_fetches=1)
        pipeline = unattended.UnattendedPipeline(str(self.root), policy=policy)
        pipeline.safety = RunSafety(pipeline.root.ledger, policy)
        with self.readonly():
            first = pipeline.ingest_documents(str(self.input), manifest["sha256"])
        self.assertEqual(first["cursor_next"], 1)
        self.add("third.xlsx", xlsx_bytes() + b"another synthetic ZIP tail")
        changed = self.manifest()
        pipeline = unattended.UnattendedPipeline(str(self.root), policy=policy)
        pipeline.safety = RunSafety(pipeline.root.ledger, policy)
        with self.readonly():
            report = pipeline.ingest_documents(str(self.input), changed["sha256"])
        self.assertEqual(report["cursor_start"], 0)
        self.assertEqual(report["manifest_sha256"], changed["sha256"])
        self.assertEqual(pipeline.safety.phases["intake"]["attempted"], 1)

    def test_failed_attempt_does_not_advance_progress(self):
        self.add("broken.xlsx", b"not a workbook")
        manifest = self.manifest()
        from campaign_tool.records.run_safety import RunSafety, RunSafetyPolicy
        pipeline = unattended.UnattendedPipeline(str(self.root))
        pipeline.safety = RunSafety(pipeline.root.ledger, RunSafetyPolicy(max_fetches=1))
        with self.readonly():
            report = pipeline.ingest_documents(str(self.input), manifest["sha256"])
        self.assertEqual((report["cursor_start"], report["cursor_next"]), (0, 0))
        self.assertEqual(pipeline.safety.phases["intake"]["attempted"], 1)
        self.assertEqual(pipeline.safety.phases["intake"]["failed"], 1)
