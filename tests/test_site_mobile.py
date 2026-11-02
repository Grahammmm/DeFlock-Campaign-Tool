"""Static mobile regressions on synthetic builds; no browser or network needed."""
import hashlib
import json
import re
import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from campaign_tool.cli import read_config
from campaign_tool.content import load_content
from campaign_tool.site import Site, build_site, build_starter
from tests.test_site_build import EXPECTED_CSP, PAGES

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/fictional-campaign"
# Captured from main before this CSS-only change; not candidate-derived snapshots.
PRINT_SHA = (
    "f7818cba38e2d518"  # pragma: allowlist secret
    "44cff79c0cfc3c58"  # pragma: allowlist secret
    "6d2514e54ef168aa"  # pragma: allowlist secret
    "64bda5cc2fd1c252"  # pragma: allowlist secret
)
STARTER_SHA = (
    "b67013717d1d9601"  # pragma: allowlist secret
    "5f0153fd7b0fac0d"  # pragma: allowlist secret
    "27cac03515f8eb37"  # pragma: allowlist secret
    "2c9d352bbbd9c7cc"  # pragma: allowlist secret
)


def css_rules(css):
    """Yield (prelude, body, position) for top-level rules, counting braces."""
    css = re.sub(r"/\*.*?\*/", lambda m: " " * len(m[0]), css, flags=re.S)
    start, opening, depth, quote, escaped = 0, 0, 0, None, False
    for i, char in enumerate(css):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "{":
            if depth == 0:
                opening = i
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                yield css[start:opening].strip(), css[opening + 1:i], start
                start = i + 1
    if depth or quote:
        raise ValueError("unbalanced CSS")


def declarations(body):
    return dict(part.strip().split(":", 1) for part in body.split(";") if ":" in part)


def rule_map(css):
    result = {}
    for selectors, body, _ in css_rules(css):
        for selector in selectors.split(","):
            result.setdefault(selector.strip(), {}).update(declarations(body))
    return result


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack, self.text, self.navs, self.current = [], [], [], []
        self.viewport, self.inline = 0, False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.inline |= "style" in attrs
        if tag == "meta" and attrs.get("name") == "viewport":
            self.viewport += attrs.get("content") == "width=device-width, initial-scale=1"
        if tag == "nav" and attrs.get("class") == "site-nav":
            self.navs.append(attrs)
        if tag == "a" and attrs.get("aria-current") == "page":
            self.current.append((attrs.get("href"), any(t == "nav" for t, _ in self.stack)))
        if tag not in {"meta", "link", "input", "img", "br", "hr"}:
            self.stack.append((tag, (attrs.get("class") or "").split()))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.text.append((data, list(self.stack)))


class SiteMobileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="synthetic-mobile-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.roots = []
        cls.title, cls.url, cls.agency = "Z" * 80, "https://example.invalid/" + "z" * 200, "Z" * 60
        for name in ("ordinary", "stress"):
            root = Path(cls.tmp.name) / name
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
            if name == "stress":
                path = root / "content/findings/cedar-policy-posted.json"
                finding = json.loads(path.read_text())
                finding.update(title=cls.title, body_md=cls.url)
                path.write_text(json.dumps(finding))
                path = root / "content/agencies.json"
                agencies = json.loads(path.read_text())
                agencies[0]["name"] = cls.agency
                path.write_text(json.dumps(agencies))
            build_site(root)
            cls.roots.append(root)
        cls.css = (cls.roots[0] / "public/style.css").read_text()
        cls.mobile = [(body, pos) for pre, body, pos in css_rules(cls.css)
                      if re.fullmatch(r"@media\s*\(max-width:\s*850px\)", pre)]
        cls.rules = rule_map(cls.mobile[-1][0])

    def test_menu_overrides_starter_hide_by_order_and_specificity(self):
        hidden = [pos for body, pos in self.mobile
                  if rule_map(body).get(".topbar nav", {}).get("display") == "none"]
        self.assertTrue(hidden)
        self.assertGreater(self.mobile[-1][1], max(hidden))
        nav = self.rules[".campaign-banner .site-nav"]
        # Two class selectors beat the starter's one class plus one type.
        self.assertGreater(".campaign-banner .site-nav".count("."), ".topbar nav".count("."))
        self.assertEqual(nav["display"], "flex")
        self.assertEqual(nav["flex-wrap"], "wrap")
        self.assertEqual(self.rules[".campaign-banner .site-nav a[aria-current=page]"]["background"], "#ffffff1f")

    def test_tap_targets_and_focus(self):
        for selector in (".campaign-banner .site-nav a", ".finding-filter select", ".signup-row input", ".signup-row button"):
            self.assertEqual(self.rules[selector]["min-height"], "44px", selector)
        self.assertEqual(self.rules[".campaign-banner .site-nav a"]["display"], "inline-flex")
        self.assertEqual(rule_map(self.css)[".site-nav a:focus-visible"]["outline"], "3px solid var(--mint)")

    def test_narrow_grid_and_bounded_media(self):
        self.assertIn("minmax(min(280px,100%),1fr)", self.css)
        self.assertNotRegex(self.css, r"minmax\(\s*280px\s*,")
        self.assertEqual(self.rules[".agency-list"]["grid-template-columns"], "minmax(0,1fr)")
        for selector in (".finding-body img", ".finding-body table"):
            self.assertEqual(self.rules[selector]["max-width"], "100%")
        self.assertEqual(self.rules[".finding-body table"]["overflow-x"], "auto")

    def test_stress_text_has_wrapping_ancestors(self):
        pages = {name: Page((self.roots[1] / "public" / name).read_text()) for name in PAGES}
        for name, needle, ancestor in (("findings/cedar-policy-posted.html", self.title, ("article", ["section", "finding-body"])),
                                       ("findings/cedar-policy-posted.html", self.url, ("article", ["section", "finding-body"])),
                                       ("agencies.html", self.agency, ("div", ["agency-list"]))):
            matches = [stack for data, stack in pages[name].text
                       if needle in data and not any(tag == "head" for tag, _ in stack)]
            self.assertTrue(matches, name)
            self.assertTrue(all(ancestor in stack for stack in matches), name)
        for selector in (".finding-body", ".agency-list article", ".meeting-list article", ".finding-list li"):
            self.assertEqual(self.rules[selector]["overflow-wrap"], "anywhere")

    def test_all_pages_viewport_navigation_and_no_inline_styles(self):
        for root in self.roots:
            pages = {p.relative_to(root / "public").as_posix(): Page(p.read_text())
                     for p in (root / "public").rglob("*.html")}
            self.assertEqual(set(pages), set(PAGES))
            for name, page in pages.items():
                self.assertEqual(page.viewport, 1, name)
                self.assertEqual(len(page.navs), 1, name)
                self.assertEqual(len(page.current), 1, name)
                self.assertTrue(all(inside for _, inside in page.current), name)
                self.assertFalse(page.inline, name)

    def test_csp_headers_print_and_starter_identical_to_main(self):
        for root in self.roots:
            site = Site(root, read_config(root), load_content(root))
            self.assertEqual(site.csp(), EXPECTED_CSP)
            self.assertEqual((root / "public/_headers").read_bytes(), (
                "/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n"
                "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
                "  Content-Security-Policy: " + EXPECTED_CSP + "\n").encode())
        block = self.css[self.css.index("@media print"):].strip()
        self.assertEqual(hashlib.sha256(block.encode()).hexdigest(), PRINT_SHA)
        public = Path(self.tmp.name) / "starter"
        build_starter(read_config(EXAMPLE), public)
        self.assertEqual(hashlib.sha256((public / "style.css").read_bytes()).hexdigest(), STARTER_SHA)


if __name__ == "__main__":
    unittest.main()
