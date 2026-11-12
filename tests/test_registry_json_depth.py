"""RF1500-24: campaign-register JSON the decoder cannot finish (RecursionError) is one fixed ValueError, so every
register verb stops with its normal `Stopped:` line (exit 1) and `--campaign NAME` with its normal usage error (exit 2).

Synthetic real-filesystem tests only: a throwaway directory, synthetic campaigns, ordinary interpreter recursion
settings, well under the 1 MiB register ceiling; no network, nothing real.
"""
import argparse
import contextlib
import errno
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import registry

REPO = Path(__file__).resolve().parents[1]
DEEP = "the register JSON is nested too deeply"
STOPPED = f"Stopped: {DEEP}\n"
DEPTH = 10_000
PAYLOADS = {  # all far below MAX_BYTES and far deeper than the decoder can recurse
    "arrays": ("[" * DEPTH + "0" + "]" * DEPTH).encode(),
    "objects": ('{"a":' * DEPTH + "0" + "}" * DEPTH).encode(),
    "truncated": ("[" * (2 * DEPTH)).encode(),
}
DAY = date(2026, 11, 11)


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-depth-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.config = self.base / "config"
        self.config.mkdir(mode=0o700)
        self.register = self.config / "campaigns.json"
        self.root = self.campaign("Synthetic")
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.register)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.release)

    def campaign(self, name):
        root = self.base / name
        root.mkdir()
        (root / "campaign.json").write_text(
            json.dumps({"schema_version": 1, "name": name, "county": "Synthetic", "state": "CA"}), encoding="utf-8")
        return root

    def seed(self, data):
        fd = os.open(self.register, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)

    def snapshot(self):
        info = os.stat(self.register)
        return (self.register.read_bytes(), info.st_ino, info.st_mode, info.st_mtime_ns, sorted(os.listdir(self.config)))

    def cli(self, *args, timeout=60):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO, text=True,
                              capture_output=True, timeout=timeout, env=dict(os.environ))

    def assert_fixed(self, exc):
        self.assertIs(type(exc), ValueError)
        self.assertEqual(str(exc), DEEP)
        self.assertIsNone(exc.__cause__)
        self.assertTrue(exc.__suppress_context__)
        shown = "".join(traceback.format_exception(exc))
        self.assertNotIn("RecursionError", shown)
        self.assertNotIn(str(self.register), shown)
        self.assertNotIn("[[[", shown)

    def run_verb(self, **fields):
        args = argparse.Namespace(**{"name": None, "directory": None, "as_of": None, "as_json": False, **fields})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = registry.run(args)
        return code, out.getvalue(), err.getvalue()


class DecoderDepth(Base):
    def test_real_deep_arrays_objects_and_truncated_prefix_are_fixed_valueerror(self):
        for label, payload in PAYLOADS.items():
            self.assertLess(len(payload), registry.MAX_BYTES)
            with self.subTest(label):
                self.seed(payload)
                with mock.patch.object(registry, "_root", side_effect=AssertionError("campaign iterated")) as root:
                    for call in (lambda: registry._load(self.register), lambda: registry.resolve("Synthetic"),
                                 registry.status_lines, lambda: registry.today_rows(DAY)):
                        with self.assertRaises(ValueError) as caught:
                            call()
                        self.assert_fixed(caught.exception)
                root.assert_not_called()

    def test_all_campaigns_verbs_refuse_without_partial_stdout(self):
        self.seed(PAYLOADS["arrays"])
        for argv in (("list",), ("status",), ("today", "--as-of", "2026-11-11"),
                     ("today", "--as-of", "2026-11-11", "--json"), ("remove", "Synthetic"),
                     ("add", "Other", "--directory", self.root)):
            with self.subTest(argv):
                result = self.cli("campaigns", *argv)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", STOPPED))
        for fields in ({"campaigns_command": "list"}, {"campaigns_command": "status"},
                       {"campaigns_command": "today", "as_of": "2026-11-11"},
                       {"campaigns_command": "today", "as_of": "2026-11-11", "as_json": True},
                       {"campaigns_command": "remove", "name": "Synthetic"},
                       {"campaigns_command": "add", "name": "Other", "directory": str(self.root)}):
            with self.subTest(fields):
                self.assertEqual(self.run_verb(**fields), (1, "", STOPPED))
        with mock.patch.object(registry, "_read_bounded", side_effect=AssertionError("register read")) as read:
            code, out, err = self.run_verb(campaigns_command="today", as_of="2026-13-40")
        read.assert_not_called()
        self.assertEqual((code, out, err), (1, "", "Stopped: --as-of must be a real YYYY-MM-DD date\n"))

    def test_named_selector_is_usage_refusal_before_command_work(self):
        self.seed(PAYLOADS["arrays"])
        before = sorted(p.name for p in self.root.iterdir())
        result = self.cli("status", "--campaign", "Synthetic")
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertTrue(result.stderr.startswith("usage: "), result.stderr)
        self.assertTrue(result.stderr.endswith(f": error: {DEEP}\n"), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(str(self.register), result.stderr)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

        parser = argparse.ArgumentParser(prog="synthetic")
        parser.add_argument("--campaign", action=registry.CampaignAction)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            parser.parse_args(["--campaign", "Synthetic"])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(err.getvalue(), f"usage: synthetic [-h] [--campaign CAMPAIGN]\nsynthetic: error: {DEEP}\n")

    def test_refused_mutations_preserve_register_and_release_lock(self):
        self.seed(PAYLOADS["objects"])
        before = self.snapshot()
        trip = AssertionError("write path reached")
        with mock.patch.object(registry, "_store", side_effect=trip) as store, \
                mock.patch.object(registry.tempfile, "mkstemp", side_effect=trip) as stage, \
                mock.patch.object(registry.os, "replace", side_effect=trip) as replace:
            for call in (lambda: registry.add("Other", self.root), lambda: registry.remove("Synthetic")):
                with self.assertRaises(ValueError) as caught:
                    call()
                self.assert_fixed(caught.exception)
        for tripwire in (store, stage, replace):
            tripwire.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        fd = os.open(self.register, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError if the transaction kept its lock
        finally:
            os.close(fd)

        self.seed(b"[]\n")  # fixture-only repair; the product never rewrites invalid input
        self.assertEqual(self.cli("campaigns", "add", "Synthetic", "--directory", self.root, timeout=30).returncode, 0)
        self.assertEqual(len(json.loads(self.register.read_bytes())), 1)
        self.assertEqual(os.stat(self.register).st_mode & 0o777, 0o600)
        self.assertEqual(self.cli("campaigns", "remove", "Synthetic", timeout=30).returncode, 0)
        self.assertEqual(json.loads(self.register.read_bytes()), [])

    def test_reader_descriptor_closed_on_decoder_exhaustion(self):
        self.seed(PAYLOADS["arrays"])
        identity = (os.stat(self.register).st_dev, os.stat(self.register).st_ino)
        opened, at_parse, real_open, real_loads = [], [], os.open, json.loads
        sentinel_r, sentinel_w = os.pipe()
        self.addCleanup(os.close, sentinel_w)
        self.addCleanup(os.close, sentinel_r)

        def still_ours(fd):
            try:
                info = os.fstat(fd)
            except OSError as exc:
                self.assertEqual(exc.errno, errno.EBADF)
                return False
            return (info.st_dev, info.st_ino) == identity

        def track_open(path, flags, *rest):
            fd = real_open(path, flags, *rest)
            if os.fspath(path) == str(self.register):
                opened.append(fd)
            return fd

        def track_loads(*args, **kwargs):
            at_parse.append([still_ours(fd) for fd in opened])
            return real_loads(*args, **kwargs)

        listing = sorted(os.listdir(self.config))
        with mock.patch.object(registry.os, "open", side_effect=track_open), \
                mock.patch.object(registry.json, "loads", side_effect=track_loads), \
                mock.patch.object(registry.fcntl, "flock", side_effect=AssertionError("lock taken")) as flock:
            for call in (lambda: registry._load(self.register), lambda: registry.resolve("Synthetic"),
                         registry.status_lines, lambda: registry.today_rows(DAY),
                         lambda: self.assertEqual(self.run_verb(campaigns_command="list"), (1, "", STOPPED))):
                with contextlib.suppress(ValueError):
                    call()
        flock.assert_not_called()
        self.assertEqual(len(opened), 5)
        self.assertEqual(at_parse, [[False] * n for n in range(1, 6)])  # closed before the parser ever runs
        self.assertFalse(any(still_ours(fd) for fd in opened))
        os.fstat(sentinel_r)  # an unrelated descriptor stays open
        self.assertEqual(sorted(os.listdir(self.config)), listing)

    def test_only_decoder_recursion_is_translated(self):
        self.seed(b"[]")
        with mock.patch.object(registry.json, "loads", side_effect=RecursionError("canary-input")):
            with self.assertRaises(ValueError) as caught:
                registry._load(self.register)
        self.assert_fixed(caught.exception)
        self.assertNotIn("canary-input", "".join(traceback.format_exception(caught.exception)))
        for kind in (MemoryError, RuntimeError, KeyboardInterrupt):
            with self.subTest(kind.__name__), mock.patch.object(registry.json, "loads", side_effect=kind("x")):
                with self.assertRaises(kind) as caught:
                    registry._load(self.register)
                self.assertIs(type(caught.exception), kind)
        with mock.patch.object(registry, "_read_bounded", side_effect=RecursionError("upstream")), \
                self.assertRaises(RecursionError):
            registry._load(self.register)

    def test_size_encoding_shape_and_ordinary_bytes_keep_precedence(self):
        with mock.patch.object(registry, "MAX_BYTES", 1024), \
                mock.patch.object(registry.json, "loads", side_effect=AssertionError("parsed")) as loads:
            self.seed(PAYLOADS["arrays"])
            with self.assertRaisesRegex(ValueError, "^the register is larger than 1 MiB$"):
                registry._load(self.register)
        loads.assert_not_called()
        for data, kind in ((b"\xff[[[", UnicodeDecodeError), (b"[", json.JSONDecodeError),
                           (b"  \n", json.JSONDecodeError)):
            with self.subTest(data):
                self.seed(data)
                with self.assertRaises(kind):
                    registry._load(self.register)
        self.seed(b'{"name": "x"}')
        with self.assertRaisesRegex(ValueError, r"^the register must hold only a list of \{name, directory, added_at\}$"):
            registry._load(self.register)
        self.seed(b"")
        self.assertEqual(registry._load(self.register), [])
        self.register.unlink()
        self.assertEqual(registry._load(self.register), [])

        bracketed = [{"name": "b", "directory": "/synthetic/[[[{{{", "added_at": "2026-01-01T00:00:00Z"}]
        self.seed(json.dumps(bracketed).encode())
        self.assertEqual(registry._load(self.register), bracketed)

        self.register.unlink()
        cafe = self.campaign("Café-[x]")
        for name, root in (("zeta", self.root), ("alpha", cafe)):
            self.assertEqual(self.cli("campaigns", "add", name, "--directory", root).returncode, 0)
        entries = json.loads(self.register.read_bytes())
        self.assertEqual(self.register.read_bytes(), (json.dumps(entries, indent=2) + "\n").encode("utf-8"))
        self.assertEqual([e["name"] for e in entries], ["zeta", "alpha"])
        self.assertEqual(os.stat(self.register).st_mode & 0o777, 0o600)
        listed = self.cli("campaigns", "list")
        self.assertEqual(listed.returncode, 0)
        self.assertEqual(listed.stdout, "".join(f"campaign | {e['name']} | {e['directory']} | added {e['added_at']}\n"
                                                for e in sorted(entries, key=lambda e: e["name"])))
        self.assertIn(str(cafe), listed.stdout)


if __name__ == "__main__":
    unittest.main()
