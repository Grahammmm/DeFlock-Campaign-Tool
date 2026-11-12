"""kit_export.write_rows anchors the private CSV to its admitted parent directory. Offline, synthetic."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_export, kit_fees, kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402
from tests.test_kit_status_csv import run  # noqa: E402

REAL_OPEN, REAL_FSYNC, REAL_CLOSE = os.open, os.fsync, os.close


class Boom(OSError):
    pass


def fd_count():
    return len(os.listdir("/proc/self/fd"))


class ParentScopeTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-parent-scope-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.private = self.tmp / "private"
        self.private.mkdir()
        self.parked = self.tmp / "parked"
        self.target = self.private / "t.csv"

    def swap_parent_for_link(self, destination):
        os.rename(self.private, self.parked)
        os.symlink(destination, self.private)

    def swap_parent_for_dir(self):
        os.rename(self.private, self.parked)
        self.private.mkdir()
        (self.private / "sentinel.txt").write_bytes(b"foreign")

    def write(self):
        kit_export.write_rows(self.target, "t.csv", ("a",), [["1"]])

    # --- the real CLI regression ---
    def test_status_csv_late_parent_links_to_public_and_content_refuse(self):
        root = self.tmp / "campaign"
        code, _, err = run("init", "--directory", root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        self.assertEqual(run("kit", "--directory", root)[0], 0)
        ready_drafts(root)
        agency = sorted((root / "kit" / "requests").glob("*.md"))[0].stem
        code, _, err = run("kit-sent", "--directory", root, "--agency", agency, "--date", "2026-03-02",
                           "--channel", "email")
        self.assertEqual(code, 0, err)
        real_status = kit_track.status
        for folder in ("public", "content"):
            with self.subTest(folder=folder):
                (root / folder).mkdir(exist_ok=True)
                private = self.tmp / f"priv-{folder}"
                private.mkdir()

                def status(*args, **kwargs):
                    report = real_status(*args, **kwargs)
                    os.rename(private, self.tmp / f"parked-{folder}")
                    os.symlink(root / folder, private)
                    return report

                with mock.patch.object(kit_track, "status", status):
                    code, out, err = run("kit-status", "--directory", root, "--csv", private / "t.csv")
                self.assertEqual(code, 1, out)
                self.assertIn("csv output parent changed", err)
                self.assertNotIn("Private file", out)
                self.assertEqual(list((root / folder).glob("*.csv")), [])
                self.assertEqual(list((self.tmp / f"parked-{folder}").iterdir()), [])

    # --- admission ---
    def test_nested_ancestor_link_dangling_link_fifo_and_missing_parent_refuse(self):
        real = self.tmp / "real"
        (real / "inner").mkdir(parents=True)
        os.symlink(real, self.tmp / "alias")
        os.symlink(self.tmp / "nowhere", self.tmp / "dangling")
        os.mkfifo(self.tmp / "fifo")
        for parent in ("alias/inner", "dangling", "fifo", "missing"):
            with self.subTest(parent=parent):
                with self.assertRaisesRegex(ValueError, "csv output parent changed or cannot be opened"):
                    kit_export.write_rows(self.tmp / parent / "t.csv", "t.csv", ("a",), [])
        self.assertEqual(list((real / "inner").iterdir()), [])

    # --- substitution after admission ---
    def test_parent_substitution_after_admission_never_redirects_create_or_cleanup(self):
        def create_swap(path, flags, *args, **kwargs):
            if flags & os.O_EXCL and not getattr(self, "swapped", False):
                self.swapped = True
                self.swap_parent_for_dir()
            return REAL_OPEN(path, flags, *args, **kwargs)

        with mock.patch.object(os, "open", side_effect=create_swap):
            with self.assertRaisesRegex(ValueError, "csv output changed or cannot be verified"):
                self.write()
        self.assertEqual(sorted(p.name for p in self.private.iterdir()), ["sentinel.txt"])
        self.assertEqual(list(self.parked.iterdir()), [])  # own partial removed from the held original

    def test_fault_after_parent_rename_cleans_only_own_partial_in_held_original(self):
        def fsync(fd):
            self.swap_parent_for_dir()
            raise Boom("fsync-fault")

        with mock.patch.object(os, "fsync", side_effect=fsync):
            with self.assertRaises(Boom):
                self.write()
        self.assertEqual(sorted(p.name for p in self.private.iterdir()), ["sentinel.txt"])
        self.assertEqual(list(self.parked.iterdir()), [])

    def test_final_parent_reacquisition_refuses_before_success(self):
        for name, swap in (("dir", self.swap_parent_for_dir),
                           ("link", lambda: self.swap_parent_for_link(self.tmp))):
            with self.subTest(swap=name):
                def fsync(fd):
                    REAL_FSYNC(fd)
                    swap()

                with mock.patch.object(os, "fsync", side_effect=fsync):
                    with self.assertRaisesRegex(ValueError, "csv output changed or cannot be verified"):
                        self.write()
                self.assertFalse(os.path.lexists(self.target))
                os.unlink(self.private) if self.private.is_symlink() else None
                if self.private.exists():
                    for p in self.private.iterdir():
                        p.unlink()
                    self.private.rmdir()
                for p in self.parked.iterdir():
                    p.unlink()
                self.parked.rmdir()
                self.private.mkdir()

    def test_reacquisition_oserror_hides_marker_and_non_oserror_propagates(self):
        calls = []
        real = kit_export._open_parent

        def second_fails(error):
            def opener(path):
                calls.append(1)
                if len(calls) > 1:
                    raise error
                return real(path)
            return opener

        with mock.patch.object(kit_export, "_open_parent", second_fails(PermissionError("private-marker"))):
            with self.assertRaises(ValueError) as ctx:
                self.write()
        self.assertNotIn("private-marker", str(ctx.exception))
        self.assertFalse(os.path.lexists(self.target))
        calls.clear()
        before = fd_count()
        with mock.patch.object(kit_export, "_open_parent", second_fails(RuntimeError("sentinel"))):
            with self.assertRaises(RuntimeError):
                self.write()
        self.assertEqual(fd_count(), before)
        self.assertFalse(os.path.lexists(self.target))

    # --- combined faults: the active error is never replaced by a release failure ---
    def fault_harness(self, fail_open=None, fail_close=lambda fd, opened: True):
        """Patch os.open/os.close: the open of the component named ``private`` raises ``fail_open`` (if given);
        every close really releases the descriptor, records it, and then raises OSError when ``fail_close``."""
        opened, closed = [], []

        def opener(path, flags, *args, **kwargs):
            if fail_open is not None and path == "private":
                raise fail_open
            fd = REAL_OPEN(path, flags, *args, **kwargs)
            opened.append(fd)
            return fd

        def closer(fd):
            REAL_CLOSE(fd)
            closed.append(fd)
            if fail_close(fd, opened):
                raise OSError("close-marker")

        return opened, closed, opener, closer

    def test_acquisition_error_survives_release_failure_on_every_acquired_ancestor(self):
        before = fd_count()
        for error in (KeyboardInterrupt(), RuntimeError("sentinel")):
            with self.subTest(error=type(error).__name__):
                opened, closed, opener, closer = self.fault_harness(fail_open=error)
                with mock.patch.object(os, "open", side_effect=opener), mock.patch.object(os, "close", side_effect=closer):
                    with self.assertRaises(type(error)) as ctx:
                        kit_export._open_parent(self.target)
                self.assertIs(ctx.exception, error)
                self.assertGreater(len(opened), 1)
                self.assertEqual(closed, opened)  # every acquired descriptor was still released
                self.assertEqual(fd_count(), before)

    def test_acquisition_oserror_with_release_failure_still_refuses_sanitized(self):
        before = fd_count()
        opened, closed, opener, closer = self.fault_harness(fail_open=PermissionError("private-marker"))
        with mock.patch.object(os, "open", side_effect=opener), mock.patch.object(os, "close", side_effect=closer):
            with self.assertRaises(ValueError) as ctx:
                kit_export._open_parent(self.target)
        self.assertEqual(str(ctx.exception), kit_export._PARENT_ERROR)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)
        self.assertNotIn("marker", str(ctx.exception))
        self.assertEqual(closed, opened)
        self.assertGreater(len(opened), 1)
        self.assertEqual(fd_count(), before)

    def test_final_release_failures_keep_sanitized_refusal_and_close_everything(self):
        before = fd_count()
        opened, closed, opener, closer = self.fault_harness()  # every close fails, the held parent included
        with mock.patch.object(os, "open", side_effect=opener), mock.patch.object(os, "close", side_effect=closer):
            with self.assertRaises(ValueError) as ctx:
                kit_export._open_parent(self.target)
        self.assertEqual(str(ctx.exception), kit_export._PARENT_ERROR)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual(sorted(closed), sorted(opened))
        self.assertEqual(fd_count(), before)

    def test_final_release_interrupt_survives_failing_parent_close(self):
        before = fd_count()
        interrupt = KeyboardInterrupt()
        opened, closed, opener, closer = self.fault_harness(fail_close=lambda fd, seen: fd == seen[-1])

        def interrupting_closer(fd):
            closer(fd)
            if fd == opened[1]:  # the first intermediate ancestor
                raise interrupt

        with mock.patch.object(os, "open", side_effect=opener), \
                mock.patch.object(os, "close", side_effect=interrupting_closer):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                kit_export._open_parent(self.target)
        self.assertIs(ctx.exception, interrupt)
        self.assertEqual(sorted(closed), sorted(opened))  # later ancestors and the parent were still released
        self.assertEqual(fd_count(), before)

    def test_reverification_error_survives_failing_release_of_the_check_descriptor(self):
        before = fd_count()
        for error, expected in ((KeyboardInterrupt(), KeyboardInterrupt),
                                (OSError("fstat-marker"), ValueError)):
            with self.subTest(error=type(error).__name__):
                seen = []

                def same_dir(fd, identity):
                    seen.append(fd)
                    raise error

                def closer(fd):
                    REAL_CLOSE(fd)
                    if seen and fd == seen[0]:
                        raise OSError("close-marker")

                with mock.patch.object(kit_export, "_same_dir", side_effect=same_dir), \
                        mock.patch.object(os, "close", side_effect=closer):
                    with self.assertRaises(expected) as ctx:
                        self.write()
                if expected is KeyboardInterrupt:
                    self.assertIs(ctx.exception, error)
                else:
                    self.assertNotIn("marker", str(ctx.exception))
                self.assertFalse(os.path.lexists(self.target))
                self.assertEqual(fd_count(), before)

    # --- leaf protections survive ---
    def test_leaf_replacement_protections_survive_parent_anchoring(self):
        def fsync(fd):
            os.rename(self.target, self.private / "moved.csv")
            self.target.write_bytes(b"keep")
            raise Boom("fsync-fault")

        with mock.patch.object(os, "fsync", side_effect=fsync):
            with self.assertRaises(Boom):
                self.write()
        self.assertEqual(self.target.read_bytes(), b"keep")
        self.assertTrue((self.private / "moved.csv").exists())

    # --- descriptors ---
    def test_all_directory_descriptors_release_once_across_failures(self):
        class Stop(BaseException):
            pass

        before = fd_count()
        for target_fn, error in ((os, "fsync"), (os, "fchmod"), (os, "fdopen")):
            with self.subTest(fault=error):
                with mock.patch.object(target_fn, error, side_effect=Boom("fault")):
                    with self.assertRaises(Boom):
                        self.write()
                self.assertEqual(fd_count(), before)
                self.assertFalse(os.path.lexists(self.target))
        with mock.patch.object(os, "fsync", side_effect=Stop()):
            with self.assertRaises(Stop):
                self.write()
        self.assertEqual(fd_count(), before)
        self.write()
        self.assertEqual(fd_count(), before)

    def test_directory_fstat_fault_closes_parent(self):
        before = fd_count()
        with mock.patch.object(os, "fstat", side_effect=Boom("fstat-fault")):
            with self.assertRaises(Boom):
                self.write()
        self.assertEqual(fd_count(), before)
        self.assertFalse(os.path.lexists(self.target))

    # --- unchanged behaviour ---
    def test_fees_writer_shares_boundary(self):
        report = {"requests": [], "totals": {"unpaid": "$0", "quoted": "$0", "paid": "$0"}, "note": "n"}
        real = kit_export._target

        def target(root, out):
            found = real(root, out)
            self.swap_parent_for_link(self.tmp)
            return found

        with mock.patch.object(kit_export, "_target", target):
            with self.assertRaisesRegex(ValueError, "csv output parent changed"):
                kit_fees.write_csv(self.tmp / "campaign", self.target, report)
        self.assertEqual([p for p in self.tmp.glob("*.csv")], [])

    def test_healthy_bytes_modes_and_no_overwrite_unchanged(self):
        kit_export.write_rows(self.target, "t.csv", ("a", "é"), [["=1", "ü"]])
        self.assertEqual(self.target.read_bytes(), "a,é\r\n=1,ü\r\n".encode())
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        with self.assertRaisesRegex(ValueError, "exists; choose a new file name"):
            self.write()
        self.assertEqual(self.target.read_bytes(), "a,é\r\n=1,ü\r\n".encode())
        kit_export.write_rows(self.private / "fresh.csv", "fresh.csv", ("a",), [])
        os.symlink(self.target, self.private / "link.csv")
        with self.assertRaisesRegex(ValueError, "exists"):
            kit_export.write_rows(self.private / "link.csv", "link.csv", ("a",), [])


if __name__ == "__main__":
    unittest.main()
