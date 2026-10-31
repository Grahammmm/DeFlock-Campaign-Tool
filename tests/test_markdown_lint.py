"""markdown_lite.lint() and its --lint CLI name copy that render() degrades (RF1240-2)."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.markdown_lite import MAX_BYTES, lint, main, render

ROOT = Path(__file__).resolve().parents[1]
CLEAN = "## Heading\n**bold**\n\n- item\n* item\n\n[x](https://example.invalid/a)"
EACH = ("[a](http://example.invalid)\n[b](/local)\n# Top\n### Sub\n1. one\n2) two\n"
        "a **b\n<b>x</b>\n*it*\n_it_")
NO_ECHO = "[SECRET-TEXT](javascript:alert(1))"
NOT_FALSE = ("2 * 3 = 6\nsnake_case_name\na < b\n**bold** and **more**\n## A **bold** heading\n"
             "[u](https://example.invalid/_a_/b_c_)")
CRLF = "\r\n\r\n# x\r\n"
SEVERAL = "[a](http://x) and *b*"
# render() output recorded from origin/main before lint() existed; lint must never change it.
RENDERED = {
    CLEAN: '<h3>Heading</h3>\n<p><strong>bold</strong></p>\n<ul><li>item</li><li>item</li></ul>\n'
           '<p><a href="https://example.invalid/a" rel="noopener noreferrer">x</a></p>',
    EACH: "<p>a b # Top ### Sub 1. one 2) two a **b &lt;b&gt;x&lt;/b&gt; *it* _it_</p>",
    NO_ECHO: "<p>SECRET-TEXT)</p>",
    NOT_FALSE: "<p>2 * 3 = 6 snake_case_name a &lt; b <strong>bold</strong> and <strong>more</strong></p>\n"
               "<h3>A <strong>bold</strong> heading</h3>\n"
               '<p><a href="https://example.invalid/_a_/b_c_" rel="noopener noreferrer">u</a></p>',
    CRLF: "<p># x</p>",
    SEVERAL: "<p>a and *b*</p>",
}


def codes(source):
    return [(line, code) for line, code, _ in lint(source)]


class LintTests(unittest.TestCase):
    def test_clean_subset_has_no_findings(self):
        self.assertEqual(lint(CLEAN), [])
        self.assertEqual(lint(""), [])

    def test_each_code_once_in_line_order(self):
        self.assertEqual(codes(EACH), [
            (1, "link-dropped"), (2, "link-dropped"), (3, "heading-level"), (4, "heading-level"),
            (5, "numbered-list"), (6, "numbered-list"), (7, "bold-unclosed"), (8, "raw-html"),
            (9, "emphasis"), (10, "emphasis")])
        details = {code: detail for _, code, detail in lint(EACH)}
        self.assertEqual(details["link-dropped"], "link target is not absolute https")
        self.assertEqual(details["heading-level"], 'only "## " headings render')
        self.assertEqual(details["numbered-list"], "numbered lists render as plain text")
        self.assertEqual(details["bold-unclosed"], "unclosed **")
        self.assertEqual(details["raw-html"], "raw HTML is shown escaped")
        self.assertEqual(details["emphasis"], "only **bold** renders")

    def test_dropped_link_text_and_target_never_echoed(self):
        findings = lint(NO_ECHO)
        self.assertEqual([code for _, code, _ in findings], ["link-dropped"])
        for _, _, detail in findings:
            self.assertNotIn("SECRET-TEXT", detail)
            self.assertNotIn("javascript", detail)
            self.assertNotIn("alert", detail)

    def test_no_false_positives(self):
        self.assertEqual(lint(NOT_FALSE), [])
        for line in ("####### seven hashes", "#hashtag", "1.5 million", "1234. year", "- [x](https://example.invalid/a)",
                     "* **bold** item", "a<3 b", "price_1 and_2", "**x** *"):
            with self.subTest(line=line):
                self.assertEqual(lint(line), [])

    def test_bypass_by_wording_still_flagged(self):
        for line in ("[a](HTTPS://example.invalid)", '[a](https://example.invalid/"x)', "[a](https:example.invalid)",
                     "[a](//example.invalid)", "- [a](mailto:x@example.invalid)", "## [a](ftp://example.invalid)",
                     "[**a**](http://example.invalid)"):
            with self.subTest(line=line):
                self.assertEqual(codes(line), [(1, "link-dropped")])
        for line in ("<!-- note -->", "</p>", "  <DIV>", "- <i>x</i>", "## <em>x</em>"):
            with self.subTest(line=line):
                self.assertEqual(codes(line), [(1, "raw-html")])
        self.assertEqual(codes("   # padded"), [(1, "heading-level")])
        self.assertEqual(codes("###### six"), [(1, "heading-level")])
        self.assertEqual(codes("999) x"), [(1, "numbered-list")])
        self.assertEqual(codes("**a** then **b"), [(1, "bold-unclosed")])

    def test_line_numbers_with_crlf_and_blank_lines(self):
        self.assertEqual(lint(CRLF), [(3, "heading-level", 'only "## " headings render')])
        self.assertEqual(codes("ok\n\n\n  *x*  \nmore\n# y"), [(4, "emphasis"), (6, "heading-level")])

    def test_several_on_one_line_and_duplicates(self):
        self.assertEqual(codes(SEVERAL), [(1, "emphasis"), (1, "link-dropped")])
        self.assertEqual(codes("[a](http://x) [b](/y) [c](https://example.invalid)"),
                         [(1, "link-dropped"), (1, "link-dropped")])
        self.assertEqual(codes("*a* and _b_"), [(1, "emphasis"), (1, "emphasis")])

    def test_guards_match_render(self):
        for bad in (None, b"## x", 3):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    lint(bad)
                with self.assertRaises(ValueError):
                    render(bad)
        big = "x" * (MAX_BYTES + 1)
        with self.assertRaises(ValueError):
            lint(big)
        with self.assertRaises(ValueError):
            render(big)
        self.assertEqual(lint("é" * (MAX_BYTES // 2)), [])

    def test_render_unchanged(self):
        for source, expected in RENDERED.items():
            with self.subTest(source=source):
                self.assertEqual(render(source), expected)
                lint(source)
                self.assertEqual(render(source), expected)


class LintCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.clean = self.dir / "clean.md"
        self.clean.write_text(CLEAN, encoding="utf-8")
        self.dirty = self.dir / "dirty.md"
        self.dirty.write_text("ok\n# Top\n[a](/local)\n", encoding="utf-8")

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_findings_printed_with_path_and_line(self):
        code, out, err = self.run_main("--lint", str(self.clean), str(self.dirty))
        self.assertEqual(code, 1)
        self.assertEqual(out, [f'{self.dirty}:2: heading-level: only "## " headings render',
                               f"{self.dirty}:3: link-dropped: link target is not absolute https",
                               "files=2 findings=2"])
        self.assertEqual(err, "")

    def test_clean_only_exits_zero(self):
        self.assertEqual(self.run_main("--lint", str(self.clean)), (0, ["files=1 findings=0"], ""))

    def test_missing_file_named_and_others_still_linted(self):
        missing = self.dir / "missing.md"
        code, out, err = self.run_main("--lint", str(missing), str(self.clean))
        self.assertEqual(code, 2)
        self.assertEqual(out, ["files=1 findings=0"])
        self.assertIn(str(missing), err)
        self.assertNotIn(str(self.clean), err)
        code, out, _ = self.run_main("--lint", str(self.dirty), str(missing))
        self.assertEqual((code, out[-1]), (2, "files=1 findings=2"))

    def test_bad_files_exit_two(self):
        latin = self.dir / "latin.md"
        latin.write_bytes(b"caf\xe9 # x\n")
        big = self.dir / "big.md"
        big.write_bytes(b"x" * (MAX_BYTES + 1))
        for bad in (latin, big, self.dir):
            with self.subTest(bad=bad.name):
                code, out, err = self.run_main("--lint", str(bad), str(self.clean))
                self.assertEqual((code, out), (2, ["files=1 findings=0"]))
                self.assertIn(str(bad), err)
        self.assertIn("not UTF-8", self.run_main("--lint", str(latin))[2])

    def test_usage_errors(self):
        for argv in ((), ("--lint",), ("--fix", str(self.clean)), (str(self.clean),),
                     ("--lint", "--quiet", str(self.clean)), ("--lint", str(self.clean), "-v")):
            with self.subTest(argv=argv):
                code, out, err = self.run_main(*argv)
                self.assertEqual((code, out), (2, []))
                self.assertTrue(err.startswith("usage:"))

    def test_read_only(self):
        before = {p: (p.read_bytes(), os.stat(p).st_mtime_ns) for p in (self.clean, self.dirty)}
        listing = sorted(os.listdir(self.dir))
        self.run_main("--lint", str(self.clean), str(self.dirty))
        self.assertEqual({p: (p.read_bytes(), os.stat(p).st_mtime_ns) for p in before}, before)
        self.assertEqual(sorted(os.listdir(self.dir)), listing)

    def test_module_entry_point(self):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.markdown_lite", "--lint", str(self.dirty)],
                                cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "files=1 findings=2")


if __name__ == "__main__":
    unittest.main()
