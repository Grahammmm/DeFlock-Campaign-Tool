#!/usr/bin/env python3
"""Attended, one-use host directory handoff. Standalone Linux/stdlib; never opens a lock."""
import argparse
from array import array
import os
import secrets
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys

MARKER = b"records-runtime-fds-v1"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _peer_uid(peer):
    return struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def _unlink_owned(directory, name, identity):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino) == identity:
            os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--reports", required=True)
    parser.add_argument("--socket-dir", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("COMMAND required after --")
    fds, listener, child, identity, name = [], None, None, None, None
    handlers = {}

    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    try:
        for path in (args.runtime, args.reports, args.socket_dir):
            fd = os.open(path, DIRECTORY_FLAGS)
            fds.append(fd)
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("not_directory")
        directory = fds[2]
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise OSError("unsafe_directory")
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            handlers[sig] = signal.signal(sig, interrupted)
        name = "r-" + secrets.token_hex(12) + ".sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        mask = os.umask(0o077)
        try:
            listener.bind(f"/proc/self/fd/{directory}/{name}")
        finally:
            os.umask(mask)
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        os.chmod(name, 0o600, dir_fd=directory, follow_symlinks=False)
        listener.listen(1)
        path = os.path.join(os.path.abspath(args.socket_dir), name)
        child = subprocess.Popen([arg.replace("{socket}", path) for arg in command], close_fds=True)
        with selectors.DefaultSelector() as selector:
            selector.register(listener, selectors.EVENT_READ)
            while child.poll() is None:
                if not selector.select(0.05):
                    continue
                if child.poll() is not None:
                    break
                peer, _ = listener.accept()
                listener.close()
                listener = None
                _unlink_owned(directory, name, identity)
                try:
                    if _peer_uid(peer) == os.geteuid():
                        peer.settimeout(5)
                        peer.sendmsg([MARKER], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", fds[:2]))])
                except OSError:
                    pass  # a denied/disconnected consumer still consumes the one attempt
                finally:
                    peer.close()
                    for fd in fds[:2]:
                        os.close(fd)
                    fds[:2] = [None, None]
                break
        status = child.wait()
        return status if status >= 0 else 128 - status
    except (OSError, ValueError):
        print("RUNTIME_SOCKET_UNSAFE", file=sys.stderr)
        return 2
    finally:
        if listener is not None:
            listener.close()
        if identity is not None:
            _unlink_owned(fds[2], name, identity)
        for fd in fds:
            if fd is not None:
                os.close(fd)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
