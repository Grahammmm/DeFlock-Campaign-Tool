"""RF1651-5: ``read_weights`` owns its raw descriptor until stream construction succeeds and refuses post-open
filesystem errors with a fixed, baseline-labelled ValueError (no exception payload, no second path).

Real files and descriptors; only the failing operation is injected, filtered to the baseline descriptor. Synthetic only.
"""
import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import site_check
from tests import test_site_weights_record as record_fixtures
from tests.test_site_budget import run_main

RECORD = {"schema": "page-weights-v1", "budget_kb": 1,
          "pages": [{"page": "index.html", "html_bytes": 1, "total": 1, "assets": []}]}
SECRET = "SYNTHETIC-PRIVATE-PAYLOAD"  # pragma: allowlist secret
SECOND = "/SYNTHETIC-SECOND-PATH"


def fd_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


def injected(code=errno.EIO):
    return OSError(code, SECRET, SECOND)


class Probe:
    """Patches ``os.fdopen`` (binary-read only) to record the baseline fd and optionally fail one operation."""

    def __init__(self, fdopen=None, fstat=None, read=None, exit_=None):
        self.fd, self.stream = None, None
        self.fdopen, self.fstat, self.read, self.exit_ = fdopen, fstat, read, exit_
        self.real_fdopen, self.real_fstat = os.fdopen, os.fstat
        self.parser_calls = 0

    def _fdopen(self, fd, *args, **kwargs):
        if args[:1] != ("rb",):
            return self.real_fdopen(fd, *args, **kwargs)
        self.fd = fd
        if self.fdopen:
            raise self.fdopen
        inner = self.real_fdopen(fd, *args, **kwargs)
        self.stream = Wrapper(inner, self)
        return self.stream

    def _fstat(self, fd):
        if self.fstat and fd == self.fd:
            raise self.fstat
        return self.real_fstat(fd)

    def __enter__(self):
        self.patches = [mock.patch.object(site_check.os, "fdopen", self._fdopen),
                        mock.patch.object(site_check.os, "fstat", self._fstat),
                        mock.patch.object(site_check.json, "loads", self._loads)]
        self.real_loads = json.loads
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()

    def _loads(self, *args, **kwargs):
        self.parser_calls += 1
        return self.real_loads(*args, **kwargs)


class Wrapper:
    def __init__(self, inner, probe):
        self.inner, self.probe = inner, probe

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.inner.close()
        if self.probe.exit_:
            raise self.probe.exit_
        return False

    def read(self, *args):
        if self.probe.read:
            raise self.probe.read
        return self.inner.read(*args)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-weights-io-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.baseline = self.tmp / "baseline.json"
        self.baseline.write_text(json.dumps(RECORD), encoding="utf-8")
        self.label = f"--weights-baseline {self.baseline}"
        self.fixed = f"{self.label}: cannot be read (filesystem error)"

    def refused(self, probe):
        with probe:
            with self.assertRaises(ValueError) as caught:
                site_check.read_weights(self.baseline)
        text = str(caught.exception)
        self.assertEqual(text, self.fixed)
        self.assertTrue(caught.exception.__suppress_context__)
        for marker in (SECRET, SECOND, "Errno"):
            self.assertNotIn(marker, text)
        self.assertFalse(fd_open(probe.fd), "baseline descriptor leaked")
        self.assertEqual(probe.parser_calls, 0)


class ConstructionTests(Base):
    def test_fdopen_oserror_closes_real_acquired_fd(self):
        sentinel = os.open(self.baseline, os.O_RDONLY)
        self.addCleanup(os.close, sentinel)
        for code in (errno.EMFILE, errno.EIO):
            for _ in range(3):  # repeated failures do not accumulate descriptors
                self.refused(Probe(fdopen=injected(code)))
        self.assertTrue(fd_open(sentinel))
        self.assertEqual(site_check.read_weights(self.baseline)["schema"], "page-weights-v1")  # recovery

    def test_fdopen_non_oserrors_cleanup_without_relabeling(self):
        for failure in (RuntimeError("x"), MemoryError(), KeyboardInterrupt(), SystemExit(3)):
            probe = Probe(fdopen=failure)
            with probe:
                with self.assertRaises(type(failure)) as caught:
                    site_check.read_weights(self.baseline)
            self.assertIs(caught.exception, failure)
            self.assertFalse(fd_open(probe.fd))

    def test_raw_close_failure_does_not_replace_primary_error(self):
        primary = injected()
        probe = Probe(fdopen=primary)
        with probe, mock.patch.object(site_check.os, "close", side_effect=OSError(errno.EBADF, "bad")) as close:
            with self.assertRaises(ValueError) as caught:
                site_check.read_weights(self.baseline)
        self.assertEqual(str(caught.exception), self.fixed)
        close.assert_called_once_with(probe.fd)
        os.close(probe.fd)  # the mocked close never released it

    def test_fd_zero_is_owned_when_construction_fails(self):
        child = (
            "import os, errno, sys\n"
            "from unittest import mock\n"
            "from campaign_tool import site_check\n"
            "os.close(0)\n"
            "real = os.fdopen\n"
            "seen = []\n"
            "def failing(fd, *a, **k):\n"
            "    seen.append(fd)\n"
            "    raise OSError(errno.EMFILE, 'x')\n"
            "with mock.patch.object(site_check.os, 'fdopen', failing):\n"
            "    try:\n"
            "        site_check.read_weights(sys.argv[1])\n"
            "    except ValueError:\n"
            "        pass\n"
            "try:\n"
            "    os.fstat(0)\n"
            "    print('LEAK')\n"
            "except OSError:\n"
            "    print('CLOSED', seen)\n")
        result = subprocess.run([sys.executable, "-B", "-c", child, str(self.baseline)], capture_output=True,
                                text=True, cwd=Path(__file__).resolve().parent.parent, timeout=60)
        self.assertEqual(result.stdout.strip(), "CLOSED [0]", result.stderr)


class PostOpenTests(Base):
    def test_metadata_and_read_oserrors_are_controlled(self):
        for code in (errno.EACCES, errno.EIO, errno.ENOENT):
            with self.subTest(op="fstat", code=code):
                self.refused(Probe(fstat=injected(code)))
            with self.subTest(op="read", code=code):
                probe = Probe(read=injected(code))
                self.refused(probe)
                self.assertTrue(probe.stream.closed)

    def test_context_exit_oserror_refuses_before_json(self):
        probe = Probe(exit_=injected())
        self.refused(probe)
        self.assertTrue(probe.stream.closed)

    def test_existing_diagnostics_unchanged(self):
        link = self.tmp / "link.json"
        link.symlink_to(self.baseline)
        big = self.tmp / "big.json"
        big.write_bytes(b" " * (site_check.WEIGHTS_MAX_BYTES + 1))
        bad = self.tmp / "bad.json"
        bad.write_text("nope {")
        for path, expected in ((link, "cannot be read as a regular file, not a symlink"),
                               (self.tmp / "absent.json", "cannot be read as a regular file, not a symlink"),
                               (big, "must be a regular file of at most"),
                               (bad, "not valid JSON")):
            with self.assertRaises(ValueError) as caught:
                site_check.read_weights(path)
            self.assertIn(f"--weights-baseline {path}: {expected}", str(caught.exception))
            self.assertNotIn("filesystem error", str(caught.exception))

    def test_directory_is_a_controlled_refusal_without_leak(self):
        # On main a directory made os.fdopen raise a raw IsADirectoryError out of read_weights.
        directory = self.tmp / "dir.json"
        directory.mkdir()
        before = set(os.listdir("/proc/self/fd"))
        with self.assertRaises(ValueError) as caught:
            site_check.read_weights(directory)
        self.assertEqual(str(caught.exception), f"--weights-baseline {directory}: cannot be read (filesystem error)")
        self.assertEqual(before, set(os.listdir("/proc/self/fd")))

    def test_successful_roundtrip(self):
        self.assertEqual(site_check.read_weights(self.baseline), RECORD)
        exact = self.tmp / "exact.json"
        head = json.dumps(RECORD).encode()
        exact.write_bytes(head + b" " * (site_check.WEIGHTS_MAX_BYTES - len(head)))
        self.assertEqual(site_check.read_weights(exact), RECORD)


class CliTests(record_fixtures.Base):
    def test_actual_cli_refusal_preserves_baseline_and_skips_record(self):
        baseline = self.private / "baseline.json"
        baseline.write_text(json.dumps(RECORD), encoding="utf-8")
        before = (baseline.read_bytes(), baseline.stat().st_mode, baseline.stat().st_mtime_ns)
        out = self.private / "new-record.json"
        probe = Probe(read=injected())
        with probe:
            code, text, err = run_main("build", "--directory", str(self.root), "--budget", "100000",
                                       "--weights-baseline", str(baseline), "--weights-out", str(out))
        self.assertEqual(code, 1)
        self.assertIn(f"Stopped: --weights-baseline {baseline}: cannot be read (filesystem error)", err)
        for marker in (SECRET, SECOND, "Errno", "Traceback"):
            self.assertNotIn(marker, err + text)
        self.assertNotIn("Page weight", text)
        self.assertNotIn("since baseline", text)
        self.assertFalse(out.exists())
        self.assertFalse(fd_open(probe.fd))
        self.assertEqual(before, (baseline.read_bytes(), baseline.stat().st_mode, baseline.stat().st_mtime_ns))
        code, text, err = run_main("build", "--directory", str(self.root), "--budget", "100000",
                                   "--weights-baseline", str(baseline), "--weights-out", str(out))  # recovery
        self.assertEqual(code, 0, err)
        self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
