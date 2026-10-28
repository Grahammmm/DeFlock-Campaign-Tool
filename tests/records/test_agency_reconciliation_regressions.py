"""Synthetic regressions for parent digest coverage and host file modes."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.agency_reconciliation import canonical, reconcile, run


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def card(name, role='agency_original', status='unassigned', parents=(), hints=()):
    return {'sha256': digest(name), 'role': role, 'agency_status': status,
            'parents': [digest(p) for p in parents], 'agency_hints': list(hints)}


def record(name, agency, line=1):
    return {'sha256': digest(name), 'agency': agency,
            'object_sha256': digest('synthetic digest object'), 'line': line}


def target(result, name='child'):
    def walk(value):
        if isinstance(value, dict):
            if value.get('sha256') == digest(name) and 'exact_hash_candidates' in value:
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)
    items = list(walk(result))
    if len(items) != 1:
        raise AssertionError('Expected exactly one target item')
    return items[0]


class ParentDigestRegressionTests(unittest.TestCase):
    def cards(self, **parent_options):
        return [card('child', parents=['parent']),
                card('parent', role='project_artifact', status='hint_present', **parent_options)]

    def test_parent_digest_retains_exact_locator_and_parent_identity(self):
        result = reconcile(self.cards(), [record('parent', 'Example Police', 7)])
        candidate = target(result)['parent_digest_candidates'][0]
        self.assertEqual(candidate['parent_sha256'], digest('parent'))
        self.assertEqual(candidate['source'], {'object_sha256': digest('synthetic digest object'), 'line': 7})
        self.assertEqual(candidate['agency'], 'Example Police')
        self.assertEqual(result['summary']['either_source_items'], 1)
        self.assertEqual(result['summary']['no_evidence_items'], 0)
        self.assertFalse(target(result)['verified'])

    def test_parent_digest_conflict_is_not_hidden(self):
        result = reconcile(self.cards(), [record('child', 'Example A'), record('parent', 'Example B', 2)])
        self.assertEqual(result['summary']['conflicting_items'], 1)

    def test_no_transitive_digest_inheritance(self):
        cards = self.cards(parents=['grandparent']) + [card('grandparent', role='project_artifact', status='hint_present')]
        result = reconcile(cards, [record('grandparent', 'Example Police')])
        self.assertEqual(target(result)['parent_digest_candidates'], [])
        self.assertEqual(result['summary']['no_evidence_items'], 1)

    def test_missing_parent_is_not_accepted_from_digest_alone(self):
        result = reconcile([card('child', parents=['parent'])], [record('parent', 'Example Police')])
        self.assertEqual(target(result)['parent_digest_candidates'], [])
        self.assertEqual(result['summary']['no_evidence_items'], 1)

    def test_excluded_parent_is_not_inherited(self):
        result = reconcile([card('child', parents=['parent']), card('parent', status='scope_excluded')],
                           [record('parent', 'Example Police')])
        self.assertEqual(target(result)['parent_digest_candidates'], [])
        self.assertEqual(result['summary']['no_evidence_items'], 1)

    def test_parent_placeholders_are_not_attributions(self):
        for placeholder in ('', 'Unknown', ' UNASSIGNED '):
            with self.subTest(placeholder=placeholder):
                result = reconcile(self.cards(), [record('parent', placeholder)])
                self.assertEqual(target(result)['parent_digest_candidates'], [])
                self.assertEqual(result['summary']['no_evidence_items'], 1)

    def test_aliases_reconcile_parent_and_exact_labels(self):
        result = reconcile(self.cards(), [record('child', 'Example PD'), record('parent', 'Example Police', 2)],
                           {'Example PD': 'Example Police'})
        self.assertEqual(result['summary']['conflicting_items'], 0)
        self.assertEqual(result['summary']['candidate_only_items'], 1)

    def test_repeated_parent_reference_does_not_duplicate_source_rows(self):
        cards = [card('child', parents=['parent', 'parent']), card('parent', role='project_artifact', status='hint_present')]
        result = reconcile(cards, [record('parent', 'Example Police', 3), record('parent', 'Example Police', 8)])
        entries = target(result)['parent_digest_candidates']
        self.assertEqual(len(entries), 2)
        self.assertEqual(sorted(e['source']['line'] for e in entries), [3, 8])


class HostModeRegressionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.snapshot = self.root / 'snapshot'
        (self.snapshot / 'objects').mkdir(parents=True)
        self.output = self.root / 'output'
        manifest = {'schema_version': 1, 'inputs': []}
        catalog = {'schema_version': 1, 'snapshot_id': hashlib.sha256(canonical(manifest).encode()).hexdigest(),
                   'cards': [card('child')]}
        hashes = {}
        for name, data in [('catalog.json', catalog), ('input-manifest.json', manifest)]:
            raw = json.dumps(data).encode()
            (self.snapshot / name).write_bytes(raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        (self.snapshot / 'artifact-hashes.json').write_text(json.dumps(hashes))

    def test_permissive_creation_modes_corrected_before_output_write(self):
        original_open = Path.open
        original_os_open = os.open
        testcase = self
        writes = []

        class CheckedWriter:
            def __init__(self, file, name):
                self.file, self.name = file, name
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self.file.__exit__(*args)
            def __getattr__(self, name):
                return getattr(self.file, name)
            def write(self, data):
                testcase.assertEqual(os.fstat(self.file.fileno()).st_mode & 0o777, 0o600)
                writes.append(self.name)
                return self.file.write(data)

        def permissive_open(path, mode='r', *args, **kwargs):
            file = original_open(path, mode, *args, **kwargs)
            if mode == 'xb' and path.name in ('candidates.json', 'receipt.json'):
                os.fchmod(file.fileno(), 0o660)
                return CheckedWriter(file, path.name)
            return file

        def permissive_lock(path, flags, *args, **kwargs):
            existed = os.path.exists(path)
            fd = original_os_open(path, flags, *args, **kwargs)
            if os.path.basename(path) == 'writer.lock' and not existed:
                os.fchmod(fd, 0o660)
            return fd

        with patch.object(Path, 'open', permissive_open), patch.object(os, 'open', permissive_lock):
            first = run(self.snapshot, self.output)
        self.assertEqual(sorted(writes), ['candidates.json', 'receipt.json'])
        for name in ('candidates.json', 'receipt.json'):
            self.assertEqual((Path(first['output']) / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.output / 'writer.lock').stat().st_mode & 0o777, 0o600)
        self.assertTrue(run(self.snapshot, self.output)['reused'])

    def test_existing_insecure_lock_rejected_without_mutation(self):
        self.output.mkdir(mode=0o700)
        lock = self.output / 'writer.lock'
        lock.write_bytes(b'existing lock marker')
        lock.chmod(0o660)
        with self.assertRaises(ValueError):
            run(self.snapshot, self.output)
        self.assertEqual(lock.read_bytes(), b'existing lock marker')
        self.assertEqual(lock.stat().st_mode & 0o777, 0o660)


if __name__ == '__main__':
    unittest.main()
