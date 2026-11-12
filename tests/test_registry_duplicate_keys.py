"""RF1550-5: a repeated JSON object member anywhere in the campaign register is one fixed ValueError, never json.loads'
last-member-wins reading, so every register verb stops with its normal `Stopped:` line (exit 1) and `--campaign NAME`
with its normal usage error (exit 2), and no refusal rewrites the register.

Synthetic real-filesystem tests only: literal JSON bytes (json.dumps cannot express a repeated member), a throwaway
directory, synthetic campaigns, well under the 1 MiB register ceiling; no network, nothing real.
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
REPEATED = "the register JSON contains a repeated object member"
STOPPED = f"Stopped: {REPEATED}\n"
DAY = date(2026, 11, 11)
STAMP = "2026-01-01T00:00:00Z"


def members(*pairs):
    """Literal JSON object text from (member-name-literal, value-literal) pairs, repeats allowed."""
    return "{" + ", ".join(f"{k}: {v}" for k, v in pairs) + "}"


def register_bytes(*objects):
    return ("[" + ", ".join(objects) + "]").encode("utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-dupkeys-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.config = self.base / "config"
        self.config.mkdir(mode=0o700)
        self.register = self.config / "campaigns.json"
        self.root = self.campaign("Synthetic")
        self.other = self.campaign("Other")
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

    def row(self, *extra, name="Synthetic", root=None):
        """One valid row's member pairs plus extra (possibly repeated) pairs appended."""
        return [('"name"', json.dumps(name)), ('"directory"', json.dumps(str(root or self.root))),
                ('"added_at"', json.dumps(STAMP)), *extra]

    def duplicated(self):
        """The spec's RED control: directory A then directory B in one row."""
        return register_bytes(members(*self.row(('"directory"', json.dumps(str(self.other))))))

    def snapshot(self):
        info = os.stat(self.register)
        return (self.register.read_bytes(), info.st_ino, info.st_mode, info.st_mtime_ns, sorted(os.listdir(self.config)))

    def cli(self, *args, timeout=60):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO, text=True,
                              capture_output=True, timeout=timeout, env=dict(os.environ))

    def assert_fixed(self, exc):
        self.assertIs(type(exc), ValueError)
        self.assertEqual(str(exc), REPEATED)
        shown = "".join(traceback.format_exception(exc))
        self.assertNotIn(str(self.register), shown)
        self.assertNotIn(str(self.root), shown)

    def run_verb(self, **fields):
        args = argparse.Namespace(**{"name": None, "directory": None, "as_of": None, "as_json": False, **fields})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = registry.run(args)
        return code, out.getvalue(), err.getvalue()

    def refuses(self, data):
        self.seed(data)
        with self.assertRaises(ValueError) as caught:
            registry._load(self.register)
        self.assert_fixed(caught.exception)


class DecodedMembers(Base):
    def test_repeated_each_required_member_refuses_even_identical_values(self):
        values = {"name": "Synthetic", "directory": str(self.root), "added_at": STAMP}
        for key in values:
            for label, other in (("identical", values[key]), ("differing", "elsewhere")):
                base = [('"name"', json.dumps("Synthetic")), ('"directory"', json.dumps(str(self.root))),
                        ('"added_at"', json.dumps(STAMP))]
                again = (f'"{key}"', json.dumps(other))
                adjacent = [base[0], base[1], base[2]]
                adjacent.insert([p[0] for p in adjacent].index(f'"{key}"') + 1, again)
                for shape, pairs in (("adjacent", adjacent), ("separated", [*base, again])):
                    with self.subTest(key=key, value=label, shape=shape):
                        self.refuses(register_bytes(members(*pairs)))
        self.seed(self.duplicated())  # neither the first nor the last directory is ever usable
        for call in (lambda: registry.resolve("Synthetic"), registry.status_lines, lambda: registry.today_rows(DAY)):
            with self.assertRaises(ValueError) as caught:
                call()
            self.assert_fixed(caught.exception)

    def test_escaped_decoded_member_identity_and_non_echo(self):
        self.refuses(register_bytes(members(*self.row(('"na\\u006de"', '"Again"')))))
        canary = "canary/\\u00e9\\u0001-private"
        for label, key, value in (("unknown-key", f'"{canary}"', '"v"'), ("unknown-both", f'"{canary}"', f'"{canary}"')):
            with self.subTest(label):
                self.refuses(register_bytes(members(*self.row((key, f'"{canary}-1"'), (key, value)))))
        self.assertNotIn("canary", REPEATED)
        # no normalisation beyond the decoder: case-distinct and unknown single members reach the unchanged shape refusal
        for pairs in ([('"Name"', '"x"'), ('"name"', '"x"'), ('"directory"', '"d"'), ('"added_at"', '"a"')],
                      [*self.row(('"extra"', '"x"'))]):
            self.seed(register_bytes(members(*pairs)))
            with self.assertRaisesRegex(ValueError, r"^the register must hold only a list of \{name, directory, added_at\}$"):
                registry._load(self.register)

    def test_every_object_is_checked_without_cross_row_false_duplicates(self):
        second = self.row(name="Second", root=self.other)
        for label, objects in (
                ("unknown member", [members(*self.row(('"x"', "1"), ('"x"', "2")))]),
                ("nested object member", [members(*self.row(('"x"', '{"k": 1, "k": 2}')))]),
                ("later row", [members(*self.row()), members(*second, ('"added_at"', '"again"'))]),
                ("nested in array", [members(*self.row(('"x"', '[{"k": 1, "k": 1}]')))])):
            with self.subTest(label):
                self.refuses(register_bytes(*objects))
        self.seed(register_bytes(members(*self.row()), members(*second)))  # same fields across distinct rows are normal
        self.assertEqual([e["name"] for e in registry._load(self.register)], ["Synthetic", "Second"])
        self.seed(register_bytes(members(*self.row()), members(*self.row(root=self.other))))  # equal names: unchanged policy
        self.assertEqual([e["name"] for e in registry._load(self.register)], ["Synthetic", "Synthetic"])


class Routes(Base):
    def test_real_cli_verbs_refuse_before_partial_output_or_iteration(self):
        self.seed(self.duplicated())
        before = self.snapshot()
        for argv in (("list",), ("status",), ("today", "--as-of", "2026-11-11"),
                     ("today", "--as-of", "2026-11-11", "--json"), ("remove", "Synthetic"),
                     ("add", "Third", "--directory", self.root)):
            with self.subTest(argv):
                result = self.cli("campaigns", *argv)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", STOPPED))
        self.assertEqual(self.snapshot(), before)
        trip = AssertionError("campaign work reached")
        with mock.patch.object(registry, "_root", side_effect=trip) as root, \
                mock.patch.object(registry, "confine", side_effect=trip) as confine, \
                mock.patch.object(registry, "_state", side_effect=trip) as state, \
                mock.patch.object(registry.fcntl, "flock", side_effect=trip) as flock:
            for fields in ({"campaigns_command": "list"}, {"campaigns_command": "status"},
                           {"campaigns_command": "today", "as_of": "2026-11-11"},
                           {"campaigns_command": "today", "as_of": "2026-11-11", "as_json": True}):
                with self.subTest(fields):
                    self.assertEqual(self.run_verb(**fields), (1, "", STOPPED))
        for tripwire in (root, confine, state, flock):  # read-only consumers start no campaign and take no lock
            tripwire.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        with mock.patch.object(registry, "_read_bounded", side_effect=AssertionError("register read")) as read:
            code, out, err = self.run_verb(campaigns_command="today", as_of="2026-13-40")
        read.assert_not_called()  # an invalid --as-of keeps its earlier precedence
        self.assertEqual((code, out, err), (1, "", "Stopped: --as-of must be a real YYYY-MM-DD date\n"))

    def test_named_selection_is_argparse_refusal_before_command_work(self):
        # the repeat sits in a row AFTER the matching named entry, so early selection cannot bypass whole-register validation
        self.seed(register_bytes(members(*self.row()), members(*self.row(('"name"', '"x"'), name="Other", root=self.other))))
        before = sorted(p.name for p in self.root.iterdir())
        result = self.cli("status", "--campaign", "Synthetic")
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertTrue(result.stderr.startswith("usage: "), result.stderr)
        self.assertTrue(result.stderr.endswith(f": error: {REPEATED}\n"), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(str(self.register), result.stderr)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

        parser = argparse.ArgumentParser(prog="synthetic")
        parser.add_argument("--campaign", action=registry.CampaignAction)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            parser.parse_args(["--campaign", "Synthetic"])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(err.getvalue(), f"usage: synthetic [-h] [--campaign CAMPAIGN]\nsynthetic: error: {REPEATED}\n")

    def test_mutations_preserve_ambiguous_bytes_and_release_lock(self):
        self.seed(self.duplicated())
        before = self.snapshot()
        trip = AssertionError("write path reached")
        with mock.patch.object(registry, "_store", side_effect=trip) as store, \
                mock.patch.object(registry.tempfile, "mkstemp", side_effect=trip) as stage, \
                mock.patch.object(registry.os, "replace", side_effect=trip) as replace:
            for call in (lambda: registry.add("Third", self.root), lambda: registry.remove("Synthetic")):
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
        self.assertEqual([e["name"] for e in json.loads(self.register.read_bytes())], ["Synthetic"])
        self.assertEqual(os.stat(self.register).st_mode & 0o777, 0o600)
        self.assertEqual(self.cli("campaigns", "remove", "Synthetic", timeout=30).returncode, 0)
        self.assertEqual(json.loads(self.register.read_bytes()), [])


class Precedence(Base):
    def test_reader_closed_and_existing_error_precedence_unchanged(self):
        self.seed(self.duplicated())
        identity = (os.stat(self.register).st_dev, os.stat(self.register).st_ino)
        opened, at_parse, real_open = [], [], os.open
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

        real_hook = registry._unique_members

        def track_hook(pairs):
            at_parse.append([still_ours(fd) for fd in opened])
            return real_hook(pairs)

        with mock.patch.object(registry.os, "open", side_effect=track_open), \
                mock.patch.object(registry, "_unique_members", side_effect=track_hook), \
                mock.patch.object(registry.fcntl, "flock", side_effect=AssertionError("lock taken")) as flock:
            for call in (lambda: registry._load(self.register), lambda: registry.resolve("Synthetic"),
                         registry.status_lines):
                with self.assertRaises(ValueError):
                    call()
        flock.assert_not_called()
        self.assertEqual(len(opened), 3)
        self.assertEqual(at_parse[0], [False])  # the reader is closed before the hook first runs
        self.assertTrue(all(not flag for seen in at_parse for flag in seen))
        self.assertFalse(any(still_ours(fd) for fd in opened))
        os.fstat(sentinel_r)  # an unrelated descriptor stays open

        with mock.patch.object(registry, "MAX_BYTES", 16):
            self.seed(self.duplicated())
            with self.assertRaisesRegex(ValueError, "^the register is larger than 1 MiB$"):
                registry._load(self.register)
        for data, kind in ((b"\xff[", UnicodeDecodeError), (b"[", json.JSONDecodeError), (b"  \n", json.JSONDecodeError)):
            with self.subTest(data):
                self.seed(data)
                with self.assertRaises(kind):
                    registry._load(self.register)
        self.seed(b"")
        self.assertEqual(registry._load(self.register), [])
        self.register.unlink()
        self.assertEqual(registry._load(self.register), [])
        self.seed(b"[" * 10_000)  # RF1500-24's narrow depth translation stays intact
        with self.assertRaisesRegex(ValueError, "^the register JSON is nested too deeply$"):
            registry._load(self.register)

    def test_normal_register_bytes_outputs_and_no_write_controls(self):
        cafe = self.campaign("Café-[x]")
        self.assertFalse(self.register.exists())
        self.assertEqual(self.cli("campaigns", "list").stdout, "")
        self.assertFalse(self.register.exists())  # read-only calls create nothing
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
        info = os.stat(self.register)
        self.assertEqual(self.cli("status", "--campaign", "zeta").returncode, 0)
        self.assertEqual(os.stat(self.register).st_mtime_ns, info.st_mtime_ns)
        self.assertEqual(self.cli("campaigns", "remove", "zeta").returncode, 0)
        self.assertEqual([e["name"] for e in json.loads(self.register.read_bytes())], ["alpha"])
        self.assertEqual(os.stat(self.register).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
