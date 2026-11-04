"""DF0137-15: examples/labels/es.json is a complete Spanish draft that the real labels validator accepts."""
import json
import re
import shutil
import tempfile
import unicodedata
import unittest
from collections import Counter
from datetime import date
from pathlib import Path

from campaign_tool.content import ContentError
from campaign_tool.labels import DEFAULTS, PLACEHOLDER, load_labels
from campaign_tool.site import build_site

ROOT = Path(__file__).resolve().parents[1]
DRAFT = ROOT / "examples" / "labels" / "es.json"
EXAMPLE = ROOT / "examples" / "fictional-campaign"
# Values that are the same word or form in Spanish and English (no translation needed).
SAME_AS_ENGLISH = {"agency.portal", "vote.no"}


def draft():
    return json.loads(DRAFT.read_text(encoding="utf-8"))


class SpanishDraftTest(unittest.TestCase):
    def test_real_validator_accepts_the_draft(self):
        merged = load_labels(draft())
        self.assertEqual(set(merged), set(DEFAULTS))
        self.assertEqual({k: merged[k] for k in DEFAULTS}, {**DEFAULTS, **draft()["labels"]})

    def test_validator_still_refuses_a_broken_draft(self):  # the check above is not a stub
        broken = draft()
        broken["labels"]["finding.corrected"] = "Corregido <b>{date}</b>"
        with self.assertRaises(ContentError):
            load_labels(broken)
        broken = draft()
        broken["labels"]["finding.corrected"] = "Corregido {fecha}"
        with self.assertRaises(ContentError):
            load_labels(broken)

    def test_key_set_equals_defaults(self):
        keys = set(draft()["labels"])
        self.assertEqual(sorted(set(DEFAULTS) - keys), [], "keys missing from es.json")
        self.assertEqual(sorted(keys - set(DEFAULTS)), [], "keys es.json has that DEFAULTS does not")

    def test_placeholder_multiset_equals_the_default(self):
        bad = [key for key, text in draft()["labels"].items()
               if Counter(PLACEHOLDER.findall(text)) != Counter(PLACEHOLDER.findall(DEFAULTS[key]))]
        self.assertEqual(bad, [])

    def test_no_value_is_left_in_english(self):
        same = sorted(key for key, text in draft()["labels"].items() if text == DEFAULTS[key])
        self.assertEqual(same, sorted(set(same) & SAME_AS_ENGLISH), "untranslated keys")
        self.assertTrue(SAME_AS_ENGLISH <= set(DEFAULTS))

    def test_values_are_nfc_and_trimmed(self):
        for key, text in draft()["labels"].items():
            with self.subTest(key):
                self.assertEqual(unicodedata.normalize("NFC", text), text)
                self.assertEqual(text, text.strip())
        raw = DRAFT.read_bytes()
        self.assertEqual(raw.decode("utf-8"), unicodedata.normalize("NFC", raw.decode("utf-8")))

    def test_draft_marking_is_in_the_readme_and_docs_not_the_file(self):
        sentence = ("Machine-made draft. A fluent Spanish reader must check every line before a campaign uses it; "
                    "nothing here is reviewed.")
        readme = (ROOT / "examples" / "labels" / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "SITE-CONTENT.md").read_text(encoding="utf-8")
        self.assertIn(sentence, readme.replace("**", ""))
        self.assertIn(sentence, docs)
        self.assertIn("examples/labels/es.json", docs)
        self.assertEqual(sorted(draft()), ["labels", "schema_version"])

    def test_example_site_builds_with_the_draft_and_lang_follows_site_json(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        root = tmp / "site"
        shutil.copytree(EXAMPLE, root)
        shutil.copy(DRAFT, root / "content" / "labels.json")
        site_json = json.loads((root / "content" / "site.json").read_text(encoding="utf-8"))
        build_site(root, today=date(2026, 10, 1))
        pages = sorted((root / "public").rglob("*.html"))
        self.assertGreater(len(pages), 5)
        index = (root / "public" / "index.html").read_text(encoding="utf-8")
        self.assertIn(">Inicio</a>", index)
        for page in pages:
            lang = re.search(r'<html lang="([^"]*)"', page.read_text(encoding="utf-8"))
            self.assertIsNotNone(lang, page.name)
            self.assertEqual(getattr(lang, "group")(1), site_json["language"], page.name)
        self.assertNotEqual(site_json["language"], "es")  # the draft alone never switches lang


if __name__ == "__main__":
    unittest.main()
