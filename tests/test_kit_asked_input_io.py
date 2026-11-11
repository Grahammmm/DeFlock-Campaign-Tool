"""RF0211-1: kit-asked bounds its original agencies.json input before parsing (synthetic only).

The real 2 MiB ceiling is used. Reads are observed on the actual opened binary stream through a forwarding spy, so the
evidence is bytes requested/returned by the real file, never a mocked size.
"""
import contextlib
import errno
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_asked  # noqa: E402
from campaign_tool.content import MAX_JSON_BYTES, ContentError  # noqa: E402
from tests.test_kit_asked import EXAMPLE, POLICE, TODAY, A, B, run  # noqa: E402

TOO_LARGE = "agencies.json: file too large"
NOT_REGULAR = "agencies.json: must be a regular file"
SOURCE_CAP = 2 * 1024 * 1024  # independent of the production constant


class Spy:
    """Forwards every call to the real stream and records the read() arguments and returned sizes."""

    def __init__(self, stream, log):
        self._stream, self._log = stream, log

    def read(self, *args):
        data = self._stream.read(*args)
        self._log.append((args, len(data)))
        return data

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *exc):
        return self._stream.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._stream, name)


class Hang(Exception):
    pass


class InputIoBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-asked-io-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.path = self.root / "content" / "agencies.json"
        self.sent_path = self.root / "kit" / "sent.json"
        self.agencies = json.loads(self.path.read_text(encoding="utf-8"))
        self.base = (json.dumps(self.agencies, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.path.write_bytes(self.base)
        request = {"sent_on": "2026-03-02", "channel": "email", "draft": f"requests/{POLICE}.md",
                   "draft_sha256": "a" * 64, "recorded_at": "2026-03-02T10:00:00+00:00", "steps": [],
                   "scopes": [A, B]}
        self.sent_path.parent.mkdir(exist_ok=True)
        self.sent_path.write_text(json.dumps({"schema_version": 1, "requests": {POLICE: request}}), encoding="utf-8")
        self.reads = []
        self.addCleanup(signal.alarm, 0)

    def padded(self, size):
        self.assertLessEqual(len(self.base), size)
        data = self.base + b" " * (size - len(self.base))
        self.path.write_bytes(data)
        return data

    def snapshot(self):
        stat = self.path.stat()
        return (self.path.read_bytes(), stat.st_mode, stat.st_ino, stat.st_mtime_ns,
                sorted(p.name for p in self.path.parent.iterdir()), self.sent_path.read_bytes())

    def spying(self):
        real = os.fdopen

        def fdopen(fd, mode="r", *args, **kwargs):
            stream = real(fd, mode, *args, **kwargs)
            return Spy(stream, self.reads) if mode == "rb" else stream
        return mock.patch.object(kit_asked.os, "fdopen", fdopen)

    def forbid_downstream(self):
        """Parser and private-history access are tripwires once input is refused."""
        for target in ("json.loads", "campaign_tool.kit_asked.kit_track.read_sent", "campaign_tool.kit_asked._titles",
                       "campaign_tool.kit_asked.kit._write_json", "campaign_tool.kit_asked.os.chmod"):
            patch = mock.patch(target, side_effect=AssertionError(f"{target} reached"))
            patch.start()
            self.addCleanup(patch.stop)


class KnownOversizeTests(InputIoBase):
    def test_known_oversize_never_reads_or_projects(self):
        for size in (SOURCE_CAP + 1, 8 * 1024 * 1024):
            with self.subTest(size=size):
                self.padded(size)
                before = self.snapshot()
                self.reads.clear()
                with self.spying(), mock.patch("campaign_tool.kit_asked.kit_track.read_sent",
                                               side_effect=AssertionError("history")), \
                        mock.patch.object(kit_asked.kit, "_write_json", side_effect=AssertionError("written")), \
                        mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")), \
                        mock.patch.object(kit_asked.json, "loads", side_effect=AssertionError("parsed")):
                    with self.assertRaises(ContentError) as caught:
                        kit_asked.propose(self.root, TODAY)
                    self.assertEqual(str(caught.exception), TOO_LARGE)
                    for apply in (False, True):
                        with contextlib.redirect_stderr(io.StringIO()) as quiet:
                            self.assertEqual(kit_asked.run_cli(self.root, apply=apply, today=TODAY), 3)
                        self.assertEqual(quiet.getvalue(), f"Stopped: {TOO_LARGE}\n")
                    for args in (("kit-asked", "--directory", self.root), ("kit-asked", "--directory", self.root, "--apply")):
                        code, out, err = run(*args)
                        self.assertEqual((code, out, err), (3, "", f"Stopped: {TOO_LARGE}\n"))
                self.assertEqual(self.reads, [])
                self.assertEqual(self.snapshot(), before)

    def test_real_cli_subprocess_refuses_with_same_contract(self):
        self.padded(8 * 1024 * 1024)
        before = self.snapshot()
        for extra in ((), ("--apply",)):
            done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit-asked", "--directory",
                                   str(self.root), *extra], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (3, "", f"Stopped: {TOO_LARGE}\n"))
        self.assertEqual(self.snapshot(), before)


class ConsumptionTests(InputIoBase):
    def test_actual_consumption_is_at_most_cap_plus_one(self):
        for size in (len(self.base), SOURCE_CAP - 1, SOURCE_CAP):
            with self.subTest(size=size):
                self.padded(size)
                self.reads.clear()
                with self.spying():
                    result = kit_asked.propose(self.root, TODAY)
                self.assertTrue(result["changed"])
                self.assertEqual(self.reads, [((SOURCE_CAP + 1,), size)])

    def test_growth_after_fstat_is_still_bounded(self):
        self.padded(SOURCE_CAP)
        real = os.fstat
        grown = []

        def fstat(fd):
            info = real(fd)
            if not grown:  # first fstat is the admission check on the opened descriptor
                with open(self.path, "ab") as handle:
                    handle.write(b" " * (3 * 1024 * 1024))
                grown.append(info.st_size)
            return info
        with self.spying(), mock.patch.object(kit_asked.os, "fstat", fstat), \
                mock.patch.object(kit_asked.json, "loads", side_effect=AssertionError("parsed")), \
                mock.patch("campaign_tool.kit_asked.kit_track.read_sent", side_effect=AssertionError("history")):
            with self.assertRaises(ContentError) as caught:
                kit_asked.propose(self.root, TODAY)
        self.assertEqual(str(caught.exception), TOO_LARGE)
        self.assertEqual(grown, [SOURCE_CAP])
        self.assertEqual(self.reads, [((SOURCE_CAP + 1,), SOURCE_CAP + 1)])

    def test_shrinking_and_truncated_inputs_follow_ordinary_parsing(self):
        real = os.fstat

        def fstat(fd):
            info = real(fd)
            self.path.write_bytes(self.base)  # shrinks in place after admission
            return info
        self.padded(SOURCE_CAP)
        with mock.patch.object(kit_asked.os, "fstat", fstat):
            self.assertTrue(kit_asked.propose(self.root, TODAY)["changed"])
        self.path.write_bytes(self.base[: len(self.base) // 2])
        with self.assertRaises(ContentError) as caught:
            kit_asked.propose(self.root, TODAY)
        self.assertTrue(str(caught.exception).startswith("agencies.json: not valid UTF-8 JSON ("))


class ByteBoundaryTests(InputIoBase):
    def test_cap_minus_one_exact_cap_and_plus_one_count_bytes(self):
        for size, ok in ((SOURCE_CAP - 1, True), (SOURCE_CAP, True), (SOURCE_CAP + 1, False)):
            with self.subTest(size=size):
                self.padded(size)
                if ok:
                    self.assertTrue(kit_asked.propose(self.root, TODAY)["changed"])
                else:
                    with self.assertRaises(ContentError) as caught:
                        kit_asked.propose(self.root, TODAY)
                    self.assertEqual(str(caught.exception), TOO_LARGE)

    def test_multibyte_text_counts_bytes_and_split_sequence_loses_to_size(self):
        self.agencies[0]["name"] = "é" * 40
        base = (json.dumps(self.agencies, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.assertGreater(len(base), len(base.decode("utf-8")))
        self.path.write_bytes(base + b" " * (SOURCE_CAP - len(base)))
        self.assertTrue(kit_asked.propose(self.root, TODAY)["changed"])
        # the overflow byte is the first half of a two-byte scalar: size wins over strict decoding
        self.path.write_bytes(base + b" " * (SOURCE_CAP - len(base)) + b"\xc3")
        with self.assertRaises(ContentError) as caught:
            kit_asked.propose(self.root, TODAY)
        self.assertEqual(str(caught.exception), TOO_LARGE)
        # a split sequence within the cap is an ordinary decode failure
        self.path.write_bytes(base + b" " * (SOURCE_CAP - len(base) - 1) + b"\xc3")
        with self.assertRaises(ContentError) as caught:
            kit_asked.propose(self.root, TODAY)
        self.assertTrue(str(caught.exception).startswith("agencies.json: not valid UTF-8 JSON ("))

    def test_list_shape_is_independent_of_size(self):
        self.path.write_bytes(b"{}" + b" " * (SOURCE_CAP - 2))
        with self.assertRaises(ContentError) as caught:
            kit_asked.propose(self.root, TODAY)
        self.assertEqual(str(caught.exception), "content/agencies.json: must be a list of agencies")


class DescriptorTypeTests(InputIoBase):
    def alarm(self):
        def handler(*_):
            raise Hang("blocked on a nonregular input")
        previous = signal.signal(signal.SIGALRM, handler)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        signal.alarm(10)

    def test_fifo_substituted_after_precheck_refuses_without_waiting(self):
        self.alarm()
        real = os.open

        def swap(path, *args, **kwargs):
            if Path(path) == self.path:
                os.unlink(self.path)
                os.mkfifo(self.path)
            return real(path, *args, **kwargs)
        with mock.patch.object(kit_asked.os, "open", swap), \
                mock.patch("campaign_tool.kit_asked.kit_track.read_sent", side_effect=AssertionError("history")):
            with self.assertRaises(ContentError) as caught:
                kit_asked.propose(self.root, TODAY)
            self.assertEqual(str(caught.exception), NOT_REGULAR)
            os.unlink(self.path)
            self.path.write_bytes(self.base)
            code, out, err = run("kit-asked", "--directory", self.root)
        self.assertEqual((code, out, err), (3, "", f"Stopped: {NOT_REGULAR}\n"))

    def test_directory_and_symlinks_are_refused_and_hardlink_is_read(self):
        directory = self.root / "elsewhere" / "agencies.json"
        directory.mkdir(parents=True)
        with self.assertRaises(ContentError) as caught:
            kit_asked._read_agencies(directory)
        self.assertEqual(str(caught.exception), NOT_REGULAR)
        link = self.root / "content" / "link.json"
        for target in (self.path, self.root / "content" / "dangling.json"):
            link.unlink(missing_ok=True)
            os.symlink(target, link)
            with self.assertRaises(ContentError) as caught:
                kit_asked._read_agencies(link)
            self.assertEqual(str(caught.exception), "link.json: symlinks are not read")
        hard = self.root / "content" / "hard.json"
        os.link(self.path, hard)
        self.assertEqual(kit_asked._read_agencies(hard), self.agencies)
        self.path.unlink()
        os.symlink(self.root / "content" / "missing.json", self.path)
        code, out, err = run("kit-asked", "--directory", self.root)
        self.assertEqual((code, out, err), (3, "", "Stopped: agencies.json: symlinks are not read\n"))


class OwnershipTests(InputIoBase):
    def traced_open(self):
        opened, closed = [], []
        real_open, real_close = os.open, os.close

        def traced(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if Path(path) == self.path:
                opened.append(fd)
            return fd

        def close(fd):
            closed.append(fd)
            return real_close(fd)
        return opened, closed, mock.patch.object(kit_asked.os, "open", traced), \
            mock.patch.object(kit_asked.os, "close", close)

    def assert_released(self, fd):
        with self.assertRaises(OSError) as caught:
            os.fstat(fd)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_opened_identity_survives_name_replacement(self):
        other = self.root / "content" / "other.json"
        other.write_text("[]", encoding="utf-8")
        real = os.fstat

        def fstat(fd):
            info = real(fd)
            os.replace(other, self.path)  # a different file now owns the name
            return info
        with mock.patch.object(kit_asked.os, "fstat", fstat):
            self.assertEqual(kit_asked._read_agencies(self.path), self.agencies)
        self.assertEqual(self.path.read_bytes(), b"[]")

    def test_every_failure_releases_exactly_the_owned_descriptor(self):
        sentinel = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, sentinel)
        real_fstat, real_fdopen = os.fstat, os.fdopen
        cases = {
            "fstat": (mock.patch.object(kit_asked.os, "fstat", side_effect=OSError(errno.EIO, "fstat")), OSError, 1),
            "fdopen": (mock.patch.object(kit_asked.os, "fdopen", side_effect=MemoryError()), MemoryError, 1),
            "type": (mock.patch.object(kit_asked.stat, "S_ISREG", return_value=False), ContentError, 1),
            "size": (mock.patch.object(kit_asked.os, "fstat",
                                       lambda fd: mock.Mock(st_mode=real_fstat(fd).st_mode, st_size=SOURCE_CAP + 1)),
                     ContentError, 1),
            "read": (mock.patch.object(kit_asked.os, "fdopen", lambda fd, mode: mock.Mock(
                read=mock.Mock(side_effect=OSError(errno.EIO, "read")), __enter__=lambda s: s,
                __exit__=lambda s, *e: (real_fdopen(fd, mode).close(), None)[1])), OSError, 0),
            "decode": (mock.patch.object(kit_asked.json, "loads", side_effect=RuntimeError("parser")), RuntimeError, 0),
        }
        for name, (patch, expected, closes) in cases.items():
            with self.subTest(name):
                opened, closed, patch_open, patch_close = self.traced_open()
                with patch_open, patch_close, patch:
                    with self.assertRaises(expected):
                        kit_asked._read_agencies(self.path)
                self.assertEqual(len(opened), 1)
                self.assertLessEqual(closed.count(opened[0]), closes)  # never a double close
                if name != "read":
                    self.assert_released(opened[0])
                os.fstat(sentinel)  # unrelated descriptor untouched

    def test_stream_failures_close_the_real_descriptor(self):
        opened, closed, patch_open, patch_close = self.traced_open()
        real = os.fdopen

        def fdopen(fd, mode):
            stream = real(fd, mode)
            stream.read = mock.Mock(side_effect=MemoryError())
            return Spy(stream, [])
        with patch_open, patch_close, mock.patch.object(kit_asked.os, "fdopen", fdopen):
            with self.assertRaises(MemoryError):
                kit_asked._read_agencies(self.path)
        self.assert_released(opened[0])

    def test_json_and_decode_errors_keep_the_established_diagnostic(self):
        for data, start in ((b"\xff\xfe[]", "agencies.json: not valid UTF-8 JSON ('utf-8' codec"),
                            (b"[1,", "agencies.json: not valid UTF-8 JSON (Expecting")):
            self.path.write_bytes(data)
            with self.assertRaises(ContentError) as caught:
                kit_asked._read_agencies(self.path)
            self.assertTrue(str(caught.exception).startswith(start), str(caught.exception))


class CompatibilityTests(InputIoBase):
    def test_healthy_apply_writes_exact_bytes_once_and_repeat_is_noop(self):
        result = kit_asked.propose(self.root, TODAY)
        expected = (json.dumps(result["document"], indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.path.chmod(0o640)
        code, out, err = run("kit-asked", "--directory", self.root, "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        before = self.snapshot()
        code, out, err = run("kit-asked", "--directory", self.root, "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Nothing written.", out)
        self.assertEqual(self.snapshot(), before)

    def test_absent_input_and_private_history_keep_their_order(self):
        self.path.unlink()
        with self.assertRaises(ContentError) as caught:
            kit_asked.propose(self.root, TODAY)
        self.assertEqual(str(caught.exception), "content/agencies.json not found; nothing to propose")
        self.path.write_bytes(self.base)
        self.sent_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            kit_asked.propose(self.root, TODAY)
        self.assertIn("kit/sent.json unreadable", str(caught.exception))

    def test_output_cap_patches_do_not_rebind_input_cap(self):
        with mock.patch.object(kit_asked, "MAX_JSON_BYTES", 1):
            result = kit_asked.propose(self.root, TODAY)  # input admitted; the output gate fires later
            self.assertIn("proposed output would exceed 1 bytes", result["error"])
        with mock.patch.object(kit_asked, "MAX_JSON_BYTES", 10 * SOURCE_CAP):
            self.padded(SOURCE_CAP + 1)  # a raised output cap does not hide an over-real-input-cap file
            with self.assertRaises(ContentError) as caught:
                kit_asked.propose(self.root, TODAY)
            self.assertEqual(str(caught.exception), TOO_LARGE)
        self.assertEqual(MAX_JSON_BYTES, SOURCE_CAP)

    def test_oversize_refuses_even_when_nothing_would_change(self):
        self.sent_path.unlink()
        self.padded(SOURCE_CAP + 1)
        with self.assertRaises(ContentError) as caught:
            kit_asked.propose(self.root, TODAY)
        self.assertEqual(str(caught.exception), TOO_LARGE)


if __name__ == "__main__":
    unittest.main()
