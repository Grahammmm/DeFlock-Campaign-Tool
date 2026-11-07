"""DF1737-01: ``build --check`` fails on site-local links/fragments that do not resolve.

Synthetic content only; no network (``urllib.request.urlopen`` is patched to raise).
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, site
from campaign_tool.site_check import check_links

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FINDING = "findings/cedar-policy-posted.html"
NO_NETWORK = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network access attempted"))


def page(body, ids=""):
    return f"<!doctype html><html><head><title>t</title></head><body>{ids}{body}</body></html>"


class HandWrittenTreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-links-")
        self.addCleanup(temporary.cleanup)
        self.public = Path(temporary.name) / "public"
        (self.public / "findings").mkdir(parents=True)
        (self.public / "style.css").write_text("body{}")
        (self.public / "my file.css").write_text("body{}")
        (self.public / "sources.html").write_text(page("", '<h2 id="library">Library</h2><a name="old-anchor"></a>'))
        (self.public / "findings" / "index.html").write_text(page("x"))

    def write(self, name, body, ids=""):
        (self.public / name).write_text(page(body, ids))

    def check(self):
        with NO_NETWORK:
            return check_links(self.public)

    def test_page_relative_resolves_against_page_directory(self):
        self.write("index.html", '<a href="sources.html#library">s</a><link rel="stylesheet" href="style.css">')
        self.write("findings/x.html", '<a href="sources.html#library">s</a><a href="../sources.html#library">ok</a>'
                   '<a href="./">idx</a><a href="../">home</a>')
        self.assertEqual(self.check(), [("findings/x.html", "sources.html#library")])

    def test_fragments_need_matching_id_or_anchor_name_on_target(self):
        self.write("index.html", '<a href="/sources.html#library">a</a><a href="/sources.html#old-anchor">b</a>'
                   '<a href="#top">c</a><a href="#missing-here">d</a><a href="/sources.html#nope">e</a>'
                   '<a href="/style.css#anything">css</a><a href="/sources.html#">empty</a>', ids='<p id="top"></p>')
        self.assertEqual(self.check(), [("index.html", "#missing-here"), ("index.html", "/sources.html#nope")])

    def test_external_empty_and_bare_hash_are_skipped_without_network(self):
        self.write("index.html", '<a href="https://example.invalid/missing">h</a><a href="mailto:a@example.invalid">m</a>'
                   '<a href="tel:+15550100">t</a><img src="data:image/png;base64,AAAA" alt=""><a href="blob:x">b</a>'
                   '<script src="//cdn.example.invalid/x.js"></script><a href="#">top</a><a href="">e</a><a>none</a>'
                   '<a href="HTTPS://EXAMPLE.INVALID/">upper</a>')
        self.assertEqual(self.check(), [])

    def test_query_stripped_and_percent_decoded_before_lookup(self):
        self.write("index.html", '<link rel="stylesheet" href="/style.css?v=3"><link rel="stylesheet" href="/my%20file.css">'
                   '<a href="?page=2">self</a><a href="/sources.html?x=1#library">q</a><a href="/my%20file.css?v=1">c</a>')
        self.assertEqual(self.check(), [])
        self.write("index.html", '<link rel="stylesheet" href="/my%20other.css?v=3"><a href="/sources.html?x#lib">q</a>')
        self.assertEqual(self.check(), [("index.html", "/my%20other.css?v=3"), ("index.html", "/sources.html?x#lib")])

    def test_escape_symlink_directory_and_missing_targets_are_broken(self):
        outside = self.public.parent / "outside.css"
        outside.write_text("x")
        os.symlink(outside, self.public / "linked.css")
        os.symlink(self.public / "findings", self.public / "linkdir")
        self.write("index.html", '<a href="/../etc/passwd">p</a><a href="../outside.css">o</a><a href="/linked.css">l</a>'
                   '<a href="/linkdir/index.html">d</a><a href="/findings">dir</a><a href="/nope/">idx</a>'
                   '<a href="/%00.css">nul</a><img src="/img/missing.svg" alt=""><a href="/findings/">ok</a><a href="/">root</a>')
        broken = {href for _, href in self.check()}
        self.assertEqual(broken, {"/../etc/passwd", "../outside.css", "/linked.css", "/linkdir/index.html", "/findings",
                                  "/nope/", "/%00.css", "/img/missing.svg"})

    def test_all_link_attributes_and_srcset_candidates_are_read(self):
        self.write("index.html", '<script src="/gone.js"></script><img src="/style.css" srcset="/a.png 1x, /style.css 2x" alt="">'
                   '<picture><source src="/b.webp"><source srcset="/style.css 480w,/c.webp 800w"></picture>'
                   '<link rel="icon" href="/favicon.ico"><a href="/x.html">x</a><a href="/x.html">dup</a>')
        self.assertEqual(self.check(), sorted([("index.html", h) for h in ("/gone.js", "/a.png", "/b.webp", "/c.webp",
                                                                          "/favicon.ico", "/x.html")]))

    def test_srcset_data_url_comma_is_not_a_candidate_separator(self):
        self.write("index.html", '<img src="/style.css" alt="" srcset="data:image/png;base64,iVBORw0KGgo= 1x, '
                   '/style.css 2x,/gone.png 3x"><source srcset="/style.css,, data:image/gif;base64,R0lG 2x">')
        self.assertEqual(self.check(), [("index.html", "/gone.png")])

    def test_symlinked_page_is_not_scanned_but_its_links_target_is_broken(self):
        self.write("index.html", '<a href="/alias.html">a</a>')
        os.symlink(self.public / "sources.html", self.public / "alias.html")
        self.assertEqual(self.check(), [("index.html", "/alias.html")])


class BuiltSiteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-links-build-")
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
    def append_link(public, name, href):
        path = public / name
        path.write_text(path.read_text(encoding="utf-8").replace("</body>", f'<a href="{href}">x</a></body>'))

    def test_fictional_example_passes_with_real_links(self):
        out, err, error = self.run_build()
        self.assertIsNone(error, err)
        self.assertEqual(err, "sources.zip skipped: no local public source copies\n")  # DF0022-11: the fictional library has no local public copy
        self.assertIn("Public tree check passed (pattern scan, internal links and accessibility pattern lint; "
                      "not an accessibility audit; human review still required).", out)
        hrefs = "".join(p.read_text(encoding="utf-8") for p in self.public.rglob("*.html"))
        for expected in ('href="/findings/"', 'href="/agencies/', 'href="/sources.html#', "#past-meetings"):
            self.assertIn(expected, hrefs)
        self.assertIn('href="#content"', (self.public / "index.html").read_text(encoding="utf-8"))  # skip link
        self.assertEqual(check_links(self.public), [])

    def test_missing_finding_target_is_named_and_fails(self):
        out, err, error = self.run_build(lambda p: self.append_link(p, FINDING, "/findings/missing.html"))
        self.assertIsNotNone(error)
        self.assertIn("1 broken link(s)", str(error))
        self.assertIn(f"public/{FINDING}: broken link /findings/missing.html", err)
        self.assertNotIn("Public tree check passed", out)

    def test_agency_fragment_checked_against_agencies_page(self):
        self.run_build()
        slugs = re.findall(r'<article id="([^"]+)"', (self.public / "agencies.html").read_text(encoding="utf-8"))
        self.assertTrue(slugs)

        def inject(public):
            self.append_link(public, "index.html", f"/agencies.html#{slugs[0]}")
            self.append_link(public, "index.html", "/agencies.html#no-such-agency")
        _, err, error = self.run_build(inject)
        self.assertIn("1 broken link(s)", str(error))
        self.assertIn("public/index.html: broken link /agencies.html#no-such-agency", err)
        self.assertNotIn(f"#{slugs[0]}\n", err)

    def test_leak_and_broken_link_both_reported_in_one_failure_after_swap(self):
        site_json = self.root / "content" / "site.json"
        data = json.loads(site_json.read_text())
        data["about_md"] = "Never publish paths like /workspace/" + "flockbot/work in copy."
        site_json.write_text(json.dumps(data))
        _, err, error = self.run_build(lambda p: self.append_link(p, "about.html", "/nowhere.html"))
        self.assertIn("potential leak(s)", str(error))
        self.assertIn("1 broken link(s)", str(error))
        self.assertIn("pilot_host_path", err)
        self.assertIn("public/about.html: broken link /nowhere.html", err)
        self.assertIn("/nowhere.html", (self.public / "about.html").read_text(encoding="utf-8"))

    def test_cli_exit_nonzero_and_starter_mode_passes(self):
        cmd = [sys.executable, "-B", "-m", "campaign_tool"]
        starter = self.root.parent / "starter"
        init = subprocess.run(cmd + ["init", "--directory", str(starter), "--name", "Synthetic Campaign",
                                     "--county", "Synthetic County", "--state", "ca"],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(init.returncode, 0, init.stderr)
        result = subprocess.run(cmd + ["build", "--directory", str(starter), "--check"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public tree check passed (pattern scan, internal links and accessibility", result.stdout)
        self.assertFalse((starter / "content").exists())
        with mock.patch.object(sys, "argv", ["campaign", "build", "--directory", str(self.root), "--check"]), \
                mock.patch("campaign_tool.site_check.check_links", return_value=[("index.html", "/x.html")]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as stop:
                cli.main()
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("public/index.html: broken link /x.html", err.getvalue())
        self.assertIn("Stopped: public tree check found 1 broken link(s)", err.getvalue())


if __name__ == "__main__":
    unittest.main()
