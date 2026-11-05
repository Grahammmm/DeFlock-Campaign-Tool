"""DF1407-06: `meetings-comment-publish` stages the exact submitted comment. Synthetic only; nothing is sent.

Run from the repository root: python3 -B -m unittest tests.test_meeting_comment_publish -v
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import cli, meeting_comment_publish, meeting_comments  # noqa: E402
from campaign_tool.content import ContentError, comment_text_problems, load_meeting  # noqa: E402

TODAY = date(2026, 10, 20)
MEETING = "cedar-council-2026-10"
LETTER = "Dear Council,\r\nPlease publish the policy. Caf\u00e9 \u2014 thanks.\r\n"


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class PublishTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-comment-publish-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(REPO / "examples" / "fictional-campaign", self.root, ignore=shutil.ignore_patterns("public"))
        old = self.root / "content" / "meetings" / "2026-10-cedar-council.json"
        self.path = old.with_name(MEETING + ".json")
        old.rename(self.path)
        self.document = json.loads(self.path.read_text(encoding="utf-8"))
        self.document["id"] = MEETING
        self.path.write_text(json.dumps(self.document), encoding="utf-8")
        self.sent = self.root / "kit" / "comments-sent.json"

    def letter(self, text=LETTER, name="letter.txt", raw=None):
        path = self.tmp / name
        path.write_bytes(raw if raw is not None else text.encode("utf-8"))
        return path

    def record(self, path, channel="email", ident=MEETING):
        return meeting_comments.record(self.root, ident, "2026-10-10", channel, path, replace=True, today=TODAY)

    def stage(self, path, **kwargs):
        return meeting_comment_publish.publish(self.root, MEETING, path, signer_public=True, today=TODAY, **kwargs)

    def refused(self, path, fragment="", **kwargs):
        before = self.snapshot()
        with self.assertRaises(meeting_comment_publish.MeetingCommentPublishError) as caught:
            self.stage(path, **kwargs)
        self.assertIn(fragment, str(caught.exception))
        self.assertEqual(self.snapshot(), before, "a refusal must change nothing")
        return str(caught.exception)

    def snapshot(self):
        return {str(p.relative_to(self.tmp)): p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()}

    def test_happy_path_every_channel_keeps_exact_bytes_and_other_fields(self):
        for channel in meeting_comments.CHANNELS:
            self.path.write_text(json.dumps(self.document), encoding="utf-8")
            path = self.letter()
            self.record(path, channel)
            comment, notes = self.stage(path, replace=True)
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(stored["submitted_comment"], comment)
            self.assertEqual((comment["on"], comment["channel"]), ("2026-10-10", channel))
            self.assertEqual(comment["text"].encode("utf-8"), path.read_bytes())  # CRLF and non-ASCII kept
            self.assertEqual(comment["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual({k: v for k, v in stored.items() if k != "submitted_comment"}, self.document)
            self.assertEqual(notes, [])
            self.assertEqual(load_meeting(stored, "m", today=TODAY)["submitted_comment"], comment)

    def test_signer_flag_is_required_before_anything_is_read_or_written(self):
        path = self.letter()
        self.record(path)
        before = self.snapshot()
        with self.assertRaises(meeting_comment_publish.MeetingCommentPublishError):
            meeting_comment_publish.publish(self.root, MEETING, path, today=TODAY)
        code, out, err = run("meetings-comment-publish", "--directory", self.root, "--id", MEETING, "--file", path)
        self.assertEqual(code, 1)
        self.assertIn("--signer-public is required", err)
        self.assertEqual(self.snapshot(), before)

    def test_cli_stages_only_and_prints_no_letter_text(self):
        path = self.letter()
        self.record(path)
        code, out, err = run("meetings-comment-publish", "--directory", self.root, "--id", MEETING, "--file", path, "--signer-public")
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing was sent", out)
        self.assertNotIn("Please publish", out + err)
        self.assertIn("submitted_comment", self.path.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "public").exists())

    def test_hash_binding_refusals_change_nothing(self):
        path = self.letter()
        self.refused(path, "no recorded letter hash")  # nothing recorded at all
        self.record(self.letter(name="no-hash.txt"), ident=MEETING)
        meeting_comments.record(self.root, MEETING, "2026-10-10", "email", None, replace=True, today=TODAY)  # no letter hash
        self.refused(path, "no recorded letter hash")
        self.record(path)
        for name, raw in (("changed-byte.txt", LETTER.replace("Dear", "Dean").encode()),
                          ("newline.txt", LETTER.replace("\r\n", "\n").encode()), ("trailing.txt", LETTER.encode() + b"\n")):
            self.refused(self.letter(raw=raw, name=name), "not the letter recorded")
        document = json.loads(self.sent.read_text(encoding="utf-8"))  # a matching-looking entry of ANOTHER meeting
        document["comments"]["other"] = document["comments"].pop(MEETING)
        self.sent.write_text(json.dumps(document), encoding="utf-8")
        self.refused(path, "no recorded letter hash")

    def test_size_pair_and_malformed_ledger(self):
        path = self.letter()
        self.record(path)
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        document["comments"][MEETING]["letter_bytes"] += 1
        self.sent.write_text(json.dumps(document), encoding="utf-8")
        self.refused(path, "not the letter recorded")
        self.sent.write_text("{not json", encoding="utf-8")
        self.refused(path, "comments-sent.json")

    def test_size_and_text_rules(self):
        cases = {"empty": (b"", "1 to 16384"), "blank": (b" \r\n\t", "1 to 16384"), "bom": (b"\xef\xbb\xbfHi", "byte-order mark"),
                 "nul": (b"Hi\x00there", "control"), "control": (b"Hi\x07there", "control"), "badutf8": (b"Hi \xff\xfe", "UTF-8"),
                 "over": (b"a" * 16385, "1 to 16384")}
        for name, (raw, fragment) in cases.items():
            with self.subTest(name):
                path = self.letter(raw=raw, name=name)
                meeting_comments.record(self.root, MEETING, "2026-10-10", "email", path, replace=True, today=TODAY)
                self.refused(path, fragment)
        exact = self.letter(raw=b"a" * 16384, name="exact")
        self.record(exact)
        self.assertEqual(len(self.stage(exact, replace=True)[0]["text"]), 16384)
        wide = self.letter(raw=("\u00e9" * 8192 + "a").encode("utf-8"), name="wide")  # 16385 bytes, fewer characters
        meeting_comments.record(self.root, MEETING, "2026-10-10", "email", wide, replace=True, today=TODAY)
        self.refused(wide, "1 to 16384", replace=True)

    def test_over_limit_file_is_read_only_up_to_one_byte_past(self):
        path = self.letter(raw=b"a" * 100000, name="huge")
        self.record(path)
        self.assertEqual(len(meeting_comment_publish._bytes(path)), 16385)

    def test_placeholders_refused_but_ordinary_brackets_pass(self):
        for text in ("Dear [name],", "From [CITY] resident", "x [Edit Me] y", "[EDIT ME: add the ask]", "see[ name ]now", "a[edit me]b"):
            with self.subTest(text):
                path = self.letter(text)
                meeting_comments.record(self.root, MEETING, "2026-10-10", "email", path, replace=True, today=TODAY)
                self.refused(path, "placeholder")
        path = self.letter("Item [7] and the [fictional] policy; [see attached].")
        meeting_comments.record(self.root, MEETING, "2026-10-10", "email", path, replace=True, today=TODAY)
        self.assertEqual(self.stage(path)[0]["text"], "Item [7] and the [fictional] policy; [see attached].")

    def test_privacy_hits_name_line_and_category_only(self):
        email, phone, plate = "jdoe" + "@" + "example.org", "(805) " + "555-0142", "7ABC" + "123"
        for text, expected in (
                (f"first {email}\nsecond\nthird", "line 1: email"), (f"a\nb\ncall {phone} now", "line 3: phone"),
                (f"a\r\nb\r\nc\r\nplate {plate}", "line 4: plate"), (f"{email} {phone}\nok", "line 1: email, phone"),
                ("jdoe [at] example [dot] org", "line 1: email")):
            with self.subTest(expected):
                path = self.letter(text)
                meeting_comments.record(self.root, MEETING, "2026-10-10", "email", path, replace=True, today=TODAY)
                message = self.refused(path, expected)
                for secret in (email, phone, plate, str(path), "second"):
                    self.assertNotIn(secret, message)

    def test_legal_wording_is_advisory_only(self):
        path = self.letter("The city violated the Public Records Act and broke the law.")
        self.record(path)
        comment, notes = self.stage(path)
        self.assertTrue(notes and notes[0].startswith("review-required: "), notes)
        self.assertNotIn("violated", notes[0])
        with mock.patch.object(meeting_comment_publish, "legal_notes", return_value=["review-required: legal-claim wording (guard-error)"]):
            self.assertEqual(self.stage(path, replace=True)[1], ["review-required: legal-claim wording (guard-error)"])

    def test_existing_comment_kept_without_replace_and_replace_changes_only_that_key(self):
        path = self.letter()
        self.record(path)
        self.stage(path)
        first = self.path.read_bytes()
        self.refused(path, "--replace")
        meeting_comments.record(self.root, MEETING, "2026-10-11", "portal", path, replace=True, today=TODAY)
        ledger = self.sent.read_bytes()
        self.stage(path, replace=True)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((stored["submitted_comment"]["on"], stored["submitted_comment"]["channel"]), ("2026-10-11", "portal"))
        self.assertEqual({k: v for k, v in stored.items() if k != "submitted_comment"}, self.document)
        self.assertNotEqual(first, self.path.read_bytes())
        self.assertEqual(self.sent.read_bytes(), ledger)  # the private record is never changed

    def test_cancelled_meeting_may_keep_its_recorded_comment(self):
        path = self.letter()
        self.record(path)
        self.path.write_text(json.dumps({**self.document, "status": "cancelled"}), encoding="utf-8")
        self.assertEqual(self.stage(path)[0]["channel"], "email")

    def test_input_and_target_path_refusals(self):
        path = self.letter()
        self.record(path)
        link = self.tmp / "link.txt"
        link.symlink_to(path)
        self.refused(link, "regular file")
        self.refused(self.tmp, "regular file")
        self.refused(self.tmp / "missing.txt", "regular file")
        fifo = self.tmp / "fifo"
        os.mkfifo(fifo)
        self.refused(fifo, "regular file")
        for bad in ("../x", "a/b", "", ".", "..", "x" * 81):
            with self.assertRaises(meeting_comment_publish.MeetingCommentPublishError):
                meeting_comment_publish.publish(self.root, bad, path, signer_public=True, today=TODAY)
        with self.assertRaises(meeting_comment_publish.MeetingCommentPublishError):  # a meeting that is not staged
            meeting_comment_publish.publish(self.root, "nope", path, signer_public=True, today=TODAY)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.stage(path)

    def test_symlinked_meeting_file_and_folder_are_refused(self):
        path = self.letter()
        self.record(path)
        real = self.tmp / "real.json"
        shutil.move(self.path, real)
        self.path.symlink_to(real)
        self.refused(path, "no staged meeting")
        self.path.unlink()
        shutil.move(real, self.path)
        folder = self.root / "content" / "meetings"
        moved = self.tmp / "moved-meetings"
        folder.rename(moved)
        folder.symlink_to(moved)
        with self.assertRaises(meeting_comment_publish.MeetingCommentPublishError):
            self.stage(path)

    def test_write_failure_keeps_the_old_file(self):
        path = self.letter()
        self.record(path)
        before = self.path.read_bytes()
        with mock.patch.object(meeting_comment_publish, "_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.stage(path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_manually_authored_comment_is_validated_by_the_loader(self):
        text = "Hello"
        good = {"on": "2026-10-10", "channel": "mail", "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
        self.assertEqual(load_meeting({**self.document, "submitted_comment": good}, "m", today=TODAY)["submitted_comment"], good)
        self.assertNotIn("submitted_comment", load_meeting(self.document, "m", today=TODAY))  # absence adds nothing
        for name, change in (("missing key", {"channel": None}), ("extra key", {"x": 1}), ("future", {"on": "2026-10-21"}),
                             ("bad date", {"on": "2026-13-01"}), ("channel", {"channel": "fax"}), ("digest shape", {"sha256": "ab"}),
                             ("digest mismatch", {"sha256": "0" * 64}), ("placeholder", {"text": "Dear [name]"}),
                             ("oversized", {"text": "a" * 16385}), ("pii", {"text": "call (805) 555-0142"})):
            value = {**good, **change}
            value = {k: v for k, v in value.items() if v is not None}
            with self.subTest(name), self.assertRaises(ContentError):
                load_meeting({**self.document, "submitted_comment": value}, "m", today=TODAY)

    def test_shared_check_names_rules_not_text(self):
        self.assertEqual(comment_text_problems("Plain words."), [])
        self.assertEqual(comment_text_problems("Item [7] stays."), [])


if __name__ == "__main__":
    unittest.main()
