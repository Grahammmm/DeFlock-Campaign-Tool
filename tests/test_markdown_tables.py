"""markdown_lite renders ``1. `` ordered lists and simple pipe tables; everything else is byte-identical (DF1507-12)."""
import datetime
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import site
from campaign_tool.markdown_lite import inline, lint, render
from campaign_tool.site import build_site
from tests.test_markdown_lint import EACH, RENDERED

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
TABLE = "| Agency | Cameras | Notes |\n|---|:---:|---:|\n| Cedar | 4 | a \\| b |\n| **Elm** | 0 | [policy](https://example.invalid/p) |"
TABLE_HTML = ('<table><thead><tr><th>Agency</th><th class="align-center">Cameras</th><th class="align-right">Notes</th>'
              '</tr></thead><tbody><tr><td>Cedar</td><td class="align-center">4</td><td class="align-right">a | b</td></tr>'
              '<tr><td><strong>Elm</strong></td><td class="align-center">0</td><td class="align-right">'
              '<a href="https://example.invalid/p" rel="noopener noreferrer">policy</a></td></tr></tbody></table>')
# render() output recorded from origin/main 90b2791 (before this change) for inputs with no "N. " line and no table.
MAIN_RENDERED = {
    "## Heading\n\nFirst **bold** line\ncontinues.\n\n- one\n- [two](https://example.invalid/a?b=1&c=2)\n":
        '<h3>Heading</h3>\n<p>First <strong>bold</strong> line continues.</p>\n<ul><li>one</li><li><a href="https://'
        'example.invalid/a?b=1&amp;c=2" rel="noopener noreferrer">two</a></li></ul>',
    '<script>alert(1)</script> and <img src=x onerror="alert(1)">':
        "<p>&lt;script&gt;alert(1)&lt;/script&gt; and &lt;img src=x onerror=&quot;alert(1)&quot;&gt;</p>",
    "[<b>x</b>](https://example.invalid)":
        '<p><a href="https://example.invalid" rel="noopener noreferrer">&lt;b&gt;x&lt;/b&gt;</a></p>',
    '**open\nclose** "quoted"': "<p>**open close** &quot;quoted&quot;</p>",
    "a\r\n\r\n- b\r\n": "<p>a</p>\n<ul><li>b</li></ul>",
    "| a | b |\nplain": "<p>| a | b | plain</p>",
    "1.5 million\n1234. year\n|x": "<p>1.5 million 1234. year |x</p>",
    "": "",
}


def main_render(source):
    """Frozen copy of origin/main's render() (before this change), used to build the reference site."""
    blocks, paragraph, items = [], [], []

    def flush():
        if paragraph:
            blocks.append("<p>" + " ".join(inline(line) for line in paragraph) + "</p>")
            paragraph.clear()
        if items:
            blocks.append("<ul>" + "".join("<li>" + inline(item) + "</li>" for item in items) + "</ul>")
            items.clear()

    for raw in source.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("## "):
            flush()
            blocks.append("<h3>" + inline(line[3:].strip()) + "</h3>")
        elif line.startswith("- ") or line.startswith("* "):
            if paragraph:
                flush()
            items.append(line[2:].strip())
        else:
            if items:
                flush()
            paragraph.append(line)
    flush()
    return "\n".join(blocks)


def malformed(source):
    return [(number, detail) for number, code, detail in lint(source) if code == "table-malformed"]


class OrderedListTests(unittest.TestCase):
    def test_ordered_lists_render(self):
        self.assertEqual(render("1. a\n2. b"), "<ol><li>a</li><li>b</li></ol>")
        self.assertEqual(render("3. a\n4. b"), '<ol start="3"><li>a</li><li>b</li></ol>')
        self.assertEqual(render("1. a\n1. b\n7. **c**"), "<ol><li>a</li><li>b</li><li><strong>c</strong></li></ol>")
        self.assertEqual(render("1. a\n- b"), "<ol><li>a</li></ol>\n<ul><li>b</li></ul>")
        self.assertEqual(render("- a\n1. b"), "<ul><li>a</li></ul>\n<ol><li>b</li></ol>")
        self.assertEqual(render("intro\n1. a\nafter"), "<p>intro</p>\n<ol><li>a</li></ol>\n<p>after</p>")
        self.assertEqual(render("1. a\n\n2. b"), '<ol><li>a</li></ol>\n<ol start="2"><li>b</li></ol>')
        self.assertEqual(render("1. <script>x</script>"), "<ol><li>&lt;script&gt;x&lt;/script&gt;</li></ol>")

    def test_paren_numbers_and_lookalikes_stay_paragraph_text(self):
        self.assertEqual(render("2) two"), "<p>2) two</p>")
        self.assertEqual(lint("2) two"), [(1, "numbered-list", 'only "1. " numbered lists render')])
        for line in ("1234. year", "1.5 million", "1.no space", "1."):
            with self.subTest(line=line):
                self.assertEqual(render(line), f"<p>{line}</p>")

    def test_lint_no_longer_flags_dot_lists_but_lints_their_text(self):
        self.assertEqual(lint("1. one\n10. ten\n999. x"), [])
        self.assertEqual([(n, c) for n, c, _ in lint("1. [a](http://x)\n2. *b*")], [(1, "link-dropped"), (2, "emphasis")])


class TableTests(unittest.TestCase):
    def test_valid_table_with_alignment_escapes_and_inline_markup(self):
        self.assertEqual(render(TABLE), TABLE_HTML)
        self.assertEqual(lint(TABLE), [])
        self.assertEqual(render("text\n" + TABLE + "\nafter"), "<p>text</p>\n" + TABLE_HTML + "\n<p>after</p>")

    def test_plain_and_left_alignment_and_header_only(self):
        self.assertEqual(render("|a|b|\n|---|:---|"),
                         '<table><thead><tr><th>a</th><th class="align-left">b</th></tr></thead></table>')

    def test_script_in_a_cell_is_escaped(self):
        html = render('| x |\n| --- |\n| <script>alert(1)</script> |\n| <img src=x onerror="y"> |')
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertIn("<td>&lt;script&gt;alert(1)&lt;/script&gt;</td>", html)
        self.assertEqual([(n, c) for n, c, _ in lint("| x |\n| --- |\n| <b>y</b> |")], [(3, "raw-html")])

    def test_header_without_separator(self):
        source = "intro\n| a | b |\nmore"
        self.assertEqual(render(source), main_render(source))
        self.assertEqual(malformed(source), [(2, "table needs a header and a --- separator row")])
        self.assertEqual(malformed("| a |\n| -- |"), [(1, "table needs a header and a --- separator row"),
                                                       (2, "table needs a header and a --- separator row")])

    def test_separator_width_differs_from_header(self):
        source = "| a | b | c |\n|---|---|\n| 1 | 2 | 3 |"
        self.assertEqual(render(source), main_render(source))
        self.assertEqual([n for n, _ in malformed(source)], [1, 2, 3])

    def test_body_row_with_wrong_count_ends_the_table(self):
        source = "|a|b|\n|---|---|\n|1|2|\n|3|\n|4|5|\n\nafter"
        self.assertEqual(render(source), "<table><thead><tr><th>a</th><th>b</th></tr></thead>"
                                         "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>\n"
                                         "<p>|3| |4|5|</p>\n<p>after</p>")
        self.assertEqual(malformed(source), [(4, "table row cell count differs from the header"),
                                             (5, "table needs a header and a --- separator row")])

    def test_wrong_count_row_may_start_a_new_valid_table(self):
        source = "|a|b|\n|---|---|\n|x|\n|---|"
        self.assertEqual(render(source).count("<table>"), 2)
        self.assertEqual(malformed(source), [])

    def test_twelve_columns_render_and_thirteen_are_refused(self):
        twelve = "|" + "c|" * 12 + "\n|" + "---|" * 12
        self.assertEqual(render(twelve).count("<th>"), 12)
        self.assertEqual(lint(twelve), [])
        thirteen = "|" + "c|" * 13 + "\n|" + "---|" * 13 + "\n|" + "1|" * 13 + "\n\n## Next"
        self.assertEqual(render(thirteen), main_render(thirteen))
        self.assertNotIn("<table", render(thirteen))
        self.assertEqual(malformed(thirteen), [(1, "at most 12 columns"), (2, "at most 12 columns"),
                                               (3, "at most 12 columns")])

    def test_escaped_closing_pipe_is_not_a_row(self):
        self.assertEqual(render("| a \\|\n|---|"), main_render("| a \\|\n|---|"))
        self.assertEqual(malformed("| a \\|\n|---|"), [(2, "table needs a header and a --- separator row")])


class ByteIdentityTests(unittest.TestCase):
    def test_existing_fixtures_render_as_on_main(self):
        stored = {source: html for source, html in RENDERED.items() if source != EACH}
        stored.update(MAIN_RENDERED)
        for source, expected in stored.items():
            with self.subTest(source=source):
                self.assertEqual(render(source), expected)
                self.assertEqual(main_render(source), expected)

    def test_example_site_byte_identical_to_a_main_build(self):
        hashes = []
        for renderer in (main_render, render):
            with tempfile.TemporaryDirectory(prefix="synthetic-md-tables-") as tmp:
                root = Path(tmp) / "campaign"
                shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
                with mock.patch.object(site, "markdown", renderer):
                    build_site(root, today=datetime.date(2026, 10, 1))
                public = root / "public"
                hashes.append({p.relative_to(public).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(public.rglob("*")) if p.is_file()})
        self.assertGreater(len(hashes[0]), 10)
        self.assertEqual(hashes[1], hashes[0])


if __name__ == "__main__":
    unittest.main()
