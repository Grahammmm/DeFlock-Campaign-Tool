"""Finding sources[].page links to the public copy (DF2037-04), built from a temp copy of the fictional example.

Synthetic content only; no network. Hex fixtures are generated at test time (no hash literals in this file).
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import content
from campaign_tool.content import ContentError, check_source_pages, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.site import Site, build_site, page_link

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
SLUG = "cedar-policy-posted"
PDF = "https://records.example.invalid/files/contract.pdf"
WHERE = f"findings/{SLUG}.sources[0].page"


class FindingPageLinkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-page-link-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.library = json.loads((self.content / "sources.json").read_text(encoding="utf-8"))
        self.sha = self.library[0]["sha256"]  # the example finding cites library entry 0

    def edit(self, name, change):
        path = self.content / name
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def setup_page(self, page, public_url: "str | None" = PDF, pages: "int | None" = 12, in_library=True):
        def library(entries):
            entries[0].update(public_url=public_url, pages=pages)
            if not in_library:
                entries[0]["sha256"] = "0" * 64
        self.edit("sources.json", library)
        self.edit(f"findings/{SLUG}.json", lambda d: d["sources"][0].update(page=page))

    def finding_html(self):
        build_site(self.root)
        return (self.root / "public" / "findings" / f"{SLUG}.html").read_text(encoding="utf-8")

    def findings_json(self):
        build_site(self.root)
        return json.loads((self.root / "public" / "data" / "findings.json").read_text(encoding="utf-8"))

    def assert_parity(self, expected):
        """build and content-check over the same tree name the same problem set (DF2037-04 amendment)."""
        problems = content_problems(self.root)
        try:
            load_content(self.root)
            built = []
        except ContentError as exc:
            built = [str(exc)]
        self.assertEqual(problems, expected)
        self.assertEqual(built, expected)

    # 1. PDF public copy with pages: link with #page=N; findings.json carries page.
    def test_pdf_link_and_open_data_page(self):
        self.setup_page(7)
        html = self.finding_html()
        self.assertIn(f'<p class="record-page"><a href="{PDF}#page=7" rel="noopener noreferrer">'
                      'Open page 7 of the public copy</a></p>', html)
        self.assertLess(html.index("Synthetic Cedar Police ALPR policy"), html.index('class="record-page"'))
        self.assertEqual(self.findings_json()["findings"][0]["sources"][0]["page"], 7)
        self.assert_parity([])

    # 2. Non-PDF path: bare URL and "(page N)" text; PDF path with a query keeps the query before the fragment.
    def test_non_pdf_url_is_linked_without_fragment(self):
        url = "https://records.example.invalid/files/contract.html"
        self.setup_page(7, public_url=url)
        html = self.finding_html()
        self.assertIn(f'<a href="{url}" rel="noopener noreferrer">Open the public copy (page 7)</a>', html)
        self.assertNotIn("#page=", html)

    def test_upper_case_pdf_with_query_keeps_query(self):
        url = "https://records.example.invalid/files/CONTRACT.PDF?download=1"
        self.setup_page(7, public_url=url)
        self.assertIn(f'<a href="{url}#page=7" rel="noopener noreferrer">Open page 7 of the public copy</a>',
                      self.finding_html())

    def test_pdf_only_in_query_is_not_a_pdf(self):
        url = "https://records.example.invalid/view?file=contract.pdf"
        self.setup_page(3, public_url=url)
        self.assertIn("Open the public copy (page 3)", self.finding_html())

    # 3. A page above the copy's pages: refused by build and content-check; pages null or 0 has no upper check.
    def test_page_above_pages_refused_by_both(self):
        self.setup_page(13)
        self.assert_parity([f"{WHERE}: above the copy's 12 pages"])
        with self.assertRaisesRegex(ContentError, r"sources\[0\]\.page: above the copy's 12 pages"):
            build_site(self.root)

    def test_page_equal_to_pages_is_accepted(self):
        self.setup_page(12)
        self.assertIn("#page=12", self.finding_html())
        self.assert_parity([])

    def test_unknown_or_zero_pages_has_no_upper_check(self):
        for pages in (None, 0):
            with self.subTest(pages=pages):
                self.setup_page(900, pages=pages)
                self.assert_parity([])
                self.assertIn("#page=900", self.finding_html())

    # 4. No library entry, or no public copy: two distinct refusals.
    def test_source_not_in_library_refused(self):
        self.setup_page(2, in_library=False)
        self.assert_parity([f"{WHERE}: source is not in the library"])

    def test_missing_sources_json_means_not_in_library(self):
        self.setup_page(2)
        (self.content / "sources.json").unlink()
        self.assert_parity([f"{WHERE}: source is not in the library"])

    def test_source_without_public_copy_refused(self):
        self.setup_page(2, public_url=None)
        self.assert_parity([f"{WHERE}: the source has no public copy"])

    def test_several_problems_content_check_lists_all_build_refuses_one(self):
        self.setup_page(13)
        second = {"sha256": "1" * 64, "locator": "Section 2", "page": 1}
        self.edit(f"findings/{SLUG}.json", lambda d: d["sources"].append(second))
        problems = content_problems(self.root)
        self.assertEqual(problems, [f"{WHERE}: above the copy's 12 pages",
                                    f"findings/{SLUG}.sources[1].page: source is not in the library"])
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        self.assertIn(str(caught.exception), problems)

    def test_broken_library_is_named_once_without_page_lines(self):
        self.setup_page(13)
        self.edit("sources.json", lambda entries: entries[0].update(agency_id="Not An Id"))
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("sources.json[0].agency_id", problems[0])
        with self.assertRaisesRegex(ContentError, r"sources.json\[0\]\.agency_id"):
            load_content(self.root)

    def test_check_source_pages_is_the_shared_rule(self):
        findings = [{"slug": "s", "sources": [{"sha256": self.sha, "page": None}, {"sha256": self.sha, "page": 5}]}]
        library = [{"sha256": self.sha, "public_url": PDF, "pages": 4}]
        self.assertEqual(check_source_pages(findings, library), ["findings/s.sources[1].page: above the copy's 4 pages"])

    # 5. load_finding refuses anything but a whole number from 1 to 100000.
    def test_load_finding_refuses_bad_pages(self):
        document = json.loads((self.content / "findings" / f"{SLUG}.json").read_text(encoding="utf-8"))
        for bad in (0, -1, 1.5, "7", True, False, None, 100001, [7]):
            with self.subTest(page=bad):
                document["sources"][0]["page"] = bad
                with self.assertRaisesRegex(ContentError, r"^f\.sources\[0\]\.page: a whole number of 1 or more$"):
                    content.load_finding(document, "f")
        for good in (1, 100000):
            document["sources"][0]["page"] = good
            self.assertEqual(content.load_finding(document, "f")["sources"][0]["page"], good)
        del document["sources"][0]["page"]
        self.assertIsNone(content.load_finding(document, "f")["sources"][0]["page"])

    # 6. A public_url that already has a fragment: bare URL, no second fragment.
    def test_existing_fragment_is_not_doubled(self):
        for url in (PDF + "#toc", PDF + "#"):
            with self.subTest(url=url):
                self.setup_page(4, public_url=url)
                html = self.finding_html()
                self.assertIn(f'<a href="{url}" rel="noopener noreferrer">Open the public copy (page 4)</a>', html)
                self.assertNotIn("#page=", html)

    # 7. Without page: no link, no page key; _assert_public still refuses any other new key.
    def test_without_page_nothing_is_added(self):
        self.edit("sources.json", lambda entries: entries[0].update(public_url=PDF, pages=12))
        self.assertNotIn("record-page", self.finding_html())
        self.assertNotIn("page", self.findings_json()["findings"][0]["sources"][0])
        self.assert_parity([])

    def test_open_data_still_refuses_other_source_keys(self):
        original = Site._public_finding

        def leaky(site, finding):
            value = original(site, finding)
            value["sources"][0]["pages"] = 12
            return value
        with mock.patch.object(Site, "_public_finding", leaky):
            with self.assertRaisesRegex(ContentError, "data/findings.json: non-public key pages"):
                build_site(self.root)

    # 8. HTML escaping of the href ("&" passes the library loader; '"' is refused there, so page_link is called direct).
    def test_href_is_escaped(self):
        url = "https://records.example.invalid/files/a.pdf?x=1&y=2"
        self.setup_page(5, public_url=url)
        self.assertIn('href="https://records.example.invalid/files/a.pdf?x=1&amp;y=2#page=5"', self.finding_html())
        self.assertEqual(page_link({"public_url": 'https://h.example.invalid/a.pdf?q="x"&r=<'}, 5),
                         '<p class="record-page"><a href="https://h.example.invalid/a.pdf?q=&quot;x&quot;&amp;r=&lt;'
                         '#page=5" rel="noopener noreferrer">Open page 5 of the public copy</a></p>')


if __name__ == "__main__":
    unittest.main()
