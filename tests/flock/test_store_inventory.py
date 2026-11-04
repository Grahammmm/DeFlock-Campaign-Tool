import ast
from contextlib import closing
import sqlite3
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / 'tools/store_inventory.py'
DOC = ROOT / 'docs/DATA-HANDLING-DECISIONS.md'
VALUES = ('clerk@city.example.gov', '(805) 555-1234', '7ABC123')


def decisions_valid(text):
    lines = [line for line in text.splitlines() if line.startswith('Decision:')]
    return lines == ['Decision: OPEN — G'] * 4


def snapshot(root):
    result = {}
    for p in sorted(root.rglob('*')):
        st = p.lstat()
        data = os.readlink(p) if p.is_symlink() else hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        result[str(p.relative_to(root))] = (stat.S_IMODE(st.st_mode), st.st_mtime_ns, data)
    return result


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.store = self.base / 'store'
        self.store.mkdir()
        self.out = self.base / 'result'

    def run_tool(self, out=None, store=None):
        return subprocess.run([sys.executable, '-B', str(TOOL), '--store', str(store or self.store),
                               '--out', str(out or self.out)], text=True, capture_output=True, check=False)

    def test_shapes_counts_privacy_and_unchanged(self):
        area = self.store / 'content'
        area.mkdir(mode=0o755)
        os.chmod(area, 0o755)
        empty = self.store / 'kit'
        empty.mkdir()
        data = (' '.join(VALUES) + '\n' + VALUES[0] + '\n').encode()
        f = area / 'record-bearing-name.txt'
        f.write_bytes(data)
        os.chmod(f, 0o644)
        outside = self.base / 'outside.txt'
        outside.write_text('outside sentinel')
        (area / 'linked.txt').symlink_to(outside)
        (area / '.hidden').mkdir()
        (area / '.hidden' / 'hidden.txt').write_text(VALUES[0])
        (area / 'binary.dat').write_bytes(b'\0')
        before = snapshot(self.store)
        run = self.run_tool()
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        self.assertEqual(before, snapshot(self.store))
        doc = json.loads((self.out / 'inventory.json').read_text())
        a = next(a for a in doc['areas'] if a['area'] == 'content')
        self.assertEqual(a['files'], 3)
        self.assertEqual(a['directories'], 2)
        self.assertEqual(a['bytes'], len(data) + 1 + len(VALUES[0]))
        self.assertEqual(a['category_counts'], {'email': 2, 'phone': 1, 'plate': 1})
        self.assertEqual(a['scan_skipped'], 2)
        self.assertEqual(a['hidden_directories_not_scanned'], 1)
        self.assertTrue(a['outside_symlink'])
        self.assertTrue(a['group_or_world_readable'])
        self.assertIn('0644', a['modes'])
        self.assertIn('0755', a['modes'])
        self.assertEqual(a['extensions']['.txt'], 2)
        self.assertEqual(next(a for a in doc['areas'] if a['area'] == 'kit')['files'], 0)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        self.assertEqual({stat.S_IMODE(p.stat().st_mode) for p in self.out.iterdir()}, {0o600})
        combined = run.stdout + run.stderr + ''.join(p.read_text() for p in self.out.iterdir())
        for secret in (*VALUES, 'record-bearing-name', 'outside sentinel'):
            self.assertNotIn(secret, combined)
        self.assertIn('not a privacy review', combined)
        self.assertIn('partial', combined)
        self.assertFalse(list(self.base.glob('.inventory-scan-*')))
        self.assertTrue(doc['review_required'])
        self.assertTrue(doc['enforced_by_code'])
        self.assertTrue(doc['source_errors'])

    def test_wal_ledger_without_symlinks_is_unchanged(self):
        private = self.store / 'private'
        objects = private / 'objects'
        private.mkdir(mode=0o700)
        objects.mkdir(mode=0o700)
        data = b'synthetic object\n'
        digest = hashlib.sha256(data).hexdigest()
        (objects / digest).write_bytes(data)
        os.chmod(objects / digest, 0o600)
        ledger = private / 'ledger.sqlite'
        with closing(sqlite3.connect(ledger)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            db.execute('CREATE TABLE objects (sha256 TEXT, byte_count INTEGER)')
            db.execute('INSERT INTO objects VALUES (?, ?)', (digest, len(data)))
            db.commit()
        os.chmod(ledger, 0o600)
        self.assertEqual(ledger.read_bytes()[18:20], b'\x02\x02')
        self.assertFalse(list(private.glob('ledger.sqlite-*')))
        self.assertFalse(any(p.is_symlink() for p in self.store.rglob('*')))
        before = snapshot(self.store)
        run = self.run_tool()
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        self.assertEqual(before, snapshot(self.store))
        doc = json.loads((self.out / 'inventory.json').read_text())
        self.assertEqual(doc['source_errors'], [])
        self.assertEqual(next(a for a in doc['areas'] if a['area'] == 'private')['files'], 2)

    def test_unreadable_or_sidecar_ledger_is_unchanged_and_reported(self):
        private = self.store / 'private'
        private.mkdir(mode=0o700)
        ledger = private / 'ledger.sqlite'
        ledger.write_bytes(b'not a database')
        for suffix in ('', '-wal', '-shm', '-journal'):
            with self.subTest(suffix=suffix):
                if suffix:
                    (private / ('ledger.sqlite' + suffix)).write_bytes(b'synthetic sidecar')
                before = snapshot(self.store)
                out = self.base / ('result' + suffix)
                run = self.run_tool(out=out)
                self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
                self.assertEqual(before, snapshot(self.store))
                doc = json.loads((out / 'inventory.json').read_text())
                self.assertIn('structural check failed', doc['source_errors'])
                if suffix:
                    (private / ('ledger.sqlite' + suffix)).unlink()

    def test_refusal_targets(self):
        (self.store / 'agencies').mkdir()
        existing = self.base / 'existing'
        existing.mkdir()
        linked = self.base / 'link'
        linked.symlink_to(self.base, target_is_directory=True)
        for target in (existing, ROOT / 'new-inventory-forbidden', self.store / 'agencies' / 'out', linked / 'out'):
            with self.subTest(target=str(target)):
                run = self.run_tool(target)
                self.assertEqual(run.returncode, 2)
                self.assertEqual(len(run.stdout.splitlines()), 1)
                if target != existing:
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(list(existing.iterdir()), [])
        self.assertFalse(list(self.base.glob('.inventory-scan-*')))

    def test_scan_fail_closed_and_receipt_cleanup(self):
        # A missing store refuses before output; a failed scan never produces a report.
        run = self.run_tool(store=self.base / 'missing')
        self.assertEqual(run.returncode, 2)
        self.assertFalse(self.out.exists())
        self.assertFalse(list(self.base.glob('.inventory-scan-*')))

    def test_scan_failure_after_receipt_is_cleaned(self):
        spec = importlib.util.spec_from_file_location('inventory_test', TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = module.scan.main
        def failed(argv):
            target = argv[argv.index('--json-out') + 1]
            module.scan.write_receipt(target, {'failed': True})
            return 1
        module.scan.main = failed
        try:
            with self.assertRaises(module.Refused):
                module.scan_receipt(self.store, self.base)
        finally:
            module.scan.main = original
        self.assertFalse(list(self.base.glob('.inventory-scan-*')))

    def test_decision_and_recommendation_guards(self):
        text = DOC.read_text()
        self.assertTrue(decisions_valid(text))
        self.assertFalse(decisions_valid(text + '\nDecision: retain 7 years\n'))
        recs = [s for s in text.splitlines() if s.startswith('Recommendation')]
        self.assertEqual(len(recs), 4)
        self.assertTrue(all(s.startswith('Recommendation (operator, not a decision):') for s in recs))
        self.assertIsNone(re.search(r'violat|unlawful|illegal|non-?complian|breach', text, re.I))

    def test_static_no_network_or_mutation_except_receipt_cleanup(self):
        tree = ast.parse(TOOL.read_text())
        imports = {alias.name.split('.')[0] for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
        self.assertFalse(imports & {'subprocess', 'socket', 'requests', 'urllib', 'http'})
        calls = [n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        self.assertFalse(set(calls) & {'chmod', 'rename', 'replace', 'remove', 'rmdir', 'upload'})
        self.assertEqual(calls.count('unlink'), 1)


if __name__ == '__main__':
    unittest.main()
