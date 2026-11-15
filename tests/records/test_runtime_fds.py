"""Real Linux sockets and subprocesses; only synthetic temporary directories."""
from array import array
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import select
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from campaign_tool.records import runtime_fds as rf

TOOL = REPO / "tools/records_runtime_fds.py"
FLAGS = rf.DIRECTORY_FLAGS


def ready(stream):
    if not select.select([stream], [], [], 10)[0]:
        raise AssertionError("child readiness timed out")
    line = stream.readline().strip()
    if not line:
        raise AssertionError("child exited before readiness")
    return line


@contextmanager
def session(root, *, runtime=None, reports=None, status=0, foreign=False):
    runtime, reports = runtime or root / "runtime", reports or root / "reports"
    private = root / "sockets"
    for path in (runtime, reports, private):
        path.mkdir(mode=0o700, exist_ok=True)
    command = [sys.executable, "-B", str(TOOL)]
    if foreign:
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "foreign-helper"]
    command += ["--runtime", str(runtime), "--reports", str(reports), "--socket-dir", str(private), "--",
                sys.executable, "-B", str(Path(__file__).resolve()), "wait", "{socket}",
                "prefix {socket} suffix {socket}", str(status)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, cwd=REPO)
    try:
        path = ready(process.stdout)
        yield Path(path), process
    finally:
        process.stdin.close()
        process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()


@contextmanager
def server(root, *, payload=rf.MARKER, count=2, regular=False, pause=False):
    directory = os.open(root, FLAGS)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = root / "test.sock"
    listener.bind(f"/proc/self/fd/{directory}/test.sock")
    os.chmod(path, 0o600)
    listener.listen(1)
    errors = []

    def serve():
        fds = []
        try:
            with listener.accept()[0] as peer:
                if pause:
                    peer.settimeout(1)
                    peer.recv(1)
                else:
                    for _ in range(count):
                        fds.append(os.open(root / "regular" if regular else root,
                                           os.O_RDONLY if regular else FLAGS))
                    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", fds))] if fds else []
                    peer.sendmsg([payload], ancillary)
        except (BrokenPipeError, ConnectionResetError):
            pass  # a refused peer may close before the synthetic server sends
        except Exception as error:
            errors.append(error)
        finally:
            for fd in fds:
                os.close(fd)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield path
    finally:
        thread.join(5)
        listener.close()
        os.close(directory)
        path.unlink(missing_ok=True)
        if thread.is_alive() or errors:
            raise AssertionError(str(errors) or "server did not finish")


class RuntimeTransport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def error(self, path, code, **kwargs):
        with self.assertRaises(rf.RuntimeSocketError) as caught:
            with rf.receive_runtime_fds(path, **kwargs):
                self.fail("unexpected handoff")
        self.assertEqual(caught.exception.code, code)

    def test_real_handoff_and_child_status(self):
        with session(self.root, status=7) as (path, process):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with rf.receive_runtime_fds(path) as source:
                fds = [source.runtime_fd, source.reports_fd]
                for fd, directory in zip(fds, ("runtime", "reports")):
                    self.assertEqual(os.fstat(fd).st_ino, (self.root / directory).stat().st_ino)
                    self.assertFalse(os.get_inheritable(fd))
                self.assertEqual(source.runtime_source, "socket")
                self.assertFalse(path.exists())
                self.error(path, rf.RUNTIME_SOCKET_UNAVAILABLE)
            for fd in fds:
                with self.assertRaises(OSError):
                    os.fstat(fd)
        self.assertEqual(process.returncode, 7)
        self.assertEqual(list((self.root / "sockets").iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["reports", "runtime", "sockets"])

    def test_command_exit_and_abrupt_death_leave_no_reusable_source(self):
        for killed in (False, True):
            with self.subTest(killed=killed):
                with session(self.root) as (path, process):
                    if killed:
                        process.kill()
                        process.wait(timeout=10)
                    else:
                        process.stdin.write("done\n")
                        process.stdin.flush()
                        process.wait(timeout=10)
                    self.error(path, rf.RUNTIME_SOCKET_UNAVAILABLE)
                path.unlink(missing_ok=True)  # only the test cleans a SIGKILL's dead inode

    def test_foreign_uid_consumes_attempt(self):
        with session(self.root, foreign=True) as (path, process):
            self.error(path, rf.RUNTIME_SOCKET_UNAVAILABLE)
            self.error(path, rf.RUNTIME_SOCKET_UNAVAILABLE)

    def test_bad_handoff_closes_every_fd(self):
        (self.root / "regular").write_text("synthetic")
        cases = [(0, False, rf.MARKER), (1, False, rf.MARKER), (3, False, rf.MARKER),
                 (2, True, rf.MARKER), (2, False, b"wrong"), (2, False, rf.MARKER + b"extra"),
                 (2, False, b"partial")]
        for count, regular, payload in cases:
            with self.subTest(count=count, regular=regular, payload=payload):
                before = len(os.listdir("/proc/self/fd"))
                with server(self.root, count=count, regular=regular, payload=payload) as path:
                    self.error(path, rf.RUNTIME_SOCKET_UNSAFE)
                self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        with server(self.root, count=0, payload=b"") as path:
            self.error(path, rf.RUNTIME_SOCKET_UNAVAILABLE)
        with server(self.root, pause=True) as path:
            self.error(path, rf.RUNTIME_SOCKET_UNAVAILABLE, timeout=0.02)
        # Force ancillary truncation with a real handoff, keeping real recvmsg semantics.
        real_socket = rf.socket.socket
        class SmallControl:
            def __init__(self, *args, **kwargs):
                self.peer = real_socket(*args, **kwargs)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.peer.close()
            def __getattr__(self, name):
                return getattr(self.peer, name)
            def recvmsg(self, size, control, flags):
                return self.peer.recvmsg(size, 0, flags)
        with server(self.root) as path:
            with mock.patch.object(rf.socket, "socket", SmallControl):
                self.error(path, rf.RUNTIME_SOCKET_UNSAFE)

    def test_unsafe_parent_entry_and_peer(self):
        private = self.root / "sockets"
        private.mkdir(mode=0o750)
        os.chmod(private, 0o750)
        self.error(private / "missing", rf.RUNTIME_SOCKET_UNSAFE)
        command = [sys.executable, "-B", str(TOOL), "--runtime", str(self.root), "--reports", str(self.root),
                   "--socket-dir", str(private), "--", "/nonexistent-must-not-launch"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stderr), (2, "RUNTIME_SOCKET_UNSAFE\n"))
        self.assertEqual(list(private.iterdir()), [])
        os.chmod(private, 0o700)
        link = self.root / "link"
        link.symlink_to(private)
        self.error(link / "missing", rf.RUNTIME_SOCKET_UNSAFE)
        entry = private / "entry"
        entry.write_text("not a socket")
        self.error(entry, rf.RUNTIME_SOCKET_UNSAFE)
        entry.unlink()
        entry.symlink_to(self.root / "missing")
        self.error(entry, rf.RUNTIME_SOCKET_UNSAFE)
        entry.unlink()
        with server(private) as path:
            os.chmod(path, 0o640)
            self.error(path, rf.RUNTIME_SOCKET_UNSAFE)
            os.chmod(path, 0o600)
            real_unpack = rf.struct.unpack
            with mock.patch.object(rf.struct, "unpack", side_effect=lambda fmt, raw:
                                   (1, os.geteuid() + 1, 1) if fmt == "3i" else real_unpack(fmt, raw)):
                self.error(path, rf.RUNTIME_SOCKET_UNSAFE)
        with session(self.root) as (path, process):
            path.unlink()
            path.write_text("replacement")
            process.stdin.write("done\n")
            process.stdin.flush()
            process.wait(timeout=10)
            self.assertEqual(path.read_text(), "replacement")

    def test_missing_input_directory_is_not_created(self):
        private = self.root / "private"
        private.mkdir(mode=0o700)
        missing = self.root / "missing"
        result = subprocess.run([sys.executable, "-B", str(TOOL), "--runtime", str(missing), "--reports",
                                 str(self.root), "--socket-dir", str(private), "--", "/nonexistent"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(missing.exists())
        self.assertEqual(list(private.iterdir()), [])


if __name__ == "__main__":
    if sys.argv[1:2] == ["wait"]:
        path, expanded, status = sys.argv[2:]
        assert expanded == f"prefix {path} suffix {path}"
        print(path, flush=True)
        sys.stdin.readline()
        sys.exit(int(status))
    if sys.argv[1:2] == ["foreign-helper"]:
        spec = importlib.util.spec_from_file_location("host_tool", TOOL)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        tool._peer_uid = lambda peer: os.geteuid() + 1  # credential seam only; actual socket/handoff path
        sys.exit(tool.main(sys.argv[2:]))
    if sys.argv[1:2] == ["probe"]:
        import fcntl
        with open(sys.argv[2], "rb") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                sys.exit(3)
        sys.exit(0)
    if sys.argv[1:2] == ["holder"]:
        import fcntl
        with open(sys.argv[2], "rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            print("held", flush=True)
            sys.stdin.readline()
        sys.exit(0)
    unittest.main()
