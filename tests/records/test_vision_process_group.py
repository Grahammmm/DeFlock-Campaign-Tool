"""M10: real synthetic wrapper grandchildren die before a vision receipt returns."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from campaign_tool.records.extract import vision
from tests.records import test_ocr_vision as fixtures

ADAPTER = '''import os
from pathlib import Path
import sys
import time
pidfile, mode = Path(sys.argv[1]), sys.argv[2]
child = os.fork()
if child == 0:
    os.close(1)
    os.close(2)
    pidfile.write_text(str(os.getpid()))
    time.sleep(30)
    os._exit(0)
for _ in range(200):
    if pidfile.exists():
        break
    time.sleep(0.005)
if mode == 'timeout':
    time.sleep(30)
if mode == 'failed':
    sys.exit(9)
print('Synthetic handwritten page transcribed with clear ordinary text.')
'''


def alive(pid):
    try:
        raw = Path('/proc') / str(pid) / 'stat'
        return raw.read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


class ProcessGroupTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.VisionFallbackTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        _, _, self.result = self.h.bundle(fixtures.Runner(text='', confidence='0'), ocr_vision=None)

    def run_case(self, mode, *, explicit_runner=False):
        script = self.h.h.root / ('adapter-' + mode + '.py')
        pidfile = self.h.h.root / ('pid-' + mode)
        script.write_text(ADAPTER)
        adapter = vision.adapter([sys.executable, str(script), str(pidfile), mode],
                                 'synthetic', 'synthetic-model', 'process-group-test-v1')
        entry = self.result['ocr_receipts'][0]
        kwargs = {'runner': subprocess.run} if explicit_runner else {}
        child = None
        try:
            receipt = vision.run_fallback(self.h.ocr_root, self.result['original_sha256'], 2,
                                          entry['receipt_id'], vision_adapter=adapter,
                                          timeout=2, attempt_id=mode, **kwargs)
            self.assertTrue(pidfile.exists(), 'synthetic child must have started')
            child = int(pidfile.read_text())
            deadline = time.monotonic() + 1
            while alive(child) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(alive(child), 'adapter grandchild outlived its receipt')
            return receipt
        finally:
            if child is None and pidfile.exists():
                child = int(pidfile.read_text())
            if child is not None and alive(child):
                os.kill(child, signal.SIGKILL)  # Clean up only this owned negative-control child.

    def test_timeout_receipt_kills_grandchild(self):
        receipt = self.run_case('timeout')
        self.assertEqual((receipt['status'], receipt['blocked_reason']), ('blocked', 'vision_timeout'))
        self.assertNotIn('sidecar.txt', receipt['artifact_sha256'])

    def test_explicit_stdlib_runner_from_routes_also_kills_grandchild(self):
        receipt = self.run_case('failed', explicit_runner=True)
        self.assertEqual(receipt['blocked_reason'], 'vision_command_failed')

    def test_success_kills_grandchild_and_keeps_unreviewed_text(self):
        receipt = self.run_case('success')
        self.assertEqual(receipt['status'], vision.TEXT_STATE)
        self.assertEqual(receipt['review_status'], 'not_reviewed')
