"""RF1900-1: surrogate-bearing statement quotes and context notes are refused before validation succeeds (synthetic, offline)."""
import json
import socket
import unittest
from unittest import mock

from campaign_tool import content_statements as reader
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from tests import test_site_statements as site_fixture

DAY = site_fixture.BUILD_DAY
FIXED = "must be valid Unicode text without surrogate code points"
SURROGATES = ("\ud800", "\udbff", "\udc00", "\udfff")


def row(**extra):
    return site_fixture.statement(**extra)


class SurrogateStatementTests(unittest.TestCase):
    # Borrow setup/helpers, not a TestCase superclass (no inherited test collection).
    sources = site_fixture.StatementsTests.sources
    roster = site_fixture.StatementsTests.roster
    build = site_fixture.StatementsTests.build
    snapshot = site_fixture.StatementsTests.snapshot

    def setUp(self):
        site_fixture.StatementsTests.setUp(self)
        self.file = self.content / "statements.json"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used")).start()

    def write(self, *rows):
        # ensure_ascii keeps the file valid UTF-8: surrogates travel as JSON escapes
        self.file.write_bytes(json.dumps(list(rows)).encode("ascii"))

    def test_quote_and_note_surrogates_are_fixed_field_refusals(self):
        for field, valid_len in (("quote", 400), ("context_note", 200)):
            for bad in SURROGATES:
                for text in (bad, bad + "x", "x" + bad, "x" + bad + "y", "x" * (valid_len - 1) + bad):
                    with self.subTest(field=field, text=text):
                        rows, problems = reader.check_statements([row(**{field: text}), row()], None, DAY)
                        self.assertEqual(problems, [f"statements.json[0].{field}: {FIXED}"])
                        self.assertEqual(rows, [row()])

    def test_both_fields_accumulate_and_read_returns_none_after_earlier_entries(self):
        self.write(row(quote="a\ud800", context_note="b\udc00"), row())
        problems = ["earlier"]
        self.assertIsNone(reader.read_statements(self.root, None, DAY, problems))
        self.assertEqual(problems, ["earlier", f"statements.json[0].quote: {FIXED}",
                                    f"statements.json[0].context_note: {FIXED}"])
        with self.assertRaises(ContentError) as caught:
            reader.read_statements(self.root, None, DAY)
        self.assertEqual(str(caught.exception), f"statements.json[0].quote: {FIXED}")

    def test_escape_spelling_decides_nothing_only_the_decoded_value(self):
        self.write(row(quote="x\ud83d\ude00y"))  # real pair: json.dumps escapes the two halves... of one scalar
        self.assertEqual(json.loads(self.file.read_bytes())[0]["quote"], "x\U0001F600y")
        self.assertEqual(reader.read_statements(self.root, None, DAY)[0]["quote"], "x\U0001F600y")
        self.file.write_bytes(b'[' + json.dumps(row(quote="X")).replace('"X"', '"\\\\ud800"').encode("ascii") + b']')
        self.assertEqual(reader.read_statements(self.root, None, DAY)[0]["quote"], "\\ud800")
        self.file.write_bytes(json.dumps([row(quote="X")]).replace('"X"', '"\\ud800"').encode("ascii"))
        with self.assertRaises(ContentError):
            reader.read_statements(self.root, None, DAY)
        # adjacent surrogate characters given directly are not combined
        _, problems = reader.check_statements([row(quote="\ud83d\ude00")], None, DAY)
        self.assertEqual(problems, [f"statements.json[0].quote: {FIXED}"])

    def test_existing_refusals_keep_precedence_and_nothing_is_echoed(self):
        canary = "canary-text-7c1e"
        _, problems = reader.check_statements(
            [row(quote="\ud800\n"), row(quote="   "), row(context_note="<\ud800>"),
             row(context_note="\ud800\t")], None, DAY)
        self.assertEqual(len(problems), 4)
        self.assertFalse(any("surrogate" in line for line in problems), problems)
        _, problems = reader.check_statements([row(quote=canary + "\ud800", context_note=canary + "\udc00")], None, DAY)
        self.assertEqual(len(problems), 2)
        for line in problems:
            self.assertNotIn(canary, line)
            self.assertNotIn("ud800", line.lower())
            self.assertNotIn("Unicode" + "EncodeError", line)

    def test_checker_and_build_refuse_and_preserve_the_public_tree(self):
        for existing in (False, True):
            if existing:
                self.write(row())
                self.build()
            for field, other in (("quote", "context_note"), ("context_note", "quote")):
                for bad in ("a\ud800b", "a\udfffb"):
                    with self.subTest(existing=existing, field=field, bad=bad):
                        self.write(row(**{field: bad}))
                        before = self.snapshot() if self.public.exists() else None
                        message = f"statements.json[0].{field}: {FIXED}"
                        self.assertIn(message, content_problems(self.root, limit=None, today=DAY))
                        with self.assertRaises(ContentError) as caught:
                            self.build()
                        self.assertEqual(str(caught.exception), message)
                        self.assertEqual(self.snapshot() if self.public.exists() else None, before)

    def test_valid_scalars_are_unchanged(self):
        texts = ("caf\u00e9 \u4e2d\u6587 e\u0301", "\U0001F600 and \U0010FFFF", "  lead  and   double  trail ", "<b> kept </b>")
        rows = [row(quote=text, context_note=text.strip() or "x") for text in texts[:3]]
        rows.append(row(quote=texts[3]))
        self.write(*rows)
        self.assertEqual(reader.read_statements(self.root, None, DAY), rows)
        self.assertEqual(reader.read_statements(self.root, None, DAY)[0]["quote"], texts[0])
        self.file.write_bytes(b"[]")
        self.assertEqual(reader.read_statements(self.root, None, DAY), [])
        self.file.unlink()
        self.assertIsNone(reader.read_statements(self.root, None, DAY))


if __name__ == "__main__":
    unittest.main()
