"""RF0111-3: an unencodable prospective agency-profile file is refused with one fixed ContentProfileError before
staging, for every mutation. Real escaped lone surrogates in a deselected agency's name; synthetic roots only.

Run from the repository root: python3 -B -m unittest tests.test_content_profile_encoding -v
"""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import content_profile  # noqa: E402
from campaign_tool.content import load_agencies  # noqa: E402
from tests import test_content_profile_documents as docs  # noqa: E402
from tests import test_content_profile_durability as dur  # noqa: E402

FIXED = "content/agencies.json: rewritten profile data cannot be encoded as UTF-8; nothing was written"


class EncodingRefusalTests(unittest.TestCase):
    def build(self, escape="\\ud800"):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-profile-encoding-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        root = base / "campaign"
        (root / "content").mkdir(parents=True)
        document = dur.fixture()
        document[2]["name"] = "Gamma MARK (deselected)"
        text = json.dumps(document, indent=2) + "\n"  # ASCII, with real escapes below
        text = text.replace("MARK", escape)
        (root / "content" / "agencies.json").write_bytes(text.encode("ascii"))
        return root

    def commands(self, root):
        return dur.DurabilityTests.commands(types.SimpleNamespace(root=root))  # type: ignore[arg-type]

    def snapshot(self, root):
        target = root / "content" / "agencies.json"
        info = target.stat()
        return (target.read_bytes(), info.st_ino, info.st_mode, info.st_mtime_ns, sorted(os.listdir(root / "content")))

    def test_real_escaped_high_and_low_surrogates_refuse_all_mutations(self):
        for escape in ("\\ud800", "\\udc00"):
            root = self.build(escape)
            target = root / "content" / "agencies.json"
            load_agencies(json.loads(target.read_text(encoding="utf-8")), "content/agencies.json", docs.TODAY)
            before = self.snapshot(root)
            self.assertEqual(len(self.commands(root)), 9)
            for name, args in self.commands(root).items():
                with self.subTest(escape=escape, name=name), \
                        mock.patch.object(content_profile, "_install", side_effect=AssertionError("installed")):
                    code, out, err = docs.run(*args)
                    self.assertEqual((code, out, err), (1, "", f"Stopped: {FIXED}\n"))
                    self.assertEqual(self.snapshot(root), before)

    def test_api_raises_fixed_error_for_recheck_and_entries(self):
        root = self.build()
        before = self.snapshot(root)
        calls = (
            lambda: content_profile.recheck_profile(root, docs.A, "policy_check", docs.ago(10), today=docs.TODAY),
            lambda: content_profile.add_profile_entry(root, docs.A, "funding", dict(dur.FUNDING, source_url="https://example.invalid/x"),
                                                       today=docs.TODAY),
            lambda: content_profile.remove_profile_entry(root, docs.A, "funding", "1", today=docs.TODAY),
        )
        for call in calls:
            with mock.patch.object(content_profile, "_install", side_effect=AssertionError("installed")):
                try:
                    call()
                except content_profile.ContentProfileError as exc:
                    self.assertEqual(str(exc), FIXED)
                    self.assertTrue(exc.__suppress_context__)
                except Exception as exc:  # the transparency call may fail earlier validation; it must not be a codec error
                    self.assertNotIsInstance(exc, UnicodeEncodeError)
                else:
                    self.fail("expected a refusal")
        self.assertEqual(self.snapshot(root), before)

    def test_noop_recheck_and_list_unchanged(self):
        root = self.build()
        before = self.snapshot(root)
        rows = content_profile.list_profile_checks(root, today=docs.TODAY)
        self.assertTrue(rows)
        self.assertEqual(self.snapshot(root), before)

    def test_catch_is_local_to_utf8_encoding(self):
        root = self.build("ok")
        for error in (UnicodeEncodeError("utf-8", "x", 0, 1, "synthetic"), MemoryError(), RecursionError()):
            with self.subTest(where="serializer", error=type(error).__name__), \
                    mock.patch.object(content_profile.json, "dumps", side_effect=error):
                with self.assertRaises(type(error)):
                    content_profile.recheck_profile(root, docs.A, "policy_check", docs.ago(10), today=docs.TODAY)
            with self.subTest(where="installer", error=type(error).__name__), \
                    mock.patch.object(content_profile, "_install", side_effect=error):
                with self.assertRaises(type(error)):
                    content_profile.recheck_profile(root, docs.A, "policy_check", docs.ago(10), today=docs.TODAY)

    def test_valid_unicode_and_pairs_keep_exact_bytes(self):
        root = self.build("ok")
        target = root / "content" / "agencies.json"
        document = json.loads(target.read_text(encoding="utf-8"))
        document[2]["name"] = "Gamma é 日本 \U0001f600 (pair)"
        target.write_bytes(json.dumps(document, indent=2).encode("ascii"))
        result = content_profile.recheck_profile(root, docs.A, "policy_check", docs.ago(10), today=docs.TODAY)
        self.assertTrue(result["changed"])
        stored = target.read_bytes()
        expected = (json.dumps(json.loads(stored.decode("utf-8")), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.assertEqual(stored, expected)
        self.assertIn("Gamma é 日本 \U0001f600 (pair)".encode("utf-8"), stored)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
