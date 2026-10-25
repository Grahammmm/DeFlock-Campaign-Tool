"""FC7-REPORT-CLOSE: write_private closes its directory descriptor on cleanup failure.

Frozen case IDs (one test method each):
    successful_write_and_replace, write_or_fsync_failure_with_successful_cleanup,
    unlink_failure_still_closes_directory, close_failure_remains_failure,
    original_target_and_residue_evidence, symlink_and_nonregular_target_refusal.

Ordinary positives use fresh report paths under the temporary-directory default
(the acceptance runner points it at a fresh run directory). Cleanup-failure
controls replace the module's OS calls with an in-memory fake, so they open,
delete and leak nothing real. Real-filesystem cases that inject a failure keep a
test-only safety net: any directory descriptor the finalizer leaves open is
recorded as a failure and then closed by the test, never leaked past it.
"""
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

# Evidence for the acceptance runner: application modules loaded before this
# module imported the code under test (the runner installs sentinels first).
APPLICATION_PRELOADED = sorted(name for name in sys.modules if name.split(".")[0] == "campaign_tool")

from campaign_tool.records.gates import safe_output  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "review-cases.json"
ARTIFACT_ENV = "FC7_REPORT_CLOSE_ARTIFACT_ROOT"
TEMPORARY_PREFIX = ".records-report-"
SYNTHETIC_TOKEN = "synthetic"
SYNTHETIC_TEMPORARY = TEMPORARY_PREFIX + SYNTHETIC_TOKEN
SYNTHETIC_TARGET = Path("/fc7-synthetic/reports/report.json")
REAL_OPEN, REAL_CLOSE, REAL_UNLINK, REAL_FSYNC = os.open, os.close, os.unlink, os.fsync
# Per-case descriptor evidence, read by the acceptance runner after each run.
OBSERVATIONS = {}


class FakeFile:
    def __init__(self, owner, fd):
        self.owner, self.fd = owner, fd

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.owner.calls.append(("file-close", self.fd))
        return False

    def fileno(self):
        return self.fd

    def write(self, text):
        self.owner.calls.append(("write", self.fd, len(text)))
        return len(text)

    def flush(self):
        self.owner.calls.append(("flush", self.fd))


class FakeOS:
    """In-memory stand-in for the os calls write_private makes; nothing real is touched."""
    O_RDONLY, O_DIRECTORY, O_NOFOLLOW = os.O_RDONLY, os.O_DIRECTORY, os.O_NOFOLLOW
    O_WRONLY, O_CREAT, O_EXCL = os.O_WRONLY, os.O_CREAT, os.O_EXCL

    def __init__(self, *, fsync_file=None, replace=None, unlink=None, close_directory=None):
        self.failures = {"fsync_file": fsync_file, "replace": replace, "unlink": unlink,
                         "close_directory": close_directory}
        self.next_fd = 1000
        self.directories, self.files, self.closed, self.calls = [], [], [], []

    def _fail(self, key):
        if self.failures[key] is not None:
            raise self.failures[key]

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        fd, self.next_fd = self.next_fd, self.next_fd + 1
        (self.directories if flags & os.O_DIRECTORY else self.files).append(fd)
        self.calls.append(("open", str(path), dir_fd, fd))
        return fd

    def mkdir(self, *args, **kwargs):
        raise AssertionError("mkdir is not expected in the mocked cases")

    def stat(self, name, *, dir_fd, follow_symlinks):
        self.calls.append(("stat", name, dir_fd, follow_symlinks))
        raise FileNotFoundError(name)

    def fdopen(self, fd, mode, encoding):
        self.calls.append(("fdopen", fd, mode, encoding))
        return FakeFile(self, fd)

    def fchmod(self, fd, mode):
        self.calls.append(("fchmod", fd, mode))

    def fsync(self, fd):
        self.calls.append(("fsync", fd))
        if fd in self.files:
            self._fail("fsync_file")

    def replace(self, source, target, *, src_dir_fd, dst_dir_fd):
        self.calls.append(("replace", source, target, src_dir_fd, dst_dir_fd))
        self._fail("replace")

    def unlink(self, name, *, dir_fd):
        self.calls.append(("unlink", name, dir_fd))
        self._fail("unlink")

    def close(self, fd):
        self.calls.append(("close", fd))
        self.closed.append(fd)
        if fd in self.directories and fd == self.directories[-1]:
            self._fail("close_directory")

    def named(self, kind):
        return [call for call in self.calls if call[0] == kind]

    def evidence(self, raised):
        return {"acquired_directories": list(self.directories), "acquired_files": list(self.files),
                "closed": list(self.closed),
                "unclosed_directories": [fd for fd in self.directories if fd not in self.closed],
                "unlinks": self.named("unlink"), "raised": repr(raised),
                "context": repr(raised.__context__) if raised is not None else None}


class RealTracker:
    """Pass-through wrappers that record directory descriptors on a real filesystem."""

    def __init__(self, *, fsync_failure=None, replace_failure=None, unlink_failure=None):
        self.fsync_failure, self.replace_failure, self.unlink_failure = (
            fsync_failure, replace_failure, unlink_failure)
        self.acquired, self.closed, self.open_directories = [], [], set()
        self.unlinks, self.fsyncs, self.replaces = [], [], []

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        fd = REAL_OPEN(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_DIRECTORY:
            self.acquired.append(fd)
            self.open_directories.add(fd)
        return fd

    def close(self, fd):
        if fd in self.open_directories:
            self.open_directories.discard(fd)
            self.closed.append(fd)
        return REAL_CLOSE(fd)

    def fsync(self, fd):
        directory = fd in self.open_directories
        self.fsyncs.append("directory" if directory else "file")
        if self.fsync_failure is not None and not directory:
            raise self.fsync_failure
        return REAL_FSYNC(fd)

    def replace(self, *args, **kwargs):
        self.replaces.append(args)
        raise self.replace_failure

    def unlink(self, name, *, dir_fd=None):
        self.unlinks.append(name)
        if self.unlink_failure is not None:
            raise self.unlink_failure
        return REAL_UNLINK(name, dir_fd=dir_fd)

    def patches(self):
        patches = [mock.patch.object(safe_output.os, "open", self.open),
                   mock.patch.object(safe_output.os, "close", self.close),
                   mock.patch.object(safe_output.os, "fsync", self.fsync),
                   mock.patch.object(safe_output.os, "unlink", self.unlink)]
        if self.replace_failure is not None:
            patches.append(mock.patch.object(safe_output.os, "replace", self.replace))
        return patches

    def release_leaks(self):
        """Test-only safety net: close and report anything the finalizer left open."""
        leaked = sorted(self.open_directories)
        for fd in leaked:
            REAL_CLOSE(fd)
        self.open_directories.clear()
        return leaked


def call_mocked(fake, *, text="new"):
    raised = None
    with mock.patch.object(safe_output, "os", fake), \
            mock.patch.object(safe_output.secrets, "token_hex", return_value=SYNTHETIC_TOKEN):
        try:
            safe_output.write_private(SYNTHETIC_TARGET, text)
        except Exception as caught:  # inspected by the caller
            raised = caught
    return raised


class ReportCloseTest(unittest.TestCase):
    def setUp(self):
        base = os.environ.get(ARTIFACT_ENV) or None
        self.tmp = tempfile.TemporaryDirectory(prefix="fc7-report-close-", dir=base)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def real(self, tracker, *args, **kwargs):
        raised = None
        patches = tracker.patches()
        for patch in patches:
            patch.start()
        try:
            safe_output.write_private(*args, **kwargs)
        except Exception as caught:  # inspected by the caller
            raised = caught
        finally:
            for patch in reversed(patches):
                patch.stop()
        return raised, tracker.release_leaks()

    def residue(self, directory=None):
        return sorted(path.name for path in (directory or self.root).glob(TEMPORARY_PREFIX + "*"))

    def observe(self, case, evidence):
        OBSERVATIONS.setdefault(case, []).append(evidence)

    def test_successful_write_and_replace(self):
        text = FIXTURE.read_text(encoding="utf-8")
        target = self.root / "report.json"
        target.write_text("old")
        target.chmod(0o644)
        alias = self.root / "alias.json"
        os.link(target, alias)
        before = target.stat().st_ino
        tracker = RealTracker()
        raised, leaked = self.real(tracker, target, text)
        self.observe("successful_write_and_replace", {"acquired": tracker.acquired, "closed": tracker.closed,
                                                      "leaked": leaked, "raised": repr(raised)})
        self.assertIsNone(raised)
        self.assertEqual(leaked, [])
        self.assertEqual(sorted(tracker.acquired), sorted(tracker.closed))
        self.assertEqual(target.read_bytes(), FIXTURE.read_bytes())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertNotEqual(target.stat().st_ino, before)
        self.assertEqual(alias.read_text(), "old")
        self.assertEqual(stat.S_IMODE(alias.stat().st_mode), 0o644)
        self.assertEqual(tracker.fsyncs, ["file", "directory"])
        self.assertEqual(tracker.unlinks, [])
        self.assertEqual(self.residue(), [])
        nested = self.root / "new" / "nested" / "report.json"
        tracker = RealTracker()
        raised, leaked = self.real(tracker, nested, text, create_parents=True)
        self.assertIsNone(raised)
        self.assertEqual(leaked, [])
        self.assertEqual(nested.read_bytes(), FIXTURE.read_bytes())
        self.assertEqual(stat.S_IMODE(nested.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o600)

    def test_write_or_fsync_failure_with_successful_cleanup(self):
        fsync_error = OSError("synthetic fsync failure")
        for label, text, tracker, expected in (
                ("write", "bad \udcff", RealTracker(), UnicodeEncodeError),
                ("fsync", "new", RealTracker(fsync_failure=fsync_error), OSError)):
            with self.subTest(stage=label):
                target = self.root / (label + ".json")
                target.write_text("old")
                target.chmod(0o640)
                raised, leaked = self.real(tracker, target, text)
                self.observe("write_or_fsync_failure_with_successful_cleanup",
                             {"stage": label, "acquired": tracker.acquired, "closed": tracker.closed,
                              "leaked": leaked, "unlinks": tracker.unlinks, "raised": repr(raised)})
                self.assertIsInstance(raised, expected)
                if label == "fsync":
                    self.assertIs(raised, fsync_error)
                self.assertEqual(leaked, [])
                self.assertEqual(sorted(tracker.acquired), sorted(tracker.closed))
                self.assertEqual(len(tracker.unlinks), 1)
                self.assertTrue(tracker.unlinks[0].startswith(TEMPORARY_PREFIX))
                self.assertEqual(target.read_text(), "old")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
                self.assertEqual(self.residue(), [])

    def test_unlink_failure_still_closes_directory(self):
        for stage in ("replace", "fsync_file"):
            with self.subTest(original=stage):
                original = OSError("synthetic %s failure" % stage)
                cleanup = OSError("synthetic cleanup failure")
                fake = FakeOS(unlink=cleanup, **{stage: original})
                raised = call_mocked(fake)
                evidence = fake.evidence(raised)
                evidence["original_stage"] = stage
                self.observe("unlink_failure_still_closes_directory", evidence)
                self.assertIs(raised, cleanup)
                self.assertIs(raised.__context__, original)
                self.assertEqual(fake.directories, [1000, 1001, 1002])
                self.assertEqual(fake.named("unlink"), [("unlink", SYNTHETIC_TEMPORARY, 1002)])
                self.assertEqual(fake.closed.count(1002), 1)
                self.assertEqual(evidence["unclosed_directories"], [])
                self.assertEqual(fake.calls[-1], ("close", 1002))
                self.assertEqual(fake.named("fsync"), [("fsync", 1003)])

    def test_close_failure_remains_failure(self):
        cases = (
            ("successful_write", {}, None),
            ("replace_failure", {"replace": OSError("synthetic replace failure")}, "replace"),
            ("replace_and_unlink_failure", {"replace": OSError("synthetic replace failure"),
                                            "unlink": OSError("synthetic cleanup failure")}, "unlink"))
        for label, failures, context in cases:
            with self.subTest(case=label):
                close_error = OSError("synthetic close failure")
                fake = FakeOS(close_directory=close_error, **failures)
                raised = call_mocked(fake)
                evidence = fake.evidence(raised)
                evidence["case"] = label
                self.observe("close_failure_remains_failure", evidence)
                self.assertIs(raised, close_error)
                self.assertEqual(fake.closed.count(1002), 1)
                self.assertEqual(evidence["unclosed_directories"], [])
                self.assertEqual(fake.calls[-1], ("close", 1002))
                if context is None:
                    self.assertIsNone(raised.__context__)
                    self.assertEqual(fake.named("unlink"), [])
                    self.assertEqual(fake.named("fsync"), [("fsync", 1003), ("fsync", 1002)])
                elif context == "replace":
                    self.assertIs(raised.__context__, failures["replace"])
                    self.assertEqual(fake.named("unlink"), [("unlink", SYNTHETIC_TEMPORARY, 1002)])
                else:
                    self.assertIs(raised.__context__, failures["unlink"])
                    self.assertIs(raised.__context__.__context__, failures["replace"])
                    self.assertEqual(fake.named("unlink"), [("unlink", SYNTHETIC_TEMPORARY, 1002)])

    def test_original_target_and_residue_evidence(self):
        target = self.root / "report.json"
        target.write_text("old")
        target.chmod(0o644)
        alias = self.root / "alias.json"
        os.link(target, alias)
        inode = target.stat().st_ino
        original = OSError("synthetic replace failure")
        cleanup = OSError("synthetic cleanup failure")
        tracker = RealTracker(replace_failure=original, unlink_failure=cleanup)
        raised, leaked = self.real(tracker, target, "new")
        residue = self.residue()
        self.observe("original_target_and_residue_evidence",
                     {"acquired": tracker.acquired, "closed": tracker.closed, "leaked": leaked,
                      "unlinks": tracker.unlinks, "residue": residue, "raised": repr(raised),
                      "context": repr(getattr(raised, "__context__", None))})
        self.assertIs(raised, cleanup)
        self.assertIs(raised.__context__, original)
        self.assertEqual(leaked, [])
        self.assertEqual(sorted(tracker.acquired), sorted(tracker.closed))
        self.assertEqual(len(tracker.replaces), 1)
        self.assertEqual(tracker.unlinks, residue)
        self.assertEqual(len(residue), 1)
        left = self.root / residue[0]
        self.assertEqual(left.read_text(), "new")
        self.assertEqual(stat.S_IMODE(left.stat().st_mode), 0o600)
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertEqual(alias.read_text(), "old")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         sorted(["alias.json", "report.json", residue[0]]))

    def test_symlink_and_nonregular_target_refusal(self):
        original = self.root / "original"
        original.write_text("unchanged")
        link = self.root / "link"
        link.symlink_to(original)
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        directory = self.root / "directory"
        directory.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        cases = (("symlink", link, {}, ValueError), ("fifo", fifo, {}, ValueError),
                 ("directory", directory, {}, ValueError),
                 ("symlink_ancestor", alias / "new" / "report", {"create_parents": True}, OSError))
        for label, path, options, expected in cases:
            with self.subTest(target=label):
                before = sorted(entry.name for entry in self.root.iterdir())
                tracker = RealTracker()
                raised, leaked = self.real(tracker, path, "bad", **options)
                self.observe("symlink_and_nonregular_target_refusal",
                             {"target": label, "acquired": tracker.acquired, "closed": tracker.closed,
                              "leaked": leaked, "raised": repr(raised)})
                self.assertIsInstance(raised, expected)
                self.assertEqual(leaked, [])
                self.assertEqual(sorted(tracker.acquired), sorted(tracker.closed))
                self.assertEqual(tracker.unlinks, [])
                self.assertEqual(sorted(entry.name for entry in self.root.iterdir()), before)
                self.assertEqual(self.residue(), [])
                self.assertEqual(self.residue(directory), [])
        self.assertEqual(original.read_text(), "unchanged")
        self.assertTrue(link.is_symlink())
        self.assertTrue(stat.S_ISFIFO(os.lstat(fifo).st_mode))
        self.assertFalse((self.root / "new").exists())


if __name__ == "__main__":
    unittest.main()
