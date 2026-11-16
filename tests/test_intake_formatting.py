"""Guard the mechanical reformat of the intake engine (folder.py).

The reformat must not change behaviour: the parsed syntax tree is frozen to the
digest taken from the pre-reformat file, and no compound (``;``) statements
remain. Source locations are ignored, so layout changes never trip the guard.
"""
import ast
import hashlib
import io
import pathlib
import sys
import tokenize
import unittest

FOLDER = pathlib.Path(__file__).resolve().parents[1] / "campaign_tool/records/intake/folder.py"
# sha256 of ast.dump(ast.parse(text), include_attributes=False) for the file as it
# stood before the reformat. ast.dump output differs between Python minors, so the
# frozen digest applies to the interpreter named here (the CI interpreter).
FROZEN_AST_MINOR = (3, 12)
FROZEN_AST_SHA256 = "555f757e0fb7fc2614530e115e1acbf6dbbca9af8ab6ef8f9ff238139774d079"


def ast_digest(text):
    return hashlib.sha256(ast.dump(ast.parse(text), include_attributes=False).encode()).hexdigest()


def semicolon_tokens(text):
    """Return (line, column) of every ';' operator token, ignoring string literals."""
    return [
        token.start
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.OP and token.string == ";"
    ]


class IntakeFormattingTests(unittest.TestCase):
    def setUp(self):
        self.text = FOLDER.read_text(encoding="utf-8")

    @unittest.skipUnless(
        sys.version_info[:2] == FROZEN_AST_MINOR,
        "frozen ast.dump digest is specific to Python %d.%d" % FROZEN_AST_MINOR,
    )
    def test_tree_matches_the_frozen_pre_reformat_digest(self):
        self.assertEqual(ast_digest(self.text), FROZEN_AST_SHA256)

    def test_no_compound_statements_remain(self):
        self.assertEqual(semicolon_tokens(self.text), [])

    def test_a_changed_limit_literal_changes_the_digest(self):
        mutated = self.text.replace("depth=5,", "depth=6,", 1)
        self.assertNotEqual(mutated, self.text)
        self.assertNotEqual(ast_digest(mutated), ast_digest(self.text))

    def test_layout_only_change_keeps_the_digest(self):
        relaid = self.text.replace("\ndef now():\n", "\n\n\ndef now():  # moved\n", 1)
        self.assertNotEqual(relaid, self.text)
        self.assertEqual(ast_digest(relaid), ast_digest(self.text))

    def test_an_added_code_semicolon_is_rejected(self):
        mutated = self.text.replace("VERSION = ", "SPARE = 1; VERSION = ", 1)
        self.assertEqual(len(semicolon_tokens(mutated)), 1)

    def test_semicolons_inside_strings_are_not_operators(self):
        text = 's = "CREATE TABLE a(x); CREATE TABLE b(y);"\nt = """\nPRAGMA a;\n"""\n'
        self.assertEqual(semicolon_tokens(text), [])
        self.assertEqual(semicolon_tokens(text + "u = 1; v = 2\n"), [(5, 5)])
        self.assertIn(";", self.text)  # SCHEMA keeps its SQL semicolons


if __name__ == "__main__":
    unittest.main()
