"""status --fix-modes tightens the descriptor admitted by its final check, never a re-resolved pathname (RF2150-5).

Synthetic files in temporary directories only. Run from the repository root:
python3 -B -m unittest tests.test_store_modes_descriptor -q
"""
import contextlib
import errno
import hashlib
import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli as cli_module  # noqa: E402
from campaign_tool import store_check  # noqa: E402

NAME = "a" * 64
SHOWN = "objects/" + "a" * 12
DATA = b"synthetic descriptor object\n"
OUTSIDE_DATA = b"synthetic outside bytes\n"
UNSUPPORTED = "Private mode repair needs no-follow descriptor support"


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def open_fds():
    return sorted(os.listdir("/proc/self/fd"))


class DescriptorBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-mode-fd-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(os.umask, os.umask(0o077))
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.private = self.root / "private"
        self.objects = self.private / "objects"
        self.objects.mkdir(parents=True)
        os.chmod(self.private, 0o700)
        os.chmod(self.objects, 0o700)
        self.target = self.objects / NAME
        self.target.write_bytes(DATA)
        os.chmod(self.target, 0o644)
        self.outside = self.base / "outside.bin"
        self.outside.write_bytes(OUTSIDE_DATA)
        os.chmod(self.outside, 0o644)
        self.parked = self.base / "parked"
        self.outside_stat = os.stat(self.outside)

    def outside_unchanged(self):
        now = os.stat(self.outside)
        self.assertEqual(self.outside.read_bytes(), OUTSIDE_DATA)
        self.assertEqual(stat.S_IMODE(now.st_mode), 0o644)
        self.assertEqual(now.st_mtime_ns, self.outside_stat.st_mtime_ns)

    def after_final_stat(self, action):
        """_lstat wrapper: the real metadata of the target is read, then ``action`` swaps the entry (second look)."""
        real, calls = store_check._lstat, []

        def wrapper(path):
            info = real(path)
            if Path(path) == self.target:
                calls.append(path)
                if len(calls) == 2:
                    action()
            return info
        return mock.patch.object(store_check, "_lstat", wrapper)

    def park_original(self):
        os.rename(self.target, self.parked)


class RaceTests(DescriptorBase):
    def test_symlink_arrives_after_real_final_stat_never_changes_outside_mode(self):
        def action():
            self.park_original()
            self.target.symlink_to(self.outside)
        with self.after_final_stat(action):
            report = store_check.fix_modes(self.root)
        self.assertEqual(report, {"fixed": [], "skipped": [SHOWN]})
        self.outside_unchanged()
        self.assertEqual(mode(self.parked), 0o644)
        self.assertTrue(self.target.is_symlink())

    def test_static_and_dangling_symlinks_are_never_opened(self):
        (self.objects / "static-link").symlink_to(self.outside)
        (self.objects / "dangling-link").symlink_to(self.base / "does-not-exist")
        real_open, opened = os.open, []

        def spy(path, *args, **kwargs):
            opened.append(Path(path).name)
            return real_open(path, *args, **kwargs)
        with mock.patch.object(os, "open", spy):
            report = store_check.fix_modes(self.root)
        self.assertEqual(report, {"fixed": [SHOWN], "skipped": []})
        self.assertEqual(opened, [NAME])
        self.outside_unchanged()

    def test_same_kind_replacement_before_open_is_skipped_by_identity(self):
        def action():
            self.park_original()
            self.target.write_bytes(DATA)
            os.chmod(self.target, 0o644)
        with self.after_final_stat(action):
            report = store_check.fix_modes(self.root)
        self.assertEqual(report, {"fixed": [], "skipped": [SHOWN]})
        self.assertEqual(mode(self.target), 0o644)
        self.assertEqual(mode(self.parked), 0o644)

    def test_directory_replaced_by_directory_before_open_is_skipped_by_identity(self):
        os.remove(self.target)
        self.target.mkdir()
        os.chmod(self.target, 0o755)

        def action():
            self.park_original()
            self.target.mkdir()
            os.chmod(self.target, 0o755)
        with self.after_final_stat(action):
            report = store_check.fix_modes(self.root)
        self.assertEqual(report, {"fixed": [], "skipped": [SHOWN]})
        self.assertEqual(mode(self.target), 0o755)
        self.assertEqual(mode(self.parked), 0o755)

    def test_replacement_after_open_cannot_redirect_fchmod(self):
        for replacement in ("symlink", "regular"):
            with self.subTest(replacement=replacement):
                if self.parked.exists():
                    os.rename(self.parked, self.target)
                    os.chmod(self.target, 0o644)
                real_open = os.open

                def swapping(path, *args, **kwargs):
                    fd = real_open(path, *args, **kwargs)
                    if Path(path) == self.target:
                        self.park_original()
                        if replacement == "symlink":
                            self.target.symlink_to(self.outside)
                        else:
                            self.target.write_bytes(DATA)
                            os.chmod(self.target, 0o644)
                    return fd
                with mock.patch.object(os, "open", swapping):
                    report = store_check.fix_modes(self.root)
                self.assertEqual(report, {"fixed": [SHOWN], "skipped": []})
                self.assertEqual(mode(self.parked), 0o600)
                self.outside_unchanged()
                if replacement == "regular":
                    self.assertEqual(mode(self.target), 0o644)
                    self.assertEqual(self.target.read_bytes(), DATA)
                os.remove(self.target)

    def test_fifo_directory_socket_and_missing_arrivals_never_block_or_mutate(self):
        def make_fifo():
            os.mkfifo(self.target)
            os.chmod(self.target, 0o644)

        def make_directory():
            self.target.mkdir()
            os.chmod(self.target, 0o755)

        def make_socket():
            here = os.getcwd()
            os.chdir(self.base)
            try:
                server = socket.socket(socket.AF_UNIX)
                self.addCleanup(server.close)
                server.bind("sock")
            finally:
                os.chdir(here)
            os.rename(self.base / "sock", self.target)
            os.chmod(self.target, 0o644)

        def timed_out(*_):
            raise AssertionError("fix_modes blocked on an arrived entry")

        for label, make in (("fifo", make_fifo), ("directory", make_directory),
                            ("socket", make_socket), ("missing", lambda: None)):
            with self.subTest(arrival=label):
                if self.parked.exists():
                    os.rename(self.parked, self.target)
                    os.chmod(self.target, 0o644)

                acting = []

                def action():
                    acting.append(True)
                    self.park_original()
                    make()
                    acting.pop()
                chmod_calls = []
                real_chmod = os.chmod

                def counting(*args, **kwargs):
                    if not acting:
                        chmod_calls.append(args)
                    return real_chmod(*args, **kwargs)
                old = signal.signal(signal.SIGALRM, timed_out)
                signal.alarm(10)
                try:
                    with self.after_final_stat(action), mock.patch.object(os, "chmod", counting):
                        report = store_check.fix_modes(self.root)
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old)
                self.assertEqual(report, {"fixed": [], "skipped": [SHOWN]})
                self.assertEqual(chmod_calls, [])
                self.assertEqual(mode(self.parked), 0o644)
                if label != "missing":
                    self.assertEqual(stat.S_IMODE(os.lstat(self.target).st_mode), 0o755 if label == "directory" else 0o644)
                    if label == "directory":
                        os.rmdir(self.target)
                    else:
                        os.remove(self.target)


class LifetimeTests(DescriptorBase):
    def record_fds(self):
        real_open, fds = os.open, []

        def spy(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if Path(path) == self.target:
                fds.append(fd)
            return fd
        return fds, mock.patch.object(os, "open", spy)

    def assert_closed(self, fds):
        self.assertTrue(fds)
        for fd in fds:
            with self.assertRaises(OSError) as raised:
                os.fstat(fd)
            self.assertEqual(raised.exception.errno, errno.EBADF)

    def test_fd_lifetime_covers_success_skip_fstat_and_fchmod_errors(self):
        sentinel = os.open(self.outside, os.O_RDONLY)
        self.addCleanup(lambda: os.close(sentinel))
        before = open_fds()

        fds, spy = self.record_fds()
        with spy:
            self.assertEqual(store_check.fix_modes(self.root), {"fixed": [SHOWN], "skipped": []})
        self.assert_closed(fds)

        os.chmod(self.target, 0o644)
        fds, spy = self.record_fds()
        swap = self.after_final_stat(lambda: (self.park_original(), self.target.write_bytes(DATA), os.chmod(self.target, 0o644)))
        with spy, swap:
            self.assertEqual(store_check.fix_modes(self.root), {"fixed": [], "skipped": [SHOWN]})
        self.assert_closed(fds)
        os.remove(self.target)
        os.rename(self.parked, self.target)

        os.chmod(self.target, 0o644)
        fds, spy = self.record_fds()
        with spy, mock.patch.object(os, "fstat", side_effect=OSError(errno.EIO, "synthetic fstat")):
            with self.assertRaises(OSError):
                store_check.fix_modes(self.root)
        self.assert_closed(fds)
        self.assertEqual(mode(self.target), 0o644)

        fds, spy = self.record_fds()
        with spy, mock.patch.object(os, "fchmod", side_effect=OSError(errno.EIO, "synthetic fchmod")):
            with self.assertRaises(OSError):
                store_check.fix_modes(self.root)
        self.assert_closed(fds)
        self.assertEqual(mode(self.target), 0o644)

        self.assertEqual(os.fstat(sentinel).st_size, len(OUTSIDE_DATA))
        for _ in range(20):
            with mock.patch.object(os, "fchmod", side_effect=OSError(errno.EIO, "synthetic")):
                with self.assertRaises(OSError):
                    store_check.fix_modes(self.root)
            self.assertEqual(store_check.fix_modes(self.root), {"fixed": [SHOWN], "skipped": []})
            os.chmod(self.target, 0o644)
        self.assertEqual(open_fds(), before)

    def test_failed_repair_keeps_earlier_repairs_and_names_no_false_fixed_entry(self):
        os.chmod(self.private, 0o755)
        real_fchmod, calls = os.fchmod, []

        def failing(fd, value):
            calls.append(value)
            if len(calls) == 2:
                raise OSError(errno.EIO, "synthetic fchmod")
            return real_fchmod(fd, value)
        with mock.patch.object(os, "fchmod", failing):
            with self.assertRaises(OSError):
                store_check.fix_modes(self.root)
        self.assertEqual(mode(self.private), 0o700)
        self.assertEqual(calls, [0o700, 0o600])
        self.assertEqual(mode(self.target), 0o644)


class RefusalTests(DescriptorBase):
    def assert_refused_without_mutation(self, patcher):
        chmod_calls = []
        with patcher, mock.patch.object(os, "chmod", lambda *a, **k: chmod_calls.append(a)):
            with self.assertRaises(ValueError) as raised:
                store_check.fix_modes(self.root)
        self.assertEqual(str(raised.exception), UNSUPPORTED)
        self.assertEqual(chmod_calls, [])
        self.assertEqual(mode(self.target), 0o644)

    def test_platform_capability_refusal_has_no_unsafe_fallback(self):
        self.assert_refused_without_mutation(mock.patch.object(os, "O_NOFOLLOW", 0))
        self.assert_refused_without_mutation(mock.patch.object(os, "fchmod", None))

    def test_open_permission_error_propagates_and_changes_nothing(self):
        def denied(path, *args, **kwargs):
            raise PermissionError(errno.EACCES, "synthetic denial")
        with mock.patch.object(os, "open", denied):
            with self.assertRaises(PermissionError):
                store_check.fix_modes(self.root)
        self.assertEqual(mode(self.target), 0o644)
        self.outside_unchanged()

    @unittest.skipIf(os.geteuid() == 0, "root ignores permission bits")
    def test_unreadable_candidate_refuses_instead_of_pathname_chmod(self):
        os.chmod(self.target, 0o070)
        with self.assertRaises(PermissionError):
            store_check.fix_modes(self.root)
        self.assertEqual(mode(self.target), 0o070)


class CompatibilityTests(DescriptorBase):
    def test_all_existing_candidate_kinds_and_reports_stay_compatible(self):
        os.chmod(self.private, 0o755)
        os.chmod(self.objects, 0o755)
        extra = {"ledger.sqlite": 0o644, "ledger.sqlite-journal": 0o660,
                 "ledger.sqlite-wal": 0o664, "ledger.sqlite-shm": 0o640}
        for name, bits in extra.items():
            (self.private / name).write_bytes(b"synthetic " + name.encode())
            os.chmod(self.private / name, bits)
        private_file = self.objects / ("b" * 64)
        private_file.write_bytes(b"owner only")
        os.chmod(private_file, 0o600)
        before = {p.name: p.read_bytes() for p in (*self.objects.iterdir(), *self.private.glob("ledger*"))}
        stamps = {p: os.stat(p).st_mtime_ns for p in (self.target, private_file)}
        members = sorted(p.name for p in self.private.rglob("*"))
        self.assertEqual([p["name"] for p in store_check.mode_problems(self.root)],
                         [".", "objects", "ledger.sqlite", "ledger.sqlite-journal", "ledger.sqlite-wal",
                          "ledger.sqlite-shm", SHOWN])
        report = store_check.fix_modes(self.root)
        self.assertEqual(report, {"fixed": [".", "objects", "ledger.sqlite", "ledger.sqlite-journal",
                                            "ledger.sqlite-wal", "ledger.sqlite-shm", SHOWN], "skipped": []})
        self.assertEqual((mode(self.private), mode(self.objects)), (0o700, 0o700))
        for name in extra:
            self.assertEqual(mode(self.private / name), 0o600)
        self.assertEqual(mode(self.target), 0o600)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (*self.objects.iterdir(), *self.private.glob("ledger*"))})
        self.assertEqual(stamps, {p: os.stat(p).st_mtime_ns for p in stamps})
        self.assertEqual(members, sorted(p.name for p in self.private.rglob("*")))
        self.assertEqual(store_check.fix_modes(self.root), {"fixed": [], "skipped": []})

    def test_absent_private_store_reports_nothing_and_creates_nothing(self):
        import shutil
        shutil.rmtree(self.private)
        self.assertEqual(store_check.fix_modes(self.root), {"fixed": [], "skipped": []})
        self.assertFalse(self.private.exists())

    def test_symlinked_private_still_stops_before_any_repair(self):
        moved = self.base / "private-elsewhere"
        os.rename(self.private, moved)
        os.chmod(moved, 0o755)
        self.private.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError):
            store_check.fix_modes(self.root)
        self.assertEqual(mode(moved), 0o755)

    def test_documentation_states_the_descriptor_contract(self):
        text = " ".join((REPO_ROOT / "docs" / "STORE-CHECK.md").read_text().split())
        for phrase in ("fchmod", UNSUPPORTED, "same device and inode", "does not roll back"):
            self.assertIn(phrase, text)


class CliTests(unittest.TestCase):
    def cli(self, root, command, *arguments, code=0):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(root), *map(str, arguments)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    @staticmethod
    def blocks(text):
        decoder, found = json.JSONDecoder(), []
        while text.strip():
            block, end = decoder.raw_decode(text.lstrip())
            found.append(block)
            text = text.lstrip()[end:]
        return found

    def test_real_cli_ordinary_repair_and_read_only_check_remain_separate(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-mode-fd-cli-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(os.umask, os.umask(0o077))
        base = Path(temporary.name)
        root, outside = base / "campaign", base / "outside.txt"
        outside.write_bytes(OUTSIDE_DATA)
        os.chmod(outside, 0o644)
        self.cli(root, "init", "--name", "Synthetic Campaign", "--county", "Synthetic", "--state", "CA")
        source = base / "first.txt"
        source.write_bytes(DATA)
        self.cli(root, "ingest", "--file", source, "--source-id", "synthetic-source")
        ledger = root / "private" / "ledger.sqlite"
        stored = next((root / "private" / "objects").iterdir())
        shown = "objects/" + stored.name[:12]
        os.chmod(ledger, 0o644)
        os.chmod(stored, 0o644)

        # check-only: read-only, no repair descriptor or fchmod
        stamps = {p: os.stat(p).st_mtime_ns for p in (ledger, stored)}
        opened = []
        real_open = os.open
        with mock.patch.object(os, "fchmod") as fchmod, \
                mock.patch.object(os, "open", lambda path, *a, **k: opened.append(path) or real_open(path, *a, **k)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as raised:
                cli_module.status(root, check=True)
        self.assertEqual(raised.exception.code, 3)
        fchmod.assert_not_called()
        self.assertEqual((mode(ledger), mode(stored)), (0o644, 0o644))
        self.assertEqual(stamps, {p: os.stat(p).st_mtime_ns for p in stamps})
        self.assertEqual([p["problem"] for p in self.blocks(out.getvalue())[-1]["check"]["problems"]],
                         ["loose_mode", "loose_mode"])

        # race seam: the object becomes an outside-pointing symlink after its final lstat
        real, calls, parked = store_check._lstat, [], base / "parked"

        def wrapper(path):
            info = real(path)
            if Path(path) == stored:
                calls.append(path)
                if len(calls) == 2:
                    os.rename(stored, parked)
                    stored.symlink_to(outside)
            return info
        with mock.patch.object(store_check, "_lstat", wrapper), contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as raised:
                cli_module.status(root, fix=True)
        self.assertEqual(raised.exception.code, 3)
        first = self.blocks(out.getvalue())[0]
        self.assertEqual(first, {"fixed": ["ledger.sqlite"], "skipped": [shown]})
        self.assertEqual(mode(outside), 0o644)
        self.assertEqual(outside.read_bytes(), OUTSIDE_DATA)
        self.assertEqual(mode(parked), 0o644)

        # ordinary real CLI repair afterwards
        stored.unlink()
        os.rename(parked, stored)
        result = self.cli(root, "status", "--fix-modes", code=0)
        blocks = self.blocks(result.stdout)
        self.assertEqual(blocks[0], {"fixed": [shown], "skipped": []})
        self.assertEqual(blocks[-1]["check"]["problems"], [])
        self.assertEqual(mode(stored), 0o600)
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), stored.name)


if __name__ == "__main__":
    unittest.main()
