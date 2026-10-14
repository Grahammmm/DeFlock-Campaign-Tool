"""Synthetic current-main recovery compatibility; no live data or authority."""
from contextlib import closing
from dataclasses import replace
import shutil
import sqlite3
import unittest

from campaign_tool.records import publish, review_retirement
from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.migrations import original_resolution_v001
from campaign_tool.records.recovery import backup as v1, backup_v2 as v2
from tests import test_records_backup_v2 as synthetic
from tests.records import test_proposal_provenance as provenance


class PublicationBackupCompatibilityTests(unittest.TestCase):
    def test_current_publication_and_resolution_schema_roundtrip_retains_guards(self):
        fixture = synthetic.RecoveryV2("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        publication = provenance.ProposalProvenanceTests("runTest")
        self.addCleanup(publication.doCleanups)
        publication.setUp()
        path = fixture.source / "publication.sqlite"
        v1.snapshot_ledger(publication.root.ledger, path)
        path.chmod(0o600)
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("PRAGMA foreign_keys=ON")
            con.executescript(publish.JOURNAL_SQL)
            con.execute("INSERT INTO staging_journal VALUES(?,?,?,?,?,?,?,?,?)",
                        ("synthetic-release", "/synthetic/staging", publication.proposal_id,
                         b"synthetic manifest", b"synthetic content", None,
                         b"synthetic current", None, "complete"))
            con.execute("CREATE TABLE backup_refs(role TEXT, source_path TEXT, sha256 TEXT)")
            con.executemany("INSERT INTO backup_refs VALUES(?,?,?)",
                            [("original_blob", str(fixture.blob), fixture.sha),
                             ("receipt", str(fixture.receipt), synthetic.digest(fixture.receipt_raw)),
                             ("review_bundle", str(fixture.review), synthetic.digest(fixture.review_raw))])
            old_schema = synthetic.schema_hash(con)
            con.executescript(original_resolution_v001.SQL)
            current_schema = synthetic.schema_hash(con)
            review_retirement.start(con)
            before = con.execute("SELECT value FROM ledger_meta WHERE key=?",
                                 (review_retirement.KEY,)).fetchone()
            self.assertEqual(before, (review_retirement.PENDING,))
            self.assertTrue(review_retirement.pending(con))
            expected_triggers = con.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
        tables = ("originals", "receipts", "ledger_meta", "backup_refs",
                  "staging_journal", "proposal_revisions", "original_resolutions")
        extra = v2.Store("publication", str(path), ("publication_store",), "sqlite",
                         required_tables=tables, schema_sha256=old_schema,
                         reference_sql=("SELECT role,source_path,sha256 FROM backup_refs",))
        stale = replace(fixture.profile, stores=fixture.profile.stores + (extra,))
        with self.assertRaisesRegex(v1.RecoveryError, "schema"):
            fixture.backup(profile=stale, name="stale-schema")
        current = replace(stale, stores=fixture.profile.stores +
                          (replace(extra, schema_sha256=current_schema),))
        sidecars_before = {str(p): p.read_bytes() for p in fixture.source.glob("*-wal")}
        with fixture.retained_connections() as connections:
            report = fixture.backup(profile=current)
            v2.verify_v2(str(fixture.base / "backup"),
                         manifest_sha256=report["manifest_sha256"])
            v2.restore_v2(str(fixture.base / "backup"), str(fixture.base / "restored"),
                          manifest_sha256=report["manifest_sha256"])
            fixture.assert_connections_closed(connections)
        self.assertEqual(sidecars_before,
                         {str(p): p.read_bytes() for p in fixture.source.glob("*-wal")})
        # Restore is an immutable snapshot. Probe its guards on an explicit,
        # separate synthetic writable clone, not by activating restored bytes.
        clone = fixture.base / "guard-probe.sqlite"
        shutil.copyfile(fixture.base / "restored/payload/publication", clone)
        clone.chmod(0o600)
        with closing(sqlite3.connect(clone)) as con:
            self.assertEqual(synthetic.schema_hash(con), current_schema)
            self.assertEqual(con.execute("SELECT value FROM ledger_meta WHERE key=?",
                                        (review_retirement.KEY,)).fetchone(), before)
            self.assertTrue(review_retirement.pending(con))
            self.assertEqual(con.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall(),
                expected_triggers)
            self.assertEqual(con.execute("SELECT count(*) FROM staging_journal").fetchone(), (1,))
            self.assertGreater(con.execute("SELECT count(*) FROM proposal_revisions").fetchone()[0], 0)
            for sql in ("DELETE FROM staging_journal", "DELETE FROM proposal_revisions",
                        "UPDATE staging_journal SET manifest=X'00'",
                        "UPDATE proposal_revisions SET manifest_content=X'00'"):
                with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    con.execute(sql)
                con.rollback()


if __name__ == "__main__":
    unittest.main()
