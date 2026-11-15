"""One-use Linux directory-descriptor transport. No runtime state is serialized."""
from array import array
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import stat
import struct

RUNTIME_SOCKET_UNSAFE = "RUNTIME_SOCKET_UNSAFE"
RUNTIME_SOCKET_UNAVAILABLE = "RUNTIME_SOCKET_UNAVAILABLE"
MARKER = b"records-runtime-fds-v1"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class RuntimeSocketError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RuntimeFDs:
    runtime_fd: int
    reports_fd: int
    runtime_source: str = "socket"


def _private(info, kind):
    if not kind(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RuntimeSocketError(RUNTIME_SOCKET_UNSAFE)


@contextmanager
def receive_runtime_fds(socket_path, *, timeout=5.0):
    """Authenticate a private same-uid socket and own exactly two directory fds until exit."""
    parent_fd, received = None, []
    try:
        path = Path(socket_path)
        try:
            parent_fd = os.open(path.parent, DIRECTORY_FLAGS)
            _private(os.fstat(parent_fd), stat.S_ISDIR)
            _private(os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False), stat.S_ISSOCK)
        except FileNotFoundError:
            raise RuntimeSocketError(RUNTIME_SOCKET_UNAVAILABLE) from None
        except OSError:
            raise RuntimeSocketError(RUNTIME_SOCKET_UNSAFE) from None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(timeout)
            try:
                peer.connect(f"/proc/self/fd/{parent_fd}/{path.name}")
                uid = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
                if uid != os.geteuid():
                    raise RuntimeSocketError(RUNTIME_SOCKET_UNSAFE)
                data, ancillary, flags, _ = peer.recvmsg(
                    len(MARKER) + 1, socket.CMSG_SPACE(256 * array("i").itemsize), socket.MSG_CMSG_CLOEXEC)
            except (OSError, TimeoutError, ValueError):
                raise RuntimeSocketError(RUNTIME_SOCKET_UNAVAILABLE) from None
            # Collect before validating, so every installed descriptor closes on every refusal.
            for level, kind, raw in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    fds = array("i")
                    fds.frombytes(raw[:len(raw) - len(raw) % fds.itemsize])
                    received.extend(fds)
            if not data and not ancillary and not flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
                raise RuntimeSocketError(RUNTIME_SOCKET_UNAVAILABLE)
            if (data != MARKER or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC) or len(ancillary) != 1
                    or ancillary[0][:2] != (socket.SOL_SOCKET, socket.SCM_RIGHTS) or len(received) != 2
                    or len(ancillary[0][2]) != 2 * array("i").itemsize):
                raise RuntimeSocketError(RUNTIME_SOCKET_UNSAFE)
            try:
                for fd in received:
                    if not stat.S_ISDIR(os.fstat(fd).st_mode):
                        raise RuntimeSocketError(RUNTIME_SOCKET_UNSAFE)
                    os.set_inheritable(fd, False)
            except OSError:
                raise RuntimeSocketError(RUNTIME_SOCKET_UNSAFE) from None
        yield RuntimeFDs(*received)
    finally:
        for fd in received:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)
