"""FA1309 synthetic tests. Importing/discovering this file installs no hooks."""
import csv
import contextlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.fixtures.fa1309 import support

GATE = ('    if (reader.fieldnames is None or reader.fieldnames.count("conf") != 1\n'
        '            or reader.fieldnames.count("text") != 1):\n'
        '        return None\n')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(contextlib.ExitStack())
        if support.RUN_ROOT is None:
            # Ordinary discovery gets reversible, fixture-lifetime guards only.
            temp = self.enterContext(tempfile.TemporaryDirectory())
            self.stack.enter_context(support.boundary(Path(temp)))
        self.root = support.fresh(support.RUN_ROOT / self._testMethodName)
        from campaign_tool.records.extract import ocr
        self.ocr = ocr


class Behavior(Fixture):
    def test_unique_headers_and_producer_shape(self):
        cases = [
            ("conf\ttext\n1\tword\n", 1.0),
            ("text\tconf\nword\t99\n", 99.0),
            ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
             "5\t1\t1\t1\t1\t1\t0\t0\t20\t10\t99\tword\n", 99.0),
            ("extra\ttext\tconf\textra\nx\tword\t80\ty\n", 80.0),
            ("text\tconf\nword\t0\nword\t100\n", 50.0),
            ("text\tconf\n \t99\n\t100\nword\t33.333\nword\t66.666\n", 50.0),
            ("text\tconf\nword\t-1\nword\t101\nword\tnan\nword\tinf\nword\t-inf\nword\tinvalid\nword\t80\n", 80.0),
            ("text\tconf\nword\tnan\n", None),
        ]
        for tsv, expected in cases:
            with self.subTest(tsv=tsv):
                self.assertEqual(self.ocr._confidence(tsv), expected)
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter="\t")
        writer.writerows([['text', 'conf', 'other'], ['quoted\ttab', '90', 'x'],
                          ['quoted\nnewline', '91.111', 'y']])
        self.assertEqual(self.ocr._confidence(stream.getvalue()), 90.56)

    def test_duplicate_conf_low_high(self):
        self.assertIsNone(self.ocr._confidence("conf\tconf\ttext\n1\t99\tword\n"))

    def test_ambiguous_or_missing_headers(self):
        for tsv in ("conf\tconf\ttext\n99\t1\tword\n",
                    "text\ttext\tconf\na\tb\t99\n",
                    "conf\ttext\tconf\ttext\n1\ta\t99\tb\n",
                    "text\nword\n", "conf\n99\n", "other\nword\n",
                    "conf\ttext\n", "", "conf\tconf\ttext\n"):
            with self.subTest(tsv=tsv):
                self.assertIsNone(self.ocr._confidence(tsv))

    def test_extraction_blocks_ambiguity_and_preserves_peer_and_rerun(self):
        from pypdf import PdfWriter
        import hashlib
        import subprocess
        writer = PdfWriter()
        for _ in range(2):
            writer.add_blank_page(width=72, height=72)
        stream = io.BytesIO()
        writer.write(stream)
        raw = stream.getvalue()
        sha = hashlib.sha256(raw).hexdigest()
        (self.root / 'blobs').mkdir()
        (self.root / 'blobs' / sha).write_bytes(raw)
        calls = []
        tsvs = ["conf\tconf\ttext\n1\t99\tword\n", "text\tconf\nword\t99\n"]
        def inert(command, **kwargs):
            calls.append(command)
            if Path(command[0]).name == 'pdftoppm':
                Path(command[-1] + '.png').write_bytes(b'synthetic raster')
            else:
                Path(command[2] + '.txt').write_text('synthetic word\n')
                Path(command[2] + '.tsv').write_text(tsvs.pop(0))
            return subprocess.CompletedProcess(command, 0, '', '')
        tools = {name: {'path': '/synthetic/bin/' + name, 'version': 'inert-test'}
                 for name in self.ocr.OCR_TOOLS}
        output = self.root / 'ocr'
        def extract():
            return self.ocr.extract_image_only_pages(self.root, sha, output,
                        tool_signature='synthetic-v1', tools=tools, runner=inert)
        receipts = extract()
        self.assertEqual(len(receipts), 2)
        blocked, peer = receipts
        self.assertEqual((blocked['status'], blocked['blocked_reason'], blocked['confidence']),
                         ('blocked', 'confidence_unavailable', None))
        self.assertEqual((peer['status'], peer['confidence']), ('ocr_text_unreviewed', 99.0))
        for receipt in receipts:
            self.assertEqual(receipt['review_status'], 'not_reviewed')
            self.assertEqual(receipt['fidelity_status'], 'not_checked')
            loaded = self.ocr.load_page_receipt(output, sha, receipt['locator']['page'], receipt['receipt_id'])
            self.assertEqual(loaded['sidecar'], b'synthetic word\n')
            self.assertEqual(set(receipt['artifact_sha256']), set(self.ocr.ARTIFACTS))
        self.assertEqual(extract(), receipts)
        self.assertEqual([Path(c[0]).name for c in calls], ['pdftoppm', 'tesseract'] * 2)
        self.assertTrue(all(c[3:] == ['-l', 'eng', 'txt', 'tsv'] for c in calls if Path(c[0]).name == 'tesseract'))
        self.assertEqual((self.root / 'blobs' / sha).read_bytes(), raw)


class GateControl(Fixture):
    def test_deciding_gate_disabled_then_restored(self):
        candidate = support.disposable(support.REPO, self.root / 'disposable')
        source = candidate / 'campaign_tool/records/extract/ocr.py'
        original = source.read_text()
        self.assertEqual(original.count(GATE), 1)
        source.write_text(original.replace(GATE, '', 1))
        broken, broken_root = support.invoke_child(candidate, 'deciding', self.root / 'broken')
        summary = json.loads((broken_root / 'result.json').read_text())
        self.assertEqual(broken.returncode, 1)
        self.assertEqual(summary['tests'], 2)
        self.assertEqual(summary['failures'], ['tests.records.test_fa1309.Behavior.test_duplicate_conf_low_high'])
        self.assertEqual(summary['errors'], [])
        self.assertEqual(summary['skipped'], [])
        self.assertIn('99.0 is not None', broken.stderr)
        source.write_text(original)
        restored, restored_root = support.invoke_child(candidate, 'deciding', self.root / 'restored')
        positive = json.loads((restored_root / 'result.json').read_text())
        self.assertEqual(restored.returncode, 0)
        self.assertEqual(positive['tests'], 2)
        self.assertEqual(positive['failures'] + positive['errors'] + positive['skipped'], [])
        self.assertEqual(source.read_text(), original)


class Isolation(Fixture):
    def test_fresh_child_paths_and_no_attempts(self):
        child, output = support.invoke_child(support.REPO, 'probe', self.root / 'child')
        self.assertEqual(child.returncode, 0, child.stderr)
        probe = json.loads((output / 'probe.json').read_text())
        self.assertEqual(probe['module'], str(support.REPO / 'campaign_tool/records/extract/ocr.py'))
        self.assertEqual(probe['attempts'], [])
        self.assertTrue(probe['bytecode_disabled'])
        self.assertTrue(probe['default_runner_guarded'])
        self.assertEqual(probe['tempdir'], str(output))

    def test_output_symlink_ancestry_refused_before_writes(self):
        target = self.root / 'target'
        target.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        for output in (alias, alias / 'new-child'):
            with self.assertRaisesRegex(ValueError, 'unsafe_output_ancestry'):
                support.fresh(output)
        self.assertEqual(list(target.iterdir()), [])

    def test_guard_context_restores_peer_defaults(self):
        import subprocess
        before = (socket.socket, subprocess.run, tempfile.tempdir, sys.dont_write_bytecode,
                  self.ocr.extract_image_only_pages.__kwdefaults__['runner'],
                  self.ocr.dependency_status.__defaults__)
        with support.boundary(self.root):
            self.assertIs(self.ocr.extract_image_only_pages.__kwdefaults__['runner'], subprocess.run)
        after = (socket.socket, subprocess.run, tempfile.tempdir, sys.dont_write_bytecode,
                 self.ocr.extract_image_only_pages.__kwdefaults__['runner'],
                 self.ocr.dependency_status.__defaults__)
        self.assertEqual(after, before)

    def test_sentinels_are_inert_and_fail_closed(self):
        before = len(support.ATTEMPTS)
        with self.assertRaisesRegex(RuntimeError, 'offline_network_model_process_sentinel'):
            socket.create_connection(('synthetic.invalid', 1))
        # A fictional sibling, never an actual live path or source record.
        with self.assertRaisesRegex(RuntimeError, 'offline_file_sentinel'):
            (support.REPO.parent / 'synthetic-forbidden-file').read_bytes()
        self.assertEqual(len(support.ATTEMPTS), before + 2)
        del support.ATTEMPTS[before:]
