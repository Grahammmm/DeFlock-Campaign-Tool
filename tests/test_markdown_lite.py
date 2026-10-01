"""Safe-subset Markdown renderer: markup allowlist and XSS neutralization."""
import unittest

from campaign_tool.markdown_lite import render


class MarkdownLiteTests(unittest.TestCase):
    def test_paragraphs_bold_lists_links_and_headings(self):
        html = render("## Heading\n\nFirst **bold** line\ncontinues.\n\n- one\n- [two](https://example.invalid/a?b=1&c=2)\n")
        self.assertEqual(html, '<h3>Heading</h3>\n<p>First <strong>bold</strong> line continues.</p>\n'
                               '<ul><li>one</li><li><a href="https://example.invalid/a?b=1&amp;c=2" rel="noopener noreferrer">two</a></li></ul>')

    def test_raw_html_is_escaped(self):
        html = render('<script>alert(1)</script> and <img src=x onerror="alert(1)">')
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", html)

    def test_unsafe_link_targets_are_dropped_to_text(self):
        for target in ("javascript:alert(1)", "JAVASCRIPT:alert(1)", "http://example.invalid",
                       "data:text/html,hi", "//example.invalid", "/local", "https://example.invalid/\"onclick=\"x",
                       "https://a b"):
            with self.subTest(target=target):
                html = render(f"[click]({target})")
                self.assertNotIn("<a ", html)
                self.assertNotIn("href", html)
                self.assertIn("click", html)

    def test_link_text_cannot_smuggle_markup(self):
        html = render('[<b>x</b>](https://example.invalid)')
        self.assertIn('rel="noopener noreferrer">&lt;b&gt;x&lt;/b&gt;</a>', html)

    def test_bold_does_not_span_lines_and_quotes_are_escaped(self):
        html = render('**open\nclose** "quoted"')
        self.assertNotIn("<strong>", html)
        self.assertIn("&quot;quoted&quot;", html)

    def test_limits_and_types(self):
        with self.assertRaises(ValueError):
            render(None)
        with self.assertRaises(ValueError):
            render("a" * (200 * 1024 + 1))
        self.assertEqual(render(""), "")
        self.assertEqual(render("\n\n  \n"), "")

    def test_deterministic_and_crlf_tolerant(self):
        self.assertEqual(render("a\r\n\r\n- b\r\n"), render("a\n\n- b\n"))


if __name__ == "__main__":
    unittest.main()
