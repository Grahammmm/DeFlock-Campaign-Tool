"""Offline synthetic acceptance for attended readiness, never live mail steps."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import mail_readiness_matrix as gate
from tools.mail_readiness_matrix import MatrixError, check_runbook, evaluate, load

ROOT = Path(__file__).resolve().parents[1]
ROWS = ('S2', 'S3', 'S3e', 'S4', 'S4b', 'S5', 'S6', 'S7', 'S7r', 'S8', 'S9a', 'S9b', 'S10', 'F1')
CELLS = ('gate_evidence', 'backup_receipt', 'rollback_rehearsal', 'authority')


def cell(status='MET', required=True, what='synthetic evidence'):
    return dict(required=required, status=status, owner='operator', prerequisite='operator admission', what=what)


def synthetic():
    rows = {}
    for row in ROWS:
        rows[row] = dict(purpose='synthetic', tools=[], **{name: cell() for name in CELLS})
        if row != 'S10':
            rows[row]['tools'] = [dict(name='synthetic', status='EXECUTABLE', path='tool.py',
                                      argv=['python3', 'tool.py'], owner='operator', prerequisite='review')]
    rows['S9b']['authority']['what'] = 'separate G exact-action approval: synthetic session'
    return dict(schema='mail-readiness-matrix-v1', rows=rows)


class RowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'tool.py').write_bytes(b'synthetic\n')
        self.matrix = synthetic()

    def verdicts(self):
        return evaluate(self.matrix, self.root)

    def test_row_gates_and_nonmutation_label(self):
        self.assertEqual(self.verdicts()['S6'], dict(verdict='READY', reasons=[]))
        for row, name, reason in [('S9b', 'authority', 's9b_separate_approval_required'),
                                  ('S2', 'authority', 'cell_authority_not_met'),
                                  ('S7', 'backup_receipt', 'cell_backup_receipt_not_met'),
                                  ('S6', 'gate_evidence', 'cell_gate_evidence_not_met'),
                                  ('F1', 'authority', 'fallback_approval_required')]:
            with self.subTest(row=row):
                self.matrix = synthetic()
                self.matrix['rows'][row][name] = cell('UNSUPPLIED', what='APPROVED on a card')
                if row == 'S2':
                    self.matrix['rows'][row][name]['what'] = 'G word PROBE at the console'
                result = self.verdicts()[row]
                self.assertEqual(result['verdict'], 'HELD')
                self.assertIn(reason, result['reasons'])
        self.matrix = synthetic()
        self.matrix['rows']['S4']['rollback_rehearsal'] = cell('UNSUPPLIED', False, 'M-31 nonmutation')
        result = self.verdicts()
        self.assertEqual(result['S4']['verdict'], 'READY')
        self.assertEqual(result['S4']['label'], 'as of one capture; nonmutation UNSUPPLIED (M-31)')
        self.assertNotIn('COMPLETE', json.dumps(result))

    def test_independence_and_live_negative_control(self):
        for row in ('S9a', 'S4', 'S6'):
            self.matrix = synthetic()
            self.matrix['rows'][row]['gate_evidence'] = cell('UNSUPPLIED')
            before = self.verdicts()
            self.matrix['rows'][row]['gate_evidence'] = cell()
            after = self.verdicts()
            self.assertEqual([r for r in ROWS if before[r] != after[r]], [row])
        self.matrix = synthetic()
        for row, name in [('S6', 'gate_evidence'), ('S7', 'backup_receipt')]:
            self.matrix['rows'][row][name] = cell('UNSUPPLIED', what='APPROVED on a card')
        def bad_predicate(c):
            return c['status'] == 'MET' or 'APPROVED' in c['what']
        for expected, context in [('READY', patch.object(gate, 'cell_met', bad_predicate)),
                                  ('HELD', patch.object(gate, 'cell_met', gate.cell_met))]:
            with context:
                for row in ('S6', 'S7'):
                    self.assertEqual(self.verdicts()[row]['verdict'], expected)

    def test_tool_identity(self):
        tool = self.matrix['rows']['S6']['tools'][0]
        digest = hashlib.sha256(b'synthetic\n').hexdigest()
        (self.root / 'linked.py').symlink_to(self.root / 'tool.py')
        (self.root / 'linkdir').symlink_to(self.root, target_is_directory=True)
        for path, sha, bad in [('tool.py', digest, False), ('tool.py', hashlib.sha256(b'wrong').hexdigest(), True),
                               ('missing.py', digest, True), ('linked.py', digest, True),
                               ('linkdir/tool.py', digest, True), ('../tool.py', digest, True),
                               (str(self.root / 'tool.py'), digest, True), ('.', digest, True)]:
            with self.subTest(path=path):
                tool.update(path=path, sha256=sha)
                self.assertEqual('tool_identity' in self.verdicts()['S6']['reasons'], bad)
        tool.pop('sha256')
        tool.update(path='tool.py', status='NON_EXECUTABLE')
        tool.pop('argv')
        self.assertIn('tool_not_executable', self.verdicts()['S6']['reasons'])

    def test_load_rejects_malformed_without_echoing(self):
        path = self.root / 'matrix.json'
        variants = []
        def altered(action):
            m = synthetic()
            action(m)
            variants.append(json.dumps(m))
        variants.append('{"schema": "private-canary", "schema": "duplicate"}')
        altered(lambda m: m['rows'].pop('S2'))
        altered(lambda m: m['rows'].update(UNKNOWN=m['rows']['S2']))
        altered(lambda m: m['rows']['S2']['authority'].pop('owner'))
        altered(lambda m: m['rows']['S2']['authority'].update(prerequisite=''))
        altered(lambda m: m['rows']['S2']['tools'][0].update(path='../tool.py'))
        altered(lambda m: m['rows']['S2']['tools'][0].update(path='/tool.py'))
        altered(lambda m: m['rows']['S2']['tools'][0].update(path=None))
        altered(lambda m: m['rows']['S2']['authority'].update(status='NOT_APPLICABLE'))
        altered(lambda m: m['rows']['S2']['tools'][0].update(sha256=hashlib.sha256(b'x').hexdigest().upper()))
        altered(lambda m: m['rows']['S2']['tools'][0].update(status='UNSUPPLIED', owner=''))
        altered(lambda m: m['rows']['S2']['tools'][0].update(status='NON_EXECUTABLE', prerequisite=''))
        altered(lambda m: m['rows']['S2']['authority'].update(required=1))
        altered(lambda m: m['rows']['S2']['tools'][0].update(argv=['python3', '<relative>']))
        variants.extend(['[]', 'null', '{', ' ' * (256 * 1024 + 1)])
        for text in variants:
            with self.subTest(index=variants.index(text)):
                path.write_text(text)
                with self.assertRaises(MatrixError) as caught:
                    load(path)
                self.assertRegex(str(caught.exception), r'^[a-z_]+$')
                self.assertNotIn('private-canary', str(caught.exception))
                failed = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/mail_readiness_matrix.py'),
                                         str(path)], capture_output=True, text=True, check=False)
                self.assertEqual(failed.returncode, 2)
                self.assertNotIn('private-canary', failed.stdout)
                self.assertNotIn(str(path), failed.stdout)
        path.write_text(json.dumps(synthetic()))
        self.assertEqual(gate.load(path), synthetic())
        linked = self.root / 'linked.json'
        linked.symlink_to(path)
        with self.assertRaises(gate.MatrixError):
            gate.load(linked)
        with self.assertRaises(gate.MatrixError):
            gate.load(self.root)
        with self.assertRaises(gate.MatrixError):
            gate.load(self.root / 'absent')


class DocTests(unittest.TestCase):
    def test_containers_and_tokens(self):
        for text in ['> ```sh\nsudo x\n> ```', '> ~~~~sh\nx\n> ~~~~',
                     '- ```sh\nx\n  ```', '>     cp a b; mv b c',
                     '- item\n\n    ```sh\nx\n    ```', '- item\n\n    cp a b',
                     '```python\nx\n```', '    cp a b', '~~~sh\nx\n~~~']:
            with self.subTest(text=text):
                self.assertIn('unsupported_code_container', gate.check_runbook(text))
        for command, code in [('a; b', 'shell_chaining'), ('sudo x', 'forbidden_token'),
                              ('x | y', 'shell_chaining'), ('PYTHONPATH=x y', 'forbidden_token'),
                              ('echo x > mail_config', 'credential_edit'),
                              ('vi /private/credentials', 'credential_edit'),
                              ('docker exec x', 'forbidden_token'), ('rm x', 'forbidden_token'),
                              ('x && y', 'shell_chaining'), ('x || y', 'shell_chaining'),
                              ('x $(y)', 'shell_chaining'), ('x `y`', 'shell_chaining'),
                              ('x << eof', 'shell_chaining'), ('x token=y', 'forbidden_token'),
                              ('x password=y', 'forbidden_token')]:
            with self.subTest(command=command):
                self.assertIn(code, gate.check_runbook('```sh\n' + command + '\n```'))
        self.assertEqual(gate.check_runbook('Prose; clean.\n\n| a | b |\n|---|---|\n'), [])
        self.assertEqual(gate.check_runbook('```sh\npython3 tool.py </private/input>\n```'), [])


class ArtifactTests(unittest.TestCase):
    def test_committed_matrix_and_runbook(self):
        matrix = load(ROOT / 'docs/runbooks/mail-readiness-matrix.json')
        results = evaluate(matrix, ROOT)
        self.assertEqual([r for r in ROWS if results[r]['verdict'] == 'READY'], ['S10'])
        self.assertEqual(sum(r['verdict'] == 'HELD' for r in results.values()), 13)
        encoded = json.dumps(matrix)
        self.assertNotRegex(encoded, r't_[0-9a-f]{8}')
        self.assertNotIn('"sha256"', encoded)
        book = (ROOT / 'docs/runbooks/ATTENDED-MAIL-PIPELINE.md').read_text()
        self.assertEqual(check_runbook(book), [])
        for sentence in ['No agent runs any step.', 'Credentials are typed only by G or the custodian.',
                         'A guard refusal means STOP; keep the exact text.']:
            self.assertIn(sentence, book)
        self.assertEqual(re.findall(r'Matrix row: (\w+)', book), list(ROWS))
        commands = [line for block in re.findall(r'^```sh\n(.*?)^```$', book, re.M | re.S)
                    for line in block.splitlines() if line]
        expected = []
        for row in matrix['rows'].values():
            for tool in row['tools']:
                if tool['status'] == 'EXECUTABLE':
                    self.assertTrue((ROOT / tool['path']).is_file())
                    expected.append(' '.join(tool['argv']))
                else:
                    self.assertTrue(tool['owner'] and tool['prerequisite'])
        self.assertCountEqual(commands, expected)
        for name in ('S9b', 'F1'):
            section = book.split('Matrix row: ' + name + '\n', 1)[1].split('\n## ', 1)[0]
            self.assertIn('HELD', section)

    def test_cli_counts_and_private_input_refusal(self):
        base = [sys.executable, '-B', str(ROOT / 'tools/mail_readiness_matrix.py')]
        good = subprocess.run(base + [str(ROOT / 'docs/runbooks/mail-readiness-matrix.json'),
                              '--runbook', str(ROOT / 'docs/runbooks/ATTENDED-MAIL-PIPELINE.md')],
                              capture_output=True, text=True, check=False)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        output = json.loads(good.stdout)
        self.assertEqual((output['rows'], output['ready'], output['held']), (14, 1, 13))
        self.assertEqual(output['runbook_codes'], [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'private-canary.json'
            path.write_text('{"private-canary": "not-a-matrix"}')
            bad = subprocess.run(base + [str(path)], capture_output=True, text=True, check=False)
            self.assertEqual(bad.returncode, 2)
            self.assertNotIn('private-canary', bad.stdout)
            self.assertNotIn(str(path), bad.stdout)
            path.write_text('```sh\nsudo x\n```')
            bad = subprocess.run(base + [str(ROOT / 'docs/runbooks/mail-readiness-matrix.json'),
                                 '--runbook', str(path)], capture_output=True, text=True, check=False)
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(json.loads(bad.stdout)['runbook_codes'], ['forbidden_token'])


if __name__ == '__main__':
    unittest.main()
