"""DF1422-14: the finding page's "Cite this finding" text and copy button, and the same string in data/findings.json.

Synthetic content only (temp copies of the fictional example); no network.
Run from the repository root: python3 -B -m unittest tests.test_site_citation
"""
import html as html_module
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import law, site as site_module
from campaign_tool.site import CITE_JS, build_site, citation

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "findings-open-data.schema.json").read_text(encoding="utf-8"))
SLUG = "cedar-policy-posted"
CAMPAIGN = "Cedar County Records Project"
TITLE = "Cedar Police produced a written ALPR usage policy (fictional)"
BASE = "https://cedar-records.example.invalid"
CITE = re.compile(r'<section class="cite" id="cite"><h3>Cite this finding</h3><p class="cite-text">([^<]*)</p>'
                  r'<button type="button" class="cite-copy" hidden data-copied="Copied" data-fallback="Select and copy">Copy</button></section>')


class CitationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-citation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"

    def edit(self, name, change):
        path = self.content / name
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def finding(self, **changes):
        self.edit(f"findings/{SLUG}.json", lambda d: d.update(changes))

    def build(self):
        build_site(self.root)
        page = (self.public / "findings" / f"{SLUG}.html").read_text(encoding="utf-8")
        data = json.loads((self.public / "data" / "findings.json").read_text(encoding="utf-8"))
        return page, data

    def cited(self):
        """(citation text on the page, citation in data/findings.json) for the one finding; page text is unescaped."""
        page, data = self.build()
        match = CITE.search(page)
        self.assertIsNotNone(match, "citation section missing or malformed")
        return html_module.unescape(match.group(1)), data["findings"][0]["citation"]

    def test_no_corrections_and_no_base_url_has_no_corrected_and_no_url(self):
        self.edit("site.json", lambda d: d.update(base_url=None))
        self.finding(corrections=[])
        text, in_json = self.cited()
        expected = f'{CAMPAIGN}. "{TITLE}" (finding fnd_0000000000000001). Published 2026-02-01.'
        self.assertEqual((text, in_json), (expected, expected))
        for needle in ("corrected", "http", "/findings/", SLUG):
            self.assertNotIn(needle, text)

    def test_newest_of_two_corrections_then_a_third_updates_page_and_json(self):
        two = [{"date": "2026-02-10", "note": "Second."}, {"date": "2026-02-03", "note": "First."}]  # not in date order
        self.finding(corrections=two)
        text, in_json = self.cited()
        self.assertEqual(text, in_json)
        self.assertIn("Published 2026-02-01, corrected 2026-02-10", text)
        self.assertNotIn("2026-02-03", text)
        # What `finding correct` writes: one more {date, note} entry in the finding document.
        self.finding(corrections=two + [{"date": "2026-03-04", "note": "Third."}], updated_at="2026-03-04T10:00:00Z")
        text, in_json = self.cited()
        self.assertEqual(text, in_json)
        self.assertIn("Published 2026-02-01, corrected 2026-03-04", text)
        self.assertNotIn("2026-02-10", text)

    def test_base_url_puts_the_absolute_url_last_and_page_and_json_are_byte_equal(self):
        self.finding(corrections=[{"date": "2026-02-03", "note": "Fixed."}])
        text, in_json = self.cited()
        self.assertEqual(text.encode("utf-8"), in_json.encode("utf-8"))
        self.assertEqual(text, f'{CAMPAIGN}. "{TITLE}" (finding fnd_0000000000000001). '
                               f'Published 2026-02-01, corrected 2026-02-03. {BASE}/findings/{SLUG}.html.')
        self.assertEqual(citation({"id": "f1", "title": "T", "slug": "s", "published_at": "2026-01-02T03:04:05Z", "corrections": []},
                                  "C", ""), 'C. "T" (finding f1). Published 2026-01-02.')

    def test_special_characters_escaped_in_html_raw_in_json_and_newline_collapsed(self):
        real = site_module.load_content

        def with_angle_title(root, today=None):  # the content loader refuses "<" in a title; the page must still escape it
            model = real(root, today)
            model["findings"][0]["title"] = 'Say "hi" <b> & more\nsecond   line'
            return model
        with mock.patch.object(site_module, "load_content", with_angle_title):
            page, data = self.build()
        raw = data["findings"][0]["citation"]
        self.assertIn('"Say "hi" <b> & more second line"', raw)
        self.assertNotIn("\n", raw)
        match = CITE.search(page)
        assert match is not None
        self.assertEqual(match.group(1), 'Cedar County Records Project. &quot;Say &quot;hi&quot; &lt;b&gt; &amp; more second line&quot; '
                                         f'(finding fnd_0000000000000001). Published 2026-02-01, corrected 2026-02-03. '
                                         f'{BASE}/findings/{SLUG}.html.')
        self.assertEqual(html_module.unescape(match.group(1)), raw)
        self.assertNotIn("<b>", page.split('class="cite-text">')[1].split("</p>")[0])

    def test_quote_ampersand_and_newline_in_a_loadable_title(self):
        self.finding(title='Say "hi" & more\nsecond   line')
        text, in_json = self.cited()
        self.assertEqual(text, in_json)
        self.assertIn('"Say "hi" & more second line"', in_json)
        self.assertNotIn("\n", in_json)

    def test_without_javascript_the_text_is_visible_and_the_button_stays_hidden(self):
        page, _ = self.build()
        found = re.search(r'<section class="cite" id="cite">.*?</section>', page, flags=re.S)
        assert found is not None
        section = found.group(0)
        self.assertIn(f'<p class="cite-text">{CAMPAIGN}.', section)
        self.assertNotRegex(section.split("<button")[0], r"hidden")  # the text itself is not hidden
        self.assertEqual(len(re.findall(r"<button\b[^>]*\bhidden\b[^>]*>", section)), 1)
        self.assertEqual(section.count("<button"), 1)
        self.assertIn('<script defer src="/findings/cite.js"></script>', page)
        # Only finding pages load it.
        for name in ("index.html", "findings/index.html", "agencies.html"):
            self.assertNotIn("cite.js", (self.public / name).read_text(encoding="utf-8"))
        self.assertEqual((self.public / "findings" / "cite.js").read_text(encoding="utf-8"), CITE_JS)

    def test_cite_js_is_first_party_and_never_writes_html(self):
        self.assertNotRegex(CITE_JS, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval|https?:|import|require\(")
        self.assertIn("textContent", CITE_JS)

    def test_withdrawn_finding_has_no_page_and_no_citation_anywhere(self):
        (self.root / "private" / "withdrawn").mkdir(parents=True)  # what `finding withdraw` does: the file leaves content/
        shutil.move(self.content / "findings" / f"{SLUG}.json", self.root / "private" / "withdrawn" / f"{SLUG}.json")
        build_site(self.root)
        self.assertFalse((self.public / "findings" / f"{SLUG}.html").exists())
        self.assertFalse((self.public / "findings" / "cite.js").exists())
        data = json.loads((self.public / "data" / "findings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["findings"], [])
        for path in self.public.rglob("*"):
            if path.is_file():
                text = path.read_bytes().decode("utf-8", "replace")
                self.assertNotIn("Cite this finding", text, path.name)
                self.assertNotIn("(finding fnd_0000000000000001)", text, path.name)

    def test_open_data_schema_requires_and_accepts_the_citation(self):
        _, data = self.build()
        law.validate(data, SCHEMA)
        del data["findings"][0]["citation"]
        with self.assertRaises(Exception):
            law.validate(data, SCHEMA)


if __name__ == "__main__":
    unittest.main()
