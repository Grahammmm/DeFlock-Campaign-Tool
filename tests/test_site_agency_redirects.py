"""RF1405-2: an organizer may redirect a retired agency-page URL (/agencies/<slug>.html) to a page the build emits,
while live agency pages, calendars and folders stay protected. Synthetic content only; no network."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.redirects import load_redirects
from campaign_tool.site import build_site
from tests.test_site_build import FILES

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
LIVE = "/agencies/cedar.html"
BUILT = ["index.html", "about.html", "agencies.html", "agencies/cedar.html", "agencies/cedar.ics",
         "agencies/other.html", "404.html"]


def doc(*pairs):
    return {"schema_version": 1, "redirects": [{"from": a, "to": b} for a, b in pairs]}


class LoaderTests(unittest.TestCase):
    def refusal(self, *pairs, built=BUILT):
        with self.assertRaises(ContentError) as caught:
            load_redirects(doc(*pairs), built, set())
        return str(caught.exception).splitlines()

    def test_retired_agency_html_sources_accepted_and_sorted(self):
        text = load_redirects(doc(("/agencies/old-b.html", LIVE), ("/agencies/old-a.html", LIVE)), BUILT, set())
        self.assertEqual(text, f"/agencies/old-a.html {LIVE} 301\n/agencies/old-b.html {LIVE} 301\n")
        reversed_text = load_redirects(doc(("/agencies/old-a.html", LIVE), ("/agencies/old-b.html", LIVE)), BUILT, set())
        self.assertEqual(text, reversed_text)

    def test_slug_grammar_boundaries(self):
        longest = "a" + "b" * 63
        load_redirects(doc((f"/agencies/{longest}.html", LIVE), ("/agencies/a.html", LIVE)), BUILT, set())
        for bad in ("/agencies/1abc.html", "/agencies/Abc.html", f"/agencies/{longest}b.html", "/agencies/a/b.html",
                    "/agencies/old.ics", "/agencies/old.js", "/agencies/old.json", "/agencies/old%2ehtml",
                    "/agencies/old.html/", "/agencies/", "/agencies", "/agencies/.html", "/agencies/old"):
            self.assertEqual(self.refusal((bad, LIVE)), [f"redirects.json: redirects[0]: from {bad} is a page this build writes"],
                             bad)

    def test_live_agency_page_alias_folder_and_calendar_refused(self):
        for source in (LIVE, "/agencies/cedar", "/agencies/cedar/", "/agencies/", "/agencies", "/agencies/cedar.ics",
                       "/agencies/other.html", "/agencies.html"):
            self.assertEqual(self.refusal((source, "/about.html")),
                             [f"redirects.json: redirects[0]: from {source} is a page this build writes"], source)

    def test_target_and_guards_still_refuse(self):
        lines = self.refusal(("/agencies/old.html", "/agencies/gone.html"), ("/agencies/old2.html", "https://x.example/"),
                             ("/agencies/old3.html", "/agencies/old3.html"), ("/agencies/old4.html", LIVE),
                             ("/agencies/old4.html", "/about.html"), ("/agencies/old5.html", "/agencies/old6.html"),
                             ("/agencies/old6.html", LIVE))
        self.assertEqual(lines, [
            "redirects.json: redirects[4]: from /agencies/old4.html repeats redirects[3]",
            "redirects.json: redirects[0]: to /agencies/gone.html is not a page this build writes",
            "redirects.json: redirects[1]: to 'https://x.example/' is external; only pages this build writes are targets",
            "redirects.json: redirects[2]: from /agencies/old3.html points at itself (a loop)",
            "redirects.json: redirects[5] -> redirects[6]: a chain (/agencies/old5.html -> /agencies/old6.html -> "
            "/agencies/cedar.html); point redirects[5] at the final page"])


class BuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-agency-redirects-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "redirects.json"

    def write(self, *pairs):
        self.path.write_text(json.dumps(doc(*pairs)), encoding="utf-8")

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def test_retired_agency_redirect_build_and_check_agree(self):
        build_site(self.root)
        before = self.tree()
        self.assertEqual(set(before), set(FILES))
        self.assertFalse((self.public / "agencies" / "old-agency.html").exists())
        self.write(("/agencies/old-agency.html", LIVE))
        self.assertEqual(content_problems(self.root), [])
        build_site(self.root)
        after = self.tree()
        self.assertEqual(after.pop("_redirects"), b"/agencies/old-agency.html /agencies/cedar.html 301\n")
        self.assertEqual(after, before)  # no alias page, no extra file, no changed byte
        self.assertFalse((self.public / "agencies" / "old-agency.html").exists())

    def test_live_agency_sources_refused_and_previous_tree_kept(self):
        build_site(self.root)
        before = self.tree()
        for source in (LIVE, "/agencies/cedar", "/agencies/", "/agencies/cedar.ics"):
            self.write((source, "/about.html"))
            expected = [f"redirects.json: redirects[0]: from {source} is a page this build writes"]
            with self.assertRaises(ContentError) as caught:
                build_site(self.root)
            self.assertEqual(str(caught.exception).splitlines(), expected)
            self.assertEqual(content_problems(self.root, limit=None), expected)
            self.assertEqual(self.tree(), before, source)

    def test_missing_agency_target_refused_by_build_and_check(self):
        self.write(("/agencies/old-agency.html", "/agencies/nobody.html"))
        expected = ["redirects.json: redirects[0]: to /agencies/nobody.html is not a page this build writes"]
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertEqual(str(caught.exception).splitlines(), expected)
        self.assertEqual(content_problems(self.root, limit=None), expected)
        self.assertFalse(self.public.exists())

    def test_no_redirects_input_leaves_default_output_and_content_check_writes_nothing(self):
        self.assertEqual(content_problems(self.root), [])
        self.assertFalse(self.public.exists())
        build_site(self.root)
        self.assertEqual(set(self.tree()), set(FILES))
        self.write()
        shutil.rmtree(self.public)
        build_site(self.root)
        self.assertEqual(set(self.tree()), set(FILES))


if __name__ == "__main__":
    unittest.main()
