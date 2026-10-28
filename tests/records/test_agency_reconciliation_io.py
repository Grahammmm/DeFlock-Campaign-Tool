"""Synthetic immutable-snapshot and private-output integration tests."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from campaign_tool.records.agency_reconciliation import canonical, run


class AgencyReconciliationIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / 'snapshot'
        (self.snapshot / 'objects').mkdir(parents=True)
        self.output = self.root / 'output'
        self.sha = hashlib.sha256(b'synthetic document').hexdigest()
        record = {'sha256': self.sha, 'agency': 'Example Police',
                  'key_points': 'SYNTHETIC_NARRATIVE_NOT_FOR_OUTPUT'}
        self.raw = (json.dumps(record) + '\n').encode()
        obj = hashlib.sha256(self.raw).hexdigest()
        self.object = self.snapshot / 'objects' / obj
        self.object.write_bytes(self.raw)
        manifest = {'schema_version': 1, 'inputs': [{'kind': 'document_digests',
                    'sha256': obj, 'object': 'objects/' + obj, 'bytes': len(self.raw)}]}
        sid = hashlib.sha256(canonical(manifest).encode()).hexdigest()
        catalog = {'schema_version': 1, 'snapshot_id': sid, 'cards': [
            {'sha256': self.sha, 'role': 'agency_original', 'agency_status': 'unassigned',
             'agency_hints': [], 'parents': []}]}
        artifacts = {}
        for name, value in [('catalog.json', catalog), ('input-manifest.json', manifest)]:
            data = json.dumps(value).encode()
            (self.snapshot / name).write_bytes(data)
            artifacts[name] = hashlib.sha256(data).hexdigest()
        (self.snapshot / 'artifact-hashes.json').write_text(json.dumps(artifacts))

    def test_private_idempotent_output_without_narrative(self):
        original = {str(p): p.read_bytes() for p in self.snapshot.rglob('*') if p.is_file()}
        first = run(self.snapshot, self.output)
        second = run(self.snapshot, self.output)
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertTrue(second['reused'])
        self.assertEqual(first['summary']['target_count'], 1)
        for p in self.output.rglob('*'):
            self.assertEqual(p.stat().st_mode & 0o077, 0)
            if p.is_file():
                self.assertNotIn(b'SYNTHETIC_NARRATIVE_NOT_FOR_OUTPUT', p.read_bytes())
        self.assertEqual(original, {str(p): p.read_bytes() for p in self.snapshot.rglob('*') if p.is_file()})

    def test_corrupt_digest_object_rejected(self):
        self.object.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            run(self.snapshot, self.output)
        self.assertFalse(self.output.exists())

    def test_corrupt_existing_output_rejected(self):
        result = run(self.snapshot, self.output)
        (Path(result['output']) / 'candidates.json').write_text('{}')
        with self.assertRaises(ValueError):
            run(self.snapshot, self.output)

    def test_output_overlap_rejected(self):
        with self.assertRaises(ValueError):
            run(self.snapshot, self.snapshot / 'output')

    def test_symlink_input_rejected(self):
        outside = self.root / 'outside'
        outside.write_bytes(self.raw)
        self.object.unlink()
        self.object.symlink_to(outside)
        with self.assertRaises(ValueError):
            run(self.snapshot, self.output)

    def test_world_readable_output_rejected(self):
        self.output.mkdir()
        self.output.chmod(0o755)
        with self.assertRaises(ValueError):
            run(self.snapshot, self.output)

    def test_explicit_aliases_bind_new_run(self):
        first = run(self.snapshot, self.output)
        aliases = self.root / 'aliases.json'
        aliases.write_text(json.dumps({'Example Police': 'Example Municipality'}))
        second = run(self.snapshot, self.output, aliases)
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertTrue(Path(first['output']).is_dir())
        self.assertEqual(second['summary']['candidate_only_items'], 1)

    def test_reuse_rejects_changed_private_modes(self):
        for part in ('directory', 'candidates.json', 'receipt.json', 'writer.lock'):
            with self.subTest(part=part):
                output = self.root / ('mode-' + part)
                result = run(self.snapshot, output)
                destination = Path(result['output'])
                target = destination if part == 'directory' else output / part if part == 'writer.lock' else destination / part
                target.chmod(0o755 if part == 'directory' else 0o644)
                with self.assertRaises(ValueError):
                    run(self.snapshot, output)


if __name__ == '__main__':
    unittest.main()
