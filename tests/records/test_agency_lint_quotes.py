import contextlib
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import cli
from campaign_tool.records.agency_run import lint_quotes

PASSAGE = "the vendor keeps audit logs, for two years"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pages = self.root / "pages"
        self.pages.mkdir()

    def page(self, doc, n, text, marker=True):
        head = "=== %s p.%d ===\n" % (doc, n) if marker else ""
        (self.pages / ("%s_p%03d.txt" % (doc, n))).write_text(head + text + "\n", encoding="utf-8")

    def run_lint(self, draft, pages=None):
        path = self.root / "draft.txt"
        path.write_text(draft, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lint_quotes.main(["--draft", str(path), "--pages", str(pages or self.pages)])
        return code, out.getvalue(), err.getvalue()


class LintQuotes(Base):
    def setUp(self):
        super().setUp()
        self.page("D1", 1, "Intro line.\n" + PASSAGE + " and more text.")
        self.page("D1", 2, "Other page content here.")

    def test_four_defects_one_hit_each(self):
        draft = ('A "the vendor keeps audit logs; for two years" (D1 p.1)\n'
                 'B "the vendor keeps for two years" (D1 p.1)\n'
                 'C "the vendor keeps audit logs, for two years" (D1 p.2)\n'
                 'D "the vendor keeps audit logs, for two years" is said.\n')
        code, out, _ = self.run_lint(draft)
        lines = out.splitlines()
        self.assertEqual(code, 1)
        self.assertEqual(lines[:4], ["1:3 Q1 not_verbatim D1 p.1", "2:3 Q1 not_verbatim D1 p.1",
                                     "3:3 Q1 not_verbatim D1 p.2", "4:3 Q2 no_pinpoint"])
        self.assertIn("4 hits", lines[4])

    def test_exact_quotes_pass(self):
        self.page("D1", 12, "the vendor keeps\n  audit   logs, for two years\nelse")
        self.page("D1", 3, "alpha beta gamma delta epsilon zeta eta")
        draft = ('"the vendor keeps audit logs, for two years" (D1 p.1)\n'
                 '"the vendor keeps  audit logs,\nfor two years" (D1 p. 12, item 6)\n'
                 '\u201cthe vendor keeps audit logs\u201d (D1 p.12)\n'
                 '"alpha beta [...] epsilon ... eta" (D1 p.3)\n')
        code, out, _ = self.run_lint(draft)
        self.assertEqual(code, 0, out)
        self.assertIn("0 hits, 4 quotes checked", out)

    def test_marker_line_is_not_text(self):
        code, out, _ = self.run_lint('"=== D1 p.1 ===" (D1 p.1)\n')
        self.assertEqual(code, 1)
        self.assertEqual(len([x for x in out.splitlines() if " Q1 " in x]), 1)

    def test_case_and_glyph_changes_are_hits(self):
        self.page("D2", 1, "the officer's report stands")
        code, out, _ = self.run_lint('"The officer\'s report stands" (D2 p.1)\n'
                                     '"the officer\u2019s report stands" (D2 p.1)\n'
                                     '"the officer\'s report stands" (D2 p.1)\n')
        self.assertEqual(code, 1)
        self.assertEqual(sum(" Q1 " in x for x in out.splitlines()), 2)

    def test_marked_omission_order_and_missing_segment(self):
        self.page("D3", 1, "alpha beta gamma delta epsilon zeta")
        code, out, _ = self.run_lint('"epsilon [...] alpha beta" (D3 p.1)\n"alpha beta ... omega" (D3 p.1)\n'
                                     '"alpha beta \u2026 zeta" (D3 p.1)\n')
        self.assertEqual(code, 1)
        self.assertEqual(sum(" Q1 " in x for x in out.splitlines()), 2)

    def test_message_quote_passes_pinpoint_and_is_counted(self):
        code, out, _ = self.run_lint('"nothing like any page" (msg abc123)\n"another unrelated quote here" (msg abc123, para 2)\n')
        self.assertEqual(code, 0, out)
        self.assertIn("2 message quotes not checked against pages", out)

    def test_short_quotes_ignored(self):
        code, out, _ = self.run_lint('a "so called" thing\n')
        self.assertEqual(code, 0)
        code, out, _ = self.run_lint('a "one two three" thing\n')
        self.assertEqual((code, out.splitlines()[0]), (1, "1:3 Q2 no_pinpoint"))

    def test_missing_page_file_is_q1(self):
        code, out, _ = self.run_lint('"one two three" (D9 p.1)\n')
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[0], "1:1 Q1 no_page_file D9 p.1")

    def test_output_never_contains_quote_text(self):
        code, out, err = self.run_lint('"zzcanaryzz one two" (D1 p.1)\n"qqcanaryqq one two" is bare\n')
        self.assertEqual(code, 1)
        self.assertNotIn("canary", out + err)
        self.assertEqual(out.splitlines()[-1], "text check only; no visual check of the page images was made")

    def test_refusals(self):
        code, out, _ = self.run_lint('"one two three\n')
        self.assertEqual((code, out.strip()), (2, "refused lint_quotes_unbalanced 1"))
        code, out, _ = self.run_lint("x" * (lint_quotes.DRAFT_MAX + 1))
        self.assertEqual((code, out.strip()), (2, "refused lint_quotes_draft_invalid"))
        code, out, _ = self.run_lint("x", pages=self.root / "nope")
        self.assertEqual((code, out.strip()), (2, "refused lint_quotes_pages_dir_invalid"))
        link = self.root / "link"
        os.symlink(self.pages, link)
        code, out, _ = self.run_lint("x", pages=link)
        self.assertEqual((code, out.strip()), (2, "refused lint_quotes_pages_dir_invalid"))
        self.assertNotIn(str(self.root), out)

    def test_inputs_unchanged(self):
        def digest():
            return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in [*self.pages.iterdir(), self.root / "draft.txt"] if p.exists()}
        self.run_lint('"the vendor keeps audit logs" (D1 p.1)\n')
        before = digest()
        self.run_lint('"the vendor keeps audit logs" (D1 p.1)\n')
        self.assertEqual(before, digest())

    def test_cli_registered(self):
        self.assertEqual(cli.COMMANDS["agency-lint-quotes"], ("agency_run.lint_quotes", "main"))


if __name__ == "__main__":
    unittest.main()
