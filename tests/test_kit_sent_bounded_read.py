"""kit_track.read_sent bounds the bytes it acquires, not only the size it first observed. Offline, synthetic."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_track  # noqa: E402
from tests import test_kit_sent  # noqa: E402

CAP = kit_track.MAX_BYTES
ENVELOPE = b'{"schema_version": 1, "requests": {}}'
REFUSAL = "kit/sent.json unreadable: larger than 1 MiB"


def padded(size):
    return ENVELOPE + b" " * (size - len(ENVELOPE))


class BoundedReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sent-bound-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        code, _, err = test_kit_sent.run("init", "--directory", self.root, "--name", "Synthetic",
                                         "--county", "San Luis Obispo", "--state", "CA")
        self.assertEqual(code, 0, err)
        (self.root / "kit").mkdir(exist_ok=True)
        self.sent = self.root / "kit" / "sent.json"

    def grow_after_size_check(self, grown):
        """Grow the real file right after read_sent's own size observation (not the is_file one)."""
        real_stat = Path.stat
        sent = self.sent

        def stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == sent and sys._getframe(1).f_code.co_name == "read_sent":
                sent.write_bytes(grown)
            return result
        return mock.patch.object(Path, "stat", stat)

    def test_post_stat_growth_refuses_before_decode(self):
        for size in (CAP + 1, 3 * CAP):
            with self.subTest(size=size):
                self.sent.write_bytes(ENVELOPE)
                with self.grow_after_size_check(padded(size)), \
                        mock.patch.object(kit_track.json, "loads", side_effect=AssertionError("parsed")):
                    with self.assertRaises(ValueError) as caught:
                        kit_track.read_sent(self.root)
                self.assertEqual(str(caught.exception), REFUSAL)

    def test_payload_read_requests_only_cap_plus_one(self):
        self.sent.write_bytes(ENVELOPE)
        requests, handles = [], []
        real_open = Path.open

        def spying_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if path != self.sent:
                return handle
            handles.append(handle)
            real_read = handle.read

            def read(size=-1):
                data = real_read(size)
                requests.append((size, len(data)))
                return data
            return mock.Mock(wraps=handle, read=read, __enter__=lambda s: s, __exit__=lambda s, *a: handle.__exit__(*a))
        with self.grow_after_size_check(padded(3 * CAP)), mock.patch.object(Path, "open", spying_open), \
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError):
            with self.assertRaises(ValueError) as caught:
                kit_track.read_sent(self.root)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertEqual(requests, [(CAP + 1, CAP + 1)])
        self.assertTrue(handles[0].closed)

    def test_exact_byte_ceiling_static_and_grown(self):
        for size, ok in ((CAP - 1, True), (CAP, True), (CAP + 1, False)):
            for grown in (False, True):
                with self.subTest(size=size, grown=grown):
                    self.sent.write_bytes(ENVELOPE if grown else padded(size))
                    context = self.grow_after_size_check(padded(size)) if grown else mock.MagicMock()
                    with context:
                        if ok:
                            self.assertEqual(kit_track.read_sent(self.root), {})
                        else:
                            with self.assertRaisesRegex(ValueError, "larger than 1 MiB"):
                                kit_track.read_sent(self.root)

    def test_existing_bad_store_contracts_unchanged(self):
        self.assertEqual(kit_track.read_sent(self.root), {})
        self.assertFalse(self.sent.exists())
        cases = {b"": "not JSON", b"\xff\xfe": "not JSON", b"{": "not JSON",
                 b'{"schema_version": 2, "requests": {}}': "expected schema_version 1"}
        for data, words in cases.items():
            with self.subTest(data=data):
                self.sent.write_bytes(data)
                with self.assertRaisesRegex(ValueError, words):
                    kit_track.read_sent(self.root)
                self.assertEqual(self.sent.read_bytes(), data)
        self.sent.unlink()
        self.sent.mkdir()
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            kit_track.read_sent(self.root)

    def test_stream_closes_on_read_error(self):
        self.sent.write_bytes(ENVELOPE)
        seen = []
        real_open = Path.open

        def failing_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if path == self.sent:
                seen.append(handle)
                handle.read = mock.Mock(side_effect=OSError("synthetic read failure"))
            return handle
        with mock.patch.object(Path, "open", failing_open):
            with self.assertRaisesRegex(OSError, "synthetic read failure"):
                kit_track.read_sent(self.root)
        self.assertTrue(seen[0].closed)

    def test_real_cli_status_refuses_growth_without_success_output(self):
        self.sent.write_bytes(ENVELOPE)
        code, out, err = test_kit_sent.run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["requests"], [])
        with self.grow_after_size_check(padded(CAP + 1)):
            code, out, err = test_kit_sent.run("kit-status", "--directory", self.root)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn("larger than 1 MiB", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
