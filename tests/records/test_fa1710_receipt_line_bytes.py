"""Fictional two-row receipt captures: lexical identity is not acceptance."""
from contextlib import ExitStack, contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch


def sha(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def offline(root):
    """Scope sentinels before application import; not a hostile-code sandbox.

    The inspected import closure is package initializers, store, receipt_projection
    and their literal migrations. It has no data constants or bound path defaults.
    All application filesystem roots and database arguments are caller-owned.
    """
    original_open = os.open
    original_connect = sqlite3.connect
    previous_umask = os.umask(0o077)

    def owned(value):
        path = Path(os.path.abspath(os.fspath(value)))
        if path != root and root not in path.parents:
            raise RuntimeError('nonfixture data access')
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise RuntimeError('fixture symlink')
        return path

    def checked_open(value, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            owned(value)
        elif Path(value).is_absolute() or '..' in Path(value).parts:
            raise RuntimeError('nonfixture relative access')
        return original_open(value, flags, mode, dir_fd=dir_fd)

    def connect(value, *args, **kwargs):
        text = os.fspath(value)
        # Schema verification uses a private database with no filesystem access.
        if text == ':memory:':
            return original_connect(value, *args, **kwargs)
        if text.startswith('file:'):
            from urllib.parse import unquote, urlsplit
            owned(unquote(urlsplit(text).path))
        else:
            owned(text)
        return original_connect(value, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise RuntimeError('offline network/model/process sentinel')

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(os, 'open', checked_open))
            stack.enter_context(patch.object(sqlite3, 'connect', connect))
            stack.enter_context(patch.object(socket, 'socket', forbidden))
            stack.enter_context(patch.object(socket, 'create_connection', forbidden))
            stack.enter_context(patch.object(subprocess, 'Popen', forbidden))
            yield
    finally:
        os.umask(previous_umask)


class ReceiptLineBytesTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix='fa1710-', dir=tempfile.gettempdir()))
        self.root = Path(self.tmp)
        self.root.chmod(0o700)
        self.stack.enter_context(offline(self.root))
        self.store = importlib.import_module('campaign_tool.records.ledger.store')
        self.rp = importlib.import_module('campaign_tool.records.ledger.receipt_projection')
        self.original_bytes = b'FA1710 fictional original, never a campaign record'
        self.original = sha(self.original_bytes)
        self.artifact = b'{"fictional":"receipt artifact","acceptance":false}'
        self.reviewed = sha(b'FA1710 fictional proposal, not the original')
        self.fields = {'original_hash': 'original', 'artifact_hash': 'receipt',
                       'artifact_path': 'path', 'reviewed_content_hash': 'reviewed'}
        self.rows = [dict(original=self.original, receipt=sha(self.artifact),
                          path='artifact.json', reviewed=self.reviewed,
                          reviewer='fictional checker', coverage=[number],
                          holds=['human review owed']) for number in (1, 2)]
        self.expected_plans = [dict(original_hash=self.original,
            artifact_hash=sha(self.artifact), artifact_path='artifact.json',
            reviewed_content_hash=self.reviewed, artifact_source_locator='artifact.json',
            declarations={'reviewer': 'fictional checker', 'coverage': [number],
                          'holds': ['human review owed']}, reasons=[])
            for number in (1, 2)]

    def spans(self, style='compact', endings=(b'\n', b'\n')):
        # Expected spans are assembled individually, never split by the product
        # splitter (or by splitlines). Alternate valid lexemes preserve meaning.
        result = []
        for row, ending in zip(self.rows, endings):
            if style == 'lexical':
                row = dict(reversed(list(row.items())))
                text = ' \t' + json.dumps(row, separators=(' , ', ' : ')) + '  '
            else:
                text = json.dumps(row, separators=(',', ':'))
            result.append(text.encode('utf-8') + ending)
        return result

    def capture(self, name, spans):
        root = self.root / name
        root.mkdir(mode=0o700)
        database = root / 'ledger.sqlite'
        self.store.initialize(database)
        with self.store.ledger(database) as con:
            con.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',
                (self.original, len(self.original_bytes), 'text/plain', None,
                 'original', 'in_scope', None, 'captured', None, '{}'))
            con.commit()
        (root / 'artifact.json').write_bytes(self.artifact)
        data = b''.join(spans)
        (root / 'index.jsonl').write_bytes(data)
        manifest = {'version': 1, 'indexes': [{'path': 'index.jsonl',
            'sha256': sha(data), 'kind': 'reviews', 'fields': self.fields,
            'declarations': {key: key for key in ('reviewer', 'coverage', 'holds')}}]}
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
        (root / 'manifest.json').write_bytes(manifest_bytes)
        before = self.store.counts(database)
        result = self.rp.project(database, root, 'manifest.json')
        self.assertEqual({key: result[key] for key in ('processed', 'added',
            'remaining', 'gaps', 'binding_verified', 'status')},
            dict(processed=2, added=2, remaining=0, gaps=0,
                 binding_verified=2, status='captured'))
        self.assertEqual(result['manifest_id'], sha(manifest_bytes))
        self.assertEqual(result['stage_promotions'], 0)
        self.assertIs(result['review_accepted'], False)
        self.assertIs(result['publication_ready'], False)
        self.assertIsNone(result['end_to_end_complete'])
        with self.store.ledger(database) as con:
            self.rp.register(con)
            sources = [dict(r) for r in con.execute('SELECT * FROM rp_sources')]
            items = [dict(r) for r in con.execute('SELECT * FROM rp_items ORDER BY line')]
            steps = [tuple(r) for r in con.execute('SELECT * FROM rp_steps ORDER BY sequence')]
            self.assertEqual(self.rp.verify(con, result['manifest_id']), 2)
            self.assertEqual(list(con.execute('SELECT * FROM rp_gaps')), [])
            self.assertEqual([r[0] for r in con.execute('SELECT body FROM rp_artifacts')],
                             [self.artifact])
            self.assertIsNotNone(con.execute("SELECT sql FROM sqlite_master WHERE name=?",
                (self.rp.source_guard.TRIGGER,)).fetchone())
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source['body'], data)
        self.assertEqual(source['size'], len(data))
        self.assertEqual(source['expected_sha'], sha(data))
        self.assertEqual(source['observed_sha'], sha(data))
        self.assertIsNone(source['error'])
        self.assertEqual([item['line'] for item in items], [1, 2])
        self.assert_raw(items, spans)
        self.assertEqual([json.loads(item['plan']) for item in items], self.expected_plans)
        self.assertEqual([item['original_sha'] for item in items], [self.original] * 2)
        self.assertEqual([item['artifact_sha'] for item in items], [sha(self.artifact)] * 2)
        self.assertEqual([item['binding_verified'] for item in items], [1, 1])
        # Replay the unchanged source, original membership and artifacts.
        replay = self.rp.project(database, root, 'manifest.json')
        self.assertEqual(replay, dict(result, added=0))
        with self.store.ledger(database) as con:
            self.rp.register(con)
            self.assertEqual([dict(r) for r in con.execute('SELECT * FROM rp_items ORDER BY line')], items)
            self.assertEqual([tuple(r) for r in con.execute('SELECT * FROM rp_steps ORDER BY sequence')], steps)
            self.assertEqual(list(con.execute('SELECT * FROM rp_gaps')), [])
            self.assertEqual(self.rp.verify(con, result['manifest_id']), 2)
        self.assertEqual(self.store.counts(database), before)
        self.assertEqual((root / 'index.jsonl').read_bytes(), data)
        self.assertEqual((root / 'artifact.json').read_bytes(), self.artifact)
        print(json.dumps({'fixture': name, 'source_sha256': sha(data),
            'manifest_sha256': sha(manifest_bytes), 'artifact_sha256': sha(self.artifact),
            'span_sha256': [sha(span) for span in spans],
            'span_sizes': [len(span) for span in spans]}, sort_keys=True))
        return source, items

    def assert_raw(self, items, spans):
        self.assertEqual([item['raw'] for item in items], spans,
                         'FA1710 exact raw byte spans including terminators')

    def test_line_ending_and_lexeme_matrix(self):
        captures = []
        for style in ('compact', 'lexical'):
            for name, endings in (
                ('lf', (b'\n', b'\n')), ('crlf', (b'\r\n', b'\r\n')),
                ('lf-unclosed', (b'\n', b'')), ('crlf-unclosed', (b'\r\n', b'')),
                ('mixed', (b'\n', b'\r\n'))):
                with self.subTest(style=style, name=name):
                    spans = self.spans(style, endings)
                    self.assertEqual([json.loads(span) for span in spans], self.rows)
                    captures.append(self.capture(style + '-' + name, spans))
        # Every body differs; semantically equal plans/bindings do not imply
        # equal raw-source, manifest, item or evidence identities.
        self.assertEqual(len(captures), 10)
        for key in ('id', 'observed_sha', 'manifest_id'):
            self.assertEqual(len({source[key] for source, _ in captures}), len(captures))
        for position in (0, 1):
            for key in ('id', 'evidence_sha'):
                self.assertEqual(len({items[position][key] for _, items in captures}), len(captures))

    def test_database_guard_allows_only_private_memory_or_owned_files(self):
        with sqlite3.connect(':memory:') as con:
            self.assertEqual(con.execute('SELECT 1').fetchone(), (1,))
        outside = self.root.parent / (self.root.name + '-outside.sqlite')
        for value, uri in ((outside, False),
                           (outside.as_uri(), True),
                           (':memory:outside', False)):
            with self.subTest(value=str(value)):
                with self.assertRaisesRegex(RuntimeError, 'nonfixture data access'):
                    sqlite3.connect(value, uri=uri)
        self.assertFalse(outside.exists())

    def test_crlf_oracle_positive(self):
        self.capture('crlf-oracle-positive', self.spans(endings=(b'\r\n', b'\r\n')))

    def stripped_result_control(self):
        """Not discovered as an ordinary test: the launcher retains its failure."""
        spans = self.spans(endings=(b'\r\n', b'\r\n'))
        _, items = self.capture('crlf-negative-control', spans)
        transformed = [dict(item, raw=item['raw'].rstrip(b'\r\n')) for item in items]
        self.assert_raw(transformed, spans)


if __name__ == '__main__':
    unittest.main()
