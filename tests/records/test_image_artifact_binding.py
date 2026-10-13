"""Synthetic artifact integrity; decoding is never image acceptance or review."""
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from campaign_tool.records import extraction_routes as routes
from campaign_tool.records import extraction_validation as validation
from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter, ExtractionBindingError
from campaign_tool.records.ledger import store


class ImageArtifactBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def image(self, form, *, frames=1):
        try:
            version = importlib.metadata.version('Pillow')
        except importlib.metadata.PackageNotFoundError:
            self.skipTest('real image decoding requires the pinned optional Pillow distribution')
        # A present distribution with a broken native decoder must fail the test,
        # rather than silently presenting an unavailable optional dependency.
        from PIL import Image
        output = io.BytesIO()
        images = [Image.new('RGB', (3, 2), color=(i * 50, 0, 0)) for i in range(frames)]
        try:
            images[0].save(output, format=form.upper(), save_all=frames > 1,
                           append_images=images[1:])
        finally:
            for image in images:
                image.close()
        source = self.root / ('synthetic.' + form)
        source.write_bytes(output.getvalue())
        source.chmod(0o400)
        return source, hashlib.sha256(output.getvalue()).hexdigest(), version

    def extracted(self, form, *, frames=1, bounded=False):
        source, subject, version = self.image(form, frames=frames)
        output = self.root / 'output'
        output.mkdir(mode=0o700)
        if bounded:
            result = routes.extract(source, subject, output, form=form, timeout=20)
            run = Path(result['run_path'])
        else:
            # Decoder/writer test on all platforms. Full process/resource isolation
            # is exercised separately in the supported Linux integration test.
            run = output
            derived = run / 'derived'
            derived.mkdir(mode=0o700)
            routes._child(source, subject, form, derived, run)
            metadata, units, pages = routes._read_output(derived)
            result = dict(status=metadata['stage'], parser=metadata['parser'],
                          parser_version=metadata['parser_version'], units=units, pages=pages,
                          review_status='not_reviewed', route=routes.route(form))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['parser'], 'pillow')
        self.assertEqual(result['parser_version'], version)
        self.assertEqual(result['review_status'], 'not_reviewed')
        self.assertEqual(result['route'], routes.route(form))
        self.assertEqual(result['route']['owner'], 'visual-review')
        self.assertTrue(result['route']['review_required'])
        metadata = json.loads((run / 'derived' / 'digest.json').read_bytes())
        units_raw = (run / 'derived' / 'units.jsonl').read_bytes()
        self.assertEqual(metadata['artifacts'], {'units.jsonl': hashlib.sha256(units_raw).hexdigest()})
        self.assertEqual([json.loads(line) for line in units_raw.splitlines()], result['units'])
        self.assertEqual(metadata['counts']['units'], frames)
        self.assertEqual([unit['locator']['frame'] for unit in result['units']], list(range(1, frames + 1)))
        self.assertTrue(all(unit['data']['visual_review'] == 'not_done' for unit in result['units']))
        return source, subject, result, run

    def test_png_binds_real_decoded_units_and_retains_visual_hold(self):
        self.extracted('png')

    def test_multiframe_gif_binds_every_decoded_frame(self):
        self.extracted('gif', frames=2)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'full extraction process isolation requires supported Linux runtime')
    def test_enrollment_replays_but_installed_evidence_gate_refuses_image(self):
        source, subject, result, run = self.extracted('png', bounded=True)
        database = self.root / 'ledger' / 'canonical.sqlite'
        store.initialize(database)
        # Unaccepted synthetic intake declarations only; no preservation/review authority.
        with store.ledger(database) as con:
            con.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                        ('intake', 'fixture', store.now(), None, 'fixture', None, None, None, 'captured', '{}'))
            con.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (subject, source.stat().st_size, 'image/png', None, 'original', 'unresolved',
                         str(source), 'receipt_validation_pending', None, '{}'))
            con.execute('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)',
                        ('image-source', subject, 'local', 'synthetic-image', None, None, 'fixture', '{}'))
            for stage in store.STAGES:
                con.execute('INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)',
                            (subject, stage, 'pending', None, 'fixture', store.now(), 'intake', None))
            con.commit()
        adapter = ExtractionLedgerAdapter(database=database, evidence_root=self.root / 'evidence')
        receipt = run / 'extraction.json'
        arguments = dict(original_path=source, receipt_path=receipt,
                         receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest())
        enrolled = adapter.enroll(**arguments)
        self.assertEqual(enrolled['stage_promotions'], 0)
        self.assertEqual(enrolled['extract_acceptance'], 'pending_trusted_validator')
        self.assertTrue(adapter.enroll(**arguments)['reused'])
        validator = validation._InstalledExtractionValidator(database, adapter.evidence_root, self.root)
        with store.ledger(database, readonly=True) as con:
            before = list(con.iterdump())
        # This is the fixed first evidence check used by ExtractionStageAdapter.accept,
        # without constructing a run/profile/authority or fabricating prerequisites.
        for _ in range(2):
            with self.assertRaisesRegex(ExtractionBindingError, '^unsupported_complete_parser$'):
                validator.evidence(enrolled['import_id'])
        with store.ledger(database, readonly=True) as con:
            self.assertEqual(list(con.iterdump()), before)
            self.assertTrue(all(row['status'] == 'pending' and row['receipt_sha256'] is None
                                for row in con.execute('SELECT * FROM stage_state')))
        self.assertEqual(source.read_bytes(), (run / 'input').read_bytes())
        self.assertEqual(result['units'][0]['data']['visual_review'], 'not_done')

    def test_msg_writer_metadata_unchanged_with_decoder_stub(self):
        # Scope: common writer only; this does not establish native MSG decoding.
        source = self.root / 'synthetic.msg'
        source.write_bytes(b'synthetic decoder input')
        subject = hashlib.sha256(source.read_bytes()).hexdigest()
        derived = self.root / 'derived'
        derived.mkdir(mode=0o700)
        units = [{'kind': 'text_line', 'locator': {'line': 1}, 'text': 'Synthetic body', 'data': {}}]
        with patch.object(routes, '_msg', return_value=(units, 'extract-msg', 'synthetic-decoder', [])):
            routes._child(source, subject, 'msg', derived, self.root)
        metadata = json.loads((derived / 'digest.json').read_bytes())
        self.assertEqual(metadata, {
            'stage': 'complete', 'counts': {'units': 1}, 'issues': [], 'children': [],
            'parser': 'legacy-intake:msg', 'parser_version': 'synthetic-decoder',
            'parser_components': {'intake': routes.folder.VERSION + '/' + routes.folder.ENGINE_REVISION,
                                  'extract-msg': 'synthetic-decoder', 'olefile': routes._version('olefile')},
            'artifacts': {'units.jsonl': hashlib.sha256((derived / 'units.jsonl').read_bytes()).hexdigest()}})


if __name__ == '__main__':
    unittest.main()
