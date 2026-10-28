"""FA42: catalogue link input reads refuse special files without blocking.

Synthetic, generated-fixture-only regressions for catalog_links._read and its
real consumers.  The deciding race (a regular input replaced by a writerless
FIFO after the genuine precheck and before the final open) runs only in a
file-launched child process under a watchdog, and only after the inert open
flag check is green, so a red baseline fails instead of hanging.

Running this file directly is reserved for child modes; the tests are
collected by ordinary unittest discovery.  Every child mode first runs the
acceptance helper's bootstrap_child(), which installs process-local offline
and live-file controls BEFORE any application import and records startup
evidence for that process:

  --fa42-fifo-child MODE ROOT       FIFO replacement race (read | snapshot)
  --fa42-guarded-unittest RUN EVIDENCE NAME...
                                    unittest for the acceptance runner
  --fa42-bootstrap-probe on|off ROOT
                                    inert startup probe; "off" removes only the
                                    startup controls (deciding A6 control)
"""
import errno
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
HELPER_PATH = REPO_ROOT / "tools" / "accept_fa42_link_nonblock.py"
CHILD_FLAG = "--fa42-fifo-child"
GUARDED_FLAG = "--fa42-guarded-unittest"
PROBE_FLAG = "--fa42-bootstrap-probe"
FIXTURE_MARKER = ".fa42-fixture-root"
WATCHDOG_SECONDS = 5
# Startup evidence returned by FIFO children launched from this process.
CHILD_STARTUP_RECORDS = []


def load_helper():
    """Load the acceptance helper (stdlib-only, no side effects) by file path."""
    spec = importlib.util.spec_from_file_location("_fa42_acceptance_helper", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _child_root(raw):
    root = Path(raw)
    info = os.lstat(root)
    if (not root.is_absolute() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or not (root / FIXTURE_MARKER).is_file()):
        raise SystemExit("fa42 child: refusing unmarked or unsafe fixture root")
    return root


def _private_subdir(root, name):
    path = root / name
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise SystemExit("fa42 child: unsafe per-run tmp directory")
    return path


def _child_startup(argv):
    """Child modes only: process-local controls before any application import."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    helper = load_helper()
    if len(argv) == 4 and argv[1] == CHILD_FLAG and argv[2] in ("read", "snapshot"):
        root = _child_root(argv[3])
        enabled = True
    elif len(argv) == 4 and argv[1] == PROBE_FLAG and argv[2] in ("on", "off"):
        root = _child_root(argv[3])
        enabled = argv[2] == "on"
    elif len(argv) >= 5 and argv[1] == GUARDED_FLAG:
        root = Path(argv[2])
        info = os.lstat(root)
        evidence = Path(argv[3])
        if (not root.is_absolute() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or evidence.parent != root or evidence.exists()):
            raise SystemExit("fa42 child: refusing unsafe run root or evidence path")
        enabled = True
    else:
        raise SystemExit("fa42: run through unittest discovery; direct use is child mode only")
    _private_subdir(root, "tmp")
    return helper, helper.bootstrap_child(root, extra_read=(REPO_ROOT,), enabled=enabled)


_CHILD_BOOT = _child_startup(sys.argv) if __name__ == "__main__" else None

from campaign_tool.records import catalog_links  # noqa: E402

REAL_OPEN = os.open
REAL_READ = os.read
REAL_FSTAT = os.fstat


# ---------------------------------------------------------------- seams/doubles

class OpenRecorder:
    """os.open seam recording (path, flags); inert unless delegate=True."""

    def __init__(self, *, delegate=True, strip=0):
        self.delegate = delegate
        self.strip = strip
        self.calls = []
        self.fds = []

    def __call__(self, path, flags, mode=0o777, *, dir_fd=None):
        flags &= ~self.strip
        self.calls.append((os.fspath(path), flags))
        if not self.delegate:
            raise OSError(errno.EPERM, "fa42 inert open seam")
        fd = REAL_OPEN(path, flags, mode, dir_fd=dir_fd)
        self.fds.append(fd)
        return fd


class ReadRecorder:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, fd, count):
        self.calls.append(fd)
        if self.fail:
            raise OSError(errno.EIO, "fa42 synthetic read failure")
        return REAL_READ(fd, count)


class FakeStat:
    def __init__(self, real, *, mode=None, size=None, ino=None):
        self.st_mode = real.st_mode if mode is None else mode
        self.st_size = real.st_size if size is None else size
        self.st_dev = real.st_dev
        self.st_ino = real.st_ino if ino is None else ino
        self.st_mtime_ns = real.st_mtime_ns
        self.st_uid = real.st_uid


class FstatDouble:
    """Wrap os.fstat; transforms[i] rewrites the i-th call's result."""

    def __init__(self, *transforms, fail=False):
        self.transforms = transforms
        self.fail = fail
        self.calls = 0

    def __call__(self, fd):
        index = self.calls
        self.calls += 1
        if self.fail:
            raise OSError(errno.EIO, "fa42 synthetic fstat failure")
        real = REAL_FSTAT(fd)
        if index < len(self.transforms) and self.transforms[index] is not None:
            return self.transforms[index](real)
        return real


def final_open_calls(calls):
    # _read's final input opens are read-only, non-directory opens.
    return [(path, flags) for path, flags in calls
            if flags & os.O_ACCMODE == os.O_RDONLY and not flags & os.O_DIRECTORY]


def assert_final_open_flags(calls, path=None):
    """Deciding flag regression: raises AssertionError on any unsafe open."""
    selected = final_open_calls(calls)
    if path is not None:
        selected = [call for call in selected if call[0] == os.fspath(path)]
    if not selected:
        raise AssertionError("no final input open was recorded")
    for opened, flags in selected:
        if flags & os.O_ACCMODE != os.O_RDONLY:
            raise AssertionError(f"final open is not read-only: {flags:#o}")
        if not flags & os.O_NOFOLLOW:
            raise AssertionError("final open lacks O_NOFOLLOW")
        if not flags & os.O_NONBLOCK:
            raise AssertionError("final open lacks O_NONBLOCK")
        if flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise AssertionError(f"final open may modify input: {flags:#o}")
    return len(selected)


def assert_no_read(reads, fds):
    """Deciding no-read regression: raises if any read touched these fds."""
    touched = [fd for fd in reads if fd in fds]
    if touched:
        raise AssertionError(f"nonregular descriptor was read {len(touched)} time(s)")


def fd_is_closed(fd):
    try:
        REAL_FSTAT(fd)
    except OSError as error:
        return error.errno == errno.EBADF
    return False


def open_fd_count():
    for directory in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(directory):
            return len(os.listdir(directory))
    return None


def write_private(path, data):
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def make_fixture_root(parent=None):
    root = Path(tempfile.mkdtemp(prefix="fa42-", dir=parent))
    root.chmod(0o700)
    write_private(root / FIXTURE_MARKER, b"fa42 synthetic fixture root\n")
    return root


def link_fixture(root):
    """Generated catalogue snapshot + registry matching the existing tests."""
    first, second = "a" * 64, "b" * 64
    snapshot = root / "snapshot"
    snapshot.mkdir(mode=0o700)
    manifest = {"schema_version": 1, "sources": []}
    snapshot_id = catalog_links._sha(catalog_links.canonical(manifest).encode("utf-8"))
    encode = lambda value: (catalog_links.canonical(value) + "\n").encode("utf-8")
    catalog_bytes = encode({"schema_version": 1, "snapshot_id": snapshot_id,
                            "cards": [{"sha256": first}, {"sha256": second}]})
    manifest_bytes = encode(manifest)
    write_private(snapshot / "catalog.json", catalog_bytes)
    write_private(snapshot / "input-manifest.json", manifest_bytes)
    write_private(snapshot / "artifact-hashes.json", encode({
        "catalog.json": catalog_links._sha(catalog_bytes),
        "input-manifest.json": catalog_links._sha(manifest_bytes)}))
    registry = write_private(root / "registry.json", encode({
        "schema_version": 1, "snapshot_id": snapshot_id,
        "catalog_sha256": catalog_links._sha(catalog_bytes),
        "agencies": [{"agency_id": "agency:north"}],
        "requests": [{"request_id": "request:one", "agency_id": "agency:north"}],
        "links": [{"source_sha256": first, "agency_id": "agency:north",
                   "request_id": "request:one"}]}))
    return snapshot, registry, root / "output", catalog_bytes


def link_code(call):
    try:
        call()
    except catalog_links.LinkError as error:
        return error.code
    return None


# ------------------------------------------------------------------ base class

class Fa42Case(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="fa42-case-")
        self.addCleanup(holder.cleanup)
        self.root = make_fixture_root(holder.name)
        self.fds_before = open_fd_count()

    def regular(self, name, data):
        return write_private(self.root / name, data)

    def read_recorded(self, path, *, fstat=None, read=None, open_seam=None):
        opener = open_seam or OpenRecorder()
        reader = read or ReadRecorder()
        patches = [mock.patch.object(os, "open", opener), mock.patch.object(os, "read", reader)]
        if fstat is not None:
            patches.append(mock.patch.object(os, "fstat", fstat))
        result, code = None, None
        for patch in patches:
            patch.start()
        try:
            result = catalog_links._read(path)
        except catalog_links.LinkError as error:
            code = error.code
        finally:
            for patch in reversed(patches):
                patch.stop()
        return result, code, opener, reader

    def assert_all_closed(self, opener):
        for fd in opener.fds:
            self.assertTrue(fd_is_closed(fd), f"descriptor {fd} leaked")

    def assert_no_fd_growth(self):
        if self.fds_before is not None:
            self.assertEqual(open_fd_count(), self.fds_before)

    def require_nonblocking_flag(self):
        # Inert seam first: a red flag prohibits any real FIFO case.
        probe = self.regular("flag-probe.json", b"{}\n")
        opener = OpenRecorder(delegate=False)
        with mock.patch.object(os, "open", opener):
            code = link_code(lambda: catalog_links._read(probe))
        try:
            assert_final_open_flags(opener.calls, probe)
        except AssertionError as error:
            self.fail(f"flag regression red ({error}); real FIFO case prohibited")
        self.assertEqual(code, "unsafe_or_missing_input")


# ------------------------------------------------------------- A2 flag checks

class FinalOpenFlagTests(Fa42Case):
    def test_inert_seam_final_open_is_nonblocking_nofollow_readonly(self):
        path = self.regular("input.json", b'{"ok": true}\n')
        opener = OpenRecorder(delegate=False)
        with mock.patch.object(os, "open", opener):
            code = link_code(lambda: catalog_links._read(path))
        self.assertEqual(code, "unsafe_or_missing_input")
        self.assertEqual(assert_final_open_flags(opener.calls, path), 1)
        self.assertEqual(len(opener.calls), 1)

    def test_delegating_seam_flags_on_regular_read(self):
        path = self.regular("input.json", b'{"ok": true}\n')
        data, code, opener, _reads = self.read_recorded(path)
        self.assertIsNone(code)
        self.assertEqual(data, b'{"ok": true}\n')
        self.assertEqual(assert_final_open_flags(opener.calls, path), 1)
        self.assert_all_closed(opener)


# --------------------------------------------- A1/A3 regular reads and bounds

class RegularReadTests(Fa42Case):
    def test_empty_and_nonempty_regular_bytes_exact(self):
        payloads = {"empty.json": b"", "bytes.json": bytes(range(256)) * 33 + b"\n"}
        for name, payload in payloads.items():
            with self.subTest(name=name):
                path = self.regular(name, payload)
                data, code, opener, _reads = self.read_recorded(path)
                self.assertIsNone(code)
                self.assertEqual(data, payload)
                self.assertEqual(catalog_links._sha(data or b""), catalog_links._sha(payload))
                self.assert_all_closed(opener)
        self.assert_no_fd_growth()

    def test_limit_edge_uses_existing_bound(self):
        payload = b"x" * 4096
        path = self.regular("edge.json", payload)
        with mock.patch.object(catalog_links, "MAX_INPUT_BYTES", len(payload)):
            data, code, opener, _ = self.read_recorded(path)
        self.assertIsNone(code)
        self.assertEqual(data, payload)
        self.assert_all_closed(opener)
        with mock.patch.object(catalog_links, "MAX_INPUT_BYTES", len(payload) - 1):
            data, code, opener, reads = self.read_recorded(path)
        self.assertEqual(code, "invalid_input_file")
        self.assertEqual(reads.calls, [])
        self.assert_all_closed(opener)
        empty = self.regular("zero.json", b"")
        with mock.patch.object(catalog_links, "MAX_INPUT_BYTES", 0):
            data, code, opener, _ = self.read_recorded(empty)
        self.assertEqual((data, code), (b"", None))
        self.assert_all_closed(opener)
        self.assert_no_fd_growth()

    def test_growth_past_bound_is_source_changed_or_oversize(self):
        payload = b"y" * 2048
        path = self.regular("grow.json", payload)
        limit = len(payload) - 10
        understated = FstatDouble(lambda real: FakeStat(real, size=limit))
        with mock.patch.object(catalog_links, "MAX_INPUT_BYTES", limit):
            data, code, opener, reads = self.read_recorded(path, fstat=understated)
        self.assertEqual(code, "source_changed_or_oversize")
        self.assertIsNone(data)
        self.assertTrue(reads.calls)
        self.assert_all_closed(opener)

    def test_changed_descriptor_identity_refused(self):
        path = self.regular("ident.json", b"{}\n")
        moved = FstatDouble(None, lambda real: FakeStat(real, ino=real.st_ino + 1))
        data, code, opener, _ = self.read_recorded(path, fstat=moved)
        self.assertEqual(code, "source_changed_or_oversize")
        self.assertEqual(moved.calls, 2)
        self.assert_all_closed(opener)

    def test_unsafe_ancestry_leaf_symlink_and_missing_refused_before_open(self):
        real_dir = self.root / "real"
        real_dir.mkdir(mode=0o700)
        target = write_private(real_dir / "input.json", b"{}\n")
        (self.root / "linked").symlink_to(real_dir, target_is_directory=True)
        (self.root / "leaf.json").symlink_to(target)
        for path in (self.root / "linked" / "input.json", self.root / "leaf.json",
                     self.root / "missing.json"):
            with self.subTest(path=path.name):
                data, code, opener, reads = self.read_recorded(path)
                self.assertEqual(code, "unsafe_or_missing_input")
                self.assertEqual(opener.calls, [])
                self.assertEqual(reads.calls, [])

    def test_stable_fifo_refused_by_genuine_precheck_without_open(self):
        self.require_nonblocking_flag()
        fifo = self.root / "stable.fifo"
        os.mkfifo(fifo, 0o600)
        data, code, opener, reads = self.read_recorded(fifo)
        self.assertEqual(code, "unsafe_or_missing_input")
        self.assertEqual(opener.calls, [])
        self.assertEqual(reads.calls, [])


# ------------------------------------- A2/A3 nonregular doubles and lifecycle

NONREGULAR_MODES = {
    "fifo": stat.S_IFIFO, "char": stat.S_IFCHR, "block": stat.S_IFBLK,
    "directory": stat.S_IFDIR, "socket": stat.S_IFSOCK,
}


class DescriptorLifecycleTests(Fa42Case):
    def test_nonregular_fstat_doubles_refused_before_read_and_closed(self):
        path = self.regular("input.json", b"{}\n")
        for name, kind in NONREGULAR_MODES.items():
            with self.subTest(kind=name):
                double = FstatDouble(lambda real, kind=kind: FakeStat(real, mode=kind | 0o600))
                data, code, opener, reads = self.read_recorded(path, fstat=double)
                self.assertEqual(code, "invalid_input_file")
                self.assertIsNone(data)
                self.assertEqual(double.calls, 1)
                assert_no_read(reads.calls, opener.fds)
                self.assertEqual(len(opener.fds), 1)
                self.assert_all_closed(opener)
        self.assert_no_fd_growth()

    def test_fstat_failure_closes_descriptor(self):
        path = self.regular("input.json", b"{}\n")
        data, code, opener, reads = self.read_recorded(path, fstat=FstatDouble(fail=True))
        self.assertEqual(code, "unsafe_or_missing_input")
        self.assertEqual(reads.calls, [])
        self.assertEqual(len(opener.fds), 1)
        self.assert_all_closed(opener)

    def test_read_failure_closes_descriptor(self):
        path = self.regular("input.json", b"{}\n")
        failing = ReadRecorder(fail=True)
        data, code, opener, reads = self.read_recorded(path, read=failing)
        self.assertEqual(code, "unsafe_or_missing_input")
        self.assertEqual(len(failing.calls), 1)
        self.assertEqual(len(opener.fds), 1)
        self.assert_all_closed(opener)

    def test_open_failures_map_and_acquire_nothing(self):
        path = self.regular("input.json", b"{}\n")
        inert = OpenRecorder(delegate=False)
        data, code, opener, reads = self.read_recorded(path, open_seam=inert)
        self.assertEqual(code, "unsafe_or_missing_input")
        self.assertEqual(opener.fds, [])
        real_checked = catalog_links._checked_input

        def checked_then_remove(candidate):
            result = real_checked(candidate)
            result.unlink()
            return result

        with mock.patch.object(catalog_links, "_checked_input", side_effect=checked_then_remove):
            data, code, opener, reads = self.read_recorded(path)
        self.assertEqual(code, "unsafe_or_missing_input")
        self.assertEqual(opener.fds, [])
        self.assertEqual(reads.calls, [])

    def test_repeated_mixed_runs_do_not_leak_descriptors(self):
        good = self.regular("good.json", b'{"n": 1}\n')
        before = open_fd_count()
        opened = []
        for index in range(40):
            if index % 4 == 0:
                _, code, opener, _ = self.read_recorded(good)
                self.assertIsNone(code)
            elif index % 4 == 1:
                _, code, opener, _ = self.read_recorded(
                    good, fstat=FstatDouble(lambda real: FakeStat(real, mode=stat.S_IFIFO)))
                self.assertEqual(code, "invalid_input_file")
            elif index % 4 == 2:
                _, code, opener, _ = self.read_recorded(good, read=ReadRecorder(fail=True))
                self.assertEqual(code, "unsafe_or_missing_input")
            else:
                _, code, opener, _ = self.read_recorded(good, fstat=FstatDouble(fail=True))
                self.assertEqual(code, "unsafe_or_missing_input")
            opened.extend(opener.fds)
            self.assert_all_closed(opener)
        self.assertEqual(len(opened), 40)
        if before is not None:
            self.assertEqual(open_fd_count(), before)


# ---------------------------------------- A2/A5 real FIFO replacement (child)

class FifoReplacementRaceTests(Fa42Case):
    def run_child(self, mode):
        self.require_nonblocking_flag()
        child_root = make_fixture_root(self.root)
        argv = [sys.executable, "-B", "-W", "error::ResourceWarning", str(THIS_FILE),
                CHILD_FLAG, mode, str(child_root)]
        try:
            done = subprocess.run(argv, cwd=child_root, capture_output=True, text=True,
                                  timeout=WATCHDOG_SECONDS, check=False)
        except subprocess.TimeoutExpired:
            self.fail(f"FIFO child ({mode}) exceeded the {WATCHDOG_SECONDS}s watchdog")
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        evidence = json.loads(done.stdout)
        self.assertEqual(evidence["mode"], mode)
        # The replacement seam must have run after the genuine precheck saw a
        # regular file, so the stable-path precheck cannot pass this alone.
        self.assertTrue(evidence["precheck_saw_regular"])
        self.assertTrue(evidence["replaced_with_fifo"])
        self.assertTrue(evidence["fifo_present_after"])
        self.assertEqual(evidence["code"], "invalid_input_file")
        self.assertGreaterEqual(evidence["fifo_opens"], 1)
        self.assertTrue(evidence["fifo_open_nonblocking"])
        self.assertEqual(evidence["fifo_reads"], 0)
        self.assertTrue(evidence["all_fds_closed"])
        # A6: this child installed its own controls before application import.
        startup = evidence["startup"]
        self.assertEqual(load_helper().verify_startup_evidence(startup, parent_pid=os.getpid()),
                         [])
        self.assertEqual(startup["ppid"], os.getpid())
        self.assertEqual(Path(startup["application_after_controls"][
            "campaign_tool.records.catalog_links"]).resolve(),
            REPO_ROOT / "campaign_tool" / "records" / "catalog_links.py")
        CHILD_STARTUP_RECORDS.append(startup)
        return evidence

    def test_regular_to_writerless_fifo_after_precheck_is_invalid_input_file(self):
        evidence = self.run_child("read")
        self.assertEqual(evidence["precheck_calls"], 1)

    def test_snapshot_consumer_fifo_replacement_refused_without_output(self):
        evidence = self.run_child("snapshot")
        self.assertFalse(evidence["output_exists"])


# ------------------------------------------------------- A5 actual integration

class CatalogueIntegrationTests(Fa42Case):
    def test_snapshot_registry_run_and_reuse_through_nonblocking_reads(self):
        snapshot, registry, output, catalog_bytes = link_fixture(self.root)
        before = open_fd_count()
        opener = OpenRecorder()
        with mock.patch.object(os, "open", opener):
            first = catalog_links.run(snapshot, registry, output)
            first_reads = len(final_open_calls(opener.calls))
            again = catalog_links.run(snapshot, registry, output)
        self.assertEqual(first_reads, 4)
        self.assertEqual(len(final_open_calls(opener.calls)), 10)
        self.assertEqual(assert_final_open_flags(opener.calls), 10)
        self.assert_all_closed(opener)
        if before is not None:
            self.assertEqual(open_fd_count(), before)
        self.assertFalse(first["reused"])
        self.assertTrue(again["reused"])
        self.assertEqual(first["run_id"], again["run_id"])
        destination = output / first["run_id"]
        candidates = json.loads((destination / "candidates.json").read_text())
        receipt = json.loads((destination / "receipt.json").read_text())
        for item in candidates["candidates"]:
            self.assertEqual((item["status"], item["verified"], item["publication_ready"]),
                             ("candidate_only", False, False))
        self.assertEqual(receipt["input_bindings"]["catalog_sha256"],
                         catalog_links._sha(catalog_bytes))
        self.assertEqual(receipt["input_bindings"]["registry_sha256"],
                         catalog_links._sha(registry.read_bytes()))
        self.assertEqual(receipt["verified_attributions"], 0)

    def test_snapshot_reader_bytes_and_bindings_exact(self):
        snapshot, _registry, _output, catalog_bytes = link_fixture(self.root)
        checked, snapshot_id, cards, bindings = catalog_links._snapshot(snapshot)
        self.assertEqual(checked, snapshot)
        self.assertEqual(bindings["catalog_sha256"], catalog_links._sha(catalog_bytes))
        self.assertEqual(bindings["snapshot_id"], snapshot_id)
        self.assertEqual(sorted(cards), ["a" * 64, "b" * 64])

    def test_consumer_refusals_preserved(self):
        snapshot, registry, output, _ = link_fixture(self.root)
        double = FstatDouble(lambda real: FakeStat(real, mode=stat.S_IFCHR))
        with mock.patch.object(os, "fstat", double):
            code = link_code(lambda: catalog_links.run(snapshot, registry, output))
        self.assertEqual(code, "invalid_input_file")
        self.assertFalse(output.exists())
        with mock.patch.object(catalog_links, "MAX_INPUT_BYTES", 8):
            code = link_code(lambda: catalog_links.run(snapshot, registry, output))
        self.assertEqual(code, "invalid_input_file")
        linked = self.root / "linked-snapshot"
        linked.symlink_to(snapshot, target_is_directory=True)
        code = link_code(lambda: catalog_links.run(linked, registry, output))
        self.assertEqual(code, "unsafe_or_missing_snapshot")
        alias = self.root / "registry-alias.json"
        alias.symlink_to(registry)
        code = link_code(lambda: catalog_links.run(snapshot, alias, output))
        self.assertEqual(code, "unsafe_or_missing_input")
        self.assertFalse(output.exists())


# ------------------------------------------------------- A4 deciding controls

class NegativeControlTests(Fa42Case):
    def test_control_without_nonblock_fails_flag_assertion_before_special_open(self):
        path = self.regular("input.json", b"{}\n")
        broken = OpenRecorder(delegate=False, strip=os.O_NONBLOCK)
        with mock.patch.object(os, "open", broken):
            code = link_code(lambda: catalog_links._read(path))
        self.assertEqual(code, "unsafe_or_missing_input")
        with self.assertRaisesRegex(AssertionError, "O_NONBLOCK"):
            assert_final_open_flags(broken.calls, path)
        self.assertEqual(broken.fds, [])
        for opened, _flags in broken.calls:
            self.assertTrue(stat.S_ISREG(os.lstat(opened).st_mode))
        restored = OpenRecorder(delegate=False)
        with mock.patch.object(os, "open", restored):
            link_code(lambda: catalog_links._read(path))
        self.assertEqual(assert_final_open_flags(restored.calls, path), 1)

    def test_control_permitting_nonregular_read_fails_no_read_assertion(self):
        path = self.regular("input.json", b"{}\n")
        fifo_mode = FstatDouble(lambda real: FakeStat(real, mode=stat.S_IFIFO | 0o600),
                                lambda real: FakeStat(real, mode=stat.S_IFIFO | 0o600))
        with mock.patch.object(stat, "S_ISREG", lambda _mode: True):
            data, code, opener, reads = self.read_recorded(path, fstat=fifo_mode)
        self.assertIsNone(code)
        self.assertEqual(data, b"{}\n")
        with self.assertRaisesRegex(AssertionError, "nonregular descriptor was read"):
            assert_no_read(reads.calls, opener.fds)
        self.assert_all_closed(opener)
        restored = FstatDouble(lambda real: FakeStat(real, mode=stat.S_IFIFO | 0o600))
        data, code, opener, reads = self.read_recorded(path, fstat=restored)
        self.assertEqual(code, "invalid_input_file")
        assert_no_read(reads.calls, opener.fds)
        self.assert_all_closed(opener)


# --------------------------------------------------- A6 portability/isolation

class StartupControlTests(Fa42Case):
    """A6 deciding control for the child bootstrap (inert probe children)."""

    def launch_probe(self, state):
        root = make_fixture_root(self.root)
        argv = [sys.executable, "-B", "-W", "error::ResourceWarning", str(THIS_FILE),
                PROBE_FLAG, state, str(root)]
        try:
            done = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                                  timeout=WATCHDOG_SECONDS, check=False)
        except subprocess.TimeoutExpired:
            self.fail(f"startup probe ({state}) exceeded the {WATCHDOG_SECONDS}s watchdog")
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return json.loads(done.stdout)

    def test_bootstrap_installs_controls_before_application_import(self):
        helper = load_helper()
        self.assertEqual((helper.GUARDED_FLAG, helper.PROBE_FLAG), (GUARDED_FLAG, PROBE_FLAG))
        evidence = self.launch_probe("on")
        self.assertEqual(helper.verify_startup_evidence(evidence, parent_pid=os.getpid()), [])
        self.assertEqual(evidence["application_before_controls"], [])
        self.assertTrue(evidence["guards"]["probe_refused"])
        self.assertEqual(evidence["guards"]["hits"], [])
        self.assertIsInstance(evidence["runtime_constants_redirected"], list)
        for name in helper.APPLICATION_CLOSURE:
            module = Path(evidence["application_after_controls"][name]).resolve()
            self.assertTrue(str(module).startswith(str(REPO_ROOT) + os.sep), name)

    def test_removed_startup_control_fails_acceptance(self):
        helper = load_helper()
        removed = self.launch_probe("off")
        self.assertFalse(removed["guards_installed"])
        problems = helper.verify_startup_evidence(removed, parent_pid=os.getpid())
        self.assertIn("startup controls not installed", problems)
        self.assertIn("live-file probe not refused", problems)
        self.assertEqual(helper.verify_startup_evidence(None), [
            "startup evidence missing or malformed"])
        same_process = dict(self.launch_probe("on"), pid=os.getpid())
        self.assertIn("startup evidence is not from a separate child process",
                      helper.verify_startup_evidence(same_process, parent_pid=os.getpid()))
        restored = self.launch_probe("on")
        self.assertEqual(helper.verify_startup_evidence(restored, parent_pid=os.getpid()), [])


class PortabilityTests(unittest.TestCase):
    def test_loaded_module_belongs_to_this_source_tree(self):
        module = Path(catalog_links.__file__).resolve()
        self.assertEqual(module, REPO_ROOT / "campaign_tool" / "records" / "catalog_links.py")

    def test_committed_sources_have_no_machine_specific_paths(self):
        forbidden = ["/" + "home/", "agent" + "-work", "/" + "Users/", ".her" + "mes",
                     "/" + "mnt/", "/" + "tmp/"]
        sources = [THIS_FILE, REPO_ROOT / "tools" / "accept_fa42_link_nonblock.py"]
        for source in sources:
            with self.subTest(source=source.name):
                text = source.read_text(encoding="utf-8")
                for needle in forbidden:
                    self.assertNotIn(needle, text)


# ------------------------------------------------------------------ child mode

def _child_main(mode, raw_root, startup):
    if hasattr(signal, "alarm"):
        signal.alarm(WATCHDOG_SECONDS)
    root = _child_root(raw_root)
    evidence = {"mode": mode, "precheck_calls": 0, "precheck_saw_regular": False,
                "replaced_with_fifo": False}
    if mode == "read":
        target = write_private(root / "input.json", b'{"fa42": "regular"}\n')
        output = None
        action = lambda: catalog_links._read(target)
    elif mode == "snapshot":
        snapshot, registry, output, _ = link_fixture(root)
        target = snapshot / "catalog.json"
        action = lambda: catalog_links.run(snapshot, registry, output)
    else:
        raise SystemExit("fa42 child: unknown mode")
    real_checked = catalog_links._checked_input

    def checked_then_replace(path):
        result = real_checked(path)
        evidence["precheck_calls"] += 1
        if result == target and not evidence["replaced_with_fifo"]:
            evidence["precheck_saw_regular"] = stat.S_ISREG(os.lstat(result).st_mode)
            result.unlink()
            os.mkfifo(result, 0o600)
            evidence["replaced_with_fifo"] = stat.S_ISFIFO(os.lstat(result).st_mode)
        return result

    opener, reader = OpenRecorder(), ReadRecorder()
    with mock.patch.object(catalog_links, "_checked_input", side_effect=checked_then_replace), \
            mock.patch.object(os, "open", opener), mock.patch.object(os, "read", reader):
        evidence["code"] = link_code(action)
    fifo_opens = [index for index, (path, _flags) in enumerate(opener.calls)
                  if path == os.fspath(target)]
    fifo_fds = {opener.fds[index] for index in fifo_opens if index < len(opener.fds)}
    evidence.update({
        "fifo_present_after": stat.S_ISFIFO(os.lstat(target).st_mode),
        "fifo_opens": len(fifo_opens),
        "fifo_open_nonblocking": bool(fifo_opens) and all(
            opener.calls[index][1] & os.O_NONBLOCK for index in fifo_opens),
        "fifo_reads": sum(1 for fd in reader.calls if fd in fifo_fds),
        "all_fds_closed": all(fd_is_closed(fd) for fd in opener.fds),
        "output_exists": output.exists() if output is not None else False,
    })
    # Written last so the guard hit list covers the whole child run.
    evidence["startup"] = startup
    print(json.dumps(evidence, sort_keys=True))
    return 0


def _guarded_unittest_main(helper, startup, argv):
    evidence_path, names = Path(argv[3]), argv[4:]
    program = unittest.main(module=None, argv=[argv[0], "-v", *names], exit=False)
    result = program.result
    loaded = sys.modules.get("tests.records.test_fa42_link_nonblock")
    startup["unittest"] = {"names": names, "ran": result.testsRun,
                           "failures": len(result.failures), "errors": len(result.errors),
                           "skipped": len(result.skipped),
                           "successful": result.wasSuccessful()}
    startup["descendant_startup"] = list(getattr(loaded, "CHILD_STARTUP_RECORDS", []))
    with open(evidence_path, "x", encoding="utf-8") as handle:
        handle.write(json.dumps(startup, indent=2, sort_keys=True) + "\n")
    problems = helper.verify_startup_evidence(startup)
    return 0 if result.wasSuccessful() and not problems else 1


if __name__ == "__main__":
    _helper, _startup = _CHILD_BOOT
    if sys.argv[1] == CHILD_FLAG:
        raise SystemExit(_child_main(sys.argv[2], sys.argv[3], _startup))
    if sys.argv[1] == PROBE_FLAG:
        # Inert probe: report startup state and exit; no application code runs.
        print(json.dumps(_startup, sort_keys=True))
        raise SystemExit(0)
    raise SystemExit(_guarded_unittest_main(_helper, _startup, sys.argv))
