"""Exercise Phone's launch and cleanup without a real browser or network."""
import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.records import test_console_phone_journey as journey


class PhoneDeadline(unittest.TestCase):
    def test_silent_or_event_only_websocket_cannot_extend_call_deadline(self):
        # The real framing code must apply the remaining budget to socket I/O.
        # A series of events must not renew the response deadline either.
        for events in (False, True):
            with self.subTest(events=events):
                clock = [0.0]
                budget = [None]

                def recv(_size):
                    timeout = budget[0]
                    if timeout is None:
                        self.fail("recv was not given a timeout")
                    if not events or timeout <= 1:
                        clock[0] += timeout
                        raise TimeoutError("silent socket")
                    clock[0] += 1
                    body = b'{"method":"Page.event"}'
                    return bytes((0x81, len(body))) + body

                ws = journey.WebSocket.__new__(journey.WebSocket)
                ws.buffer = b""
                ws.sock = Mock(recv=recv, settimeout=lambda t: budget.__setitem__(0, t))
                phone = journey.Phone.__new__(journey.Phone)
                phone.ws, phone.counter, phone.events = ws, 0, []
                with patch.object(journey.time, 'monotonic', lambda: clock[0]), patch.object(journey, 'TIMEOUT', 3):
                    with self.assertRaisesRegex(journey.CdpError, "Runtime.evaluate: timed out"):
                        phone.call("Runtime.evaluate")
                self.assertLessEqual(clock[0], 3)
                self.assertEqual(len(phone.events), 2 if events else 0)

    def test_cleanup_kills_group_even_when_leader_exited_and_shares_wait_budget(self):
        # A reaped leader can leave children; an unresponsive leader must not
        # give the second wait a fresh budget on top of the first wait's overrun.
        for leader_exited in (True, False):
            with self.subTest(leader_exited=leader_exited):
                clock = [0.0]
                waits = []

                def wait(timeout):
                    waits.append(timeout)
                    if not leader_exited and len(waits) == 1:
                        clock[0] += timeout + 1
                        raise subprocess.TimeoutExpired("chrome", timeout)
                    return 0

                phone = journey.Phone.__new__(journey.Phone)
                phone.ws, phone.profile = None, None
                phone.process = Mock(pid=1234, wait=wait)
                with patch.object(journey.time, 'monotonic', lambda: clock[0]), patch.object(journey.os, 'killpg') as kill:
                    phone.close()
                self.assertEqual([args.args for args in kill.call_args_list], [(1234, signal.SIGTERM), (1234, signal.SIGKILL)])
                self.assertEqual(waits, [10, 20 if leader_exited else 9])


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
    print(json.dumps([os.getpid(), str(profile), os.environ.get('TMPDIR')]), file=record)
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
        pid, profile, _ = launch
        self.assertFalse(Path(profile).exists(), profile)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)

    def test_long_tmpdir_keeps_browser_socket_paths_short(self):
        # Protects phone launch when the worker TMPDIR exceeds the Unix socket limit (107 bytes).
        long_root = self.root / ('x' * 110)
        long_root.mkdir()
        with patch.dict(os.environ, TMPDIR=str(long_root), TMP=str(self.root.parent)):
            with patch.object(tempfile, 'tempdir', str(long_root)):
                phone = journey.Phone(self.browser('ready'))
        try:
            launch, = self.launches()
            _, profile, child_tmp = launch
            for path in (profile, child_tmp):
                self.assertLessEqual(len(os.fsencode(os.path.realpath(path))), 60)
        finally:
            phone.close()
        self.assert_cleaned(launch)

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
