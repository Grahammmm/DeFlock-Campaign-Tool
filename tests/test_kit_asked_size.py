"""RF0035-6: kit-asked keeps its output inside the content reader's byte ceiling (synthetic only).

The real 2 MiB ceiling is exercised through the exact candidate size: the module cap is patched to that size (or one less),
so the boundary is the writer's own bytes (indent=2, UTF-8, final newline), not a source-size proxy.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_asked  # noqa: E402
from campaign_tool.content import MAX_JSON_BYTES, _read_json, load_agencies  # noqa: E402
from tests.test_kit_asked import EXAMPLE, POLICE, TODAY, A, B, run  # noqa: E402


class KitAskedSizeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-asked-size-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.path = self.root / "content" / "agencies.json"
        self.sent_path = self.root / "kit" / "sent.json"
        self.agencies = json.loads(self.path.read_text(encoding="utf-8"))
        self.path.write_text(json.dumps(self.agencies, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        request = {"sent_on": "2026-03-02", "channel": "email", "draft": f"requests/{POLICE}.md",
                   "draft_sha256": "a" * 64, "recorded_at": "2026-03-02T10:00:00+00:00", "steps": [],
                   "scopes": [A, B]}
        self.sent_path.parent.mkdir(exist_ok=True)
        self.sent_path.write_text(json.dumps({"schema_version": 1, "requests": {POLICE: request}}), encoding="utf-8")

    def candidate_bytes(self):
        result = kit_asked.propose(self.root, TODAY)
        self.assertTrue(result["changed"])
        self.assertIsNone(result["error"])
        return (json.dumps(result["document"], indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def snapshot(self):
        stat = self.path.stat()
        return (self.path.read_bytes(), stat.st_mode, stat.st_ino, stat.st_mtime_ns,
                sorted(p.name for p in self.path.parent.iterdir()), self.sent_path.read_bytes())

    def apply(self, cap):
        with mock.patch.object(kit_asked, "MAX_JSON_BYTES", cap):
            return run("kit-asked", "--directory", self.root, "--apply")

    def test_cap_minus_one_exact_cap_and_plus_one(self):
        expected = self.candidate_bytes()
        before = self.snapshot()
        with mock.patch.object(kit_asked.kit, "_write_json", side_effect=AssertionError("written")), \
                mock.patch.object(kit_asked.os, "chmod", side_effect=AssertionError("chmod")):
            code, out, err = self.apply(len(expected) - 1)
        self.assertEqual(code, 3)
        self.assertIn(f"content/agencies.json: proposed output would exceed {len(expected) - 1} bytes", err)
        self.assertNotIn("Written", out)
        self.assertEqual(self.snapshot(), before)
        # a dry run refuses the same way
        with mock.patch.object(kit_asked, "MAX_JSON_BYTES", len(expected) - 1):
            self.assertEqual(run("kit-asked", "--directory", self.root)[0], 3)
        self.assertEqual(self.snapshot(), before)
        # exactly the cap is accepted; installed bytes equal the admitted encoding and the real reader reopens them
        code, out, err = self.apply(len(expected))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.path.read_bytes(), expected)
        with mock.patch("campaign_tool.content.MAX_JSON_BYTES", len(expected)):
            load_agencies(_read_json(self.path), "content/agencies.json", TODAY)
        # the final newline counts: without it the cap would have fitted
        self.assertEqual(len(expected.rstrip(b"\n")) + 1, len(expected))

    def test_compact_input_expansion_uses_full_writer_encoding(self):
        self.path.write_text(json.dumps(self.agencies, separators=(",", ":")), encoding="utf-8")
        compact = len(self.path.read_bytes())
        expected = self.candidate_bytes()
        self.assertGreater(len(expected), compact + 50)
        before = self.snapshot()
        code, out, err = self.apply(compact + 10)  # source and the one added item fit; the indented output does not
        self.assertEqual(code, 3)
        self.assertIn("proposed output would exceed", err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.apply(MAX_JSON_BYTES)[0], 0)
        self.assertEqual(self.path.read_bytes(), expected)

    def test_multibyte_profile_counts_utf8_bytes_not_codepoints(self):
        self.agencies[0]["name"] = "é" * 40
        self.path.write_text(json.dumps(self.agencies, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        expected = self.candidate_bytes()
        text_len = len(expected.decode("utf-8"))
        self.assertGreater(len(expected), text_len)
        before = self.snapshot()
        code, out, err = self.apply(text_len)  # enough characters, too few bytes
        self.assertEqual(code, 3)
        self.assertNotIn("é", out + err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.apply(len(expected))[0], 0)
        self.assertEqual(self.path.read_bytes(), expected)

    def test_no_change_never_serializes_or_rewrites(self):
        self.assertEqual(self.apply(MAX_JSON_BYTES)[0], 0)
        before = self.snapshot()
        with mock.patch.object(kit_asked.json, "dumps", side_effect=AssertionError("serialized")):
            code, out, err = self.apply(1)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Nothing written.", out)
        self.assertEqual(self.snapshot(), before)

    def test_structural_error_precedes_byte_error(self):
        with mock.patch.object(kit_asked, "load_agencies", side_effect=kit_asked.ContentError("shape problem")):
            code, out, err = self.apply(1)
        self.assertEqual(code, 3)
        self.assertIn("shape problem", err)
        self.assertNotIn("exceed", err)

    def test_unencodable_candidate_is_safe_and_prewrite(self):
        self.agencies[0]["name"] = "bad \ud800 text"
        text = json.dumps(self.agencies, indent=2, ensure_ascii=True) + "\n"
        self.path.write_text(text, encoding="utf-8")
        before = self.snapshot()
        with mock.patch.object(kit_asked.kit, "_write_json", side_effect=AssertionError("written")):
            code, out, err = self.apply(MAX_JSON_BYTES)
        self.assertEqual(code, 3)
        self.assertIn("content/agencies.json: proposed output cannot be encoded as UTF-8", err)
        self.assertNotIn("bad", err)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
