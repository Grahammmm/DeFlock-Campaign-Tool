"""Exercise Phone's launch and cleanup without a real browser or network."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.records import test_console_phone_journey as journey


class PhoneStartup(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / "launches.jsonl"
        self.enterContext(patch.object(journey, "STARTUP_TIMEOUT", 1))
        self.connect = self.enterContext(patch.object(journey.urllib.request, "urlopen"))
        self.connect.return_value = io.StringIO('[{"type": "page", "id": "fake-page"}]')
        self.websocket = self.enterContext(patch.object(journey, "WebSocket")).return_value
        self.websocket.recv.side_effect = [json.dumps({"id": n, "result": {}}) for n in range(1, 5)]

    def browser(self, *launch_modes):
        binary = self.root / "fake-chrome"
        binary.write_text("""#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

log = Path(__file__).with_name('launches.jsonl')
profile = Path(next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--user-data-dir=')))
attempt = len(log.read_text().splitlines()) if log.exists() else 0
with log.open('a') as record:
    print(json.dumps([os.getpid(), str(profile)]), file=record)
for n in range(25):
    print(f'fake chrome launch {attempt + 1} stderr line {n:02d}', file=sys.stderr, flush=True)
mode = """ + repr(launch_modes) + """[attempt]
if mode == 'exit':
    sys.exit(7)
if mode == 'ready':
    (profile / 'DevToolsActivePort').write_text('17321\\n/devtools/browser/fake\\n')
time.sleep(15)  # Backstop if the helper fails to terminate the process.
""", encoding="utf-8")
        binary.chmod(0o700)
        return str(binary)

    def launches(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_cleaned(self, launch):
        pid, profile = launch
        self.assertFalse(Path(profile).exists(), profile)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)

    def test_relaunches_stalled_browser_once_with_a_fresh_profile(self):
        # Without a retry, the first launch fails before the fake transport can initialize.
        phone = journey.Phone(self.browser("hang", "ready"))
        try:
            first, second = self.launches()
            self.assertNotEqual(first[1], second[1])
            self.assert_cleaned(first)
            self.assertTrue(Path(second[1], "DevToolsActivePort").is_file())
            self.connect.assert_called_once_with("http://127.0.0.1:17321/json/list", timeout=journey.TIMEOUT)
            self.assertEqual(self.websocket.send.call_count, 4)
        finally:
            phone.close()
        self.assert_cleaned(second)

    def test_second_timeout_reports_stderr_and_cleans_both_launches(self):
        # A reset deadline must not turn a persistent startup failure into unbounded relaunches.
        with self.assertRaisesRegex(journey.CdpError, r"never opened its debugging port.*elapsed \d+\.\d+s") as error:
            journey.Phone(self.browser("hang", "hang"))
        self.assertIn("fake chrome launch 2 stderr line 05", str(error.exception))
        self.assertIn("fake chrome launch 2 stderr line 24", str(error.exception))
        self.assertNotIn("stderr line 04", str(error.exception))
        first, second = self.launches()
        self.assertNotEqual(first[1], second[1])
        self.assert_cleaned(first)
        self.assert_cleaned(second)
        self.connect.assert_not_called()

    def test_does_not_relaunch_a_browser_that_exits_at_start(self):
        # Retrying a deterministic launch failure could hide the original exit code and diagnostic.
        with self.assertRaisesRegex(journey.CdpError, r"exited at start \(code 7\).*elapsed \d+\.\d+s") as error:
            journey.Phone(self.browser("exit"))
        self.assertIn("fake chrome launch 1 stderr line 24", str(error.exception))
        launch, = self.launches()
        self.assert_cleaned(launch)
        self.connect.assert_not_called()

    def test_does_not_retry_cdp_setup_after_the_port_opens(self):
        # A broad retry on CdpError would also retry a failure after reaching the endpoint.
        self.websocket.recv.side_effect = journey.CdpError("browser closed the websocket")
        with self.assertRaisesRegex(journey.CdpError, r"browser closed the websocket.*elapsed \d+\.\d+s") as error:
            journey.Phone(self.browser("ready"))
        self.assertIn("fake chrome launch 1 stderr line 24", str(error.exception))
        launch, = self.launches()
        self.assert_cleaned(launch)
        self.websocket.close.assert_called_once_with()
