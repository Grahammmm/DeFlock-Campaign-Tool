"""H1 regressions using the real intake CLI and synthetic schema-producing inputs."""
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from email.message import EmailMessage

REPO = Path(__file__).resolve().parents[2]
INTAKE_SCRIPT = REPO / 'campaign_tool/records/intake/folder.py'
QUARANTINE_SCRIPT = INTAKE_SCRIPT.with_name('scope_quarantine.py')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def archive(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, raw in members.items():
            z.writestr(zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0)), raw)
    return buf.getvalue()


class ScopeQuarantineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / 'originals'
        self.root.mkdir()
        self.out = self.base / 'out'
        self.originals = {}

    def add(self, name, raw):
        path = self.root / name
        path.write_bytes(raw)
        self.originals[path] = raw
        return path

    def cli(self, script, *args):
        proc = subprocess.run([sys.executable, '-B', str(script), *map(str, args)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def intake(self):
        self.cli(INTAKE_SCRIPT, 'run', '--root', self.root, '--output', self.out)

    def quarantine(self, *paths):
        result = self.cli(QUARANTINE_SCRIPT, '--output', self.out, *paths)
        for path, raw in self.originals.items():
            self.assertEqual(path.read_bytes(), raw, 'original changed: ' + str(path))
        return json.loads(result.stdout.splitlines()[-1])

    def rows(self, sql, args=()):
        with sqlite3.connect(self.out / 'intake.sqlite') as db:
            return db.execute(sql, args).fetchall()

    def snapshot(self, raw):
        ident = sha(raw)
        derived = self.out / 'derived' / ident
        return {
            'blob': (self.out / 'blobs' / ident).read_bytes(),
            'derived': {str(p.relative_to(derived)): p.read_bytes()
                        for p in derived.rglob('*') if p.is_file()},
            'docs': self.rows('SELECT * FROM docs WHERE sha=?', (ident,)),
            'units': self.rows('SELECT * FROM units WHERE sha=? ORDER BY ordinal', (ident,)),
            'preservations': self.rows('SELECT * FROM preservations WHERE sha=?', (ident,)),
        }

    def assert_retained(self, raw, before):
        ident = sha(raw)
        self.assertTrue((self.out / 'blobs' / ident).is_file(), 'retained blob missing: ' + ident)
        self.assertTrue((self.out / 'derived' / ident).is_dir(), 'retained derived directory missing: ' + ident)
        self.assertEqual(self.snapshot(raw), before)
        self.assertEqual(self.rows('SELECT * FROM scope_exclusions WHERE sha=?', (ident,)), [])
        self.assertFalse((self.out / 'quarantine' / ident).exists())

    def assert_quarantined(self, raw):
        ident = sha(raw)
        self.assertEqual(self.rows('SELECT stage,digest FROM docs WHERE sha=?', (ident,)),
                         [('out_of_scope', None)])
        self.assertEqual(self.rows('SELECT * FROM units WHERE sha=?', (ident,)), [])
        self.assertEqual(self.rows('SELECT * FROM preservations WHERE sha=?', (ident,)), [])
        self.assertTrue(self.rows('SELECT * FROM scope_exclusions WHERE sha=?', (ident,)))
        self.assertFalse((self.out / 'blobs' / ident).exists())
        self.assertFalse((self.out / 'derived' / ident).exists())
        self.assertEqual((self.out / 'quarantine' / ident / 'blob').read_bytes(), raw)
        self.assertTrue((self.out / 'quarantine' / ident / 'derived').is_dir())

    def shared_fixture(self, shared=b'synthetic shared logo'):
        personal_note = b'synthetic personal note'
        contract = b'synthetic agency contract'
        a = archive({'logo.txt': shared, 'note.txt': personal_note})
        b = archive({'logo.txt': shared, 'contract.txt': contract})
        path_a = self.add('personal.zip', a)
        path_b = self.add('records.zip', b)
        self.intake()
        return path_a, path_b, a, b, shared, personal_note, contract

    def test_shared_child_and_unrelated_units_survive_replay_and_inventory(self):
        for shared in (b'synthetic shared logo', b''):
            with self.subTest(shared=shared):
                # Each case needs a fresh intake, not a previous successful receipt.
                if self.originals:
                    self.out = self.base / 'out-empty'
                    self.root = self.base / 'originals-empty'
                    self.root.mkdir()
                    self.originals = {}
                pa, pb, a, b, child, note, contract = self.shared_fixture(shared)
                before = {raw: self.snapshot(raw) for raw in (b, child, contract)}
                result = self.quarantine(pa)
                self.assertEqual(result['quarantined_hashes'], 2)
                self.assert_quarantined(a)
                self.assert_quarantined(note)
                for raw, snap in before.items():
                    self.assert_retained(raw, snap)
                self.assertEqual(self.rows('SELECT parent FROM edges WHERE child=?', (sha(child),)),
                                 [(sha(b),)])
                self.quarantine(pa)
                self.intake()
                for raw, snap in before.items():
                    self.assert_retained(raw, snap)
                queue = [json.loads(line)['sha256'] for line in
                         (self.out / 'all-agency-document-queue.jsonl').read_text().splitlines()]
                self.assertIn(sha(b), queue)
                self.assertNotIn(sha(a), queue)

    def test_all_named_parents_release_shared_child_in_either_order(self):
        pa, pb, a, b, child, note, contract = self.shared_fixture()
        result = self.quarantine(pb, pa)
        self.assertEqual(result['quarantined_hashes'], 5)
        for raw in (a, b, child, note, contract):
            self.assert_quarantined(raw)

    def test_sequential_quarantine_releases_last_parent_shared_child(self):
        pa, pb, a, b, child, note, contract = self.shared_fixture()
        self.quarantine(pa)
        self.assertTrue((self.out / 'blobs' / sha(child)).is_file())
        self.quarantine(pb)
        for raw in (a, b, child, note, contract):
            self.assert_quarantined(raw)

    def test_exclusive_nested_descendants_quarantine_but_shared_subtree_survives(self):
        leaf = b'synthetic nested leaf'
        nested = archive({'leaf.txt': leaf})
        a = archive({'inner.zip': nested, 'exclusive.txt': b'only personal'})
        b = archive({'inner.zip': nested, 'contract.txt': b'only agency'})
        pa = self.add('personal.zip', a)
        pb = self.add('records.zip', b)
        self.intake()
        before = {raw: self.snapshot(raw) for raw in (b, nested, leaf)}
        self.quarantine(pa)
        self.assert_quarantined(a)
        self.assert_quarantined(b'only personal')
        for raw, snap in before.items():
            self.assert_retained(raw, snap)
        self.quarantine(pb)
        for raw in (b, nested, leaf, b'only agency'):
            self.assert_quarantined(raw)

    def test_named_leaf_adds_direct_container_not_unrelated_shared_sibling_parent(self):
        pa, pb, a, b, child, note, contract = self.shared_fixture()
        named = self.add('named-note.txt', note)
        self.intake()
        before = {raw: self.snapshot(raw) for raw in (b, child, contract)}
        self.quarantine(named)
        self.assert_quarantined(a)
        self.assert_quarantined(note)
        for raw, snap in before.items():
            self.assert_retained(raw, snap)

    def test_nested_direct_container_with_outside_parent_is_not_moved_or_hash_excluded(self):
        leaf = b'synthetic named leaf'
        nested = archive({'leaf.txt': leaf, 'sibling.txt': b'synthetic sibling'})
        outer = archive({'nested.zip': nested})
        self.add('outer.zip', outer)
        named = self.add('named-leaf.txt', leaf)
        self.intake()
        before = {raw: self.snapshot(raw) for raw in (outer, nested, leaf, b'synthetic sibling')}
        edges = self.rows('SELECT * FROM edges ORDER BY parent,locator')
        result = self.quarantine(named)
        self.assertEqual(result['quarantined_hashes'], 0)
        for raw, snap in before.items():
            self.assert_retained(raw, snap)
        self.assertEqual(self.rows('SELECT * FROM edges ORDER BY parent,locator'), edges)
        self.assertEqual(self.rows('SELECT sha FROM scope_exclusions WHERE path=?', (str(named),)), [('',)])
        self.assertEqual(json.loads((self.out / 'scope-excluded-hashes.json').read_text()), [])
        self.quarantine(named)
        self.intake()
        self.assert_retained(leaf, before[leaf])

    def test_eml_direct_container_and_exclusive_zip_descendants(self):
        leaf = b'synthetic private attachment'
        nested = archive({'leaf.txt': leaf})
        msg = EmailMessage()
        msg['From'] = 'sender@example.invalid'
        msg['Subject'] = 'synthetic container'
        msg.set_content('synthetic body')
        msg.add_attachment(nested, maintype='application', subtype='zip', filename='nested.zip')
        mail = msg.as_bytes()
        self.add('mail.eml', mail)
        named = self.add('named-archive.zip', nested)
        self.intake()
        result = self.quarantine(named)
        self.assertEqual(result['quarantined_hashes'], 3)
        for raw in (mail, nested, leaf):
            self.assert_quarantined(raw)

    def test_empty_unknown_and_invalid_paths_do_not_spread(self):
        raw = b'synthetic retained record'
        path = self.add('records.txt', raw)
        self.intake()
        before = self.snapshot(raw)
        self.assertEqual(self.quarantine(self.root / 'missing.txt')['quarantined_hashes'], 0)
        self.assertEqual(self.rows('SELECT path,sha FROM scope_exclusions'), [(str(self.root / 'missing.txt'), '')])
        proc = subprocess.run([sys.executable, '-B', str(QUARANTINE_SCRIPT), '--output', str(self.out),
                               str(path), 'relative.txt'], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('exact absolute source paths required', proc.stderr)
        self.assert_retained(raw, before)
        self.assertEqual(self.rows('SELECT path,sha FROM scope_exclusions'), [(str(self.root / 'missing.txt'), '')])


if __name__ == '__main__':
    unittest.main()
