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


Q = "the vendor keeps audit logs"
RENDER = "c" * 64


class LintQuotesLedger(Base):
    """Q3: the recorded original-page ledger, checked against inventory.tsv and the original bytes."""

    def setUp(self):
        super().setUp()
        self.orig = self.root / "orig"
        self.orig.mkdir()
        self.bytes = {}
        rows = ["\t".join(lint_quotes.INVENTORY)]
        for doc, layer, notes in (("D1", "native", "no text layer: p.2"), ("D2", "ocr", "")):
            self.bytes[doc] = ("synthetic original %s" % doc).encode()
            (self.orig / (doc + ".pdf")).write_bytes(self.bytes[doc])
            rows.append("\t".join((doc, doc + ".pdf", "2026-01-01", doc + ".pdf", "2",
                                   hashlib.sha256(self.bytes[doc]).hexdigest(), layer, "unclassified", "r", notes)))
            for n in (1, 2):
                self.page(doc, n, Q + " and more")
        self.inv = self.root / "inventory.tsv"
        self.inv.write_text("\n".join(rows) + "\n")

    def sha(self, doc):
        return hashlib.sha256(self.bytes[doc]).hexdigest()

    def row(self, draft, doc="D1", page=1, method="native-reextract", render="", verdict="VERIFIED-ORIGINAL", **over):
        start = draft.index('"' + Q + '"')
        line, col = draft.count("\n", 0, start) + 1, start - (draft.rfind("\n", 0, start) + 1) + 1
        row = {"quote_id": "%d:%d:%s" % (line, col, hashlib.sha256(Q.encode()).hexdigest()), "doc_id": doc,
               "page": str(page), "original_sha256": self.sha(doc), "method": method, "render_sha256": render,
               "verdict": verdict, "checked_by": "records-reviewer", "checked_at": "2026-11-19T10:00:00-08:00", **over}
        return "\t".join(row[k] for k in lint_quotes.LEDGER)

    def run_q3(self, draft, rows, extra=None):
        ledger = self.root / "quote-verify.tsv"
        ledger.write_text("\n".join(["\t".join(lint_quotes.LEDGER)] + rows) + "\n")
        path = self.root / "draft.txt"
        path.write_text(draft, encoding="utf-8")
        argv = ["--draft", str(path), "--pages", str(self.pages), "--ledger", str(ledger),
                "--inventory", str(self.inv), "--original-root", str(self.orig)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lint_quotes.main(argv if extra is None else extra(argv))
        return code, out.getvalue(), err.getvalue()

    def reasons(self, out):
        return [x.split(" ", 2)[1:] for x in out.splitlines() if " Q" in x and x[0].isdigit()]

    def test_q3_clean_ledger_passes(self):
        native = '"%s" (D1 p.1) [VERIFIED-ORIGINAL]\n' % Q
        ocr = '"%s" (D2 p.1)  [VERIFIED-ORIGINAL]\n' % Q
        unverified = '"%s" (D2 p.2)\n' % Q
        for draft, row in ((native, self.row(native)),
                           (ocr, self.row(ocr, "D2", method="visual-render", render=RENDER)),
                           (unverified, self.row(unverified, "D2", 2, "none", verdict="UNVERIFIED-OCR-ONLY"))):
            code, out, _ = self.run_q3(draft, [row])
            self.assertEqual(code, 0, out)
            self.assertIn("Q3 checked the ledger for consistency only; no visual check", out)

    def test_q3_ledger_failure_classes(self):
        d = '"%s" (D1 p.1)\n' % Q
        o = '"%s" (D2 p.1)\n' % Q
        claimed = d.rstrip() + " [VERIFIED-ORIGINAL]\n"
        cases = [
            ("ledger_missing", d, []),
            ("ledger_missing", d.replace("audit logs", "audit"), [self.row(d)]),
            ("pinpoint_mismatch", d, [self.row(d, page=2, method="visual-render", render=RENDER)]),
            ("pinpoint_mismatch", d, [self.row(d, doc="D2")]),
            ("original_mismatch", d, [self.row(d, original_sha256="d" * 64)]),
            ("method_invalid", o, [self.row(o, "D2")]),
            ("method_invalid", '"%s" (D1 p.2)\n' % Q, [self.row('"%s" (D1 p.2)\n' % Q, page=2)]),
            ("method_invalid", o, [self.row(o, "D2", method="visual-render")]),
            ("method_invalid", d, [self.row(d, method="none")]),
            ("claim_unverified", claimed, [self.row(claimed, verdict="UNVERIFIED-OCR-ONLY")]),
            ("source_unsupported", '"%s" (msg abc1)\n' % Q, []),
            ("source_unsupported", '"%s" is bare\n' % Q, []),
        ]
        for reason, draft, rows in cases:
            with self.subTest(reason=reason, draft=draft):
                code, out, _ = self.run_q3(draft, rows)
                self.assertEqual(code, 1, out)
                self.assertEqual([r for r in self.reasons(out) if r[0] == "Q3"], [["Q3", reason]], out)
                self.assertNotIn(" Q1 ", out)

    def test_q3_original_bytes_changed_alone(self):
        d = '"%s" (D1 p.1)\n' % Q
        row = self.row(d)
        (self.orig / "D1.pdf").write_bytes(b"changed original bytes")
        code, out, _ = self.run_q3(d, [row])
        self.assertEqual((code, self.reasons(out)), (1, [["Q3", "original_mismatch"]]))

    def test_q3_bad_original_files_are_hits_not_paths(self):
        d = '"%s" (D1 p.1)\n' % Q
        row = self.row(d)
        original = self.orig / "D1.pdf"
        original.unlink()
        (self.root / "outside.pdf").write_bytes(self.bytes["D1"])
        for make in (lambda: os.symlink(self.root / "outside.pdf", original), lambda: original.mkdir(),
                     lambda: None):
            with self.subTest():
                make()
                code, out, _ = self.run_q3(d, [row])
                self.assertEqual((code, self.reasons(out)), (1, [["Q3", "original_mismatch"]]))
                self.assertNotIn(str(self.root), out)
                if original.is_dir():
                    original.rmdir()
                elif original.is_symlink():
                    original.unlink()
        text = self.inv.read_text().replace("D1.pdf\t2026", "../outside.pdf\t2026").replace("\tD1.pdf\t2\t", "\t../outside.pdf\t2\t")
        self.inv.write_text(text)
        code, out, _ = self.run_q3(d, [row])
        self.assertEqual((code, self.reasons(out)), (1, [["Q3", "original_mismatch"]]))

    def test_q3_refusals_are_fixed_and_path_free(self):
        d = '"%s" (D1 p.1)\n' % Q
        good = self.row(d)
        for name, rows in (("duplicate", [good, good]), ("digest", [self.row(d, original_sha256="XYZ")]),
                           ("render", [self.row(d, render="nothex")]), ("role", [self.row(d, checked_by="Jordan Q")]),
                           ("time", [self.row(d, checked_at="2026-11-19T10:00:00")]),
                           ("enum", [self.row(d, verdict="VERIFIED")]), ("page", [self.row(d, page=0)]), ("short", ["a\tb"])):
            with self.subTest(name):
                code, out, _ = self.run_q3(d, rows)
                self.assertEqual((code, out.strip()), (2, "refused lint_quotes_ledger_invalid"))
        for flag in ("--ledger", "--inventory", "--original-root"):
            with self.subTest(flag):
                def drop(argv, flag=flag):
                    i = argv.index(flag)
                    return argv[:i] + argv[i + 2:]
                code, out, _ = self.run_q3(d, [good], extra=drop)
                self.assertEqual((code, out.strip()), (2, "refused lint_quotes_q3_options_incomplete"))
        self.inv.write_text(self.inv.read_text() + self.inv.read_text().splitlines()[1] + "\n")
        code, out, _ = self.run_q3(d, [good])
        self.assertEqual((code, out.strip()), (2, "refused lint_quotes_inventory_invalid"))
        self.assertNotIn(str(self.root), out)

    def test_q3_never_writes_or_echoes(self):
        d = '"zzcanaryzz one two" (D1 p.1) [VERIFIED-ORIGINAL]\n'
        files = sorted(self.root.rglob("*"))
        before = {p: p.read_bytes() for p in files if p.is_file()}
        code, out, err = self.run_q3(d, [])
        self.assertEqual((code, self.reasons(out)[-1:]), (1, [["Q3", "ledger_missing"]]))
        self.assertNotIn("canary", out + err)
        after = {p: p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file() and p.name not in ("quote-verify.tsv", "draft.txt")}
        self.assertEqual({p: b for p, b in before.items() if p in after}, after)

    def test_q3_off_by_default_keeps_message_quotes_passing(self):
        code, out, _ = self.run_lint('"%s" (msg abc123)\n' % Q)
        self.assertEqual(code, 0, out)
        self.assertNotIn("Q3", out)


if __name__ == "__main__":
    unittest.main()
