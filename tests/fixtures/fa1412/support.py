"""Reversible, process-local boundaries for synthetic HTTPS fixtures only.

Not a hostile-code sandbox. The statically inspected portal import closure has
no import-time data reads or path-bound defaults. No policy file is ever loaded.
"""
import builtins
import contextlib
import io
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
from unittest.mock import patch


class OfflineViolation(RuntimeError):
    pass


@contextlib.contextmanager
def offline_boundary(root, repo):
    """Allow source/runtime reads and fixture-only writes; restore every hook."""
    root = Path(root).resolve()
    repo = Path(repo).resolve()
    root.mkdir(parents=True, exist_ok=True)
    read_roots = [root, repo, Path(sys.base_prefix).resolve(),
                  Path(sys.prefix).resolve()]
    read_roots.extend(Path(sysconfig.get_path(key)).resolve()
                      for key in ('stdlib', 'platstdlib', 'purelib', 'platlib'))
    original_open, original_io, original_os = builtins.open, io.open, os.open

    def check(path, writing=False):
        if isinstance(path, int):
            return
        path = Path(os.fsdecode(path)).resolve()
        roots = [root] if writing else read_roots
        if not any(path == base or base in path.parents for base in roots):
            raise OfflineViolation('fixture file boundary')

    def guarded_open(path, mode='r', *args, **kwargs):
        check(path, any(c in mode for c in 'wax+'))
        return original_open(path, mode, *args, **kwargs)

    def guarded_io(path, mode='r', *args, **kwargs):
        check(path, any(c in mode for c in 'wax+'))
        return original_io(path, mode, *args, **kwargs)

    def guarded_os(path, flags, *args, **kwargs):
        check(path, bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
        return original_os(path, flags, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise OfflineViolation('network/model/process/data access forbidden')

    with contextlib.ExitStack() as stack:
        for target, value in (
            ('builtins.open', guarded_open), ('io.open', guarded_io),
            ('os.open', guarded_os), ('socket.socket', forbidden),
            ('socket.getaddrinfo', forbidden), ('socket.create_connection', forbidden),
            ('subprocess.Popen', forbidden), ('os.system', forbidden),
            ('os.fork', forbidden), ('os.posix_spawn', forbidden),
            ('os.posix_spawnp', forbidden), ('sqlite3.connect', forbidden),
            ('multiprocessing.process.BaseProcess.start', forbidden),
        ):
            stack.enter_context(patch(target, value))
        stack.enter_context(patch.object(sys, 'dont_write_bytecode', True))
        stack.enter_context(patch.object(sys, 'pycache_prefix', str(root / 'bytecode')))
        stack.enter_context(patch.object(tempfile, 'tempdir', str(root)))
        yield


def hook_identity():
    return (builtins.open, io.open, os.open, socket.socket, socket.getaddrinfo,
            socket.create_connection, subprocess.Popen, sqlite3.connect,
            tempfile.tempdir, sys.dont_write_bytecode, sys.pycache_prefix)
