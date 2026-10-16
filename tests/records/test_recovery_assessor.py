"""Offline synthetic diagnostic coverage; never a live recovery rehearsal."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.recovery.assessor import Dependency, assess, _schema
from campaign_tool.records.recovery.backup import sha


class RecoveryAssessorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "offline"
        self.root.mkdir(mode=0o700)
        self.historical = "/synthetic-private-runtime"
        self.commit = "a" * 40
        self.blob = self.file("blob", b"synthetic original")
        self.receipt = b'{"schema":"synthetic-stage-receipt"}'
        self.review = b'{"schema":"synthetic-review-content"}'
        self.resume = self.file("resume", b'{"schema":"records-advance-resume-v1","cursor":null,"frontier":null}')
        self.canonical = self.root / "ledger.sqlite"
        self.intake = self.root / "intake.sqlite"
        with closing(sqlite3.connect(self.canonical)) as con, con:
            con.executescript("""
            CREATE TABLE originals(sha256 TEXT,storage_path TEXT);
            CREATE TABLE receipts(sha256 TEXT,path TEXT);
            CREATE TABLE stage_artifacts(sha256 TEXT,payload BLOB);
            CREATE TABLE stage_content(stage TEXT,content_sha256 TEXT);
            CREATE TABLE mail_checkpoints(uidvalidity INTEGER,highest_uid INTEGER);
            CREATE TABLE ledger_meta(key TEXT,value TEXT);
            """)
            con.execute("INSERT INTO originals VALUES(?,?)", (sha(self.blob.read_bytes()), self.historical + "/blob"))
            con.executemany("INSERT INTO stage_artifacts VALUES(?,?)", [(sha(b), b) for b in (self.receipt, self.review)])
            con.execute("INSERT INTO receipts VALUES(?,?)", (sha(self.receipt), "ledger-stage-artifact:" + sha(self.receipt)))
            con.execute("INSERT INTO stage_content VALUES('review',?)", (sha(self.review),))
            con.execute("INSERT INTO mail_checkpoints VALUES(1,42)")
            con.execute("INSERT INTO ledger_meta VALUES('publication-recovery-root-v1',?)", (self.historical,))
        with closing(sqlite3.connect(self.intake)) as con, con:
            con.execute("CREATE TABLE preservations(sha TEXT,path TEXT)")
            con.execute("INSERT INTO preservations VALUES(?,?)", (sha(self.blob.read_bytes()), self.historical + "/blob"))
        for path in (self.canonical, self.intake):
            path.chmod(0o600)
        self.refresh()

    def file(self, name, raw):
        path = self.root / name
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def refresh(self):
        self.dependencies = []
        for path, kind, historical in ((self.canonical, "canonical", "ledger.sqlite"),
                                       (self.intake, "intake", "intake/intake.sqlite"),
                                       (self.blob, "original_blob", "blob"),
                                       (self.resume, "resume", "reports/unattended-advance-resume.json")):
            schema = ""
            if kind in ("canonical", "intake"):
                with closing(sqlite3.connect(path)) as con:
                    schema = _schema(con)
            self.dependencies.append(Dependency(str(path), self.historical + "/" + historical,
                                                kind, sha(path.read_bytes()), schema))
        self.dependencies = tuple(self.dependencies)

    def run_assessor(self, dependencies=None):
        return assess(offline_root=str(self.root), historical_root=self.historical,
                      dependencies=self.dependencies if dependencies is None else dependencies, code_commit=self.commit)

    def edit(self, sql, args=()):
        with closing(sqlite3.connect(self.canonical)) as con, con:
            con.execute(sql, args)
        self.refresh()

    def test_embedded_evidence_and_both_cursors_are_held_not_file_placeholders(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        report = self.run_assessor()
        self.assertEqual(report["status"], "held")
        self.assertEqual(report["coverage"], {"files_verified": 4, "embedded_artifacts_verified": 2,
                         "embedded_receipts_verified": 1, "embedded_review_objects_verified": 1,
                         "mail_checkpoint_rows": 1, "unattended_cursor_files": 1,
                         "external_references_verified": 2})
        self.assertIn("embedded_objects_have_no_v2_object_reference_adapter", report["reasons"])
        self.assertFalse(report["activation"])
        self.assertFalse(report["capture_eligible"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        encoded = json.dumps(report)
        for secret in (str(self.root), self.historical, self.receipt.decode(), "ledger-stage-artifact"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(report["schema_sha256"]["canonical"], self.dependencies[0].schema_sha256)
        self.assertEqual(report["code_identity"]["declared_commit"], self.commit)

    def test_missing_changed_unknown_and_constant_query_dependencies_reject(self):
        for mutation in ("missing", "changed", "unknown", "constant", "empty_placeholder", "schema"):
            with self.subTest(mutation=mutation):
                deps = list(self.dependencies)
                if mutation == "missing": deps.pop(2)
                elif mutation == "changed": deps[2] = replace(deps[2], sha256="0" * 64)
                elif mutation == "unknown": deps[2] = replace(deps[2], kind="review_bundle")
                elif mutation == "constant": deps[0] = replace(deps[0], reference_sql=("SELECT 'receipt','fake','0000'",))
                elif mutation == "schema": deps[0] = replace(deps[0], schema_sha256="0" * 64)
                else:
                    path = self.file("placeholder", b"")
                    deps.append(Dependency(str(path), self.historical + "/placeholder", "receipt", sha(b"")))
                report = self.run_assessor(tuple(deps))
                self.assertEqual(report["status"], "rejected")
                self.assertEqual(report["coverage"], {})
                if mutation == "empty_placeholder": path.unlink()

    def test_corrupt_missing_embedded_objects_reject(self):
        self.edit("UPDATE stage_artifacts SET payload=? WHERE sha256=?", (b"corrupt", sha(self.review)))
        self.assertEqual(self.run_assessor()["reasons"], ["embedded_artifact_invalid"])
        self.edit("DELETE FROM stage_artifacts WHERE sha256=?", (sha(self.review),))
        self.assertEqual(self.run_assessor()["reasons"], ["embedded_stage_content_missing"])

    def test_external_receipt_maps_actual_file_and_hash(self):
        path = self.file("external-receipt", b"synthetic receipt bytes")
        self.edit("UPDATE receipts SET sha256=?,path=?", (sha(path.read_bytes()), self.historical + "/receipt"))
        self.assertEqual(self.run_assessor()["status"], "rejected")
        deps = self.dependencies + (Dependency(str(path), self.historical + "/receipt", "receipt", sha(path.read_bytes())),)
        report = self.run_assessor(deps)
        self.assertEqual(report["status"], "held")
        self.assertEqual(report["coverage"]["external_references_verified"], 3)

    def test_missing_or_invalid_cursor_and_duplicate_json_reject(self):
        for raw in (b'{}', b'{"schema":"records-advance-resume-v1","cursor":null,"cursor":null,"frontier":null}',
                    b'{"schema":"records-advance-resume-v1","cursor":["x","bad"],"frontier":null}'):
            self.resume.write_bytes(raw)
            self.refresh()
            self.assertEqual(self.run_assessor()["status"], "rejected")
        self.edit("DROP TABLE mail_checkpoints")
        self.assertEqual(self.run_assessor()["status"], "rejected")

    def test_denied_symlink_unknown_file_and_sidecar_fail_closed(self):
        with patch("campaign_tool.records.recovery.assessor.source_fd", side_effect=PermissionError("PRIVATE_PATH")):
            report = self.run_assessor()
        self.assertEqual(report["reasons"], ["dependency_unreadable_or_invalid"])
        self.assertNotIn("PRIVATE_PATH", json.dumps(report))
        for name in ("unknown", "ledger.sqlite-wal"):
            p = self.file(name, b"synthetic")
            self.assertEqual(self.run_assessor()["reasons"], ["undeclared_or_missing_file"])
            p.unlink()
        p = self.root / "symlink"
        p.symlink_to(self.blob)
        self.assertEqual(self.run_assessor()["reasons"], ["symlink_rejected"])

    def test_hash_pinned_deep_resume_returns_sanitized_rejection(self):
        raw = b'[' * 1100 + b'0' + b']' * 1100
        self.assertLessEqual(len(raw), 4096)
        self.resume.write_bytes(raw)
        self.refresh()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        report = self.run_assessor()
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["reasons"], ["resume_invalid"])
        self.assertEqual(report["coverage"], {})
        self.assertFalse(report["activation"])
        self.assertFalse(report["capture_eligible"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_publication_binding_mismatch_refuses_without_rebinding(self):
        self.edit("UPDATE ledger_meta SET value='/different-runtime'")
        before = self.canonical.read_bytes()
        self.assertEqual(self.run_assessor()["reasons"], ["publication_binding_mismatch"])
        self.assertEqual(before, self.canonical.read_bytes())

    def test_deep_directory_inventory_returns_sanitized_rejection(self):
        # Exercise the real descriptor walk below its entry bound. A lower
        # interpreter recursion limit keeps the fixture small and portable.
        directory = self.root
        for _ in range(180):
            directory = directory / "d"
            directory.mkdir(mode=0o700)
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        previous_limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(150)
            report = self.run_assessor()
        finally:
            sys.setrecursionlimit(previous_limit)
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["reasons"], ["dependency_unreadable_or_invalid"])
        self.assertEqual(report["coverage"], {})
        self.assertEqual(report["schema_sha256"], {})
        self.assertFalse(report["activation"])
        self.assertFalse(report["capture_eligible"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        encoded = json.dumps(report)
        for private in (str(self.root), self.historical, str(directory), "RecursionError"):
            self.assertNotIn(private, encoded)

    def test_zero_review_rows_remain_explicitly_held(self):
        self.edit("DELETE FROM stage_content")
        report = self.run_assessor()
        self.assertEqual(report["status"], "held")
        self.assertIn("review_evidence_not_observed", report["reasons"])

    def test_declared_sidecar_credential_and_nonempty_placeholder_reject(self):
        for name, kind, reason in (("ledger.sqlite-wal", "configuration", "sqlite_sidecar_forbidden"),
                                   (".env", "configuration", "credential_dependency_forbidden"),
                                   ("fake-receipt", "receipt", "unreferenced_evidence_placeholder")):
            with self.subTest(name=name):
                path = self.file(name, b"synthetic placeholder bytes")
                dep = Dependency(str(path), self.historical + "/" + name, kind, sha(path.read_bytes()))
                self.assertEqual(self.run_assessor(self.dependencies + (dep,))["reasons"], [reason])
                path.unlink()

    def test_code_pin_and_offline_copy_boundary(self):
        self.commit = "not-a-commit"
        self.assertEqual(self.run_assessor()["reasons"], ["code_identity_invalid"])
        report = assess(offline_root=str(self.root), historical_root=str(self.root),
                        dependencies=self.dependencies, code_commit="a" * 40)
        self.assertEqual(report["reasons"], ["offline_copy_required"])

    def test_schema_and_content_bounds_and_mutation_are_rejected(self):
        with patch("campaign_tool.records.recovery.assessor.MAX_ROWS", 1):
            self.assertEqual(self.run_assessor()["reasons"], ["dependency_row_bound"])
        with patch("campaign_tool.records.recovery.assessor.MAX_BYTES", 1):
            self.assertEqual(self.run_assessor()["reasons"], ["dependency_size_bound"])
        from campaign_tool.records.recovery.assessor import _inventory
        def changed(root):
            result = _inventory(root)
            if not hasattr(changed, "seen"):
                changed.seen = True
            else:
                result["synthetic-mutation"] = ("file", ())
            return result
        with patch("campaign_tool.records.recovery.assessor._inventory", side_effect=changed):
            self.assertEqual(self.run_assessor()["reasons"], ["dependency_changed"])

    def test_generated_column_function_is_not_executed(self):
        self.edit("DROP TABLE originals")
        self.edit("CREATE TABLE originals(raw TEXT, sha256 TEXT GENERATED ALWAYS AS (lower(raw)), storage_path TEXT)")
        self.edit("INSERT INTO originals(raw,storage_path) VALUES(?,?)", (sha(self.blob.read_bytes()), self.historical + "/blob"))
        self.assertEqual(self.run_assessor()["reasons"], ["generated_columns_unsupported"])
