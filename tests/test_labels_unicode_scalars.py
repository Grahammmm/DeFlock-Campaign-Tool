"""RF0640-4: a decoded unpaired surrogate in labels.json is refused at the shared label admission."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.labels import DEFAULTS, MAX_LABEL_CHARS, load_labels
from campaign_tool.meeting_pack import _load_labels

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "fictional-campaign"
SAFE = "must contain only Unicode scalar values"


def doc(**labels):
    return {"schema_version": 1, "labels": labels}


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO,
                          text=True, capture_output=True, timeout=180)


def snapshot(root):
    return {p.relative_to(root).as_posix(): (p.lstat().st_mode, p.lstat().st_mtime_ns,
                                             p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


class SurrogateAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def campaign(self, raw=None):
        root = self.tmp / f"c{len(list(self.tmp.iterdir()))}"
        shutil.copytree(EXAMPLE, root)
        if raw is not None:
            (root / "content" / "labels.json").write_bytes(raw)
        return root

    @staticmethod
    def raw(value, key="nav.home"):
        return json.dumps(doc(**{key: value})).encode("ascii")  # ASCII-only file; json escapes the surrogate

    def test_every_surrogate_codepoint_refuses_known_label_value(self):
        for cp in range(0xD800, 0xE000):
            for text in (chr(cp), f"a{chr(cp)}b", f"{chr(cp)}x", "y" * 299 + chr(cp)):
                with self.assertRaises(ContentError) as caught:
                    load_labels(doc(**{"nav.home": text}))
                self.assertEqual(str(caught.exception), f"labels.json.labels.nav.home: {SAFE}")
        for text in ("\ud83c\udf0d", "\udf0d\ud83c"):  # literal paired / reversed code points passed directly
            with self.assertRaisesRegex(ContentError, SAFE):
                load_labels(doc(**{"nav.home": text}))
        for text in ("\ud7ff", "\ue000", "\U00010000", "\U0010ffff", "Map \U0001f30d"):
            self.assertEqual(load_labels(doc(**{"nav.home": text}))["nav.home"], text)

    def test_ascii_json_escapes_decode_before_admission(self):
        for escaped in (r"\ud800", r"\udfff", r"\udc00\ud800", r"x\ud800"):
            raw = ('{"schema_version": 1, "labels": {"nav.home": "' + escaped + '"}}').encode("ascii")
            root = self.campaign(raw)
            with self.assertRaisesRegex(ContentError, "labels.json.labels.nav.home: " + SAFE):
                load_content(root)
        pair = b'{"schema_version": 1, "labels": {"nav.home": "Map \\ud83c\\udf0d"}}'
        self.assertEqual(load_content(self.campaign(pair))["labels"]["nav.home"], "Map \U0001f30d")
        with self.assertRaises(ContentError) as caught:  # truly malformed JSON keeps its parser error
            load_content(self.campaign(b'{"schema_version": 1, "labels": {"nav.home": "\\ud8"}}'))
        self.assertNotIn(SAFE, str(caught.exception))

    def test_content_check_and_build_preview_agree_on_label_location(self):
        for key in ("nav.home", sorted(k for k in DEFAULTS if k.startswith("js."))[0],
                    sorted(k for k in DEFAULTS if k.startswith("handout."))[0]):
            root = self.campaign(self.raw("a\ud800b", key))
            expected = f"labels.json.labels.{key}: {SAFE}"
            with self.assertRaisesRegex(ContentError, expected):
                load_content(root)
            self.assertTrue(any(expected in str(p) for p in content_problems(root)))
            check = cli("content-check", "--directory", root)
            self.assertEqual(check.returncode, 3, (check.stdout, check.stderr))
            self.assertNotIn("0 problem(s)", check.stdout)
            before = snapshot(root)
            for args in (("build",), ("build", "--preview")):
                result = cli(*args, "--directory", root)
                self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
                self.assertEqual(result.stderr, f"Stopped: {expected}\n")
            self.assertEqual(snapshot(root), before)  # nothing created, nothing touched

    def test_private_pack_and_copy_from_share_admission_without_install(self):
        root = self.campaign(self.raw("a\ud800b"))
        with self.assertRaisesRegex(ContentError, "labels.json.labels.nav.home: " + SAFE):
            _load_labels(root / "content")
        new = self.tmp / "new"
        self.assertEqual(cli("init", "--directory", new, "--name", "Pine", "--county", "Pine County", "--state", "OR").returncode, 0)
        result = cli("content", "init", "--directory", new, "--from", root)
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn("Stopped:", result.stderr)
        self.assertFalse(os.path.lexists(new / "content"))
        self.assertEqual(list(new.glob(".content.new-*")), [])

    def test_known_key_error_precedence_and_existing_rules_are_unchanged(self):
        cases = [
            ({"schema_version": 2, "labels": {}}, "labels.json.schema_version: must be 1"),
            (doc(**{"nav.hom": "a\ud800"}), "labels.json.labels.nav.hom: unknown label"),
            (doc(**{"nav.home": 7}), "must be text of 1-300 characters"),
            (doc(**{"nav.home": "  "}), "must be text of 1-300 characters"),
            (doc(**{"nav.home": "x" * (MAX_LABEL_CHARS + 1)}), "must be text of 1-300 characters"),
            (doc(**{"nav.home": "a<b"}), "must not contain <, > or control characters"),
            (doc(**{"nav.home": "a\x07"}), "must not contain <, > or control characters"),
            (doc(**{"nav.home": "a {bogus}"}), "placeholder {bogus} is not allowed here"),
            (doc(**{"nav.home": "a {"}), "unpaired brace {"),
            (doc(**{"nav.home": "a<\ud800"}), SAFE),  # surrogate wins over markup ...
            (doc(**{"nav.home": "{bogus}\ud800"}), SAFE),  # ... and placeholder checks
        ]
        for document, needle in cases:
            with self.subTest(needle=needle):
                with self.assertRaises(ContentError) as caught:
                    load_labels(document)
                self.assertIn(needle, str(caught.exception))
                self.assertNotIn("\\ud800", str(caught.exception))

    def test_valid_unicode_and_legacy_values_are_unchanged(self):
        values = ["Caf\u00e9", "e\u0301", "\u65e5\u672c\u8a9e", "\U0001f30d", "\U0001f468\u200d\U0001f469", "a\ufe0f",
                  "  pad  ", "z" * MAX_LABEL_CHARS, "Tom & \"Jerry\""]
        for value in values:
            self.assertEqual(load_labels(doc(**{"nav.home": value}))["nav.home"], value)
        self.assertEqual(load_labels({"schema_version": 1, "labels": {}}), dict(DEFAULTS))

    def test_refusal_is_nonsticky(self):
        root = self.campaign(self.raw("a\ud800b"))
        self.assertEqual(cli("build", "--directory", root).returncode, 1)
        (root / "content" / "labels.json").write_bytes(self.raw("Inicio"))
        ok = cli("build", "--directory", root)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn(">Inicio</a>", (root / "public" / "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
