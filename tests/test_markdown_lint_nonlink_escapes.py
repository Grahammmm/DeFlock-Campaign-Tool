"""Non-link lint checks respect the renderer's backslash-escaped punctuation (RF1500-16)."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.markdown_lite import lint, render

ROOT = Path(__file__).resolve().parents[1]
B = ("bold-unclosed", "unclosed **")
E = ("emphasis", "only **bold** renders")
H = ("raw-html", "raw HTML is shown escaped")
DROPPED = ("link-dropped", "link target is not absolute https")

# Each source holds real one-backslash escapes ("\\*" in Python is one backslash then "*").
FOUR = {
    "Literal \\**": "<p>Literal &#42;*</p>",
    "Literal \\*word\\*": "<p>Literal &#42;word&#42;</p>",
    "Literal \\_word\\_": "<p>Literal &#95;word&#95;</p>",
    "Literal \\<b>word\\</b>": "<p>Literal &#60;b&gt;word&#60;/b&gt;</p>",
}

# Independent goldens captured on main before this change: source -> (render bytes, full lint tuples).
NOESCAPE = {
    'plain text & "quotes" é': ("<p>plain text &amp; &quot;quotes&quot; é</p>", []),
    "**bold** ok": ("<p><strong>bold</strong> ok</p>", []),
    "**open": ("<p>**open</p>", [(1, *B)]),
    "*em* and _em_": ("<p>*em* and _em_</p>", [(1, *E), (1, *E)]),
    "<b>x</b>": ("<p>&lt;b&gt;x&lt;/b&gt;</p>", [(1, *H)]),
    "&#42;x&#42; &#95;y&#95; &lt;b&gt;": ("<p>&amp;#42;x&amp;#42; &amp;#95;y&amp;#95; &amp;lt;b&amp;gt;</p>", []),
    "\\# \\! \\~ \\` \\( \\)": ("<p>&#35; &#33; &#126; &#96; &#40; &#41;</p>", []),
    "a < b": ("<p>a &lt; b</p>", []),
}


class NonLinkEscapes(unittest.TestCase):
    def test_explicit_literal_stars_underscores_and_html_are_clean(self):
        for source, html in FOUR.items():
            with self.subTest(source=source):
                self.assertEqual(source.count("\\"), source.count("\\*") + source.count("\\_") + source.count("\\<"))
                self.assertEqual(lint(source), [])
                self.assertEqual(render(source), html)
        beside = "**real** \\*x\\* \\<i>"
        self.assertEqual(lint(beside), [])
        self.assertEqual(render(beside), "<p><strong>real</strong> &#42;x&#42; &#60;i&gt;</p>")

    def test_escaped_closer_does_not_hide_unmatched_bold(self):
        self.assertEqual(lint("Literal **word\\**"), [(1, *B)])
        self.assertEqual(render("Literal **word\\**"), "<p>Literal **word&#42;*</p>")
        self.assertEqual(lint("Literal **word**"), [])
        self.assertEqual(lint("Literal **word"), [(1, *B)])

    def test_backslash_pair_parity_keeps_real_findings(self):
        for n in range(1, 5):
            p = "\\" * n
            odd = n % 2 == 1
            cases = {f"{p}**x": [] if odd else [(1, *B)],
                     f"{p}*word{p}*": [] if odd else [(1, *E)],
                     f"{p}_word{p}_": [] if odd else [(1, *E)],
                     f"{p}<b>": [] if odd else [(1, *H)]}
            for source, expected in cases.items():
                with self.subTest(source=source):
                    self.assertEqual(lint(source), expected)
        self.assertEqual(render("\\\\**x"), "<p>&#92;**x</p>")
        self.assertEqual(render("\\\\\\<b>"), "<p>&#92;&#60;b&gt;</p>")
        # unsupported escapes shield nothing
        self.assertEqual(lint("\\.*word*"), [(1, *E)])
        self.assertEqual(lint('\\"<b>'), [(1, *H)])

    def test_mixed_literal_and_genuine_occurrences_are_not_suppressed(self):
        self.assertEqual(lint("\\<b>x</b> <i>y"), [(1, *H)])
        self.assertEqual(lint("\\*a\\* *b* _c_"), [(1, *E), (1, *E)])
        found = lint("\\*\\* **x [CANARY](http://example.invalid/CANARY)")
        self.assertEqual(found, [(1, *B), (1, *DROPPED)])
        self.assertNotIn("CANARY", repr(found))

    def test_all_inline_contexts_and_physical_lines(self):
        source = "\r\n".join([
            "\\*a\\* \\<b>", "## \\*a\\*", "- \\_a\\_", "* \\<b> x", "1. \\**", "",  # lines 1-6 clean
            "  ## *a*  ", "- **x\\**", "2. \\\\<b>",
            "| \\*h\\* | **h\\** |", "|---|---|", "| \\<b> | <i> |",
        ])
        expected = [(7, *E), (8, *B), (9, *H), (10, *B), (12, *H)]
        self.assertEqual(lint(source), expected)
        self.assertEqual(lint(source.replace("\r\n", "\n")), expected)

    def test_link_image_and_noescape_contracts_are_frozen(self):
        for source, (html, found) in NOESCAPE.items():
            with self.subTest(source=source):
                self.assertEqual(render(source), html)
                self.assertEqual(lint(source), found)
        self.assertEqual(lint("\\[x](http://example.invalid)"), [])
        self.assertEqual(lint("\\\\[x](http://example.invalid)"), [(1, *DROPPED)])
        self.assertEqual([code for _, code, _ in lint("\\![x](http://example.invalid)")], ["image", "link-dropped"])
        self.assertEqual(lint("![x](/p.png)"), [])


class RealCli(unittest.TestCase):
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

    def test_real_cli_literal_file_becomes_clean_without_writes(self):
        clean = self.dir / "clean.md"
        clean.write_text("\n".join(FOUR) + "\n", encoding="utf-8")
        dirty = self.dir / "dirty.md"
        dirty.write_text("Literal **word\\**\n\\*a\\* *b*\n", encoding="utf-8")
        bad = self.dir / "bad.md"
        bad.write_bytes(b"\xff\n")
        missing = self.dir / "missing.md"
        before = self.snapshot()
        self.assertEqual(self.cli(clean), (0, "files=1 findings=0\n", ""))
        dirty_out = f"{dirty}:1: bold-unclosed: unclosed **\n{dirty}:2: emphasis: only **bold** renders\n"
        self.assertEqual(self.cli(dirty, clean), (1, dirty_out + "files=2 findings=2\n", ""))
        self.assertEqual(self.cli(missing, bad, clean), (
            2, "files=1 findings=0\n",
            f"{missing}: cannot lint (No such file or directory)\n{bad}: cannot lint (not UTF-8)\n"))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
