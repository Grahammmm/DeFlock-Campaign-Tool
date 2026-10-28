"""Synthetic-only FA1310 fixture boundary; reversible in shared discovery.

Not a hostile-code sandbox. Portal paths are explicit arguments (no bound data
root defaults). HOME/XDG/temp roots are redirected before application imports.
"""
import builtins
import contextlib
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
HOST = "portal.example.test"
BASE = "https://" + HOST
URL = BASE + "/documents/123/download?part=one&part=two%2Fthree"


def safe_path(value):
    path = Path(os.path.abspath(value))
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError("unsafe_output_ancestry")
    return path


def fresh(root, prefix="invocation-"):
    root = safe_path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


class Boundary(contextlib.ExitStack):
    """Deny live I/O before imports; undo all process changes on exit."""
    def __init__(self, root):
        super().__init__()
        self.root = safe_path(root)
        self.attempts = []
        self.expected = []

    def deny(self, kind):
        self.attempts.append(kind)
        raise RuntimeError("fa1310_forbidden_" + kind)

    def check(self, value, writing=False):
        if isinstance(value, int):
            return
        path = Path(os.path.abspath(os.fsdecode(value)))
        allowed = (self.root,) if writing else self.read_roots
        # Resolve aliases for reads; output ancestry is checked without resolving.
        if writing:
            safe_path(path)
        target = path.resolve()
        if not any(target == root or root in target.parents for root in allowed):
            self.deny("live_file")

    def __enter__(self):
        super().__enter__()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        home, tmp = self.root / "home", self.root / "tmp"
        home.mkdir(exist_ok=True)
        tmp.mkdir(exist_ok=True)
        self.read_roots = tuple({REPO, self.root, Path(sys.prefix).resolve(),
                                 Path(sys.base_prefix).resolve()})
        env = {key: str(home / key.lower()) for key in
               ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")}
        env.update(HOME=str(home), TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp))
        self.enter_context(patch.dict(os.environ, env))
        self.enter_context(patch.object(tempfile, "tempdir", str(tmp)))
        self.enter_context(patch.object(sys, "dont_write_bytecode", True))
        for module, name in ((socket, "socket"), (socket, "create_connection"),
                             (socket, "getaddrinfo"), (urllib.request, "urlopen"),
                             (http.client.HTTPConnection, "connect")):
            self.enter_context(patch.object(module, name, side_effect=lambda *a, **k: self.deny("network")))
        original_profile = sys.getprofile()
        def profile(frame, event, arg):
            if event == "call" and frame.f_globals.get("__name__") == "campaign_tool.digest.model" and frame.f_code.co_name in {"chat_json", "narrative"}:
                self.deny("model")
        sys.setprofile(profile)
        self.callback(sys.setprofile, original_profile)
        for module, name in ((builtins, "open"), (io, "open")):
            original = getattr(module, name)
            def guarded(file, mode="r", *args, _original=original, **kwargs):
                self.check(file, any(flag in mode for flag in "wax+"))
                return _original(file, mode, *args, **kwargs)
            self.enter_context(patch.object(module, name, guarded))
        def descriptor_path(path, descriptor):
            if descriptor is None:
                return path
            # Existing TemporaryDirectory cleanup uses descriptor-relative I/O.
            # Admit it only when the actual directory descriptor is invocation-owned.
            base = Path(os.readlink("/proc/self/fd/" + str(descriptor)))
            self.check(base, True)
            return base / os.fsdecode(path)
        original_open = os.open
        def os_open(path, flags, *args, **kwargs):
            self.check(descriptor_path(path, kwargs.get("dir_fd")), bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
            return original_open(path, flags, *args, **kwargs)
        self.enter_context(patch.object(os, "open", os_open))
        original_connect = sqlite3.connect
        def connect(database, *args, **kwargs):
            if database != ":memory:":
                self.check(database, True)
            return original_connect(database, *args, **kwargs)
        self.enter_context(patch.object(sqlite3, "connect", connect))
        for name in ("mkdir", "remove", "unlink", "rmdir", "chmod"):
            original = getattr(os, name)
            def mutate(path, *args, _original=original, **kwargs):
                self.check(descriptor_path(path, kwargs.get("dir_fd")), True)
                return _original(path, *args, **kwargs)
            self.enter_context(patch.object(os, name, mutate))
        for name in ("rename", "replace", "link", "symlink"):
            original = getattr(os, name)
            def pair(source, destination, *args, _original=original, **kwargs):
                self.check(source, True)
                self.check(destination, True)
                return _original(source, destination, *args, **kwargs)
            self.enter_context(patch.object(os, name, pair))
        # No application subprocesses, models or scripts are needed by this closure.
        self.enter_context(patch.object(subprocess, "Popen", side_effect=lambda *a, **k: self.deny("process")))
        return self


def modules():
    # Full staged production modules, not copied AST slices or fake packages.
    from campaign_tool.records.portal.notice import Links, inventory_notice
    from campaign_tool.records.portal.collections import BodyLinks, CollectionInventory, Scope
    from campaign_tool.records.portal.store import Queue, digest_bytes
    from campaign_tool.records.run import root_lock
    return Links, inventory_notice, BodyLinks, CollectionInventory, Scope, Queue, digest_bytes, root_lock
