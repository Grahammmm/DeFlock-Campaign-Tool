"""DF2022-02: the built style.css ends with one @media print block.

Builds the fictional example into a temporary copy; synthetic content only, no
network. The block is parsed with a small brace matcher, not a regex.
"""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool import site as site_module
from campaign_tool.site import build_site, preview
from campaign_tool.cli import read_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
HIDE = [".skip-link", ".site-nav", ".banner-signup-link", ".hero", ".city-picker", ".map-key", ".map-credit",
        ".signup-form", ".signup-embed", "iframe", "dialog"]
CHROME = [".skip-link", ".site-nav", ".banner-signup-link", ".hero", ".city-picker", ".signup-form"]
KEEP = [".finding-body", ".source-table", ".notice", ".meeting-list"]
LINK = 'a[href^="http"]'


def skip_comment_or_string(css, i):
    """Index after a comment or string starting at i, else i."""
    if css.startswith("/*", i):
        end = css.find("*/", i + 2)
        if end < 0:
            raise ValueError("unterminated comment")
        return end + 2
    if css[i] in "\"'":
        j = i + 1
        while j < len(css) and css[j] != css[i]:
            j += 2 if css[j] == "\\" else 1
        if j >= len(css):
            raise ValueError("unterminated string")
        return j + 1
    return i


def match_brace(css, open_index):
    """Index just after the brace closing the one at open_index."""
    depth, i = 0, open_index
    while i < len(css):
        nxt = skip_comment_or_string(css, i)
        if nxt != i:
            i = nxt
            continue
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced braces")


def top_level_at_rules(css):
    """(start, end, prelude) of each top-level at-rule; plain rules are skipped."""
    found, i = [], 0
    while i < len(css):
        nxt = skip_comment_or_string(css, i)
        if nxt != i:
            i = nxt
            continue
        if css[i] == "{":
            start = css.rfind("}", 0, i) + 1
            prelude = re.sub(r"/\*.*?\*/", "", css[start:i], flags=re.S).strip()
            end = match_brace(css, i)
            if prelude.startswith("@"):
                found.append((start, end, prelude))
            i = end
            continue
        i += 1
    return found


def rules(body):
    """(selectors, declarations) of each plain rule inside a block body."""
    out, i = [], 0
    while i < len(body):
        nxt = skip_comment_or_string(body, i)
        if nxt != i:
            i = nxt
            continue
        if body[i] == "{":
            start = max(body.rfind("}", 0, i), -1) + 1
            selector = re.sub(r"/\*.*?\*/", "", body[start:i], flags=re.S).strip()
            end = match_brace(body, i)
            out.append(([s.strip() for s in selector.split(",")], body[i + 1:end - 1]))
            i = end
            continue
        i += 1
    return out


class SitePrintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="synthetic-site-print-")
        root = Path(cls.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
        cls.root, cls.public = root, root / "public"
        build_site(root)
        cls.css = (cls.public / "style.css").read_text(encoding="utf-8")
        cls.pages = {p.relative_to(cls.public).as_posix(): p.read_text(encoding="utf-8")
                     for p in cls.public.rglob("*.html")}
        prints = [r for r in top_level_at_rules(cls.css) if r[2] == "@media print"]
        cls.block = prints
        cls.start, cls.end, _ = prints[-1] if prints else (0, 0, "")
        cls.body = cls.css[cls.css.index("{", cls.start) + 1:cls.end - 1] if prints else ""

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def hidden(self):
        return [sel for sels, decl in rules(self.body) if "display:none" in decl.replace(" ", "") for sel in sels]

    def test_exactly_one_print_block_and_it_is_the_last_at_rule(self):
        self.assertEqual(len(self.block), 1)
        self.assertEqual(top_level_at_rules(self.css)[-1][2], "@media print")
        self.assertEqual(self.css[self.end:].strip(), "", "nothing may follow the print block")
        self.assertEqual(self.css.count("@media print"), 1)

    def test_hide_list_complete_and_chrome_classes_still_rendered(self):
        hidden = self.hidden()
        for selector in HIDE:
            self.assertIn(selector, hidden)
        with tempfile.TemporaryDirectory(prefix="synthetic-signup-print-") as tmp:
            root = Path(tmp) / "campaign"  # the example has no signup; a hosted one renders .signup-form
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
            site = json.loads((root / "content" / "site.json").read_text(encoding="utf-8"))
            site["signup"] = {"mode": "brevo_hosted", "form_html_allowlisted_url": "https://synthetic.sibforms.com/serve/x"}
            (root / "content" / "site.json").write_text(json.dumps(site), encoding="utf-8")
            build_site(root)
            signup_index = (root / "public" / "index.html").read_text(encoding="utf-8")
        rendered = "\n".join(self.pages.values()) + signup_index
        for selector in CHROME:  # a renamed class would silently print chrome again
            pattern = r'class="(?:[^"]* )?' + re.escape(selector[1:]) + r'(?: [^"]*)?"'
            self.assertTrue(re.search(pattern, rendered), f"{selector} not rendered on any built page")

    def test_kept_content_never_hidden(self):
        for selector in self.hidden():
            for kept in KEEP:
                self.assertNotRegex(selector, re.escape(kept) + r"(?![\w-])", selector)
        bypass = self.body + ".source-table{display:none}"  # the check itself catches a hidden keeper
        self.assertIn(".source-table", [s for sels, d in rules(bypass) if "display:none" in d for s in sels])

    def test_link_rule_only_external_and_applies_to_built_pages(self):
        after = [sels for sels, decl in rules(self.body) if "attr(href)" in decl]
        self.assertEqual(after, [[LINK + "::after"]])
        sources = re.findall(r'<a [^>]*href="([^"]+)"', self.pages["sources.html"])
        self.assertTrue([h for h in sources if h.startswith("https://")], "sources page needs an https link")
        finding = re.findall(r'<a [^>]*href="([^"]+)"', self.pages["findings/cedar-policy-posted.html"])
        internal = [h for h in finding if h.startswith("/sources.html#")]
        self.assertTrue(internal)
        for href in internal + ["#content", "mailto:organizer@example.invalid", "/meetings.html"]:
            self.assertFalse(href.startswith("http"), href)

    def test_no_remote_fetch_in_print_block(self):
        self.assertNotIn("url(", self.body)
        self.assertNotIn("@import", self.body)
        self.assertEqual(self.body.replace(LINK, "").count("http"), 0)

    def test_only_an_append_and_csp_unchanged(self):
        from tests.test_site_build import EXPECTED_CSP
        self.assertIn("Content-Security-Policy: " + EXPECTED_CSP, (self.public / "_headers").read_text(encoding="utf-8"))
        without = self.css[:self.start] + self.css[self.end:]
        extra = site_module.EXTRA_CSS
        self.assertTrue(self.css.endswith(extra))
        pre = extra[:extra.index("@media print")]
        self.assertEqual(without.rstrip("\n"), (self.css[:-len(extra)] + pre).rstrip("\n"))
        self.assertTrue(pre.endswith(".site-footer p{margin:0}.site-footer a{color:inherit}\n"))
        self.assertNotIn("print", self.css[:self.start])

    def test_details_open_and_page_breaks(self):
        decls = {sel: decl for sels, decl in rules(self.body) for sel in sels}
        self.assertIn("content-visibility:visible", decls["details::details-content"])
        self.assertIn("break-inside:avoid", decls[".meeting-list article"])
        self.assertIn("<details><summary>Public comment kit</summary>", self.pages["meetings.html"])

    def test_starter_build_has_no_print_block(self):
        css = preview(read_config(EXAMPLE))["css"]
        self.assertNotIn("@media print", css)
        with tempfile.TemporaryDirectory(prefix="synthetic-starter-print-") as tmp:
            root = Path(tmp) / "campaign"
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public", "content"))
            build_site(root)
            self.assertEqual((root / "public" / "style.css").read_text(encoding="utf-8"), css)

    def test_parser_refuses_unbalanced_css(self):
        with self.assertRaises(ValueError):
            top_level_at_rules("@media print{a{color:#000}")
        with self.assertRaises(ValueError):
            top_level_at_rules("/* open")
        self.assertEqual(top_level_at_rules('a::after{content:"}"}@media print{b{c:d}}')[0][2], "@media print")


if __name__ == "__main__":
    unittest.main()
