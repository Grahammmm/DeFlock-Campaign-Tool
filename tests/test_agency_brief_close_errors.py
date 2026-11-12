"""RF1651-4: an OSError reported while closing build_brief's output-directory descriptors keeps publication state.

Synthetic data only. Faults forward the real close first, then raise one EIO carrying private canaries.
"""
import contextlib
import os
import shutil
import stat
import unittest
from datetime import datetime
from unittest import mock

from campaign_tool import agency_brief
from campaign_tool.agency_brief import BriefError
from tests import test_agency_brief as base

HTML, TXT, POLICE, NOW, SENTINEL = base.HTML, base.TXT, base.POLICE, base.NOW, base.SENTINEL
RELEASE = "kit/briefs: output directory descriptor release failed"
INDICATOR = "; output directory descriptor release failed"
RETRY = ": check the files and run the same command again with --replace"
BOTH = "; both files were written: kit/briefs/" + HTML + ", kit/briefs/" + TXT + RETRY
INCOMPLETE = ("; kit/briefs/" + HTML + " was already written, so the pair is incomplete (or mixed with an older twin)"
              + RETRY)
SETUP = ("kit/briefs: output directory setup could not be flushed; crash durability is unconfirmed;"
         " no brief files were written")
CANARY = SENTINEL + "-CLOSE\nSYNTH-ÜNÏ-\u202e-PRIVATE"


class CloseCase(base.BriefCase):
    def layout(self, kind):
        shutil.rmtree(self.root / "kit", ignore_errors=True)
        briefs = self.root / "kit" / "briefs"
        if kind in ("both", "replace"):
            briefs.mkdir(parents=True, mode=0o755)
        if kind == "replace":
            for name in (HTML, TXT):
                (briefs / name).write_bytes(b"OLD " + name.encode())
        return briefs

    def healthy_pair(self):
        shutil.rmtree(self.root / "kit", ignore_errors=True)
        self.brief()
        folder = self.root / "kit" / "briefs"
        return {name: (folder / name).read_bytes() for name in (HTML, TXT)}

    @contextlib.contextmanager
    def closing(self, fail=(), error=None, sentinel=None):
        """Patch os.close: forward the real close, then fail owned output directories named in fail.

        Armed by the first output-directory acquisition, so earlier input-loader closes are never touched.
        Yields a log of (label, descriptor) for every output-directory close attempt.
        """
        real_close, real_subdir = os.close, agency_brief._subdir
        armed, log = [False], []

        def subdir(parent, name):
            armed[0] = True
            return real_subdir(parent, name)

        def label(fd):
            try:
                info = os.fstat(fd)
            except OSError:
                return None
            if not stat.S_ISDIR(info.st_mode):
                return None
            key = (info.st_dev, info.st_ino)
            for name, path in (("root", self.root), ("kit", self.root / "kit"), ("briefs", self.root / "kit" / "briefs")):
                try:
                    found = path.stat()
                except OSError:
                    continue
                if key == (found.st_dev, found.st_ino):
                    return name
            return None

        def close(fd):
            name = label(fd) if armed[0] else None
            real_close(fd)
            if name is None:
                return
            log.append((name, fd))
            if sentinel is not None and not sentinel and name in fail:
                sentinel.append(os.open(os.devnull, os.O_RDONLY))  # may take the number just released
            if name in fail:
                raise error or OSError(5, CANARY + " " + str(self.root))

        with mock.patch.object(agency_brief, "_subdir", side_effect=subdir), mock.patch.object(os, "close", side_effect=close):
            yield log

    @contextlib.contextmanager
    def fsync_fault(self, kind):
        """kind: root/kit setup flush, file1/file2 data flush, dir1/dir2 briefs directory flush, link1/link2 publication."""
        real_fsync, real_link = os.fsync, os.link
        calls = {"file": 0, "dir": 0, "link": 0}

        def where(fd):
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                return "file"
            for name, path in (("root", self.root), ("kit", self.root / "kit"), ("briefs", self.root / "kit" / "briefs")):
                try:
                    found = path.stat()
                except OSError:
                    continue
                if (info.st_dev, info.st_ino) == (found.st_dev, found.st_ino):
                    return name
            return None

        def fsync(fd):
            place = where(fd)
            if place in ("file", "briefs"):
                counter = "file" if place == "file" else "dir"
                calls[counter] += 1
                if kind == {"file": "file", "dir": "dir"}[counter] + str(calls[counter]):
                    raise OSError(5, CANARY)
            elif place == kind:
                raise OSError(5, CANARY)
            return real_fsync(fd)

        def link(*args, **kwargs):
            calls["link"] += 1
            if kind == f"link{calls['link']}":
                raise OSError(5, CANARY)
            return real_link(*args, **kwargs)

        with mock.patch.object(os, "fsync", side_effect=fsync), mock.patch.object(os, "link", side_effect=link):
            yield

    def cli(self, replace=False):
        with mock.patch.object(agency_brief, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            return base.run("agency-brief", "--directory", self.root, "--agency", POLICE, *(["--replace"] if replace else []))

    def refusal(self):
        with self.assertRaises(BriefError) as caught:
            self.brief(replace=self.replace)
        message = str(caught.exception)
        self.assertNotIn(SENTINEL, message)
        self.assertNotIn(str(self.tmp), message)
        self.assertNotIn("Errno", message)
        self.assertIsNone(caught.exception.__cause__)
        return message

    replace = False


class DirectoryCloseAfterSuccessTests(CloseCase):
    def test_each_directory_close_error_after_success_reports_both_written(self):
        for layout in ("neither", "both", "replace"):
            self.replace = layout == "replace"
            expected = self.healthy_pair()
            for fail in (("root",), ("kit",), ("briefs",), ("briefs", "kit", "root")):
                with self.subTest(layout=layout, fail=fail):
                    briefs = self.layout(layout)
                    with self.closing(fail) as log:
                        message = self.refusal()
                    self.assertEqual(message, RELEASE + BOTH)
                    self.assertEqual({n: (briefs / n).read_bytes() for n in (HTML, TXT)}, expected)
                    for name in (HTML, TXT):
                        self.assertEqual(stat.S_IMODE((briefs / name).stat().st_mode), 0o600)
                    self.assertEqual(sorted(name for name, _ in log), ["briefs", "kit", "root"])  # each owner once
                    self.assertEqual([name for name, _ in log], ["briefs", "kit", "root"])  # newest first
                    for fragment in ("incomplete", "could not be written", "durability"):
                        self.assertNotIn(fragment, message)


class PrimaryStateThroughCloseTests(CloseCase):
    def test_root_close_error_preserves_first_and_second_twin_failure_state(self):
        for layout in ("neither", "replace"):
            self.replace = layout == "replace"
            expected = self.healthy_pair()
            for kind in ("file1", "file2", "link1", "link2"):
                if kind.startswith("link") and layout == "replace":
                    continue  # replacement publishes with os.replace, not os.link
                with self.subTest(layout=layout, kind=kind):
                    briefs = self.layout(layout)
                    twin = 1 if kind.endswith("1") else 2
                    phase = "file data write/flush" if kind.startswith("file") else "target publication"
                    with self.fsync_fault(kind), self.closing(("root",)):
                        message = self.refusal()
                    name = HTML if twin == 1 else TXT
                    core = f"kit/briefs/{name} could not be written ({phase} failed)" + INDICATOR
                    self.assertEqual(message, core + ("" if twin == 1 else INCOMPLETE))
                    self.assertEqual(message.count("incomplete"), twin - 1)
                    self.assertNotIn("both files were written", message)
                    old = {n: b"OLD " + n.encode() for n in (HTML, TXT)}
                    if twin == 1:
                        found = {p.name: p.read_bytes() for p in briefs.iterdir()}
                        self.assertEqual(found, old if layout == "replace" else {})
                    else:
                        self.assertEqual((briefs / HTML).read_bytes(), expected[HTML])
                        if layout == "replace":
                            self.assertEqual((briefs / TXT).read_bytes(), old[TXT])
                        else:
                            self.assertFalse((briefs / TXT).exists())
                    self.assertFalse(list(briefs.glob(".*.tmp")))

    def test_postpublication_fsync_error_survives_inner_and_root_close_errors(self):
        for layout in ("neither", "replace"):
            self.replace = layout == "replace"
            expected = self.healthy_pair()
            for kind in ("dir1", "dir2"):
                for fail in (("briefs",), ("root",), ("briefs", "kit", "root")):
                    with self.subTest(layout=layout, kind=kind, fail=fail):
                        briefs = self.layout(layout)
                        with self.fsync_fault(kind), self.closing(fail) as log:
                            message = self.refusal()
                        name = HTML if kind == "dir1" else TXT
                        core = (f"kit/briefs/{name} was written; directory flush failed;"
                                " durability could not be confirmed" + INDICATOR)
                        self.assertEqual(message, core + (INCOMPLETE if kind == "dir1" else BOTH))
                        self.assertEqual(message.count(INDICATOR), 1)
                        self.assertEqual((briefs / HTML).read_bytes(), expected[HTML])
                        if kind == "dir2":
                            self.assertEqual((briefs / TXT).read_bytes(), expected[TXT])
                        self.assertEqual(sorted(n for n, _ in log), ["briefs", "kit", "root"])

    def test_postpublication_cleanup_failure_is_kept_through_close_error(self):
        expected = self.healthy_pair()
        briefs = self.layout("neither")
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if str(path).endswith(".tmp"):
                raise OSError(5, CANARY)
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(os, "unlink", side_effect=unlink), self.closing(("root",)):
            message = self.refusal()
        temporary = f".{HTML}.{os.getpid()}.tmp"
        self.assertIn(f"kit/briefs/{HTML} was written; temporary cleanup failed", message)
        self.assertIn(f"temporary cleanup failed for kit/briefs/{temporary}; check retained temporary" + INDICATOR, message)
        self.assertEqual(message.count("incomplete"), 1)
        self.assertEqual((briefs / HTML).read_bytes(), expected[HTML])

    def test_setup_error_survives_child_and_root_release_errors(self):
        for layout in ("neither", "both", "replace"):
            for kind, fail, owned in (("root", ("kit", "root"), ["kit", "root"]),
                                      ("kit", ("briefs", "kit", "root"), ["briefs", "kit", "root"])):
                with self.subTest(layout=layout, kind=kind):
                    self.replace = layout == "replace"
                    briefs = self.layout(layout)
                    (self.root / "kit").mkdir(exist_ok=True)
                    unrelated = self.root / "kit" / "unrelated.txt"
                    unrelated.write_bytes(b"UNRELATED")
                    unrelated.chmod(0o640)
                    before = base.tree(self.root / "kit")
                    with self.fsync_fault(kind), self.closing(fail) as log:
                        message = self.refusal()
                    self.assertEqual(message, SETUP + INDICATOR)
                    self.assertEqual(base.tree(self.root / "kit"), before)
                    self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode), 0o640)
                    self.assertEqual([name for name, _ in log], owned)  # no briefs close when never acquired


class DescriptorOwnershipTests(CloseCase):
    def test_multiple_close_reports_never_retry_recycled_descriptor(self):
        expected = self.healthy_pair()
        briefs = self.layout("neither")
        with self.closing(("briefs", "kit", "root")) as log:
            message = self.refusal()
        self.assertEqual(message, RELEASE + BOTH)
        self.assertEqual(message.count(INDICATOR) + message.count(RELEASE), 1)
        self.assertEqual(len({fd for _, fd in log}), 3)  # one attempt per ownership generation
        self.assertEqual({n: (briefs / n).read_bytes() for n in (HTML, TXT)}, expected)

    def test_focused_recycled_number_is_not_closed_again(self):
        self.healthy_pair()
        self.layout("neither")
        sentinel = []
        before = len(os.listdir("/proc/self/fd"))
        try:
            with self.closing(("briefs", "kit", "root"), sentinel=sentinel) as log:
                self.refusal()
            self.assertTrue(sentinel)
            os.fstat(sentinel[0])  # still ours: nothing closed the recycled number
            attempts = [fd for _, fd in log]
            self.assertEqual(len(attempts), len(set(attempts)))
            self.assertEqual(attempts.count(sentinel[0]), 1 if sentinel[0] in attempts else 0)
        finally:
            for fd in sentinel:
                os.close(fd)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_non_domain_primary_exception_survives_close_oserror(self):
        self.layout("neither")
        for primary in (RuntimeError("synthetic"), KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(primary=type(primary).__name__):
                def fail(*args, **kwargs):
                    raise primary
                with mock.patch.object(agency_brief, "_write_file", side_effect=fail), \
                        self.closing(("briefs", "kit", "root")) as log:
                    with self.assertRaises(type(primary)) as caught:
                        self.brief()
                self.assertIs(caught.exception, primary)
                self.assertEqual([name for name, _ in log], ["briefs", "kit", "root"])

    def test_close_hook_programming_error_is_not_a_release_report(self):
        self.layout("neither")
        boom = RuntimeError("synthetic hook bug")
        with self.closing(("briefs",), error=boom) as log:
            with self.assertRaises(RuntimeError) as caught:
                self.brief()
        self.assertIs(caught.exception, boom)
        self.assertEqual(sorted(name for name, _ in log), ["briefs", "kit", "root"])  # the rest were still released

    def test_descriptors_are_not_leaked_after_close_errors(self):
        self.healthy_pair()
        self.layout("neither")
        before = len(os.listdir("/proc/self/fd"))
        with self.closing(("briefs", "kit", "root")):
            self.refusal()
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)


class CliAndRecoveryTests(CloseCase):
    def test_actual_cli_private_refusal_and_explicit_recovery(self):
        expected = self.healthy_pair()
        briefs = self.layout("neither")
        with self.closing(("root",)):
            code, out, err = self.cli()
        self.assertEqual((code, out), (1, ""))
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertEqual(lines[0], "Stopped: " + RELEASE + BOTH)
        for private in (SENTINEL, "ÜNÏ", str(self.tmp), "Errno", "Traceback", "Fix:"):
            self.assertNotIn(private, err)
        self.assertEqual({n: (briefs / n).read_bytes() for n in (HTML, TXT)}, expected)
        times = {n: (briefs / n).stat().st_mtime_ns for n in (HTML, TXT)}
        modes = {n: stat.S_IMODE((briefs / n).stat().st_mode) for n in (HTML, TXT)}
        self.assertEqual(modes, {HTML: 0o600, TXT: 0o600})
        code, out, err = self.cli()  # fault removed, no --replace: refuses and keeps both
        self.assertEqual((code, out), (1, ""))
        self.assertIn("already exists; both files were kept unchanged", err)
        self.assertEqual({n: (briefs / n).stat().st_mtime_ns for n in (HTML, TXT)}, times)
        (briefs / TXT).write_bytes(b"independent healthy bytes")
        code, out, err = self.cli(True)  # explicit recovery, no rollback of what was published
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Private brief written.", out)
        self.assertEqual({n: (briefs / n).read_bytes() for n in (HTML, TXT)}, expected)

    def test_no_fault_bytes_modes_and_prior_diagnostics_unchanged(self):
        expected = self.healthy_pair()
        for layout in ("neither", "both", "replace"):
            with self.subTest(layout=layout):
                briefs = self.layout(layout)
                before = len(os.listdir("/proc/self/fd"))
                with self.closing(()) as log:
                    result = self.brief(replace=layout == "replace")
                self.assertEqual(result, {"written": ["kit/briefs/" + HTML, "kit/briefs/" + TXT], "flags": []})
                self.assertEqual({n: (briefs / n).read_bytes() for n in (HTML, TXT)}, expected)
                self.assertEqual([name for name, _ in log], ["briefs", "kit", "root"])
                self.assertEqual(len(os.listdir("/proc/self/fd")), before)
                for name in (HTML, TXT):
                    self.assertEqual(stat.S_IMODE((briefs / name).stat().st_mode), 0o600)
        # prior diagnostics without a close fault are byte-compatible
        self.layout("neither")
        with self.fsync_fault("kit"):
            self.assertEqual(self.refusal(), SETUP)
        self.layout("neither")
        with self.fsync_fault("file2"):
            self.assertEqual(self.refusal(), f"kit/briefs/{TXT} could not be written (file data write/flush failed)"
                             + INCOMPLETE)
        self.layout("replace")
        self.replace = False
        self.assertEqual(self.refusal(), f"kit/briefs/{HTML} already exists; both files were kept unchanged")
        self.layout("neither")
        with self.fsync_fault("dir2"):
            self.assertEqual(self.refusal(), f"kit/briefs/{TXT} was written; directory flush failed;"
                             " durability could not be confirmed" + BOTH)


if __name__ == "__main__":
    unittest.main()
