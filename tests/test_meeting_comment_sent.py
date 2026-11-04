"""DF1207-09: `meetings-comment-sent` records a comment the organizer submitted by hand. Synthetic only.

Run from the repository root: python3 -B -m unittest tests.test_meeting_comment_sent -v
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
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import cli, meeting_comments, today  # noqa: E402
from campaign_tool.meeting_pack import build  # noqa: E402

AS_OF = date(2026, 3, 15)
NOW = datetime(2026, 3, 15, tzinfo=timezone.utc)
LETTER_TEXT = "Synthetic letter text that must never be copied into kit zq7"


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def meeting(ident, starts, deadline, status="scheduled"):
    written = {"how": "email the synthetic clerk", "deadline": deadline}
    return {"id": ident, "body": f"Synthetic Council {ident}", "starts_at": starts, "status": status,
            "agenda_url": f"https://agenda.synthetic.example/{ident}", "comment_kit_md": f"Hello {ident}.\n",
            "participation": {"in_person": True, "remote": None, "written_comment": written,
                              "source_url": "https://agenda.synthetic.example/rules", "verified_by": "Pat Organizer",
                              "verified_at": "2026-03-10"}}


class MeetingCommentSentTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-comment-sent-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(REPO / "examples" / "fictional-campaign", self.root)
        self.content = self.root / "content"
        for folder in ("findings", "meetings"):
            shutil.rmtree(self.content / folder, ignore_errors=True)
            (self.content / folder).mkdir()
        for document in (meeting("m-a", "2026-03-23T18:00:00-07:00", "2026-03-22T12:00:00-07:00"),
                         meeting("m-b", "2026-03-24T18:00:00-07:00", "2026-03-21T12:00:00-07:00"),
                         meeting("m-off", "2026-03-19T18:00:00-07:00", "2026-03-18T12:00:00-07:00", "cancelled")):
            (self.content / "meetings" / f"{document['id']}.json").write_text(json.dumps(document), encoding="utf-8")
        self.sent = self.root / "kit" / "comments-sent.json"

    def record(self, ident="m-a", on="2026-03-12", channel="email", **kwargs):
        return meeting_comments.record(self.root, ident, on, channel, today=AS_OF, **kwargs)

    def refused(self, *args, fragment="", **kwargs):
        with self.assertRaises(meeting_comments.MeetingCommentError) as caught:
            self.record(*args, **kwargs)
        self.assertIn(fragment, str(caught.exception))

    def deadlines(self):
        return [item["what"].split("(")[-1].rstrip(")") for item in today.todo(self.root, AS_OF)
                if item["kind"] == "comment_deadline"]

    def snapshot(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.root.rglob("*")) if p.is_file()}

    def letter(self, text=LETTER_TEXT):
        path = self.tmp / "letter-as-sent.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def test_record_drops_that_meetings_deadline_only(self):
        self.assertEqual(self.deadlines(), ["m-b", "m-a"])
        entry = self.record()
        self.assertEqual(self.deadlines(), ["m-b"])
        self.assertEqual({k: entry[k] for k in ("on", "channel")}, {"on": "2026-03-12", "channel": "email"})
        self.assertEqual(json.loads(self.sent.read_text())["comments"]["m-a"], entry)
        self.assertEqual(self.sent.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.sent.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(meeting_comments.read(self.root), {"m-a": entry})
        self.assertEqual(meeting_comments.read(self.tmp / "nowhere"), {})

    def test_all_four_channels_and_the_meeting_day_itself_are_accepted(self):
        for channel in ("email", "portal", "mail", "in_person"):
            self.assertEqual(self.record(channel=channel, replace=True)["channel"], channel)
        self.assertEqual(meeting_comments.record(self.root, "m-a", "2026-03-23", "in_person", today=date(2026, 3, 30),
                                                 replace=True)["on"], "2026-03-23")
        self.refused(channel="fax", fragment="--channel")

    def test_future_date_or_after_the_meeting_day_refused_and_nothing_written(self):
        before = self.snapshot()
        with self.assertRaises(meeting_comments.MeetingCommentError) as caught:
            meeting_comments.record(self.root, "m-a", "2026-03-16", "email", today=date(2026, 3, 15))
        self.assertIn("future", str(caught.exception))
        with self.assertRaises(meeting_comments.MeetingCommentError) as caught:
            meeting_comments.record(self.root, "m-a", "2026-03-24", "email", today=date(2026, 3, 30))
        self.assertIn("after the meeting day 2026-03-23", str(caught.exception))
        for bad in ("2026-3-5", "2026-02-30", "tomorrow", "", "２０２６-03-10"):
            self.refused(on=bad, fragment="YYYY-MM-DD")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.sent.exists())

    def test_unknown_cancelled_and_path_like_ids_refused(self):
        before = self.snapshot()
        self.refused("nope", fragment="no staged meeting nope")
        self.refused("m-off", fragment="cancelled")
        for bad in ("m-a#x", "../m-a", "..", ".", "", "a/b", "m-a\n", "x" * 81):
            self.refused(bad, fragment="")
        (self.content / "meetings" / "link.json").symlink_to(self.content / "meetings" / "m-a.json")
        before = self.snapshot()
        self.refused("link", fragment="no staged meeting")
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_needs_replace_and_replacement_is_fresh(self):
        first = meeting_comments.record(self.root, "m-a", "2026-03-10", "email", today=AS_OF,
                                        now=datetime(2026, 3, 11, tzinfo=timezone.utc))
        stored = self.sent.read_bytes()
        self.refused(on="2026-03-12", fragment="2026-03-10")
        self.assertEqual(self.sent.read_bytes(), stored)
        second = meeting_comments.record(self.root, "m-a", "2026-03-12", "portal", today=AS_OF, replace=True,
                                         now=datetime(2026, 3, 13, tzinfo=timezone.utc))
        self.assertEqual((second["on"], second["channel"]), ("2026-03-12", "portal"))
        self.assertNotEqual(second["recorded_at"], first["recorded_at"])
        self.assertEqual(list(meeting_comments.read(self.root)), ["m-a"])

    def test_letter_file_checks_and_only_hash_and_size_are_kept(self):
        for bad in (self.tmp / "missing.txt", self.tmp):
            self.refused(file=bad, fragment="--file")
        link = self.tmp / "link.txt"
        link.symlink_to(self.letter())
        self.refused(file=link, fragment="--file")
        big = self.tmp / "big.bin"
        big.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        self.refused(file=big, fragment="5 MiB")
        self.assertFalse(self.sent.exists())
        entry = self.record(file=self.letter())
        self.assertEqual(entry["letter_sha256"], hashlib.sha256(LETTER_TEXT.encode()).hexdigest())
        self.assertEqual(entry["letter_bytes"], len(LETTER_TEXT))
        edge = self.tmp / "edge.bin"
        edge.write_bytes(b"x" * (5 * 1024 * 1024))
        self.assertEqual(self.record(file=edge, replace=True)["letter_bytes"], 5 * 1024 * 1024)
        self.record(file=self.letter(), replace=True)
        for path in (self.root / "kit").rglob("*"):
            if path.is_file():
                data = path.read_text(encoding="utf-8")
                self.assertNotIn("zq7", data)
                self.assertNotIn("letter-as-sent", data)
                self.assertNotIn(str(self.tmp), data)

    def test_malformed_file_is_one_unreadable_item_and_record_refuses(self):
        bad_documents = ["{not json", "[]", json.dumps({"schema_version": 2, "comments": {}}),
                         json.dumps({"schema_version": 1, "comments": []}),
                         json.dumps({"schema_version": 1, "comments": {"m-a": {"on": "2026-03-12"}}}),
                         json.dumps({"schema_version": 1, "comments": {"m-a": {
                             "on": "2026-03-12", "channel": "fax", "recorded_at": "x"}}}),
                         json.dumps({"schema_version": 1, "comments": {"m-a": {
                             "on": "2026-03-12", "channel": "email", "recorded_at": "x", "letter_sha256": "ab"}}})]
        self.sent.parent.mkdir(exist_ok=True)
        for text in bad_documents:
            self.sent.write_text(text, encoding="utf-8")
            items = today.todo(self.root, AS_OF)
            unreadable = [item for item in items if item["kind"] == "unreadable"]
            self.assertEqual(len(unreadable), 1, text)
            self.assertIn("kit/comments-sent.json", unreadable[0]["what"])
            self.assertEqual(self.deadlines(), ["m-b", "m-a"], text)
            before = self.sent.read_bytes()
            self.refused(on="2026-03-12", fragment="comments-sent.json")
            self.assertEqual(self.sent.read_bytes(), before)
        self.sent.unlink()
        self.sent.symlink_to(self.tmp / "elsewhere.json")
        self.refused(fragment="symlink")

    def test_meeting_pack_names_the_comment_and_its_files_are_unchanged(self):
        first = build(self.root, "m-a", today=NOW)
        self.assertIsNone(first["comment_sent"])
        folder = self.root / "kit" / "meeting-packs" / "m-a"
        files = {p.name: p.read_bytes() for p in folder.iterdir()}
        self.assertEqual(sorted(files), ["comment.md", "handout.html", "sources.csv"])
        self.record(channel="portal")
        again = build(self.root, "m-a", today=NOW, replace=True)
        self.assertEqual(again["comment_sent"], {"on": "2026-03-12", "channel": "portal"})
        self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, files)
        self.assertIsNone(build(self.root, "m-b", today=NOW)["comment_sent"])
        self.sent.write_text("{broken", encoding="utf-8")  # a bad file never stops a pack
        self.assertIsNone(build(self.root, "m-a", today=NOW, replace=True)["comment_sent"])

    def test_without_a_record_today_output_is_unchanged(self):
        plain = today.run_cli(self.root, "2026-03-15")
        self.assertIn("m-a", plain)
        self.sent.parent.mkdir(exist_ok=True)
        self.sent.write_text(json.dumps({"schema_version": 1, "comments": {}}), encoding="utf-8")
        self.assertEqual(today.run_cli(self.root, "2026-03-15"), plain)
        self.assertEqual(today.run_cli(self.root, "2026-03-15", as_json=True),
                         json.dumps(today.todo(self.root, AS_OF), indent=2, ensure_ascii=False))

    def test_cli_records_prints_prefix_and_says_nothing_was_sent(self):
        before = {k: v for k, v in self.snapshot().items() if k.startswith("content/")}
        code, out, err = run("meetings-comment-sent", "--directory", self.root, "--id", "m-a", "--on", "2026-03-10",
                             "--channel", "mail", "--file", self.letter())
        self.assertEqual((code, err), (0, ""), err)
        digest = hashlib.sha256(LETTER_TEXT.encode()).hexdigest()
        self.assertIn(digest[:12], out)
        self.assertNotIn(digest, out)
        self.assertTrue(out.endswith("Recorded only. Nothing was sent.\n"))
        self.assertEqual({k: v for k, v in self.snapshot().items() if k.startswith("content/")}, before)
        code, out, err = run("meetings-comment-sent", "--directory", self.root, "--id", "m-a", "--on", "2026-03-11",
                             "--channel", "mail")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("2026-03-10", err)
        code, out, err = run("meetings-comment-sent", "--directory", self.root, "--id", "m-a", "--on", "2026-03-11",
                             "--channel", "mail", "--replace")
        self.assertEqual(code, 0, err)
        code, out, err = run("meetings-comment-sent", "--directory", self.root, "--id", "m-a", "--on", "2026-03-11",
                             "--channel", "pigeon")
        self.assertEqual(code, 2)
        code, out, err = run("meeting-pack", "--directory", self.root, "--id", "m-b", "--allow-past")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["comment_sent"], None)
        code, out, err = run("meeting-pack", "--directory", self.root, "--id", "m-a", "--allow-past")
        self.assertEqual(code, 0, err)
        self.assertIn("Comment recorded as submitted on 2026-03-11 (mail).", out.splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
