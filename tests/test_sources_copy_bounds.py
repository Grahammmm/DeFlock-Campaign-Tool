"""RF0535-3: `sources verify-copy` enforces the downloaded-copy byte limit on the bytes actually read.

A file that grows after the descriptor size check must be refused before any check is recorded. Synthetic campaign and
bytes only; nothing is fetched. Hashes are built from bytes at test time (no hash literals).
"""
import contextlib
import errno
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, sources_cmd
from campaign_tool.sources_cmd import verify_copy
from tests.test_sources_verify_copy import A, EXAMPLE, RECORD, REPO_ROOT, TODAY, entry, tree

MIB = 1024 * 1024
SUFFIX = b"synthetic growth after the descriptor size check; "
U1, U2 = "https://example.invalid/fixtures/one", "https://example.invalid/fixtures/two"
SENTINEL = "synthetic-private-sentinel"


def digest_of(data):
    return hashlib.sha256(data).hexdigest()


class RecordingDigest:
    """Forwards to a real sha256 and records what was hashed."""
    created = []

    def __init__(self):
        self.inner = hashlib.sha256()
        self.sizes = []
        self.finished = False
        RecordingDigest.created.append(self)

    def update(self, data):
        self.sizes.append(len(data))
        self.inner.update(data)

    def hexdigest(self):
        self.finished = True
        return self.inner.hexdigest()


class RecordingHandle:
    """Forwards every read to the genuine binary handle; optionally caps returned chunks and grows the file."""

    def __init__(self, inner, log, short=None, after_read=None):
        self.inner, self.log, self.short, self.after_read = inner, log, short, after_read
        self.closed_calls = 0

    def read(self, size=-1):
        if self.log["overflowed"]:
            self.log["reads_after_overflow"] += 1
        self.log["requests"].append(size)
        data = self.inner.read(size if self.short is None else min(size, self.short))
        self.log["returned"].append(len(data))
        if self.after_read is not None:
            self.after_read(len(self.log["returned"]))
        return data

    def close(self):
        self.closed_calls += 1
        self.inner.close()

    @property
    def closed(self):
        return self.inner.closed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class CopyBoundsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-copy-bounds-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.sources = self.root / "content" / "sources.json"
        self.fds, self.stacks = [], []
        RecordingDigest.created = []

    # -- helpers ------------------------------------------------------------------------------------------------
    def write_library(self, document):
        self.sources.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def download(self, data, name="downloaded.pdf"):
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def snapshot(self):
        info = self.sources.stat()
        return tree(self.root), info.st_mode, info.st_mtime_ns

    def grower(self, path, extra):
        """A real-fstat wrapper that, once, appends ``extra`` bytes to ``path`` right after the genuine fstat."""
        real, state = os.fstat, {"done": False}

        def fstat(fd):
            info = real(fd)
            if not state["done"]:
                state["done"] = True
                with open(path, "ab") as out:
                    out.write(extra)
            return info
        return mock.patch.object(sources_cmd.os, "fstat", fstat)

    def recording(self, short=None, after_read=None):
        """Patch the descriptor reader and the digest so reads and hashed bytes are recorded, all forwarded for real."""
        log = {"requests": [], "returned": [], "overflowed": False, "reads_after_overflow": 0, "handles": []}
        real_fdopen, real_open = os.fdopen, os.open

        def fdopen(fd, mode="r", *args, **kwargs):
            inner = real_fdopen(fd, mode, *args, **kwargs)
            if mode != "rb":
                return inner
            handle = RecordingHandle(inner, log, short, after_read)
            log["handles"].append(handle)
            return handle

        def opener(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            self.fds.append(fd)
            return fd
        namespace = mock.Mock(sha256=RecordingDigest)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(sources_cmd.os, "fdopen", fdopen))
        stack.enter_context(mock.patch.object(sources_cmd.os, "open", opener))
        stack.enter_context(mock.patch.object(sources_cmd, "hashlib", namespace))
        self.stacks.append(stack)
        self.addCleanup(stack.close)
        return log

    def stop_recording(self):
        while self.stacks:
            self.stacks.pop().close()

    def assert_closed(self):
        for fd in self.fds:
            with self.assertRaises(OSError) as caught:
                os.fstat(fd)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def verify(self, file, sha=A, reason=None, fragment=None):
        return verify_copy(self.root, sha, str(file), "2026-02-01", reason, today=TODAY, fragment=fragment)

    # -- boundary -----------------------------------------------------------------------------------------------
    def test_copy_budget_empty_minus_one_exact_and_plus_one(self):
        self.assertEqual(sources_cmd.COPY_MAX, 209715200)
        for cap in (0, 5, MIB - 1, MIB, MIB + 3):
            with mock.patch.object(sources_cmd, "COPY_MAX", cap):
                for size in sorted({0, max(cap - 1, 0), cap}):
                    with self.subTest(cap=cap, size=size):
                        data = bytes(range(256)) * (size // 256 + 1)
                        data = data[:size]
                        self.assertEqual(sources_cmd._hash_copy(str(self.download(data))), digest_of(data))
                with self.subTest(cap=cap, size="cap+1"):
                    file = self.download(b"x" * (cap + 1))
                    with mock.patch.object(sources_cmd.hashlib, "sha256", side_effect=AssertionError("hashed")):
                        with self.assertRaisesRegex(ValueError, "larger than .* MiB; nothing hashed"):
                            sources_cmd._hash_copy(str(file))

    # -- growth after the real fstat ----------------------------------------------------------------------------
    def test_growth_after_real_fstat_refuses_before_attestation(self):
        cap, extra = len(RECORD) + 10, SUFFIX * 3
        copy = self.download(RECORD)
        grown = RECORD + extra
        self.assertGreater(len(grown), cap)
        self.write_library([entry(digest_of(grown), "cedar-grown"), entry(A, "cedar-original", url=U2)])
        self.sources.chmod(0o640)
        before = self.snapshot()
        for sha, reason in ((digest_of(grown), None), (A, "redacted"), (A, "converted"), (A, "unknown"),
                            (digest_of(grown), "unknown")):
            with self.subTest(sha=sha[:8], reason=reason):
                copy.write_bytes(RECORD)
                before_copy = copy.read_bytes()
                with mock.patch.object(sources_cmd, "COPY_MAX", cap), self.grower(copy, extra), \
                        mock.patch.object(sources_cmd, "_rewrite", side_effect=AssertionError("rewrote")):
                    with self.assertRaisesRegex(ValueError, r"--file: larger than 0 MiB; verification not recorded"):
                        self.verify(copy, sha=sha[:12], reason=reason)
                self.assertEqual(copy.read_bytes(), before_copy + extra)  # the caller's file is not touched by the tool
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.sources.parent.glob(".sources-*")), [])

    def assert_stops_at_cap_plus_one(self, cap, copy, log):
        digest = RecordingDigest.created[0]
        self.assertTrue(log["requests"] and all(1 <= n <= MIB for n in log["requests"]))
        consumed = 0
        for requested, returned in zip(log["requests"], log["returned"]):
            self.assertLessEqual(requested, cap + 1 - consumed)  # never asks for more than one byte past the limit
            consumed += returned
        self.assertEqual(consumed, cap + 1)  # it really read through to the overflow, and no further
        self.assertLessEqual(sum(digest.sizes), cap)
        self.assertFalse(digest.finished)  # no digest is ever produced
        self.assertEqual(log["reads_after_overflow"], 0)
        self.assert_closed()
        self.assertTrue(all(handle.closed for handle in log["handles"]))

    def test_stream_stops_at_cap_plus_one_without_hashing_overflow(self):
        cap = 2 * MIB + 5
        with self.subTest("grown right after the real fstat"):
            copy = self.download(b"a" * 100, "after-fstat.pdf")
            log = self.recording()
            with mock.patch.object(sources_cmd, "COPY_MAX", cap), self.grower(copy, b"b" * (4 * MIB)):
                with self.assertRaisesRegex(ValueError, "verification not recorded"):
                    sources_cmd._hash_copy(str(copy))
            self.assert_stops_at_cap_plus_one(cap, copy, log)
        self.stop_recording()
        RecordingDigest.created, self.fds = [], []
        with self.subTest("grown between chunks"):
            copy = self.download(b"a" * 100, "between-chunks.pdf")

            def grow(count):
                if count == 1:
                    with open(copy, "ab") as out:
                        out.write(b"c" * (10 * MIB))
            log = self.recording(after_read=grow)
            with mock.patch.object(sources_cmd, "COPY_MAX", cap):
                with self.assertRaisesRegex(ValueError, "verification not recorded"):
                    sources_cmd._hash_copy(str(copy))
            self.assert_stops_at_cap_plus_one(cap, copy, log)

    def test_short_reads_exact_cap_eof_and_legal_growth(self):
        cap = 40
        with mock.patch.object(sources_cmd, "COPY_MAX", cap):
            exact = bytes(range(cap))
            log = self.recording(short=7)
            self.assertEqual(sources_cmd._hash_copy(str(self.download(exact))), digest_of(exact))
            self.assertTrue(all(1 <= n <= MIB for n in log["requests"]))
            self.assertEqual(sum(log["returned"]), cap)
            self.assertEqual(log["returned"][-1], 0)  # true EOF, not a short read, ends the stream
            self.assertGreater(len(log["returned"]), 2)
            self.stop_recording()

            small = self.download(b"abc", "grows-legally.pdf")
            with self.grower(small, b"defg"):
                self.assertEqual(sources_cmd._hash_copy(str(small)), digest_of(b"abcdefg"))  # legal growth is hashed

            empty = self.download(b"", "empty.pdf")
            self.assertEqual(sources_cmd._hash_copy(str(empty)), digest_of(b""))
            with self.grower(empty, b"z" * (cap + 1)):
                with self.assertRaisesRegex(ValueError, "verification not recorded"):
                    sources_cmd._hash_copy(str(empty))

            over = self.download(b"q" * 10, "over-short.pdf")
            self.recording(short=3)
            with self.grower(over, b"q" * cap):
                with self.assertRaisesRegex(ValueError, "verification not recorded"):
                    sources_cmd._hash_copy(str(over))
            self.assertFalse(RecordingDigest.created[-1].finished)

    def test_stream_resources_close_on_overflow_read_failure_and_success(self):
        cap = 20
        with mock.patch.object(sources_cmd, "COPY_MAX", cap):
            self.write_library([entry(A, "cedar-one")])
            before = self.snapshot()
            log = self.recording()
            copy = self.download(b"w" * 30)  # initial rejection
            with self.assertRaisesRegex(ValueError, "nothing hashed"):
                sources_cmd._hash_copy(str(copy))
            ok = self.download(b"v" * 10, "ok.pdf")  # accepted
            sources_cmd._hash_copy(str(ok))
            late = self.download(b"u" * 10, "late.pdf")  # late overflow
            with self.grower(late, b"u" * 30):
                with self.assertRaisesRegex(ValueError, "verification not recorded"):
                    sources_cmd._hash_copy(str(late))
            self.assertEqual(len(self.fds), 3)
            self.assert_closed()
            self.assertEqual([h.closed_calls for h in log["handles"]], [1, 1])
            self.assertTrue(all(h.closed for h in log["handles"]))
            self.stop_recording()

            # a mid-read failure propagates unchanged and records nothing
            failing = self.download(b"t" * 10, "failing.pdf")
            real_fdopen, self.fds = os.fdopen, []

            class Broken(RecordingHandle):
                def read(self, size=-1):
                    raise OSError(errno.EIO, "synthetic read failure")

            def fdopen(fd, mode="r", *args, **kwargs):
                inner = real_fdopen(fd, mode, *args, **kwargs)
                return Broken(inner, {"requests": [], "returned": [], "overflowed": False, "reads_after_overflow": 0}) \
                    if mode == "rb" else inner
            seen = []
            with mock.patch.object(sources_cmd.os, "fdopen", lambda *a, **k: seen.append(fdopen(*a, **k)) or seen[-1]):
                with mock.patch.object(sources_cmd, "_rewrite", side_effect=AssertionError("rewrote")):
                    with self.assertRaises(OSError) as caught:
                        self.verify(failing, sha=A)
            self.assertEqual(caught.exception.errno, errno.EIO)
            self.assertTrue(seen[0].closed)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.sources.parent.glob(".sources-*")), [])

    # -- CLI ----------------------------------------------------------------------------------------------------
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        argv = ["campaign_tool", "sources", "verify-copy", "--directory", str(self.root), *args]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exit_:
                return exit_.code, out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def test_cli_growth_refusal_has_exit_three_no_json_and_no_private_values(self):
        copy = self.download(RECORD, f"{SENTINEL}.pdf")
        grown = RECORD + SUFFIX * 4
        self.write_library([entry(digest_of(grown), "cedar-grown", url=f"https://example.invalid/{SENTINEL}")])
        before = self.snapshot()
        with mock.patch.object(sources_cmd, "COPY_MAX", len(RECORD) + 5), self.grower(copy, SUFFIX * 4), \
                mock.patch.object(sources_cmd, "_rewrite", side_effect=AssertionError("rewrote")):
            code, out, err = self.run_cli("--sha256", digest_of(grown)[:12], "--file", str(copy),
                                          "--checked-on", "2026-02-01")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertEqual(err, "Stopped: --file: larger than 0 MiB; verification not recorded\n")
        for private in (SENTINEL, str(self.tmp), digest_of(grown), "Traceback", SUFFIX.decode()):
            self.assertNotIn(private, err)
        self.assertEqual(self.snapshot(), before)

    def test_cli_initial_oversize_sparse_file_in_subprocess(self):
        self.write_library([entry(A, "cedar-one")])
        big = self.tmp / "sparse.pdf"
        with open(big, "wb") as out:
            out.truncate(sources_cmd.COPY_MAX + 1)
        before = self.snapshot()
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "sources", "verify-copy", "--directory",
                               str(self.root), "--sha256", A[:12], "--file", str(big), "--checked-on", "2026-02-01"],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 3)
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.stderr, "Stopped: --file: larger than 200 MiB; nothing hashed\n")
        self.assertEqual(self.snapshot(), before)

    # -- preservation -------------------------------------------------------------------------------------------
    def test_url_groups_and_existing_validation_precedence_survive_budget_fix(self):
        match, differ = {"matches": True, "checked_on": "2026-01-25"}, \
            {"matches": False, "checked_on": "2026-01-26", "reason": "redacted"}
        self.write_library([entry(A, "cedar-one", url=U1, public_copy=match), entry(A, "cedar-alias", url=U1,
                            public_copy=match), entry(A, "cedar-two", url=U2, public_copy=differ)])
        before = self.snapshot()
        cap, copy = len(RECORD) + 5, self.download(RECORD)
        with mock.patch.object(sources_cmd, "COPY_MAX", cap):
            with self.grower(copy, SUFFIX), mock.patch.object(sources_cmd, "_rewrite", side_effect=AssertionError()):
                with self.assertRaisesRegex(ValueError, "verification not recorded"):
                    self.verify(copy, sha=A[:12], fragment="cedar-one")
            copy.write_bytes(RECORD)  # the tool never restores the caller's grown file; the test does
            self.assertEqual(self.snapshot(), before)
        # selection and date/reason validation still refuse before the download is opened
        with mock.patch.object(sources_cmd.os, "open", side_effect=AssertionError("opened")):
            for call, message in (
                    (lambda: self.verify(copy, sha=A[:12]), "multiple public URLs"),
                    (lambda: self.verify(copy, sha="0" * 12, fragment="cedar-one"), "no library entry starts with"),
                    (lambda: self.verify(copy, sha=A[:12], fragment="cedar-none"), "no library entry for this record"),
                    (lambda: self.verify(copy, sha=A[:12], fragment="cedar-one", reason="photocopied"),
                     "--differs-because: one of"),
                    (lambda: verify_copy(self.root, A[:12], str(copy), "2026-02-30", today=TODAY, fragment="cedar-one"),
                     "a real YYYY-MM-DD date")):
                with self.assertRaisesRegex(ValueError, message):
                    call()
        self.assertEqual(self.snapshot(), before)
        # ordinary under-cap copies keep the exact result and touch only the selected URL group
        result = self.verify(copy, sha=A[:12], fragment="cedar-one")
        self.assertEqual(result, {"sha256_prefix": A[:12], "matches": True, "checked_on": "2026-02-01"})
        checks = [item.get("public_copy") for item in json.loads(self.sources.read_text(encoding="utf-8"))]
        self.assertEqual(checks, [{"matches": True, "checked_on": "2026-02-01"}] * 2 + [differ])
        other = self.download(b"synthetic different copy\n", "other.pdf")
        result = self.verify(other, sha=A[:12], reason="converted", fragment="cedar-two")
        self.assertEqual(result, {"sha256_prefix": A[:12], "matches": False, "checked_on": "2026-02-01",
                                  "reason": "converted", "copy_sha256_prefix": digest_of(b"synthetic different copy\n")[:12]})


if __name__ == "__main__":
    unittest.main()
