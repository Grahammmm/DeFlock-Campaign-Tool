import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records import playbook as pb

MARK = "@@"
CANARY = "canary-secret-member"
DRAFT = "This is illegal."


def _raw(book, dup_text='"{k}": 1, "{k}": 2'.format(k=CANARY), where=None):
    """Serialise a book with a repeated member written into the object found by ``where(book)``."""
    book = copy.deepcopy(book)
    where(book)[MARK] = 0
    return json.dumps(book).replace('"' + MARK + '": 0', dup_text)


def _top(book):
    return book


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = pb.load()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)

    def write(self, text, name="book.json"):
        path = Path(self.tmp.name) / name
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    def cli(self, *argv, stdin="x"):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["records playbook", *argv]), mock.patch("sys.stdin", io.StringIO(stdin)), \
                redirect_stdout(out), redirect_stderr(err):
            code = pb.main()
        return code, out.getvalue(), err.getvalue()


class DuplicateMemberTests(_Base):
    def test_duplicate_banned_phrases_cannot_disable_lint_rule(self):
        healthy = json.dumps(self.book)
        kept = [b for b in self.book["banned_phrases"] if b["id"] != "illegal"]
        shadow = healthy[:-1] + ', "banned_phrases": ' + json.dumps(kept) + "}"
        path = self.write(shadow)
        with self.assertRaises(pb.PlaybookError) as raised:
            pb.load(path)
        self.assertEqual((raised.exception.code, raised.exception.detail), ("duplicate_json_member", ""))
        for action in (("check",), ("lint", "-")):
            code, out, err = self.cli(*action, "--playbook", str(path), stdin=DRAFT)
            self.assertEqual((code, out, err), (2, "", "records playbook: refused duplicate_json_member\n"))
        control = self.write(healthy, "control.json")
        code, out, err = self.cli("lint", "-", "--playbook", str(control), stdin=DRAFT)
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(out.splitlines()[-1], "needs edit")
        self.assertIn("illegal at 8: legal conclusion", out)

    def test_nested_duplicate_members_refuse_before_validation(self):
        places = {
            "tier": lambda b: b["tiers"][0],
            "citation": lambda b: next(iter(b["citations"].values())),
            "first_request item": lambda b: b["first_request"]["items"][0],
            "followup pattern": lambda b: b["followup_patterns"][0],
            "banned phrase": lambda b: b["banned_phrases"][0],
            "extension object": lambda b: b.setdefault("x_ext", {"inner": {}})["inner"],
        }
        for label, where in places.items():
            with self.subTest(label), mock.patch.object(pb, "validate") as validate, \
                    mock.patch.object(pb.re, "compile") as compile_, mock.patch.object(pb, "lint") as lint:
                path = self.write(_raw(self.book, where=where))
                with self.assertRaises(pb.PlaybookError) as raised:
                    pb.load(path)
                self.assertEqual((raised.exception.code, raised.exception.detail), ("duplicate_json_member", ""))
                validate.assert_not_called()
                compile_.assert_not_called()
                lint.assert_not_called()

    def test_escaped_equal_and_differing_duplicate_names_refuse(self):
        esc = CANARY.replace("c", "\\u0063")
        variants = (
            '"{c}": 1, "{c}": 1',
            '"{c}": 1, "{c}": 2',
            '"{c}": 1, "{e}": 2',
            '"{e}": 2, "{c}": 1',
            '"{e}": 1, "{e}": 1',
        )
        for text in (v.format(c=CANARY, e=esc) for v in variants):
            with self.subTest(text):
                path = self.write(_raw(self.book, text, _top), "dup-" + CANARY + ".json")
                with self.assertRaises(pb.PlaybookError) as raised:
                    pb.load(path)
                self.assertEqual((raised.exception.code, raised.exception.detail), ("duplicate_json_member", ""))
                self.assertNotIn(CANARY, str(raised.exception))
                code, out, err = self.cli("check", "--playbook", str(path))
                self.assertEqual((code, out, err), (2, "", "records playbook: refused duplicate_json_member\n"))
                self.assertNotIn(CANARY, err)

    def test_same_names_in_distinct_objects_are_valid(self):
        self.assertGreater(len(self.book["tiers"]), 1)
        book = copy.deepcopy(self.book)
        for tier in book["tiers"]:
            tier["x_same"] = {"id": 1, "name": 2}
        book["x_escape"] = {"a\\b": 1, "a\\\\b": 2, "a/b": 3, "a\\/b": 4}
        loaded = pb.load(self.write(json.dumps(book), "scopes.json"))
        self.assertEqual(loaded, book)

    def test_existing_structural_duplicate_ids_keep_their_codes(self):
        book = copy.deepcopy(self.book)
        book["followup_patterns"].append(copy.deepcopy(book["followup_patterns"][0]))
        with self.assertRaises(pb.PlaybookError) as raised:
            pb.load(self.write(json.dumps(book), "pat.json"))
        self.assertEqual(raised.exception.code, "pattern_duplicate")
        book = copy.deepcopy(self.book)
        book["banned_phrases"].append(copy.deepcopy(book["banned_phrases"][0]))
        with self.assertRaises(pb.PlaybookError) as raised:
            pb.load(self.write(json.dumps(book), "ban.json"))
        self.assertEqual((raised.exception.code, raised.exception.detail), ("banned_invalid", "duplicate id"))

    def test_shipped_and_custom_unambiguous_books_are_unchanged(self):
        self.assertEqual(pb.load(), pb.load(pb.DEFAULT_PATH))
        self.assertEqual(pb.load(), json.loads(pb.DEFAULT_PATH.read_text(encoding="utf-8")))
        custom = copy.deepcopy(self.book)
        custom["banned_phrases"] = [{"id": "only", "pattern": "caf\u00e9", "reason": "r"}]
        data = json.dumps(custom, indent=2, ensure_ascii=False).encode("utf-8") + b"\n\n"
        path = self.write(data, "custom.json")
        loaded = pb.load(path)
        self.assertEqual(loaded, custom)
        self.assertEqual(list(loaded), list(custom))
        self.assertEqual(pb.lint(loaded, "Un caf\u00e9 is illegal")["hits"],
                         [{"id": "only", "reason": "r", "start": 3, "text": "caf\u00e9"}])
        self.assertEqual(path.read_bytes(), data)

    def test_read_size_syntax_and_dispatch_contracts_unchanged(self):
        cases = (
            (Path(self.tmp.name) / "missing.json", "unreadable"),
            (self.write(b"\xff\xfe{}", "bad-utf8.json"), "not_json"),
            (self.write("{not json", "broken.json"), "not_json"),
            (self.write(b" " * (pb.MAX_BYTES + 1), "big.json"), "too_large"),
            (self.write("[]", "list.json"), "not_object"),
            (self.write('{"schema": "records-playbook-v1"}', "thin.json"), "field_missing"),
        )
        for path, code in cases:
            with self.subTest(code):
                with self.assertRaises(pb.PlaybookError) as raised:
                    pb.load(path)
                self.assertEqual(raised.exception.code, code)
                self.assertNotEqual(raised.exception.code, "duplicate_json_member")
        code, out, err = self.cli("check")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("playbook ok: "))


if __name__ == "__main__":
    unittest.main()
