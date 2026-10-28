"""Synthetic batch/snapshot composition; no product import or hooks at discovery.

The deliberately faulty reader is an in-memory oracle control, not a parser
qualification or a modification of captured database evidence.
"""
from contextlib import contextmanager, ExitStack
import builtins
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.request


TABLE_ORDER = {
    'rp_manifests': 'id', 'rp_sources': 'manifest_id, ordinal',
    'rp_items': 'source_id, line', 'rp_artifacts': 'sha',
    'rp_gaps': 'source_id, item_id, reason', 'rp_steps': 'manifest_id, sequence',
}
FROZEN_MESSAGE = 'frozen-index bytes must survive resume'


def sha(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def offline_boundary(prefix):
    """Scoped sentinels installed BEFORE application import, restored on exit.

    Inspected import closure has no data-path constants or bound path defaults:
    package __init__, store, receipt_projection and their three SQL migrations.
    All database and receipt paths are explicit synthetic arguments. These
    sentinels are not a hostile-code sandbox.
    """
    prefix = Path(prefix).resolve()
    real_connect = sqlite3.connect
    real_import = builtins.__import__
    prior_temp = tempfile.tempdir
    prior_umask = os.umask(0o077)

    def denied(*args, **kwargs):
        raise RuntimeError('FA1601 offline network/model sentinel')

    def importing(name, *args, **kwargs):
        if name.split('.')[0] in {'openai', 'anthropic', 'ollama', 'litellm', 'requests'}:
            return denied()
        return real_import(name, *args, **kwargs)

    def connect(database, *args, **kwargs):
        value = os.fspath(database)
        if value.startswith('file:'):
            from urllib.parse import unquote, urlsplit
            value = unquote(urlsplit(value).path)
        target = Path(value).absolute()
        if prefix not in target.parents or any(p.is_symlink() for p in (target, *target.parents)):
            raise RuntimeError('FA1601 real-data SQLite sentinel')
        return real_connect(database, *args, **kwargs)

    try:
        tempfile.tempdir = str(prefix)
        with ExitStack() as stack:
            stack.enter_context(patch.object(socket.socket, 'connect', denied))
            stack.enter_context(patch.object(socket.socket, 'connect_ex', denied))
            stack.enter_context(patch.object(socket, 'create_connection', denied))
            stack.enter_context(patch.object(urllib.request, 'urlopen', denied))
            stack.enter_context(patch.object(builtins, '__import__', importing))
            stack.enter_context(patch.object(sqlite3, 'connect', connect))
            yield
    finally:
        tempfile.tempdir = prior_temp
        os.umask(prior_umask)


def read_capture(store, database):
    with store.ledger(database, readonly=True) as con:
        return {table: [dict(row) for row in con.execute(
            f'SELECT * FROM {table} ORDER BY {order}')]
            for table, order in TABLE_ORDER.items()}


def faulty_resume_reader(store, database, caller_root):
    """Intentionally reread today's final index, ONLY in the resume observation.

    No SQL writes and no patches to product code. All other observations,
    including captured artifacts, are left intact so the deciding assertion is
    specifically the frozen-source byte comparison.
    """
    result = read_capture(store, database)
    for source in result['rp_sources']:
        if source['ordinal'] == 2:
            source['body'] = (caller_root / source['path']).read_bytes()
    return result


class ReceiptPartitionsTests(unittest.TestCase):
    def setUp(self):
        # A caller-owned directory, not a product default; fixture-owned teardown
        # is permitted. Launcher chooses a retained parent for all these paths.
        self.tmp = tempfile.TemporaryDirectory(prefix='fa1601-')
        self.addCleanup(self.tmp.cleanup)
        self.prefix = Path(self.tmp.name)
        self.prefix.chmod(0o700)
        self.boundary = offline_boundary(self.prefix)
        self.boundary.__enter__()
        self.addCleanup(self.boundary.__exit__, None, None, None)
        self.store = importlib.import_module('campaign_tool.records.ledger.store')
        self.rp = importlib.import_module('campaign_tool.records.ledger.receipt_projection')

    def seed(self, path, original):
        self.store.initialize(path)
        with self.store.ledger(path) as con:
            con.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (sha(original), len(original), 'text/plain', None, 'original',
                         'in_scope', None, 'captured', None, '{}'))
            con.commit()

    def assert_nonpromotion(self, result):
        self.assertEqual(result['stage_promotions'], 0)
        self.assertIs(result['review_accepted'], False)
        self.assertIs(result['publication_ready'], False)
        self.assertIsNone(result['end_to_end_complete'])

    def assert_equivalent(self, expected, observed):
        # This exact assertion is shared with the separately labelled bad reader.
        self.assertEqual([r['body'] for r in expected['rp_sources']],
                         [r['body'] for r in observed['rp_sources']], FROZEN_MESSAGE)
        for table in TABLE_ORDER:
            self.assertEqual(expected[table], observed[table], table)

    def comparison(self, failure, batch_size, reader=read_capture):
        caller = self.prefix / f'{failure}-{batch_size}'
        caller.mkdir(mode=0o700)
        original = b'FA1601 fictional original membership'
        artifact_a = b'{"fiction":"FA1601 alpha receipt"}'
        artifact_b = b'{"fiction":"FA1601 beta receipt"}'
        for name, body in [('original.txt', original), ('alpha.json', artifact_a),
                           ('beta.json', artifact_b)]:
            (caller / name).write_bytes(body)
        row = {'original': sha(original), 'artifact': sha(artifact_a),
               'path': 'alpha.json', 'declaration': 'fictional initial'}
        final_row = dict(row, artifact=sha(artifact_b), path='beta.json')

        def lines(rows):
            return b''.join(json.dumps(r, sort_keys=True).encode() + b'\n' for r in rows)

        good_first = lines([row, row])
        good_final = b'not a JSON record\n' + lines([row, final_row])
        repaired_middle = lines([dict(row, declaration='fictional repaired peer')])
        changed_final = b'not a JSON record\n' + lines([
            dict(row, declaration='fictional changed source'), final_row])
        paths = ['first.jsonl', 'middle.jsonl', 'final.jsonl']
        mapping = {'original_hash': 'original', 'artifact_hash': 'artifact',
                   'artifact_path': 'path'}
        manifest = {'version': 1, 'indexes': [
            {'path': path, 'sha256': sha(body), 'kind': 'reviews', 'fields': mapping,
             'declarations': {'status': 'declaration'}}
            for path, body in zip(paths, [good_first, repaired_middle, good_final])]}
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
        (caller / 'manifest.json').write_bytes(manifest_bytes)
        (caller / paths[0]).write_bytes(good_first)
        (caller / paths[2]).write_bytes(good_final)
        failed_bytes = None if failure == 'missing' else b'fictional wrong index bytes\n'
        if failed_bytes is not None:
            (caller / paths[1]).write_bytes(failed_bytes)
        one, resumed, fresh = [caller / (name + '.sqlite') for name in ('one', 'resumed', 'fresh')]
        for db in (one, resumed, fresh):
            self.seed(db, original)
        before = {db: self.store.counts(db) for db in (one, resumed, fresh)}
        with self.store.ledger(resumed, readonly=True) as con:
            original_rows = [tuple(r) for r in con.execute('SELECT * FROM originals ORDER BY sha256')]
        fixed_bytes = {name: (caller / name).read_bytes()
                       for name in ('original.txt', 'alpha.json', 'beta.json', 'manifest.json')}

        def project(db, size):
            result = self.rp.project(db, caller, 'manifest.json', batch_size=size, max_batches=1)
            self.assert_nonpromotion(result)
            return result

        complete = project(one, 100)
        first = project(resumed, batch_size)
        self.assertEqual(first['added'], batch_size)
        self.assertEqual(first['remaining'], 5 - batch_size)
        self.assertEqual(first['status'], 'in_progress')
        initial = read_capture(self.store, resumed)
        self.assertEqual([r['ordinal'] for r in initial['rp_sources']], [0, 1, 2])
        self.assertEqual(initial['rp_sources'][1]['body'], failed_bytes)
        error = 'missing_file' if failure == 'missing' else 'index_hash_mismatch'
        self.assertEqual(initial['rp_sources'][1]['error'], error)
        self.assertEqual(initial['rp_sources'][2]['body'], good_final)

        # After capture, only source-index bytes change. Manifest, originals and
        # every artifact remain byte-identical for the entire comparison.
        (caller / paths[1]).write_bytes(repaired_middle)
        (caller / paths[2]).write_bytes(changed_final)
        additions = [first['added']]
        last = first
        for _ in range(5):
            if last['remaining'] == 0:
                break
            last = project(resumed, batch_size)
            additions.append(last['added'])
        self.assertEqual(last['remaining'], 0)
        self.assertEqual(additions, [1, 1, 1, 1, 1] if batch_size == 1 else [2, 2, 1])
        self.assertEqual(complete['added'], 5)
        self.assertNotEqual(complete['added'], last['added'])
        self.assertEqual({k: v for k, v in complete.items() if k != 'added'},
                         {k: v for k, v in last.items() if k != 'added'})
        replay = project(resumed, batch_size)
        self.assertEqual(replay['added'], 0)
        self.assertEqual({k: v for k, v in last.items() if k != 'added'},
                         {k: v for k, v in replay.items() if k != 'added'})
        self.assertEqual(complete['processed'], 5)
        self.assertEqual(complete['binding_verified'], 4)
        self.assertEqual(complete['gaps'], 2)
        self.assertEqual(complete['status'], 'captured_with_gaps')

        expected = read_capture(self.store, one)
        actual = read_capture(self.store, resumed)
        self.assertEqual(actual['rp_sources'], initial['rp_sources'])
        self.assertEqual([r['sequence'] for r in actual['rp_steps']], [1, 2, 3, 4, 5])
        source_ordinals = {r['id']: r['ordinal'] for r in actual['rp_sources']}
        by_id = {r['id']: r for r in actual['rp_items']}
        schedule = [(source_ordinals[by_id[s['item_id']]['source_id']],
                     by_id[s['item_id']]['line']) for s in actual['rp_steps']]
        self.assertEqual(schedule, [(0, 1), (0, 2), (2, 1), (2, 2), (2, 3)])
        self.assertEqual([r['body'] for r in actual['rp_artifacts']],
                         [r['body'] for r in expected['rp_artifacts']])
        self.assertEqual({r['body'] for r in actual['rp_artifacts']}, {artifact_a, artifact_b})
        self.assertEqual(sorted(r['reason'] for r in actual['rp_gaps']),
                         sorted([error, 'malformed_record']))
        malformed = [r for r in actual['rp_items'] if r['raw'] == b'not a JSON record\n']
        self.assertEqual(len(malformed), 1)
        self.assertEqual(malformed[0]['binding_verified'], 0)
        self.assertEqual(sum(r['artifact_sha'] == sha(artifact_a) for r in actual['rp_items']), 3)

        repaired = project(fresh, 100)
        fresh_capture = read_capture(self.store, fresh)
        self.assertIsNone(fresh_capture['rp_sources'][1]['error'])
        self.assertEqual(fresh_capture['rp_sources'][1]['body'], repaired_middle)
        self.assertEqual(fresh_capture['rp_sources'][2]['error'], 'index_hash_mismatch')
        self.assertEqual(repaired['processed'], 3)
        self.assertEqual(repaired['binding_verified'], 3)
        self.assertTrue(any(r['source_id'] == fresh_capture['rp_sources'][1]['id']
                            and r['binding_verified'] == 1 for r in fresh_capture['rp_items']))
        for db in (one, resumed, fresh):
            self.assertEqual(self.store.counts(db), before[db])
            with self.store.ledger(db, readonly=True) as con:
                self.assertEqual([tuple(r) for r in con.execute('SELECT * FROM originals ORDER BY sha256')],
                                 original_rows)
                self.assertEqual(con.execute('SELECT count(*) FROM stage_state').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT count(*) FROM stage_events').fetchone()[0], 0)
        self.assertEqual({name: (caller / name).read_bytes() for name in fixed_bytes}, fixed_bytes)
        if reader is read_capture:
            observed = reader(self.store, resumed)
        else:
            observed = reader(self.store, resumed, caller)
        self.assert_equivalent(expected, observed)

    def test_missing_peer_frozen_across_batches(self):
        for size in (1, 2):
            with self.subTest(batch_size=size):
                self.comparison('missing', size)

    def test_hash_mismatch_peer_frozen_across_batches(self):
        for size in (1, 2):
            with self.subTest(batch_size=size):
                self.comparison('hash-mismatch', size)


# Not a discoverable TestCase: the launcher explicitly selects this local
# variant, retains its actual failed unittest result, and reruns the positive.
# Ordinary unittest discovery therefore never contains an expected-red case.
def negative_suite():
    class FaultyCaptureReader(ReceiptPartitionsTests):
        def test_faulty_resume_reader(self):
            self.comparison('missing', 1, reader=faulty_resume_reader)
    return unittest.TestSuite([FaultyCaptureReader('test_faulty_resume_reader')])


if __name__ == '__main__':
    unittest.main()
