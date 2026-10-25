"""FA93: the portal pointer report refuses symlinked input/output ancestors.

tools/portal_reconcile.py reads two JSON inputs and may create one new report
file. Before FA93 it opened each path with O_NOFOLLOW on the leaf only, so a
symlinked parent directory was silently followed. These tests exercise the
real read_json/write_new functions and the real CLI on private synthetic
trees under the test temporary directory and prove that:

  * an alias at the root side, in the middle or as the immediate parent of
    the leaf is refused before any descriptor on the alias target is opened
    (so no target byte is read and no target output is created);
  * explicit parent traversal ("..") is refused instead of being resolved;
  * ordinary absolute and relative paths (including a "./" prefix) work;
  * operations stay bound to the already-opened directory descriptor when an
    ancestor is swapped for a symlink mid-operation (deterministic seam);
  * FIFOs and directories refuse without blocking, existing output is never
    overwritten, missing output parents are not created, the byte cap is
    unchanged, descriptors are closed and diagnostics stay counts-only.

GateControl restores leaf-only opening in a disposable copy of the module and
shows the intermediate-alias test detects it while the ordinary positive
still passes. Inputs are copies of the repository's synthetic
portal_reconcile fixtures (example.test hosts, placeholder hashes).
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import portal_reconcile

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "portal_reconcile.py"
FIXTURES = ROOT / "tests" / "fixtures" / "portal_reconcile"
OBSERVATION_BYTES = (FIXTURES / "observations.json").read_bytes()
QUEUE_BYTES = (FIXTURES / "queue-export.json").read_bytes()
REAL_ROWS = json.loads(OBSERVATION_BYTES)["observations"]
# The alias target holds a different, equally valid producer-shaped document so
# a read through an alias is distinguishable from a read of the real file.
TARGET_DOCUMENT = {"schema": portal_reconcile.OBSERVATION_SCHEMA, "observations": [REAL_ROWS[0]]}
TARGET_BYTES = json.dumps(TARGET_DOCUMENT, sort_keys=True).encode()
CLI_TIMEOUT = 60
EXPECTED_CAP = 64 * 1024 * 1024


def fd_count():
    return len(os.listdir("/proc/self/fd"))


@contextlib.contextmanager
def opened_inodes():
    """Record (st_dev, st_ino) of every descriptor os.open returns."""
    real_open = os.open
    seen = set()

    def recording_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        info = os.fstat(fd)
        seen.add((info.st_dev, info.st_ino))
        return fd

    with mock.patch.object(os, "open", recording_open):
        yield seen


def inode(path):
    info = os.lstat(path)
    return info.st_dev, info.st_ino


def snapshot(directory):
    """Listing plus bytes of every regular file below directory (no follow)."""
    state = {}
    for current, dirs, files in os.walk(directory):
        dirs.sort()
        for name in sorted(dirs + files):
            path = os.path.join(current, name)
            info = os.lstat(path)
            rel = os.path.relpath(path, directory)
            if stat.S_ISREG(info.st_mode):
                state[rel] = ("file", Path(path).read_bytes())
            elif stat.S_ISLNK(info.st_mode):
                state[rel] = ("link", os.readlink(path))
            else:
                state[rel] = ("other", stat.S_IFMT(info.st_mode))
    return state


class Behavior(unittest.TestCase):
    """A1: real read_json/write_new/CLI on private synthetic trees."""

    MOD = portal_reconcile

    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="fa93-")
        self.addCleanup(holder.cleanup)
        # realpath: the private tree itself must have no aliased ancestor.
        self.base = Path(os.path.realpath(holder.name))
        for top in ("real", "target"):
            leaf_dir = self.base / top / "a" / "b"
            leaf_dir.mkdir(parents=True)
            (leaf_dir / "queue.json").write_bytes(QUEUE_BYTES)
        (self.base / "real" / "a" / "b" / "obs.json").write_bytes(OBSERVATION_BYTES)
        (self.base / "target" / "a" / "b" / "obs.json").write_bytes(TARGET_BYTES)
        self.target_file = self.base / "target" / "a" / "b" / "obs.json"
        self.target_inodes = {inode(self.target_file), inode(self.base / "target" / "a" / "b"),
                              inode(self.base / "target" / "a"), inode(self.base / "target")}

    # --- helpers -------------------------------------------------------------

    def read(self, path):
        return self.MOD.read_json(path, self.MOD.OBSERVATION_SCHEMA, "observations")

    def assert_read_refused_without_target_access(self, path):
        before = snapshot(self.base / "target")
        fds = fd_count()
        with opened_inodes() as seen:
            try:
                self.read(path)
                raised = None
            except self.MOD.InputError as exc:
                raised = exc
        # Target access is checked first: it is the defect being guarded.
        self.assertFalse(seen & self.target_inodes, "a descriptor on the alias target was opened")
        self.assertIsInstance(raised, self.MOD.InputError, "aliased input was not refused")
        self.assertEqual(str(raised), "input_unreadable")
        self.assertEqual(snapshot(self.base / "target"), before)
        self.assertEqual(fd_count(), fds)

    def assert_write_refused_without_target_access(self, path):
        before = snapshot(self.base / "target")
        real_before = snapshot(self.base / "real")
        fds = fd_count()
        with opened_inodes() as seen:
            try:
                self.MOD.write_new(path, "{}\n")
                raised = None
            except OSError as exc:
                raised = exc
        self.assertFalse(seen & self.target_inodes, "a descriptor on the alias target was opened")
        self.assertIsInstance(raised, OSError, "aliased output was not refused")
        self.assertEqual(snapshot(self.base / "target"), before)
        self.assertEqual(snapshot(self.base / "real"), real_before)
        self.assertEqual(fd_count(), fds)

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *args], capture_output=True, text=True,
                              cwd=cwd or self.base, timeout=CLI_TIMEOUT, check=False)

    def assert_cli_error(self, result, code):
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, json.dumps({"error": code}) + "\n")
        self.assertNotIn(str(self.base), result.stderr)

    # --- ordinary positives ----------------------------------------------------

    def test_ordinary_directory_positive_read(self):
        fds = fd_count()
        self.assertEqual(self.read(str(self.base / "real" / "a" / "b" / "obs.json")), REAL_ROWS)
        self.assertEqual(self.read(self.base / "real" / "a" / "b" / "obs.json"), REAL_ROWS)
        self.assertEqual(fd_count(), fds)

    def test_relative_and_current_directory_prefix_positive(self):
        with contextlib.chdir(self.base):
            for path in ("real/a/b/obs.json", "./real/a/b/obs.json", "real/./a//b/obs.json"):
                with self.subTest(path=path):
                    self.assertEqual(self.read(path), REAL_ROWS)
            with contextlib.chdir(self.base / "real" / "a" / "b"):
                self.assertEqual(self.read("obs.json"), REAL_ROWS)
                self.assertEqual(self.read("./obs.json"), REAL_ROWS)
            self.MOD.write_new("./real/a/b/relative.json", "{\"ok\": 1}\n")
        out = self.base / "real" / "a" / "b" / "relative.json"
        self.assertEqual(out.read_text(), "{\"ok\": 1}\n")
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    def test_ordinary_write_creates_new_0600_file(self):
        out = self.base / "real" / "a" / "b" / "report.json"
        self.MOD.write_new(str(out), "{\"n\": 2}\n")
        self.assertEqual(out.read_bytes(), b"{\"n\": 2}\n")
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertTrue(stat.S_ISREG(os.lstat(out).st_mode))

    def test_cli_absolute_and_relative_positive(self):
        absolute = self.cli("--observations", str(self.base / "real/a/b/obs.json"),
                            "--queue-export", str(self.base / "real/a/b/queue.json"))
        self.assertEqual((absolute.returncode, absolute.stderr), (0, ""))
        expected = self.MOD.render(self.MOD.reconcile(REAL_ROWS, json.loads(QUEUE_BYTES)["items"]))
        self.assertEqual(absolute.stdout, expected)
        relative = self.cli("--observations", "./real/a/b/obs.json", "--queue-export", "real/a/b/queue.json",
                            "--output", "./real/a/b/cli-report.json")
        self.assertEqual((relative.returncode, relative.stdout, relative.stderr), (0, "", ""))
        self.assertEqual((self.base / "real/a/b/cli-report.json").read_text(), expected)

    # --- input aliases ---------------------------------------------------------

    def test_root_side_symlink_input_refused_before_target_access(self):
        os.symlink(self.base / "target", self.base / "rootlink")
        self.assert_read_refused_without_target_access(str(self.base / "rootlink" / "a" / "b" / "obs.json"))

    def test_intermediate_symlink_input_refused_before_target_access(self):
        os.symlink(self.base / "target" / "a", self.base / "real" / "mid")
        self.assert_read_refused_without_target_access(str(self.base / "real" / "mid" / "b" / "obs.json"))

    def test_leaf_parent_symlink_input_refused_before_target_access(self):
        os.symlink(self.base / "target" / "a" / "b", self.base / "real" / "a" / "near")
        self.assert_read_refused_without_target_access(str(self.base / "real" / "a" / "near" / "obs.json"))

    def test_leaf_symlink_input_refused_before_target_access(self):
        os.symlink(self.target_file, self.base / "real" / "a" / "b" / "leaf.json")
        self.assert_read_refused_without_target_access(str(self.base / "real" / "a" / "b" / "leaf.json"))

    def test_relative_first_component_symlink_input_refused(self):
        os.symlink(self.base / "target", self.base / "rootlink")
        with contextlib.chdir(self.base):
            for path in ("rootlink/a/b/obs.json", "./rootlink/a/b/obs.json"):
                with self.subTest(path=path):
                    self.assert_read_refused_without_target_access(path)

    def test_parent_traversal_input_refused_not_resolved(self):
        candidates = [str(self.base / "real" / "a" / ".." / "a" / "b" / "obs.json"),
                      str(self.base / "target" / ".." / "real" / "a" / "b" / "obs.json")]
        with contextlib.chdir(self.base / "real" / "a"):
            candidates += ["../a/b/obs.json", "b/../b/obs.json", "b/obs.json/..", "b/obs.json/."]
            for path in candidates:
                with self.subTest(path=path):
                    with opened_inodes() as seen:
                        with self.assertRaises(self.MOD.InputError) as caught:
                            self.read(path)
                    self.assertEqual(str(caught.exception), "input_unreadable")
                    self.assertFalse(seen & self.target_inodes)

    def test_cli_input_alias_keeps_input_unreadable_envelope(self):
        os.symlink(self.base / "target" / "a", self.base / "real" / "mid")
        queue = str(self.base / "real/a/b/queue.json")
        for observations in (str(self.base / "real/mid/b/obs.json"), "real/mid/b/obs.json",
                             "./real/a/../a/b/obs.json"):
            with self.subTest(observations=observations):
                self.assert_cli_error(self.cli("--observations", observations, "--queue-export", queue),
                                      "input_unreadable")
        self.assert_cli_error(self.cli("--observations", str(self.base / "real/a/b/obs.json"),
                                       "--queue-export", "real/mid/b/queue.json"), "input_unreadable")

    # --- output aliases ---------------------------------------------------------

    def test_root_side_symlink_output_refused_without_create(self):
        os.symlink(self.base / "target", self.base / "rootlink")
        self.assert_write_refused_without_target_access(str(self.base / "rootlink" / "a" / "b" / "out.json"))

    def test_intermediate_symlink_output_refused_without_create(self):
        os.symlink(self.base / "target" / "a", self.base / "real" / "mid")
        self.assert_write_refused_without_target_access(str(self.base / "real" / "mid" / "b" / "out.json"))

    def test_leaf_parent_symlink_output_refused_without_create(self):
        os.symlink(self.base / "target" / "a" / "b", self.base / "real" / "a" / "near")
        self.assert_write_refused_without_target_access(str(self.base / "real" / "a" / "near" / "out.json"))

    def test_leaf_symlink_output_refused_without_create(self):
        dangling = self.base / "target" / "a" / "b" / "fresh.json"
        os.symlink(dangling, self.base / "real" / "a" / "b" / "out.json")
        self.assert_write_refused_without_target_access(str(self.base / "real" / "a" / "b" / "out.json"))
        self.assertFalse(os.path.lexists(dangling))

    def test_parent_traversal_output_refused_without_create(self):
        real_before = snapshot(self.base)
        for path in (str(self.base / "real" / "a" / ".." / "a" / "b" / "out.json"),
                     str(self.base / "real" / "a" / "b" / "..")):
            with self.subTest(path=path):
                with self.assertRaises(OSError):
                    self.MOD.write_new(path, "{}\n")
        self.assertEqual(snapshot(self.base), real_before)

    def test_cli_output_alias_keeps_output_not_created_envelope(self):
        os.symlink(self.base / "target" / "a", self.base / "real" / "mid")
        before = snapshot(self.base / "target")
        for output in (str(self.base / "real/mid/b/out.json"), "./real/mid/b/out.json"):
            with self.subTest(output=output):
                result = self.cli("--observations", "real/a/b/obs.json", "--queue-export", "real/a/b/queue.json",
                                  "--output", output)
                self.assert_cli_error(result, "output_not_created")
        self.assertEqual(snapshot(self.base / "target"), before)

    # --- descriptor binding (deterministic ancestor swap) ----------------------

    def swap_for_alias(self, original, moved, alias_target):
        os.rename(original, moved)
        os.symlink(alias_target, original)

    def test_parent_swap_after_open_stays_bound_to_descriptor(self):
        real_b = self.base / "real" / "a" / "b"
        moved = self.base / "moved-b"
        checked = self.MOD._open_parent

        def swapping(path):
            opened = checked(path)
            self.swap_for_alias(real_b, moved, self.base / "target" / "a" / "b")
            return opened

        with mock.patch.object(self.MOD, "_open_parent", swapping), opened_inodes() as seen:
            rows = self.read(str(real_b / "obs.json"))
        self.assertEqual(rows, REAL_ROWS)
        self.assertNotIn(inode(self.target_file), seen)
        os.unlink(real_b)
        os.rename(moved, real_b)
        target_before = snapshot(self.base / "target")
        with mock.patch.object(self.MOD, "_open_parent", swapping):
            self.MOD.write_new(str(real_b / "swap-out.json"), "{\"bound\": true}\n")
        self.assertEqual((moved / "swap-out.json").read_text(), "{\"bound\": true}\n")
        self.assertEqual(snapshot(self.base / "target"), target_before)

    def test_intermediate_swap_during_traversal_stays_bound(self):
        real_a = self.base / "real" / "a"
        moved = self.base / "moved-a"
        child = self.MOD._open_child_dir
        swapped = []
        real_dir = inode(self.base / "real")

        def swapping(parent_fd, name):
            fd = child(parent_fd, name)
            info = os.fstat(parent_fd)
            if name == "a" and (info.st_dev, info.st_ino) == real_dir and not swapped:
                self.swap_for_alias(real_a, moved, self.base / "target" / "a")
                swapped.append(name)
            return fd

        target_before = snapshot(self.base / "target")
        with mock.patch.object(self.MOD, "_open_child_dir", swapping), opened_inodes() as seen:
            rows = self.read(str(real_a / "b" / "obs.json"))
        self.assertEqual(swapped, ["a"])
        self.assertEqual(rows, REAL_ROWS)
        self.assertFalse(seen & self.target_inodes)
        # Once swapped, the alias itself is refused on a fresh traversal.
        with self.assertRaises(self.MOD.InputError):
            self.read(str(real_a / "b" / "obs.json"))
        os.unlink(real_a)
        os.rename(moved, real_a)
        swapped.clear()
        with mock.patch.object(self.MOD, "_open_child_dir", swapping), opened_inodes() as seen:
            self.MOD.write_new(str(real_a / "b" / "bound-out.json"), "{}\n")
        self.assertEqual(swapped, ["a"])
        self.assertFalse(seen & self.target_inodes)
        self.assertEqual(snapshot(self.base / "target"), target_before)
        # Created through the bound descriptor: in the moved original directory.
        self.assertEqual((moved / "b" / "bound-out.json").read_text(), "{}\n")

    # --- special files, existing outputs, missing parents ---------------------

    def test_fifo_and_directory_inputs_refuse_without_blocking(self):
        leaf_dir = self.base / "real" / "a" / "b"
        os.mkfifo(leaf_dir / "pipe.json")
        os.mkfifo(self.base / "real" / "pipedir")
        queue = str(leaf_dir / "queue.json")
        for observations, code in ((str(leaf_dir / "pipe.json"), "input_not_regular_file"),
                                   (str(leaf_dir), "input_not_regular_file"),
                                   (str(self.base / "real" / "pipedir" / "obs.json"), "input_unreadable")):
            with self.subTest(observations=observations):
                self.assert_cli_error(self.cli("--observations", observations, "--queue-export", queue), code)
        with self.assertRaises(self.MOD.InputError) as caught:
            self.read(str(leaf_dir / "pipe.json"))
        self.assertEqual(str(caught.exception), "input_not_regular_file")

    def test_existing_outputs_never_overwritten_and_special_files_refused(self):
        leaf_dir = self.base / "real" / "a" / "b"
        os.mkfifo(leaf_dir / "pipe-out.json")
        (leaf_dir / "dir-out.json").mkdir()
        before = snapshot(self.base)
        for name in ("obs.json", "pipe-out.json", "dir-out.json"):
            with self.subTest(name=name):
                result = self.cli("--observations", "real/a/b/obs.json", "--queue-export", "real/a/b/queue.json",
                                  "--output", str(leaf_dir / name))
                self.assert_cli_error(result, "output_not_created")
                with self.assertRaises(FileExistsError):
                    self.MOD.write_new(str(leaf_dir / name), "{}\n")
        self.assertEqual(snapshot(self.base), before)

    def test_missing_output_parent_not_created(self):
        before = snapshot(self.base)
        with self.assertRaises(FileNotFoundError):
            self.MOD.write_new(str(self.base / "real" / "absent" / "deeper" / "out.json"), "{}\n")
        result = self.cli("--observations", "real/a/b/obs.json", "--queue-export", "real/a/b/queue.json",
                          "--output", "real/absent/out.json")
        self.assert_cli_error(result, "output_not_created")
        self.assertEqual(snapshot(self.base), before)

    # --- cap, descriptors, diagnostics ---------------------------------------

    def test_byte_cap_exact_and_plus_one_unchanged(self):
        self.assertEqual(self.MOD.MAX_INPUT_BYTES, EXPECTED_CAP)
        head = b"{\"schema\": \"portal-pointer-observations/v1\", \"observations\": []"
        exact = self.base / "real" / "a" / "b" / "exact.json"
        exact.write_bytes(head + b" " * (EXPECTED_CAP - len(head) - 1) + b"}")
        self.assertEqual(exact.stat().st_size, EXPECTED_CAP)
        self.assertEqual(self.read(str(exact)), [])
        over = self.base / "real" / "a" / "b" / "over.json"
        over.write_bytes(head + b" " * (EXPECTED_CAP - len(head)) + b"}")
        self.assertEqual(over.stat().st_size, EXPECTED_CAP + 1)
        with self.assertRaises(self.MOD.InputError) as caught:
            self.read(str(over))
        self.assertEqual(str(caught.exception), "input_too_large")
        exact.unlink()
        over.unlink()

    def test_repeated_refusals_and_reloads_close_every_descriptor(self):
        os.symlink(self.base / "target" / "a", self.base / "real" / "mid")
        refused_in = str(self.base / "real" / "mid" / "b" / "obs.json")
        refused_out = str(self.base / "real" / "mid" / "b" / "out.json")
        good_in = str(self.base / "real" / "a" / "b" / "obs.json")
        baseline = fd_count()
        for round_number in range(25):
            with self.assertRaises(self.MOD.InputError):
                self.read(refused_in)
            with self.assertRaises(OSError):
                self.MOD.write_new(refused_out, "{}\n")
            with self.assertRaises(self.MOD.InputError):
                self.read(str(self.base / "real" / "a" / "b"))
            self.assertEqual(self.read(good_in), REAL_ROWS)
            self.MOD.write_new(str(self.base / "real" / "a" / "b" / ("r%d.json" % round_number)), "{}\n")
        self.assertEqual(fd_count(), baseline)
        for _ in range(2):
            result = self.cli("--observations", refused_in, "--queue-export", "real/a/b/queue.json")
            self.assert_cli_error(result, "input_unreadable")
            self.assertNotIn("mid", result.stderr)
            self.assertNotIn("obs", result.stderr)


class GateControl(unittest.TestCase):
    """A3: leaf-only opening restored in a disposable copy must be detected."""

    DEFECT_TEST = "test_intermediate_symlink_input_refused_before_target_access"
    POSITIVE_TEST = "test_ordinary_directory_positive_read"
    evidence = {}

    @staticmethod
    def leaf_only_source():
        source = SCRIPT.read_text()
        marker = "def _open_parent(path):"
        if source.count(marker) != 1:
            raise AssertionError("candidate parent helper not found exactly once")
        leaf_only = (
            "def _open_parent(path):  # FA93 GATE CONTROL: pre-FA93 leaf-only opening restored\n"
            "    parent, leaf = os.path.split(os.fsdecode(path))\n"
            "    return os.open(parent or \".\", os.O_RDONLY | os.O_DIRECTORY), leaf\n\n\n")
        return source.replace(marker, leaf_only + "def _fa93_checked_open_parent(path):")

    def load_control(self, holder):
        copy_dir = Path(holder) / "control" / "tools"
        copy_dir.mkdir(parents=True)
        copy = copy_dir / "portal_reconcile.py"
        copy.write_text(self.leaf_only_source())
        name = "fa93_gate_control_portal_reconcile"
        saved_path = list(sys.path)
        spec = importlib.util.spec_from_file_location(name, copy)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path[:] = saved_path
        self.assertEqual(module.__file__, str(copy))
        self.assertIn("GATE CONTROL", Path(module.__file__).read_text())
        return module

    @staticmethod
    def run_pair(module):
        bound = type("BoundBehavior", (Behavior,), {"MOD": module})
        suite = unittest.TestSuite([bound(GateControl.DEFECT_TEST), bound(GateControl.POSITIVE_TEST)])
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        failed = sorted(test.id().rsplit(".", 1)[-1] for test, _ in result.failures)
        errored = sorted(test.id().rsplit(".", 1)[-1] for test, _ in result.errors)
        return {"tests_run": result.testsRun, "failed": failed, "errored": errored,
                "skipped": len(result.skipped), "output": stream.getvalue()}

    def test_leaf_only_opening_is_detected_and_candidate_passes(self):
        with tempfile.TemporaryDirectory(prefix="fa93-control-") as holder:
            control = self.load_control(holder)
            broken = self.run_pair(control)
        candidate = self.run_pair(portal_reconcile)
        GateControl.evidence = {"broken": broken, "candidate": candidate}
        self.assertEqual(broken["tests_run"], 2)
        self.assertEqual(broken["failed"], [self.DEFECT_TEST])
        self.assertEqual(broken["errored"], [])
        self.assertIn("a descriptor on the alias target was opened", broken["output"])
        self.assertEqual((candidate["tests_run"], candidate["failed"], candidate["errored"], candidate["skipped"]),
                         (2, [], [], 0))


if __name__ == "__main__":
    unittest.main()
