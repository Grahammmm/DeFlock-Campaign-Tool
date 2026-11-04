"""Synthetic redaction contract and real publisher paths; no live approval or records."""
import ast
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.flock import test_publish as base

spec = importlib.util.spec_from_file_location('_redaction_publish_test', base.TOOLS / 'flock_publish.py')
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)
reader = publish.redaction
STAMP = '2026-10-30T00:00:00Z'
SIGNATURE = '-----BEGIN SSH SIGNATURE-----\nsynthetic\n-----END SSH SIGNATURE-----\n'
VALUES = ('synthetic@example.invalid', '202-555-0187', '7ZZZ321')


def digest(data):
    return hashlib.sha256(data).hexdigest()


class RedactionRecordTest(unittest.TestCase):
    def setUp(self):
        fixture = base.FlockPublishTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.store, self.rundir = fixture, fixture.store, fixture.run_dir
        self.source = self.store / 'original.txt'
        self.source.write_text('\n'.join(VALUES) + '\n')
        self.sha = digest(self.source.read_bytes())
        self.folder = reader.record_dir(self.store, base.AGENCY, self.sha)
        self.folder.mkdir(mode=0o700)
        self.derivative = self.store / 'sanitized.txt'
        self.derivative.write_bytes(b'[removed]\n')
        self.derivative.chmod(0o600)
        facts_path = self.rundir.parents[1] / 'FACTS.csv'
        facts = publish.compare.facts_tool
        rows = [r for _, r in facts.load(facts_path, base.AGENCY)[1]]
        mapping = {}
        for row in rows:
            old = row['fact_id']
            row['source_sha256'] = self.sha
            row['fact_id'] = facts.fact_id(row)
            mapping[old] = row['fact_id']
        facts.write_atomic(facts_path, rows)
        for name in ('FINDINGS.csv', 'VERIFY.csv', 'PUBLIC-OK.csv'):
            text = (self.rundir / name).read_text()
            for old, new in mapping.items():
                text = text.replace(old, new)
            (self.rundir / name).write_text(text)
        meta = json.loads((self.rundir / 'RUN.json').read_text())
        meta['inputs']['FACTS.csv'] = digest(facts_path.read_bytes())
        (self.rundir / 'RUN.json').write_text(json.dumps(meta))
        self.proposal = dict(schema_version=1, document_id='synthetic', source_sha256=self.sha,
            redaction_required=True, proposed=[dict(locator=dict(line=1, offset=0, length=25), category='email')],
            not_detected_note='', scan_receipt='c' * 64, created_at=STAMP)
        raw = self.record('PROPOSAL.json', self.proposal)
        self.signoff = dict(schema_version=1, scope='flock-redaction', document_id='synthetic', source_sha256=self.sha,
            proposal_sha256=digest(raw), signed_by='synthetic-reviewer', signed_at=STAMP, signature=SIGNATURE)
        self.record('SIGNOFF.json', self.signoff)
        self.applied = dict(schema_version=1, document_id='synthetic', source_sha256=self.sha,
            proposal_sha256=digest(raw), derivative_sha256=digest(self.derivative.read_bytes()),
            derivative_path=str(self.derivative), signed_by='synthetic-reviewer', signed_at=STAMP, applied_at=STAMP)
        self.record('ELIGIBILITY.json', self.applied)
        self.verify = mock.patch.object(reader.approval, 'verify', return_value='synthetic-reviewer').start()
        self.addCleanup(mock.patch.stopall)

    def record(self, name, value):
        raw = (json.dumps(value, sort_keys=True) + '\n').encode()
        path = self.folder / name
        path.write_bytes(raw)
        path.chmod(0o600)
        return raw

    def outcome(self):
        result = reader.eligibility(self.store, base.AGENCY, self.sha, reader.original_hash(self.store, [str(self.source)]))
        for value in VALUES:
            self.assertNotIn(value, str(result))
        return result

    def export(self, reason=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = publish.main(['export', str(self.rundir), '--site-slug', 'sample'])
        text = output.getvalue()
        path = self.rundir / 'PUBLISH.json'
        raw = path.read_bytes() if path.exists() else b''
        for value in VALUES:
            self.assertNotIn(value, text + raw.decode())
        if reason:
            self.assertEqual(code, 1)
            self.assertEqual(text, ''.join('finding %s: refused redaction:%s\n' % (fid, reason)
                for fid in ('N1', 'O1', 'A1', 'T1')) + 'exported=0 refused=4\n')
            self.assertFalse(path.exists())
        else:
            self.assertEqual(code, 0, text)
            self.assertEqual([f['review']['status'] for f in json.loads(raw)['findings']], ['pending'] * 4)
        return raw, text

    def test_no_record(self):
        shutil.rmtree(self.folder)
        self.assertEqual(self.outcome(), ('no_record', 'no_record'))

    def test_not_required(self):
        self.record('PROPOSAL.json', dict(self.proposal, redaction_required=False))
        (self.folder / 'SIGNOFF.json').unlink()
        self.assertEqual(self.outcome(), ('eligible', 'not_required'))
        self.export()
        self.verify.assert_not_called()

    def test_unsigned(self):
        (self.folder / 'SIGNOFF.json').unlink()
        self.assertEqual(self.outcome(), ('ineligible', 'unsigned'))
        self.export('unsigned')

    def test_signer_untrusted(self):
        self.verify.return_value = 'other-reviewer'
        self.assertEqual(self.outcome(), ('ineligible', 'signer_untrusted'))
        self.export('signer_untrusted')

    def test_proposal_mismatch(self):
        self.record('SIGNOFF.json', dict(self.signoff, proposal_sha256='d' * 64))
        self.assertEqual(self.outcome(), ('ineligible', 'proposal_mismatch'))

    def test_source_mismatch(self):
        self.source.write_bytes(self.source.read_bytes() + b'x')
        self.assertEqual(self.outcome(), ('ineligible', 'source_mismatch'))
        self.export('source_mismatch')

    def test_derivative_missing(self):
        self.derivative.unlink()
        self.assertEqual(self.outcome(), ('ineligible', 'derivative_missing'))

    def test_derivative_mismatch(self):
        self.derivative.write_bytes(b'changed')
        self.assertEqual(self.outcome(), ('ineligible', 'derivative_mismatch'))
        self.export('derivative_mismatch')

    def test_ok(self):
        self.assertEqual(self.outcome(), ('eligible', 'ok'))
        raw = (self.folder / 'PROPOSAL.json').read_text()
        self.verify.assert_called_once_with(reader.approval.payload('flock-redaction', 'synthetic', raw), SIGNATURE)
        self.export()

    def test_absent_directory_preserves_document_bytes(self):
        baseline, _ = self.export()
        (self.rundir / 'PUBLISH.json').unlink()
        shutil.rmtree(self.folder.parent)
        current, text = self.export()
        self.assertEqual(current, baseline)
        self.assertIn('review-required: source %s: redaction:no_record\n' % self.sha[:12], text)
        self.assertIn('exported=4 refused=0', text)

    def test_approval_error_and_unprotected_trust_file(self):
        for message in ('signature rejected', 'trust path is not root-owned and protected'):
            with self.subTest(case=message):
                self.verify.side_effect = reader.approval.ApprovalError(message)
                self.assertEqual(self.outcome(), ('ineligible', 'signer_untrusted'))
        # Exercise the real trust-file protection without reading a live trust file.
        self.verify.side_effect = None
        with mock.patch.object(reader.approval.os, 'lstat', return_value=type('Stat', (),
                {'st_uid': 123, 'st_mode': 0o100600})()):
            with self.assertRaises(reader.approval.ApprovalError):
                reader.approval._require_root(self.store / 'synthetic-trust')

    def test_malformed_records(self):
        for name, original, reason in [('PROPOSAL.json', self.proposal, 'proposal_mismatch'),
                ('SIGNOFF.json', self.signoff, 'unsigned'), ('ELIGIBILITY.json', self.applied, 'proposal_mismatch')]:
            for mutation in ({'schema_version': 2}, {'extra': 'synthetic'}, {'schema_version': True}):
                with self.subTest(record=name, mutation=mutation):
                    self.record(name, dict(original, **mutation))
                    self.assertEqual(self.outcome(), ('ineligible', reason))
            for field in original:
                with self.subTest(record=name, missing=field):
                    self.record(name, {k: v for k, v in original.items() if k != field})
                    self.assertEqual(self.outcome(), ('ineligible', reason))
            (self.folder / name).write_text('{')
            self.assertEqual(self.outcome(), ('ineligible', reason))
            (self.folder / name).write_text('{"schema_version":1,"schema_version":1}')
            self.assertEqual(self.outcome(), ('ineligible', reason))
            self.record(name, original)
        self.record('SIGNOFF.json', dict(self.signoff, signed_by=''))
        self.assertEqual(self.outcome(), ('ineligible', 'unsigned'))
        self.record('SIGNOFF.json', dict(self.signoff, document_id='other-document'))
        self.assertEqual(self.outcome(), ('ineligible', 'proposal_mismatch'))

    def test_private_record_modes_and_links(self):
        for path, mode in ((self.folder, 0o770), (self.folder, 0o644), (self.folder / 'PROPOSAL.json', 0o644)):
            before = path.stat().st_mode & 0o777
            path.chmod(mode)
            self.assertEqual(self.outcome(), ('ineligible', 'proposal_mismatch'))
            path.chmod(before)
        moved = self.folder.with_name('moved')
        self.folder.rename(moved)
        self.folder.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.outcome(), ('ineligible', 'proposal_mismatch'))
        self.folder.unlink()
        moved.rename(self.folder)
        with mock.patch.object(reader, 'REPO', self.store):
            self.assertEqual(self.outcome(), ('ineligible', 'proposal_mismatch'))

    def test_original_location_refusals(self):
        for locations in ([], [''], ['relative'], [str(self.source) + '/..'], [str(self.store / 'missing')],
                          [str(self.source), str(self.derivative)], [str(self.store.parent / 'outside')]):
            with self.subTest(case=locations):
                self.assertIsNone(reader.original_hash(self.store, locations))
        link = self.store / 'link'
        link.symlink_to(self.source)
        self.assertIsNone(reader.original_hash(self.store, [str(link)]))
        self.assertEqual(reader.original_hash(self.store, [str(self.source)] * 2), self.sha)
        with mock.patch.object(reader, '_bytes', side_effect=PermissionError):
            self.assertIsNone(reader.original_hash(self.store, [str(self.source)]))
        self.source.unlink()
        self.export('source_mismatch')

    def test_legacy_fact_missing_location_refuses(self):
        path = self.rundir.parents[1] / 'FACTS.csv'
        facts = publish.compare.facts_tool
        rows = [r for _, r in facts.load(path, base.AGENCY)[1]]
        for row in rows:
            row.pop('source_path')
        facts.write_atomic(path, rows)
        meta = json.loads((self.rundir / 'RUN.json').read_text())
        meta['inputs']['FACTS.csv'] = digest(path.read_bytes())
        (self.rundir / 'RUN.json').write_text(json.dumps(meta))
        self.export('source_mismatch')

    def test_two_cited_documents_refuse_entry_once(self):
        path = self.rundir.parents[1] / 'FACTS.csv'
        facts = publish.compare.facts_tool
        rows = [r for _, r in facts.load(path, base.AGENCY)[1]]
        other = dict(rows[0], source_sha256='d' * 64, source_path=str(self.store / 'missing'))
        other['fact_id'] = facts.fact_id(other)
        facts.write_atomic(path, rows + [other])
        findings = self.rundir / 'FINDINGS.csv'
        with findings.open(newline='') as handle:
            table = list(csv.reader(handle))
        table[1][table[0].index('fact_ids')] += ';' + other['fact_id']
        with findings.open('w', newline='') as handle:
            csv.writer(handle).writerows(table)
        meta = json.loads((self.rundir / 'RUN.json').read_text())
        meta['inputs']['FACTS.csv'] = digest(path.read_bytes())
        (self.rundir / 'RUN.json').write_text(json.dumps(meta))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = publish.main(['export', str(self.rundir), '--site-slug', 'sample'])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(output.getvalue().count('finding N1: refused redaction:source_mismatch'), 1)
        self.assertIn('exported=3 refused=1', output.getvalue())

    def test_reader_no_writes(self):
        before = {str(p): p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
        with mock.patch.object(os, 'open', wraps=os.open) as opened:
            self.assertEqual(self.outcome(), ('eligible', 'ok'))
        self.assertTrue(all(not args[1] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
                            for args, kwargs in opened.call_args_list))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.store.rglob('*') if p.is_file()})

    def test_citation_import_preserves_location(self):
        facts = self.rundir.parents[1] / 'FACTS.csv'
        destination = self.store / 'new-store'
        code, text = base.run('flock_facts.py', 'import', str(facts), '--store', str(destination),
                             '--added-by', 'synthetic', '--date', '2026-10-30')
        self.assertEqual(code, 0, text)
        target = destination / 'agencies' / base.AGENCY / 'FACTS.csv'
        tool = publish.compare.facts_tool
        valid, rows, errors = tool.load(target, base.AGENCY)
        self.assertTrue(valid)
        self.assertEqual(errors, [])
        self.assertEqual({r['source_path'] for _, r in rows}, {str(self.source)})
        self.assertEqual(target.read_bytes(), facts.read_bytes())

    def test_static_read_only_and_single_patterns(self):
        text = (base.TOOLS / 'flock_redaction_record.py').read_text()
        tree = ast.parse(text)
        imports = [n.module or '' for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        self.assertFalse(set(imports) & {'subprocess', 'socket', 'requests', 'urllib', 'http'})
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(attrs & {'write', 'write_text', 'write_bytes', 'getenv', 'environ'})
        self.assertNotIn('EMAIL =', text)
        self.assertNotIn('PHONE =', text)
        self.assertNotIn('PLATE =', text)
        self.assertIn('flock_privacy.py', text)
        self.assertIn('from campaign_tool import approval', text)


def acceptance_proof(output):
    """Real main baseline and an actual read-only mount; no live trust file is read."""
    output = Path(output)
    if not output.is_absolute() or base.ROOT == output or base.ROOT in output.parents:
        raise ValueError('proof output must be absolute and outside repository')
    case = RedactionRecordTest()
    case.setUp()
    try:
        command = ['bwrap', '--ro-bind', '/', '/', sys.executable, '-B', str(Path(__file__).resolve()),
                   '--readonly-reader', str(case.store), case.sha, str(case.source)]
        done = subprocess.run(command, capture_output=True, text=True, check=True)
        print(done.stdout, end='')
        shutil.rmtree(case.folder.parent)
        source = subprocess.run(['git', 'show', 'origin/main:tools/flock_publish.py'], cwd=base.ROOT,
                                capture_output=True, text=True, check=True).stdout
        main = {'__file__': str(base.TOOLS / 'flock_publish.py'), '__name__': '_baseline_publish'}
        exec(compile(source, str(base.TOOLS / 'flock_publish.py'), 'exec'), main)
        code, lines, expected = main['derive'](case.rundir, 'sample')
        case.assertEqual(code, 0, lines)
        raw, text = case.export()
        case.assertEqual(raw, (json.dumps(expected, indent=2, ensure_ascii=False) + '\n').encode())
        output.mkdir(mode=0o700, exist_ok=True)
        (output / 'PUBLISH.json').write_bytes(raw)
        (case.rundir / 'PUBLISH.json').unlink()
        case.source.unlink()
        _, refusal = case.export('source_mismatch')
        (output / 'refusal-lines.txt').write_text(text + refusal)
        print('BASELINE ok: byte-identical PUBLISH.json to actual origin/main with no redaction directory')
        print('PRIVACY proof files: PUBLISH.json refusal-lines.txt')
    finally:
        case.doCleanups()


if __name__ == '__main__':
    if sys.argv[1:2] == ['--readonly-reader']:
        store, sha, source = sys.argv[2:]
        reader.approval.verify = lambda data, signature: 'synthetic-reviewer'
        assert reader.eligibility(store, base.AGENCY, sha, reader.original_hash(store, [source])) == ('eligible', 'ok')
        print('READONLY ok: eligibility on read-only mounted store')
    elif sys.argv[1:2] == ['--acceptance-proof']:
        acceptance_proof(sys.argv[2])
    else:
        unittest.main()
