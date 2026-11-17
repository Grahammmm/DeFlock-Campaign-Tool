"""RF1210-14: locate_online bounds one geocoder response to 1 MiB before decoding.

Synthetic in-memory streams only; the opener seam is the only thing replaced; no network.
"""
import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, discovery  # noqa: E402
from tests import test_kit as _kit  # noqa: E402

CAP = discovery.MAX_GEOCODER_BYTES
REFUSED = "Census geocoder lookup failed: response exceeds 1 MiB"
PAYLOAD = {"result": {"addressMatches": [{"geographies": {
    "Counties": [{"GEOID": "06079", "NAME": "San Luis Obispo County"}],
    "Incorporated Places": [{"GEOID": "0648945", "NAME": "Morro Bay city", "BASENAME": "Morro Bay"}]}}]}}


class Stream(io.BytesIO):
    """Records every read size; optionally serves short fragments or fails midstream."""

    def __init__(self, data, chunks=None, fail_after=None):
        super().__init__(data)
        self.sizes = []
        self.chunks = list(chunks or [])
        self.fail_after = fail_after
        self.exits = 0
        self.consumed = 0

    def read(self, size=-1):
        self.sizes.append(size)
        if size is None or size < 0:
            raise AssertionError("unbounded read")
        if self.fail_after is not None and self.tell() >= self.fail_after:
            raise OSError("midstream reset")
        if self.chunks:
            size = min(size, self.chunks.pop(0))
        data = super().read(size)
        self.consumed += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exits += 1
        self.close()
        return False


def body(total, payload=PAYLOAD):
    raw = json.dumps(payload).encode()
    return raw + b" " * (total - len(raw))


def locate(stream):
    opener = mock.Mock(return_value=stream)
    try:
        return discovery.locate_online("Morro Bay", "CA", opener=opener), opener
    except ValueError as exc:
        return exc, opener


class Bounds(unittest.TestCase):
    def test_real_response_cap_before_decode(self):
        for total in (CAP - 1, CAP):
            stream = Stream(body(total))
            found, opener = locate(stream)
            self.assertEqual((found.county_fips, found.place_name), ("06079", "Morro Bay"), total)
            self.assertTrue(all(0 < s <= 65536 for s in stream.sizes), total)
            opener.assert_called_once()
        for total in (CAP + 1, 5 * CAP):
            stream = Stream(body(total))
            with mock.patch.object(discovery.json, "loads") as loads, \
                    mock.patch.object(discovery, "_select_geocoder_entries") as select:
                err, _ = locate(stream)
            self.assertIsInstance(err, ValueError)
            self.assertEqual(str(err), REFUSED)
            loads.assert_not_called()
            select.assert_not_called()
            self.assertTrue(all(0 < s <= 65536 for s in stream.sizes))
            self.assertLessEqual(stream.consumed, CAP + 1)
            self.assertTrue(stream.closed)

    def test_short_reads_and_eof(self):
        raw = json.dumps(PAYLOAD, ensure_ascii=False).encode()
        stream = Stream(raw, chunks=[1, 2, 3, 5, 7] * 40)
        found, _ = locate(stream)
        self.assertEqual(found.county_fips, "06079")
        payload = json.loads(json.dumps(PAYLOAD))
        payload["result"]["addressMatches"][0]["geographies"]["Incorporated Places"][0]["BASENAME"] = "Cañón"
        raw = json.dumps(payload, ensure_ascii=False).encode()
        split = raw.index("ñ".encode()) + 1  # inside the two-byte character
        found, _ = locate(Stream(raw, chunks=[split, 1, 10]))
        self.assertEqual(found.place_name, "Cañón")
        # fragmented over-limit input refuses at the same total
        stream = Stream(body(CAP + 10), chunks=[40000] * 100)
        err, _ = locate(stream)
        self.assertEqual(str(err), REFUSED)
        self.assertLessEqual(stream.consumed, CAP + 1)

    def test_content_length_is_not_the_budget(self):
        for header in (None, "10", "999999999", "not-a-number"):
            for total, ok in ((2000, True), (CAP + 1, False)):
                stream = Stream(body(total))
                stream.headers = {} if header is None else {"Content-Length": header}
                stream.length = None if header is None else header
                found, _ = locate(stream)
                if ok:
                    self.assertEqual(found.county_fips, "06079", header)
                else:
                    self.assertEqual(str(found), REFUSED, header)

    def test_context_cleanup_on_all_paths(self):
        cases = [(body(2000), None), (body(CAP + 1), "exceeds"), (b"x" * 100, "reset"),
                 (b"\xff", "lookup failed"), (b"not json", "lookup failed"),
                 (json.dumps([]).encode(), "malformed"),
                 (json.dumps({"result": {"addressMatches": []}}).encode(), "no match"),
                 (json.dumps({"result": {"addressMatches": [{"geographies": {}}]}}).encode(), "no county")]
        for data, expect in cases:
            stream = Stream(data, fail_after=10 if expect == "reset" else None)
            found, opener = locate(stream)
            with self.subTest(expect=expect):
                if expect is None:
                    self.assertEqual(found.county_fips, "06079")
                else:
                    self.assertIn(expect, str(found))
                self.assertEqual(stream.exits, 1)
                opener.assert_called_once()
                self.assertEqual(opener.call_args.kwargs, {"timeout": 10})
        stream = Stream(b"x")
        stream.read = mock.Mock(side_effect=RuntimeError("bug"))
        with self.assertRaises(RuntimeError):
            discovery.locate_online("S", "CA", opener=mock.Mock(return_value=stream))
        self.assertEqual(stream.exits, 1)


class KitCli(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-bounds-")
        self.addCleanup(self.temporary.cleanup)
        self.root = _kit.init(Path(self.temporary.name) / "campaign", county="San Louis Obispo")

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.stat().st_mode, p.stat().st_mtime_ns,
                                                 p.read_bytes() if p.is_file() else None)
                for p in sorted(self.root.rglob("*"))}

    def run_kit(self, data):
        streams = []

        def opener(*args, **kwargs):
            streams.append(Stream(data))
            return streams[-1]

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["campaign_tool", "kit", "--directory", str(self.root), "--online"]), \
                mock.patch("urllib.request.urlopen", side_effect=opener) as urlopen, \
                mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
                code = None
            except SystemExit as caught:
                code = caught.code
        return code, out.getvalue(), err.getvalue(), urlopen, streams

    def test_actual_kit_cli_retains_offline_hint_and_tree(self):
        before = self.snapshot()
        self.assertNotIn("kit", {p.split("/")[0] for p in before})
        code, out, err, urlopen, streams = self.run_kit(body(CAP + 1))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.count("response exceeds 1 MiB"), 1, err)
        self.assertIn("No county or city named", err)
        self.assertIn("Online lookup also failed", err)
        self.assertNotIn("Traceback", err)
        urlopen.assert_called_once()
        self.assertTrue(streams[0].closed)
        self.assertEqual(self.snapshot(), before)
        # a healthy response afterwards still works, with bounded reads only
        healthy = {"result": {"addressMatches": [{"geographies": {
            "Counties": [{"GEOID": "06079", "NAME": "San Luis Obispo County"}]}}]}}
        code, out, err, urlopen, streams = self.run_kit(json.dumps(healthy).encode())
        self.assertIn(code, (0, None), err)
        self.assertNotIn("Traceback", err)
        self.assertTrue(all(0 < s <= 65536 for s in streams[0].sizes))

    def test_offline_path_never_acquires_response(self):
        from campaign_tool import kit
        seed = discovery.load_seed("us-ca")
        with mock.patch("urllib.request.urlopen") as urlopen:
            found = kit._resolve({"state": "CA", "county": "San Luis Obispo"}, seed, True)
            self.assertEqual(found.county_fips, "06079")
            with self.assertRaises(ValueError):
                kit._resolve({"state": "CA", "county": "Nowhereland"}, seed, False)
            with self.assertRaises(ValueError):
                discovery.locate("San Luis Obispo", "CA", seed)  # ambiguous
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
