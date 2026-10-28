"""Every schemas/*.schema.json must be well-formed JSON with a usable top level."""

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law  # noqa: E402

SCHEMAS = REPO_ROOT / "schemas"


class SchemaFileTests(unittest.TestCase):
    def test_every_schema_file_loads(self):
        paths = sorted(SCHEMAS.glob("*.schema.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(schema=path.name):
                document = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(document, dict)
                self.assertEqual(document.get("type"), "object", path.name)
                self.assertTrue(document.get("properties") or document.get("required"), path.name)

    def test_law_package_schema_refs_resolve(self):
        schema = law.load_schema()
        self.assertEqual(schema["properties"]["schema_version"], {"const": 1})
        self.assertIn("source", schema["$defs"])
        self.assertIn("date", schema["$defs"])
        # A minimal document that satisfies the schema must validate.
        minimal = {
            "schema_version": 1, "jurisdiction": "us-zz", "status": "draft",
            "reviewed_by": [], "reviewed_at": None,
            "records_law": {
                "name": "Synthetic Records Act", "citation": "Synth. Code § 1",
                "determination_days": 5, "determination_extension_days": 0,
                "day_type": "business", "fee_basis": "none", "appeal": "none",
                "sources": [{"title": "s", "url": "https://example.invalid/law",
                             "accessed": "2026-09-30"}]},
            "rules": [], "request_scopes": []}
        law.validate(minimal, schema)
        with self.assertRaises(ValueError):
            law.validate({**minimal, "jurisdiction": "USZZ"}, schema)


if __name__ == "__main__":
    unittest.main()
