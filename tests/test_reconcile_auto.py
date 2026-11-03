"""Synthetic acceptance tests for the unattended drift detector."""
import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import MappingProxyType
from unittest import mock

from campaign_tool.records.ledger import stages, store
from tests.records import test_ledger_stages
from tools import reconcile_auto as tool

REPO = Path(__file__).resolve().parents[1]


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.campaign = self.base / 'campaign'
        self.run_cli('init', '--name', 'Synthetic', '--county', 'Synthetic', '--state', 'CA')
        self.objects = self.campaign / 'private' / 'objects'

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', args[0],
                                 '--directory', str(self.campaign), *args[1:]],
                                cwd=REPO, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def ingest(self, name, data):
        path = self.base / name
        path.write_bytes(data)
        self.run_cli('ingest', '--file', str(path), '--source-id', 'synthetic')
        return hashlib.sha256(data).hexdigest()

    def check(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tool.main(['check', *map(str, args)])
        text = out.getvalue()
        self.assertNotIn(str(self.base), text)
        self.assertNotRegex(text, r'[a-f0-9]{64}|@|\b(late|overdue)\b|violat|unlawful|illegal|non-?complian|breach')
        return code, text

    def snapshot(self, root):
        return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest()) for p in root.rglob('*')
                if p.is_file() and not p.name.endswith(('-shm', '-wal'))}

    def test_clean_campaign(self):
        self.ingest('approved', b'synthetic clean')
        before = self.snapshot(self.campaign)
        code, text = self.check('--campaign', self.campaign)
        self.assertEqual(code, 0)
        self.assertIn('roots=1 alerts=0', text)
        self.assertEqual(before, self.snapshot(self.campaign))

    def test_missing_changed_orphan_temp_duplicate_stable(self):
        missing = self.ingest('reviewed', b'missing')
        changed = self.ingest('ok', b'changed')
        (self.objects / missing).unlink()
        (self.objects / changed).write_bytes(b'CHANGED')
        for i in range(2):
            (self.objects / hashlib.sha256(str(i).encode()).hexdigest()).write_text('orphan')
        (self.objects / '.intake-synthetic').write_text('partial')
        before = self.snapshot(self.campaign)
        first = self.check('--campaign', self.campaign)
        second = self.check('--campaign', self.campaign)
        self.assertEqual(first, second)
        self.assertEqual(first[0], 3)
        for name in ('missing_file', 'hash_mismatch', 'no_ledger_row', 'leftover_temp'):
            self.assertIn('object-' + name, first[1])
        self.assertIn('object-no_ledger_row | action | count=2', first[1])
        self.assertEqual(before, self.snapshot(self.campaign))

    def test_missing_and_bad_ledgers(self):
        self.assertIn('ledger-unreadable', self.check('--campaign', self.campaign)[1])
        self.ingest('one', b'object')
        (self.campaign / 'private' / 'ledger.sqlite').write_bytes(b'not sqlite')
        self.assertIn('ledger-unreadable', self.check('--campaign', self.campaign)[1])
        empty = self.base / 'empty'
        empty.mkdir()
        self.assertIn('records-ledger-missing', self.check('--records-root', empty)[1])

    def test_records_readonly_connections_and_unchanged(self):
        fixture = test_ledger_stages.StageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.database.parent
        before = self.snapshot(root)
        connect = sqlite3.connect
        observed = []
        def readonly(database, *args, **kwargs):
            if database != ':memory:':
                self.assertIn('mode=ro', str(database))
                observed.append(database)
            return connect(database, *args, **kwargs)
        with mock.patch('sqlite3.connect', side_effect=readonly):
            self.assertEqual(stages.counts(fixture.database)['originals'], 2)
            code, text = self.check('--records-root', root)
        self.assertTrue(observed)
        self.assertEqual(code, 0, text)
        self.assertEqual(before, self.snapshot(root))

    def test_missing_schema_readonly_does_not_install(self):
        root = self.base / 'ledger-root'
        root.mkdir(mode=0o700)
        db = root / 'ledger.sqlite'
        store.initialize(db)
        before = db.read_bytes()
        with self.assertRaisesRegex(stages.StageError, 'controller_schema_missing'):
            stages.counts(db)
        self.assertEqual(before, db.read_bytes())
        self.assertEqual(self.check('--records-root', root)[0], 1)
        self.assertEqual(before, db.read_bytes())
        self.assertEqual(stages.query_counts(db)['originals'], 0)

    def test_real_drift_and_blocked_stage_persist(self):
        fixture = test_ledger_stages.StageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.database.parent
        fixture.prepare('preserve')
        claim = fixture.runner.claim(fixture.subject, 'preserve')
        fixture.runner.validators = MappingProxyType({})
        with self.assertRaisesRegex(stages.StageError, 'domain_adapter_unavailable'):
            fixture.runner.promote(fixture.subject, 'preserve',
                                   json.dumps(fixture.packet('preserve')).encode(), claim_id=claim['claim_id'])
        public = root / 'proposals' / 'public'
        public.mkdir(parents=True)
        (public / 'approved.md').write_text('edited synthetic content')
        digest = hashlib.sha256(b'original synthetic content').hexdigest()
        with store.ledger(fixture.database) as con:
            con.execute('INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?)',
                        ('approved', 'A', None, digest, '0' * 64, None, None, 'none', None, None))
            con.commit()
        before = self.snapshot(root)
        first = self.check('--records-root', root)
        self.assertEqual(first, self.check('--records-root', root))
        self.assertEqual(first[0], 3)
        self.assertIn('proposal-drifted', first[1])
        self.assertIn('stage-blocked', first[1])
        self.assertEqual(before, self.snapshot(root))

    def test_records_mapping_repeated_and_no_wording_override(self):
        root = self.base / 'records'
        root.mkdir()
        status = {'ledger': True, 'end_to_end_complete': False, 'drifted_proposals': ['approved'],
                  'review_retirement_pending': ['one'], 'blocked': [{'stage': 'extract', 'reason': 'ok'}],
                  'alerts': [{'key': 'resolved-synthetic'}]}
        with mock.patch.object(tool.records_run, 'status', return_value=status):
            first = self.check('--records-root', root)
            second = self.check('--records-root', root)
        self.assertEqual(first, second)
        self.assertEqual(first[0], 3)
        for kind in ('proposal-drifted', 'stage-blocked', 'ledger-alert-open', 'review-retirement-pending'):
            self.assertIn(kind, first[1])

    def test_partial_and_all_roots_and_link_refusals(self):
        self.ingest('one', b'synthetic')
        records = self.base / 'records'
        records.mkdir()
        flock = self.base / 'flock'
        flock.mkdir()
        result = self.check('--campaign', self.campaign, '--records-root', records, '--flock-store', flock)
        self.assertIn('roots=3', result[1])
        self.assertIn('records-ledger-missing', result[1])
        result = self.check('--campaign', self.base / 'missing', '--records-root', records)
        self.assertEqual(result[0], 1)
        self.assertIn('checker-failed:campaign', result[1])
        self.assertIn('records-ledger-missing', result[1])
        link = self.base / 'link'
        link.symlink_to(records, target_is_directory=True)
        self.assertEqual(self.check('--records-root', link)[0], 1)

    def test_real_empty_flock_store_runs_unmocked(self):
        root = self.base / 'flock-real'
        agency = root / 'agencies' / 'sample-city-police-department'
        agency.mkdir(parents=True)
        fixtures = REPO / 'tests' / 'fixtures' / 'flock'
        (root / 'LAW.csv').write_bytes((fixtures / 'law-sample.csv').read_bytes())
        (agency / 'POLICY.csv').write_bytes((fixtures / 'policy-sample.csv').read_bytes())
        (agency / 'FACTS.csv').write_text(','.join(tool.flock.facts_tool.V2) + '\n')
        (agency / 'CONTACT.csv').write_text(
            'agency,role,email,source_url,source_sha256,checked_at,added_by\n'
            'Sample City Police Department,records,records' + chr(64) + 'city.example,'
            'https://www.city.example/records,' + 'a' * 64 + ',2026-10-30,t\n')
        before = self.snapshot(root)
        code, text = self.check('--flock-store', root)
        self.assertNotIn('checker-failed', text)
        self.assertIn('roots=1 alerts=0 info=0 checkers_failed=0', text)
        self.assertEqual(code, 0, text)
        self.assertEqual(before, self.snapshot(root))
        (agency / 'FACTS.csv').write_text('not,a,facts,header\n')
        code, text = self.check('--flock-store', root)
        self.assertEqual(code, 3, text)
        self.assertNotIn('checker-failed', text)
        self.assertIn('| flock-store-facts | action | count=', text)

    def test_loose_mode_is_an_object_alert_not_checker_failure(self):
        sha = self.ingest('loose', b'loose mode')
        os.chmod(self.objects / sha, 0o644)
        (self.objects / hashlib.sha256(b'orphan').hexdigest()).write_text('orphan')
        os.chmod(self.objects / hashlib.sha256(b'orphan').hexdigest(), 0o600)
        code, text = self.check('--campaign', self.campaign)
        self.assertEqual(code, 3, text)
        self.assertNotIn('checker-failed', text)
        self.assertIn('object-loose_mode | action | count=1', text)
        self.assertIn('object-no_ledger_row | action | count=1', text)

    def test_flock_sections_and_failures(self):
        root = self.base / 'flock'
        root.mkdir()
        def fake_run(_root, _args):
            for section in tool.flock.SECTIONS:
                if section == 'facts':
                    print('facts | secret@never-print')
                    print('facts | errors=2')
                elif section == 'slugs':
                    print('slugs | failed: ValueError')
                    print('slugs | errors=1')
                else:
                    print(section + ' | errors=0')
            return 1
        with mock.patch.object(tool.flock, 'run', side_effect=fake_run):
            code, text = self.check('--flock-store', root)
        self.assertEqual(code, 3)
        self.assertIn('flock-store-facts | action | count=2', text)
        self.assertIn('flock-store-slugs-failed', text)

    def test_private_alert_key_not_exposed_in_json(self):
        root = self.base / 'records'
        root.mkdir()
        target = self.base / 'output.json'
        status = {'ledger': True, 'drifted_proposals': [], 'review_retirement_pending': [],
                  'blocked': [], 'alerts': [{'key': 'private-synthetic-message'},
                                            {'key': 'extract:' + 'a' * 64 + ':resolved'}]}
        with mock.patch.object(tool.records_run, 'status', return_value=status):
            self.assertEqual(self.check('--records-root', root, '--json-out', target)[0], 3)
        text = target.read_text()
        self.assertNotIn('private-synthetic-message', text)
        self.assertNotIn('a' * 64, text)
        self.assertEqual(json.loads(text)['alerts'][0]['keys'], ['extract:' + 'a' * 12])

    def test_json_output_and_refusals(self):
        self.ingest('one', b'synthetic')
        target = self.base / 'alerts.json'
        self.assertEqual(self.check('--campaign', self.campaign, '--json-out', target)[0], 0)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(target.read_text()),
                         {'schema': 'flock-alerts-v1', 'review_required': True, 'alerts': []})
        self.assertEqual(self.check('--campaign', self.campaign, '--json-out', target)[0], 1)
        self.assertEqual(self.check('--campaign', self.campaign, '--json-out', REPO / 'bad.json')[0], 1)
        self.assertEqual(self.check('--campaign', self.campaign, '--json-out', self.campaign / 'bad.json')[0], 1)
        link = self.base / 'link'
        link.symlink_to(self.base, target_is_directory=True)
        self.assertEqual(self.check('--campaign', self.campaign, '--json-out', link / 'bad.json')[0], 1)
        with mock.patch.object(tool, 'write_new', side_effect=OSError):
            self.assertEqual(self.check('--campaign', self.campaign, '--json-out', self.base / 'x')[0], 1)
        self.assertFalse((self.base / 'x').exists())
        self.assertEqual(self.check('--campaign', self.base / 'missing', '--json-out', self.base / 'x')[0], 1)
        self.assertFalse((self.base / 'x').exists())

    def test_usage_and_imports(self):
        for args in ([], ['check'], ['check', '--fix'], ['check', '--apply'], ['repair']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                tool.main(args)
            self.assertEqual(error.exception.code, 2)
        tree = ast.parse((REPO / 'tools/reconcile_auto.py').read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split('.')[0])
        self.assertFalse(names & {'smtplib', 'imaplib', 'socket', 'ssl', 'http', 'urllib', 'requests', 'subprocess'})
