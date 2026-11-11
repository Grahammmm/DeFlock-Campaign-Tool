"""RF2311-1: content/events.json is read through a MAX_JSON_BYTES + 1 byte ceiling before decoding. Synthetic data only."""
import json
import unittest
from unittest import mock

from campaign_tool import content_events
from campaign_tool.content import MAX_JSON_BYTES, ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.content_events import read_events
from campaign_tool.site import build_site

from tests.test_site_events import TODAY, EventsCase, event

REAL_OPEN = open


class Spy:
    """Forwarding real binary stream: records each read request, the bytes returned and closure."""

    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def read(self, *args):
        data = self.stream.read(*args)
        self.log["requests"].append(args)
        self.log["returned"] += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stream.close()
        self.log["closed"] += 1
        return False


def padded(size, head=b"[]"):
    return head + b" " * (size - len(head))


class BoundedReadTests(EventsCase):
    def spied(self):
        log = {"requests": [], "returned": 0, "closed": 0}
        patch = mock.patch.object(content_events, "open", lambda p, mode="r": Spy(REAL_OPEN(p, mode), log), create=True)
        patch.start()
        self.addCleanup(patch.stop)
        return log

    def test_consumption_never_exceeds_cap_plus_one(self):
        for size, ok in ((MAX_JSON_BYTES - 1, True), (MAX_JSON_BYTES, True), (MAX_JSON_BYTES + 1, False), (3 * MAX_JSON_BYTES, False)):
            self.put(padded(size))
            log = self.spied()
            if ok:
                self.assertEqual(read_events(self.root), [])
            else:
                with self.assertRaisesRegex(ContentError, r"^events\.json: file too large$"):
                    read_events(self.root)
            self.assertLessEqual(log["returned"], MAX_JSON_BYTES + 1, size)
            self.assertTrue(all(r == (MAX_JSON_BYTES + 1,) for r in log["requests"]))
            self.assertEqual(log["closed"], 1)
            mock.patch.stopall()

    def test_growth_after_metadata_cannot_bypass_cap(self):
        path = self.put(b"[]")
        self.assertTrue(path.is_file())
        real_is_file = type(path).is_file

        def grow_then_check(self_path):
            result = real_is_file(self_path)
            if self_path.name == "events.json":
                with REAL_OPEN(self_path, "ab") as handle:
                    handle.write(b" " * (3 * MAX_JSON_BYTES))
            return result
        log = self.spied()
        with mock.patch.object(type(path), "is_file", grow_then_check):
            with self.assertRaisesRegex(ContentError, r"^events\.json: file too large$"):
                read_events(self.root)
        self.assertLessEqual(log["returned"], MAX_JSON_BYTES + 1)

    def test_size_precedes_decoding_and_parsing(self):
        for head in (b"\xff\xfe", b"{not json", b"[]"):
            self.put(padded(MAX_JSON_BYTES + 1, head).replace(b" ", b"a") if head != b"[]" else padded(MAX_JSON_BYTES + 1))
            with mock.patch.object(content_events.json, "loads", side_effect=AssertionError("parsed")), \
                    mock.patch.object(content_events, "load_events", side_effect=AssertionError("loaded")):
                with self.assertRaisesRegex(ContentError, r"^events\.json: file too large$"):
                    read_events(self.root)

    def test_decoder_errors_at_limit_keep_legacy_text(self):
        self.put(b"\xff" + b" " * (MAX_JSON_BYTES - 1))
        with self.assertRaisesRegex(ContentError, r"^events\.json: not valid UTF-8 JSON \(.*utf-8"):
            read_events(self.root)
        self.put(b"")
        with self.assertRaisesRegex(ContentError, r"^events\.json: not valid UTF-8 JSON"):
            read_events(self.root)

    def test_utf8_limit_is_in_bytes_and_split_char_refused_by_size(self):
        text = "é" * 10
        body = json.dumps([event(1, title=text)], ensure_ascii=False).encode("utf-8")
        self.put(body + b" " * (MAX_JSON_BYTES - len(body)))
        self.assertEqual(read_events(self.root)[0]["title"], text)
        # one extra byte, then a multibyte tail straddling the detection byte
        self.put(body + b" " * (MAX_JSON_BYTES - len(body) - 1) + "é".encode("utf-8"))
        with self.assertRaisesRegex(ContentError, r"^events\.json: file too large$"):
            read_events(self.root)

    def test_collect_and_consumers_share_admission(self):
        self.put(padded(MAX_JSON_BYTES + 1))
        problems = ["earlier"]
        self.assertIsNone(read_events(self.root, problems))
        self.assertEqual(problems, ["earlier", "events.json: file too large"])
        self.assertIn("events.json: file too large", content_problems(self.root))
        with self.assertRaisesRegex(ContentError, "events.json: file too large"):
            build_site(self.root, today=TODAY)

    def test_oversize_build_preserves_public_outputs(self):
        before = self.build()
        self.put(padded(MAX_JSON_BYTES + 1))
        with self.assertRaises(ContentError):
            build_site(self.root, today=TODAY)
        self.assertEqual(self.tree(), before)

    def test_closes_on_failures_and_oserror_and_missing_unchanged(self):
        log = self.spied()
        self.put(b"[" * 100000)
        with self.assertRaisesRegex(ContentError, "nested too deeply|not valid"):
            read_events(self.root)
        self.assertEqual(log["closed"], 1)
        self.put(b"[]")
        with mock.patch.object(Spy, "read", side_effect=OSError("boom")):
            with self.assertRaisesRegex(ContentError, r"^events\.json: could not be read$"):
                read_events(self.root)
        self.assertEqual(log["closed"], 2)
        self.path.unlink()
        self.assertIsNone(read_events(self.root))


if __name__ == "__main__":
    unittest.main()
