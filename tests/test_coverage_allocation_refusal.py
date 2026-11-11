"""RF1440-1: a refused staging-file allocation in ``content coverage add/remove`` is the fixed ``CoverageError``.

``tempfile.mkstemp`` in ``coverage._write`` used to sit outside the write's ``OSError`` boundary, so a read-only ``content/``
raised a raw PermissionError (CLI exit 1) naming the absolute campaign and staging path. Synthetic copies of the fictional
example only; sockets are blocked and nothing is fetched, built or published.
"""
import contextlib
import errno
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli
from campaign_tool import coverage as coverage_module
from campaign_tool.coverage import CoverageError, add_coverage, list_coverage, remove_coverage

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
FIXED = "coverage.json: could not be written; the previous file was kept"
STOPPED = "Stopped: " + FIXED
ONE, TWO = "https://news.example.test/one", "https://news.example.test/two"
SENTINEL = "/sentinel/abs/campaign/content/.coverage-sentinel.tmp"
ERRNOS = (errno.EACCES, errno.ENOSPC, errno.EROFS, errno.EMFILE)


def entry(url, headline="Published headline"):
    return {"outlet": "Example News", "headline": headline, "url": url, "published_on": "2026-09-01", "findings": [],
            "kind": "news"}


def encoded(*entries):
    return (json.dumps({"schema_version": 1, "entries": list(entries)}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def snapshot(root):
    """Every path under ``root``: bytes, mode and mtime for files, mode for folders, the target for symlinks."""
    found = {}
    for path in sorted(Path(root).rglob("*")):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            found[path.relative_to(root).as_posix()] = ("link", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            found[path.relative_to(root).as_posix()] = ("dir", info.st_mode)
        else:
            found[path.relative_to(root).as_posix()] = (path.read_bytes(), info.st_mode, info.st_mtime_ns)
    return found


class AllocationCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-allocation-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.path = self.content / "coverage.json"
        (self.root / "private-sentinel.txt").write_text("synthetic private sentinel\n", encoding="utf-8")
        (self.content / "public-sentinel.txt").write_text("synthetic public sentinel\n", encoding="utf-8")
        os.symlink("private-sentinel.txt", self.root / "sentinel-link")
        NO_SOCKETS.start()
        self.addCleanup(NO_SOCKETS.stop)

    def store(self, *entries, mode=None):
        self.path.write_bytes(encoded(*entries))
        if mode is not None:
            os.chmod(self.path, mode)

    def add(self, url=TWO, headline="Published headline"):
        return add_coverage(self.root, outlet="Example News", headline=headline, url=url, published_on="2026-09-01")

    def temps(self):
        return sorted(p.name for p in self.content.iterdir() if p.name.startswith(".coverage"))

    @contextlib.contextmanager
    def read_only_content(self):
        mode = stat.S_IMODE(os.lstat(self.content).st_mode)
        os.chmod(self.content, 0o555)
        try:
            probe = self.content / ".rf1440-capability-probe"
            try:
                os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            except PermissionError:
                pass
            else:  # a privileged runtime can still write here: the real-denial case cannot be observed
                os.unlink(probe)
                self.skipTest("this runtime can write into a 0555 folder (privileged); deterministic cases still run")
            yield
        finally:
            os.chmod(self.content, mode)

    @contextlib.contextmanager
    def refusing_allocation(self, error):
        """mkstemp raises ``error``; nothing downstream of allocation may run."""
        allocator = mock.Mock(side_effect=error)
        downstream = mock.Mock(side_effect=AssertionError("downstream of a failed allocation"))
        with mock.patch.object(coverage_module.tempfile, "mkstemp", allocator), \
             mock.patch.object(coverage_module.os, "fdopen", downstream), \
             mock.patch.object(coverage_module.os, "replace", downstream), \
             mock.patch.object(coverage_module.os, "unlink", downstream):
            yield allocator
        self.assertEqual(downstream.call_count, 0)

    def run_cli(self, *argv):
        """The actual CLI ``main`` in process: (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["campaign_tool", "content", "coverage", *argv, "--directory", str(self.root)]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def subprocess_cli(self, *argv):
        return subprocess.run([sys.executable, "-m", "campaign_tool", "content", "coverage", *argv, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def assert_path_free(self, text):
        for leak in (str(self.tmp), str(self.root), SENTINEL, ".coverage-", "Traceback", "Errno"):
            self.assertNotIn(leak, text)


class AllocationRefusalTests(AllocationCase):
    def test_allocation_oserrors_become_fixed_coverage_error(self):
        operations = (("absent add", None, lambda: self.add()),
                      ("existing add", (entry(ONE),), lambda: self.add()),
                      ("existing remove", (entry(ONE), entry(TWO)), lambda: remove_coverage(self.root, url=ONE)))
        for name, entries, operation in operations:
            for number in ERRNOS:
                with self.subTest(name, errno=errno.errorcode[number]):
                    if entries is None:
                        self.path.unlink(missing_ok=True)
                    else:
                        self.store(*entries)
                    before = snapshot(self.root)
                    with self.refusing_allocation(OSError(number, "sentinel " + SENTINEL, SENTINEL)) as allocator:
                        with self.assertRaises(CoverageError) as caught:
                            operation()
                    error = caught.exception
                    self.assertIs(type(error), CoverageError)
                    self.assertEqual(str(error), FIXED)
                    self.assertEqual(error.exit_code, 2)
                    self.assertTrue(error.__suppress_context__)
                    self.assertIsNone(error.__cause__)
                    self.assertEqual(allocator.call_count, 1)
                    self.assertEqual(allocator.call_args, mock.call(dir=self.content, prefix=".coverage-", suffix=".tmp"))
                    self.assertEqual(snapshot(self.root), before)

    def test_in_process_cli_allocation_errnos_exit_two(self):
        self.store(entry(ONE))
        for number in ERRNOS:
            for argv in (("add", "--outlet", "O", "--headline", "H", "--url", TWO, "--published-on", "2026-09-01"),
                         ("remove", "--url", ONE)):
                with self.subTest(argv[0], errno=errno.errorcode[number]):
                    before = snapshot(self.root)
                    with self.refusing_allocation(OSError(number, "sentinel " + SENTINEL, SENTINEL)):
                        code, out, err = self.run_cli(*argv)
                    self.assertEqual((code, out), (2, ""))
                    self.assertEqual(err.splitlines()[0], STOPPED)
                    self.assert_path_free(err)
                    self.assertEqual(snapshot(self.root), before)

    def test_real_readonly_content_refuses_without_mutation(self):
        for name, entries in (("absent", None), ("existing", (entry(ONE),))):
            with self.subTest(name):
                if entries is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.store(*entries)
                with self.read_only_content():
                    before = snapshot(self.root)
                    with self.assertRaises(CoverageError) as caught:
                        self.add()
                    self.assertEqual(str(caught.exception), FIXED)
                    if entries is not None:
                        with self.assertRaises(CoverageError) as caught:
                            remove_coverage(self.root, url=ONE)
                        self.assertEqual(str(caught.exception), FIXED)
                    self.assertEqual(snapshot(self.root), before)
                    self.assertEqual(self.temps(), [])
                    self.assertFalse((self.root / "public").exists())
                self.assertEqual(self.path.exists(), entries is not None)

    def test_actual_cli_permission_denial_is_exit_two(self):
        self.store(entry(ONE))
        with self.read_only_content():
            before = snapshot(self.root)
            for argv in (("add", "--outlet", "O", "--headline", "H", "--url", TWO, "--published-on", "2026-09-01"),
                         ("remove", "--url", ONE)):
                with self.subTest(argv[0]):
                    result = self.subprocess_cli(*argv)
                    self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
                    self.assertEqual(result.stderr.splitlines()[0], STOPPED)
                    self.assert_path_free(result.stderr)
                    self.assertNotIn("added", result.stderr + result.stdout)
                    self.assertEqual(snapshot(self.root), before)
            self.assertEqual(self.temps(), [])

    def test_restoring_permissions_allows_normal_retry(self):
        with self.read_only_content():
            with self.assertRaises(CoverageError):
                self.add(ONE)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.add(ONE), {"action": "added", "url": ONE})
        self.assertEqual(self.path.read_bytes(), encoded(entry(ONE)))
        self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o644)

        os.chmod(self.path, 0o600)
        with self.read_only_content():
            with self.assertRaises(CoverageError):
                self.add(TWO)
        self.assertEqual(self.add(TWO), {"action": "added", "url": TWO})
        self.assertEqual(self.path.read_bytes(), encoded(entry(ONE), entry(TWO)))
        self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600)
        self.assertEqual(self.temps(), [])

        for url in (ONE, TWO):
            with self.read_only_content():
                with self.assertRaises(CoverageError):
                    remove_coverage(self.root, url=url)
            self.assertEqual(remove_coverage(self.root, url=url), {"action": "removed", "url": url})
        self.assertEqual(self.path.read_bytes(), b'{\n  "schema_version": 1,\n  "entries": []\n}\n')
        self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600)
        self.assertEqual(self.temps(), [])

    def test_validation_and_non_oserror_precedence_stay_unchanged(self):
        never = mock.Mock(side_effect=AssertionError("allocation reached"))
        with mock.patch.object(coverage_module.tempfile, "mkstemp", never):
            self.path.write_bytes(b"{not json")
            with self.assertRaises(CoverageError) as caught:
                self.add()
            self.assertNotEqual(str(caught.exception), FIXED)
            self.store(entry(ONE))
            with self.assertRaisesRegex(CoverageError, "already listed|duplicate"):
                self.add(ONE)
            with self.assertRaisesRegex(CoverageError, "URL not found"):
                remove_coverage(self.root, url="https://news.example.test/unknown")
            with mock.patch.object(coverage_module, "MAX_JSON_BYTES", 100):
                with self.assertRaisesRegex(CoverageError, "would exceed"):
                    self.add()
            self.path.unlink()
            (self.tmp / "target.json").write_bytes(encoded())
            os.symlink(self.tmp / "target.json", self.path)
            with self.assertRaisesRegex(CoverageError, "symbolic links|symlinks"):
                self.add()
            self.path.unlink()
        self.assertEqual(never.call_count, 0)
        self.assertEqual((self.tmp / "target.json").read_bytes(), encoded())

        for error in (RuntimeError("sentinel runtime"), MemoryError()):
            with self.subTest(type(error).__name__):
                with mock.patch.object(coverage_module.tempfile, "mkstemp", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        self.add()
                self.assertIs(caught.exception, error)
                self.assertFalse(self.path.exists())

    def test_post_allocation_failure_and_success_contracts_survive(self):
        self.store(entry(ONE))
        old, listing = self.path.read_bytes(), sorted(p.name for p in self.content.iterdir())
        for name in ("fsync", "replace"):
            with self.subTest(name):
                with mock.patch.object(coverage_module.os, name, side_effect=OSError(errno.EIO, "sentinel")):
                    with self.assertRaises(CoverageError) as caught:
                        self.add()
                self.assertEqual(str(caught.exception), FIXED)
                self.assertEqual((self.path.read_bytes(), sorted(p.name for p in self.content.iterdir())), (old, listing))
        real_fdopen = os.fdopen

        def failing_write(fd, *args, **kwargs):
            handle = real_fdopen(fd, *args, **kwargs)
            handle.write = mock.Mock(side_effect=OSError(errno.ENOSPC, "sentinel"))
            return handle
        with mock.patch.object(coverage_module.os, "fdopen", side_effect=failing_write):
            with self.assertRaises(CoverageError) as caught:
                self.add()
        self.assertEqual(str(caught.exception), FIXED)
        self.assertEqual((self.path.read_bytes(), sorted(p.name for p in self.content.iterdir())), (old, listing))

        headline = "Café review: \u00e9t\u00e9 \u2014 \u65b0\u95fb"
        self.assertEqual(self.add(TWO, headline=headline), {"action": "added", "url": TWO})
        self.assertEqual(self.path.read_bytes(), encoded(entry(ONE), entry(TWO, headline)))
        self.assertEqual([e["url"] for e in list_coverage(self.root)], [ONE, TWO])
        self.assertEqual(remove_coverage(self.root, url=ONE), {"action": "removed", "url": ONE})
        self.assertEqual(self.path.read_bytes(), encoded(entry(TWO, headline)))
        self.assertEqual(self.temps(), [])


if __name__ == "__main__":
    unittest.main()
