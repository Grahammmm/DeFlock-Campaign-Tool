"""RF1255-1: meetings-status --json stays valid UTF-8 for admitted lone-surrogate text.

Synthetic data only; nothing is fetched, sent or written into the campaign.
Run from the repository root: python3 -B -m unittest tests.test_meetings_status_json_unicode -q
"""
import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool import meetings_status
from campaign_tool.meetings_status import inventory, run_cli
from tests.test_meetings_status import AS_OF, REPO_ROOT, snapshot, write, meeting

ENV = {**os.environ, "PYTHONIOENCODING": "utf-8:strict", "PYTHONDONTWRITEBYTECODE": "1"}


def raw_meeting(mid, starts_escaped, body="Body"):
    return ('{"id": "%s", "body": "%s", "starts_at": "%s", "agenda_item": "Item 1"}' % (mid, body, starts_escaped))


def strict_stdout():
    return io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict", newline="\n")


def run_in_process(root, as_json=True):
    out = strict_stdout()
    real = sys.stdout
    sys.stdout = out
    try:
        code = run_cli(argparse.Namespace(as_of=AS_OF, as_json=as_json), root)
        out.flush()
    finally:
        sys.stdout = real
    return code, out.buffer.getvalue()


def cli(root, *extra):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "meetings-status", "--directory", str(root),
                           "--as-of", AS_OF, *extra], cwd=REPO_ROOT, capture_output=True, env=ENV, timeout=120)


class JsonUnicodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-ms-json-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "campaign"
        (self.root / "content" / "meetings").mkdir(parents=True)

    def put(self, mid, text):
        (self.root / "content" / "meetings" / f"{mid}.json").write_text(text, encoding="utf-8")

    def test_surrogate_separator_encodes_strictly_and_roundtrips(self):
        self.put("a-sur", raw_meeting("a-sur", "2026-11-20\\ud80018:00:00-08:00"))
        self.put("b-ok", json.dumps(meeting("b-ok", "2026-11-21T18:00:00-08:00"), ensure_ascii=False))
        before = snapshot(self.root)
        expected = inventory(self.root, AS_OF, directory=str(self.root))
        self.assertIn("\ud800", json.dumps(expected, ensure_ascii=False))
        code, data = run_in_process(self.root)
        self.assertEqual(code, 0)
        text = data.decode("utf-8")
        self.assertNotIn("\\\\ud800", text)
        self.assertIn("\\ud800", text)
        self.assertEqual(json.loads(text), expected)
        self.assertTrue(data.endswith(b"}\n") and not data.endswith(b"\n\n"))
        result = cli(self.root, "--json")
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        self.assertEqual(json.loads(result.stdout.decode("utf-8")), expected)
        self.assertEqual(result.stdout, data)
        self.assertEqual(snapshot(self.root), before)
        text_mode = cli(self.root)
        self.assertEqual(text_mode.returncode, 0)
        text_mode.stdout.decode("utf-8")

    def test_scalar_unicode_and_literal_escapes_unchanged(self):
        self.put("c-uni", json.dumps(meeting("c-uni", "2026-11-20T18:00:00-08:00", ), ensure_ascii=False)
                 .replace("Body c-uni", "Caf\u00e9 \u4e2d\u6587 \U0001f600 \\\\ud800 back\\\\slash"))
        report = inventory(self.root, AS_OF, directory=str(self.root))
        old = json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
        code, data = run_in_process(self.root)
        self.assertEqual((code, data), (0, old))
        self.assertIn("Caf\u00e9".encode("utf-8"), data)

    def test_unreadable_neighbor_keeps_exit_and_healthy_rows(self):
        self.put("a-sur", raw_meeting("a-sur", "2026-11-20\\udc0018:00:00-08:00"))
        self.put("z-bad", "{not json SECRET-CANARY")
        code, data = run_in_process(self.root)
        self.assertEqual(code, 1)
        report = json.loads(data.decode("utf-8"))
        kinds = sorted(row["kind"] for row in report["rows"])
        self.assertEqual(kinds, ["staged", "unreadable"])
        self.assertNotIn(b"SECRET-CANARY", data)

    @unittest.skipIf(os.name != "posix", "needs POSIX undecodable filename bytes")
    def test_surrogateescape_directory_roundtrips(self):
        base = os.fsencode(self.tmp.name) + b"/bad-\xff-dir"
        try:
            os.mkdir(base)
        except OSError:
            self.skipTest("filesystem rejects undecodable names")
        directory = os.fsdecode(base)
        os.makedirs(os.path.join(directory, "content", "meetings"))
        write(Path(directory) / "content" / "meetings" / "a-future.json",
              meeting("a-future", "2026-11-20T18:00:00-08:00"))
        out = strict_stdout()
        real = sys.stdout
        sys.stdout = out
        try:
            code = run_cli(argparse.Namespace(as_of=AS_OF, as_json=True), Path(directory))
            out.flush()
        finally:
            sys.stdout = real
        self.assertEqual(code, 0)
        data = out.buffer.getvalue()
        report = json.loads(data.decode("utf-8"))
        self.assertEqual(report, inventory(Path(directory), AS_OF, directory=directory))
        self.assertIn("\udcff", json.dumps(report, ensure_ascii=False))
        self.assertIn(b"\\udcff", data)
        result = cli(directory, "--json")
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        self.assertEqual(result.stdout, data)

    def test_escape_matrix_no_double_encoding(self):
        report = {"schema_version": 1, "as_of": AS_OF, "private": True, "rows": [
            {"kind": "staged", "v": "\ud800 \udfff \udc00 \\ud800 \\\\ \u00e9"}]}
        real, meetings_status.inventory = meetings_status.inventory, lambda *a, **k: report
        try:
            code, data = run_in_process(self.root)
        finally:
            meetings_status.inventory = real
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(data.decode("utf-8")), report)
        self.assertIn(b"\\ud800 \\udfff \\udc00 \\\\ud800 \\\\\\\\ \xc3\xa9", data)


if __name__ == "__main__":
    unittest.main()
