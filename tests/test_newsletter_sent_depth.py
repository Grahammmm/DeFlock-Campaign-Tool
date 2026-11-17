"""RF1831-3: deeply nested JSON in newsletter sent history / current draft is a fixed ValueError. Synthetic."""
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

HIST = "kit/newsletter-sent.json unreadable: JSON nested too deeply"
DRAFT = "kit/newsletter-draft.json unreadable: JSON nested too deeply"
GOOD_DRAFT = '{"subject": "S1", "html": "<p>x</p>"}'
DEEP = "[" * 100000 + "]" * 100000
DEEP_OBJ = '{"a":' * 100000 + "1" + "}" * 100000
EMPTY_HIST = '{"schema_version": 1, "issues": []}'


class DeepJsonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-sent-deep-")
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

    def test_deep_history_refused_with_fixed_error(self):
        for nested in (DEEP, DEEP_OBJ):
            self.sent.write_text('{"schema_version": 1, "issues": [], "x": %s}' % nested)
            before = self.entries()
            with self.assertRaises(ValueError) as ctx:
                last_sent(self.root)
            self.assertEqual(str(ctx.exception), HIST)
            self.assertIsNone(ctx.exception.__cause__)
            with self.assertRaises(ValueError) as ctx:
                record_sent(self.root, "2026-03-05")
            self.assertEqual(str(ctx.exception), HIST)
            self.assertEqual(self.entries(), before)
            self.assertEqual(self.draft.read_text(), GOOD_DRAFT)

    def test_deep_draft_refused_and_nothing_written(self):
        self.sent.write_text(EMPTY_HIST)
        for nested in (DEEP, DEEP_OBJ):
            self.draft.write_text('{"subject": "S", "x": %s}' % nested)
            before, sent_before = self.entries(), self.sent.read_bytes()
            with self.assertRaises(ValueError) as ctx:
                record_sent(self.root, "2026-03-05")
            self.assertEqual(str(ctx.exception), DRAFT)
            self.assertIsNone(ctx.exception.__cause__)
            self.assertEqual(self.entries(), before)
            self.assertEqual(self.sent.read_bytes(), sent_before)

    def test_cli_stops_without_traceback_then_recovers(self):
        self.sent.write_text('{"schema_version": 1, "issues": [], "x": %s}' % DEEP)
        code, out, err = self.cli("sent", "--date", "2026-03-05")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(HIST, err)
        self.assertNotIn("Traceback", err)
        self.sent.write_text(EMPTY_HIST)
        record = record_sent(self.root, "2026-03-05")
        self.assertEqual(record["sent_on"], "2026-03-05")
        self.assertEqual(last_sent(self.root), "2026-03-05")

    def test_history_non_decoder_recursion_propagates(self):
        self.sent.write_text(EMPTY_HIST)
        with mock.patch("campaign_tool.newsletter.sent._read_regular", side_effect=RecursionError("read failure")):
            with self.assertRaisesRegex(RecursionError, "read failure"):
                last_sent(self.root)
        self.sent.write_text('{"schema_version": 1, "issues": [{"sent_on": "2026-03-05"}]}')
        with mock.patch("campaign_tool.newsletter.sent._valid_day", side_effect=RecursionError("validation failure")):
            with self.assertRaisesRegex(RecursionError, "validation failure"):
                last_sent(self.root)

    def test_shallow_extensions_and_other_exceptions_keep_contract(self):
        self.sent.write_text('{"schema_version": 1, "issues": [], "x": {"a": [[1]]}}')
        self.assertIsNone(last_sent(self.root))
        for exc in (RuntimeError, MemoryError, KeyboardInterrupt):
            with mock.patch("campaign_tool.newsletter.sent.json.loads", side_effect=exc("boom")):
                with self.assertRaises(exc):
                    last_sent(self.root)


if __name__ == "__main__":
    unittest.main()
