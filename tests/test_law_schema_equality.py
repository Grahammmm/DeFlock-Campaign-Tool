"""law.validate const/enum keep JSON booleans and numbers distinct, recursively.

Schema validation only; no legal review. Run from the repository root:
python3 -B -m unittest tests.test_law_schema_equality -v
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law  # noqa: E402


def refused(document, schema):
    try:
        law.validate(document, schema)
    except ValueError as exc:
        return str(exc)
    return None


class ConstEnumEquality(unittest.TestCase):
    def test_const_boolean_number_aliases_refuse(self):
        for document, const in ((True, 1), (True, 1.0), (False, 0), (False, 0.0),
                                (1, True), (1.0, True), (0, False), (0.0, False)):
            with self.subTest(document=document, const=const):
                message = refused(document, {"const": const})
                self.assertIsNotNone(message)
                self.assertIn("expected constant", message)

    def test_enum_boolean_number_aliases_refuse(self):
        for document, options in ((False, [0]), (True, [1.0]), (0, [False]), (1, [True, "x"]),
                                  (True, [0, 2]), (True, [])):
            with self.subTest(document=document, options=options):
                message = refused(document, {"enum": options})
                self.assertIsNotNone(message)
                self.assertIn("is not one of", message)
        self.assertIsNone(refused(True, {"enum": [1, 0, True]}))
        self.assertIsNone(refused(1, {"enum": [True, 1]}))

    def test_nested_values_compare_by_json_kind(self):
        bad = [({"x": [True]}, {"x": [1]}), ([False], [0]),
               ([1, 2, True], [1, 2, 1]), ([1, True, 3], [1, 1, 3]), ([True, 2], [1, 2]),
               ({"a": {"b": [{"c": False}]}}, {"a": {"b": [{"c": 0}]}}),
               ([1, 2], [1, 2, 3]), ([1, 2], [2, 1]), ({"a": 1}, {"a": 1, "b": 2}),
               ({"a": 1}, {"b": 1}), ([], {}), (None, 0), ("", []), ({}, None), ([1], 1)]
        for document, const in bad:
            for keyword, schema in (("const", {"const": const}), ("enum", {"enum": [const]})):
                with self.subTest(document=document, const=const, keyword=keyword):
                    snapshot = copy.deepcopy((document, schema))
                    self.assertIsNotNone(refused(document, schema))
                    self.assertEqual(snapshot, (document, schema))
        good = ({"a": [1, {"b": True}], "c": None}, {"c": None, "a": [1.0, {"b": True}]})
        self.assertIsNone(refused(good[0], {"const": good[1]}))
        self.assertIsNone(refused(good[0], {"enum": [0, good[1]]}))

    def test_deep_tree_without_recursion(self):
        def tree(leaf, depth=5000):
            node = leaf
            for _ in range(depth):
                node = [node]
            return node
        self.assertTrue(law._json_equal(tree(1), tree(1.0)))
        self.assertFalse(law._json_equal(tree(True), tree(1)))

    def test_numeric_and_boolean_controls_unchanged(self):
        for document, const in ((1, 1.0), (1.0, 1), (-0.0, 0), (True, True), (False, False),
                                ("a", "a"), (None, None)):
            self.assertIsNone(refused(document, {"const": const}))
            self.assertIsNone(refused(document, {"enum": [const]}))
        self.assertIsNotNone(refused(2, {"const": 1}))
        self.assertIsNotNone(refused(2, {"enum": [1, 3]}))
        # type policy is untouched
        self.assertIsNotNone(refused(True, {"type": "integer"}))
        self.assertIsNotNone(refused(1.5, {"type": "integer"}))
        self.assertIsNone(refused(1, {"type": "integer"}))

    def test_ref_properties_and_items(self):
        schema = {"definitions": {"one": {"const": 1}},
                  "properties": {"a": {"$ref": "#/definitions/one"},
                                 "b": {"items": {"enum": [0]}}}}
        self.assertIsNone(refused({"a": 1, "b": [0, 0.0]}, schema))
        self.assertIn("$.a", refused({"a": True}, schema))
        self.assertIn("$.b[1]", refused({"b": [0, False]}, schema))


class PackageAdmission(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def write(self, package):
        folder = self.base / "us-ca"
        folder.mkdir(exist_ok=True)
        (folder / "package.json").write_text(json.dumps(package), encoding="utf-8")

    def test_shipped_package_loads_and_boolean_version_refuses(self):
        package = copy.deepcopy(law.load_package("us-ca"))
        self.write(package)
        self.assertEqual(law.load_package("us-ca", self.base)["schema_version"], 1)
        shown = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "show", "us-ca",
                                "--base", str(self.base)], cwd=REPO_ROOT,
                               capture_output=True, text=True)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        package["schema_version"] = True
        self.write(package)
        with self.assertRaises(ValueError) as caught:
            law.load_package("us-ca", self.base)
        self.assertIn("schema_version", str(caught.exception))
        shown = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "show", "us-ca",
                                "--base", str(self.base)], cwd=REPO_ROOT,
                               capture_output=True, text=True)
        self.assertEqual(shown.returncode, 1)
        self.assertIn("Stopped:", shown.stderr)


if __name__ == "__main__":
    unittest.main()
