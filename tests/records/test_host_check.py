"""tools/records_host_check.py: read-only records host preflight (every probe injected)."""

import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "records_host_check.py"
sys.path.insert(0, str(ROOT))
from campaign_tool import law  # noqa: E402

_spec = importlib.util.spec_from_file_location("records_host_check", TOOL)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)

PIN = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32
GIB = 1024 ** 3
DIGEST = re.compile(r"[0-9a-f]{64}")
FORBIDDEN_VERBS = {"install", "pull", "run", "start", "restart", "systemctl", "modprobe", "sysctl", "apt", "apt-get",
                   "load", "build", "create", "rm", "rmi", "stop", "kill", "exec", "pip"}


def parser_pins():
    return {hc.test_env.normalise(name): ver for name, ver, _ in hc.test_env.parse(hc.PARSERS.read_text(encoding="utf-8"))}


class FakeHost:
    """Synthetic probe results; anything not configured is an unrun probe (None)."""

    def __init__(self, **over):
        self.sys, self.rel = over.get("system", "Linux"), over.get("release", "6.8.0-45-generic")
        self.files = over.get("files", {"/proc/sys/user/max_user_namespaces": "63000"})
        self.paths = over.get("paths", {"/sys/fs/cgroup", "/sys/fs/cgroup/cgroup.controllers"})
        self.bins = over.get("bins", {"docker", "tesseract"})
        self.cmds = {"docker-version": ("Docker version 27.3.1, build ce12230", "ok"),
                     "docker-rootless": ('["name=seccomp,profile=builtin","name=rootless","name=cgroupns"]', "ok"),
                     "docker-images": (f"{PIN}\n{OTHER}", "ok"), "tesseract-version": ("tesseract 5.3.0", "ok")}
        self.cmds.update(over.get("cmds", {}))
        self.free = over.get("free", 10 * GIB)
        self.enc = over.get("encoding", "UTF-8")
        self.dists = over.get("dists", parser_pins())
        self.ran = []

    def system(self): return self.sys
    def release(self): return self.rel
    def read(self, path): return self.files.get(path)
    def exists(self, path): return path in self.paths
    def which(self, name): return name in self.bins
    def tmp_dir(self): return "/synthetic-tmp"
    def free_bytes(self, path): return self.free
    def encoding(self): return self.enc
    def dist_version(self, name): return self.dists.get(hc.test_env.normalise(name))

    def command(self, key):
        self.ran.append(hc.COMMANDS[key])
        return self.cmds.get(key, (None, "empty output"))


class NullHost:
    """Every probe unrun."""
    def system(self): return None
    def release(self): return None
    def read(self, path): return None
    def exists(self, path): return False
    def which(self, name): return False
    def command(self, key): return None, "not run"
    def tmp_dir(self): return None
    def free_bytes(self, path): return None
    def encoding(self): return None
    def dist_version(self, name): return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.target = self.dir / "runtime"
        self.target.mkdir()
        self.wrapper = self.dir / "run-records.sh"
        self.wrapper.write_text(f"#!/bin/sh\nEXPECTED_IMAGE={PIN}\nMIN_FREE_BYTES=2147483648\n", encoding="utf-8")

    def run_tool(self, host, *extra, wrapper=True):
        argv = ["--target", str(self.target)] + (["--wrapper", str(self.wrapper)] if wrapper else []) + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hc.main(argv, host=host)
        return code, out.getvalue(), err.getvalue()

    def rows(self, host, **kw):
        pin, free, note = hc.read_wrapper(str(self.wrapper))
        return {r["id"]: r for r in hc.probe(host, str(self.target), kw.get("pin", pin), note, free)}


class VerdictCases(Base):
    def test_ready_host_all_eleven_rows_in_order_exit_0(self):
        code, out, _ = self.run_tool(FakeHost())
        self.assertEqual(code, 0)
        rows = hc.probe(FakeHost(), str(self.target), PIN)
        self.assertEqual([r["id"] for r in rows], list(hc.ROW_IDS))
        self.assertEqual(len(rows), 11)
        for r in rows:
            self.assertEqual(set(r), {"id", "state", "measured", "required", "blocking", "remedy"})
            self.assertEqual(r["state"], "pass", r)
            self.assertTrue(r["measured"] and r["required"] and r["remedy"])
        self.assertIn("verdict: host ready\n", out)
        self.assertTrue(out.endswith("host-check | pass=11 fail=0 unknown=0 blocking-fail=0 | "
                                     "read-only; nothing installed or started\n"), out)

    def test_missing_container_runtime(self):
        code, out, _ = self.run_tool(FakeHost(bins={"tesseract"}))
        self.assertEqual(code, 1)
        self.assertIn("verdict: not ready — container-runtime\n", out)
        rows = self.rows(FakeHost(bins={"tesseract"}))
        self.assertEqual(rows["container-runtime"]["state"], "fail")
        self.assertEqual(rows["rootless-ok"]["state"], "unknown")
        self.assertEqual(rows["image-pin"]["state"], "unknown")

    def test_macos_host_kernel_fail_dependent_rows_unknown(self):
        host = FakeHost(system="Darwin", release="23.6.0")
        code, out, _ = self.run_tool(host)
        rows = self.rows(FakeHost(system="Darwin", release="23.6.0"))
        self.assertEqual(code, 1)
        self.assertEqual((rows["os-kernel"]["state"], rows["os-kernel"]["measured"]), ("fail", "Darwin (not Linux)"))
        for rid in ("user-namespaces", "cgroup-v2", "rootless-ok"):
            self.assertEqual((rows[rid]["state"], rows[rid]["measured"]), ("unknown", hc.NOT_LINUX))
        self.assertNotIn("docker-rootless", [k for k, v in hc.COMMANDS.items() if v in host.ran])
        self.assertIn("os-kernel          fail     Darwin (not Linux)", out)
        self.assertIn("non-Linux host: user-namespaces, cgroup-v2, rootless-ok are unknown, not fail", out)
        self.assertIn("verdict: not ready — os-kernel\n", out)

    def test_unrun_probe_is_unknown_and_never_ready(self):
        rows = hc.probe(NullHost(), str(self.target), PIN)
        self.assertEqual(len(rows), 11)
        self.assertEqual([r["id"] for r in rows if r["state"] == "pass"], [])
        host = FakeHost(cmds={"docker-rootless": (None, "timed out")})
        code, out, _ = self.run_tool(host)
        self.assertEqual(code, 1)
        self.assertIn("rootless-ok        unknown  daemon not queried (timed out)", out)
        self.assertIn("verdict: not ready — unverified: rootless-ok\n", out)

    def test_empty_subprocess_stdout_is_unknown_not_pass(self):
        empty = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"", b"")  # noqa: E731
        self.assertEqual(hc.run_capped(hc.COMMANDS["docker-version"], run=empty, which=lambda n: "/bin/x"),
                         (None, "empty output"))
        for key in ("docker-version", "docker-rootless", "docker-images"):
            rows = self.rows(FakeHost(cmds={key: (None, "empty output")}))
            states = {r: rows[r]["state"] for r in ("container-runtime", "rootless-ok", "image-pin")}
            self.assertNotIn(key == "docker-version" and "pass", states.values())
            self.assertEqual(states[{"docker-version": "container-runtime", "docker-rootless": "rootless-ok",
                                     "docker-images": "image-pin"}[key]], "unknown")
        code, out, _ = self.run_tool(FakeHost(cmds={"docker-version": ("", "ok")}))
        self.assertIn("verdict: not ready — unverified: container-runtime", out)

    def test_json_out_0600_valid_and_refuses_existing(self):
        out_path = self.dir / "host-check.json"
        code, out, _ = self.run_tool(FakeHost(), "--json-out", str(out_path))
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(out_path.stat().st_mode), 0o600)
        doc = json.loads(out_path.read_text(encoding="utf-8"))
        law.validate(doc, json.loads((ROOT / "schemas" / "records-host-check-v1.json").read_text(encoding="utf-8")))
        self.assertEqual((doc["schema"], doc["ready"], doc["verdict"]), ("records-host-check-v1", True, "host ready"))
        self.assertEqual([r["id"] for r in doc["rows"]], list(hc.ROW_IDS))
        before = out_path.read_bytes()
        code, out, err = self.run_tool(FakeHost(), "--json-out", str(out_path))
        self.assertEqual((code, out), (1, "NEWFILE: not written\n"))
        self.assertIn("never overwritten", err)
        self.assertEqual(out_path.read_bytes(), before)


class SubprocessDiscipline(unittest.TestCase):
    def fake(self, result=None, exc=None):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            if exc:
                raise exc
            return result
        return run, calls

    def test_failure_modes_never_pass_never_raise(self):
        argv = hc.COMMANDS["tesseract-version"]
        cases = [(self.fake(subprocess.CompletedProcess(argv, 0, b"", b"")), "empty output"),
                 (self.fake(subprocess.CompletedProcess(argv, 2, b"tesseract 5.3.0", b"")), "exit 2"),
                 (self.fake(exc=subprocess.TimeoutExpired(argv, 10)), "timed out"),
                 (self.fake(exc=OSError("boom")), "could not run"),
                 (self.fake(subprocess.CompletedProcess(argv, 0, b"5.3.0 " * 20000, b"")), "output over cap")]
        for (run, calls), reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(hc.run_capped(argv, run=run, which=lambda n: "/bin/x"), (None, reason))
                sent, kw = calls[0]
                self.assertIsInstance(sent, list)
                self.assertEqual(kw["timeout"], hc.TIMEOUT_SECONDS)
                self.assertNotIn("shell", kw)
        run, calls = self.fake()
        self.assertEqual(hc.run_capped(argv, run=run, which=lambda n: None), (None, "missing"))
        self.assertEqual(hc.run_capped(("sh", "-c", "true"), run=run, which=lambda n: "/bin/sh"),
                         (None, "binary not allowlisted"))
        self.assertEqual(calls, [])
        for reason in ("exit 2", "timed out", "output over cap"):
            rows = {r["id"]: r for r in hc.probe(FakeHost(cmds={"tesseract-version": (None, reason)}), "/t", PIN)}
            self.assertEqual(rows["ocr-engine"]["state"], "unknown")
            self.assertIn(reason, rows["ocr-engine"]["measured"])


class ImagePin(Base):
    def test_no_local_image_unknown_and_no_network(self):
        calls = []
        with mock.patch.object(socket.socket, "connect", lambda *a: calls.append(a)), \
                mock.patch.object(socket, "create_connection", lambda *a, **k: calls.append(a)):
            host = FakeHost(cmds={"docker-images": (None, "empty output")})
            code, out, _ = self.run_tool(host)
        self.assertEqual(calls, [])
        self.assertIn("image-pin          unknown  no local image metadata (empty output)", out)
        self.assertEqual(code, 1)
        self.assertIn("verdict: not ready — unverified: image-pin", out)
        self.assertNotIn(("docker", "pull"), [tuple(c[:2]) for c in host.ran])

    def test_differing_local_digest_fails_with_short_form_only(self):
        host = FakeHost(cmds={"docker-images": (OTHER, "ok")})
        out_path = self.dir / "o.json"
        code, out, _ = self.run_tool(host, "--json-out", str(out_path))
        text = out + out_path.read_text(encoding="utf-8")
        self.assertEqual(code, 1)
        self.assertIn(f"image-pin          fail     no local image matches {PIN[:19]} (1 local image(s))", out)
        self.assertIn("verdict: not ready — image-pin", out)
        self.assertNotIn(PIN, text)
        self.assertNotIn(OTHER[7:19], text)
        self.assertEqual(DIGEST.findall(text), [])

    def test_pin_has_one_source_the_run_wrapper(self):
        source = TOOL.read_text(encoding="utf-8")
        self.assertEqual(DIGEST.findall(source), [], "a second digest constant in the tool")
        seeded = source + f'\nSECOND_PIN = "sha256:{"ef" * 32}"\n'
        self.assertEqual(len(DIGEST.findall(seeded)), 1)
        code, out, _ = self.run_tool(FakeHost(), wrapper=False)  # local images present, no pin given
        self.assertIn("image-pin          unknown  pin not provided (--wrapper)", out)
        self.assertEqual(code, 1)
        self.wrapper.write_text("EXPECTED_IMAGE=@EXPECTED_IMAGE@\n", encoding="utf-8")
        self.assertEqual(self.rows(FakeHost())["image-pin"]["measured"], "no rendered EXPECTED_IMAGE in wrapper")
        link = self.dir / "link.sh"
        link.symlink_to(self.dir / "missing")
        self.assertEqual(hc.read_wrapper(str(link))[0], None)


class BlockingRules(Base):
    def test_blocking_unknown_never_ready_nonblocking_fail_does_not_block(self):
        for rid, host in (("disk-headroom", FakeHost(free=None)),
                          ("user-namespaces", FakeHost(files={})),
                          ("os-kernel", FakeHost(release="unparsed"))):
            with self.subTest(rid=rid):
                self.assertEqual(hc.verdict(hc.probe(host, "/t", PIN)), (False, f"not ready — unverified: {rid}"))
        host = FakeHost(bins={"docker"}, dists={}, encoding="ANSI_X3.4-1968")
        code, out, _ = self.run_tool(host)
        rows = self.rows(FakeHost(bins={"docker"}, dists={}, encoding="ANSI_X3.4-1968"))
        for rid in ("ocr-engine", "parsers", "locale-utf8"):
            self.assertEqual((rows[rid]["state"], rows[rid]["blocking"]), ("fail", False))
        self.assertEqual(code, 0)
        self.assertIn("verdict: host ready", out)
        self.assertIn("blocking-fail=0", out)

    def test_blocking_fail_named_before_unverified(self):
        host = FakeHost(files={}, free=1024)
        self.assertEqual(hc.verdict(hc.probe(host, "/t", PIN)), (False, "not ready — disk-headroom"))
        self.assertEqual(self.rows(FakeHost(free=GIB))["disk-headroom"]["state"], "fail")  # wrapper min 2 GiB
        old = FakeHost(release="5.10.0", cmds={"docker-version": ("Docker version 19.03.1", "ok")})
        rows = {r["id"]: r for r in hc.probe(old, "/t", PIN)}
        self.assertEqual((rows["os-kernel"]["state"], rows["container-runtime"]["state"]), ("fail", "fail"))
        self.assertEqual(hc.probe(FakeHost(), None, PIN)[8]["measured"], "no --target")


class OutputHygiene(Base):
    def test_no_host_identifiers_in_either_output(self):
        seeded = ["alice", "box.example.lan", "10.1.2.3", "/home/alice", "s3cr3t-token-value", "/dev/sda1", "tmpfs"]
        host = FakeHost(release="6.8.0-alice-box.example.lan-10.1.2.3",
                        cmds={"docker-version": ("Docker version 27.3.1, build x /home/alice 10.1.2.3 box.example.lan", "ok"),
                              "docker-rootless": ('["name=rootless","/home/alice/.docker","s3cr3t-token-value"]', "ok"),
                              "tesseract-version": ("tesseract 5.3.0 /dev/sda1 tmpfs alice", "ok")},
                        encoding="UTF-8 alice")
        out_path = self.dir / "h.json"
        with mock.patch.dict(os.environ, {"HERMES_SEEDED_SECRET": "s3cr3t-token-value"}):
            code, out, _ = self.run_tool(host, "--json-out", str(out_path))
        text = out + out_path.read_text(encoding="utf-8")
        for value in seeded + [str(self.dir), str(Path.home())]:
            self.assertNotIn(value, text)
        self.assertIn("docker 27.3.1", text)


class JsonOutRefusals(Base):
    def assertRefused(self, target, rule):
        code, out, err = self.run_tool(FakeHost(), "--json-out", target)
        self.assertEqual((code, out), (1, "NEWFILE: not written\n"))
        self.assertIn(rule, err)

    def test_symlink_inside_repo_relative_missing_folder(self):
        link = self.dir / "linkdir"
        link.symlink_to(self.dir)
        self.assertRefused(str(link / "x.json"), "no symlink on its folder")
        self.assertRefused(str(ROOT / "host-check.json"), "outside the repository")
        self.assertRefused("x.json", "absolute path")
        self.assertRefused(str(self.dir / "nope" / "x.json"), "existing folder")
        self.assertFalse((ROOT / "host-check.json").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")
    def test_unwritable_and_write_error_remove_file(self):
        locked = self.dir / "locked"
        locked.mkdir(mode=0o500)
        self.addCleanup(locked.chmod, 0o700)
        code, out, _ = self.run_tool(FakeHost(), "--json-out", str(locked / "x.json"))
        self.assertEqual(code, 1)
        self.assertTrue(out.endswith("NEWFILE: not written\n"))
        target = self.dir / "w.json"
        with mock.patch.object(hc.os, "write", side_effect=OSError("disk full")):
            code, out, _ = self.run_tool(FakeHost(), "--json-out", str(target))
        self.assertEqual(code, 1)
        self.assertTrue(out.endswith("NEWFILE: not written\n"))
        self.assertFalse(target.exists())


def snapshot(root, skip=(".git",)):
    seen = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip and d != "__pycache__"]
        for name in files:
            st = os.lstat(os.path.join(base, name))
            seen[os.path.relpath(os.path.join(base, name), root)] = (st.st_size, st.st_mtime_ns, st.st_mode)
    return seen


class NoMutation(Base):
    def test_real_host_class_runs_only_fixed_readonly_commands(self):
        ran, connects = [], []

        def recorder(argv, run=None, which=None):
            ran.append(tuple(argv))
            return None, "not run"
        out_path = self.dir / "fs.json"
        before_repo, before_tmp = snapshot(ROOT), snapshot(self.dir)
        with mock.patch.object(hc, "run_capped", recorder), \
                mock.patch.object(socket.socket, "connect", lambda *a: connects.append(a)), \
                mock.patch.object(hc.subprocess, "Popen", side_effect=AssertionError("Popen")), \
                mock.patch.object(hc.os, "system", side_effect=AssertionError("os.system")):
            self.run_tool(hc.Host(), "--json-out", str(out_path))
        self.assertEqual(connects, [])
        self.assertTrue(set(ran) <= set(hc.COMMANDS.values()), ran)
        self.assertEqual(snapshot(ROOT), before_repo)
        after_tmp = snapshot(self.dir)
        self.assertEqual(set(after_tmp) - set(before_tmp), {"fs.json"})
        self.assertEqual({k: v for k, v in after_tmp.items() if k != "fs.json"}, before_tmp)

    def test_static_imports_and_subprocess_calls(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertFalse(imported & {"socket", "ssl", "http", "urllib", "requests", "ftplib", "smtplib"}, imported)
        for argv in hc.COMMANDS.values():
            self.assertFalse(set(argv) & FORBIDDEN_VERBS, argv)
            self.assertIn(argv[0], hc.ALLOWED_BINARIES)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        names = {c.func.attr for c in calls if isinstance(c.func, ast.Attribute)
                 and isinstance(c.func.value, ast.Name) and c.func.value.id in ("os", "subprocess")}
        self.assertFalse(names & {"Popen", "system", "popen", "check_call", "check_output", "call", "execv", "execve"})
        runs = [c for c in calls if isinstance(c.func, ast.Name) and c.func.id == "run"]
        self.assertEqual(len(runs), 1)
        self.assertIn("timeout", [k.arg for k in runs[0].keywords])
        self.assertNotIn("shell", [k.arg for k in runs[0].keywords])
        self.assertNotIsInstance(runs[0].args[0], ast.Constant)


class Cli(unittest.TestCase):
    def test_help_and_usage(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            hc.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--json-out", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            hc.main(["--bogus"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
