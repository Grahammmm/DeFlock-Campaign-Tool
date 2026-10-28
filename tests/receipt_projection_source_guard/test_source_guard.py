"""Synthetic, in-memory SQL/replay acceptance; no filesystem or live ledgers."""
import sqlite3
import unittest

from campaign_tool.records.ledger import receipt_projection as rp
from campaign_tool.records.ledger.migrations import v001


class SourceGuardTests(unittest.TestCase):
    def connection(self, *, legacy=False):
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA recursive_triggers=ON')
        con.executescript(v001.SQL)
        if legacy:
            rp.register(con)
            con.executescript(rp.migration.SQL)
            con.execute('INSERT INTO rp_schema VALUES(?,?)',
                        (rp.VERSION, rp.digest(rp.migration.SQL.encode())))
            con.commit()
        else:
            rp.install(con)
        return con

    def seed(self, con, error=None, *, missing_body=False):
        original = rp.digest(b'fictional original')
        artifact = b'fictional receipt'
        artifact_sha = rp.digest(artifact)
        con.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (original, 18, 'text/plain', None, 'original', 'in_scope',
                     None, 'captured', None, '{}'))
        con.execute('INSERT INTO rp_artifacts VALUES(?,?,?)',
                    (artifact_sha, len(artifact), artifact))
        raw = (rp.canonical({'original': original, 'receipt': artifact_sha,
                             'path': 'synthetic-receipt.txt'}) + '\n').encode()
        mapping = {'kind': 'reviews', 'fields': {'original_hash': 'original',
                   'artifact_hash': 'receipt', 'artifact_path': 'path'},
                   'declarations': {}, 'artifact_paths': {}}
        expected = '0' * 64 if error == 'index_hash_mismatch' else rp.digest(raw)
        manifest = rp.canonical({'version': 1, 'indexes': [{
            'path': 'synthetic-index.jsonl', 'sha256': expected, **mapping}]}).encode()
        mid = rp.digest(manifest)
        sid = rp.identity(mid, 0)
        con.execute('INSERT INTO rp_manifests VALUES(?,?,?)',
                    (mid, '/synthetic-memory-only', manifest))
        body = None if missing_body else raw
        con.execute('INSERT INTO rp_sources VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (sid, mid, 0, 'synthetic-index.jsonl', rp.canonical(mapping),
                     expected, None if missing_body else rp.digest(raw),
                     None if missing_body else len(raw), body, error))
        if error:
            con.execute('INSERT INTO rp_gaps(id,source_id,item_id,reason) VALUES(?,?,NULL,?)',
                        (rp.identity(sid, None, error), sid, error))
        plan = rp.plan(raw, rp.canonical(mapping))
        values = (rp.identity(sid, 1), mid, sid, 1, raw, plan, artifact_sha,
                  None, original, rp.bound(plan, artifact_sha, None, original))
        self.assertEqual(values[-1], 1, 'synthetic item must otherwise be binding_verified')
        con.commit()
        return mid, values + (rp.evidence(*values),)

    def append_item(self, con, values):
        con.execute('INSERT INTO rp_items VALUES(?,?,?,?,?,?,?,?,?,?,?)', values)
        con.execute('INSERT INTO rp_steps VALUES(?,?,?,?)',
                    (values[1], 1, values[0], values[-1]))
        con.commit()

    def snapshot(self, con):
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: [tuple(row) for row in con.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in tables}

    def assert_history_preserved(self, before, con):
        after = self.snapshot(con)
        for table, rows in before.items():
            if table == 'rp_schema':
                self.assertEqual(after[table][:len(rows)], rows)
                self.assertEqual(after[table][len(rows):], [(
                    rp.source_guard.VERSION, rp.digest(rp.source_guard.SQL.encode()))])
            else:
                self.assertEqual(after[table], rows, table)

    def test_failed_source_sql_admission_accepts_zero_items(self):
        for error in ('index_hash_mismatch', 'changing_input'):
            for insert in ('INSERT', 'INSERT OR IGNORE', 'INSERT OR REPLACE'):
                with self.subTest(error=error, insert=insert):
                    con = self.connection()
                    mid, values = self.seed(con, error)
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'requires successful source'):
                        con.execute(insert + ' INTO rp_items VALUES(?,?,?,?,?,?,?,?,?,?,?)', values)
                    self.assertEqual(con.execute('SELECT count(*) FROM rp_items').fetchone()[0], 0)
                    self.assertEqual(rp.verify(con, mid), 0)

    def test_valid_source_sql_and_replay_succeed(self):
        con = self.connection()
        mid, values = self.seed(con)
        self.append_item(con, values)
        before = self.snapshot(con)
        con.execute('PRAGMA query_only=ON')
        self.assertEqual(rp.verify(con, mid), 1)
        self.assertEqual(rp.verify(con, mid), 1)
        self.assertEqual(con.execute('SELECT binding_verified FROM rp_items').fetchone()[0], 1)
        self.assertEqual(self.snapshot(con), before)

    def test_legacy_invalid_items_are_inspectable_but_never_certified(self):
        for error in ('index_hash_mismatch', 'changing_input'):
            with self.subTest(error=error):
                con = self.connection(legacy=True)
                mid, values = self.seed(con, error)
                # Intact v001 functions/triggers accept this historical defect.
                self.append_item(con, values)
                before = self.snapshot(con)
                con.execute('PRAGMA query_only=ON')
                with self.assertRaisesRegex(ValueError, 'belongs to failed source'):
                    rp.verify(con, mid)
                self.assertEqual(self.snapshot(con), before)
                con.execute('PRAGMA query_only=OFF')
                rp.install(con)
                self.assert_history_preserved(before, con)
                con.execute('PRAGMA query_only=ON')
                with self.assertRaisesRegex(ValueError, 'belongs to failed source'):
                    rp.verify(con, mid)
                self.assertEqual(con.execute('SELECT count(*) FROM rp_items').fetchone()[0], 1)

    def test_legacy_failed_source_gap_snapshots_remain_readable(self):
        cases = [('index_hash_mismatch', False), ('changing_input', False)]
        cases += [(error, True) for error in (
            'missing_file', 'path_escape', 'unsafe_or_unreadable_path',
            'not_regular_file', 'oversize', 'changing_input')]
        for error, missing_body in cases:
            with self.subTest(error=error, missing_body=missing_body):
                con = self.connection(legacy=True)
                mid, _ = self.seed(con, error, missing_body=missing_body)
                before = self.snapshot(con)
                con.execute('PRAGMA query_only=ON')
                self.assertEqual(rp.verify(con, mid), 0)
                self.assertEqual(self.snapshot(con), before)
                con.execute('PRAGMA query_only=OFF')
                rp.install(con)
                self.assert_history_preserved(before, con)
                con.execute('PRAGMA query_only=ON')
                self.assertEqual(rp.verify(con, mid), 0)

    def test_legacy_valid_snapshot_and_v001_identity_are_preserved(self):
        con = self.connection(legacy=True)
        mid, values = self.seed(con)
        self.append_item(con, values)
        before = self.snapshot(con)
        self.assertEqual(rp.verify(con, mid), 1)
        rp.install(con)
        self.assert_history_preserved(before, con)
        self.assertEqual(rp.verify(con, mid), 1)
        after = self.snapshot(con)
        rp.install(con)
        self.assertEqual(self.snapshot(con), after)

    def test_fresh_install_is_idempotent(self):
        con = self.connection()
        before = self.snapshot(con)
        rp.install(con)
        self.assertEqual(self.snapshot(con), before)
        self.assertEqual(len(before['rp_schema']), 2)

    def test_missing_or_changed_registered_guard_fails_closed(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                con = self.connection()
                con.execute('DROP TRIGGER rp_item_source_success')
                if changed:
                    con.execute('CREATE TRIGGER rp_item_source_success BEFORE INSERT '
                                'ON rp_items BEGIN SELECT 1; END')
                con.commit()
                with self.assertRaisesRegex(ValueError, 'guard missing or changed'):
                    rp.install(con)

    def test_unknown_schema_identity_is_rejected_without_rewriting(self):
        con = self.connection(legacy=True)
        con.execute("INSERT INTO rp_schema VALUES('unknown-version','unknown-checksum')")
        con.commit()
        before = self.snapshot(con)
        with self.assertRaisesRegex(ValueError, 'schema identity mismatch'):
            rp.install(con)
        self.assertEqual(self.snapshot(con), before)


if __name__ == '__main__':
    unittest.main()
