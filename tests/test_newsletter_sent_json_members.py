"""RF1221-8: duplicate JSON members are refused in newsletter sent history and the current draft. Synthetic."""
import contextlib
import io
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.__main__ import main  # noqa: E402
from campaign_tool.newsletter.sent import last_sent, record_sent  # noqa: E402

HIST = "kit/newsletter-sent.json unreadable: duplicate JSON keys"
DRAFT = "kit/newsletter-draft.json unreadable: duplicate JSON keys"
SHA = "ab" * 32
GOOD_DRAFT = '{"subject": "S1", "html": "<p>x</p>"}'


def history(extra=""):
    issue = ('{"sent_on": "2026-03-05", "draft_sha256": "%s", "subject": "S", "recorded_at": "2026-03-05T00:00:00+00:00"%s}'
             % (SHA, extra))
    return '{"schema_version": 1, "issues": [%s]}' % issue


class DuplicateMemberTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-sent-dup-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "campaign"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(self.root), "--name",
                        "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        self.kit = self.root / "kit"
        self.kit.mkdir(exist_ok=True)
        self.sent, self.draft = self.kit / "newsletter-sent.json", self.kit / "newsletter-draft.json"
        self.draft.write_text(GOOD_DRAFT)

    def entries(self):
        return sorted(str(p.relative_to(self.kit)) for p in self.kit.rglob("*"))

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(["--directory", str(self.root), *args])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_duplicate_history_members_are_refused(self):
        cases = ['{"schema_version": 1, "issues": [], "issues": []}',
                 history().replace('"schema_version": 1', '"schema_version": 1, "\\u0069ssues": []'),
                 history(', "subject": "S"'), history(', "sent_on": "2026-03-05"'),
                 history(', "draft_sha256": "%s"' % ("cd" * 32)), history(', "recorded_at": "x"')]
        for text in cases:
            with self.subTest(text=text):
                self.sent.write_text(text)
                before = (self.sent.read_bytes(), self.entries())
                with self.assertRaises(ValueError) as ctx:
                    last_sent(self.root)
                self.assertEqual(str(ctx.exception), HIST)
                with self.assertRaises(ValueError) as ctx:
                    record_sent(self.root, "2026-03-06")
                self.assertEqual(str(ctx.exception), HIST)
                self.assertEqual((self.sent.read_bytes(), self.entries()), before)

    def test_duplicate_draft_members_never_record(self):
        for text in ('{"subject": "A", "subject": "B"}', '{"subject": "A", "subject": "A"}',
                     '{"subject": "A", "html": "x", "html": "y"}',
                     '{"subject": "A", "public_archive": {"k": 1, "k": 2}}',
                     '{"subject": "A", "unknown": {"a": {"b": 1, "b": 1}}}',
                     '{"x": 1, "x": 2}'):
            with self.subTest(text=text):
                self.draft.write_text(text)
                with self.assertRaises(ValueError) as ctx:
                    record_sent(self.root, "2026-03-06")
                self.assertEqual(str(ctx.exception), DRAFT)
                self.assertEqual(self.entries(), ["newsletter-draft.json"])

    def test_error_does_not_echo_key_and_distinct_keys_pass(self):
        self.draft.write_text('{"subject": "A", "SECRETKEY": 1, "SECRETKEY": 2}')
        with self.assertRaises(ValueError) as ctx:
            record_sent(self.root, "2026-03-06")
        self.assertNotIn("SECRETKEY", str(ctx.exception))
        self.draft.write_text('{"subject": "A", "a": {"k": 1}, "b": {"k": 2}, "K": 3, "k": 4}')
        self.assertEqual(record_sent(self.root, "2026-03-06")["subject"], "A")
        self.assertEqual(last_sent(self.root), "2026-03-06")

    def test_cli_stops_without_mutation(self):
        self.sent.write_text('{"schema_version": 1, "issues": [], "issues": []}')
        for args in (("sent", "--date", "2026-03-06"), ("--record-sent", "2026-03-06")):
            with self.subTest(args=args):
                before = (self.sent.read_bytes(), self.entries())
                code, out, err = self.cli(*args)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("Stopped: " + HIST, err)
                self.assertNotIn("Traceback", err)
                self.assertEqual((self.sent.read_bytes(), self.entries()), before)
        self.sent.unlink()
        self.draft.write_text('{"subject": "A", "subject": "B"}')
        for args in (("sent", "--date", "2026-03-06"), ("--record-sent", "2026-03-06")):
            with self.subTest(args=args):
                code, out, err = self.cli(*args)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("Stopped: " + DRAFT, err)
                self.assertEqual(self.entries(), ["newsletter-draft.json"])

    def test_unrelated_refusals_unchanged_and_repair_recovers(self):
        self.draft.write_text('{"subject": ')
        with self.assertRaisesRegex(ValueError, "is not a newsletter draft"):
            record_sent(self.root, "2026-03-06")
        self.draft.write_text('{"html": "x"}')
        with self.assertRaisesRegex(ValueError, "is not a newsletter draft"):
            record_sent(self.root, "2026-03-06")
        self.sent.write_text("{")
        with self.assertRaises(ValueError) as ctx:
            last_sent(self.root)
        self.assertTrue(str(ctx.exception).startswith("kit/newsletter-sent.json unreadable: "))
        self.assertNotIn("duplicate", str(ctx.exception))
        self.sent.unlink()
        self.draft.write_text(GOOD_DRAFT)
        self.assertEqual(record_sent(self.root, "2026-03-06")["sent_on"], "2026-03-06")
        self.assertEqual(last_sent(self.root), "2026-03-06")


if __name__ == "__main__":
    unittest.main()
