"""Actual tiny email/TXT composition; no inherited tests or always-pass authority."""
import importlib
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


class PipelineConnectionTests(unittest.TestCase):
    def test_pending_acceptance_replay_and_subject_guards(self):
        if not os.environ.get('RECORDS_COMPOSITION_MANIFEST'):
            self.skipTest('explicit pinned composition manifest required')
        import tests.records
        manifest = json.loads(Path(os.environ['RECORDS_COMPOSITION_MANIFEST']).read_text())
        helper_root = str(Path(manifest['roots']['wp5']) / 'tests/records')
        if helper_root not in tests.records.__path__:
            tests.records.__path__.append(helper_root)
        fixture = importlib.import_module('tests.records.test_pipeline_composition')
        original_compose = fixture.PipelineCompositionTests.compose
        extraction_reports, catalog_reports, guard_reports = [], [], []

        def compose(test):
            from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
            from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter
            from campaign_tool.records.extraction_validation import ExtractionStageAdapter
            from campaign_tool.records.catalog_stage import CatalogStage
            from campaign_tool.records.runner.pipeline import PipelineConnection, PreparedItem
            from campaign_tool.records.runner.contracts import IntegrationGap
            captured = {}
            old_preserve = CanonicalMailBackend.preserve
            old_enroll = ExtractionLedgerAdapter.enroll
            old_accept = ExtractionStageAdapter.accept
            old_process = CatalogStage.process

            def preserve(obj, *args, **kwargs):
                value = old_preserve(obj, *args, **kwargs)
                captured.update(backend=obj, preserved=value)
                return value

            def enroll(obj, **kwargs):
                result = old_enroll(obj, **kwargs)
                captured.update(enrollment=obj, enroll_kwargs=dict(kwargs), subject=result['original_sha256'])
                return result

            def plan(artifact=None, expected=None, author='synthetic-author'):
                k = captured['enroll_kwargs']
                return PreparedItem(captured['subject'], Path(k['original_path']), Path(k['receipt_path']),
                    k['receipt_sha256'], Path(artifact) if artifact else None, expected, author)

            def connection(item, catalog=None):
                return PipelineConnection(backend=captured['backend'], enrollment=captured['enrollment'],
                    extraction=captured['extraction'], catalog=catalog, items=(item,))

            def accept(obj, *args, **kwargs):
                captured['extraction'] = obj
                if captured.get('inside_extract') or extraction_reports:
                    result = old_accept(obj, *args, **kwargs)
                    captured['last_accept'] = result
                    return result
                c = connection(plan())
                captured['inside_extract'] = True
                try:
                    first = c.advance(captured['preserved'])
                    first_result = captured['last_accept']
                    replay = c.advance(captured['preserved'])
                    extraction_reports.extend((first, replay))
                finally:
                    captured['inside_extract'] = False
                return first_result

            def process(obj, artifact, expected_sha256, **kwargs):
                if captured.get('inside_catalog') or catalog_reports:
                    result = old_process(obj, artifact, expected_sha256, **kwargs)
                    captured['last_catalog'] = result
                    return result
                item = plan(artifact, expected_sha256, kwargs['author_id'])
                c = connection(item, obj)
                captured['inside_catalog'] = True
                try:
                    first = c.advance(captured['preserved'])
                    first_result = captured['last_catalog']
                    replay = c.advance(captured['preserved'])
                    catalog_reports.extend((first, replay))
                    # A wrong subject with broken support must NOT reach process,
                    # whose error path could otherwise invalidate that subject.
                    card = json.loads(Path(artifact).read_bytes())
                    other = next(s for s in captured['preserved'].documents if s != item.subject_sha256)
                    for label, content in (
                            ('wrong-subject', {**card, 'subject_sha256': other, 'supports': None}),
                            ('invalid-schema', {**card, 'schema': 'not-a-card'})):
                        import hashlib
                        path = Path(artifact).with_name(label + '.json')
                        raw = json.dumps(content).encode();path.write_bytes(raw);path.chmod(0o600)
                        bad = replace(item, catalog_artifact=path, catalog_sha256=hashlib.sha256(raw).hexdigest())
                        with patch.object(obj, 'process', side_effect=AssertionError('must not call catalog')) as blocked:
                            report = connection(bad, obj).advance(captured['preserved'])
                        self.assertEqual(blocked.call_count, 0)
                        self.assertEqual(report['after'], replay['after'])
                        self.assertEqual(sum(i['status'] == 'blocked' for i in report['items']), 1)
                        guard_reports.append(label)
                    bad = replace(item, catalog_sha256='0' * 64)
                    with patch.object(obj, 'process', side_effect=AssertionError('must not call catalog')) as blocked:
                        report = connection(bad, obj).advance(captured['preserved'])
                    self.assertEqual(blocked.call_count, 0)
                    self.assertEqual(report['after'], replay['after'])
                    guard_reports.append('changed-card-bytes')
                    bad = replace(item, extraction_receipt_sha256='0' * 64)
                    report = connection(bad, obj).advance(captured['preserved'])
                    self.assertEqual(report['after'], replay['after'])
                    self.assertEqual(sum(i['status'] == 'blocked' for i in report['items']), 1)
                    self.assertEqual(sum(i['status'] == 'pending' for i in report['items']), 1)
                    guard_reports.append('extraction-failure-isolated')
                    with patch.object(captured['extraction'].runner, 'database', Path(artifact).with_name('other.sqlite')):
                        with self.assertRaisesRegex(IntegrationGap, 'canonical_database_mismatch'):
                            connection(item, obj)
                    guard_reports.append('database-binding')
                    with patch.object(captured['extraction'].runner, 'validators', {}):
                        with self.assertRaisesRegex(IntegrationGap, 'installed_extract_binding_required'):
                            connection(item, obj)
                    guard_reports.append('authority-binding')
                finally:
                    captured['inside_catalog'] = False
                return first_result

            with patch.object(CanonicalMailBackend, 'preserve', preserve), \
                 patch.object(ExtractionLedgerAdapter, 'enroll', enroll), \
                 patch.object(ExtractionStageAdapter, 'accept', accept), \
                 patch.object(CatalogStage, 'process', process):
                return original_compose(test)

        with patch.object(fixture.PipelineCompositionTests, 'compose', compose):
            result = unittest.TestResult()
            unittest.TestSuite([fixture.PipelineCompositionTests('test_catalog_handoff_acceptance')]).run(result)
        self.maxDiff = None
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.assertEqual(result.skipped, [])
        self.assertEqual(len(extraction_reports), 2)
        self.assertEqual(len(catalog_reports), 2)
        initial, extract_replay = extraction_reports
        self.assertEqual(initial['before']['done'], dict(preserve=2, extract=0, catalog=0, detect=0, review=0, compare=0, privacy=0))
        self.assertEqual(initial['after']['done'], dict(preserve=2, extract=1, catalog=0, detect=0, review=0, compare=0, privacy=0))
        self.assertEqual(initial['after'], extract_replay['after'])
        first, replay = catalog_reports
        self.assertEqual(first['before']['done']['catalog'], 0)
        self.assertEqual(first['after']['done'], dict(preserve=2, extract=1, catalog=1, detect=0, review=0, compare=0, privacy=0))
        self.assertEqual(first['after'], replay['after'])
        self.assertFalse(first['release_ready'])
        self.assertFalse(first['publication_ready'])
        self.assertFalse(first['fresh_mailbox_coverage'])
        self.assertEqual(first['accepted_receipt_count'], 4)
        self.assertEqual(sum(i['status'] == 'pending' for i in first['items']), 1)
        self.assertEqual(sum(i['status'] == 'done' for i in first['items']), 1)
        self.assertNotEqual(first['extract_run_id'], first['catalog_run_id'])
        self.assertEqual(len(guard_reports), 6)
