"""RF0015-6: a comment-receipt update must not write a store its own reader (2 MiB) would refuse. Synthetic only.

Run from the repository root: python3 -B -m unittest tests.test_meeting_comment_bounds -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import meeting_comments, today  # noqa: E402
from campaign_tool.content import MAX_JSON_BYTES  # noqa: E402
from tests.test_meeting_comment_sent import AS_OF, NOW, meeting, run  # noqa: E402

REFUSAL = "kit/comments-sent.json: update would exceed 2 MiB; nothing was written"
OLD = {"on": "2026-03-01", "channel": "email", "recorded_at": "2026-03-01T00:00:00.000000+00:00"}


def pretty(document):
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def store(count, indent=2, prefix="h"):
    comments = {f"{prefix}{i:07d}": dict(OLD) for i in range(count)}
    return {"schema_version": 1, "comments": comments}


class MeetingCommentBoundsTests(unittest.TestCase):
    def setUp(self):
        mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")).start()
        self.addCleanup(mock.patch.stopall)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-comment-bounds-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(REPO / "examples" / "fictional-campaign", self.root)
        for folder in ("findings", "meetings"):
            shutil.rmtree(self.root / "content" / folder, ignore_errors=True)
            (self.root / "content" / folder).mkdir()
        for document in (meeting("m-a", "2026-03-23T18:00:00-07:00", "2026-03-22T12:00:00-07:00"),
                         meeting("m-b", "2026-03-24T18:00:00-07:00", "2026-03-21T12:00:00-07:00")):
            (self.root / "content" / "meetings" / f"{document['id']}.json").write_text(json.dumps(document))
        self.kit = self.root / "kit"
        self.sent = self.kit / "comments-sent.json"

    def seed(self, data):
        self.kit.mkdir(mode=0o700, exist_ok=True)
        self.sent.write_bytes(data)
        os.chmod(self.sent, 0o600)
        return data

    def record(self, ident="m-b", channel="email", **kwargs):
        return meeting_comments.record(self.root, ident, "2026-03-12", channel, today=AS_OF, now=NOW, **kwargs)

    def assert_refused_untouched(self, before, **kwargs):
        stat = self.sent.stat()
        with self.assertRaises(meeting_comments.MeetingCommentError) as caught:
            self.record(**kwargs)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertEqual(self.sent.read_bytes(), before)
        self.assertEqual((self.sent.stat().st_mtime_ns, self.sent.stat().st_mode), (stat.st_mtime_ns, stat.st_mode))
        self.assertEqual([p.name for p in self.kit.iterdir()], ["comments-sent.json"])
        meeting_comments.read(self.root)  # still readable

    _near = None

    def near_cap(self):
        """Canonical store whose size is within 20 bytes of the cap, so any new entry overflows it."""
        if MeetingCommentBoundsTests._near is None:
            count = MAX_JSON_BYTES // 130
            while len(pretty(store(count))) > MAX_JSON_BYTES - 300:
                count -= 1
            document = store(count)
            document["comments"]["p"] = dict(OLD)
            gap = MAX_JSON_BYTES - 20 - len(pretty(document))
            document = store(count)
            document["comments"]["p" * (1 + gap)] = dict(OLD)
            MeetingCommentBoundsTests._near = (count + 1, pretty(document))
        return MeetingCommentBoundsTests._near

    def test_real_cap_canonical_append_refuses_before_poisoning(self):
        count, data = self.near_cap()
        before = self.seed(data)
        self.assertLessEqual(len(before), MAX_JSON_BYTES)
        self.assertGreater(len(before), MAX_JSON_BYTES - 40)
        self.assertEqual(len(meeting_comments.read(self.root)), count)
        self.assert_refused_untouched(before)

    def test_real_cap_compact_input_expansion_refuses(self):
        count = self.near_cap()[0] + 800
        document = store(count)
        compact = json.dumps(document, separators=(",", ":")).encode()
        self.assertLess(len(compact) + 200, MAX_JSON_BYTES)
        self.assertGreater(len(pretty(document)), MAX_JSON_BYTES)
        before = self.seed(compact)
        self.assertEqual(len(meeting_comments.read(self.root)), count)
        self.assert_refused_untouched(before)

    def exact_store(self, pad, ident="m-b", channel="email", extra=None):
        """Old store (with a padding key of ``pad`` chars) and the exact bytes record() would write."""
        old = store(3)
        old["comments"]["é" * pad] = dict(OLD)
        new = json.loads(json.dumps(old))
        entry = {"on": "2026-03-12", "channel": channel, **(extra or {}),
                 "recorded_at": NOW.isoformat(timespec="microseconds")}
        new["comments"][ident] = entry
        new["comments"] = dict(sorted(new["comments"].items()))
        return old, pretty(new)

    def test_exact_prospective_utf8_boundary_includes_newline_at_real_cap(self):
        base = len(self.exact_store(1)[1])
        pad = (MAX_JSON_BYTES - base) // 2  # each padding char is 2 UTF-8 bytes
        _, expected = self.exact_store(pad)
        extra = MAX_JSON_BYTES - len(expected) + 2 * pad - 2 * pad  # ASCII bytes still missing
        for ascii_pad, ok in ((extra, True), (extra + 1, False)):
            with self.subTest(ascii_pad=ascii_pad):
                shutil.rmtree(self.kit, ignore_errors=True)
                key = "é" * pad + "x" * ascii_pad
                old = store(3)
                old["comments"][key] = dict(OLD)
                new = json.loads(json.dumps(old))
                new["comments"]["m-b"] = {"on": "2026-03-12", "channel": "email",
                                          "recorded_at": NOW.isoformat(timespec="microseconds")}
                new["comments"] = dict(sorted(new["comments"].items()))
                before = self.seed(pretty(old))
                if ok:
                    self.assertEqual(len(pretty(new)), MAX_JSON_BYTES)
                    self.assertLess(len(pretty(new).decode("utf-8")), MAX_JSON_BYTES)  # characters alone misjudge
                    self.record()
                    self.assertEqual(self.sent.read_bytes(), pretty(new))
                    self.assertEqual(len(meeting_comments.read(self.root)), 5)
                else:
                    self.assertEqual(len(pretty(new)), MAX_JSON_BYTES + 1)
                    self.assert_refused_untouched(before)

    def test_small_cap_boundary_matrix(self):
        old, expected = self.exact_store(5)
        for cap, ok in ((len(expected) - 1, False), (len(expected), True), (len(expected) + 1, True)):
            with self.subTest(cap=cap):
                shutil.rmtree(self.kit, ignore_errors=True)
                before = self.seed(pretty(old))
                with mock.patch.object(meeting_comments, "MAX_JSON_BYTES", cap), \
                        mock.patch("campaign_tool.content.MAX_JSON_BYTES", MAX_JSON_BYTES):
                    if ok:
                        self.record()
                        self.assertEqual(self.sent.read_bytes(), expected)
                    else:
                        self.assert_refused_untouched(before)

    def test_replace_is_budgeted_for_channel_and_letter_metadata(self):
        letter = self.tmp / "letter.txt"
        letter.write_text("Synthetic letter")
        old, grown = self.exact_store(5, channel="in_person", ident="m-a")
        old["comments"]["m-a"] = {"on": "2026-03-01", "channel": "mail", "recorded_at": OLD["recorded_at"]}
        _, with_letter = self.exact_store(5, ident="m-a", channel="mail", extra={
            "letter_sha256": "0" * 64, "letter_bytes": 16})
        _, shrunk = self.exact_store(5, ident="m-a", channel="mail")
        cases = ((dict(channel="in_person"), grown, 1), (dict(channel="mail", file=letter), with_letter, 1),
                 (dict(channel="mail"), shrunk, 0))
        for kwargs, expected, _ in cases:
            for cap, ok in ((len(expected) - 1, False), (len(expected), True)):
                with self.subTest(kwargs=sorted(kwargs), cap=cap):
                    shutil.rmtree(self.kit, ignore_errors=True)
                    before = self.seed(pretty(old))
                    # the digest differs from the dummy one only in value, never in length
                    with mock.patch.object(meeting_comments, "MAX_JSON_BYTES", cap):
                        if ok:
                            self.record(ident="m-a", replace=True, **kwargs)
                            self.assertEqual(len(self.sent.read_bytes()), len(expected))
                        elif len(before) > cap:
                            continue  # shrinking case: old store is already over this tiny cap
                        else:
                            self.assert_refused_untouched(before, ident="m-a", replace=True, **kwargs)
        # duplicate without --replace keeps its old refusal
        shutil.rmtree(self.kit, ignore_errors=True)
        self.seed(pretty(old))
        with self.assertRaises(meeting_comments.MeetingCommentError) as caught:
            self.record(ident="m-a")
        self.assertIn("already has a comment recorded", str(caught.exception))

    def test_overflow_precedes_any_output_side_effect(self):
        with mock.patch.object(meeting_comments, "MAX_JSON_BYTES", 10), \
                mock.patch.object(meeting_comments, "_kit_folder") as folder, \
                mock.patch.object(meeting_comments, "_write_json") as writer:
            with self.assertRaises(meeting_comments.MeetingCommentError) as caught:
                self.record()
        self.assertEqual(str(caught.exception), REFUSAL)
        folder.assert_not_called()
        writer.assert_not_called()
        self.assertFalse(self.kit.exists())

    def test_cli_overflow_is_fixed_private_refusal_not_success(self):
        before = self.seed(self.near_cap()[1])
        arguments = ("meetings-comment-sent", "--directory", self.root, "--id", "m-b", "--on", "2026-03-12",
                     "--channel", "email")
        code, out, err = run(*arguments)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(REFUSAL, err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Recorded only", err)
        self.assertNotIn(str(self.root), err)
        self.assertEqual(self.sent.read_bytes(), before)

    def test_refused_update_retains_downstream_submission_state(self):
        data = json.loads(self.near_cap()[1])
        data["comments"].pop("h0000000")
        data["comments"]["m-a"] = {"on": "2026-03-10", "channel": "portal", "recorded_at": OLD["recorded_at"]}
        data["comments"] = dict(sorted(data["comments"].items()))
        before = self.seed(pretty(data))
        mapping = meeting_comments.read(self.root)
        deadlines = [i["what"] for i in today.todo(self.root, AS_OF) if i["kind"] == "comment_deadline"]
        self.assertEqual(len(deadlines), 1)  # m-a suppressed, m-b still due
        self.assert_refused_untouched(before, ident="m-b")
        self.assertEqual(meeting_comments.read(self.root), mapping)
        self.assertEqual([i["what"] for i in today.todo(self.root, AS_OF) if i["kind"] == "comment_deadline"],
                         deadlines)

    def test_in_cap_writer_bytes_are_unchanged(self):
        for channel in meeting_comments.CHANNELS:
            shutil.rmtree(self.kit, ignore_errors=True)
            entry = self.record(channel=channel)
            self.assertEqual(self.sent.read_bytes(), pretty({"schema_version": 1, "comments": {"m-b": entry}}))
            self.assertEqual(self.sent.stat().st_mode & 0o777, 0o600)
        self.assertEqual(entry["recorded_at"], NOW.isoformat(timespec="microseconds"))
        self.assertIsInstance(NOW, datetime)
        self.assertIs(NOW.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
