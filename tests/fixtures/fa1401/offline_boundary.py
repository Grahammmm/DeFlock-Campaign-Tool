"""FA1401 scoped data-file and SQLite boundary for synthetic receipt tests.

While installed, every Python-level file open, directory/link/rename/unlink
change and SQLite connection reachable from the ledger code is checked before
the real call runs. Writes are admitted only under the explicit write roots
(the run's generated fixture and temp roots). Reads are additionally admitted
under the repository and the running interpreter's own runtime. Anything else
is recorded as a hit and refused with PermissionError before the real call, so
the test or launcher that installed the boundary fails. Already-open file
descriptors (os.fdopen) pass, because the descriptor itself was admitted.

The boundary is scoped: install() saves every original attribute and restore()
puts the exact objects back, so unrelated tests see untouched process state.
This module imports no application code and needs no network or data path.
"""
import builtins
import io
import os
import sqlite3
import sqlite3.dbapi2
import sysconfig
import sys
from urllib.parse import parse_qs, unquote, urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

MARK = "fa1401_file_sentinel"
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_ABSENT = object()


def _fd_directory(fd):
    """Directory path behind a dir_fd (Linux /proc, or macOS F_GETPATH); None if unknown."""
    proc = "/proc/self/fd/%d" % fd
    if os.path.exists(proc):
        return os.readlink(proc)
    if fcntl is not None and hasattr(fcntl, "F_GETPATH"):
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
        return os.fsdecode(raw.split(b"\0", 1)[0])
    return None


def resolve(path, dir_fd=None, *, write):
    """Absolute real path judged for this access, or None when it cannot be resolved."""
    raw = os.fsdecode(os.fspath(path))
    if not os.path.isabs(raw):
        base = os.getcwd() if dir_fd is None else _fd_directory(dir_fd)
        if base is None:
            return None
        raw = os.path.join(base, raw)
    if write:
        # A write changes the final name itself, so only its directory is followed.
        head, tail = os.path.split(os.path.abspath(raw))
        return os.path.join(os.path.realpath(head), tail)
    return os.path.realpath(raw)


def _real(path):
    return os.path.realpath(os.fsdecode(os.fspath(path)))


def runtime_roots():
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, os.path.dirname(os.__file__)}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = sysconfig.get_paths().get(key)
        if value:
            roots.add(value)
    return sorted({_real(root) for root in roots if root})


def _within(path, roots):
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


class FileBoundary:
    def __init__(self, *, repo, write_roots, hits=None):
        self.write_roots = sorted({_real(root) for root in write_roots})
        self.read_roots = sorted(set(self.write_roots) | {_real(repo)} | set(runtime_roots()))
        self.hits = [] if hits is None else hits
        self.saved = []

    # -- policy ---------------------------------------------------------------------
    def admitted(self, path, write):
        if path is None:
            return False
        return _within(path, self.write_roots if write else self.read_roots)

    def check(self, name, accesses):
        for path, dir_fd, write in accesses:
            judged = resolve(path, dir_fd, write=write)
            if not self.admitted(judged, write):
                self.hits.append({"hook": name, "write": write, "path": judged or os.fsdecode(os.fspath(path))})
                raise PermissionError("%s:%s refused %s access outside the admitted fixture/runtime roots"
                                      % (MARK, name, "write" if write else "read"))

    # -- per-call access classification --------------------------------------------
    @staticmethod
    def _open(file, mode="r", *args, **kwargs):
        if isinstance(file, int):
            return []
        return [(file, None, any(flag in mode for flag in "wax+"))]

    @staticmethod
    def _os_open(path, flags, *args, dir_fd=None, **kwargs):
        return [(path, dir_fd, bool(flags & WRITE_FLAGS))]

    @staticmethod
    def _change(path, *args, dir_fd=None, **kwargs):
        return [(path, dir_fd, True)]

    @staticmethod
    def _pair(src, dst, *args, src_dir_fd=None, dst_dir_fd=None, **kwargs):
        return [(src, src_dir_fd, True), (dst, dst_dir_fd, True)]

    @staticmethod
    def _connect(database, *args, uri=False, **kwargs):
        if isinstance(database, (bytes, str)) and os.fsdecode(database) in ("", ":memory:"):
            return []
        text = os.fsdecode(os.fspath(database))
        if uri and text.startswith("file:"):
            parts = urlsplit(text)
            modes = parse_qs(parts.query).get("mode", [])
            location = unquote(parts.path)
            if modes == ["memory"] or location in ("", ":memory:"):
                return []
            return [(location, None, modes != ["ro"])]
        return [(text, None, True)]

    def targets(self):
        return ((builtins, "open", self._open), (io, "open", self._open), (os, "open", self._os_open),
                (os, "mkdir", self._change), (os, "rmdir", self._change), (os, "unlink", self._change),
                (os, "remove", self._change), (os, "rename", self._pair), (os, "replace", self._pair),
                (os, "link", self._pair), (sqlite3, "connect", self._connect),
                (sqlite3.dbapi2, "connect", self._connect))

    def wrap(self, name, classify, real):
        boundary = self

        def guarded(*args, **kwargs):
            boundary.check(name, classify(*args, **kwargs))
            return real(*args, **kwargs)
        setattr(guarded, MARK, name)
        setattr(guarded, "__wrapped__", real)
        return guarded

    # -- scope ----------------------------------------------------------------------
    def install(self):
        if self.saved:
            raise RuntimeError("fa1401 file boundary already installed")
        for owner, attribute, classify in self.targets():
            current = vars(owner).get(attribute, _ABSENT)
            self.saved.append((owner, attribute, current))
            setattr(owner, attribute, self.wrap(owner.__name__ + "." + attribute, classify,
                                                getattr(owner, attribute)))
        return self

    def restore(self):
        while self.saved:
            owner, attribute, value = self.saved.pop()
            if value is _ABSENT:
                if attribute in vars(owner):
                    delattr(owner, attribute)
            else:
                setattr(owner, attribute, value)

    def __enter__(self):
        return self.install()

    def __exit__(self, *exc):
        self.restore()
        return False


def target_names():
    return sorted(owner.__name__ + "." + attribute for owner, attribute, _ in FileBoundary(
        repo=os.curdir, write_roots=()).targets())


def active():
    """Names of boundary hooks currently installed in this process."""
    return sorted(owner.__name__ + "." + attribute for owner, attribute, _ in FileBoundary(
        repo=os.curdir, write_roots=()).targets() if hasattr(getattr(owner, attribute), MARK))


def identity():
    """Exact attribute objects of every hooked name, for restoration checks."""
    return {owner.__name__ + "." + attribute: (vars(owner).get(attribute), getattr(owner, attribute))
            for owner, attribute, _ in FileBoundary(repo=os.curdir, write_roots=()).targets()}
