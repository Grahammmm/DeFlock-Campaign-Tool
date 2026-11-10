"""kit-copy reads its source draft from the opened file with an actual-byte bound (1 MiB, inclusive).

Synthetic campaigns only; no network or SMTP. Growth and replacement are injected through the real pathname-stat
and descriptor-fstat seams, and descriptor reads/closes are observed by wrapping the real os calls.
"""
import contextlib
import errno
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_copy  # noqa: E402
from tests import test_kit_mail  # noqa: E402

CAP = kit_copy.MAX_BYTES
TODAY = test_kit_mail.TODAY
LARGER = "is larger than 1 MiB"
REGULAR = r"must be a regular file \(not a symlink\)"
REAL_OS_READ, REAL_OS_FSTAT, REAL_OS_OPEN, REAL_OS_CLOSE = os.read, os.fstat, os.open, os.close
REAL_PATH_STAT = Path.stat


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def pad(base, size):
    """``base`` extended to exactly ``size`` bytes by a plain tail line after the draft's end marker."""
    gap = size - len(base)
    assert gap >= 2
    return base + b"\n" + b"x" * (gap - 1)


class Watch:
    """Observes the real descriptor calls for one path: opens, closes, read requests and bytes returned."""

    def __init__(self, path, short=None, on_fstat=None, fail=None):
        self.path, self.short, self.on_fstat, self.fail = Path(path), short, on_fstat, fail
        self.fds, self.closed, self.requests, self.returned, self.stack = [], [], [], 0, contextlib.ExitStack()

    def open(self, name, flags, *args, **kwargs):
        fd = REAL_OS_OPEN(name, flags, *args, **kwargs)
        if isinstance(name, (str, os.PathLike)) and Path(name) == self.path:
            self.fds.append(fd)
        return fd

    def close(self, fd):
        if fd in self.fds:
            self.closed.append(fd)
        return REAL_OS_CLOSE(fd)

    def fstat(self, fd, *args, **kwargs):
        info = REAL_OS_FSTAT(fd, *args, **kwargs)
        if fd in self.fds:
            if self.fail == "fstat":
                raise OSError(errno.EIO, "synthetic fstat failure")
            if self.on_fstat:
                action, self.on_fstat = self.on_fstat, None
                action()
        return info

    def read(self, fd, count):
        if fd not in self.fds:
            return REAL_OS_READ(fd, count)
        self.requests.append(count)
        if self.fail == "read":
            raise OSError(errno.EIO, "synthetic read failure")
        data = REAL_OS_READ(fd, count if self.short is None else min(count, self.short))
        self.returned += len(data)
        return data

    def __enter__(self):
        for name in ("open", "close", "fstat", "read"):
            self.stack.enter_context(mock.patch.object(kit_copy.os, name, getattr(self, name)))
        return self

    def __exit__(self, *exc):
        self.stack.close()


class KitCopyBoundsTests(unittest.TestCase):
    def setUp(self):
        fixture = test_kit_mail.KitMailTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.verify(records_email="records@example.org")
        self.fx, self.root, self.draft, self.out = fixture, fixture.root, fixture.draft, fixture.out
        self.original = self.draft.read_bytes()
        self.label = f"kit/requests/{fixture.a}.md"
        self.tmp = Path(tempfile.mkdtemp(prefix="synthetic-kit-copy-bounds-", dir=fixture.tmp))

    def grow(self, size=CAP + 4096):
        self.draft.write_bytes(pad(self.original, size))

    def reader(self, data):
        leaf = self.tmp / "leaf.md"
        leaf.write_bytes(data)
        return kit_copy._regular(leaf, "leaf")

    def draft_stats(self):
        """Number of following Path.stat calls kit_copy._regular makes on the draft (stable across Python versions)."""
        calls = []

        def spy(path, *args, **kwargs):
            if path == self.draft:
                calls.append(1)
            return REAL_PATH_STAT(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", spy):
            kit_copy._regular(self.draft, self.label)
        return len(calls)

    @contextlib.contextmanager
    def after_path_stat(self, action):
        """Run ``action`` right after the draft's last pathname stat in _regular; the real stat result is returned."""
        total, seen = self.draft_stats(), []

        def hook(path, *args, **kwargs):
            info = REAL_PATH_STAT(path, *args, **kwargs)
            if path == self.draft:
                seen.append(1)
                if len(seen) == total:
                    action()
            return info

        with mock.patch.object(Path, "stat", hook):
            yield

    def tree(self):
        out = self.out
        listing = {p.name: (p.lstat().st_mode, p.lstat().st_mtime_ns, p.read_bytes()) for p in sorted(out.iterdir())} \
            if out.is_dir() else None
        return listing, stat.S_IMODE(out.stat().st_mode) if out.is_dir() else None

    def command(self, target):
        return run("kit-copy", "--directory", self.root, "--agency", self.fx.a, "--for", target, "--json")

    def test_regular_exact_encoded_byte_boundaries(self):
        for size in (0, CAP - 1, CAP):
            self.assertEqual(self.reader(b"a" * size), "a" * size)
        multibyte = ("é" * (CAP // 2)).encode()
        self.assertEqual(len(multibyte), CAP)
        self.assertEqual(self.reader(multibyte), multibyte.decode())
        for data in (b"a" * (CAP + 1), ("é" * (CAP // 2 + 1)).encode()):
            with Watch(self.tmp / "leaf.md") as watch, self.assertRaisesRegex(ValueError, LARGER):
                self.reader(data)
            self.assertEqual((watch.requests, watch.fds, watch.closed), ([], [], []))  # static oversize: no read

    def test_growth_after_path_stat_refuses_real_copy_targets(self):
        for target in ("portal", "email", "mail"):
            with self.subTest(target=target):
                self.draft.write_bytes(self.original)
                before = self.tree()
                with self.after_path_stat(self.grow):
                    code, out, err = self.command(target)
                self.assertEqual(code, 3, err)
                self.assertEqual(out, "")
                self.assertIn(f"Stopped: {self.label} {LARGER}", err)
                self.assertNotIn("Copy only", err)
                self.assertEqual(self.tree(), before)

    def test_growth_after_real_fstat_stops_at_cap_plus_one(self):
        for short in (None, 1000):
            with self.subTest(short=short):
                self.draft.write_bytes(self.original)
                with mock.patch.object(kit_copy, "sendable", side_effect=AssertionError("sendable called")), \
                        Watch(self.draft, short=short, on_fstat=self.grow) as watch, \
                        self.assertRaisesRegex(ValueError, LARGER):
                    kit_copy._regular(self.draft, self.label)
                self.assertLessEqual(watch.returned, CAP + 1)
                self.assertEqual(watch.returned, CAP + 1)
                remaining = CAP + 1
                for request in watch.requests:
                    self.assertLessEqual(request, min(1 << 16, remaining))
                    remaining -= request if short is None else min(request, short)
                self.assertEqual((len(watch.fds), watch.closed), (1, watch.fds))

    def test_short_reads_continue_to_eof_without_truncating(self):
        data = pad(self.original, 200_000)
        self.draft.write_bytes(data)
        with Watch(self.draft, short=7000) as watch:
            text = kit_copy._regular(self.draft, self.label)
        self.assertEqual(text, data.decode("utf-8"))
        self.assertGreater(len(watch.requests), 3)
        self.draft.write_bytes(b"Subject: s\n\nbody \xc3")  # incomplete multibyte sequence at EOF
        with Watch(self.draft, short=3) as watch, self.assertRaises(UnicodeDecodeError):
            kit_copy._regular(self.draft, self.label)
        self.assertEqual(watch.closed, watch.fds)

    def test_opened_leaf_revalidates_type_without_following_or_blocking(self):
        secret = self.tmp / "target.md"
        secret.write_bytes(self.original)

        def symlink():
            self.draft.unlink()
            self.draft.symlink_to(secret)

        def directory():
            self.draft.unlink()
            self.draft.mkdir()

        def fifo():
            self.draft.unlink()
            os.mkfifo(self.draft)

        for name, swap in (("symlink", symlink), ("directory", directory), ("fifo", fifo)):
            with self.subTest(name):
                self.draft.write_bytes(self.original)
                with mock.patch.object(kit_copy, "sendable", side_effect=AssertionError("sendable called")), \
                        self.after_path_stat(swap), Watch(self.draft) as watch:
                    with self.assertRaisesRegex(ValueError, REGULAR):
                        kit_copy._regular(self.draft, self.label)
                self.assertEqual(watch.requests, [])
                self.assertEqual(watch.closed, watch.fds)
                if self.draft.is_dir() and not self.draft.is_symlink():
                    self.draft.rmdir()
                else:
                    self.draft.unlink()
        self.draft.mkdir()  # static refusals keep the existing diagnostic
        with self.assertRaisesRegex(ValueError, REGULAR):
            kit_copy._regular(self.draft, self.label)
        self.draft.rmdir()
        self.draft.symlink_to(self.tmp / "missing.md")
        with self.assertRaisesRegex(ValueError, REGULAR):
            kit_copy._regular(self.draft, self.label)
        self.draft.unlink()
        with self.assertRaisesRegex(ValueError, REGULAR):
            kit_copy._regular(self.draft, self.label)

    def test_descriptor_lifetime_on_admission_read_and_decode_paths(self):
        cases = (("ok", {}, None), ("size", {"on_fstat": self.grow}, ValueError),
                 ("fstat", {"fail": "fstat"}, OSError), ("read", {"fail": "read"}, OSError))
        for name, options, error in cases:
            with self.subTest(name):
                self.draft.write_bytes(self.original)
                with Watch(self.draft, **options) as watch:
                    if error:
                        with self.assertRaises(error):
                            kit_copy._regular(self.draft, self.label)
                    else:
                        kit_copy._regular(self.draft, self.label)
                self.assertEqual(len(watch.fds), 1)
                self.assertEqual(watch.closed, watch.fds)
        self.draft.write_bytes(b"Subject: s\n\n\xff\xfe")
        with Watch(self.draft) as watch, self.assertRaises(UnicodeDecodeError):
            kit_copy._regular(self.draft, self.label)
        self.assertEqual((len(watch.fds), watch.closed), (1, watch.fds))
        with self.assertRaisesRegex(OSError, "synthetic"), \
                mock.patch.object(kit_copy.os, "open", side_effect=OSError(errno.EACCES, "synthetic open failure")):
            kit_copy._regular(self.draft, self.label)  # an unrelated open failure keeps its class

    def test_reader_refusal_preserves_existing_outgoing_and_inputs(self):
        for target in ("portal", "email", "mail"):
            for grown in (False, True):
                with self.subTest(target=target, grown=grown):
                    self.draft.write_bytes(self.original)
                    self.out.mkdir(exist_ok=True)
                    for name in (f"{self.fx.a}.txt", f"{self.fx.a}.eml", f"{self.fx.a}.letter.html",
                                 f"{self.fx.a}.letter.txt", ".stage-sentinel"):
                        (self.out / name).write_bytes(b"EXISTING " + name.encode())
                    os.chmod(self.out, 0o755)
                    before = self.tree()
                    if grown:
                        with self.after_path_stat(self.grow):
                            code, out, err = self.command(target)
                    else:
                        self.grow()
                        code, out, err = self.command(target)
                    inputs = {p: p.read_bytes() for p in (self.draft, self.fx.settings, self.fx.agencies)}
                    self.assertEqual((code, out), (3, ""))
                    self.assertIn(LARGER, err)
                    self.assertNotIn("Traceback", err)
                    self.assertEqual(self.tree(), before)
                    self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o755)
                    self.assertFalse((self.fx.kit / "sent.json").exists())
                    self.assertEqual(inputs[self.draft], pad(self.original, CAP + 4096))
                    self.assertEqual(json.loads(inputs[self.fx.settings])["requester"],
                                     ["Pat Example", "1 Test Way", "Exampleville, CA 90001"])

    def test_valid_outputs_cr_subject_and_mail_gate_remain_compatible(self):
        for target in ("portal", "email"):
            self.draft.write_bytes(self.original)
            ordinary = self.command(target)
            self.assertEqual(ordinary[0], 0, ordinary[2])
            ordinary_bytes = (self.out / f"{self.fx.a}.{'txt' if target == 'portal' else 'eml'}").read_bytes()
            if target == "portal":
                self.assertEqual(stat.S_IMODE((self.out / f"{self.fx.a}.txt").stat().st_mode), 0o600)
            self.draft.write_bytes(pad(self.original, CAP))  # exactly the cap, with the padding in the excluded tail
            capped = self.command(target)
            self.assertEqual(capped[0], 0, capped[2])
            self.assertEqual((self.out / f"{self.fx.a}.{'txt' if target == 'portal' else 'eml'}").read_bytes(),
                             ordinary_bytes)
        self.draft.write_bytes(self.original)
        first = kit_copy.copy(self.root, self.fx.a, "mail", today=TODAY)
        twins = [(self.root / name).read_bytes() for name in first["written"]]
        self.draft.write_bytes(pad(self.original, CAP))
        self.assertEqual(kit_copy.copy(self.root, self.fx.a, "mail", today=TODAY), first)
        self.assertEqual([(self.root / name).read_bytes() for name in first["written"]], twins)
        # Raw CR in the subject stays visible; strict UTF-8 is still enforced; oversize in an excluded tail still counts.
        self.draft.write_bytes(self.original.replace(b"Subject: ", b"Subject: a\rb ", 1))
        code, _, err = self.command("portal")
        self.assertEqual(code, 3)
        self.assertIn("subject must be one line", err)
        self.draft.write_bytes(self.original + b"\n\xff\xfe")
        with self.assertRaises(UnicodeDecodeError):
            kit_copy.copy(self.root, self.fx.a, "portal")


if __name__ == "__main__":
    unittest.main()
