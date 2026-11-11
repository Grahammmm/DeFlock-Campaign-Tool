"""Link lint recognises exactly the link candidates the renderer does, incl. escaped punctuation (RF0421-2)."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.markdown_lite import lint, render

ROOT = Path(__file__).resolve().parents[1]
DROPPED = "link-dropped"
HOST = "link-host"
DETAILS = {DROPPED: "link target is not absolute https", HOST: "link host is not a public DNS name"}

# Independent goldens: source -> (exact rendered bytes on main before this change, link-only lint codes).
CORPUS = {
    "\\[x](http://example.invalid)": ("<p>&#91;x](http://example.invalid)</p>", []),
    "\\[x](https://localhost)": ("<p>&#91;x](https://localhost)</p>", []),
    "[x](https://example.invalid/a\\*)": (
        '<p><a href="https://example.invalid/a*" rel="noopener noreferrer">x</a></p>', []),
    "[x](https://example.invalid/a\\_b)": (
        '<p><a href="https://example.invalid/a_b" rel="noopener noreferrer">x</a></p>', []),
    "[x](https://example.invalid/a\\#b)": (
        '<p><a href="https://example.invalid/a#b" rel="noopener noreferrer">x</a></p>', []),
    "[x](https://example.invalid/a\\!b\\~c)": (
        '<p><a href="https://example.invalid/a!b~c" rel="noopener noreferrer">x</a></p>', []),
    "[x\\](http://example.invalid)": ("<p>[x&#93;(http://example.invalid)</p>", []),
    "**\\[x](http://example.invalid)**": ("<p><strong>&#91;x](http://example.invalid)</strong></p>", []),
    "\\\\[x](http://example.invalid)": ("<p>&#92;x</p>", [DROPPED]),
    "\\\\\\[x](http://example.invalid)": ("<p>&#92;&#91;x](http://example.invalid)</p>", []),
    "\\\\\\\\[x](https://localhost)": ("<p>&#92;&#92;x</p>", [HOST]),
    "[x](https://example.invalid/a\\\\*)": ("<p>x</p>", [DROPPED]),
    "[x](https://example.invalid/a\\&b)": ("<p>x</p>", [DROPPED]),
    "[x](https://exa\\_mple.invalid/)": ("<p>x</p>", [HOST]),
    "&#91;x](http://example.invalid)": ("<p>&amp;#91;x](http://example.invalid)</p>", []),
    "[x](https://example.invalid/a&amp;b)": (
        '<p><a href="https://example.invalid/a&amp;amp;b" rel="noopener noreferrer">x</a></p>', []),
    "\\![x](http://example.invalid)": ("<p>&#33;x</p>", [DROPPED]),
    "[x](http://example.invalid)": ("<p>x</p>", [DROPPED]),
}


def link_codes(source, images=None):
    return [code for _, code, _ in lint(source, images) if code in DETAILS]


class EscapedLinkRecognition(unittest.TestCase):
    def test_escaped_opening_bracket_is_literal_not_a_link_candidate(self):
        for source in ("\\[x](http://example.invalid)", "\\[x](https://localhost)", "[x\\](http://example.invalid)",
                       "**\\[x](http://example.invalid)**"):
            with self.subTest(source=source):
                self.assertEqual(render(source), CORPUS[source][0])
                self.assertEqual(lint(source), [])

    def test_supported_escaped_url_punctuation_links_without_false_warning(self):
        for source in ("[x](https://example.invalid/a\\*)", "[x](https://example.invalid/a\\_b)",
                       "[x](https://example.invalid/a\\#b)", "[x](https://example.invalid/a\\!b\\~c)"):
            with self.subTest(source=source):
                self.assertEqual(render(source), CORPUS[source][0])
                self.assertEqual(lint(source), [])

    def test_backslash_pair_parity_preserves_actual_dropped_candidates(self):
        # one backslash escapes the bracket; two leave a real candidate after a literal backslash; and so on
        for source in ("\\[x](http://example.invalid)", "\\\\\\[x](http://example.invalid)"):
            self.assertEqual(lint(source), [], source)
        self.assertEqual(lint("\\\\[x](http://example.invalid)"), [(1, DROPPED, DETAILS[DROPPED])])
        self.assertEqual(lint("\\\\\\\\[x](https://localhost)"), [(1, HOST, DETAILS[HOST])])
        # an escaped backslash in a target is still a backslash, which no https target may contain
        self.assertEqual(link_codes("[x](https://example.invalid/a\\\\*)"), [DROPPED])
        # an unsupported escape is not unescaped: the target stays invalid
        self.assertEqual(link_codes("[x](https://example.invalid/a\\&b)"), [DROPPED])
        # the escaped character reaches the host check without its backslash
        self.assertEqual(link_codes("[x](https://exa\\_mple.invalid/)"), [HOST])
        for source in ("\\\\[x](http://example.invalid)", "\\\\\\\\[x](https://localhost)",
                       "[x](https://example.invalid/a\\\\*)", "[x](https://example.invalid/a\\&b)",
                       "[x](https://exa\\_mple.invalid/)"):
            self.assertEqual(render(source), CORPUS[source][0], source)

    def test_original_entities_and_expansion_limits_use_renderer_view(self):
        for source in ("&#91;x](http://example.invalid)", "[x](https://example.invalid/a&amp;b)"):
            self.assertEqual(render(source), CORPUS[source][0])
            self.assertEqual(lint(source), [], source)
        # label limit 300 is measured on the protected text: five characters per escaped "*"
        at_limit = "[" + "\\*" * 60 + "](http://example.invalid)"
        over_limit = "[" + "\\*" * 61 + "](http://example.invalid)"
        self.assertEqual(link_codes(at_limit), [DROPPED])
        self.assertEqual(link_codes(over_limit), [])
        self.assertNotIn("<a ", render(over_limit))
        self.assertTrue(render(over_limit).startswith("<p>[&#42;"))
        # "&" expands to "&amp;" under ordinary HTML escaping, so 60 fit and 61 do not
        self.assertEqual(link_codes("[" + "&" * 60 + "](http://example.invalid)"), [DROPPED])
        self.assertEqual(link_codes("[" + "&" * 61 + "](http://example.invalid)"), [])
        # target limit 2000 on the protected text: 23 literal characters plus five per escaped "#"
        prefix = "http://example.invalid/"
        self.assertEqual(link_codes(f"[x]({prefix}" + "\\#" * 395 + ")"), [DROPPED])
        self.assertEqual(link_codes(f"[x]({prefix}" + "\\#" * 396 + ")"), [])
        # non-ASCII text and quotes behave as in the renderer
        self.assertEqual(link_codes("[é\"](http://example.invalid)"), [DROPPED])

    def test_all_inline_contexts_share_escape_aware_link_scan(self):
        bad = "[b](http://example.invalid)"
        ok = "\\[a](http://example.invalid)"
        source = "\r\n".join([
            f"{ok}", f"## {ok}", f"- {ok}", f"* {ok}", f"1. {ok}",  # lines 1-5 clean
            f"  {bad} {bad}  ", "", f"## {bad}", f"- {bad}", f"* \\[c](https://localhost)", f"2. [d](https://localhost)",
            f"| {ok} | h |", "|---|---|", f"| {bad} | {ok} |", f"| [e](https://localhost) | x |",
        ])
        expected = [
            (6, DROPPED, DETAILS[DROPPED]), (6, DROPPED, DETAILS[DROPPED]),
            (8, DROPPED, DETAILS[DROPPED]), (9, DROPPED, DETAILS[DROPPED]),
            (11, HOST, DETAILS[HOST]), (14, DROPPED, DETAILS[DROPPED]), (15, HOST, DETAILS[HOST]),
        ]
        # line 10 is the escaped host example and must stay clean
        self.assertEqual(lint(source), expected)
        self.assertEqual(lint(source.replace("\r\n", "\n")), expected)

    def test_image_prefix_and_nonlink_diagnostics_are_not_silenced(self):
        image = "![x](http://example.invalid)"
        for images in (None, {}, {"/p.png": (1, 1)}):
            self.assertEqual(lint(image, images), [(1, "image", lint(image)[0][2])], images)
        self.assertEqual(lint("![x](/p.png)"), [])
        # a protected bang is not an image prefix for the link scan; the raw "![" image warning remains
        self.assertEqual([code for _, code, _ in lint("\\![x](http://example.invalid)")], ["image", DROPPED])
        # genuine findings survive next to an escaped example
        mixed = lint("\\[a](http://example.invalid) [b](https://localhost) **x <b> *y*")
        self.assertEqual(sorted(code for _, code, _ in mixed),
                         ["bold-unclosed", "emphasis", HOST, "raw-html"])
        self.assertEqual(link_codes("\\[a](http://example.invalid) [b](http://example.invalid)"), [DROPPED])

    def test_diagnostics_do_not_echo_link_values_and_renderer_bytes_never_change(self):
        for source, (html, codes) in CORPUS.items():
            with self.subTest(source=source):
                self.assertEqual(render(source), html)
                self.assertEqual(link_codes(source), codes)
                self.assertEqual(render(source), html)  # lint never alters rendering
        found = lint("[CANARYLABEL](http://canaryhost.invalid/CANARYPATH) \\[CANARYTWO](https://localhost)")
        self.assertEqual(found, [(1, DROPPED, DETAILS[DROPPED])])
        self.assertNotIn("CANARY", repr(found))


class RealCli(unittest.TestCase):
    FIVE = ("\\[x](http://example.invalid)\n\\[x](https://localhost)\n[x](https://example.invalid/a\\*)\n"
            "[x](https://example.invalid/a\\_b)\n[x](https://example.invalid/a\\#b)\n")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in sorted(self.dir.iterdir())}

    def cli(self, *paths):
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.markdown_lite", "--lint", *map(str, paths)],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout, done.stderr

    def test_real_cli_five_line_false_positive_fixture_is_clean_and_read_only(self):
        clean = self.dir / "clean.md"
        clean.write_text(self.FIVE, encoding="utf-8")
        before = self.snapshot()
        self.assertEqual(self.cli(clean), (0, "files=1 findings=0\n", ""))
        self.assertEqual(self.snapshot(), before)

    def test_real_cli_dirty_missing_and_repeated_operands(self):
        clean = self.dir / "clean.md"
        clean.write_text(self.FIVE, encoding="utf-8")
        dirty = self.dir / "dirty.md"
        dirty.write_text("\\[a](http://example.invalid)\n[CANARYLABEL](http://example.invalid/CANARYPATH)\n",
                         encoding="utf-8")
        missing = self.dir / "missing.md"
        before = self.snapshot()
        finding = f"{dirty}:2: link-dropped: link target is not absolute https\n"
        self.assertEqual(self.cli(clean, dirty), (1, finding + "files=2 findings=1\n", ""))
        self.assertEqual(self.cli(dirty, clean, dirty), (1, finding + finding + "files=3 findings=2\n", ""))
        code, out, err = self.cli(missing, clean)
        self.assertEqual((code, out), (2, "files=1 findings=0\n"))
        self.assertEqual(err, f"{missing}: cannot lint (No such file or directory)\n")
        code, out, err = self.cli(dirty, missing, clean)
        self.assertEqual((code, out), (2, finding + "files=2 findings=1\n"))
        self.assertNotIn("CANARY", out + err)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
