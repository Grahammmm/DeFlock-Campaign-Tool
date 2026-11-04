"""Synthetic redaction workflow; verifier patched only in tests, never a live signature."""
import argparse
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('_test_redact', ROOT / 'tools/flock_redact.py')
spec.loader.exec_module(redact := importlib.util.module_from_spec(spec))
spec = importlib.util.spec_from_file_location('_test_redact_scan', ROOT / 'tools/flock_privacy_scan.py')
spec.loader.exec_module(scan := importlib.util.module_from_spec(spec))
VALUES = ('synthetic@example.invalid', '202-555-0187', '7ZZZ321')
SIGNATURE = '-----BEGIN SSH SIGNATURE-----\nsynthetic\n-----END SSH SIGNATURE-----\n'


class RedactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        (self.store / 'originals').mkdir(mode=0o700)
        self.source = self.store / 'originals' / 'original.txt'
        self.raw = ('\n'.join(VALUES) + '\n').encode()
        self.source.write_bytes(self.raw)
        self.source.chmod(0o600)
        self.sha = redact.digest(self.raw)
        self.receipt = self.store / 'scan.json'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan.main([str(self.source), '--json-out', str(self.receipt)]), 3)
        self.folder = redact.record.record_dir(self.store, 'synthetic', self.sha)
        self.target = self.source.with_name(self.sha + '.redacted.txt')
        self.signature = self.store / 'signature'
        self.signature.write_text(SIGNATURE)
        self.signature.chmod(0o600)
        self.verify = mock.patch.object(redact.approval, 'verify', return_value='synthetic-reviewer').start()
        self.addCleanup(mock.patch.stopall)
        self.logs = []

    def run_command(self, command, *extra, expected=0):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = redact.main([command, str(self.store), 'synthetic', self.sha, str(self.source), *map(str, extra)])
        text = output.getvalue()
        self.logs.append(text)
        self.assertEqual(code, expected, text)
        for value in VALUES:
            self.assertNotIn(value, text)
        return text

    def propose(self):
        self.run_command('propose', '--scan-receipt', self.receipt)

    def sign(self):
        self.run_command('signoff', '--signature', self.signature)

    def signed(self):
        self.propose()
        self.sign()

    def unchanged(self):
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertEqual(redact.digest(self.source.read_bytes()), self.sha)

    def snapshot(self):
        return {str(p.relative_to(self.store)): p.read_bytes() for p in self.store.rglob('*') if p.is_file()}

    def no_apply(self):
        self.assertFalse(self.target.exists())
        self.assertFalse((self.folder / 'ELIGIBILITY.json').exists())
        self.assertEqual(list(self.store.glob('.redact-*')), [])

    def test_propose_categories_locators_no_values(self):
        self.propose()
        rec = json.loads((self.folder / 'PROPOSAL.json').read_bytes())
        self.assertEqual(rec['document_id'], self.sha)
        self.assertEqual(rec['source_sha256'], self.sha)
        self.assertEqual(rec['scan_receipt'], redact.digest(self.receipt.read_bytes()))
        self.assertTrue(rec['redaction_required'])
        self.assertEqual(rec['proposed'], [dict(category=kind, locator=dict(line=i, offset=0, length=len(value)))
                         for i, (kind, value) in enumerate(zip(('email', 'phone', 'plate'), VALUES), 1)])
        for value in VALUES:
            self.assertNotIn(value.encode(), (self.folder / 'PROPOSAL.json').read_bytes())
        self.assertEqual({p.name for p in self.folder.iterdir()}, {'PROPOSAL.json'})
        self.unchanged()

    def test_payload_exact_readonly(self):
        self.propose()
        before = self.snapshot()
        text = self.run_command('payload')
        raw = (self.folder / 'PROPOSAL.json').read_bytes()
        self.assertEqual(text.encode(), redact.approval.payload('flock-redaction', self.sha, raw.decode()))
        self.assertIn('sha256: ' + redact.digest(raw), text)
        self.assertEqual(self.snapshot(), before)
        self.unchanged()

    def test_refused_signature_writes_nothing(self):
        self.propose()
        before = self.snapshot()
        self.verify.side_effect = redact.approval.ApprovalError('synthetic secret error')
        self.assertEqual(self.run_command('signoff', '--signature', self.signature, expected=1),
                         'redact: refused signer_untrusted\n')
        self.assertEqual(self.snapshot(), before)
        self.unchanged()

    def test_signoff_verified_principal_duplicate_immutable(self):
        self.signed()
        rec = json.loads((self.folder / 'SIGNOFF.json').read_bytes())
        self.assertEqual(rec['signed_by'], 'synthetic-reviewer')
        self.verify.assert_called_with(redact.payload((self.folder / 'PROPOSAL.json').read_bytes(), self.sha), SIGNATURE)
        before = self.snapshot()
        self.assertIn('already_exists', self.run_command('signoff', '--signature', self.signature, expected=1))
        self.assertEqual(self.snapshot(), before)
        self.unchanged()

    def test_apply_unsigned(self):
        self.propose()
        before = self.snapshot()
        self.assertIn('unsigned', self.run_command('apply', expected=1))
        self.assertEqual(self.snapshot(), before)
        self.no_apply()
        self.unchanged()

    def test_apply_changed_source(self):
        self.signed()
        self.source.write_bytes(self.raw + b'x')
        before = self.snapshot()
        self.assertIn('source_mismatch', self.run_command('apply', expected=1))
        self.assertEqual(self.snapshot(), before)
        self.no_apply()

    def test_apply_success_status_and_private_modes(self):
        self.signed()
        self.assertIn(': signed\n', self.run_command('status'))
        self.run_command('apply')
        self.assertIn(': applied\n', self.run_command('status'))
        self.assertEqual(redact.record.eligibility(self.store, 'synthetic', self.sha, self.sha), ('eligible', 'ok'))
        self.assertEqual(self.target.read_text(), '[email:25 bytes removed]\n[phone:12 bytes removed]\n[plate:7 bytes removed]\n')
        for path in (self.target, *self.folder.iterdir()):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for path in (self.folder, *self.folder.parents):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            if path == self.store:
                break
        for path in self.store.rglob('*'):
            self.assertFalse(path.is_symlink())
            if path.is_file() and path not in (self.source,):
                for value in VALUES:
                    self.assertNotIn(value.encode(), path.read_bytes())
        for value in VALUES:
            self.assertNotIn(hashlib.sha256(value.encode()).hexdigest(), self.target.read_text())
        self.unchanged()

    def test_status_stale_source(self):
        self.signed()
        self.run_command('apply')
        self.source.write_bytes(self.raw + b'x')
        before = self.snapshot()
        self.assertIn(': stale source_mismatch\n', self.run_command('status', expected=1))
        self.assertEqual(self.snapshot(), before)

    def test_status_none_and_proposed_readonly(self):
        before = self.snapshot()
        self.assertIn(': none\n', self.run_command('status'))
        self.assertEqual(self.snapshot(), before)
        self.propose()
        before = self.snapshot()
        self.assertIn(': proposed\n', self.run_command('status'))
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_proposal_and_apply(self):
        self.signed()
        before = self.snapshot()
        self.run_command('propose', '--scan-receipt', self.receipt, expected=1)
        self.assertEqual(before, self.snapshot())
        self.run_command('apply')
        before = self.snapshot()
        self.assertIn('already_exists', self.run_command('apply', expected=1))
        self.assertEqual(before, self.snapshot())

    def test_signature_file_refusals(self):
        self.propose()
        cases = [('empty', b'', 'signature_invalid'), ('large', b'x' * (redact.approval.MAX_SIGNATURE + 1), 'signature_oversized'),
                 ('encoding', b'\xff', 'signature_encoding')]
        for name, raw, reason in cases:
            self.signature.write_bytes(raw)
            before = self.snapshot()
            self.assertIn(reason, self.run_command('signoff', '--signature', self.signature, expected=1))
            self.assertEqual(before, self.snapshot())
        self.signature.unlink()
        self.signature.symlink_to(self.source)
        self.assertIn('signature_link', self.run_command('signoff', '--signature', self.signature, expected=1))
        self.signature.unlink()
        os.mkfifo(self.signature)
        self.assertIn('signature_special', self.run_command('signoff', '--signature', self.signature, expected=1))
        self.verify.assert_not_called()
        self.assertFalse((self.folder / 'SIGNOFF.json').exists())
        self.unchanged()

    def test_signature_for_other_proposal(self):
        self.propose()
        expected = redact.payload((self.folder / 'PROPOSAL.json').read_bytes() + b' ', self.sha)
        def verify(data, signature):
            if data != expected:
                raise redact.approval.ApprovalError('signature mismatch')
            return 'synthetic-reviewer'
        self.verify.side_effect = verify
        self.run_command('signoff', '--signature', self.signature, expected=1)
        self.assertFalse((self.folder / 'SIGNOFF.json').exists())

    def test_apply_reverifies_signature_and_proposal(self):
        self.signed()
        self.verify.side_effect = redact.approval.ApprovalError('revoked')
        before = self.snapshot()
        self.assertIn('signer_untrusted', self.run_command('apply', expected=1))
        self.assertEqual(before, self.snapshot())
        self.no_apply()
        self.verify.side_effect = None
        prop = json.loads((self.folder / 'PROPOSAL.json').read_bytes())
        prop['proposed'] = []
        (self.folder / 'PROPOSAL.json').write_bytes(redact.encode(prop))
        self.assertIn('proposal_mismatch', self.run_command('apply', expected=1))
        self.no_apply()

    def test_staging_eligibility_required_before_live_links(self):
        self.signed()
        real = redact.record.eligibility
        calls = []
        def check(store, slug, sha, current):
            calls.append(Path(store))
            if Path(store) != self.store:
                self.no_live_outputs()
                result = real(store, slug, sha, current)
                self.assertEqual(result, ('eligible', 'ok'))
                return 'ineligible', 'derivative_mismatch'
            return real(store, slug, sha, current)
        before = self.snapshot()
        with mock.patch.object(redact.record, 'eligibility', side_effect=check):
            self.assertIn('derivative_mismatch', self.run_command('apply', expected=1))
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], self.store)
        self.assertEqual(self.snapshot(), before)
        self.no_apply()

    def no_live_outputs(self):
        self.assertFalse(self.target.exists())
        self.assertFalse((self.folder / 'ELIGIBILITY.json').exists())

    def test_injected_write_and_promotion_failures_leave_live_untouched(self):
        self.signed()
        before = self.snapshot()
        write = redact.write_new
        link = os.link
        for failure in ('derivative', 'eligibility', 'promotion'):
            def fail_write(path, raw):
                if ((failure == 'derivative' and path.name == 'derivative.txt') or
                        (failure == 'eligibility' and path.name == 'ELIGIBILITY.json')):
                    raise OSError('injected')
                return write(path, raw)
            def fail_link(src, dst):
                if failure == 'promotion' and Path(dst).name == 'ELIGIBILITY.json':
                    raise OSError('injected')
                return link(src, dst)
            with self.subTest(failure=failure), mock.patch.object(redact, 'write_new', side_effect=fail_write), \
                    mock.patch.object(redact.os, 'link', side_effect=fail_link):
                self.run_command('apply', expected=1)
            self.assertEqual(self.snapshot(), before)
            self.no_apply()
            self.unchanged()

    def test_final_reader_failure_rolls_back(self):
        self.signed()
        real = redact.record.eligibility
        def check(store, *args):
            return ('ineligible', 'proposal_mismatch') if Path(store) == self.store else real(store, *args)
        before = self.snapshot()
        with mock.patch.object(redact.record, 'eligibility', side_effect=check):
            self.run_command('apply', expected=1)
        self.assertEqual(before, self.snapshot())
        self.no_apply()

    def test_existing_target_or_record_never_overwritten(self):
        self.signed()
        for path in (self.target, self.folder / 'ELIGIBILITY.json'):
            path.write_bytes(b'held')
            before = self.snapshot()
            self.assertIn('already_exists', self.run_command('apply', expected=1))
            self.assertEqual(before, self.snapshot())
            path.unlink()
        self.target.symlink_to(self.source)
        self.run_command('apply', expected=1)
        self.assertTrue(self.target.is_symlink())
        self.unchanged()

    def test_span_boundaries_multibyte_crossline_overlap(self):
        for raw in (b'a@example.invalid', b'a@example.invalid b@example.invalid',
                    b'jdoe [at]\nslo [dot] gov', 'é a@example.invalid'.encode(),
                    b'202-555-0187@example.invalid'):
            with self.subTest(raw=raw):
                spans = redact.spans(raw)
                rendered = redact.derivative(raw, spans)
                self.assertTrue(spans)
                self.assertEqual(redact.spans(rendered), [])
                for item in spans:
                    loc = item['locator']
                    starts = [0] + [i + 1 for i, v in enumerate(raw) if v == 10]
                    start = starts[loc['line'] - 1] + loc['offset']
                    self.assertNotIn(raw[start:start + loc['length']], rendered)
        spans = redact.spans(b'a@example.invalid b@example.invalid')
        self.assertEqual(len(spans), 2)
        self.assertEqual([s['locator']['offset'] for s in spans], [0, 18])
        self.assertEqual(redact.spans('é a@example.invalid'.encode())[0]['locator']['offset'], 3)
        overlap = redact.spans(b'202-555-0187@example.invalid')
        self.assertEqual({x['category'] for x in overlap}, {'email', 'phone'})
        self.assertEqual(redact.derivative(b'202-555-0187@example.invalid', overlap),
                         b'[phone:12 bytes removed][email:28 bytes removed]')

    def test_blind_spots_documented_and_not_proposed(self):
        self.assertEqual(redact.spans(b'2025550187 jdoe at slo dot gov'), [])
        docs = (ROOT / 'docs/REDACTION.md').read_text()
        for text in ('Bare digit runs', 'spelled-out email', 'not human privacy'):
            self.assertIn(text, docs)

    def test_scan_receipt_missing_stale_narrow_allowed(self):
        original = json.loads(self.receipt.read_bytes())
        mutations = [[], None, dict(original, totals=[]), dict(original, files=[None]),
                     dict(original, schema='other'), dict(original, allow_file_used=True),
                     dict(original, categories=['email']), dict(original, files=[]),
                     dict(original, files=[dict(original['files'][0], sha256='c' * 64)])]
        for value in mutations:
            self.receipt.write_bytes(redact.encode(value))
            self.assertIn('scan_receipt_mismatch', self.run_command('propose', '--scan-receipt', self.receipt, expected=1))
            self.assertFalse(self.folder.exists())
        self.unchanged()

    def test_path_and_mode_refusals(self):
        self.propose()
        self.folder.chmod(0o755)
        self.run_command('payload', expected=1)
        self.folder.chmod(0o700)
        moved = self.store / 'moved'
        self.source.rename(moved)
        self.source.symlink_to(moved)
        self.run_command('apply', expected=1)
        self.source.unlink()
        moved.rename(self.source)
        self.source.write_bytes(b'\xff')
        self.run_command('propose', '--scan-receipt', self.receipt, expected=1)
        self.no_apply()

    def test_source_drift_during_staging_leaves_no_outputs(self):
        self.signed()
        real = redact.record.eligibility
        def drift(store, *args):
            result = real(store, *args)
            self.source.write_bytes(self.raw + b'x')
            return result
        with mock.patch.object(redact.record, 'eligibility', side_effect=drift):
            self.assertIn('source_mismatch', self.run_command('apply', expected=1))
        self.no_apply()

    def test_source_drift_after_promotion_rolls_back(self):
        self.signed()
        real = os.link
        def drift(src, dst):
            real(src, dst)
            if Path(dst).name == 'ELIGIBILITY.json':
                self.source.write_bytes(self.raw + b'x')
        with mock.patch.object(redact.os, 'link', side_effect=drift):
            self.assertIn('source_mismatch', self.run_command('apply', expected=1))
        self.no_apply()

    def test_partial_staged_write_removed(self):
        self.signed()
        before = self.snapshot()
        def fail(fd):
            raise OSError('injected flush failure')
        with mock.patch.object(redact.os, 'fsync', side_effect=fail):
            self.run_command('apply', expected=1)
        self.assertEqual(before, self.snapshot())
        self.no_apply()
        self.unchanged()

    def test_missing_applied_derivative_is_stale_not_signed(self):
        self.signed()
        self.run_command('apply')
        self.target.unlink()
        self.assertIn(': stale derivative_missing\n', self.run_command('status', expected=1))

    def test_no_match_proposal_needs_human_review_and_no_derivative(self):
        self.source.write_bytes(b'ordinary synthetic text')
        self.sha = redact.digest(self.source.read_bytes())
        self.folder = redact.record.record_dir(self.store, 'synthetic', self.sha)
        self.receipt.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan.main([str(self.source), '--json-out', str(self.receipt)]), 0)
        self.propose()
        rec = json.loads((self.folder / 'PROPOSAL.json').read_bytes())
        self.assertEqual(rec['proposed'], [])
        self.assertFalse(rec['redaction_required'])
        self.assertEqual(redact.record.eligibility(self.store, 'synthetic', self.sha, self.sha), ('eligible', 'not_required'))
        self.assertIn(': proposed\n', self.run_command('status'))
        self.sign()
        self.assertIn('not_required', self.run_command('apply', expected=1))
        self.assertFalse((self.folder / 'ELIGIBILITY.json').exists())

    def test_parser_full_option_sets_no_approval_shortcut(self):
        ap = redact.parser()
        sub = next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction))
        for name, child in sub.choices.items():
            options = {o for a in child._actions for o in a.option_strings}
            self.assertEqual(options, {'-h', '--help'} | ({'--signature'} if name == 'signoff' else
                                                        {'--scan-receipt'} if name == 'propose' else set()))
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as exc:
            ap.parse_args(['signoff', str(self.store), 'synthetic', self.sha, str(self.source)])
        self.assertEqual(exc.exception.code, 2)
        text = (ROOT / 'tools/flock_redact.py').read_text()
        for option in ('--sign', '--yes', '--force', '--auto-approve', '--signer', '--as'):
            self.assertNotIn(repr(option), text)
        tree = ast.parse(text)
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertFalse(imports & {'subprocess', 'socket', 'requests', 'urllib', 'http'})
        self.assertFalse({n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} & {'getenv', 'environ'})


class PublisherIntegrationTest(unittest.TestCase):
    def test_real_publisher_eligible_then_source_mismatch(self):
        from tests.flock.test_redaction_record import RedactionRecordTest
        case = RedactionRecordTest()
        case.setUp()
        try:
            # Reuse the real publisher's cited synthetic facts, not pre-made records.
            for path in case.folder.iterdir():
                path.unlink()
            case.folder.rmdir()
            receipt = case.store / 'scan.json'
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
                scanned = Path(tmp) / 'scan.json'
                self.assertEqual(scan.main([str(case.source), '--json-out', str(scanned)]), 3)
                redact.write_new(receipt, scanned.read_bytes())
            signature = case.store / 'signature'
            signature.write_text(SIGNATURE)
            common = [str(case.store), 'synthetic-county-sheriff', case.sha, str(case.source)]
            from tests.flock.test_publish import AGENCY
            common[1] = AGENCY
            for command, extra in [('propose', ['--scan-receipt', str(receipt)]),
                                   ('signoff', ['--signature', str(signature)]), ('apply', [])]:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = redact.main([command, *common, *extra])
                self.assertEqual(code, 0, command + ': ' + output.getvalue())
                for value in VALUES:
                    self.assertNotIn(value, output.getvalue())
            case.export()
            (case.rundir / 'PUBLISH.json').unlink()
            case.source.write_bytes(case.source.read_bytes() + b'x')
            case.export('source_mismatch')
        finally:
            case.doCleanups()


def acceptance_fixture(output):
    """Persistent synthetic, value-free outputs for the exact acceptance privacy scan."""
    output = Path(output)
    output.mkdir(mode=0o700)
    case = RedactTest()
    case.setUp()
    try:
        case.signed()
        case.run_command('apply')
        status = case.run_command('status')
        for path in case.folder.iterdir():
            redact.write_new(output / path.name, path.read_bytes())
        redact.write_new(output / 'derivative.txt', case.target.read_bytes())
        redact.write_new(output / 'status.txt', status.encode())
        redact.write_new(output / 'scan.json', case.receipt.read_bytes())
        print('synthetic fixture: PROPOSAL.json SIGNOFF.json ELIGIBILITY.json derivative.txt status.txt scan.json')
    finally:
        case.doCleanups()


if __name__ == '__main__':
    if sys.argv[1:2] == ['--acceptance-fixture']:
        acceptance_fixture(sys.argv[2])
    else:
        unittest.main()
