"""RF0540-2: overdeep JSON in kit/comments-sent.json is a controlled malformed-input refusal. Synthetic only.

Run from the repository root: python3 -B -m unittest tests.test_meeting_comment_decode_errors -q
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import meeting_comments as mc, today  # noqa: E402
from campaign_tool.content import ContentError  # noqa: E402
from campaign_tool.meeting_pack import build  # noqa: E402
from tests import test_meeting_comment_sent as base  # noqa: E402

DEPTH = 10000
SHAPES = {
    "arrays": "[" * DEPTH + "]" * DEPTH,
    "objects": '{"a":' * DEPTH + "0" + "}" * DEPTH,
    "mixed": '[{"a":' * (DEPTH // 2) + "0" + "}]" * (DEPTH // 2),
}
PUBLIC = "kit/comments-sent.json is not readable JSON (ContentError)"


def store_text(shape, where):
    deep = SHAPES[shape]
    good = '{"on":"2026-03-12","channel":"email","recorded_at":"x"}'
    if where == "root":
        return '{"schema_version":1,"comments":{},"x":%s}' % deep
    if where == "comments":
        return '{"schema_version":1,"comments":{"m-a":%s}}' % deep
    return '{"schema_version":1,"comments":{"m-a":%s}}' % good.replace('"x"}', '"x","y":%s}' % deep)


class DecodeErrorTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-decode-errors-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(REPO / "examples" / "fictional-campaign", self.root)
        content = self.root / "content"
        for folder in ("findings", "meetings"):
            shutil.rmtree(content / folder, ignore_errors=True)
            (content / folder).mkdir()
        for doc in (base.meeting("m-a", "2026-03-23T18:00:00-07:00", "2026-03-22T12:00:00-07:00"),
                    base.meeting("m-b", "2026-03-24T18:00:00-07:00", "2026-03-21T12:00:00-07:00")):
            (content / "meetings" / f"{doc['id']}.json").write_text(json.dumps(doc), encoding="utf-8")
        self.sent = self.root / "kit" / "comments-sent.json"
        self.sent.parent.mkdir(exist_ok=True)

    def cases(self):
        for shape in SHAPES:
            for where in ("root", "comments", "entry"):
                yield shape, where, store_text(shape, where)

    def test_actual_nested_arrays_objects_and_mixed_receipts_refuse_cleanly(self):
        for shape, where, text in self.cases():
            self.sent.write_text(text, encoding="utf-8")
            with self.subTest(shape=shape, where=where):
                with self.assertRaises(ContentError) as private:
                    mc._read_receipts(self.sent)
                self.assertEqual(str(private.exception), "kit/comments-sent.json: not valid UTF-8 JSON")
                with self.assertRaises(mc.MeetingCommentError) as public:
                    mc.read(self.root)
                self.assertEqual(str(public.exception), PUBLIC)
                self.assertIsNone(public.exception.__cause__)
        # shallow malformed controls keep their distinct messages
        self.sent.write_text("{not json", encoding="utf-8")
        with self.assertRaises(mc.MeetingCommentError) as shallow:
            mc.read(self.root)
        self.assertEqual(str(shallow.exception), PUBLIC)
        self.sent.write_text("[]", encoding="utf-8")
        with self.assertRaises(mc.MeetingCommentError) as shape:
            mc.read(self.root)
        self.assertIn("expected", str(shape.exception))

    def test_decoder_refusal_closes_the_real_receipt_descriptor(self):
        self.sent.write_text(SHAPES["arrays"], encoding="utf-8")
        opened = []
        real_open = os.open

        def spy(path, flags, *a, **k):
            fd = real_open(path, flags, *a, **k)
            if str(path) == str(self.sent):
                opened.append(fd)
            return fd

        with mock.patch("os.open", spy), self.assertRaises(ContentError):
            mc._read_receipts(self.sent)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_record_and_replace_refuse_before_all_output_or_letter_work(self):
        self.sent.write_text(SHAPES["arrays"], encoding="utf-8")
        before = self.sent.read_bytes()
        for replace in (False, True):
            with mock.patch.object(mc, "_letter") as letter, mock.patch.object(mc, "_kit_folder") as kit, \
                    mock.patch.object(mc, "_write_json") as write:
                with self.assertRaises(mc.MeetingCommentError) as caught:
                    mc.record(self.root, "m-a", "2026-03-12", "email", today=base.AS_OF, replace=replace)
                self.assertEqual(str(caught.exception), PUBLIC)
                for spy in (letter, kit, write):
                    spy.assert_not_called()
            self.assertEqual(self.sent.read_bytes(), before)

    def test_actual_comment_sent_cli_uses_existing_refusal_contract(self):
        self.sent.write_text(SHAPES["mixed"], encoding="utf-8")
        before = self.sent.read_bytes()
        for extra in ((), ("--replace",)):
            code, out, err = base.run("meetings-comment-sent", "--directory", self.root, "--id", "m-a",
                                      "--on", "2026-03-10", "--channel", "email", *extra)
            self.assertEqual(code, 1)  # the CLI's existing ValueError exit code on main
            self.assertEqual(out, "")
            self.assertIn("comments-sent.json", err)
            self.assertNotIn("Traceback", err)
            self.assertNotIn("recursion", err.lower())
            self.assertEqual(self.sent.read_bytes(), before)

    def test_today_recovers_one_bad_store_without_suppressing_deadlines(self):
        self.sent.write_text(SHAPES["objects"], encoding="utf-8")
        items = today.todo(self.root, base.AS_OF)
        unreadable = [i for i in items if i["kind"] == "unreadable"]
        self.assertEqual(len(unreadable), 1)
        self.assertIn("kit/comments-sent.json", unreadable[0]["what"])
        deadlines = [i for i in items if i["kind"] == "comment_deadline"]
        self.assertEqual(len(deadlines), 2)

    def test_pack_completes_existing_unreadable_store_fallback(self):
        self.sent.write_text(SHAPES["arrays"], encoding="utf-8")
        before = self.sent.read_bytes()
        report = build(self.root, "m-a", today=base.NOW)
        self.assertIsNone(report["comment_sent"])
        folder = self.root / "kit" / "meeting-packs" / "m-a"
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["comment.md", "handout.html", "sources.csv"])
        self.assertEqual(self.sent.read_bytes(), before)
        replaced = build(self.root, "m-a", today=base.NOW, replace=True)
        self.assertIsNone(replaced["comment_sent"])

    def test_only_decode_recursion_is_relabelled(self):
        self.sent.write_text('{"schema_version":1,"comments":{}}', encoding="utf-8")
        with mock.patch.object(mc.json, "loads", side_effect=RecursionError("x")), \
                self.assertRaises(ContentError):
            mc._read_receipts(self.sent)
        for exc in (RuntimeError("x"), MemoryError()):
            with mock.patch.object(mc.json, "loads", side_effect=exc), self.assertRaises(type(exc)):
                mc._read_receipts(self.sent)
        # a RecursionError outside the decoder seam is not relabelled as bad JSON
        with mock.patch.object(mc, "_read_receipts", return_value={"schema_version": 1, "comments": {}}), \
                mock.patch.object(mc, "_entry", side_effect=RecursionError("x")):
            self.sent.write_text('{"schema_version":1,"comments":{}}', encoding="utf-8")
            self.assertEqual(mc.read(self.root), {})
        with mock.patch.object(mc, "_read_receipts", return_value={"schema_version": 1,
                                                                  "comments": {"m-a": {}}}), \
                mock.patch.object(mc, "_entry", side_effect=RecursionError("x")), \
                self.assertRaises(RecursionError):
            mc.read(self.root)
        with mock.patch.object(mc.os, "read", side_effect=RecursionError("x")), \
                self.assertRaises(RecursionError):
            mc._read_receipts(self.sent)

    def test_ordinary_receipts_preserve_every_existing_contract(self):
        self.assertEqual(mc.read(self.root), {})
        self.sent.write_text('{"schema_version":1,"comments":{}}', encoding="utf-8")
        self.assertEqual(mc.read(self.root), {})
        entry = mc.record(self.root, "m-a", "2026-03-12", "portal", today=base.AS_OF)
        self.assertEqual(mc.read(self.root)["m-a"], entry)
        self.sent.write_bytes(b"\xff\xfe")
        with self.assertRaises(mc.MeetingCommentError) as bad:
            mc.read(self.root)
        self.assertEqual(str(bad.exception), PUBLIC)


if __name__ == "__main__":
    unittest.main()
