"""FA102: receipt install verifies its rp_ schema objects, not only the checksum markers.

Card-owned synthetic ledgers only: every case builds a fresh initialized ledger under the
active temporary root, captures fictional evidence through the real project() entry point,
then mutates only that disposable copy. Runs under ordinary unittest as well as
tools/accept_fa102.py.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest

from campaign_tool.records.ledger import store, receipt_projection as rp
from campaign_tool.records.ledger.migrations import receipt_projection_v001 as migration
from campaign_tool.records.ledger.migrations import receipt_source_guard_v001 as source_guard

MISMATCH = 'sidecar schema objects mismatch'
GUARD_REFUSAL = 'receipt source guard missing or changed'
IDENTITY_REFUSAL = 'sidecar schema identity mismatch'
# Independent frozen inventory of the unchanged base migration (not derived from production code).
TABLES = ('rp_schema', 'rp_manifests', 'rp_sources', 'rp_artifacts', 'rp_items', 'rp_gaps', 'rp_steps')
CONSISTENCY = ('rp_source_consistency', 'rp_item_consistency', 'rp_gap_consistency', 'rp_step_consistency')
APPEND_ONLY = tuple(t + suffix for t in TABLES for suffix in ('_no_replace', '_no_update', '_no_delete'))
BASE_TRIGGERS = CONSISTENCY + APPEND_ONLY
# Filled by GateControl; read back by tools/accept_fa102.py for the negative-control receipt.
CONTROL_OUTCOMES = []


def sha(data):
    return hashlib.sha256(data).hexdigest()


def reference(*scripts):
    """The test's own in-memory reference of every sqlite_master row the scripts create."""
    con = sqlite3.connect(':memory:')
    try:
        rp.register(con)  # CHECK constraints name the rp_ SQL functions at CREATE time
        for script in scripts:
            con.executescript(script)
        return {(r[0], r[1]): (r[2], r[3]) for r in con.execute('SELECT type,name,tbl_name,sql FROM sqlite_master')}
    finally:
        con.close()


def rp_master(db):
    con = sqlite3.connect(db)
    try:
        return {(r[0], r[1]): (r[2], r[3]) for r in con.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master').fetchall()
            if r[1].lower().startswith('rp_') or r[2].lower().startswith('rp_')}
    finally:
        con.close()


def snapshot(db):
    """Complete logical state: sqlite_master, every table's rows and the raw capture bytes."""
    con = sqlite3.connect(db)
    try:
        master = sorted(con.execute('SELECT type,name,tbl_name,sql FROM sqlite_master').fetchall(), key=repr)
        tables = [r[1] for r in master if r[0] == 'table' and not r[1].startswith('sqlite_')]
        rows = {t: sorted(con.execute('SELECT * FROM "%s"' % t.replace('"', '""')).fetchall(), key=repr)
                for t in tables}
        names = {r[1] for r in master}
        captures = {}
        for table, key, column in (('rp_manifests', 'id', 'body'), ('rp_sources', 'id', 'body'),
                                   ('rp_items', 'id', 'raw'), ('rp_artifacts', 'sha', 'body')):
            if table in names:
                captures[table] = sorted((k, None if b is None else sha(bytes(b)))
                                         for k, b in con.execute('SELECT %s,%s FROM %s' % (key, column, table)))
        meta = con.execute("SELECT key,value FROM ledger_meta ORDER BY key").fetchall()
        return {'master': master, 'rows': rows, 'captures': captures, 'ledger_meta': meta}
    finally:
        con.close()


def mutate(db, *statements):
    con = sqlite3.connect(db, isolation_level=None)
    try:
        rp.register(con)
        con.execute('PRAGMA foreign_keys=OFF')
        for statement in statements:
            con.execute(statement)
    finally:
        con.close()


def rewrite_table_sql(db, table, old, new):
    """Alter a stored table constraint in a disposable synthetic copy (writable_schema)."""
    con = sqlite3.connect(db, isolation_level=None)
    try:
        rp.register(con)
        current = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        if old not in current:
            raise AssertionError('fixture constraint text not found: ' + old)
        version = con.execute('PRAGMA schema_version').fetchone()[0]
        con.execute('PRAGMA writable_schema=ON')
        con.execute("UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?", (current.replace(old, new, 1), table))
        con.execute('PRAGMA schema_version=%d' % (version + 1))
        con.execute('PRAGMA writable_schema=OFF')
    finally:
        con.close()
    check = sqlite3.connect(db)
    try:
        rp.register(check)
        if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise AssertionError('synthetic constraint rewrite corrupted the copy')
    finally:
        check.close()


def disable_trigger_sql(sql):
    """A changed definition that silently disables the trigger while keeping its name."""
    begin = re.search(r'\sBEGIN\b', sql)
    if not begin:
        raise AssertionError('trigger without BEGIN')
    head, tail = sql[:begin.start()], sql[begin.start():]
    if re.search(r'\bWHEN\b', head):
        head = re.sub(r'\bWHEN\b', 'WHEN 0 AND', head, count=1)
    else:
        head += ' WHEN 0'
    return head + tail


class Ledger:
    """A fresh private root with an initialized ledger and one captured fictional receipt."""

    def __init__(self, case):
        tmp = tempfile.TemporaryDirectory(prefix='fa102-')
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'root'
        self.root.mkdir(mode=0o700)
        self.db = self.root / 'ledger.sqlite'
        store.initialize(self.db)
        self.original = sha(b'fa102 fictional original')
        with store.ledger(self.db) as con:
            con.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (self.original, 24, 'text/plain', None, 'original', 'in_scope', None, 'captured', None, '{}'))
            con.commit()
        self.count = 0
        self.first = self.manifest()
        self.result = rp.project(self.db, self.root, self.first)

    def manifest(self):
        """A new synthetic artifact/index/manifest triple (distinct manifest id each call)."""
        self.count += 1
        n = self.count
        art = json.dumps({'reviewer': 'fictional checker %d' % n, 'verdict': 'declared pass'}).encode()
        (self.root / ('artifact-%d.json' % n)).write_bytes(art)
        row = {'original': self.original, 'receipt': sha(art), 'path': 'artifact-%d.json' % n,
               'reviewer': 'fictional checker %d' % n}
        index = json.dumps(row).encode() + b'\n'
        (self.root / ('index-%d.jsonl' % n)).write_bytes(index)
        manifest = {'version': 1, 'indexes': [{
            'path': 'index-%d.jsonl' % n, 'sha256': sha(index), 'kind': 'reviews',
            'fields': {'original_hash': 'original', 'artifact_hash': 'receipt', 'artifact_path': 'path'},
            'declarations': {'reviewer': 'reviewer'}}]}
        name = 'manifest-%d.json' % n
        (self.root / name).write_text(json.dumps(manifest))
        return name

    def to_historical_base(self):
        """Rebuild the pre-guard historical state: base marker only, no source guard, same evidence."""
        no_delete = reference(migration.SQL)[('trigger', 'rp_schema_no_delete')][1]
        mutate(self.db, 'DROP TRIGGER rp_schema_no_delete',
               "DELETE FROM rp_schema WHERE version='%s'" % source_guard.VERSION,
               'DROP TRIGGER %s' % source_guard.TRIGGER, no_delete)


class Fixture:
    def ledger(self):
        return Ledger(self)

    def markers(self, db):
        con = sqlite3.connect(db)
        try:
            return dict(con.execute('SELECT version,checksum FROM rp_schema').fetchall())
        finally:
            con.close()

    def base_marker(self):
        return {migration.VERSION: sha(migration.SQL.encode())}

    def guarded_marker(self):
        return {**self.base_marker(), source_guard.VERSION: sha(source_guard.SQL.encode())}

    def assert_flags(self, result):
        self.assertEqual((result['stage_promotions'], result['review_accepted'], result['publication_ready'],
                          result['end_to_end_complete']), (0, False, False, None))

    def assert_refused(self, ledger, message, module=rp):
        """Exact refusal before ANY write, through project() and install(), with state preserved."""
        before = snapshot(ledger.db)
        markers = self.markers(ledger.db)
        fresh = ledger.manifest()
        with self.assertRaises(ValueError) as caught:
            module.project(ledger.db, ledger.root, fresh)
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(snapshot(ledger.db), before)
        with store.ledger(ledger.db) as con:
            with self.assertRaises(ValueError) as again:
                module.install(con)
        self.assertEqual(str(again.exception), message)
        after = snapshot(ledger.db)
        self.assertEqual(after, before)
        self.assertEqual(self.markers(ledger.db), markers)
        self.assertEqual(after['ledger_meta'], [('acceptance', 'candidate_unreconciled')])


class Behavior(Fixture, unittest.TestCase):
    def test_reference_inventory_is_the_unchanged_migration(self):
        self.assertEqual(sha(migration.SQL.encode()), rp.digest(migration.SQL.encode()))
        expected = reference(migration.SQL)
        self.assertEqual(sorted(n for t, n in expected if t == 'table'), sorted(TABLES))
        self.assertEqual(sorted(n for t, n in expected if t == 'trigger'), sorted(BASE_TRIGGERS))
        self.assertEqual(len(BASE_TRIGGERS), 25)

    def test_fresh_install_creates_exact_objects_and_markers(self):
        ledger = self.ledger()
        self.assertEqual(ledger.result['status'], 'captured')
        self.assertEqual(ledger.result['binding_verified'], 1)
        self.assert_flags(ledger.result)
        self.assertEqual(rp_master(ledger.db), reference(migration.SQL, source_guard.SQL))
        self.assertEqual(self.markers(ledger.db), self.guarded_marker())
        self.assertEqual(snapshot(ledger.db)['ledger_meta'], [('acceptance', 'candidate_unreconciled')])

    def test_exact_reopen_and_replay_are_unchanged(self):
        ledger = self.ledger()
        before = snapshot(ledger.db)
        again = rp.project(ledger.db, ledger.root, ledger.first)
        self.assertEqual((again['processed'], again['added'], again['status']), (1, 0, 'captured'))
        self.assert_flags(again)
        self.assertEqual(snapshot(ledger.db), before)
        with store.ledger(ledger.db) as con:
            self.assertIsNone(rp.install(con))
            self.assertEqual(rp.verify(con, again['manifest_id']), 1)
        self.assertEqual(snapshot(ledger.db), before)

    def test_valid_new_project_after_reopen_captures_and_verifies(self):
        ledger = self.ledger()
        before = snapshot(ledger.db)
        result = rp.project(ledger.db, ledger.root, ledger.manifest())
        self.assertEqual((result['processed'], result['added'], result['binding_verified'], result['status']),
                         (1, 1, 1, 'captured'))
        self.assert_flags(result)
        after = snapshot(ledger.db)
        for table, rows in before['captures'].items():
            self.assertTrue(set(rows) <= set(after['captures'][table]), table)
        self.assertEqual(len(after['rows']['rp_manifests']), 2)

    def test_historical_base_only_upgrade_preserves_evidence(self):
        ledger = self.ledger()
        ledger.to_historical_base()
        self.assertEqual(self.markers(ledger.db), self.base_marker())
        self.assertEqual(rp_master(ledger.db), reference(migration.SQL))
        before = snapshot(ledger.db)
        result = rp.project(ledger.db, ledger.root, ledger.first)
        self.assertEqual((result['processed'], result['added'], result['status']), (1, 0, 'captured'))
        self.assertEqual(self.markers(ledger.db), self.guarded_marker())
        self.assertEqual(rp_master(ledger.db), reference(migration.SQL, source_guard.SQL))
        after = snapshot(ledger.db)
        self.assertEqual(after['captures'], before['captures'])
        for table in TABLES[1:]:
            self.assertEqual(after['rows'][table], before['rows'][table], table)
        self.assertEqual(rp.project(ledger.db, ledger.root, ledger.manifest())['binding_verified'], 1)

    def test_each_removed_base_trigger_is_refused_on_guarded_reopen(self):
        for name in BASE_TRIGGERS:
            with self.subTest(trigger=name):
                ledger = self.ledger()
                mutate(ledger.db, 'DROP TRIGGER %s' % name)
                self.assert_refused(ledger, MISMATCH)

    def test_each_changed_base_trigger_is_refused_on_guarded_reopen(self):
        expected = reference(migration.SQL)
        for name in BASE_TRIGGERS:
            with self.subTest(trigger=name):
                ledger = self.ledger()
                mutate(ledger.db, 'DROP TRIGGER %s' % name, disable_trigger_sql(expected[('trigger', name)][1]))
                changed = rp_master(ledger.db)[('trigger', name)][1]
                self.assertNotEqual(changed, expected[('trigger', name)][1])
                self.assert_refused(ledger, MISMATCH)

    def test_altered_table_constraints_are_refused(self):
        for table, old, new in (('rp_items', 'CHECK(line>0)', 'CHECK(line>=0)'),
                                ('rp_artifacts', 'CHECK(sha=rp_sha(body) AND size=length(body))', 'CHECK(1)'),
                                ('rp_gaps', "DEFAULT 'caller'", "DEFAULT 'operator'")):
            with self.subTest(table=table):
                ledger = self.ledger()
                rewrite_table_sql(ledger.db, table, old, new)
                self.assert_refused(ledger, MISMATCH)

    def test_each_omitted_table_is_refused(self):
        for table in TABLES[1:]:
            with self.subTest(table=table):
                ledger = self.ledger()
                mutate(ledger.db, 'DROP TABLE %s' % table)
                self.assertNotIn(('table', table), rp_master(ledger.db))
                # Dropping rp_items also drops the source guard on it; that refusal stays first and exact.
                self.assert_refused(ledger, GUARD_REFUSAL if table == 'rp_items' else MISMATCH)

    def test_unknown_rp_namespace_objects_are_refused(self):
        for statement in ('CREATE TABLE rp_extra(x)',
                          'CREATE TABLE RP_Upper(x)',
                          'CREATE VIEW rp_view AS SELECT id FROM rp_items',
                          'CREATE INDEX rp_items_extra ON rp_items(line)',
                          'CREATE INDEX fa102_unnamed_index ON rp_items(plan)',
                          'CREATE TRIGGER fa102_after_gap AFTER INSERT ON rp_gaps BEGIN SELECT 1; END',
                          'CREATE TRIGGER rp_on_originals BEFORE INSERT ON originals BEGIN SELECT 1; END'):
            with self.subTest(statement=statement):
                ledger = self.ledger()
                mutate(ledger.db, statement)
                self.assert_refused(ledger, MISMATCH)

    def test_base_only_reopen_refuses_before_guard_append(self):
        for statement in ('DROP TRIGGER rp_items_no_update', 'CREATE TABLE rp_extra(x)',
                          'DROP TRIGGER rp_item_consistency', 'DROP TABLE rp_items'):
            with self.subTest(statement=statement):
                ledger = self.ledger()
                ledger.to_historical_base()
                mutate(ledger.db, statement)
                self.assert_refused(ledger, MISMATCH)
                self.assertEqual(self.markers(ledger.db), self.base_marker())
                self.assertNotIn(('trigger', source_guard.TRIGGER), rp_master(ledger.db))

    def test_missing_source_guard_keeps_existing_refusal(self):
        for extra in ((), ('DROP TRIGGER rp_items_no_update',), ('CREATE TABLE rp_extra(x)',)):
            with self.subTest(extra=extra):
                ledger = self.ledger()
                mutate(ledger.db, 'DROP TRIGGER %s' % source_guard.TRIGGER, *extra)
                self.assert_refused(ledger, GUARD_REFUSAL)

    def test_changed_source_guard_keeps_existing_refusal(self):
        ledger = self.ledger()
        changed = disable_trigger_sql(source_guard.SQL.strip().rstrip(';'))
        mutate(ledger.db, 'DROP TRIGGER %s' % source_guard.TRIGGER, changed, 'DROP TRIGGER rp_gaps_no_delete')
        self.assert_refused(ledger, GUARD_REFUSAL)

    def test_unknown_marker_identity_keeps_existing_refusal(self):
        ledger = self.ledger()
        no_replace = reference(migration.SQL)[('trigger', 'rp_schema_no_replace')][1]
        mutate(ledger.db, 'DROP TRIGGER rp_schema_no_replace', "INSERT INTO rp_schema VALUES('fa102-unknown','0')",
               no_replace)
        self.assert_refused(ledger, IDENTITY_REFUSAL)

    def test_unrelated_canonical_objects_are_not_false_positives(self):
        statements = ('CREATE TABLE fa102_canonical_note(x TEXT)',
                      'CREATE INDEX fa102_originals_kind ON originals(sha256, bytes)',
                      'CREATE TRIGGER fa102_canonical_audit AFTER INSERT ON originals BEGIN SELECT 1; END',
                      'CREATE VIEW fa102_receipt_view AS SELECT id FROM rp_items')
        guarded = self.ledger()
        mutate(guarded.db, *statements)
        result = rp.project(guarded.db, guarded.root, guarded.manifest())
        self.assertEqual((result['added'], result['binding_verified'], result['status']), (1, 1, 'captured'))
        self.assert_flags(result)
        base = self.ledger()
        base.to_historical_base()
        mutate(base.db, *statements)
        result = rp.project(base.db, base.root, base.first)
        self.assertEqual((result['processed'], result['added']), (1, 0))
        self.assertEqual(self.markers(base.db), self.guarded_marker())
        for db in (guarded.db, base.db):
            self.assertEqual(rp_master(db), reference(migration.SQL, source_guard.SQL))
            self.assertEqual(snapshot(db)['ledger_meta'], [('acceptance', 'candidate_unreconciled')])


def regression(case, module, ledger):
    """The schema-gate regression: a removed append-only guard must be refused exactly."""
    case.assert_refused(ledger, MISMATCH, module=module)


def update_attempt(db):
    con = sqlite3.connect(db, isolation_level=None)
    try:
        rp.register(con)
        con.execute('UPDATE rp_items SET plan=plan')
        return 'update_applied'
    except sqlite3.DatabaseError as error:
        return 'update_refused:' + str(error)
    finally:
        con.close()


class GateControl(Fixture, unittest.TestCase):
    CONTROL = 'campaign_tool.records.ledger._fa102_marker_only_control'

    def marker_only_module(self):
        """A disposable module instance whose install trusts markers only (schema gate disabled)."""
        spec = importlib.util.spec_from_file_location(self.CONTROL, rp.__file__)
        module = importlib.util.module_from_spec(spec)
        sys.modules[self.CONTROL] = module
        self.addCleanup(sys.modules.pop, self.CONTROL, None)
        spec.loader.exec_module(module)
        module._check_schema_objects = lambda con, scripts: None
        return module

    def record(self, module, case, outcome, detail=''):
        CONTROL_OUTCOMES.append({'module': module, 'case': case, 'outcome': outcome, 'detail': detail[:400]})

    def run_regression(self, label, module, ledger):
        try:
            regression(self, module, ledger)
        except AssertionError as error:
            self.record(label, 'removed_guard_regression', 'regression_failed', str(error))
            return False
        self.record(label, 'removed_guard_regression', 'regression_passed')
        return True

    def test_marker_only_install_is_detected_and_exact_schema_positive_passes(self):
        del CONTROL_OUTCOMES[:]
        control = self.marker_only_module()
        self.assertEqual(Path(control.__file__).resolve(), Path(rp.__file__).resolve())
        self.assertIsNot(control, rp)
        # Control: everything valid except one removed base append-only guard.
        broken = self.ledger()
        mutate(broken.db, 'DROP TRIGGER rp_items_no_update')
        self.assertEqual(self.markers(broken.db), self.guarded_marker())
        control_passed = self.run_regression('marker_only', control, broken)
        self.record('marker_only', 'append_only_update_after_reopen', update_attempt(broken.db))
        # Restored: the staged module on an identical fresh synthetic defect.
        staged_broken = self.ledger()
        mutate(staged_broken.db, 'DROP TRIGGER rp_items_no_update')
        staged_passed = self.run_regression('staged', rp, staged_broken)
        # Positives: both modules accept an exact schema; the intact guard still blocks updates.
        for label, module in (('marker_only', control), ('staged', rp)):
            valid = self.ledger()
            result = module.project(valid.db, valid.root, valid.manifest())
            self.record(label, 'exact_schema_positive',
                        'positive_passed' if (result['added'], result['binding_verified']) == (1, 1) else 'positive_failed')
            self.record(label, 'append_only_update_on_exact_schema', update_attempt(valid.db))
        observed = {(o['module'], o['case']): o['outcome'] for o in CONTROL_OUTCOMES}
        self.assertFalse(control_passed, 'marker-only control was not detected by the regression')
        self.assertTrue(staged_passed)
        self.assertEqual(observed, {
            ('marker_only', 'removed_guard_regression'): 'regression_failed',
            ('marker_only', 'append_only_update_after_reopen'): 'update_applied',
            ('staged', 'removed_guard_regression'): 'regression_passed',
            ('marker_only', 'exact_schema_positive'): 'positive_passed',
            ('marker_only', 'append_only_update_on_exact_schema'): 'update_refused:append-only evidence',
            ('staged', 'exact_schema_positive'): 'positive_passed',
            ('staged', 'append_only_update_on_exact_schema'): 'update_refused:append-only evidence',
        })


if __name__ == '__main__':
    unittest.main()
