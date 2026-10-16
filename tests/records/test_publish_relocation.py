"""Synthetic backup-to-relocated-runtime boundary; no live data or targets."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch

from campaign_tool.records import publish, review_retirement
from campaign_tool.records.recovery import backup as v1, backup_v2 as v2
from campaign_tool.records.run import Root
from tests import test_records_backup_v2 as recovery_fixtures
from tests.records import test_publish_authority as publication_fixtures


def tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file() and path.name != "run.lock"}


def database_rows(path):
    with closing(sqlite3.connect(path)) as con:
        return tuple(con.iterdump())


class RollbackRelocationTests(unittest.TestCase):
    def test_restored_clone_refuses_historical_targets_before_any_recovery_write(self):
        recovery = recovery_fixtures.RecoveryV2("runTest")
        publication = publication_fixtures.PublicationFixture("runTest")
        self.addCleanup(recovery.doCleanups)
        self.addCleanup(publication.doCleanups)
        recovery.setUp()
        publication.setUp()
        publication.two_releases()
        write = publish._write

        def interrupt(path, data):
            write(path, data)
            if path.name == "current.json":
                raise SystemExit("synthetic rollback interruption")

        with patch.object(publish, "_write", side_effect=interrupt), self.assertRaises(SystemExit):
            publication.rollback()
        path = recovery.source / "publication.sqlite"
        v1.snapshot_ledger(publication.root.ledger, path)
        path.chmod(0o600)
        with closing(sqlite3.connect(path)) as con, con:
            self.assertEqual(con.execute(
                "SELECT count(*) FROM staging_journal WHERE state='prepared'").fetchone(), (1,))
            con.execute("CREATE TABLE backup_refs(role TEXT, source_path TEXT, sha256 TEXT)")
            con.executemany("INSERT INTO backup_refs VALUES(?,?,?)", [
                ("original_blob", str(recovery.blob), recovery.sha),
                ("receipt", str(recovery.receipt), recovery_fixtures.digest(recovery.receipt_raw)),
                ("review_bundle", str(recovery.review), recovery_fixtures.digest(recovery.review_raw)),
            ])
            schema = recovery_fixtures.schema_hash(con)
        component = v2.Store("publication", str(path), ("publication_store",), "sqlite",
            required_tables=("proposals", "staging_journal", "backup_refs"), schema_sha256=schema,
            reference_sql=("SELECT role,source_path,sha256 FROM backup_refs",))
        profile = replace(recovery.profile, stores=recovery.profile.stores + (component,))
        report = recovery.backup(profile=profile)
        archive = recovery.base / "backup"
        v2.verify_v2(str(archive), manifest_sha256=report["manifest_sha256"])
        restored = recovery.base / "restored"
        result = v2.restore_v2(str(archive), str(restored), manifest_sha256=report["manifest_sha256"])
        self.assertFalse(result["activation"])
        clone = Root(recovery.base / "writable-qualification-clone").prepare()
        shutil.copyfile(restored / "payload/publication", clone.ledger)
        isolated = recovery.base / "isolated-staging"
        shutil.copytree(publication.staging, isolated)
        # Retain a pending retirement and revoked approval in the restored clone.
        with closing(sqlite3.connect(clone.ledger)) as con, con:
            review_retirement.start(con)
            con.execute("UPDATE proposals SET owner_approval='none',approved_content_sha256=NULL")
        source_before = tree_bytes(publication.staging)
        target_before = tree_bytes(isolated)
        source_ledger_before = database_rows(publication.root.ledger)
        clone_before = database_rows(clone.ledger)
        with patch.object(publish, "root_lock", wraps=publish.root_lock) as locked:
            for _ in range(2):
                with self.assertRaisesRegex(publish.PublishError, "publication_root_binding_mismatch"):
                    publish.rollback(clone.path, publication.proposal, staging=isolated)
            with self.assertRaisesRegex(publish.PublishError, "publication_root_binding_mismatch"):
                publish.approve(clone.path, publication.proposal, owner_id="synthetic-owner")
            self.assertNotIn(publication.staging, [call.args[0] for call in locked.call_args_list])
            self.assertNotIn(isolated, [call.args[0] for call in locked.call_args_list])
        self.assertEqual(tree_bytes(publication.staging), source_before)
        self.assertEqual(tree_bytes(isolated), target_before)
        self.assertEqual(database_rows(publication.root.ledger), source_ledger_before)
        self.assertEqual(database_rows(clone.ledger), clone_before)


class LegacyRootBindingTests(publication_fixtures.PublicationFixture):
    def test_unbound_existing_history_refuses_without_rebinding_or_output_changes(self):
        self.two_releases()
        with publish.store.ledger(self.root.ledger) as con:
            con.execute("DELETE FROM ledger_meta WHERE key=?", (publish.PUBLICATION_ROOT_KEY,))
            con.commit()
        before = tree_bytes(self.staging)
        ledger_before = database_rows(self.root.ledger)
        with self.assertRaisesRegex(publish.PublishError, "publication_root_binding_required"):
            self.rollback()
        self.assertEqual(tree_bytes(self.staging), before)
        self.assertEqual(database_rows(self.root.ledger), ledger_before)
