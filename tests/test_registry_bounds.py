"""RF1025-6: the campaign register is read through one bounded regular-file descriptor, and a writer never produces a
register the reader would refuse (1 MiB, raw bytes).

Synthetic real-filesystem tests only: a throwaway directory, synthetic rows and campaigns, no network, nothing real.
Most cases patch the module ceiling down so no test needs a large fixture; the first two use the real 1 MiB value.
"""
import argparse
import contextlib
import errno
import io
import json
import os
import signal
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import registry

REAL_CAP = 1 << 20
TOO_BIG = "the register is larger than 1 MiB"
WOULD_BE = "the register would be larger than 1 MiB; register not updated"
NOT_REGULAR = "the register is not a regular file"


def row(name, directory="/nonexistent-synthetic/x", added="2026-01-01T00:00:00Z"):
    return {"name": name, "directory": directory, "added_at": added}


def pretty(entries):
    return (json.dumps(entries, indent=2) + "\n").encode("utf-8")


def compact(entries):
    return json.dumps(entries, separators=(",", ":")).encode("utf-8")


def padded(entries, size, ascii_only=True):
    """Valid register JSON whose raw length is exactly `size` bytes (trailing JSON whitespace)."""
    body = (json.dumps(entries, indent=2, ensure_ascii=ascii_only) + "\n").encode("utf-8")
    assert len(body) <= size
    return body + b" " * (size - len(body))


class Bounds(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-bounds-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.directory = self.base / "config"
        self.directory.mkdir(mode=0o700)
        self.register = self.directory / "campaigns.json"
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.register)})
        patch.start()
        self.addCleanup(patch.stop)

    def seed(self, data):
        fd = os.open(self.register, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)

    def campaign(self, name):
        root = self.base / name
        root.mkdir()
        (root / "campaign.json").write_text(
            json.dumps({"schema_version": 1, "name": name, "county": "Synthetic", "state": "CA"}), encoding="utf-8")
        return root

    def cap(self, value):
        patch = mock.patch.object(registry, "MAX_BYTES", value)
        patch.start()
        self.addCleanup(patch.stop)

    @contextlib.contextmanager
    def watch(self, chunk=None, after_fstat=None, fstat_error=None, read_error=None, before_open=None):
        """Records the real descriptor, every requested/returned read size, and can fail or perturb one stage."""
        seen = {"fds": [], "reads": [], "returned": []}
        real_open, real_read, real_fstat = os.open, os.read, os.fstat

        def opened(path, flags, *args, **kwargs):
            mine = os.fspath(path) == str(self.register)
            if mine and before_open:
                before_open()
            fd = real_open(path, flags, *args, **kwargs)
            if mine:
                seen["fds"].append(fd)
            return fd

        def read(fd, count):
            mine = fd in seen["fds"]
            if mine:
                seen["reads"].append(count)
                if read_error:
                    raise OSError(errno.EIO, "synthetic read failure")
                count = min(count, chunk) if chunk else count
            data = real_read(fd, count)
            if mine:
                seen["returned"].append(len(data))
            return data

        def fstat(fd):
            if fd not in seen["fds"]:
                return real_fstat(fd)
            if fstat_error:
                raise OSError(errno.EIO, "synthetic fstat failure")
            captured = real_fstat(fd)
            if after_fstat:
                after_fstat()  # the file changes after the size was observed
            return captured

        with mock.patch.object(os, "open", opened), mock.patch.object(os, "read", read), \
                mock.patch.object(os, "fstat", fstat):
            yield seen

    def assert_closed(self, seen):
        for fd in seen["fds"]:
            with self.assertRaises(OSError) as closed:
                os.fstat(fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)

    def no_parse(self):
        return mock.patch.object(registry.json, "loads", side_effect=AssertionError("parser reached"))

    def refused(self, message):
        return self.assertRaisesRegex(ValueError, "^" + message + "$")

    # -- input -------------------------------------------------------------------------------------------------------

    def test_padded_oversize_register_refused_before_read_and_parse(self):
        fd = os.open(self.register, os.O_WRONLY | os.O_CREAT, 0o600)  # valid "[]" then a sparse 4 MiB of padding
        with os.fdopen(fd, "wb") as out:
            out.write(b"[]")
            out.truncate(4 * REAL_CAP + 2)
        before = (self.register.stat().st_size, os.stat(self.register).st_mtime_ns)
        with self.watch() as seen, self.no_parse(), self.refused(TOO_BIG):
            registry._load(self.register)
        self.assertEqual(seen["reads"], [])  # refused on the descriptor's size: no content consumed
        self.assertEqual(len(seen["fds"]), 1)
        self.assert_closed(seen)
        self.assertEqual((self.register.stat().st_size, os.stat(self.register).st_mtime_ns), before)

    def test_raw_byte_cap_minus_one_exact_and_plus_one(self):
        entries = [row("alpha", "/nonexistent-synthetic/é€", "2026-01-01T00:00:00Z")]  # multibyte: bytes != characters
        for size, ok in ((REAL_CAP - 1, True), (REAL_CAP, True), (REAL_CAP + 1, False)):
            with self.subTest(size=size):
                self.seed(padded(entries, size, ascii_only=False))
                self.assertEqual(self.register.stat().st_size, size)
                if ok:
                    self.assertEqual(registry._load(self.register), entries)
                else:
                    with self.refused(TOO_BIG):
                        registry._load(self.register)
        text = padded(entries, REAL_CAP, ascii_only=False).decode("utf-8")  # fewer characters than bytes
        self.seed((text + " ").encode("utf-8"))
        self.assertLess(len(text) + 1, REAL_CAP)  # a character count would admit this register; the byte count refuses
        with self.refused(TOO_BIG):
            registry._load(self.register)

    def test_missing_empty_and_whitespace_only_registers_keep_their_behaviour(self):
        self.assertEqual(registry._load(self.directory / "absent.json"), [])
        absent_parent = self.base / "no-such-directory" / "campaigns.json"
        self.assertEqual(registry._load(absent_parent), [])
        self.assertFalse(absent_parent.parent.exists())  # a read-only consumer creates nothing
        self.seed(b"")
        self.assertEqual(registry._load(self.register), [])
        self.seed(b"  \n")
        with self.assertRaises(ValueError):
            registry._load(self.register)

    def test_growth_after_real_fstat_hits_cap_plus_one_guard(self):
        self.cap(1000)
        entries = [row("alpha")]
        self.seed(padded(entries, 900))

        def grow():
            with open(self.register, "ab") as out:
                out.write(b" " * 300)  # legal whitespace appended after the size was observed

        for chunk in (None, 64):
            with self.subTest(chunk=chunk):
                self.seed(padded(entries, 900))
                with self.watch(chunk=chunk, after_fstat=grow) as seen, self.no_parse(), self.refused(TOO_BIG):
                    registry._load(self.register)
                self.assertLessEqual(sum(seen["returned"]), 1001)
                self.assertEqual(sum(seen["returned"]), 1001)  # stopped at exactly cap + 1, not at the grown size
                self.assertEqual(len(seen["fds"]), 1)
                self.assertTrue(all(0 < n <= 1001 for n in seen["reads"]))
                self.assertEqual(seen["reads"][0], 1001)
                self.assert_closed(seen)
                self.assertEqual(self.register.read_bytes(), padded(entries, 900) + b" " * 300)

    def test_short_reads_accumulate_without_unbounded_fallback(self):
        self.cap(500)
        entries = [row("alpha"), row("beta")]
        for size in (len(pretty(entries)), 500):  # small, and exactly at the cap
            with self.subTest(size=size):
                self.seed(padded(entries, size))
                with self.watch(chunk=7) as seen:
                    self.assertEqual(registry._load(self.register), entries)
                self.assertEqual(sum(seen["returned"]), size)
                self.assertEqual(seen["returned"][-1], 0)  # one read observed EOF; no retry loop after it
                self.assertEqual(len(seen["reads"]), -(-size // 7) + 1)
                remaining = 501
                for asked, got in zip(seen["reads"], seen["returned"]):
                    self.assertTrue(0 < asked <= remaining)
                    remaining -= got
                self.assert_closed(seen)

    def test_leaf_admission_and_reader_fd_cleanup(self):
        entries = [row("alpha")]
        sentinel = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(lambda: os.close(sentinel) if self._open(sentinel) else None)
        target = self.base / "elsewhere.json"
        target.write_bytes(pretty(entries))

        def static_symlink():
            os.symlink(target, self.register)

        def dangling():
            os.symlink(self.base / "does-not-exist", self.register)

        def directory():
            self.register.mkdir()

        def fifo():
            os.mkfifo(self.register, 0o600)

        def stat_failure():
            self.seed(pretty(entries))

        def read_failure():
            self.seed(pretty(entries))

        def bad_utf8():
            self.seed(b"\xff\xfe[]")

        def bad_json():
            self.seed(b"{not json")

        def bad_shape():
            self.seed(json.dumps([{"name": "a"}]).encode())

        def substituted():  # a regular register swapped for a symlink between the caller's checks and the open
            self.seed(pretty(entries))

        def swap():
            self.register.unlink()
            os.symlink(target, self.register)

        def on_alarm(*_):
            raise TimeoutError("a special file blocked the reader")

        cases = [("symlink", static_symlink, {}, NOT_REGULAR), ("dangling", dangling, {}, NOT_REGULAR),
                 ("directory", directory, {}, NOT_REGULAR), ("fifo", fifo, {}, NOT_REGULAR),
                 ("fstat", stat_failure, {"fstat_error": True}, "synthetic fstat failure|.*"),
                 ("read", read_failure, {"read_error": True}, "synthetic read failure|.*"),
                 ("substituted", substituted, {"before_open": swap}, NOT_REGULAR),
                 ("utf8", bad_utf8, {}, None), ("json", bad_json, {}, None), ("shape", bad_shape, {}, None)]
        old = signal.signal(signal.SIGALRM, on_alarm)
        self.addCleanup(signal.signal, signal.SIGALRM, old)
        for label, make, options, message in cases:
            with self.subTest(label):
                if self.register.is_dir() and not self.register.is_symlink():
                    self.register.rmdir()
                else:
                    self.register.unlink(missing_ok=True)
                make()
                signal.alarm(10)
                try:
                    with self.watch(**options) as seen:
                        if message is None:
                            with self.assertRaises(ValueError):  # unchanged small-input refusals
                                registry._load(self.register)
                        elif label in ("fstat", "read"):
                            with self.assertRaises(OSError):
                                registry._load(self.register)
                        else:
                            with self.refused(message):
                                registry._load(self.register)
                finally:
                    signal.alarm(0)
                if label not in ("symlink", "dangling", "substituted"):  # those refuse at open: no descriptor exists
                    self.assertTrue(seen["fds"])
                self.assert_closed(seen)
                self.assertTrue(self._open(sentinel))  # an unrelated descriptor was never closed
        self.assertEqual(target.read_bytes(), pretty(entries))

    @staticmethod
    def _open(fd):
        try:
            os.fstat(fd)
            return True
        except OSError:
            return False

    # -- prospective output ------------------------------------------------------------------------------------------

    def test_store_measures_exact_existing_serialization_before_stage(self):
        old = [row("keep")]
        sibling = self.directory / "unrelated.txt"
        sibling.write_bytes(b"synthetic sibling\n")
        cases = {"ascii": [row("alpha"), row("beta")], "unicode-escaped": [row("é€", "/nonexistent-synthetic/β")],
                 "indent": [row(f"n{i}") for i in range(6)]}
        for label, entries in cases.items():
            oracle = (json.dumps(entries, indent=2) + "\n").encode("utf-8")
            self.assertIn(b"\n  ", oracle)
            for cap, ok in ((len(oracle) - 1, False), (len(oracle), True), (len(oracle) + 1, True)):
                with self.subTest(label=label, cap=cap):
                    self.seed(pretty(old))
                    os.utime(self.register, ns=(1_000_000_000_000_000_000,) * 2)
                    before = (self.register.read_bytes(), os.stat(self.register))
                    sibling_before = (sibling.read_bytes(), sorted(p.name for p in self.directory.iterdir()))
                    with mock.patch.object(registry, "MAX_BYTES", cap):
                        if ok:
                            registry._store(self.register, entries)
                            self.assertEqual(self.register.read_bytes(), oracle)  # unchanged escaping/order/indent
                            self.assertEqual(stat.S_IMODE(os.stat(self.register).st_mode), 0o600)
                            self.assertEqual(registry._load(self.register), entries)
                            continue
                        with mock.patch.object(registry.tempfile, "mkstemp") as stage, \
                                mock.patch.object(os, "fchmod") as chmod, mock.patch.object(os, "fsync") as sync, \
                                mock.patch.object(os, "replace") as replace, mock.patch.object(os, "unlink") as unlink, \
                                self.refused(WOULD_BE):
                            registry._store(self.register, entries)
                    for call in (stage, chmod, sync, replace, unlink):
                        call.assert_not_called()
                    after = os.stat(self.register)
                    self.assertEqual(self.register.read_bytes(), before[0])
                    self.assertEqual((after.st_ino, after.st_mtime_ns, after.st_mode),
                                     (before[1].st_ino, before[1].st_mtime_ns, before[1].st_mode))
                    self.assertEqual((sibling.read_bytes(), sorted(p.name for p in self.directory.iterdir())),
                                     sibling_before)

    def test_newline_only_overflow_is_refused(self):
        entries = [row("alpha")]
        bare = len(json.dumps(entries, indent=2).encode("utf-8"))
        self.cap(bare)  # the document alone fits; the writer's final newline is what overflows
        self.seed(b"[]")
        with self.refused(WOULD_BE):
            registry._store(self.register, entries)
        self.assertEqual(self.register.read_bytes(), b"[]")

    def test_real_add_and_remove_never_install_over_cap_candidate(self):
        rows = [row(f"r{i}", f"/nonexistent-synthetic/r{i}") for i in range(6)]
        first, second = self.campaign("A"), self.campaign("B")
        # remove: the compact input is admitted, but its pretty-printed remainder is over the ceiling
        remainder = pretty(rows[:-1])
        self.cap(len(remainder) - 1)
        self.seed(compact(rows))
        self.assertLessEqual(len(compact(rows)), registry.MAX_BYTES)
        before = os.stat(self.register)
        with mock.patch.object(registry, "_root", side_effect=AssertionError("a campaign was opened")), \
                mock.patch.object(registry.tempfile, "mkstemp") as stage, mock.patch.object(os, "replace") as replace:
            with self.refused(WOULD_BE):
                registry.remove("r5")
        stage.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(self.register.read_bytes(), compact(rows))
        self.assertEqual(os.stat(self.register).st_ino, before.st_ino)
        self.assertEqual(len(registry._load(self.register)), 6)  # still readable
        self.cap(len(remainder))  # at the cap the same removal succeeds, and the result reads back
        with mock.patch.object(registry, "_root", side_effect=AssertionError("a campaign was opened")):
            registry.remove("r5")
        self.assertEqual(self.register.read_bytes(), remainder)
        self.assertEqual(registry._load(self.register), rows[:-1])
        # add: measure the real candidate size first, then show cap-1 refuses and cap succeeds
        self.cap(REAL_CAP)
        self.seed(pretty(rows))
        registry.add("A", first)
        size = len(self.register.read_bytes())
        self.assertEqual(registry._load(self.register)[-1]["name"], "A")
        self.cap(size - 1)
        self.seed(pretty(rows))
        with self.refused(WOULD_BE):
            registry.add("A", first)
        self.assertEqual(self.register.read_bytes(), pretty(rows))
        self.assertEqual(self.others(), [])
        self.cap(size)
        registry.add("A", first)
        self.assertEqual(len(self.register.read_bytes()), size)
        self.assertEqual([e["name"] for e in registry._load(self.register)], [r["name"] for r in rows] + ["A"])
        at_cap = self.register.read_bytes()
        with self.refused(WOULD_BE):  # the register is at its limit: the next candidate is refused, A stays
            registry.add("B", second)
        self.assertEqual(self.register.read_bytes(), at_cap)
        self.assertEqual(self.others(), [])

    def others(self):
        return sorted(p.name for p in self.directory.iterdir() if p.name != "campaigns.json")

    def test_first_add_refusal_leaves_only_the_lock_placeholder(self):
        self.cap(10)
        root = self.campaign("A")
        with self.refused(WOULD_BE):
            registry.add("A", root)
        self.assertEqual(self.register.read_bytes(), b"")  # the transaction's own empty placeholder; no stage remains
        self.assertEqual(self.others(), [])
        self.assertEqual(registry._load(self.register), [])

    # -- consumers ---------------------------------------------------------------------------------------------------

    def run_cli(self, command, **extra):
        args = argparse.Namespace(campaigns_command=command, name=None, directory=None, as_of=None, as_json=False, **extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = registry.run(args)
        return code, out.getvalue(), err.getvalue()

    def test_every_consumer_refuses_register_before_campaign_reads(self):
        self.cap(1000)
        first = self.campaign("A")
        self.seed(padded([row("A", str(first), "2026-01-01T00:00:00Z")], 1001))
        canary = "CANARY-" + str(first)
        with mock.patch.object(registry, "_root", side_effect=AssertionError("campaign opened")), \
                mock.patch.object(registry, "confine", side_effect=AssertionError("campaign confined")), \
                mock.patch("campaign_tool.today.todo", side_effect=AssertionError("campaign iterated")):
            for command in ("list", "status", "today"):
                with self.subTest(command):
                    code, out, err = self.run_cli(command)
                    self.assertEqual((code, out), (1, ""))
                    self.assertEqual(err, f"Stopped: {TOO_BIG}\n")
                    self.assertNotIn(canary, err)
                    self.assertNotIn("Traceback", err)
            parser = argparse.ArgumentParser(prog="t")
            parser.add_argument("--campaign", action=registry.CampaignAction)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stopped:
                parser.parse_args(["--campaign", "A"])
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn(TOO_BIG, err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_ordinary_register_output_is_unchanged(self):
        self.assertEqual(self.run_cli("list"), (0, "", ""))  # no register: empty, exit 0
        self.assertFalse(self.register.exists())
        a, b = self.campaign("A"), self.campaign("B")
        registry.add("B", b)
        registry.add("A", a)
        code, out, err = self.run_cli("list")
        lines = out.splitlines()
        self.assertEqual((code, err, len(lines)), (0, "", 2))
        self.assertTrue(lines[0].startswith(f"campaign | A | {a} | added ") and lines[1].startswith("campaign | B | "))
        self.assertEqual(stat.S_IMODE(os.stat(self.register).st_mode), 0o600)
        self.assertEqual(registry.resolve("B")[0], b)


if __name__ == "__main__":
    unittest.main()
