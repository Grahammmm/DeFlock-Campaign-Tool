"""FC4-CATALOG-JSON: catalog JSONL input must be unambiguous before roles are assigned.

Synthetic data only. The fixture shapes (records-index database, prior queue,
review snapshot, support receipt) are reused from tests.records.test_catalog so
these cases exercise the real ``jsonl`` and ``import_catalog`` entry points.
"""
import json
import math
import os
import unittest

from campaign_tool.records.catalog import digest_file, import_catalog, jsonl
from tests.records import test_catalog as catalog_fixture


def diagnostic(number):
    return "invalid_catalog_json line " + str(number)


# One representative value per JSON type; duplicates must be refused whatever
# the type of either occurrence.
TYPED_VALUES = ('"agency_original"', '"unresolved"', '7', '-2.5', 'true', 'false',
                'null', '[]', '["x", 1]', '{}', '{"inner": "v"}')
NONFINITE_CONSTANTS = ("NaN", "Infinity", "-Infinity")
OVERFLOW_LITERALS = ("1e400", "-1e400", "1E+309", "-1.5e309", "2e308",
                     "1.7976931348623159e308", "-1.7976931348623159e308",
                     "1" + "0" * 400 + ".0", "-" + "9" * 320 + ".5")


class CatalogJsonAmbiguityTests(unittest.TestCase):
    def setUp(self):
        catalog_fixture.CatalogTests.setUp(self)
        self.sha = self.hashes[0]

    # helpers ---------------------------------------------------------------
    def write_lines(self, name, lines):
        path = self.root / name
        path.write_bytes("".join(line + "\n" for line in lines).encode("utf-8"))
        return path

    def assert_rejected(self, lines, number, *, secrets=()):
        path = self.write_lines("probe-%d.jsonl" % len(os.listdir(self.root)), lines)
        before = path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            jsonl(path)
        message = str(caught.exception)
        self.assertEqual(message, diagnostic(number))
        self.assertIs(type(caught.exception), ValueError)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        for secret in secrets:
            self.assertNotIn(secret, message)
        self.assertEqual(path.read_bytes(), before)

    def run_import(self, output=None):
        return import_catalog(self.database, self.queue, self.snapshot, self.digests,
                              output or self.output, support_root=self.support,
                              blob_root=self.blobs)

    def snapshot_line(self, sha, role_fields):
        return '{"sha256": "%s", %s, "publication_readiness": "not_ready"}' % (sha, role_fields)

    def tree_hashes(self, root):
        return {str(p.relative_to(root)): digest_file(p) for p in sorted(root.rglob("*")) if p.is_file()}

    # frozen named cases -----------------------------------------------------
    def test_unique_rows_and_blank_line_numbers(self):
        decomposed = "e\u0301"
        lines = ['{"sha256": "%s", "effective_role": "agency_original"}' % self.sha,
                 "",
                 "   \t ",
                 '{"name": "Caf\u00e9 \u2603", "esc": "\\u00e9\\ud83d\\ude00", "%s": 1, "\u00e9": 2}' % decomposed,
                 '{"nested": {"a": [1, {"a": 2}], "b": {"a": 3}}, "list": [{"k": 1}, {"k": 2}]}']
        path = self.root / "unique.jsonl"
        path.write_bytes(("\n".join(lines)).encode("utf-8"))  # final row has no newline
        rows = jsonl(path)
        self.assertEqual([number for number, _ in rows], [1, 4, 5])
        self.assertEqual(rows[0], (1, {"sha256": self.sha, "effective_role": "agency_original"}))
        self.assertEqual(rows[1][1], {"name": "Caf\u00e9 \u2603", "esc": "\u00e9\U0001F600",
                                      decomposed: 1, "\u00e9": 2})
        self.assertEqual(rows[2][1], {"nested": {"a": [1, {"a": 2}], "b": {"a": 3}},
                                      "list": [{"k": 1}, {"k": 2}]})
        self.assertTrue(all(isinstance(row, tuple) and len(row) == 2 for row in rows))
        empty = self.write_lines("blank.jsonl", ["", "  ", ""])
        self.assertEqual(jsonl(empty), [])

    def test_duplicate_role_both_orders(self):
        for first, second in (("unresolved", "agency_original"), ("agency_original", "unresolved")):
            with self.subTest(order=(first, second)):
                row = self.snapshot_line(self.sha, '"effective_role": "%s", "effective_role": "%s"' % (first, second))
                self.assert_rejected(['{"sha256": "%s"}' % self.hashes[1], "", row], 3,
                                     secrets=("effective_role", first, second, self.sha))
        # same class across every declared value type, both positions, unrelated fields fixed
        for left in TYPED_VALUES:
            for right in TYPED_VALUES:
                with self.subTest(left=left, right=right):
                    row = self.snapshot_line(self.sha, '"effective_role": %s, "effective_role": %s' % (left, right))
                    self.assert_rejected([row], 1)
        # the sha256 identity key itself is also refused when doubled
        self.assert_rejected(['{"sha256": "%s", "sha256": "%s"}' % (self.hashes[0], self.hashes[1])], 1,
                             secrets=("sha256", self.hashes[0], self.hashes[1]))
        # through the real import: neither order may select a role
        for index, (first, second) in enumerate((("unresolved", "agency_original"),
                                                 ("agency_original", "unresolved"))):
            with self.subTest(import_order=(first, second)):
                self.snapshot.write_text(
                    self.snapshot_line(self.hashes[0], '"effective_role": "%s"' % "agency_original") + "\n"
                    + self.snapshot_line(self.hashes[1], '"effective_role": "%s", "effective_role": "%s"' % (first, second)) + "\n")
                output = self.root / ("order-output-%d" % index)
                with self.assertRaises(ValueError) as caught:
                    self.run_import(output)
                self.assertEqual(str(caught.exception), diagnostic(2))
                self.assertFalse((output / "CURRENT.json").exists())

    def test_equal_duplicate_values(self):
        for value in TYPED_VALUES:
            with self.subTest(value=value):
                self.assert_rejected([self.snapshot_line(self.sha, '"effective_role": %s, "effective_role": %s' % (value, value))], 1)
        self.assert_rejected(['{"a": 1, "\\u0061": 1}'], 1)  # equal after escape decoding
        self.assert_rejected(['{"k": {"x": [1, 2]}, "k": {"x": [1, 2]}}'], 1)
        self.assert_rejected(['{"a": 1, "b": 2, "a": 1}'], 1)  # non-adjacent

    def test_nested_duplicate_keys(self):
        shapes = ('{"outer": {"role": "a", "role": "b"}}',
                  '{"rows": [{"role": "a", "role": "a"}]}',
                  '{"rows": [1, {"ok": 1}, {"deep": {"x": 1, "x": 2}}]}',
                  '{"a": {"b": {"c": {"d": {"e": 1, "e": 1}}}}}',
                  '{"a": [[[{"k": null, "k": true}]]]}',
                  '{"m": {"\\u00e9": 1, "\u00e9": 2}}')
        for shape in shapes:
            with self.subTest(shape=shape):
                self.assert_rejected(['{"sha256": "%s"}' % self.sha, shape], 2)
        # siblings at different depths that share a name are not duplicates
        self.assertEqual(jsonl(self.write_lines("siblings.jsonl", ['{"k": {"k": {"k": 1}}, "j": [{"k": 1}, {"k": 2}]}'])),
                         [(1, {"k": {"k": {"k": 1}}, "j": [{"k": 1}, {"k": 2}]})])

    def test_nan_and_infinities(self):
        for constant in NONFINITE_CONSTANTS:
            for template in ('{"score": %s}', '{"values": [1, %s]}', '{"a": {"b": %s}}',
                             '{"sha256": "' + self.sha + '", "effective_role": "agency_original", "bytes": %s}'):
                with self.subTest(constant=constant, template=template):
                    self.assert_rejected(["", template % constant], 2, secrets=(constant,))
        for constant in NONFINITE_CONSTANTS:  # top-level constant is a nonfinite number, not a row
            with self.subTest(top=constant):
                self.assert_rejected([constant], 1)

    def test_exponent_overflow(self):
        for literal in OVERFLOW_LITERALS:
            for template in ('{"bytes": %s}', '{"values": [0, %s]}', '{"a": {"b": [%s]}}'):
                with self.subTest(literal=literal[:24], template=template):
                    self.assert_rejected(['{"ok": 1}', template % literal], 2, secrets=(literal,))

    def test_malformed_json_and_nonobject(self):
        for text in ('{"a": ', '{"a": 1,}', "{'a': 1}", '{"a": 1} {"b": 2}', "nan", "inf",
                     '{"a": 01}', '{"a": .5}', '{"a": +1}'):
            with self.subTest(syntax=text):
                path = self.write_lines("syntax.jsonl", ['{"ok": 1}', text])
                with self.assertRaises(ValueError) as caught:
                    jsonl(path)
                self.assertIsInstance(caught.exception, json.JSONDecodeError)
        for text in ("[1, 2]", '"text"', "42", "-1.5", "null", "true", "false", "[]", '[{"a": 1}]'):
            with self.subTest(nonobject=text):
                path = self.write_lines("nonobject.jsonl", [text])
                with self.assertRaises(ValueError) as caught:
                    jsonl(path)
                self.assertEqual(str(caught.exception), "JSONL rows must be objects")

    def test_finite_boundary_values(self):
        literals = ("1.7976931348623157e308", "-1.7976931348623157e308", "1.7976931348623158e308",
                    "5e-324", "2.2250738585072014e-308", "1e-400", "-1e-400", "-0.0", "0.0",
                    "1e308", "0", "-0", "123456789012345678901234567890", "1" + "0" * 400)
        for literal in literals:
            with self.subTest(literal=literal[:24]):
                path = self.write_lines("finite.jsonl", ['{"v": %s, "w": [%s], "role": "agency_original"}' % (literal, literal)])
                [(number, row)] = jsonl(path)
                expected = json.loads(literal)
                self.assertEqual(number, 1)
                self.assertEqual(type(row["v"]), type(expected))
                self.assertEqual(row["v"], expected)
                self.assertEqual(row["w"], [expected])
                if isinstance(expected, float):
                    self.assertTrue(math.isfinite(row["v"]))
                    self.assertEqual(math.copysign(1.0, row["v"]), math.copysign(1.0, expected))
        self.assertEqual(jsonl(self.write_lines("max.jsonl", ['{"v": 1.7976931348623157e308}']))[0][1]["v"],
                         1.7976931348623157e308)

    def test_valid_import_then_rejected_import_preserves_current(self):
        first = self.run_import()
        destination = self.output / first["snapshot_id"]
        cards = json.loads((destination / "catalog.json").read_text())["cards"]
        self.assertEqual(sorted(c["sha256"] for c in cards), sorted(self.hashes))
        self.assertEqual({c["role"] for c in cards}, {"agency_original"})
        manifest = json.loads((destination / "input-manifest.json").read_text())
        self.assertEqual([(i["kind"], i["sha256"]) for i in manifest["inputs"]],
                         [("prior_queue", digest_file(self.queue)), ("review_snapshot", digest_file(self.snapshot)),
                          ("preserved_support_not_adjudicated", digest_file(self.support / "receipt.json"))])
        self.assertEqual(sorted(p.name for p in destination.iterdir()),
                         sorted(["COVERAGE-RECONCILIATION.json", "DOCUMENT-REVIEW-QUEUE.jsonl",
                                 "artifact-hashes.json", "board.html", "catalog.json", "input-manifest.json",
                                 "intake.sqlite", "inventory.jsonl", "objects"]))
        pointer = json.loads((self.output / "CURRENT.json").read_text())
        self.assertEqual(pointer["snapshot_id"], first["snapshot_id"])
        current_bytes = (self.output / "CURRENT.json").read_bytes()
        accepted = self.tree_hashes(destination)
        entries = set(os.listdir(self.output))

        agency_dir = self.digests / "synthetic-agency"
        agency_dir.mkdir()
        cases = (("snapshot", self.snapshot, [self.snapshot_line(self.hashes[0], '"effective_role": "agency_original"'),
                                              self.snapshot_line(self.hashes[1], '"effective_role": "unresolved", "effective_role": "agency_original"')], 2),
                 ("queue", self.queue, ['{"sha256": "%s", "agency_hints": ["A"], "agency_hints": ["B"]}' % self.hashes[0]], 1),
                 ("digests", agency_dir / "document-digests.jsonl", ['{"sha256": "%s"}' % self.hashes[0], "",
                                                                     '{"sha256": "%s", "score": 1e999}' % self.hashes[1]], 3))
        originals = {path: (path.read_bytes(), path.stat().st_atime_ns, path.stat().st_mtime_ns)
                     for path in (self.snapshot, self.queue)}
        for name, path, lines, number in cases:
            with self.subTest(input=name):
                path.write_bytes("".join(line + "\n" for line in lines).encode())
                ambiguous = path.read_bytes()
                with self.assertRaises(ValueError) as caught:
                    self.run_import()
                self.assertEqual(str(caught.exception), diagnostic(number))
                self.assertEqual(path.read_bytes(), ambiguous)
                self.assertEqual((self.output / "CURRENT.json").read_bytes(), current_bytes)
                self.assertEqual(self.tree_hashes(destination), accepted)
                residue = set(os.listdir(self.output)) - entries
                self.assertTrue(all(entry.startswith(".catalog-stage-") for entry in residue), residue)
                if path in originals:  # exact bytes and mtime, because the manifest binds both
                    data, atime, mtime = originals[path]
                    path.write_bytes(data)
                    os.utime(path, ns=(atime, mtime))
                else:
                    path.write_text('{"sha256": "%s"}\n' % self.hashes[0])
        # the restored valid inputs import again and still yield the original snapshot
        (agency_dir / "document-digests.jsonl").rename(self.root / "digests-moved-aside.jsonl")
        again = self.run_import()
        self.assertEqual(again["snapshot_id"], first["snapshot_id"])
        self.assertTrue(again["reused"])
        self.assertEqual((self.output / "CURRENT.json").read_bytes(), current_bytes)

    def test_input_bytes_unchanged(self):
        inputs = {"accepted": ['{"sha256": "%s", "effective_role": "agency_original"}' % self.sha, ""],
                  "duplicate": ['{"effective_role": "unresolved", "effective_role": "agency_original"}'],
                  "nonfinite": ['{"v": NaN}'], "overflow": ['{"v": 1e400}'], "syntax": ['{"v": ']}
        for name, lines in inputs.items():
            with self.subTest(input=name):
                path = self.write_lines(name + ".jsonl", lines)
                os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
                before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size, path.stat().st_ino)
                try:
                    jsonl(path)
                except ValueError:
                    pass
                after = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size, path.stat().st_ino)
                self.assertEqual(after, before)
        snapshot_before = self.snapshot.read_bytes()
        self.snapshot.write_bytes(snapshot_before.replace(b'"effective_role": "agency_original"',
                                                          b'"effective_role": "agency_original", "effective_role": "unresolved"', 1))
        changed = self.snapshot.read_bytes()
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertEqual(self.snapshot.read_bytes(), changed)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.suffix == ".jsonl") ,
                         sorted(["accepted.jsonl", "duplicate.jsonl", "nonfinite.jsonl", "overflow.jsonl",
                                 "queue.jsonl", "snapshot.jsonl", "syntax.jsonl"]))


CASE_MAP = {
    "unique_rows_and_blank_line_numbers": "test_unique_rows_and_blank_line_numbers",
    "duplicate_role_both_orders": "test_duplicate_role_both_orders",
    "equal_duplicate_values": "test_equal_duplicate_values",
    "nested_duplicate_keys": "test_nested_duplicate_keys",
    "nan_and_infinities": "test_nan_and_infinities",
    "exponent_overflow": "test_exponent_overflow",
    "malformed_json_and_nonobject": "test_malformed_json_and_nonobject",
    "finite_boundary_values": "test_finite_boundary_values",
    "valid_import_then_rejected_import_preserves_current": "test_valid_import_then_rejected_import_preserves_current",
    "input_bytes_unchanged": "test_input_bytes_unchanged",
}


if __name__ == "__main__":
    unittest.main()
