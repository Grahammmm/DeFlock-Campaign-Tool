"""FA1306: real staged API, synthetic ledgers, caller-owned transactions.

Discovery imports only standard-library/support code. Application imports and
irreversible sentinels occur exclusively in disposable file-launched children.
"""
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.fixtures.fa1306 import support as s


class ChildTests(unittest.TestCase):
    def run(self, result=None):
        if s.RUN is not None:
            return super().run(result)
        # Ordinary repository discovery remains isolated without poisoning peers.
        result = result or self.defaultTestResult()
        result.startTest(self)
        try:
            with tempfile.TemporaryDirectory(prefix='fa1306-discovery-') as tmp:
                out = Path(tmp)
                proc = child(s.REPO, self.id(), out)
                if proc.returncode:
                    raise AssertionError(proc.stdout + proc.stderr)
        except Exception:
            result.addFailure(self, sys.exc_info())
        else:
            result.addSuccess(self)
        finally:
            result.stopTest(self)
        return result


def child(repo, selector, out):
    out = s.checked_directory(out)
    out.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, '-B', str(repo / 'tools' / 'accept_fa1306.py'),
            '--child', selector, '--output-root', str(out)]
    proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=120)
    (out / 'child.stdout').write_text(proc.stdout)
    (out / 'child.stderr').write_text(proc.stderr)
    (out / 'command.json').write_text(json.dumps(dict(argv=argv, cwd=str(repo), exit=proc.returncode)))
    return proc


class Behavior(ChildTests):
    def world(self, installed=False):
        return s.World(self, installed)

    def caller(self, w):
        con = w.store.ledger(w.db)
        c = con.__enter__()
        self.addCleanup(con.__exit__, None, None, None)
        c.execute('BEGIN IMMEDIATE')
        c.execute('CREATE TABLE synthetic_outer(value)')
        c.execute('INSERT INTO synthetic_outer VALUES(17)')
        c.execute('SAVEPOINT caller_nested')
        c.execute('INSERT INTO synthetic_outer VALUES(23)')
        return c

    def permit_closed(self, con):
        try:
            value = con.execute('SELECT original_resolution_append_allowed(1)').fetchone()[0]
        except sqlite3.OperationalError as exc:
            self.assertIn('no such function', str(exc))
        else:
            self.assertEqual(value, 0)

    def test_success_fresh_and_installed_replay_caller_rollback(self):
        for installed in (False, True):
            with self.subTest(installed=installed):
                w = self.world(installed)
                con = self.caller(w)
                before = w.extension(con)
                identity, reused = w.append(con)
                self.assertFalse(reused)
                self.assertTrue(con.in_transaction)
                self.assertEqual(w.append(con), (identity, True))
                self.assertEqual(con.execute('SELECT count(*) FROM original_resolutions').fetchone()[0], 1)
                self.assertEqual(w.resolution.effective_original(con, w.subject, output_root=w.output,
                                 mail_policy=w.policy)['resolution_sha256'], identity)
                self.permit_closed(con)
                con.execute('ROLLBACK TO caller_nested')
                self.assertEqual(w.extension(con), before)
                con.execute('RELEASE caller_nested')
                con.commit()
                self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM synthetic_outer')], [(17,)])
                con.execute('BEGIN IMMEDIATE')
                self.assertEqual(w.append(con), (identity, False))
                con.commit()
                prior = w.snapshot(con)
                con.execute('BEGIN IMMEDIATE')
                self.assertEqual(w.append(con), (identity, True))
                self.permit_closed(con)
                con.commit()
                self.assertEqual(w.snapshot(con), prior)

    def refusal(self, mode):
        for installed in (False, True):
            with self.subTest(installed=installed):
                w = self.world(installed)
                con = self.caller(w)
                changes = {}
                if mode == 'original':
                    changes['subject'] = '0' * 64
                    code = 'original_resolution_not_unresolved'
                elif mode == 'policy':
                    v = dict(w.value, originals={})
                    changes['policy'] = s.policy(v)
                    code = 'resolution_policy_invalid'
                else:
                    con.execute("UPDATE stage_state SET status='blocked' WHERE stage='extract'")
                    code = 'original_resolution_requires_unaccepted_original'
                before = w.snapshot(con)
                with self.assertRaisesRegex(ValueError, '^' + code + '$'):
                    w.append(con, **changes)
                self.permit_closed(con)
                con.execute('RELEASE caller_nested')
                con.commit()  # Decide after catch-and-commit, not merely while pending.
                self.assertEqual(w.snapshot(con), before, 'rejected append leaked extension schema')
                self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM synthetic_outer')], [(17,), (23,)])
                self.assertEqual(bool(w.extension(con)[0]), installed)

    def test_missing_original_outer_commit_has_no_extension_leak(self):
        self.refusal('original')

    def test_missing_policy_entry_outer_commit_has_no_extension_leak(self):
        self.refusal('policy')

    def test_ineligible_outer_commit_has_no_extension_leak(self):
        self.refusal('eligibility')

    def test_post_insert_self_check_failures_rollback_only_call(self):
        from campaign_tool.records.ledger import original_resolution as resolution
        for installed in (False, True):
            for error in (ValueError('synthetic_check'), KeyboardInterrupt('synthetic_check'),
                          sqlite3.OperationalError('synthetic_check')):
                with self.subTest(installed=installed, error=type(error).__name__):
                    w = self.world(installed)
                    con = self.caller(w)
                    before = w.snapshot(con)
                    reached = []

                    def fail(c, subject, **context):
                        self.assertEqual(c.execute('SELECT count(*) FROM original_resolutions').fetchone()[0], 1)
                        self.assertEqual(c.execute('SELECT original_resolution_append_allowed(1)').fetchone()[0], 0)
                        reached.append(subject)
                        raise error

                    expected = 'original_resolution_write_failed' if isinstance(error, sqlite3.Error) else 'synthetic_check'
                    typ = ValueError if isinstance(error, sqlite3.Error) else type(error)
                    with patch.object(resolution, 'effective_original', side_effect=fail):
                        with self.assertRaisesRegex(typ, '^' + expected + '$') as caught:
                            w.append(con)
                    if not isinstance(error, sqlite3.Error):
                        self.assertIs(caught.exception, error)
                    self.assertEqual(reached, [w.subject])
                    self.assertTrue(con.in_transaction)
                    self.assertEqual(w.snapshot(con), before)
                    self.permit_closed(con)
                    con.execute('RELEASE caller_nested')
                    con.commit()
                    self.assertEqual(w.snapshot(con), before)
                    con.execute('BEGIN IMMEDIATE')
                    self.assertFalse(w.append(con)[1])
                    self.permit_closed(con)
                    con.rollback()

    def test_replay_failure_retains_committed_row_and_extension(self):
        from campaign_tool.records.ledger import original_resolution as resolution
        w = self.world()
        with w.store.ledger(w.db) as con:
            con.execute('BEGIN IMMEDIATE')
            identity, _ = w.append(con)
            con.commit()
            before = w.snapshot(con)
            con.execute('BEGIN IMMEDIATE')
            with patch.object(resolution, 'effective_original', side_effect=ValueError('synthetic_replay')):
                with self.assertRaisesRegex(ValueError, '^synthetic_replay$'):
                    w.append(con)
            self.permit_closed(con)
            con.commit()
            self.assertEqual(w.snapshot(con), before)
            con.execute('BEGIN IMMEDIATE')
            self.assertEqual(w.append(con), (identity, True))
            con.rollback()

    def test_no_transaction_no_schema_or_capability(self):
        for installed in (False, True):
            w = self.world(installed)
            with w.store.ledger(w.db) as con:
                before = w.snapshot(con)
                with self.assertRaisesRegex(ValueError, '^original_resolution_transaction_required$'):
                    w.append(con)
                self.assertFalse(con.in_transaction)
                self.assertEqual(w.snapshot(con), before)
                self.permit_closed(con)


class GateControl(unittest.TestCase):
    def test_schema_outside_savepoint_mutant_is_decisive(self):
        root = Path(tempfile.mkdtemp(prefix='gate-', dir=s.RUN))
        target = root / 'repo'
        shutil.copytree(s.REPO, target, ignore=shutil.ignore_patterns('.git', '.venv', '__pycache__', 'node_modules'))
        path = target / 'campaign_tool/records/ledger/original_resolution_mail.py'
        positive = path.read_text()
        marker = '        ensure_schema(con)\n'
        self.assertEqual(positive.count(marker), 1, 'mutant requires the frozen protective boundary')
        broken_source = positive.replace(marker, '', 1)
        broken_source = broken_source.replace('    con.execute("SAVEPOINT mail_resolution_append")',
                                              '    ensure_schema(con)\n    con.execute("SAVEPOINT mail_resolution_append")', 1)
        path.write_text(broken_source)
        selector = __name__ + '.Behavior.test_missing_original_outer_commit_has_no_extension_leak'
        broken = child(target, selector, root / 'broken')
        receipt = json.loads((root / 'broken' / 'result.json').read_text())
        self.assertEqual(broken.returncode, 1)
        self.assertEqual((receipt['tests'], receipt['errors'], receipt['skips']), (1, [], []))
        self.assertEqual(len(receipt['failures']), 1)
        self.assertIn('test_missing_original_outer_commit_has_no_extension_leak', receipt['failures'][0])
        self.assertIn('rejected append leaked extension schema', broken.stderr)
        # Successful append and unrelated caller work stay otherwise valid in mutant.
        controls = child(target, __name__ + '.Behavior.test_success_fresh_and_installed_replay_caller_rollback', root / 'broken-positive')
        self.assertEqual(controls.returncode, 0, controls.stderr)
        path.write_text(positive)
        restored = child(target, selector, root / 'restored')
        self.assertEqual(restored.returncode, 0, restored.stderr)


class Isolation(unittest.TestCase):
    def test_fresh_child_candidate_modules_no_unsafe_attempts(self):
        root = Path(tempfile.mkdtemp(prefix='isolation-', dir=s.RUN))
        proc = child(s.REPO, 'probe', root / 'probe')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads((root / 'probe' / 'probe.json').read_text())
        self.assertEqual(value['attempts'], [])
        self.assertEqual(Path(value['module']), s.REPO / 'campaign_tool/records/ledger/original_resolution_mail.py')
        self.assertTrue(value['bytecode_disabled'])
        self.assertTrue(Path(value['temp']).is_relative_to(root))

    def test_linked_output_ancestry_refused_before_writes(self):
        root = Path(tempfile.mkdtemp(prefix='links-', dir=s.RUN))
        real = root / 'real'
        real.mkdir()
        link = root / 'alias'
        link.symlink_to(real, target_is_directory=True)
        before = list(real.iterdir())
        argv = [sys.executable, '-B', str(s.REPO / 'tools/accept_fa1306.py'),
                '--case', 'behavior', '--output-root', str(link / 'not-created')]
        proc = subprocess.run(argv, cwd=s.REPO, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('unsafe output ancestry', proc.stderr)
        self.assertEqual(list(real.iterdir()), before)
        for unsafe in (link, link / 'child'):
            with self.assertRaisesRegex(ValueError, 'unsafe output ancestry'):
                s.checked_directory(unsafe)
