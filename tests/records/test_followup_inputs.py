"""RF0834-2: follow-up settings and the selected-run register are admitted only as bounded regular files.

A FIFO at either path used to block in open() before the regular-file test. The FIFO cases run in a child
process with a deadline; a timeout is a failure, never a passing refusal. Synthetic projects only.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import followup_clock as fc
from tests.records.test_followup_clock import doc, row

REPO = Path(__file__).resolve().parents[2]
DEADLINE = 20  # seconds; a FIFO without a writer blocks forever, so any finite bound detects the hang
DRAFT = {"schema": fc.SETTINGS_SCHEMA, "agencies": {"ag1": {"autonomy": "draft"}}}
INVALID_OFF = dict(fc._off(), invalid=True)
CHILD = r"""
import json, sys
from campaign_tool.records import followup_clock as fc
what, root = sys.argv[1], sys.argv[2]
sys.stderr.write("READY\n"); sys.stderr.flush()
if what == "settings":
    print(json.dumps(fc.load_settings(root)))
elif what == "queue":
    print(json.dumps(fc.queue_followups(root, "run-1")))
elif what == "console":
    from campaign_tool.records import console_settings
    page = console_settings.render(root)
    print(json.dumps(["settings file invalid: all reminders off" in page, page.count("<td>draft</td>")]))
else:
    try:
        print(json.dumps(fc.read_register(root, "run-1")))
    except (ValueError, OSError) as error:
        print(json.dumps({"refused": isinstance(error, (ValueError, OSError))}))
"""


def fd_count():
    return len(os.listdir("/proc/self/fd"))


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.project("project")

    def project(self, name):
        root = self.base / name
        root.mkdir(mode=0o700)
        return root

    def put_register(self, root, rows=None, run="run-1"):
        directory = root / "run-outputs" / run
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / fc.REGISTER_NAME
        path.write_text(json.dumps(doc([row()] if rows is None else rows)), encoding="utf-8")
        return path

    def put_settings(self, root, body=DRAFT, mode=0o600):
        path = root / fc.SETTINGS_FILE
        if path.is_fifo():
            path.unlink()
        path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        os.chmod(path, mode)
        return path

    def fifo(self, path):
        if path.exists():
            path.unlink()
        os.mkfifo(path, 0o600)
        return path

    def writer(self, path):
        """Hold the FIFO open read-write so a writer is connected and the open cannot block."""
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        os.write(fd, json.dumps(DRAFT).encode())
        return fd

    def child(self, *argv, module=False):
        command = [sys.executable, "-B"] + (["-m", "campaign_tool.records"] if module else ["-c", CHILD]) + list(argv)
        try:
            done = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=DEADLINE,
                                  env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        except subprocess.TimeoutExpired:  # subprocess.run kills and reaps the child before raising
            self.fail("child blocked past %ss: %s" % (DEADLINE, argv[0]))
        self.assertNotIn("Traceback", done.stderr)
        return done

    def tree(self, root):
        out = []
        for path in sorted(root.rglob("*")):
            info = os.lstat(path)
            body = path.read_bytes() if stat.S_ISREG(info.st_mode) else b""
            out.append((str(path.relative_to(root)), stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), body))
        return out


class FifoInputs(Base):
    def test_settings_fifo_with_and_without_writer_returns_invalid_off(self):
        self.assertEqual(json.loads(self.child("settings", str(self.root)).stdout), fc._off())
        self.put_settings(self.root, {"schema": fc.SETTINGS_SCHEMA, "default": {"autonomy": "off"}})
        self.assertEqual(json.loads(self.child("settings", str(self.root)).stdout), fc._off())
        self.put_settings(self.root)
        self.assertEqual(json.loads(self.child("settings", str(self.root)).stdout)["agencies"],
                         {"ag1": {"autonomy": "draft"}})
        path = self.fifo(self.root / fc.SETTINGS_FILE)
        done = self.child("settings", str(self.root))
        self.assertEqual((done.returncode, json.loads(done.stdout)), (0, INVALID_OFF))
        self.assertEqual(done.stderr, "READY\n")
        self.writer(path)
        self.assertEqual(json.loads(self.child("settings", str(self.root)).stdout), INVALID_OFF)

    def test_register_fifo_cli_refuses_before_queue_access(self):
        self.put_settings(self.root)
        self.put_register(self.root, run="run-0")
        path = self.fifo(self.put_register(self.root))
        before = self.tree(self.root)
        for connected in (False, True):
            with self.subTest(writer=connected):
                if connected:
                    self.writer(path)
                self.assertEqual(json.loads(self.child("register", str(self.root)).stdout), {"refused": True})
                done = self.child("followups", "plan", "--project", str(self.root), "--run-id", "run-1", module=True)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "register_unreadable\n"))
                self.assertFalse((self.root / "approvals").exists())
                self.assertEqual(self.tree(self.root), before)

    def test_bad_settings_does_not_stall_queue_or_console(self):
        self.put_settings(self.root)
        self.put_register(self.root)
        self.assertEqual(fc.queue_followups(self.root, "run-1")["created"], 1)
        queued = aq.load_queue(self.root)
        self.fifo(self.root / fc.SETTINGS_FILE)
        before = self.tree(self.root)
        summary = json.loads(self.child("queue", str(self.root)).stdout)
        self.assertEqual((summary["settings_invalid"], summary["created"], summary["existing"], summary["skipped_off"]),
                         (True, 0, 0, 1))
        self.assertEqual(json.loads(self.child("console", str(self.root)).stdout), [True, 0])
        self.assertEqual(self.tree(self.root), before)
        self.assertEqual(aq.load_queue(self.root), queued)
        self.put_settings(self.root)
        self.assertEqual(json.loads(self.child("console", str(self.root)).stdout), [False, 1])


class Bounds(Base):
    def parsed(self):
        seen = []
        real = fc._strict
        return seen, mock.patch.object(fc, "_strict", side_effect=lambda raw: seen.append(len(raw)) or real(raw))

    def test_input_byte_limits_and_short_reads(self):
        body = json.dumps(DRAFT).encode()
        register_body = self.put_register(self.root).read_bytes()
        cases = (("settings", lambda: fc.load_settings(self.root), self.root / fc.SETTINGS_FILE, body, "MAX_SETTINGS"),
                 ("register", lambda: fc.read_register(self.root, "run-1"),
                  self.root / "run-outputs" / "run-1" / fc.REGISTER_NAME, register_body, "MAX_REGISTER"))
        for name, call, path, good, limit in cases:
            path.write_bytes(good)
            os.chmod(path, 0o600)
            control = call()
            for size, admitted in ((len(good) + 1, True), (len(good), True), (len(good) - 1, False)):
                with self.subTest(name=name, limit=size), mock.patch.object(fc, limit, size):
                    seen, patch = self.parsed()
                    with patch:
                        if admitted:
                            self.assertEqual(call(), control)
                        elif name == "settings":
                            self.assertEqual(call(), INVALID_OFF)
                        else:
                            self.assertRaises(ValueError, call)
                    self.assertTrue(all(n <= size for n in seen))
                    self.assertEqual(seen, [len(good)] if admitted else [])
            real_read = os.read
            with self.subTest(name=name, reads="short"), mock.patch("os.read", lambda fd, n: real_read(fd, min(n, 3))):
                self.assertEqual(call(), control)
            real_fstat = os.fstat

            def grow(fd):
                info = real_fstat(fd)
                with open(path, "ab") as stream:
                    stream.write(b" " * 64)
                return info
            with self.subTest(name=name, grows="after fstat"), mock.patch.object(fc, limit, len(good) + 8), \
                    mock.patch("os.fstat", grow):
                seen, patch = self.parsed()
                with patch:
                    if name == "settings":
                        self.assertEqual(call(), INVALID_OFF)
                    else:
                        self.assertRaises(ValueError, call)
                self.assertEqual(seen, [])
            path.write_bytes(good)

    def test_settings_policy_kept_and_register_permissions_compatible(self):
        settings = self.put_settings(self.root)
        register = self.put_register(self.root)
        os.chmod(register, 0o644)
        os.link(register, self.root / "register-copy.json")
        self.assertIsNotNone(fc.read_register(self.root, "run-1"))
        os.link(settings, self.root / "settings-copy.json")
        self.assertEqual(fc.load_settings(self.root), INVALID_OFF)
        os.unlink(self.root / "settings-copy.json")
        for mode in (0o640, 0o604, 0o610):
            os.chmod(settings, mode)
            self.assertEqual(fc.load_settings(self.root), INVALID_OFF)
        os.chmod(settings, 0o400)
        self.assertEqual(fc.load_settings(self.root)["agencies"], {"ag1": {"autonomy": "draft"}})


class Refusals(Base):
    def test_descriptor_and_symlink_refusals_close_every_fd(self):
        settings = self.put_settings(self.root)
        register = self.put_register(self.root)
        good = (settings.read_bytes(), register.read_bytes())
        outside = self.project("outside")
        self.put_settings(outside)
        self.put_register(outside)
        settings_call = lambda: fc.load_settings(self.root)
        register_call = lambda: fc.read_register(self.root, "run-1")

        def check(label, settings_result=INVALID_OFF, register_error=(ValueError, OSError)):
            with self.subTest(label):
                before, fds = self.tree(self.root), fd_count()
                self.assertEqual(settings_call(), settings_result)
                if register_error is None:
                    self.assertIsNone(register_call())
                else:
                    self.assertRaises(register_error, register_call)
                self.assertEqual((self.tree(self.root), fd_count()), (before, fds))

        def reset():
            for path, body in zip((settings, register), good):
                if path.is_symlink() or path.is_fifo() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
                path.write_bytes(body)
                os.chmod(path, 0o600)
        settings.unlink()
        register.unlink()
        check("absent", fc._off(), None)
        for label, make in (("leaf symlink", lambda p: p.symlink_to(outside / p.name)),
                            ("dangling symlink", lambda p: p.symlink_to(self.base / "nothing")),
                            ("directory", lambda p: p.mkdir()),
                            ("fifo with writer", lambda p: (self.fifo(p), self.writer(p)))):
            for path in (settings, register):
                if path.exists() or path.is_symlink():
                    path.unlink()
                make(path)
            check(label)
            reset()
        run = self.root / "run-outputs" / "run-1"
        run.rename(self.root / "run-real")
        run.symlink_to(self.root / "run-real")
        check("ancestor symlink", fc.load_settings(self.root), ValueError)
        fds = fd_count()
        self.assertRaises(OSError, fc._read_bounded, run / fc.REGISTER_NAME, fc.MAX_REGISTER)
        self.assertEqual(fd_count(), fds)
        run.unlink()
        (self.root / "run-real").rename(run)
        for name, target in (("open", "os.open"), ("fstat", "os.fstat"), ("read", "os.read")):
            real = {"os.open": os.open, "os.fstat": os.fstat, "os.read": os.read}[target]

            def fail(*args, _real=real, _name=name, **kwargs):
                if _name != "open" or args[0] in (fc.SETTINGS_FILE, fc.REGISTER_NAME):
                    raise OSError("injected")
                return _real(*args, **kwargs)
            with mock.patch(target, fail):
                check("injected " + name, register_error=OSError)
        for label, raw in (("utf-8", b'{"schema": "\xff"}'), ("duplicate", b'{"schema": 1, "schema": 2}'),
                           ("nonfinite", b'{"schema": NaN}'), ("unknown", b'{"schema": "%s", "extra": 1}' %
                                                              fc.SETTINGS_SCHEMA.encode())):
            settings.write_bytes(raw)
            register.write_bytes(raw.replace(fc.SETTINGS_SCHEMA.encode(), fc.REGISTER_SCHEMA.encode()))
            check(label, register_error=ValueError)
        reset()
        self.assertEqual(settings_call()["agencies"], {"ag1": {"autonomy": "draft"}})
        self.assertEqual(register_call(), json.loads(good[1]))

    def test_two_projects_and_selected_run_stay_isolated(self):
        other = self.project("other")
        for root in (self.root, other):
            self.put_settings(root)
            self.put_register(root, run="run-0")
        self.put_register(self.root)
        self.fifo(other / fc.SETTINGS_FILE)
        self.writer(other / fc.SETTINGS_FILE)
        self.writer(self.fifo(self.put_register(other)))
        before = (self.tree(self.root), self.tree(other))
        for _ in range(2):
            self.assertEqual(fc.load_settings(self.root)["agencies"], {"ag1": {"autonomy": "draft"}})
            self.assertEqual(fc.load_settings(other), INVALID_OFF)
            self.assertEqual(fc.read_register(self.root, "run-1")["requests"][0]["agency_key"], "ag1")
            self.assertRaises(ValueError, fc.read_register, other, "run-1")
            self.assertRaises(ValueError, fc.queue_followups, other, "run-1")
        self.assertIsNone(fc.read_register(self.root, "run-2"))
        self.assertEqual((self.tree(self.root), self.tree(other)), before)
        self.assertFalse((other / "approvals").exists())


if __name__ == "__main__":
    unittest.main()
