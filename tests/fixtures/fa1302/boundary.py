"""Scoped synthetic-fixture boundary; importing this module changes no process state.

Not a hostile-code sandbox. All application data paths in this closure are explicit
arguments; tempfile's constant and cached default, HOME and temp environment roots
are rebound before application imports. No data-path bound defaults exist in the
selected closure. Network/model and file access sentinels are reversible for peer
unittest discovery. The file worker additionally installs a process-local audit hook.
"""
import builtins
from contextlib import ExitStack, contextmanager
import http.client
import io
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
WORKER = Path(__file__).with_name("worker.py")


def checked_output(value):
    path = Path(os.path.abspath(value))
    # Check every existing component before mkdir, including dangling links.
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise ValueError("symlink_output_ancestry")
        if part.exists() and not part.is_dir():
            raise ValueError("output_ancestor_not_directory")
    if path == REPO or REPO in path.parents:
        raise ValueError("output_must_be_external")
    return path


def fresh_output(value, prefix="run-"):
    root = checked_output(value)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


class Boundary:
    def __init__(self, root):
        self.root = checked_output(root)
        self.events = []
        self.read_roots = (REPO, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())

    def refuse(self, kind):
        self.events.append(kind)
        raise RuntimeError("synthetic_boundary_" + kind)

    def path(self, value, write=False, directory=False):
        if isinstance(value, int):
            return
        path = Path(os.path.abspath(os.fsdecode(value)))
        if path == Path(os.devnull):
            return
        # Realpath prevents a fixture symlink escaping its admitted root.
        real = path.resolve()
        # secure_open walks from / using O_DIRECTORY descriptors. Ancestor
        # traversal is not permission to open any ancestor's file contents.
        if directory and not write and any(real in p.parents for p in (self.root, *self.read_roots)):
            return
        if real == self.root or self.root in real.parents:
            return
        if not write and any(real == p or p in real.parents for p in self.read_roots):
            return
        self.refuse("file")

    @contextmanager
    def enter(self, *, audit=False):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.root / "tmp"
        home = self.root / "home"
        temp.mkdir(exist_ok=True)
        home.mkdir(exist_ok=True)
        active = [True]
        old_profile = sys.getprofile()
        model_file = str(REPO / "campaign_tool/digest/model.py")
        def model_guard(frame, event, arg):
            if (event == "call" and frame.f_code.co_filename == model_file
                    and frame.f_code.co_name in {"narrative", "chat_json", "_strict_local_post", "_connect_local"}):
                self.refuse("model")
            if old_profile is not None:
                old_profile(frame, event, arg)
        real_open, real_io, real_os = builtins.open, io.open, os.open
        real_connect, real_popen = sqlite3.connect, subprocess.Popen

        def file_open(file, mode="r", *args, **kw):
            self.path(file, any(c in mode for c in "wax+"))
            return real_open(file, mode, *args, **kw)

        def io_open(file, mode="r", *args, **kw):
            self.path(file, any(c in mode for c in "wax+"))
            return real_io(file, mode, *args, **kw)

        def os_open(file, flags, *args, **kw):
            if kw.get("dir_fd") is None:
                self.path(file, bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)),
                          bool(flags & os.O_DIRECTORY))
            return real_os(file, flags, *args, **kw)

        def db_open(database, *args, **kw):
            name = os.fsdecode(database)
            if name.startswith("file:"):
                from urllib.parse import unquote, urlsplit
                name = unquote(urlsplit(name).path)
            if name != ":memory:":
                self.path(name, True)
            return real_connect(database, *args, **kw)

        def denied(*args, **kw):
            self.refuse("network_or_model")

        def popen(argv, *args, **kw):
            if (not isinstance(argv, list) or len(argv) < 3
                    or argv[:3] != [sys.executable, "-B", str(WORKER)]
                    or kw.get("shell") or kw.get("env") is None
                    or any(k in kw["env"] for k in ("PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH"))):
                self.refuse("child")
            return real_popen(argv, *args, **kw)

        def hook(event, args):
            if not active[0]:
                return
            if event.startswith("socket.") and event not in ("socket.__new__",):
                self.refuse("network_or_model")
            if event == "open":
                mode, flags = args[1:3]
                self.path(args[0], bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)),
                          bool(flags & os.O_DIRECTORY))
            elif event == "sqlite3.connect":
                db_open_path = args[0]
                if str(db_open_path) != ":memory:":
                    from urllib.parse import unquote, urlsplit
                    name = str(db_open_path)
                    self.path(unquote(urlsplit(name).path) if name.startswith("file:") else name, True)

        def mutation(real, two_paths=False):
            def checked(path, *args, **kw):
                if kw.get("dir_fd") is None and kw.get("src_dir_fd") is None:
                    self.path(path, True)
                if two_paths and kw.get("dst_dir_fd") is None:
                    self.path(args[0], True)
                return real(path, *args, **kw)
            return checked

        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "dont_write_bytecode", True))
            stack.enter_context(patch.object(tempfile, "tempdir", str(temp)))
            stack.enter_context(patch.dict(os.environ, {"HOME": str(home), "TMPDIR": str(temp),
                                                       "TMP": str(temp), "TEMP": str(temp)}))
            for obj, name, replacement in ((builtins, "open", file_open), (io, "open", io_open),
                    (os, "open", os_open), (sqlite3, "connect", db_open),
                    (socket, "create_connection", denied), (socket, "getaddrinfo", denied),
                    (socket.socket, "connect", denied), (socket.socket, "connect_ex", denied),
                    (urllib.request, "urlopen", denied), (http.client.HTTPConnection, "connect", denied),
                    (subprocess, "Popen", popen)):
                stack.enter_context(patch.object(obj, name, replacement))
            for name in ("mkdir", "remove", "unlink", "rmdir", "chmod"):
                stack.enter_context(patch.object(os, name, mutation(getattr(os, name))))
            for name in ("rename", "replace", "link"):
                stack.enter_context(patch.object(os, name, mutation(getattr(os, name), True)))
            # Symlink targets are inert text; only the link destination is written.
            real_symlink = os.symlink
            def symlink(source, destination, *args, **kw):
                self.path(destination, True)
                return real_symlink(source, destination, *args, **kw)
            stack.enter_context(patch.object(os, "symlink", symlink))
            if audit:
                sys.addaudithook(hook)  # only the dedicated file worker calls audit=True
            sys.setprofile(model_guard)
            try:
                yield self
            finally:
                sys.setprofile(old_profile)
                active[0] = False


def child_env(root):
    # Do not inherit provider settings or interpreter path overrides.
    return {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}


def cli_adapter(root):
    """Only the exact existing synthetic CLI source may use this admitted adapter."""
    def launch(code, *args):
        from tests.records import test_mail_adoption_recovery as recovery
        if code != recovery.CLI:
            raise ValueError("unadmitted_compatibility_child")
        output = fresh_output(root / "cli", "child-")
        argv = [sys.executable, "-B", str(WORKER), "cli", str(root), *args]
        result = subprocess.run(argv,
                                cwd=REPO, env=child_env(root), capture_output=True, text=True, timeout=120)
        import hashlib
        import json
        (output / "result.json").write_text(json.dumps({"argv": argv, "cwd": str(REPO), "exit": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
            "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest()}, indent=2) + "\n")
        return result
    return launch
