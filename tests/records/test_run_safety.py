"""Synthetic unattended safety probes: no network, private config or record corpus."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, create_autospec, patch
from zoneinfo import ZoneInfo

from campaign_tool.records import run, schedule, unattended as unattended_module
from campaign_tool.records.intake import mail_delta
from campaign_tool.records.ledger import store
from campaign_tool.records.run_safety import RunSafety, RunSafetyPolicy, digest, fixed_code
from campaign_tool.records.unattended import GuardedIMAPIntake, UnattendedPipeline
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from tests.records.test_imap_intake import FakeIMAP
from tests.records.test_run_slice import synthetic_email


def original(number):
    return hashlib.sha256(str(number).encode()).hexdigest()


class SafetyFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = run.Root(Path(self.temp.name) / 'root')
        self.root.prepare()

    def guard(self, **kwargs):
        return RunSafety(self.root.ledger, **kwargs)

    def success(self, guard, number, phase='intake'):
        self.assertTrue(guard.begin(phase, original(number)))
        guard.finish()

class SafetyTests(SafetyFixture, unittest.TestCase):
    def test_policy_rejects_bool_zero_negative_and_over_cap(self):
        for value in (True, False, 0, -1, 201, '200', 200.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RunSafetyPolicy(max_originals=value)
            with self.subTest(fetch=value), self.assertRaises(ValueError):
                RunSafetyPolicy(max_fetches=value)
        self.assertEqual(RunSafetyPolicy().max_originals, 200)

    def test_first_fault_stops_and_persists(self):
        guard = self.guard()
        self.assertTrue(guard.begin('intake', original(1)))
        guard.finish('fault', 'imap_fetch_failed')
        self.assertEqual(guard.report()['stop_code'], 'failure_rate_exceeded')
        self.assertFalse(self.guard().begin('advance', original(2)))

    def test_exact_ten_percent_does_not_stop_more_than_ten_does(self):
        guard = self.guard()
        for i in range(9):
            self.success(guard, i)
        guard.begin('intake', original(10))
        guard.finish('fault', 'imap_fetch_failed')
        self.assertFalse(guard.stopped)
        guard.begin('intake', original(11))
        guard.finish('fault', 'imap_fetch_failed')
        self.assertTrue(guard.stopped)

    def test_three_distinct_same_codes_stop_but_replay_does_not_inflate(self):
        guard = self.guard()
        for n in range(30):
            guard.begin('advance', original(n))
            if n in (9, 19, 29):
                guard.finish('fault', 'stage_failed', (original(n),))
            else:
                guard.finish()
        self.assertEqual(guard.report()['stop_code'], 'repeated_failure_code')

    def test_repeated_same_original_and_phase_separation(self):
        guard = self.guard()
        for n in range(30):
            guard.begin('advance', original(n))
            guard.finish('fault', 'stage_failed', (original(99),)) if n in (9, 19, 29) else guard.finish()
        self.assertFalse(guard.stopped)
        guard.begin('intake', original(100))
        guard.finish('fault', 'imap_fetch_failed')
        self.assertTrue(guard.stopped)
        self.assertEqual(len(guard.state['repeats']['advance:stage_failed']), 1)

    def test_same_code_counter_persists_between_runs(self):
        for run_number in range(3):
            guard = self.guard()
            for n in range(9):
                self.success(guard, run_number * 100 + n, 'advance')
            guard.begin('advance', original(run_number * 100 + 99))
            guard.finish('fault', 'stage_failed', (original(run_number),))
        self.assertEqual(guard.report()['stop_code'], 'repeated_failure_code')

    def test_fetch_and_original_budgets_independent_global_200(self):
        guard = self.guard()
        for phase in ('intake', 'advance'):
            for n in range(200):
                self.success(guard, n, phase)
            self.assertFalse(guard.begin(phase, original(201)))
        self.assertFalse(guard.stopped)

    def test_interrupted_active_attempt_creates_hold(self):
        guard = self.guard()
        guard.begin('advance', original(1))
        recovered = self.guard()
        self.assertEqual(recovered.report()['stop_code'], 'interrupted_attempt')
        self.assertFalse(recovered.begin('intake', original(2)))

    def test_monotonic_budget(self):
        now = [5.0]
        guard = self.guard(clock=lambda: now[0], policy=RunSafetyPolicy(seconds=3))
        now[0] = 8.0
        self.assertFalse(guard.begin('advance', original(1)))
        self.assertEqual(guard.report()['stop_code'], 'time_budget_exceeded')

    def test_privacy_fault_hard_stop_even_below_rate(self):
        guard = self.guard()
        for n in range(20):
            self.success(guard, n, 'advance')
        guard.begin('advance', original(21))
        guard.finish('fault', 'privacy_redaction_fault', (original(21),))
        self.assertEqual(guard.report()['stop_code'], 'privacy_redaction_fault')

    def test_unsupported_is_hold_not_technical_fault(self):
        guard = self.guard()
        guard.begin('intake', original(1))
        guard.finish('held', 'unsupported_rfc822_part', (original(1),))
        self.assertEqual(guard.phases['intake']['failed'], 0)
        self.assertEqual(guard.phases['intake']['held'], 1)
        self.assertTrue(guard.stopped)

    def test_reviewed_pr50_mime_codes_are_projected_without_relaxing_trust(self):
        # Exact reviewed PR50 producer literals; not derived from MAIL_CODES.
        codes = (
            "invalid_mime_content_type",
            "invalid_related_container",
            "ambiguous_related_content_id",
            "invalid_related_content_id",
            "invalid_related_start",
            "missing_related_root",
            "related_root_type_mismatch",
            "invalid_multipart_disposition",
            "unsupported_attached_multipart_part",
            "invalid_mime_container",
            "invalid_leaf_disposition",
        )
        self.assertEqual(len(codes), 11)
        for code in codes:
            with self.subTest(code=code):
                self.assertEqual(fixed_code(mail_delta.Rejected(code)), code)
                self.assertEqual(fixed_code(RuntimeError(code)), 'fetch_or_preserve_failed')
        class Untrusted(mail_delta.Rejected):
            def __str__(self):
                raise AssertionError('untrusted formatter must not execute')
        self.assertEqual(fixed_code(Untrusted(codes[0])), 'fetch_or_preserve_failed')
        for args in ((), ('SYNTHETIC_PRIVATE_MARKER',), (codes[0], 'SYNTHETIC_PRIVATE_MARKER'), (None,)):
            self.assertEqual(fixed_code(mail_delta.Rejected(*args)), 'fetch_or_preserve_failed')

    def test_only_exact_trusted_exception_codes_are_exposed(self):
        class Untrusted(mail_delta.Rejected):
            pass
        self.assertEqual(fixed_code(mail_delta.Rejected('export_scope_mismatch')), 'export_scope_mismatch')
        self.assertEqual(fixed_code(mail_delta.Rejected('unsupported_rfc822_part')), 'unsupported_rfc822_part')
        for error in (RuntimeError('SYNTHETIC_EXCEPTION_MARKER'), mail_delta.Rejected('SYNTHETIC_EXCEPTION_MARKER'), Untrusted('export_scope_mismatch')):
            self.assertEqual(fixed_code(error), 'fetch_or_preserve_failed')

    def test_corrupted_state_is_fail_closed(self):
        with store.ledger(self.root.ledger) as con:
            con.execute('INSERT INTO ledger_meta(key,value) VALUES(?,?)', ('unattended-safety-v1', '{'))
            con.commit()
        self.assertEqual(self.guard().report()['stop_code'], 'invalid_safety_state')

    def test_empty_explicit_subject_list_does_not_expand(self):
        pipeline = run.Pipeline(self.root.path)
        with patch.object(pipeline, 'subjects', side_effect=AssertionError('expanded empty batch')):
            self.assertEqual(pipeline.advance_all([]), [])

    def test_health_status_mapping_fails_closed(self):
        zone = ZoneInfo('America/Los_Angeles')
        slot = datetime(2026, 1, 2, 7, tzinfo=zone)
        for status, expected in {'completed': 'ok', 'completed_with_gaps': 'gaps', 'partial': 'gaps',
                                 'held': 'held', 'failed': 'failed', 'interrupted': 'failed', 'unknown': 'failed'}.items():
            rows = [{'run_id': 'synthetic-run', 'started_at': (slot + timedelta(minutes=1)).isoformat(),
                     'ended_at': (slot + timedelta(minutes=2)).isoformat(), 'status': status}]
            self.assertEqual(schedule.classify_slot(slot, rows, slot + timedelta(minutes=5))[0], expected)

    def test_render_is_supervised_unattended_but_never_activated(self):
        paths = schedule.render(self.root.path, engine_root='/opt/example/engine', python='/usr/bin/python3')
        text = Path(paths[schedule.SERVICE_NAME]).read_text()
        self.assertIn('--unattended --max-originals-per-run 200', text)
        self.assertIn('TimeoutStartSec=2760', text)
        self.assertIn('TimeoutStopSec=60', text)


class GuardedIntakeTests(SafetyFixture, unittest.TestCase):
    def intake(self, folders, limit=200, fail_on=None):
        guard = self.guard(policy=RunSafetyPolicy(max_fetches=limit))
        fake = FakeIMAP(folders, fail_on=fail_on)
        config = {'account_id': 'synthetic-account', 'folders': list(folders)}
        backend = create_autospec(CanonicalMailBackend, instance=True)
        engine = GuardedIMAPIntake(config=config, ledger=self.root.ledger, mail_root=self.root.sub('mail'),
                                    backend=backend, alert=Mock(), client_factory=lambda _: fake,
                                    safety=guard, preserve_raw=lambda raw, identity: hashlib.sha256(raw).hexdigest(),
                                    supported_receipt=lambda _: ())
        with patch('campaign_tool.records.unattended.eml_export.export_message', return_value=Path('synthetic-receipt')):
            report = engine.run()
        self.assertEqual(backend.preserve.call_count, report['preserved'])
        for call in backend.preserve.call_args_list:
            self.assertEqual(call.args[1], config['account_id'])
            self.assertIn(call.args[2].name, config['folders'])
            self.assertGreater(call.args[2].uidvalidity, 0)
            self.assertGreater(call.args[3], 0)
        return report, engine, guard

    def test_limit_global_across_folders_checkpoint_deferred_edge(self):
        raw = b'synthetic mail bytes'
        report, engine, guard = self.intake({
            'INBOX': {'uidvalidity': 1, 'messages': {i: raw for i in range(1, 151)}},
            'Other': {'uidvalidity': 2, 'messages': {i: raw for i in range(1, 101)}},
            'Visible': {'uidvalidity': 3, 'messages': {1: raw}},
        })
        self.assertEqual(report['attempted'], 200)
        self.assertEqual(report['deferred'], 51)
        self.assertTrue(report['limit_reached'])
        self.assertEqual(engine._checkpoint('INBOX')['highest_uid'], 150)
        self.assertEqual(engine._checkpoint('Other')['highest_uid'], 50)
        self.assertEqual(engine._checkpoint('Visible')['highest_uid'], 0)
        self.assertEqual(len(report['folders']), 3)

    def test_failure_charged_no_checkpoint_skip_other_folder_visible(self):
        raw = b'synthetic mail bytes'
        report, engine, guard = self.intake({
            'INBOX': {'uidvalidity': 1, 'messages': {1: raw, 2: raw}},
            'Other': {'uidvalidity': 2, 'messages': {1: raw}},
        }, fail_on={('INBOX', 1)})
        self.assertEqual(report['attempted'], 1)
        self.assertEqual(report['deferred'], 2)
        self.assertEqual(engine._checkpoint('INBOX')['highest_uid'], 0)
        self.assertEqual(len(report['folders']), 2)
        self.assertEqual(report['stop_code'], 'failure_rate_exceeded')
        self.assertNotIn('OSError', json.dumps(report))

    def test_reviewed_mime_codes_reach_guard_report_and_hold_checkpoint(self):
        codes = (
            'invalid_mime_content_type', 'invalid_related_container', 'ambiguous_related_content_id',
            'invalid_related_content_id', 'invalid_related_start', 'missing_related_root',
            'related_root_type_mismatch', 'invalid_multipart_disposition',
            'unsupported_attached_multipart_part', 'invalid_mime_container', 'invalid_leaf_disposition',
        )
        for index, code in enumerate(codes):
            with self.subTest(code=code):
                root = run.Root(Path(self.temp.name) / ('mime-' + str(index))).prepare()
                guard = RunSafety(root.ledger)
                fake = FakeIMAP({'INBOX': {'uidvalidity': 1, 'messages': {1: b'synthetic rejected bytes'}}})
                engine = GuardedIMAPIntake(config={'account_id': 'synthetic', 'folders': ['INBOX']},
                                          ledger=root.ledger, mail_root=root.sub('mail'),
                                          backend=create_autospec(CanonicalMailBackend, instance=True), alert=Mock(),
                                          client_factory=lambda _: fake, safety=guard,
                                          preserve_raw=lambda raw, identity: hashlib.sha256(raw).hexdigest(),
                                          supported_receipt=lambda _: ())
                with patch('campaign_tool.records.unattended.eml_export.export_message', side_effect=mail_delta.Rejected(code)):
                    report = engine.run()
                self.assertEqual(report['failure_codes'], [code])
                self.assertEqual(report['folders'][0]['failed'], code)
                self.assertEqual(report['attempted'], 1)
                self.assertEqual(engine._checkpoint('INBOX')['highest_uid'], 0)
                self.assertEqual(guard.phases['intake']['failed'], 1)
                engine.backend.preserve.assert_not_called()


class PipelineSafetyTests(SafetyFixture, unittest.TestCase):
    def inbox(self, attachment_name='policy.txt'):
        path = Path(self.temp.name) / 'inbox'
        path.mkdir(mode=0o700)
        (path / 'one.eml').write_bytes(synthetic_email(filename=attachment_name))
        os.chmod(path / 'one.eml', 0o600)
        return path

    def test_empty_run_has_terminal_record_and_zero_attempts(self):
        pipeline = UnattendedPipeline(self.root.path)
        report = pipeline.run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['exit_code'], 0)
        self.assertEqual(report['safety']['phases']['advance']['attempted'], 0)
        with store.ledger(self.root.ledger, readonly=True) as con:
            row = con.execute('SELECT status,ended_at FROM runs WHERE run_id=?', (pipeline.run_id,)).fetchone()
        self.assertEqual(row['status'], 'completed')
        self.assertTrue(row['ended_at'])

    def test_model_configuration_holds_before_intake(self):
        pipeline = UnattendedPipeline(self.root.path, model=Mock(model_id='synthetic-model', privacy_tier='strict_local'))
        with patch.object(pipeline, 'ingest_inbox', side_effect=AssertionError('intake should not start')):
            report = pipeline.run(inbox='/nonexistent/synthetic')
        self.assertEqual(report['safety']['stop_code'], 'model_configuration_forbidden')
        self.assertEqual(report['exit_code'], 2)

    def test_nominal_stage_exception_is_safe_failed_and_nonzero(self):
        pipeline = run.Pipeline(self.root.path)
        with patch.object(pipeline, 'stage_extract', side_effect=RuntimeError('SYNTHETIC_PRIVATE_MARKER')):
            report = pipeline.run(self.inbox())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['exit_code'], 2)
        self.assertNotIn('SYNTHETIC_PRIVATE_MARKER', json.dumps(report))

    def test_clean_pipeline_and_bounded_backlog(self):
        pipeline = UnattendedPipeline(self.root.path, policy=RunSafetyPolicy(max_originals=1))
        report = pipeline.run(self.inbox())
        self.assertEqual(report['safety']['phases']['advance']['attempted'], 1)
        self.assertEqual(report['originals_deferred'], 1)
        self.assertFalse(report['end_to_end_complete'])

    def test_unknown_attachment_retained_and_not_advanced(self):
        pipeline = UnattendedPipeline(self.root.path)
        report = pipeline.run(self.inbox('unknown.xyz'))
        self.assertEqual(report['status'], 'held')
        self.assertEqual(report['safety']['stop_code'], 'decoder_needed')
        self.assertEqual(report['safety']['phases']['intake']['failed'], 0)
        self.assertEqual(report['safety']['phases']['advance']['attempted'], 0)
        self.assertEqual(len(list((self.root.sub('mail') / 'retained').glob('*.eml'))), 1)
        with store.ledger(self.root.ledger, readonly=True) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM ledger_meta WHERE key LIKE 'unattended-input:%'").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM stage_state WHERE stage='extract' AND status='done'").fetchone()[0], 0)

    def test_authored_checkpoint_failure_disclosure_probe(self):
        # Approved repair: persistence failure is charged while the attempt remains active.
        guard = self.guard()
        fake = FakeIMAP({'INBOX': {'uidvalidity': 1, 'messages': {1: b'synthetic'}}})
        engine = GuardedIMAPIntake(config={'account_id': 'synthetic', 'folders': ['INBOX']},
                                    ledger=self.root.ledger, mail_root=self.root.sub('mail'), backend=Mock(), alert=Mock(),
                                    client_factory=lambda _: fake, safety=guard,
                                    preserve_raw=lambda raw, identity: hashlib.sha256(raw).hexdigest(), supported_receipt=lambda _: ())
        original_save = engine._save
        def fail_success(folder, validity, uid, success=True):
            if uid:
                raise OSError('SYNTHETIC_CHECKPOINT_FAILURE')
            return original_save(folder, validity, uid, success=success)
        with patch.object(engine, '_save', side_effect=fail_success), patch('campaign_tool.records.unattended.eml_export.export_message', return_value=Path('synthetic')):
            report = engine.run()
        self.assertEqual(guard.phases['intake']['failed'], 1)
        self.assertEqual(report['attempted'], 1)
        self.assertEqual(engine._checkpoint('INBOX')['highest_uid'], 0)
        self.assertEqual(report['stop_code'], 'failure_rate_exceeded')

    def test_deferred_intake_prevents_full_completion_claim(self):
        pipeline = UnattendedPipeline(self.root.path)
        with patch.object(pipeline, 'ingest_inbox', return_value={'messages': 1, 'preserved': [], 'replayed': [], 'failures': [], 'deferred': 1}):
            report = pipeline.run(inbox='/synthetic-not-read')
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['intake_deferred'], 1)
        self.assertEqual(report['end_to_end_complete'], 0)

    def test_provisioned_ocr_is_not_a_model_activation_hold(self):
        pipeline = UnattendedPipeline(self.root.path, ocr_tools={'synthetic': 'tools'}, ocr_tool_signature='synthetic')
        report = pipeline.run()
        self.assertEqual(report['status'], 'completed')
        self.assertIsNone(report['safety']['stop_code'])

    def test_image_route_delegates_only_with_provisioned_tools(self):
        for provisioned in (False, True):
            pipeline = UnattendedPipeline(self.root.path, ocr_tools={'synthetic': 'tools'} if provisioned else None)
            pipeline.safety = self.guard()
            states = {name: {'status': 'pending' if name == 'extract' else 'done', 'receipt_sha256': None} for name in run.STAGE_ORDER}
            with patch.object(pipeline, '_states', return_value=states), patch.object(pipeline, '_original', return_value={}), patch.object(pipeline, '_form', return_value='png'), patch.object(pipeline, 'stage_extract', return_value={'status': 'blocked', 'reason': 'visual_review_required'}) as extract:
                outcomes = pipeline.advance_all([original(70)])
            self.assertEqual(extract.call_count, int(provisioned))
            self.assertEqual(outcomes[0]['outcome'], 'review_required')
            self.assertFalse(pipeline.safety.stopped)

    def test_real_backend_replay_and_receipt_binding(self):
        inbox = self.inbox()
        pipeline = UnattendedPipeline(self.root.path)
        report = pipeline.run(inbox)
        self.assertEqual((report['status'], report['exit_code']), ('completed', 0))
        self.assertEqual(report['originals'], 2)
        self.assertEqual(report['safety']['phases']['advance']['attempted'], 2)
        receipt = self.root.sub('runs') / (pipeline.run_id + '.json')
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        with store.ledger(self.root.ledger, readonly=True) as con:
            row = con.execute('SELECT status,summary FROM runs WHERE run_id=?', (pipeline.run_id,)).fetchone()
            self.assertEqual(row['status'], 'completed')
            self.assertEqual(json.loads(row['summary'])['report_sha256'], hashlib.sha256(receipt.read_bytes()).hexdigest())
            self.assertEqual(con.execute('SELECT count(*) FROM mail_messages').fetchone()[0], 1)
        replay = UnattendedPipeline(self.root.path).run(inbox)
        self.assertEqual(replay['exit_code'], 0)
        self.assertEqual(len(replay['intake']['replayed']), 1)
        self.assertEqual(replay['safety']['phases']['intake']['attempted'], 1)
        self.assertEqual(replay['safety']['phases']['advance']['attempted'], 0)

    def test_receipt_failure_cannot_leave_completed_or_healthy_run(self):
        pipeline = UnattendedPipeline(self.root.path)
        with patch('campaign_tool.records.unattended._private_bytes', side_effect=OSError('SYNTHETIC_RECEIPT_MARKER')):
            report = pipeline.run()
        self.assertEqual((report['status'], report['exit_code']), ('failed', 2))
        self.assertEqual(report['failure_code'], 'report_write_failed')
        self.assertEqual(report['safety']['stop_code'], 'report_write_failed')
        self.assertNotIn('SYNTHETIC_RECEIPT_MARKER', json.dumps(report))
        with store.ledger(self.root.ledger, readonly=True) as con:
            row = con.execute('SELECT status,summary FROM runs WHERE run_id=?', (pipeline.run_id,)).fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertIsNone(json.loads(row['summary'])['report_sha256'])
        self.assertEqual(schedule.health(self.root.path)['exit_code'], 1)
        subsequent = UnattendedPipeline(self.root.path).run()
        self.assertEqual(subsequent['safety']['stop_code'], 'report_write_failed')
        self.assertEqual(subsequent['safety']['phases']['intake']['attempted'], 0)
        self.assertEqual(subsequent['safety']['phases']['advance']['attempted'], 0)

    def test_private_creation_is_exclusive_and_symlink_safe(self):
        path = self.root.sub('runs') / 'synthetic.json'
        unattended_module._private_bytes(path, b'{}')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            unattended_module._private_bytes(path, b'not overwritten')
        link = self.root.sub('runs') / 'synthetic-link.json'
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            unattended_module._private_bytes(link, b'not followed')
        self.assertEqual(path.read_bytes(), b'{}')

    def test_msg_preflight_requires_decoder_readiness(self):
        subject = original(900)
        with patch('campaign_tool.records.unattended.eml_export.attachment_forms', return_value={subject: 'msg'}):
            with patch('campaign_tool.records.unattended.importlib.import_module', side_effect=ModuleNotFoundError('SYNTHETIC_DECODER_MARKER')):
                self.assertEqual(UnattendedPipeline._unsupported(Path('synthetic')), (subject,))
            with patch('campaign_tool.records.unattended.importlib.import_module', return_value=object()) as decoder:
                self.assertEqual(UnattendedPipeline._unsupported(Path('synthetic')), ())
                decoder.assert_called_once_with('extract_msg')

    def test_missing_msg_decoder_preserves_input_without_uid_skip(self):
        config = Path(self.temp.name) / 'synthetic-config.json'
        config.write_text(json.dumps({'host': 'mail.example.invalid', 'username': 'synthetic-user',
                                      'account_id': 'synthetic-account', 'password': chr(120) * 4,
                                      'folders': ['INBOX']}))
        os.chmod(config, 0o600)
        raw = synthetic_email(filename='synthetic.msg')
        fake = FakeIMAP({'INBOX': {'uidvalidity': 1, 'messages': {1: raw}}})
        actual_import = unattended_module.importlib.import_module
        def imports(name, *args, **kwargs):
            if name == 'extract_msg':
                raise ModuleNotFoundError('synthetic decoder absence')
            return actual_import(name, *args, **kwargs)
        pipeline = UnattendedPipeline(self.root.path)
        with patch('campaign_tool.records.unattended.importlib.import_module', side_effect=imports):
            report = pipeline.run(mail_config=config, client_factory=lambda _: fake)
        self.assertEqual(report['status'], 'held')
        self.assertEqual(report['safety']['stop_code'], 'decoder_needed')
        with store.ledger(self.root.ledger, readonly=True) as con:
            self.assertEqual(con.execute('SELECT highest_uid FROM mail_checkpoints').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM mail_messages').fetchone()[0], 1)
        self.assertEqual(len(list((self.root.sub('mail') / 'retained').glob('*.eml'))), 1)
        self.assertEqual(report['safety']['phases']['advance']['attempted'], 0)

    def test_advance_wiring_caps_distinct_originals_at_200(self):
        pipeline = UnattendedPipeline(self.root.path)
        pipeline.safety = self.guard()
        states = {name: {'status': 'pending', 'receipt_sha256': None} for name in run.STAGE_ORDER}
        subjects = [original(n) for n in range(201)]
        with patch.object(pipeline, '_states', return_value=states), patch.object(pipeline, '_original', return_value={}), patch.object(pipeline, '_form', return_value='png'), patch.object(pipeline, 'stage_extract', side_effect=AssertionError('unprovisioned image must remain a review hold')):
            progress = pipeline.advance_all(subjects + subjects[:3])
        self.assertEqual(len(progress), 200)
        self.assertEqual(pipeline.originals_deferred, 1)
        self.assertEqual(pipeline.safety.phases['advance']['attempted'], 200)
        self.assertEqual(pipeline.safety.phases['advance']['review_required'], 200)
        self.assertFalse(pipeline.safety.stopped)
