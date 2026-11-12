"""A Markdown table pipe is a delimiter when the backslash run before it is even; an odd run escapes it (RF1721-5).

Every expectation below is a literal written from the escape rule (a pair is one visible backslash), never derived
from the scanner under test.
"""
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from campaign_tool import markdown_lite
from campaign_tool.markdown_lite import MAX_BYTES, _cells, lint, render
from campaign_tool.site import build_site

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "fictional-campaign"
BS = "\\"
NEEDS_HEADER = "table needs a header and a --- separator row"
DIFFERS = "table row cell count differs from the header"


class CellText(HTMLParser):
    """Decoded text of each th/td, in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells, self.open = [], False

    def handle_starttag(self, tag, attrs):
        if tag in ("th", "td"):
            self.cells.append("")
            self.open = True

    def handle_endtag(self, tag):
        if tag in ("th", "td"):
            self.open = False

    def handle_data(self, data):
        if self.open:
            self.cells[-1] += data


def decoded_cells(html):
    parser = CellText()
    parser.feed(html)
    return parser.cells


def one_col(cell_html, head="h"):
    return (f"<table><thead><tr><th>{head}</th></tr></thead><tbody><tr><td>{cell_html}</td></tr></tbody></table>")


def two_col(first, second, head=("h1", "h2")):
    return (f"<table><thead><tr><th>{head[0]}</th><th>{head[1]}</th></tr></thead>"
            f"<tbody><tr><td>{first}</td><td>{second}</td></tr></tbody></table>")


def malformed(source):
    return [(n, d) for n, c, d in lint(source) if c == "table-malformed"]


class InteriorPipeTests(unittest.TestCase):
    def test_interior_pipe_counts_zero_through_six_backslashes(self):
        for n in range(7):
            row = "| a" + BS * n + "| b |"
            if n % 2 == 0:
                cells, shown = ["a" + BS * n, "b"], BS * (n // 2)
                body_html = two_col("a" + "&#92;" * (n // 2), "b")
                head_html = ("<table><thead><tr><th>" + "a" + "&#92;" * (n // 2) + "</th><th>b</th></tr></thead></table>")
                head_src, body_src = row + "\n|---|---|", "| h1 | h2 |\n|---|---|\n" + row
                decoded_head, decoded_body = ["a" + shown, "b"], ["h1", "h2", "a" + shown, "b"]
            else:
                cells, shown = ["a" + BS * (n - 1) + "| b"], BS * ((n - 1) // 2)
                body_html = one_col("a" + "&#92;" * ((n - 1) // 2) + "| b")
                head_html = "<table><thead><tr><th>" + "a" + "&#92;" * ((n - 1) // 2) + "| b</th></tr></thead></table>"
                head_src, body_src = row + "\n|---|", "| h |\n|---|\n" + row
                decoded_head, decoded_body = ["a" + shown + "| b"], ["h", "a" + shown + "| b"]
            with self.subTest(backslashes=n):
                self.assertEqual(_cells(row), cells)
                self.assertEqual(render(head_src), head_html)
                self.assertEqual(lint(head_src), [])
                self.assertEqual(decoded_cells(render(head_src)), decoded_head)
                self.assertEqual(render(body_src), body_html)
                self.assertEqual(lint(body_src), [])
                self.assertEqual(decoded_cells(render(body_src)), decoded_body)

    def test_closing_pipe_requires_even_backslash_parity(self):
        self.assertIsNone(_cells("| a" + BS + "|"))
        self.assertIsNone(_cells("| a | b" + BS * 3 + "|"))
        self.assertEqual(_cells("| a" + BS * 2 + "|"), ["a" + BS * 2])
        self.assertEqual(_cells("| a | b" + BS * 4 + "|"), ["a", "b" + BS * 4])
        # adjacent delimiters, empty cells, surrounding whitespace
        self.assertEqual(_cells("|a" + BS * 2 + "||b|"), ["a" + BS * 2, "", "b"])
        self.assertEqual(_cells("||"), [""])
        self.assertEqual(_cells("|  a" + BS * 2 + "  |\t" + BS * 2 + "\t|"), ["a" + BS * 2, BS * 2])
        self.assertEqual(_cells("|" + BS + "|" + BS * 2 + "|"), ["|" + BS * 2])
        self.assertIsNone(_cells("|" + BS + "|"))
        # a row closed only by an odd run is not a table row, so the header has no body and the next line is text
        source = "| a |\n|---|\n| b" + BS + "|"
        self.assertEqual(render(source), "<table><thead><tr><th>a</th></tr></thead></table>\n<p>| b&#124;</p>")
        self.assertEqual(malformed(source), [])
        closed = "| a |\n|---|\n| b" + BS * 2 + "|"
        self.assertEqual(render(closed), one_col("b&#92;", head="a"))
        self.assertEqual(lint(closed), [])
        crlf = "| a |\r\n|---|\r\n| b" + BS * 2 + "|\r\n"
        self.assertEqual(render(crlf), one_col("b&#92;", head="a"))
        self.assertEqual(lint(crlf), [])

    def test_literal_pipe_and_other_escapes_are_consumed_once(self):
        cases = [
            ("a " + BS + "| b", "a | b", "a | b"),
            ("a" + BS * 3 + "| b", "a" + BS * 2 + "| b", "a&#92;| b"),
            ("a" + BS * 5 + "| b", "a" + BS * 4 + "| b", "a&#92;&#92;| b"),
            (BS + "|" + BS + "|", "||", "||"),
            (BS + "*x" + BS + "* " + BS + "[y" + BS + "] " + BS + "_z" + BS + "_",
             BS + "*x" + BS + "* " + BS + "[y" + BS + "] " + BS + "_z" + BS + "_",
             "&#42;x&#42; &#91;y&#93; &#95;z&#95;"),
            ('say "q" & <b>x</b> &#65;', 'say "q" & <b>x</b> &#65;',
             "say &quot;q&quot; &amp; &lt;b&gt;x&lt;/b&gt; &amp;#65;"),
            (BS * 2 + BS + "|" + BS * 2, BS * 2 + "|" + BS * 2, "&#92;|&#92;"),
        ]
        for raw, cell, html in cases:
            source = "| h |\n|---|\n| " + raw + " |"
            with self.subTest(raw=raw):
                self.assertEqual(_cells("| " + raw + " |"), [cell])
                self.assertEqual(render(source), one_col(html))
                self.assertEqual(lint(source), [] if "<b>" not in raw else [(3, "raw-html", "raw HTML is shown escaped")])
                self.assertNotIn("<b>", render(source))
        self.assertEqual(decoded_cells(render("| h |\n|---|\n| a" + BS * 3 + "| b |")), ["h", "a" + BS + "| b"])
        self.assertEqual(decoded_cells(render("| h |\n|---|\n| &#65; &amp; |")), ["h", "&#65; &amp;"])


class CorrectedWidthTests(unittest.TestCase):
    def test_corrected_width_keeps_inline_findings_in_their_cell(self):
        head = "| h1" + BS * 2 + " | h2 | h3 | h4 | h5 |"
        row = ("| [a](http://example.invalid)" + BS * 2 + " | [b](https://localhost/x) | **open | <i>x</i> | **real** |")
        source = head + "\n|---|---|---|---|---|\n" + row
        self.assertEqual(lint(source), [
            (3, "bold-unclosed", "unclosed **"),
            (3, "link-dropped", "link target is not absolute https"),
            (3, "link-host", "link host is not a public DNS name"),
            (3, "raw-html", "raw HTML is shown escaped"),
        ])
        html = render(source)
        self.assertEqual(html.count("<td"), 5)
        self.assertIn("<td><strong>real</strong></td>", html)
        self.assertIn("<td>a&#92;</td>", html)
        self.assertNotIn("<i>", html)
        for _, _, detail in lint(source):
            self.assertNotIn("example", detail)
            self.assertNotIn("localhost", detail)
        # a clean row with the same paired runs has no findings, and its separator row never does
        clean = head + "\n|---|---|---|---|---|\n| a" + BS * 2 + "| b | c | d | e |"
        self.assertEqual(lint(clean), [])

    def test_wrong_width_restart_alignment_and_column_limits_stay_strict(self):
        wide = "| a | b |\n|---|---|\n| x" + BS * 2 + "| y | z |\n| p | q |"
        self.assertEqual(malformed(wide), [(3, DIFFERS), (4, NEEDS_HEADER)])
        self.assertEqual(render(wide), "<table><thead><tr><th>a</th><th>b</th></tr></thead></table>\n"
                                       "<p>| x&#92;| y | z | | p | q |</p>")
        restart = wide + "\n\n| c" + BS * 2 + "| d |\n|:---|:---:|\n| 1 | 2" + BS * 2 + "|"
        self.assertEqual(malformed(restart), [(3, DIFFERS), (4, NEEDS_HEADER)])
        self.assertEqual(render(restart).split("\n")[-1],
                         '<table><thead><tr><th class="align-left">c&#92;</th><th class="align-center">d</th></tr></thead>'
                         '<tbody><tr><td class="align-left">1</td><td class="align-center">2&#92;</td></tr></tbody></table>')
        right = "| a" + BS * 2 + "| b |\n|---:|---|\n| 1 | 2 |\n\nafter"
        self.assertEqual(render(right), '<table><thead><tr><th class="align-right">a&#92;</th><th>b</th></tr></thead>'
                                        '<tbody><tr><td class="align-right">1</td><td>2</td></tr></tbody></table>\n<p>after</p>')
        twelve = "|" + ("c" + BS * 2 + "|") * 12 + "\n|" + "---|" * 12
        self.assertEqual(render(twelve).count("<th>"), 12)
        self.assertEqual(lint(twelve), [])
        thirteen = "|" + ("c" + BS * 2 + "|") * 13 + "\n|" + "---|" * 13 + "\n|" + "1|" * 13
        self.assertNotIn("<table", render(thirteen))
        self.assertEqual(malformed(thirteen), [(1, "at most 12 columns"), (2, "at most 12 columns"),
                                               (3, "at most 12 columns")])


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-md-parity-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in sorted(self.dir.iterdir())}

    def run_cli(self, *paths):
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.markdown_lite", "--lint", *map(str, paths)],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout, done.stderr

    def test_cli_fixed_body_row_exits_clean_and_changes_nothing(self):
        good = self.dir / "parity.md"
        good.write_text("intro\n\n| a | b |\n|---|---|\n| x" + BS * 2 + "| y |\n", encoding="utf-8")
        dirty = self.dir / "dirty.md"
        dirty.write_text("# Top\n", encoding="utf-8")
        before = self.snapshot()
        self.assertEqual(self.run_cli(good), (0, "files=1 findings=0\n", ""))
        self.assertEqual(self.run_cli(good, dirty),
                         (1, f'{dirty}:1: heading-level: only "## " headings render\nfiles=2 findings=1\n', ""))
        missing = self.dir / "missing.md"
        code, out, err = self.run_cli(good, missing, dirty)
        self.assertEqual(code, 2)
        self.assertEqual(out, f'{dirty}:1: heading-level: only "## " headings render\nfiles=2 findings=1\n')
        self.assertEqual(err, f"{missing}: cannot lint (No such file or directory)\n")
        self.assertEqual(self.snapshot(), before)


class SiteTests(unittest.TestCase):
    def digests(self, root, skip=()):
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file() and p.relative_to(root).parts[0] not in skip}

    def test_site_custom_page_uses_corrected_table_without_side_effects(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-md-parity-site-") as tmp:
            root = Path(tmp) / "campaign"
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
            build_site(root, today=datetime.date(2026, 10, 1))
            baseline = self.digests(root / "public")
            shutil.rmtree(root / "public")
            pages = root / "content" / "pages"
            pages.mkdir(exist_ok=True)
            body = "Intro.\n\n| Name | Note |\n|---|---|\n| a" + BS * 2 + "| <b>x</b> |\n"
            (pages / "parity.json").write_text(json.dumps({"title": "Parity", "body_md": body}), encoding="utf-8")
            sources = self.digests(root, skip=("public",))
            build_site(root, today=datetime.date(2026, 10, 1))
            self.assertEqual(self.digests(root, skip=("public",)), sources)
            page = (root / "public" / "parity.html").read_text(encoding="utf-8")
            self.assertIn("<table><thead><tr><th>Name</th><th>Note</th></tr></thead><tbody><tr><td>a&#92;</td>"
                          "<td>&lt;b&gt;x&lt;/b&gt;</td></tr></tbody></table>", page)
            self.assertNotIn("<b>x</b>", page)
            self.assertEqual(decoded_cells(page)[-4:], ["Name", "Note", "a" + BS, "<b>x</b>"])
            after = self.digests(root / "public")
            for name, digest in baseline.items():
                if name not in ("sitemap.xml", "sitemap.txt"):
                    self.assertEqual(after[name], digest, name)
            self.assertEqual(sorted(set(after) - set(baseline)), ["parity.html"])


class UnaffectedTests(unittest.TestCase):
    CORPUS = {
        "| a | b |\n|---|---|\n| 1 | 2 |":
            "<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>",
        "| a |\n|---|\n| x \\| y |": one_col("x | y", head="a"),
        "| a |\n|---|\n| x \\\\\\| y |": one_col("x &#92;| y", head="a"),
        "| a |\n|---|\n| x \\\\\\\\\\| y |": one_col("x &#92;&#92;| y", head="a"),
        "| a \\|\n|---|": "<p>| a &#124; |---|</p>",
        "text\n- one\n1. two\n## Head\n[l](https://example.invalid/p) ![alt](/x.png)":
            '<p>text</p>\n<ul><li>one</li></ul>\n<ol><li>two</li></ol>\n<h3>Head</h3>\n'
            '<p><a href="https://example.invalid/p" rel="noopener noreferrer">l</a> !alt</p>',
        "a \\\\| b": "<p>a &#92;| b</p>",
    }

    def test_unaffected_corpus_remains_byte_identical_and_scanning_is_linear(self):
        for source, html in self.CORPUS.items():
            with self.subTest(source=source):
                self.assertEqual(render(source), html)
        self.assertEqual(lint("| a |\n|---|\n| x \\\\\\| y |"), [])
        self.assertEqual(lint("- one\n[l](http://example.invalid)"), [(2, "link-dropped", "link target is not absolute https")])

        class Counting(str):
            calls = yielded = 0

            def __iter__(self):
                type(self).calls += 1
                for ch in str.__iter__(self):
                    type(self).yielded += 1
                    yield ch

        line = Counting("|" + ("x" + BS * 2 + "|") * 50000)
        self.assertLess(len(line), MAX_BYTES)
        cells = _cells(line)
        self.assertEqual(len(cells or ()), 50000)
        self.assertEqual((Counting.calls, Counting.yielded), (1, len(line)))
        with self.assertRaises(ValueError):
            render("x" * (MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            lint("x" * (MAX_BYTES + 1))
        self.assertFalse(hasattr(markdown_lite, "_PIPE"))


if __name__ == "__main__":
    unittest.main()
