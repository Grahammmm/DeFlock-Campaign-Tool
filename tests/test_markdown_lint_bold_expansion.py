"""Bold lint follows the renderer's protected-length boundary (RF0520-5)."""
import os
import subprocess
import sys
import tempfile
import unittest

from campaign_tool import markdown_lite as ml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = (1, "bold-unclosed", "unclosed **")


def bold(body):
    return "**" + body + "**"


class BoldExpansionTests(unittest.TestCase):
    def test_html_expansion_crosses_bold_boundary(self):
        # (body, renders strong?) -- protected length: & = 5, " = 6, ' = 5
        cases = [
            ("&" * 100, True), ("&" * 101, False),
            ('"' * 83, True), ('"' * 84, False),
            ("a" * 495 + "&", True), ("a" * 496 + "&", False),
            ("a" * 500, True), ("a" * 501, False),
            ("'" * 83, True), ("'" * 84, False),
        ]
        for body, ok in cases:
            with self.subTest(body=body[:3], n=len(body)):
                out = ml.render(bold(body))
                self.assertEqual("<strong>" in out, ok)
                self.assertEqual(ml.lint(bold(body)), [] if ok else [FIX])
                if not ok:
                    self.assertIn("<p>**", out)

    def test_angle_brackets_expand(self):
        self.assertNotIn(FIX, ml.lint(bold("<" * 125)))   # &lt; is 4 chars: 500
        self.assertIn(FIX, ml.lint(bold("<" * 126)))

    def test_source_entities_are_literal(self):
        self.assertEqual(ml.lint(bold("&amp;" * 20)), [])          # 20*9 = 180, under
        self.assertEqual(ml.lint(bold("&amp;" * 100)), [FIX])      # 100*9 > 500
        self.assertNotIn("<strong>", ml.render(bold("&amp;" * 100)))
        self.assertIn("&amp;amp;", ml.render(bold("&amp;" * 100)))

    def test_escape_parity_and_genuine_unclosed(self):
        self.assertEqual(ml.lint(r"\*\*x\*\*"), [])
        self.assertEqual(ml.lint(bold("a\\*b")), [])
        self.assertEqual(ml.lint("**x"), [FIX])
        self.assertEqual(ml.lint("**x** and **y"), [FIX])
        # clean span beside an overflowing one: one code only
        self.assertEqual(ml.lint(bold("&" * 101) + " " + bold("&" * 101)), [FIX])
        self.assertEqual(ml.lint(bold("ok") + " " + bold("&" * 101)), [FIX])

    def test_block_contexts_and_line_numbers(self):
        bad = bold("&" * 101)
        src = "\r\n".join(["", "## " + bad, "- " + bad, "* " + bad, "1. " + bad, "  " + bad + "  ",
                           "| a | b |", "|---|---|", "| " + bad + " | " + bad + " |"])
        self.assertEqual(ml.lint(src), sorted(
            [(2, "bold-unclosed", "unclosed **"), (3, "bold-unclosed", "unclosed **"),
             (4, "bold-unclosed", "unclosed **"), (5, "bold-unclosed", "unclosed **"),
             (6, "bold-unclosed", "unclosed **"), (9, "bold-unclosed", "unclosed **"),
             (9, "bold-unclosed", "unclosed **")]))

    def test_other_diagnostics_unchanged(self):
        bad = bold("&" * 101)
        got = ml.lint(bad + " [x](http://example.com) <b>x</b> *y*")
        codes = [c for _, c, _ in got]
        self.assertEqual(sorted(codes), sorted(["bold-unclosed", "link-dropped", "raw-html", "emphasis"]))

    def test_privacy_inert(self):
        body = '<script>"canary"</script>' + "&" * 101
        out = ml.render(bold(body))
        self.assertNotIn("<script>", out)
        self.assertEqual(ml.lint(bold(body)), [FIX] + [x for x in ml.lint(bold(body)) if x != FIX])
        self.assertNotIn("canary", repr(ml.lint(bold(body))))

    def _cli(self, *args):
        p = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.markdown_lite", "--lint", *args],
                           cwd=ROOT, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def test_real_cli_reports_overflow_without_writes(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.md")
            good = os.path.join(d, "good.md")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("ok\n" + bold("&" * 101) + "\n")
            with open(good, "w", encoding="utf-8") as f:
                f.write(bold("&" * 100) + "\n")
            def snapshot():
                out = {}
                for n in os.listdir(d):
                    with open(os.path.join(d, n), "rb") as f:
                        out[n] = (f.read(), os.stat(os.path.join(d, n)).st_mtime_ns)
                return out
            snap = snapshot()
            code, out, err = self._cli(bad)
            self.assertEqual((code, err), (1, ""))
            self.assertEqual(out, f"{bad}:2: bold-unclosed: unclosed **\nfiles=1 findings=1\n")
            self.assertEqual(self._cli(good)[:2], (0, "files=1 findings=0\n"))
            after = snapshot()
            self.assertEqual(snap, after)

    def test_cli_read_errors_keep_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.md")
            miss = os.path.join(d, "missing.md")
            junk = os.path.join(d, "junk.md")
            with open(bad, "w", encoding="utf-8") as f:
                f.write(bold("&" * 101) + "\n")
            with open(junk, "wb") as f:
                f.write(b"\xff\xfe\xff")
            for order in ([miss, bad], [bad, miss], [junk, bad], [bad, junk]):
                code, out, _ = self._cli(*order)
                self.assertEqual(code, 2)
                self.assertIn(f"{bad}:1: bold-unclosed: unclosed **", out)


if __name__ == "__main__":
    unittest.main()
