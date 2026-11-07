"""DF1737-02: ``build --check`` accessibility pattern lint (a lint, not an audit).

Synthetic content only; no network (``urllib.request.urlopen`` is patched to raise).
"""
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, fixes, site
from campaign_tool.site_check import check_accessibility

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NO_NETWORK = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network access attempted"))
PASSED = ("Public tree check passed (pattern scan, internal links and accessibility pattern lint; "
          "not an accessibility audit; human review still required).")


def page(body, html='<html lang="en">'):
    return f"<!doctype html>{html}<head><title>t</title></head><body>{body}</body></html>"


class HandWrittenPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-a11y-")
        self.addCleanup(temporary.cleanup)
        self.public = Path(temporary.name) / "public"
        (self.public / "sub").mkdir(parents=True)

    def check(self, body, name="index.html", **kwargs):
        (self.public / name).write_text(page(body, **kwargs), encoding="utf-8")
        return check_accessibility(self.public)

    def test_clean_page_has_no_hits(self):
        self.assertEqual(self.check('<h1>A</h1><h2 id="a">B</h2><a href="/x">Read</a><img src="d.png" alt="">'), [])

    def test_img_without_alt_names_site_path_or_external(self):
        self.assertEqual(self.check('<img src="x.png">'), [("index.html", "img-alt", "x.png")])
        self.assertEqual(self.check('<img src="x.png" alt="">'), [])
        self.assertEqual(self.check('<img src="/i/y.png?v=2#f"><img src="https://cdn.example.invalid/z.png">'
                                    '<img src="//cdn.example.invalid/w.png"><img>'),
                         [("index.html", "img-alt", "-"), ("index.html", "img-alt", "/i/y.png"),
                          ("index.html", "img-alt", "external")])

    def test_heading_skip_only_when_going_more_than_one_deeper(self):
        self.assertEqual(self.check("<h2>a</h2><h4>b</h4>"), [("index.html", "heading-skip", "h2\u2192h4")])
        self.assertEqual(self.check("<h4>a</h4><h2>b</h2><h3>c</h3>"), [])
        self.assertEqual(self.check("<h3>first</h3><h4>b</h4>"), [])
        self.assertEqual(self.check("<h1>a</h1><h3>b</h3><h1>c</h1><h6>d</h6>"),
                         [("index.html", "heading-skip", "h1\u2192h3"), ("index.html", "heading-skip", "h1\u2192h6")])

    def test_link_text_counts_descendant_text_img_alt_and_labels(self):
        self.assertEqual(self.check('<a href="/x"><img src="i.png" alt=""></a>'), [("index.html", "link-text", "/x")])
        for ok in ('<a href="/x"><img src="i.png" alt="Map"></a>', '<a href="/x" aria-label="Map"><img src="i.png" alt=""></a>',
                   '<a href="/x" title="Map"></a>', '<a href="/x"><span><b> Map </b></span></a>', '<a>no href</a>', '<a name="n"></a>'):
            self.assertEqual(self.check(ok), [], ok)
        self.assertEqual(self.check('<a href="/y"> \n&nbsp; <span> </span></a><a href="/z" aria-label="  "></a>'),
                         [("index.html", "link-text", "/y"), ("index.html", "link-text", "/z")])

    def test_duplicate_id_once_per_page_not_across_pages(self):
        self.assertEqual(self.check('<p id="a"></p><div id="a"></div><span id="a"></span><p id="b"></p>'),
                         [("index.html", "duplicate-id", "a")])
        (self.public / "index.html").write_text(page('<p id="a"></p>'), encoding="utf-8")
        self.assertEqual(self.check('<p id="a"></p>', name="sub/other.html"), [])

    def test_html_lang_missing_or_empty(self):
        for html in ("<html>", '<html lang="">', '<html lang="  ">', ""):
            self.assertEqual(self.check("<p>x</p>", html=html), [("index.html", "html-lang", "-")], repr(html))
        self.assertEqual(self.check("<p>x</p>", html='<html lang="es">'), [])

    def test_malformed_html_does_not_crash_and_later_hits_still_reported(self):
        body = '<div><p>open<span></div></div></a></h3><a href="/u">unclosed<ul><li>x</ul><h2>a</h2><h5>b</h5><img src="m.png"'
        self.assertEqual(self.check(body + ">"), [("index.html", "heading-skip", "h2\u2192h5"),
                                                  ("index.html", "img-alt", "m.png")])
        self.assertEqual(self.check('<a href="/p"><a href="/q">q</a><p id="d"><p id="d">'),
                         [("index.html", "duplicate-id", "d"), ("index.html", "link-text", "/p")])

    def test_pages_are_named_and_sorted_symlinks_skipped(self):
        (self.public / "sub" / "b.html").write_text(page('<img src="b.png">'), encoding="utf-8")
        (self.public / "index.html").write_text(page("<h1>a</h1>", html="<html>"), encoding="utf-8")
        (self.public / "alias.html").symlink_to(self.public / "sub" / "b.html")
        self.assertEqual(check_accessibility(self.public), [("index.html", "html-lang", "-"), ("sub/b.html", "img-alt", "b.png")])


class BuiltSiteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-a11y-build-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

    def run_build(self, inject=None):
        real = site.build_site

        def build_then_edit(root, today=None, **kwargs):
            written = real(root, today, **kwargs)
            if inject:
                inject(self.public)
            return written

        out, err = io.StringIO(), io.StringIO()
        error = None
        with NO_NETWORK, mock.patch.object(site, "build_site", build_then_edit), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.build(self.root, check=True)
            except ValueError as exc:
                error = exc
        return out.getvalue(), err.getvalue(), error

    @staticmethod
    def append(public, name, html):
        path = public / name
        path.write_text(path.read_text(encoding="utf-8").replace("</body>", html + "</body>"), encoding="utf-8")

    def test_fictional_example_builds_and_check_passes(self):
        out, err, error = self.run_build()
        self.assertIsNone(error, err)
        self.assertEqual(err, "note: sources.zip skipped: no local public source copies\n")  # DF0022-11: the fictional library has no local public copy
        self.assertIn(PASSED, out)
        self.assertEqual(check_accessibility(self.public), [])
        self.assertIn('id="city-records-link" href="sources.html#library">Review the evidence notes</a>',
                      (self.public / "index.html").read_text(encoding="utf-8"))

    def test_a11y_hit_alone_fails_with_its_count(self):
        out, err, error = self.run_build(lambda p: self.append(p, "about.html", '<img src="/style.css"><p id="top"></p>'))
        self.assertEqual(str(error), "public tree check found 2 accessibility issue(s); fix content and rebuild")
        self.assertIn("public/about.html: img-alt /style.css\n", err)
        self.assertIn("public/about.html: duplicate-id top\n", err)
        self.assertNotIn("Public tree check passed", out)
        self.assertIsNone(fixes.fix_for(error))

    def test_leak_link_and_a11y_hits_all_printed_one_failure_counts_per_kind(self):
        site_json = self.root / "content" / "site.json"
        data = json.loads(site_json.read_text())
        data["about_md"] = "Never publish paths like /workspace/" + "flockbot/work in copy."
        site_json.write_text(json.dumps(data))
        _, err, error = self.run_build(lambda p: self.append(p, "about.html", '<a href="/nowhere.html"></a>'))
        self.assertRegex(str(error), r"^public tree check found [1-9]\d* potential leak\(s\) and 1 broken link\(s\) "
                                     r"and 1 accessibility issue\(s\); fix content and rebuild$")
        self.assertIn("pilot_host_path", err)
        self.assertIn("public/about.html: broken link /nowhere.html", err)
        self.assertIn("public/about.html: link-text /nowhere.html", err)

    def test_cli_exits_nonzero_once_with_stopped_line(self):
        with mock.patch.object(sys, "argv", ["campaign", "build", "--directory", str(self.root), "--check"]), \
                mock.patch("campaign_tool.site_check.check_accessibility",
                           return_value=[("index.html", "html-lang", "-"), ("x/y.html", "heading-skip", "h2\u2192h4")]), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as stop:
                cli.main()
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("public/index.html: html-lang -\npublic/x/y.html: heading-skip h2\u2192h4\n", err.getvalue())
        self.assertEqual(len(re.findall("Stopped:", err.getvalue())), 1)
        self.assertIn("Stopped: public tree check found 2 accessibility issue(s)", err.getvalue())
        self.assertNotIn("Public tree check passed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
