"""RF1500-14: `meetings-comment-publish` refuses an update whose saved meeting would exceed the reader's JSON byte budget.

Synthetic only; nothing is sent. Run: python3 -B -m unittest tests.test_meeting_comment_publish_bounds -q
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import cli, content, meeting_comment_publish as mcp, meeting_comments  # noqa: E402

TODAY = date(2026, 10, 20)
MEETING = "cedar-council-2026-10"
LETTER = "Dear Council,\r\nPlease publish \u2014 caf\u00e9 \"quoted\" back\\slash.\r\n"
MESSAGE = "content/meetings: submitted comment update would exceed 2 MiB; nothing was written"
URL = "https://example.invalid/agenda/"


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def written(document):
    """The bytes the existing writer produces, rebuilt independently."""
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class BoundsTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-comment-bounds-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(REPO / "examples" / "fictional-campaign", self.root, ignore=shutil.ignore_patterns("public"))
        old = self.root / "content" / "meetings" / "2026-10-cedar-council.json"
        self.path = old.with_name(MEETING + ".json")
        old.rename(self.path)
        self.document = json.loads(self.path.read_text(encoding="utf-8"))
        self.document["id"] = MEETING
        self.save(self.document)
        self.letter = self.tmp / "letter.txt"
        self.letter.write_bytes(LETTER.encode("utf-8"))
        meeting_comments.record(self.root, MEETING, "2026-10-10", "email", self.letter, replace=True, today=TODAY)

    def save(self, document):
        self.path.write_bytes(written(document))

    def pad_to(self, size):
        """Pad the agenda URL (a valid, accepted field) so the saved meeting is exactly `size` bytes."""
        self.document["agenda_url"] = URL
        self.save(self.document)
        self.document["agenda_url"] = URL + "a" * (size - len(self.path.read_bytes()))
        self.save(self.document)
        self.assertEqual(len(self.path.read_bytes()), size)

    def stage(self, **kwargs):
        return mcp.publish(self.root, MEETING, self.letter, signer_public=True, today=TODAY, **kwargs)

    def tree(self):
        return {str(p.relative_to(self.tmp)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in self.tmp.rglob("*") if p.is_file()}

    def expected(self, **extra):
        comment = {"on": "2026-10-10", "channel": "email", "text": LETTER, "sha256": meeting_comments.read(self.root)[MEETING]["letter_sha256"]}
        return written({**json.loads(self.path.read_text(encoding="utf-8")), "submitted_comment": comment})

    def refused(self, **kwargs):
        before = self.tree()
        with mock.patch.object(mcp, "_write_json") as writer, self.assertRaises(mcp.MeetingCommentPublishError) as caught:
            self.stage(**kwargs)
        self.assertEqual(str(caught.exception), MESSAGE)
        writer.assert_not_called()
        self.assertEqual(self.tree(), before, "a refusal must change nothing")

    def test_real_cap_first_submission_refuses_without_poisoning_meeting(self):
        self.pad_to(content.MAX_JSON_BYTES - 10)
        content._read_json(self.path)  # readable today
        self.refused()
        self.assertEqual(len(content._read_json(self.path)["agenda_url"]), len(self.document["agenda_url"]))
        self.assertFalse((self.root / "public").exists())

    def test_real_cap_replace_preserves_existing_submission(self):
        self.stage()
        later = self.tmp / "later.txt"
        later.write_bytes(b"Second, longer letter. " * 100)
        meeting_comments.record(self.root, MEETING, "2026-10-11", "portal", later, replace=True, today=TODAY)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.document = {**self.document, "submitted_comment": stored["submitted_comment"]}
        self.pad_to(content.MAX_JSON_BYTES - 10)
        before = self.tree()
        with mock.patch.object(mcp, "_write_json") as writer, self.assertRaises(mcp.MeetingCommentPublishError) as caught:
            mcp.publish(self.root, MEETING, later, signer_public=True, replace=True, today=TODAY)
        self.assertEqual(str(caught.exception), MESSAGE)
        writer.assert_not_called()
        self.assertEqual(self.tree(), before)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["submitted_comment"], stored["submitted_comment"])

    def test_exact_budget_allows_equality_and_refuses_one_byte_over_with_utf8_accounting(self):
        size = len(self.expected())
        self.assertGreater(size, len(json.dumps(json.loads(self.expected()), ensure_ascii=False, separators=(",", ":"))))  # not compact
        self.assertGreater(len(self.expected()), len(self.expected().decode("utf-8")))  # multibyte: bytes exceed characters
        with mock.patch.object(mcp, "MAX_JSON_BYTES", size - 1):
            self.refused()
        with mock.patch.object(mcp, "MAX_JSON_BYTES", size):
            comment, _ = self.stage()
        self.assertEqual(self.path.read_bytes(), self.expected_after(comment))
        self.assertEqual(len(self.path.read_bytes()), size)
        self.assertEqual(comment["text"].encode("utf-8"), LETTER.encode("utf-8"))  # CRLF and multibyte text kept exactly
        # replace charges the replacement object only, not the old plus the new submission
        for cap, ok in ((size - 1, False), (size, True)):
            with mock.patch.object(mcp, "MAX_JSON_BYTES", cap):
                if ok:
                    self.stage(replace=True)
                    self.assertEqual(len(self.path.read_bytes()), size)
                else:
                    self.refused(replace=True)

    def expected_after(self, comment):
        document = {k: v for k, v in json.loads(self.path.read_text(encoding="utf-8")).items() if k != "submitted_comment"}
        return written({**document, "submitted_comment": comment})

    def test_cli_capacity_refusal_is_exit_one_without_partial_success(self):
        self.pad_to(content.MAX_JSON_BYTES - 10)
        before = self.tree()
        for extra in ((), ("--replace",)):
            code, out, err = run("meetings-comment-publish", "--directory", self.root, "--id", MEETING, "--file", self.letter,
                                 "--signer-public", *extra)
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(err.strip(), "Stopped: " + MESSAGE)
            for leak in (str(self.root), "example.invalid", "Please publish", "Traceback", "Staged only"):
                self.assertNotIn(leak, out + err)
        self.assertEqual(self.tree(), before)

    def test_earlier_refusals_keep_precedence_and_writer_errors_propagate(self):
        with mock.patch.object(mcp, "MAX_JSON_BYTES", 1):
            with self.assertRaisesRegex(mcp.MeetingCommentPublishError, "--signer-public is required"):
                mcp.publish(self.root, MEETING, self.letter, today=TODAY)
            with self.assertRaisesRegex(mcp.MeetingCommentPublishError, "invalid meeting id"):
                mcp.publish(self.root, "bad/id", self.letter, signer_public=True, today=TODAY)
        content_cap = content.MAX_JSON_BYTES
        self.assertEqual(content_cap, 2 * 1024 * 1024)
        self.stage()
        other = self.tmp / "other.txt"
        other.write_bytes(b"not the recorded letter")
        with mock.patch.object(mcp, "MAX_JSON_BYTES", 1):
            with self.assertRaisesRegex(mcp.MeetingCommentPublishError, "already shows a submitted comment"):
                self.stage()
            with self.assertRaisesRegex(mcp.MeetingCommentPublishError, "not the letter recorded"):
                mcp.publish(self.root, MEETING, other, signer_public=True, replace=True, today=TODAY)
        with mock.patch.object(mcp, "_write_json", side_effect=OSError("disk full")), self.assertRaisesRegex(OSError, "disk full"):
            self.stage(replace=True)

    def test_normal_retry_after_reducing_unrelated_content_stages_exact_letter(self):
        self.pad_to(content.MAX_JSON_BYTES - 10)
        self.refused()
        receipt = (self.root / "kit" / "comments-sent.json").read_bytes()
        self.document["agenda_url"] = URL
        self.save(self.document)
        comment, _ = self.stage()
        self.assertEqual(content.load_meeting(content._read_json(self.path), "m", today=TODAY)["submitted_comment"], comment)
        self.assertEqual(comment["text"].encode("utf-8"), LETTER.encode("utf-8"))
        self.assertEqual((comment["on"], comment["channel"]), ("2026-10-10", "email"))
        self.assertEqual((self.root / "kit" / "comments-sent.json").read_bytes(), receipt)
        self.assertFalse((self.root / "public").exists())


if __name__ == "__main__":
    unittest.main()
