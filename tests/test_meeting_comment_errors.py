"""RF0445-13: malformed stored comment-receipt entries are refused with value-free diagnostics. Synthetic only.

Run from the repository root: python3 -B -m unittest tests.test_meeting_comment_errors -v
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
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
GENERIC = "kit/comments-sent.json: a stored comment entry is malformed"
DATE = "kit/comments-sent.json: stored comment on must be a real YYYY-MM-DD date"
KEY = "PRIVATE_KEY_CANARY@example.invalid"
SHA = "a" * 64
GOOD = {"on": "2026-03-12", "channel": "email", "recorded_at": "2026-03-12T00:00:00Z"}


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def meeting(ident, starts, deadline):
    written = {"how": "email the synthetic clerk", "deadline": deadline}
    return {"id": ident, "body": f"Synthetic Council {ident}", "starts_at": starts, "status": "scheduled",
            "agenda_url": f"https://agenda.synthetic.example/{ident}", "comment_kit_md": f"Hello {ident}.\n",
            "participation": {"in_person": True, "remote": None, "written_comment": written,
                              "source_url": "https://agenda.synthetic.example/rules", "verified_by": "Pat Organizer",
                              "verified_at": "2026-03-10"}}


class MeetingCommentErrorTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-comment-errors-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(REPO / "examples" / "fictional-campaign", self.root)
        content = self.root / "content"
        for folder in ("findings", "meetings"):
            shutil.rmtree(content / folder, ignore_errors=True)
            (content / folder).mkdir()
        for document in (meeting("m-a", "2026-03-23T18:00:00-07:00", "2026-03-22T12:00:00-07:00"),
                         meeting("m-b", "2026-03-24T18:00:00-07:00", "2026-03-21T12:00:00-07:00")):
            (content / "meetings" / f"{document['id']}.json").write_text(json.dumps(document), encoding="utf-8")
        self.sent = self.root / "kit" / "comments-sent.json"

    def store(self, comments):
        self.sent.parent.mkdir(exist_ok=True)
        self.sent.write_text(json.dumps({"schema_version": 1, "comments": comments}), encoding="utf-8")

    def snapshot(self):
        return {str(p.relative_to(self.root)): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None,
                                                p.stat().st_mode, p.stat().st_mtime_ns)
                for p in sorted(self.root.rglob("*"))}

    def message(self, value, key=KEY):
        with self.assertRaises(meeting_comments.MeetingCommentError) as caught:
            meeting_comments._entry(value, key)
        return str(caught.exception)

    def test_real_malformed_receipt_key_is_never_in_errors(self):
        for key in (KEY, "m-a"):
            self.store({key: {"on": "2026-03-12"}})
            with self.assertRaises(meeting_comments.MeetingCommentError) as caught:
                meeting_comments.read(self.root)
            self.assertEqual(str(caught.exception), GENERIC)

    def test_stored_date_errors_use_fixed_label(self):
        for bad in (20260312, None, ["2026-03-12"], {"d": 1}, "2026-3-12", "２０２６-03-12", "2026-02-30", "2026-13-01"):
            self.assertEqual(self.message({**GOOD, "on": bad}), DATE, repr(bad))
        self.assertEqual(meeting_comments._entry({**GOOD, "on": "2028-02-29"}, KEY)["on"], "2028-02-29")
        with self.assertRaises(meeting_comments.MeetingCommentError) as caught:
            meeting_comments.record(self.root, "m-a", "2026-3-12", "email", today=AS_OF)
        self.assertIn("must be a real YYYY-MM-DD date", str(caught.exception))

    def test_every_entry_refusal_is_value_free(self):
        sentinel = "METADATA_VALUE_CANARY"
        cases = [sentinel, ["x"], {"on": GOOD["on"], "channel": "email"},
                 {**GOOD, "extra": sentinel},
                 {**GOOD, "channel": sentinel}, {**GOOD, "recorded_at": 5},
                 {**GOOD, "letter_sha256": SHA}, {**GOOD, "letter_bytes": 3},
                 {**GOOD, "letter_sha256": sentinel, "letter_bytes": 3},
                 {**GOOD, "letter_sha256": 7, "letter_bytes": 3},
                 {**GOOD, "letter_sha256": SHA, "letter_bytes": True},
                 {**GOOD, "letter_sha256": SHA, "letter_bytes": -1},
                 {**GOOD, "letter_sha256": SHA, "letter_bytes": "3"}]
        for value in cases:
            text = self.message(value)
            self.assertEqual(text, GENERIC, repr(value))
            self.assertNotIn(KEY, text)
            self.assertNotIn(sentinel, text)

    def test_long_unicode_and_control_keys_do_not_amplify_diagnostics(self):
        for key in ("k" * 100_000, "ключ-鍵-🔑", "line\nbreak\x1b[31m", ""):
            self.assertEqual(self.message({"on": "2026-03-12"}, key), GENERIC)
            self.assertEqual(self.message({**GOOD, "on": "nope"}, key), DATE)
        self.store({"k" * 100_000: {"on": "2026-03-12"}})
        code, out, err = run("meetings-comment-sent", "--directory", self.root, "--id", "m-a", "--on", "2026-03-12",
                             "--channel", "email", "--replace")
        self.assertLess(len(err), 300)
        self.assertNotIn("kkkk", err)

    def test_real_cli_refuses_before_writes_without_private_values(self):
        for comments, fixed in (({KEY: {"on": "2026-03-12"}}, GENERIC),
                                ({KEY: {**GOOD, "on": "2026-02-30"}}, DATE)):
            self.store(comments)
            before = self.snapshot()
            for extra in ((), ("--replace",)):
                code, out, err = run("meetings-comment-sent", "--directory", self.root, "--id", "m-a",
                                     "--on", "2026-03-12", "--channel", "email", *extra)
                self.assertEqual((code, out), (1, ""), err)
                self.assertIn(fixed, err)
                self.assertNotIn("KEY_CANARY", err)
                self.assertNotIn("Traceback", err)
                self.assertNotIn("Recorded only", err)
            self.assertEqual(self.snapshot(), before)
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(self.tmp)}
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "meetings-comment-sent", "--directory",
                               str(self.root), "--id", "m-a", "--on", "2026-03-12", "--channel", "email"],
                              cwd=REPO, env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn(DATE, done.stderr)
        self.assertNotIn(KEY, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_today_retains_one_safe_unreadable_and_all_deadlines(self):
        self.store({KEY: {"on": "2026-03-12"}})
        before = self.snapshot()
        items = today.todo(self.root, AS_OF)
        unreadable = [item for item in items if item["kind"] == "unreadable"]
        self.assertEqual(len(unreadable), 1)
        self.assertIn(GENERIC, unreadable[0]["what"])
        self.assertEqual(len([i for i in items if i["kind"] == "comment_deadline"]), 2)
        for text in (today.run_cli(self.root, "2026-03-15"), today.run_cli(self.root, "2026-03-15", as_json=True)):
            self.assertNotIn("KEY_CANARY", text)
        self.assertEqual(self.snapshot(), before)
        self.store({"m-a": GOOD})  # a valid receipt still suppresses only its own meeting
        items = today.todo(self.root, AS_OF)
        self.assertEqual([i["kind"] for i in items if i["kind"] == "unreadable"], [])
        self.assertEqual(len([i for i in items if i["kind"] == "comment_deadline"]), 1)

    def test_pack_keeps_existing_fallback(self):
        self.store({KEY: {"on": "2026-03-12"}})
        self.assertIsNone(build(self.root, "m-a", today=NOW)["comment_sent"])
        self.store({"m-a": {**GOOD, "channel": "portal"}})
        report = build(self.root, "m-a", today=NOW, replace=True)
        self.assertEqual(report["comment_sent"], {"on": "2026-03-12", "channel": "portal"})

    def test_valid_and_legacy_admission_is_unchanged(self):
        self.assertEqual(meeting_comments.read(self.root), {})
        self.store({})
        self.assertEqual(meeting_comments.read(self.root), {})
        comments: dict = {channel: {**GOOD, "channel": channel} for channel in meeting_comments.CHANNELS}
        comments["orphan key / with spaces"] = {**GOOD, "letter_sha256": SHA, "letter_bytes": 0}
        comments[""] = GOOD
        self.store(comments)
        self.assertEqual(meeting_comments.read(self.root), comments)
        meeting_comments.record(self.root, "m-a", "2026-03-12", "email", today=AS_OF)
        self.assertEqual(meeting_comments.read(self.root)["m-a"]["channel"], "email")


if __name__ == "__main__":
    unittest.main()
