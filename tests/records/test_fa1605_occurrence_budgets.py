"""Small fictional captured populations; no import-time application work."""
from contextlib import ExitStack, contextmanager
import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing


@contextmanager
def offline(root):
    """Scoped accidental-access sentinels, not a hostile-code sandbox.

    The selected store/projection import closure has no data path constants or
    data-bearing bound defaults. All database paths are explicit. The legacy
    compatibility fixture's sole ambient path is TemporaryDirectory's default.
    """
    root = Path(root).absolute()
    repo = Path(__file__).resolve().parents[2]
    connect = sqlite3.connect
    original_open, original_io_open = builtins.open, io.open
    temporary_directory = tempfile.TemporaryDirectory
    old_umask = os.umask(0o077)
    os.umask(old_umask)

    def path_checked(value, *, write=False):
        if isinstance(value, int):
            return
        text = os.fsdecode(value)
        if text.startswith('file:'):
            text = text[5:].split('?', 1)[0]
        path = Path(text).absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise RuntimeError('synthetic_path_symlink')
        if not path.is_relative_to(root) and (write or not path.is_relative_to(repo)):
            raise RuntimeError('real_data_access_refused')

    def database(value, *args, **kwargs):
        # Only SQLite's exact private-memory target is non-filesystem. Keep
        # file: URIs and every real database path subject to the same guard.
        if os.fspath(value) == ':memory:':
            return connect(value, *args, **kwargs)
        path_checked(value, write=True)
        return connect(value, *args, **kwargs)

    def guarded_open(original):
        def opened(value, mode='r', *args, **kwargs):
            path_checked(value, write=any(c in mode for c in 'wax+'))
            return original(value, mode, *args, **kwargs)
        return opened

    def temporary(*args, **kwargs):
        kwargs.setdefault('dir', root)
        return temporary_directory(*args, **kwargs)

    def refused(*args, **kwargs):
        raise RuntimeError('network_model_or_child_refused')

    try:
        with ExitStack() as stack:
            stack.enter_context(patch('sqlite3.connect', database))
            stack.enter_context(patch('builtins.open', guarded_open(original_open)))
            stack.enter_context(patch('io.open', guarded_open(original_io_open)))
            stack.enter_context(patch('tempfile.TemporaryDirectory', temporary))
            stack.enter_context(patch('socket.create_connection', refused))
            stack.enter_context(patch.object(socket.socket, 'connect', refused))
            stack.enter_context(patch('subprocess.Popen', refused))
            yield
    finally:
        os.umask(old_umask)


def capture(root):
    """Build two source-shaped fictional rows and import through real APIs."""
    from campaign_tool.records.ledger import store
    source = root / 'source'
    source.mkdir(mode=0o700)
    database = root / 'target' / 'ledger.sqlite'
    original = hashlib.sha256(b'FA1605 fictional original').hexdigest()
    receipt = json.dumps(dict(mtime_ns=1, ctime_ns=2, device=3, inode=4,
                              observed_at='2000-01-01T00:00:00+00:00'))
    with closing(sqlite3.connect(source / 'intake.sqlite')) as con, con:
        con.execute('CREATE TABLE docs(sha TEXT PRIMARY KEY,bytes INTEGER,format TEXT,stage TEXT)')
        con.execute('INSERT INTO docs VALUES(?,?,?,?)', (original, 12, 'txt', 'complete'))
        con.execute('CREATE TABLE occurrences(oid TEXT,sha TEXT,root TEXT,path TEXT,parent TEXT,locator TEXT,receipt TEXT,ignored_note TEXT,PRIMARY KEY(oid,sha))')
        for index in range(2):
            con.execute('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)',
                        ('fictional-' + str(index), original, 'fictional-root',
                         str(index) + '.txt', None, None, receipt, 'café 雪 🐦' * 4))
        for name in store.SOURCE_TABLES:
            if name not in ('docs', 'occurrences'):
                con.execute('CREATE TABLE "' + name + '"(sha TEXT)')
    manifest = {'database_snapshot_sha256': hashlib.sha256((source / 'intake.sqlite').read_bytes()).hexdigest()}
    (source / 'input-manifest.json').write_text(json.dumps(manifest))
    result = store.import_legacy(source, database)
    if result['status'] != 'captured':
        raise RuntimeError('fixture_capture_incomplete')
    with store.ledger(database, readonly=True) as con:
        payloads = [r[0].encode('utf-8') for r in con.execute(
            "SELECT payload_json FROM legacy_rows WHERE source_table='occurrences' ORDER BY source_rowid")]
    for index, raw in enumerate(payloads):
        (root / ('retained-payload-' + str(index) + '.json')).write_bytes(raw)
    # Independent Python lengths of retained bytes, not the loader's SQL.
    retained = [(root / ('retained-payload-' + str(i) + '.json')).read_bytes() for i in range(len(payloads))]
    return database, result['import_id'], retained


def snapshot(database):
    from campaign_tool.records.ledger.store import ledger
    with ledger(database, readonly=True) as con:
        schema = [tuple(row) for row in con.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        contents = {}
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            name = row[0]
            contents[name] = sorted([tuple(r) for r in con.execute('SELECT * FROM "' + name + '"')], key=repr)
    return schema, contents


class OccurrenceBudgetTests(unittest.TestCase):
    def setUp(self):
        # Each test keeps its own residue; the launcher supplies its temp root.
        self.root = Path(tempfile.mkdtemp(prefix='fa1605-'))
        self.guard = offline(self.root)
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)
        from campaign_tool.records.ledger import occurrence_projection, store
        self.projection = occurrence_projection
        # Prove cold-cache execution under the sentinels on every test.
        store._base_schema.cache_clear()
        self.database, self.import_id, self.payloads = capture(self.root)
        self.rows = len(self.payloads)
        self.bytes = sum(map(len, self.payloads))
        self.characters = sum(len(raw.decode('utf-8')) for raw in self.payloads)
        self.assertEqual(self.rows, 2)
        self.assertGreater(self.bytes, self.characters + 1)
        self.assertLess(max(map(len, self.payloads)), self.characters)
        self.assertTrue(all('café 雪 🐦' in json.loads(raw)['ignored_note'] for raw in self.payloads))

    def success(self):
        before = snapshot(self.database)
        with patch.object(self.projection, 'MAX_PAYLOAD_BYTES', self.bytes):
            first = self.projection.project_occurrences(self.database, self.import_id, max_rows=self.rows)
            projected = snapshot(self.database)
            second = self.projection.project_occurrences(self.database, self.import_id, max_rows=self.rows)
        self.assertFalse(first['reused'])
        self.assertTrue(second['reused'])
        self.assertEqual(first['projection_id'], second['projection_id'])
        self.assertEqual(first['canonical_kinds'], {'local': self.rows})
        self.assertEqual(first['blocked_source_rows'], 0)
        self.assertEqual(first['stage_promotions'], 0)
        self.assertFalse(first['publication_ready'])
        self.assertIsNone(first['end_to_end_complete'])
        self.assertEqual(projected, snapshot(self.database))
        after = projected[1]
        for table, data in before[1].items():
            if table not in ('occurrences', 'joins', 'ledger_meta'):
                self.assertEqual(data, after[table], table)
        self.assertEqual([r for r in after['ledger_meta'] if not r[0].startswith('extension:')], before[1]['ledger_meta'])
        from campaign_tool.records.ledger.store import ledger
        with ledger(self.database, readonly=True) as con:
            saved = [r[0].encode('utf-8') for r in con.execute(
                'SELECT payload_json FROM occurrence_projection_rows ORDER BY source_rowid')]
        self.assertEqual(saved, self.payloads)

    def refusal(self, rows, budget):
        before = snapshot(self.database)
        self.assertNotIn('occurrence_projection_rows', before[1])
        with patch.object(self.projection, 'MAX_PAYLOAD_BYTES', budget), patch.object(
                self.projection, 'decode_source', side_effect=ValueError('decode_spy_reached')) as spy, patch.object(
                self.projection, '_ensure_schema', side_effect=RuntimeError('projection_write_reached')) as extension:
            try:
                self.projection.project_occurrences(self.database, self.import_id, max_rows=rows)
            except ValueError as error:
                self.assertEqual(str(error), 'occurrence_projection_resource_limit', 'UTF8_REFUSAL_ORACLE')
            else:
                self.fail('UTF8_REFUSAL_ORACLE: refusal missing')
            spy.assert_not_called()
            extension.assert_not_called()
        self.assertEqual(snapshot(self.database), before)
        # Restore only the budget, using the exact same capture and target.
        self.success()

    def test_exact_aggregate_bytes_and_rows_project_and_replay(self):
        self.success()

    def test_row_only_one_below_refuses_before_decode(self):
        self.refusal(self.rows - 1, self.bytes)

    def test_byte_only_one_below_refuses_before_decode(self):
        self.refusal(self.rows, self.bytes - 1)

    def test_utf8_between_character_and_byte_lengths_refuses(self):
        budget = self.characters + (self.bytes - self.characters) // 2
        self.assertLess(self.characters, budget)
        self.assertLess(budget, self.bytes)
        self.refusal(self.rows, budget)


@contextmanager
def character_budget_variant():
    """Only the aggregate budget measure changes, through a connection seam."""
    from campaign_tool.records.ledger import occurrence_projection as projection
    loader = projection._load_rows

    class CharacterMeasure:
        def __init__(self, con):
            self.con = con

        def execute(self, sql, parameters=()):
            sql = sql.replace('length(CAST(payload_json AS BLOB))', 'length(payload_json)')
            return self.con.execute(sql, parameters)

    def faulty(con, import_id, max_rows):
        return loader(CharacterMeasure(con), import_id, max_rows)

    with patch.object(projection, '_load_rows', faulty):
        yield
