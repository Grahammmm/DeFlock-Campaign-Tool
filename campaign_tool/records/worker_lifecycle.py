"""Linux-only, private worker supervisor and authenticated quiescence cancellation.

Run in a dedicated, single-threaded interpreter; this is not a security sandbox.
No numerical PID or process-group kill is used. See the lifecycle contract in docs.
"""
import argparse
import ctypes
import hmac
import json
import math
import os
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

MAX_WALL_SECONDS = 2700
MAX_GRACE_SECONDS = 30
MAX_WAIT_SECONDS = 65
MAX_OWNED = 256
HARD_CHILD_CAP = 4096
MAX_MESSAGE = 4096
POLL_SECONDS = 0.025
EXIT_CODES = {"completed": 0, "worker_failed": 10, "cancelled": 20,
              "timed_out": 21, "invalid_state": 30, "unsupported": 31,
              "not_quiescent": 32, "capacity_exceeded": 33}


class Rejected(Exception):
    """Fixed-code rejection; deliberately carries no input or exception text."""


class Unsupported(Exception):
    """Required Linux ownership primitives are unavailable."""


def _result(status, quiescent=False, worker_exit=None, escalated=False):
    return {"schema": 1, "status": status, "quiescent": quiescent,
            "worker_exit": worker_exit, "escalated": escalated}


def exit_code(receipt, *, cancelling=False):
    """Cancel returns zero only for a live, authenticated quiescence ACK."""
    if receipt["status"] == "completed" and not receipt["quiescent"]:
        return EXIT_CODES["not_quiescent"]
    if cancelling and receipt["status"] in {
            "completed", "worker_failed", "cancelled", "timed_out"}:
        return 0 if receipt["quiescent"] else EXIT_CODES["not_quiescent"]
    return EXIT_CODES[receipt["status"]]


def _number(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise Rejected()
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected()
        result[key] = value
    return result


def _decode(raw):
    if len(raw) > MAX_MESSAGE:
        raise Rejected()
    return json.loads(raw, object_pairs_hook=_pairs)


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _read_proc(path):
    with open(path, "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise Rejected()
    return raw


def _boot():
    value = _read_proc("/proc/sys/kernel/random/boot_id").decode().strip()
    if not re.fullmatch(r"[0-9a-f-]{36}", value):
        raise Unsupported()
    return value


def _identity(pid):
    # comm may contain spaces and parentheses. Field 22 is starttime.
    fields = _read_proc(f"/proc/{pid}/stat").rsplit(b")", 1)[1].split()
    uid_line = next(line for line in _read_proc(f"/proc/{pid}/status").splitlines()
                    if line.startswith(b"Uid:"))
    uids = [int(value) for value in uid_line.split()[1:]]
    if len(uids) != 4 or any(value != os.getuid() for value in uids):
        raise Rejected()
    return {"pid": pid, "start": int(fields[19]), "uid": uids[0]}, int(fields[1])


def _children():
    raw = _read_proc(f"/proc/self/task/{os.getpid()}/children")
    children = [int(value) for value in raw.split()]
    if len(children) > HARD_CHILD_CAP:
        raise Rejected()
    return children


def _linux():
    if (sys.platform != "linux" or os.getuid() == 0 or os.getuid() != os.geteuid()
            or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal")):
        raise Unsupported()
    try:
        fd = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(fd, 0)
        finally:
            os.close(fd)
        _boot()
    except OSError:
        raise Unsupported() from None


class _Directory:
    """All file/socket operations are anchored to a no-symlink directory FD."""

    def __init__(self, path, *, create=False):
        self.path = os.fspath(path)
        if not os.path.isabs(self.path):
            raise Rejected()
        parts = self.path.split("/")[1:]
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise Rejected()
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for index, part in enumerate(parts):
                if create and index == len(parts) - 1:
                    os.mkdir(part, 0o700, dir_fd=fd)
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=fd)
                os.close(fd)
                fd = new
            self.fd = fd
            self.inode = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            self.check()
        except BaseException:
            os.close(fd)
            raise

    def check(self):
        value = os.fstat(self.fd)
        named = os.stat(self.path, follow_symlinks=False)
        if (not stat.S_ISDIR(named.st_mode) or value.st_uid != os.getuid()
                or stat.S_IMODE(value.st_mode) != 0o700
                or (named.st_dev, named.st_ino) != self.inode):
            raise Rejected()

    def read(self, name):
        self.check()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > MAX_MESSAGE):
                raise Rejected()
            raw = os.read(fd, MAX_MESSAGE + 1)
            if len(raw) != info.st_size:
                raise Rejected()
            fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            return raw, fingerprint
        finally:
            os.close(fd)

    def write(self, name, value):
        self.check()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self.fd)
        try:
            os.fchmod(fd, 0o600)
            raw = _encode(value)
            if len(raw) > MAX_MESSAGE or os.write(fd, raw) != len(raw):
                raise Rejected()
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.fd)

    def socket_path(self):
        return f"/proc/self/fd/{self.fd}/control.sock"

    def close(self):
        os.close(self.fd)


def _validate_state(value):
    keys = {"schema", "identity", "boot", "run_id", "token", "wall_seconds",
            "term_seconds", "kill_seconds", "max_owned"}
    if not isinstance(value, dict) or set(value) != keys or type(value["schema"]) is not int or value["schema"] != 1:
        raise Rejected()
    identity = value["identity"]
    if (not isinstance(identity, dict) or set(identity) != {"pid", "start", "uid"}
            or any(type(item) is not int for item in identity.values())
            or identity["pid"] <= 1 or identity["start"] <= 0 or identity["uid"] != os.getuid()
            or value["boot"] != _boot()):
        raise Rejected()
    for name, width in (("run_id", 32), ("token", 64)):
        if not isinstance(value[name], str) or not re.fullmatch(r"[0-9a-f]{%d}" % width, value[name]):
            raise Rejected()
    _number(value["wall_seconds"], MAX_WALL_SECONDS)
    _number(value["term_seconds"], MAX_GRACE_SECONDS)
    _number(value["kill_seconds"], MAX_GRACE_SECONDS)
    if type(value["max_owned"]) is not int or not 1 <= value["max_owned"] <= MAX_OWNED:
        raise Rejected()
    return value


def _peer(sock):
    return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))


def _signal_child(pid, sig):
    # Direct children cannot have their PIDs reused before this sole reaper waits.
    # Still pin and recheck start/UID/parent around pidfd_open, never kill(pid).
    fd = None
    try:
        before, parent = _identity(pid)
        if parent != os.getpid():
            raise Rejected()
        fd = os.pidfd_open(pid)
        after, parent = _identity(pid)
        if before != after or parent != os.getpid():
            raise Rejected()
        signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass
    finally:
        if fd is not None:
            os.close(fd)


def supervise(command, state_dir, *, wall_seconds=MAX_WALL_SECONDS,
              term_seconds=5, kill_seconds=5, max_owned=MAX_OWNED):
    """Return fixed metadata; requires a fresh state_dir and exclusive child ownership.

    Worker output is discarded. TERM/INT/HUP to this supervisor request cleanup.
    KILL/crash of the supervisor cannot produce an acknowledgement.
    """
    _number(wall_seconds, MAX_WALL_SECONDS)
    _number(term_seconds, MAX_GRACE_SECONDS)
    _number(kill_seconds, MAX_GRACE_SECONDS)
    if type(max_owned) is not int or not 1 <= max_owned <= MAX_OWNED or not command:
        raise Rejected()
    _linux()
    if (len(os.listdir("/proc/self/task")) != 1 or _children()
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
        raise Unsupported()
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
        raise Unsupported()
    if libc.prctl(36, ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
        raise Unsupported()
    directory = server = worker = None
    clients = []
    saved_handlers = {}
    requested = [False]
    status = None
    shutdown_at = None
    worker_exit = None
    escalated = False
    quiescent = False
    started = time.monotonic()
    try:
        directory = _Directory(state_dir, create=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(directory.socket_path())
        os.chmod(directory.socket_path(), 0o600)
        server.listen(8)
        server.setblocking(False)
        identity, _ = _identity(os.getpid())
        state = {"schema": 1, "identity": identity, "boot": _boot(),
                 "run_id": os.urandom(16).hex(), "token": os.urandom(32).hex(),
                 "wall_seconds": wall_seconds, "term_seconds": term_seconds,
                 "kill_seconds": kill_seconds, "max_owned": max_owned}
        directory.write("state.json", state)
        raw, fingerprint = directory.read("state.json")
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            saved_handlers[sig] = signal.signal(sig, lambda *_: requested.__setitem__(0, True))
        worker = subprocess.Popen(command, start_new_session=True, close_fds=True,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        while True:
            now = time.monotonic()
            try:
                if directory.read("state.json") != (raw, fingerprint):
                    raise Rejected()
                children = _children()
                if len(children) > max_owned and status not in {"invalid_state", "capacity_exceeded"}:
                    status = "capacity_exceeded"
                    shutdown_at = shutdown_at if shutdown_at is not None else now
            except Exception:
                status = "invalid_state"
                shutdown_at = shutdown_at if shutdown_at is not None else now
                children = []
                # Control-state faults do not revoke kernel-derived child ownership.
                try:
                    children = _children()
                except Exception:
                    pass
            if requested[0] and status is None:
                status, shutdown_at = "cancelled", now
            if now - started >= wall_seconds and status is None:
                status, shutdown_at = "timed_out", now
            # One bounded handshake per tick, including malicious/partial clients.
            connection = None
            try:
                connection, _ = server.accept()
                connection.settimeout(POLL_SECONDS)
                _, uid, _ = _peer(connection)
                request = _decode(connection.recv(MAX_MESSAGE + 1))
                if (uid != os.getuid() or not isinstance(request, dict)
                        or set(request) != {"schema", "run_id", "token"}
                        or type(request["schema"]) is not int or request["schema"] != 1
                        or request["run_id"] != state["run_id"]
                        or not isinstance(request["token"], str)
                        or not hmac.compare_digest(request["token"], state["token"])
                        or status in {"invalid_state", "capacity_exceeded"} or len(clients) >= 8):
                    raise Rejected()
                # Recheck pinned control state immediately before authorizing.
                if directory.read("state.json") != (raw, fingerprint):
                    raise Rejected()
                clients.append(connection)
                connection = None
                if status is None:
                    status, shutdown_at = "cancelled", time.monotonic()
            except BlockingIOError:
                pass
            except Exception:
                if connection is not None:
                    try:
                        connection.sendall(_encode(_result("invalid_state")))
                    except OSError:
                        pass
            finally:
                if connection is not None:
                    connection.close()
            if shutdown_at is not None:
                age = time.monotonic() - shutdown_at
                sig = signal.SIGKILL if age >= term_seconds else signal.SIGTERM
                escalated = escalated or sig == signal.SIGKILL
                for pid in children:
                    try:
                        _signal_child(pid, sig)
                    except Exception:
                        status = "invalid_state"
            # waitpid ECHILD, not leader exit or an empty proc snapshot, proves
            # quiescence. Orphan descendants are adopted by this subreaper.
            for _ in range(HARD_CHILD_CAP):
                try:
                    pid, wait_status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    quiescent = True
                    break
                if pid == 0:
                    break
                if pid == worker.pid:
                    worker_exit = os.waitstatus_to_exitcode(wait_status)
                    worker.returncode = worker_exit
                    if status is None:
                        status = "completed" if worker_exit == 0 else "worker_failed"
                        shutdown_at = time.monotonic()
            if quiescent:
                break
            if shutdown_at is not None and time.monotonic() - shutdown_at >= term_seconds + kill_seconds:
                status = "not_quiescent"
                break
            time.sleep(POLL_SECONDS)
        result = _result(status or "invalid_state", quiescent, worker_exit, escalated)
        envelope = {"identity": state["identity"], "boot": state["boot"],
                    "run_id": state["run_id"], "result": result}
        try:
            directory.write("receipt.json", envelope)
        except Exception:
            result = _result("invalid_state", quiescent, worker_exit, escalated)
            envelope["result"] = result
        # Even a persisted error receipt is never a successful cancel ACK.
        for connection in clients:
            try:
                connection.settimeout(POLL_SECONDS)
                connection.sendall(_encode(envelope))
            except OSError:
                pass
        return result
    finally:
        for connection in clients:
            connection.close()
        if server is not None:
            server.close()
        if directory is not None:
            directory.close()
        for sig, handler in saved_handlers.items():
            signal.signal(sig, handler)
        libc.prctl(36, ctypes.c_ulong(previous.value), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))


def cancel(state_dir, *, wait_seconds=MAX_WAIT_SECONDS):
    """Authenticate a live supervisor and await its bound, persisted receipt.

    Never signals a process itself and never accepts a stale disk-only receipt.
    """
    _number(wait_seconds, MAX_WAIT_SECONDS)
    _linux()
    directory = _Directory(state_dir)
    pidfd = None
    try:
        raw, fingerprint = directory.read("state.json")
        state = _validate_state(_decode(raw))
        expected = state["identity"]
        identity, _ = _identity(expected["pid"])
        if identity != expected:
            raise Rejected()
        pidfd = os.pidfd_open(expected["pid"])
        identity, _ = _identity(expected["pid"])
        if identity != expected or select.select([pidfd], [], [], 0)[0]:
            raise Rejected()
        deadline = time.monotonic() + wait_seconds
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(wait_seconds)
            connection.connect(directory.socket_path())
            peer_pid, peer_uid, _ = _peer(connection)
            if peer_pid != expected["pid"] or peer_uid != expected["uid"]:
                raise Rejected()
            if directory.read("state.json") != (raw, fingerprint):
                raise Rejected()
            connection.sendall(_encode({"schema": 1, "run_id": state["run_id"], "token": state["token"]}))
            response = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _result("not_quiescent")
                connection.settimeout(remaining)
                chunk = connection.recv(MAX_MESSAGE + 1 - len(response))
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > MAX_MESSAGE:
                    raise Rejected()
            envelope = _decode(response)
        if (not isinstance(envelope, dict) or set(envelope) != {"identity", "boot", "run_id", "result"}
                or envelope["identity"] != expected or envelope["boot"] != state["boot"]
                or envelope["run_id"] != state["run_id"]
                or directory.read("state.json") != (raw, fingerprint)):
            raise Rejected()
        result = envelope["result"]
        if (not isinstance(result, dict) or set(result) != set(_result("invalid_state"))
                or type(result["schema"]) is not int or result["schema"] != 1
                or result["status"] not in EXIT_CODES
                or type(result["quiescent"]) is not bool or type(result["escalated"]) is not bool
                or (result["worker_exit"] is not None and
                    (type(result["worker_exit"]) is not int or not -255 <= result["worker_exit"] <= 255))):
            raise Rejected()
        if _decode(directory.read("receipt.json")[0]) != envelope:
            raise Rejected()
        return result
    except (TimeoutError, socket.timeout):
        return _result("not_quiescent")
    finally:
        if pidfd is not None:
            os.close(pidfd)
        directory.close()


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected()


def main(arguments=None):
    """Only fixed status metadata is printed, including for argument errors."""
    try:
        parser = _Parser(add_help=False)
        subs = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
        run = subs.add_parser("supervise", add_help=False)
        run.add_argument("--state-dir", required=True)
        run.add_argument("--wall-seconds", type=float, default=MAX_WALL_SECONDS)
        run.add_argument("--term-seconds", type=float, default=5)
        run.add_argument("--kill-seconds", type=float, default=5)
        run.add_argument("--max-owned", type=int, default=MAX_OWNED)
        run.add_argument("command", nargs=argparse.REMAINDER)
        stop = subs.add_parser("cancel", add_help=False)
        stop.add_argument("--state-dir", required=True)
        stop.add_argument("--wait-seconds", type=float, default=MAX_WAIT_SECONDS)
        args = parser.parse_args(arguments)
        if args.action == "supervise":
            command = args.command
            if not command or command[0] != "--":
                raise Rejected()
            result = supervise(command[1:], args.state_dir, wall_seconds=args.wall_seconds,
                               term_seconds=args.term_seconds, kill_seconds=args.kill_seconds,
                               max_owned=args.max_owned)
        else:
            result = cancel(args.state_dir, wait_seconds=args.wait_seconds)
    except Unsupported:
        result = _result("unsupported")
    except Exception:
        result = _result("invalid_state")
    print(json.dumps(result, sort_keys=True), flush=True)
    return exit_code(result, cancelling="args" in locals() and args.action == "cancel")


if __name__ == "__main__":
    raise SystemExit(main())
