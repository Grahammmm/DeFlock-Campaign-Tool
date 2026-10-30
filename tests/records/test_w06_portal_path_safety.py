"""W06: offline synthetic report paths stay on walked directory descriptors."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest import mock

from tools import portal_reconcile as portal


class Behavior(unittest.TestCase):
    prc = portal

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="w06-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base, self.outside = self.root / "base", self.root / "outside"
        self.base.mkdir()
        self.outside.mkdir()
        self.sentinel = self.outside / "sentinel"
        self.sentinel.write_bytes(b"unchanged")
        self.before = self.sentinel.stat()
        fixture = Path(__file__).resolve().parents[1] / "fixtures/portal_reconcile"
        self.obs, self.que = self.base / "obs.json", self.base / "queue.json"
        self.obs.write_bytes((fixture / "observations.json").read_bytes())
        self.que.write_bytes((fixture / "queue-export.json").read_bytes())
        self.output = self.base / "report.json"
        self.expected = portal.render(portal.reconcile(json.loads(self.obs.read_text())["observations"],
                                                       json.loads(self.que.read_text())["items"]))
        self.fd_count = len(os.listdir("/proc/self/fd")) if Path("/proc/self/fd").exists() else None
        self.addCleanup(self.check_fds)

    def check_fds(self):
        if self.fd_count is not None:
            self.assertEqual(len(os.listdir("/proc/self/fd")), self.fd_count)

    def invoke(self, obs=None, que=None, output=None):
        args = ["--observations", str(self.obs if obs is None else obs),
                "--queue-export", str(self.que if que is None else que)]
        if output is not None:
            args += ["--output", str(output)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.prc.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def refused(self, path, kind):
        kwargs = {kind: path, "output": self.output} if kind != "output" else {"output": path}
        outside_before = sorted(str(p.relative_to(self.outside)) for p in self.outside.rglob("*"))
        with mock.patch.object(self.prc.os, "fdopen", wraps=os.fdopen) as fdopen, \
                mock.patch.object(self.prc.os, "read", wraps=os.read) as read, \
                mock.patch.object(self.prc.os, "open", wraps=os.open) as opening:
            result = self.invoke(**kwargs)
        code = "output_not_created" if kind == "output" else "input_unreadable"
        self.assertEqual(result, (2, "", json.dumps({"error": code}) + "\n"))
        if kind != "output":
            # The queue is checked after the already validated observations.
            self.assertEqual(fdopen.call_count, 1 if kind == "que" else 0)
            self.assertFalse(read.called)
        if kind == "output" and Path(path).is_symlink() and not Path(path).is_dir():
            self.assertFalse(any(call.args[1] == "w" for call in fdopen.call_args_list))
        else:
            self.assertFalse(any(call.args[1] & os.O_CREAT for call in opening.call_args_list))
        self.assertFalse(self.output.exists())
        self.assertEqual(sorted(str(p.relative_to(self.outside)) for p in self.outside.rglob("*")), outside_before)
        self.assertEqual(self.sentinel.read_bytes(), b"unchanged")
        self.assertEqual(self.sentinel.stat().st_ino, self.before.st_ino)

    def test_baseline_and_leaf_links_refused_before_io(self):
        link = self.root / "link"
        link.symlink_to(self.base, target_is_directory=True)
        for kind, name in (("obs", "obs.json"), ("que", "queue.json")):
            with self.subTest(kind=kind):
                self.refused(link / name, kind)
        link.unlink()
        link.symlink_to(self.outside, target_is_directory=True)
        self.refused(link / "report.json", "output")
        link.unlink()
        for kind, target in (("obs", self.obs), ("que", self.que), ("output", self.sentinel)):
            with self.subTest(leaf=kind):
                link.symlink_to(target)
                self.refused(link, kind)
                link.unlink()

    def test_each_depth_absolute_and_relative_refused(self):
        old = Path.cwd()
        try:
            os.chdir(self.base)
            for depth in ("a", "a/b", "a/b/c"):
                for relative in (False, True):
                    with self.subTest(depth=depth, relative=relative):
                        parent = self.base / depth
                        parent.parent.mkdir(parents=True, exist_ok=True)
                        tail = Path("a/b/c").relative_to(depth)
                        target = self.outside / tail
                        target.mkdir(parents=True, exist_ok=True)
                        (target / "obs.json").write_bytes(self.obs.read_bytes())
                        parent.symlink_to(self.outside, target_is_directory=True)
                        path = Path("a/b/c") if relative else self.base / "a/b/c"
                        try:
                            self.refused(path / "obs.json", "obs")
                            self.refused(path / "report.json", "output")
                        finally:
                            parent.unlink()
                            (target / "obs.json").unlink()
                            while target != self.outside:
                                previous = target.parent
                                target.rmdir()
                                target = previous
        finally:
            os.chdir(old)

    def test_valid_absolute_relative_and_fixture(self):
        self.assertEqual(json.loads(self.expected)["classes"], {
            "fetched": 3, "queued": 2, "failed": 2, "unproven": 4, "held_policy": 4,
            "held_redirect": 1, "missing_request_identity": 1})
        old = Path.cwd()
        try:
            for relative in (False, True):
                if relative:
                    os.chdir(self.base)
                obs, que, out = (("./obs.json", "queue.json", "report.json") if relative
                                 else (self.obs, self.que, self.output))
                self.assertEqual(self.invoke(obs=obs, que=que), (0, self.expected, ""))
                self.assertEqual(self.invoke(obs=obs, que=que, output=out), (0, "", ""))
                self.assertEqual(self.output.read_text(), self.expected)
                self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
                self.output.unlink()
        finally:
            os.chdir(old)

    def test_missing_dotdot_and_empty_names(self):
        for path in (self.base / "missing/obs", self.base / "missing",
                     str(self.base) + "/../base/obs.json", "", str(self.base) + "/"):
            with self.subTest(input=str(path)):
                self.refused(path, "obs")
        for path in (self.base / "missing/report", str(self.base) + "/../base/report.json",
                     str(self.base) + "/"):
            with self.subTest(output=str(path)):
                self.refused(path, "output")
        self.assertFalse((self.base / "missing").exists())

    def test_parent_swap_after_walk_keeps_original_inode(self):
        real_open, real_sync = os.open, os.fsync
        for kind in ("output", "read", "missing-read"):
            with self.subTest(kind=kind):
                parent = self.base / kind
                parent.mkdir()
                leaf = parent / "leaf"
                if kind == "read":
                    leaf.write_bytes(self.obs.read_bytes())
                if kind != "output":
                    (self.outside / "leaf").write_text(json.dumps({"schema": portal.OBSERVATION_SCHEMA,
                                                                  "observations": []}))
                moved = self.base / (kind + "-moved")
                inode, synced = parent.stat().st_ino, []
                def opening(path, flags, *args, **kwargs):
                    if str(path) in ("leaf", str(leaf)):
                        parent.rename(moved)
                        parent.symlink_to(self.outside, target_is_directory=True)
                    return real_open(path, flags, *args, **kwargs)
                def syncing(fd):
                    if stat.S_ISDIR(os.fstat(fd).st_mode):
                        synced.append(os.fstat(fd).st_ino)
                    return real_sync(fd)
                with mock.patch.object(self.prc.os, "open", opening), \
                        mock.patch.object(self.prc.os, "fsync", syncing):
                    if kind == "output":
                        self.assertEqual(self.invoke(output=leaf), (0, "", ""))
                    elif kind == "read":
                        self.assertEqual(self.invoke(obs=leaf), (0, self.expected, ""))
                    else:
                        self.assertEqual(self.invoke(obs=leaf), (2, "", '{"error": "input_unreadable"}\n'))
                if kind == "output":
                    self.assertTrue((moved / "leaf").is_file())
                    self.assertEqual((moved / "leaf").read_text(), self.expected)
                    self.assertEqual(synced, [inode])
                else:
                    (self.outside / "leaf").unlink()
                self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["sentinel"])


class GateControl(unittest.TestCase):
    def test_old_walk_detected_without_mutating_production(self):
        before = dict(vars(portal))
        source = Path(portal.__file__).read_bytes()
        mutant = types.ModuleType("tools._w06_mutant")
        mutant.__dict__.update(before)
        for name, value in before.items():
            if isinstance(value, types.FunctionType) and value.__module__ == portal.__name__:
                mutant.__dict__[name] = types.FunctionType(value.__code__, mutant.__dict__,
                                                          value.__name__, value.__defaults__, value.__closure__)
        def old_walk(path):
            return os.open(".", os.O_RDONLY | os.O_DIRECTORY), os.fspath(path)
        mutant._walk_parent = old_walk
        bound = type("OldWalk", (Behavior,), {"prc": mutant})
        names = ("test_baseline_and_leaf_links_refused_before_io", "test_each_depth_absolute_and_relative_refused",
                 "test_valid_absolute_relative_and_fixture")
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream).run(unittest.TestSuite(bound(n) for n in names))
        self.assertFalse(result.errors, stream.getvalue())
        self.assertEqual({getattr(c, "test_case", c)._testMethodName for c, _ in result.failures}, set(names[:2]))
        self.assertEqual(dict(vars(portal)), before)
        self.assertEqual(Path(portal.__file__).read_bytes(), source)
