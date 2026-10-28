"""Synthetic native-limit meet tests; no host resource API is called.

Portable unittest loading needs no staging manifest. The imported closure
(folder -> config) has no data-path constants or bound data-path defaults;
config accepts explicit paths only. Scoped guards precede application import.
These guards cover this suite's closure, not arbitrary hostile Python code.
"""
from contextlib import ExitStack, contextmanager
import importlib
import io
import json
import os
from pathlib import Path
import resource as host_resource
import socket
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
import urllib.request


KIND = "fictional-native-limit"
INFINITY = -1


def finite_meet(requested, inherited):
    """Independent finite-set oracle (no production calls)."""
    candidates = {requested}
    candidates.update(value for value in inherited if value != INFINITY)
    return sorted(candidates)[0]


@contextmanager
def offline_import_scope(fixture):
    """No data IO, model import, process or network use is needed here."""
    repo = Path(__file__).resolve().parents[2]
    original_open, original_io_open = open, io.open

    def checked_open(original):
        def guarded(path, mode="r", *args, **kwargs):
            if isinstance(path, int):
                raise AssertionError("unexpected descriptor data IO")
            target = Path(path).absolute()
            resolved = target.resolve()
            synthetic = resolved == fixture or fixture in resolved.parents
            code_read = repo in resolved.parents and resolved.suffix == ".py" and not any(
                flag in mode for flag in "wax+"
            )
            if not (synthetic or code_read):
                raise AssertionError("unexpected real-data IO")
            return original(path, mode, *args, **kwargs)
        return guarded

    class NoModels:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in {"ollama", "openai", "anthropic", "transformers"}:
                raise AssertionError("unexpected model import")
            return None

    with ExitStack() as stack:
        for owner, name in ((socket, "socket"), (socket, "create_connection"),
                            (socket, "getaddrinfo"), (urllib.request, "urlopen"),
                            (subprocess, "Popen"), (os, "system"),
                            (sqlite3, "connect")):
            stack.enter_context(patch.object(owner, name, side_effect=AssertionError("offline sentinel")))
        stack.enter_context(patch("builtins.open", checked_open(original_open)))
        stack.enter_context(patch.object(io, "open", checked_open(original_io_open)))
        stack.enter_context(patch.object(sys, "meta_path", [NoModels(), *sys.meta_path]))
        # No application imports have occurred before the scoped sentinels.
        yield importlib.import_module("campaign_tool.records.intake.folder")


class NativeIntakeLimitMeetTests(unittest.TestCase):
    def setUp(self):
        self.before = (list(sys.path), tempfile.tempdir, dict(os.environ),
                       sys.modules.get("resource"), socket.socket,
                       subprocess.Popen, sqlite3.connect, list(sys.meta_path))
        self.enterContext(patch.object(tempfile, "tempdir", tempfile.tempdir))
        parent = os.environ.get("FA1704_FIXTURE_ROOT") or tempfile.gettempdir()
        self.fixture = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="fictional-meet-", dir=parent)))
        self.real_get = self.enterContext(patch.object(host_resource, "getrlimit", side_effect=AssertionError("real get forbidden")))
        self.real_set = self.enterContext(patch.object(host_resource, "setrlimit", side_effect=AssertionError("real set forbidden")))
        self.intake = self.enterContext(offline_import_scope(self.fixture))

    def tearDown(self):
        self.real_get.assert_not_called()
        self.real_set.assert_not_called()
        self.doCleanups()
        current = (list(sys.path), tempfile.tempdir, dict(os.environ),
                   sys.modules.get("resource"), socket.socket,
                   subprocess.Popen, sqlite3.connect, list(sys.meta_path))
        self.assertEqual(current, self.before, "scoped mocks or global state leaked")

    def invoke(self, requested, inherited, *, helper=None, fail_get=None, fail_set=None):
        trace = []
        fake = ModuleType("resource")
        fake.RLIM_INFINITY = INFINITY

        def get(kind):
            trace.append(("get", kind))
            if fail_get is not None:
                raise fail_get
            return inherited

        def set_limit(kind, limits):
            trace.append(("set", kind, limits))
            if fail_set is not None:
                raise fail_set

        fake.getrlimit, fake.setrlimit = Mock(side_effect=get), Mock(side_effect=set_limit)
        previous = sys.modules.get("resource")
        try:
            with patch.dict(sys.modules, {"resource": fake}):
                try:
                    value = (helper or self.intake.apply_resource_limit)(KIND, requested)
                except (OSError, ValueError) as error:
                    return None, trace, fake, error
                return value, trace, fake, None
        finally:
            self.assertIs(sys.modules.get("resource"), previous)

    def assert_meet(self, outcome, requested, inherited):
        value, trace, fake, error = outcome
        expected = finite_meet(requested, inherited)
        self.assertIsNone(error)
        self.assertEqual((value, trace), (expected, [
            ("get", KIND), ("set", KIND, (expected, expected))
        ]), "FA1704 meet trace/value mismatch")
        fake.getrlimit.assert_called_once_with(KIND)
        fake.setrlimit.assert_called_once_with(KIND, (expected, expected))
        return value

    def test_ordinary_lattice(self):
        # Finite soft <= hard, finite soft/unlimited hard, both unlimited.
        values = (0, 2, 7, 9)
        pairs = [(soft, hard) for soft in values for hard in values if soft <= hard]
        pairs += [(soft, INFINITY) for soft in values] + [(INFINITY, INFINITY)]
        for requested in values:
            for inherited in pairs:
                with self.subTest(requested=requested, inherited=inherited):
                    self.assert_meet(self.invoke(requested, inherited), requested, inherited)

    def test_direct_inherited_helper_does_not_set(self):
        for inherited in ((INFINITY, INFINITY), (0, 9), (2, 9), (7, 7), (9, INFINITY)):
            with self.subTest(inherited=inherited):
                value, trace, fake, error = self.invoke(7, inherited, helper=self.intake.inherited_limit)
                self.assertIsNone(error)
                self.assertEqual(value, finite_meet(7, inherited))
                self.assertEqual(trace, [("get", KIND)])
                fake.getrlimit.assert_called_once_with(KIND)
                fake.setrlimit.assert_not_called()

    def test_defensive_sentinel_combinations(self):
        # Unlimited soft with finite hard is NOT an ordinary OS pair.
        for requested in (0, 2, 7, 9):
            for hard in (0, 2, 7, 9):
                with self.subTest(defensive=(INFINITY, hard), requested=requested):
                    self.assert_meet(self.invoke(requested, (INFINITY, hard)), requested, (INFINITY, hard))

    def test_tightening_each_finite_input_cannot_increase_result(self):
        for requested in (0, 2, 7, 9):
            for soft in (0, 2, 7, 9):
                for hard in (0, 2, 7, 9):
                    if soft > hard:
                        continue
                    original = self.assert_meet(self.invoke(requested, (soft, hard)), requested, (soft, hard))
                    # Hold the other two coordinates fixed; keep ordinary pairs.
                    for coordinate, value in enumerate((requested, soft, hard)):
                        for tightened in range(value + 1):
                            triple = [requested, soft, hard]
                            triple[coordinate] = tightened
                            if triple[1] > triple[2]:
                                continue
                            with self.subTest(original=(requested, soft, hard), coordinate=coordinate, tightened=tightened):
                                result = self.assert_meet(self.invoke(triple[0], tuple(triple[1:])), triple[0], tuple(triple[1:]))
                                self.assertLessEqual(result, original)

    def test_get_failure_propagates_without_set(self):
        failure = OSError("fictional get refusal")
        for helper in (self.intake.inherited_limit, self.intake.apply_resource_limit):
            value, trace, fake, error = self.invoke(7, (2, 9), helper=helper, fail_get=failure)
            self.assertIs(error, failure)
            self.assertIsNone(value)
            self.assertEqual(trace, [("get", KIND)])
            fake.getrlimit.assert_called_once_with(KIND)
            fake.setrlimit.assert_not_called()

    def test_set_failure_propagates_once_without_retry_or_restore(self):
        for failure in (OSError("fictional set refusal"), ValueError("fictional invalid ceiling")):
            value, trace, fake, error = self.invoke(7, (2, 9), fail_set=failure)
            self.assertIs(error, failure)
            self.assertIsNone(value)
            self.assertEqual(trace, [("get", KIND), ("set", KIND, (2, 2))])
            fake.getrlimit.assert_called_once_with(KIND)
            fake.setrlimit.assert_called_once_with(KIND, (2, 2))

    def test_oracle_detects_faulty_setter_that_ignores_tighter_soft_limit(self):
        def faulty_setter(kind, requested):
            import resource
            _, hard = resource.getrlimit(kind)
            effective = min(requested, hard) if hard != resource.RLIM_INFINITY else requested
            resource.setrlimit(kind, (effective, effective))
            return effective

        faulty = self.invoke(7, (2, 9), helper=faulty_setter)
        try:
            self.assert_meet(faulty, 7, (2, 9))
        except AssertionError as error:
            self.assertIn("FA1704 meet trace/value mismatch", str(error))
            print("FA1704_EXPECTED_ASSERTION " + json.dumps({
                "fault": "ignored inherited soft", "exception": type(error).__name__,
                "traceback": traceback.format_exc(), "faulty_value": faulty[0],
                "expected_value": finite_meet(7, (2, 9)),
            }, sort_keys=True))
        else:
            self.fail("faulty setter escaped the deciding assertion")
        self.assert_meet(self.invoke(7, (2, 9)), 7, (2, 9))

    def test_scoped_guards_and_ordinary_loading_restore_peer_state(self):
        # Same-process before/after loaders, not just clean subprocesses.
        selector = __name__ + ".NativeIntakeLimitMeetTests.test_direct_inherited_helper_does_not_set"
        before = (list(sys.path), tempfile.tempdir, dict(os.environ), list(sys.meta_path), sys.modules["resource"])
        for _ in range(2):
            suite = unittest.defaultTestLoader.loadTestsFromName(selector)
            stream = io.StringIO()
            result = unittest.TextTestRunner(stream=stream).run(suite)
            self.assertEqual(result.testsRun, 1, stream.getvalue())
            self.assertTrue(result.wasSuccessful(), stream.getvalue())
            self.assertEqual(result.skipped, [])
            self.assertEqual((list(sys.path), tempfile.tempdir, dict(os.environ), list(sys.meta_path), sys.modules["resource"]), before)


if __name__ == "__main__":
    unittest.main()
