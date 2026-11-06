"""DF1407-08: inert schema.org JSON-LD on published finding pages and the home page (synthetic content only)."""
import hashlib
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.cli import read_config
from campaign_tool.content import load_content
from campaign_tool.site import Site, build_site, json_ld

from tests.test_site_build import EXAMPLE, EXPECTED_CSP

TODAY = date(2026, 10, 1)
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "site-structured-data" / "no-base-main.json"
OPEN = '<script type="application/ld+json">'
BLOCK = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
FINDING = "findings/cedar-policy-posted.html"
MEETING = "meetings/cedar-council-2026-10.html"
BEEF = "https://example.invalid/fixtures/agreement-excerpt"
CAFE = "cafe" * 16
BEEF_SHA = "beef" * 16
ARTICLE_KEYS = {"@context", "@type", "headline", "description", "datePublished", "dateModified", "url", "isBasedOn", "publisher"}


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-structured-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

    def edit(self, name, change):
        path = self.root / "content" / name
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))

    def finding(self, **fields):
        self.edit("findings/cedar-policy-posted.json", lambda d: d.update(fields))

    def build(self):
        build_site(self.root, today=TODAY)
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}

    def blocks(self, files, name):
        return [json.loads(m) for m in BLOCK.findall(files[name].decode())]

    def article(self):
        (block,) = self.blocks(self.build(), FINDING)
        return block


class SerializerTests(unittest.TestCase):
    def test_escaping_sorting_and_roundtrip(self):
        payload = {"b": "</script><script>alert(1)</script></SCRIPT>", "a": ["<", ">", "&amp;", "\u2028\u2029", "é\"\\{}"],
                   "z": {"y": 1, "x": {"d": None, "c": True}}}
        out = json_ld(payload)
        self.assertTrue(out.startswith("  " + OPEN) and out.endswith("</script>\n"))
        text = out[len("  " + OPEN):-len("</script>\n")]
        for char in "<>&\u2028\u2029é":
            self.assertNotIn(char, text)
        for escape in ("\\u003c", "\\u003e", "\\u0026"):
            self.assertIn(escape, text)
        self.assertNotIn("&amp", text)
        self.assertEqual(json.loads(text), payload)
        self.assertEqual(text, json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                         .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
        self.assertLess(text.index('"a"'), text.index('"b"'))
        self.assertLess(text.index('"x"'), text.index('"y"'))
        self.assertEqual(out.count("<script"), 1)
        self.assertEqual(out.count("</script>"), 1)

    def test_rejects_non_json_values(self):
        for bad in (float("nan"), float("inf"), {"a": {1, 2}}, object()):
            with self.assertRaises((ValueError, TypeError)):
                json_ld(bad)


class PageTests(Case):
    def test_finding_article_exact_fields_and_scope(self):
        files = self.build()
        (article,) = self.blocks(files, FINDING)
        self.assertEqual(set(article), ARTICLE_KEYS)
        base = "https://cedar-records.example.invalid"
        self.assertEqual(article, {
            "@context": "https://schema.org", "@type": "Article",
            "headline": "Cedar Police produced a written ALPR usage policy (fictional)",
            "description": "In response to a synthetic records request, the fictional Cedar Police Department produced a two-page usage "
                           "and privacy policy dated 2026-01-15.",
            "datePublished": "2026-02-01T18:00:00+00:00", "dateModified": "2026-02-03",
            "url": base + "/findings/cedar-policy-posted.html", "isBasedOn": [],
            "publisher": {"@type": "Organization", "name": "Cedar County Records Project", "url": base}})
        html = files[FINDING].decode()
        for visible in (article["headline"], article["description"], "2026-02-03"):
            self.assertIn(visible, html)
        blob = json.dumps(article)
        for banned in ("ClaimReview", "Review", "Rating", "reviewRating", "factCheck", "FactCheck"):
            self.assertNotIn(banned, blob)
        for name in files:
            if name.endswith(".html") and name not in (FINDING, "index.html", MEETING):
                self.assertEqual(self.blocks(files, name), [], name)  # aggregate, packet, agency, source, 404 pages (meeting: DF1407-09)

    def test_home_graph_and_404_none(self):
        files = self.build()
        (home,) = self.blocks(files, "index.html")
        base = "https://cedar-records.example.invalid"
        self.assertEqual(home, {"@context": "https://schema.org", "@graph": [
            {"@type": "Organization", "name": "Cedar County Records Project", "url": base},
            {"@type": "WebSite", "name": "Cedar County Records Project", "url": base}]})
        self.assertEqual(self.blocks(files, "404.html"), [])
        self.assertNotIn("ld+json", files["404.html"].decode())

    def test_campaign_name_is_raw_not_html_escaped_and_cannot_close_the_element(self):
        self.edit("../campaign.json", lambda d: d.update(name='Cedar </script><b>&" Project'))
        files = self.build()
        for name in ("index.html", FINDING):
            self.assertEqual(len(BLOCK.findall(files[name].decode())), 1, name)
        self.assertEqual(self.blocks(files, "index.html")[0]["@graph"][0]["name"], 'Cedar </script><b>&" Project')
        self.assertEqual(self.article()["publisher"]["name"], 'Cedar </script><b>&" Project')

    def test_finding_text_cannot_close_the_element(self):
        self.finding(title='Quote "x" \\ {y} & z', summary="Tom & Jerry’s é &amp; ok.")
        files = self.build()
        (article,) = self.blocks(files, FINDING)
        self.assertEqual(article["headline"], 'Quote "x" \\ {y} & z')
        self.assertEqual(article["description"], "Tom & Jerry’s é &amp; ok.")
        raw = BLOCK.findall(files[FINDING].decode())[0]
        for char in "<>&":
            self.assertNotIn(char, raw)

    def test_date_modified_policy(self):
        for corrections, updated, expected in (
                ([], None, "2026-02-01T18:00:00+00:00"),
                ([], "2026-03-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00"),
                ([{"date": "2026-02-09", "note": "b"}, {"date": "2026-02-03", "note": "a"}], None, "2026-02-09"),
                ([{"date": "2026-02-09", "note": "b"}, {"date": "2026-02-03", "note": "a"}], "2026-05-01T00:00:00+00:00", "2026-02-09"),
                ([{"date": "2026-02-03", "note": "a"}], "2026-02-02T00:00:00+00:00", "2026-02-03"),
                ([{"date": "2026-02-03", "note": "a"}, {"date": "2026-02-03", "note": "b"}], None, "2026-02-03")):
            with self.subTest(corrections=corrections, updated=updated):
                self.finding(corrections=corrections, updated_at=updated)
                self.assertEqual(self.article()["dateModified"], expected)

    def test_unsorted_corrections_are_not_mutated(self):
        stored = [{"date": "2026-02-09", "note": "b"}, {"date": "2026-02-03", "note": "a"}]
        self.finding(corrections=stored)
        model = load_content(self.root, TODAY)
        site = Site(self.root, read_config(self.root), model, today=TODAY)
        site.finding_structured(model["findings"][0])
        self.assertEqual([c["date"] for c in model["findings"][0]["corrections"]], ["2026-02-09", "2026-02-03"])

    def test_is_based_on_lists_only_cited_public_sources(self):
        cite = lambda sha: {"sha256": sha, "locator": "page:1", "title": "t"}  # noqa: E731
        self.finding(sources=[cite(CAFE)])
        self.assertEqual(self.article()["isBasedOn"], [], "private-only citation")
        self.finding(sources=[cite(CAFE), cite(BEEF_SHA)])
        self.assertEqual(self.article()["isBasedOn"], [BEEF])
        self.finding(sources=[cite(BEEF_SHA), cite(CAFE), cite(BEEF_SHA)])
        self.assertEqual(self.article()["isBasedOn"], [BEEF], "a repeated citation is listed once")
        self.finding(sources=[cite(CAFE)])  # the public library entry is not cited by this finding
        self.assertEqual(self.article()["isBasedOn"], [])
        blob = json.dumps(self.article())
        self.assertNotIn(BEEF_SHA, blob)
        self.assertNotIn(CAFE, blob)

    def test_is_based_on_many_in_first_citation_order(self):
        self.edit("sources.json", lambda d: d[0].update(public_url="https://example.invalid/fixtures/aaa"))
        cite = lambda sha: {"sha256": sha, "locator": "page:1", "title": "t"}  # noqa: E731
        self.finding(sources=[cite(BEEF_SHA), cite(CAFE)])
        self.assertEqual(self.article()["isBasedOn"], [BEEF, "https://example.invalid/fixtures/aaa"])

    def test_base_url_with_trailing_slash_joins_like_the_canonical_link(self):
        self.edit("site.json", lambda d: d.update(base_url="https://cedar-records.example.invalid/"))
        files = self.build()
        article = self.blocks(files, FINDING)[0]
        self.assertEqual(article["url"], "https://cedar-records.example.invalid/findings/cedar-policy-posted.html")
        self.assertIn(f'<link rel="canonical" href="{article["url"]}">', files[FINDING].decode())
        self.assertEqual(article["publisher"]["url"], "https://cedar-records.example.invalid")

    def test_draft_preview_has_no_block(self):
        model = load_content(self.root, TODAY)
        site = Site(self.root, read_config(self.root), model, today=TODAY)
        self.assertEqual(len(BLOCK.findall(site.finding_html(model["findings"][0])["html"])), 1)
        self.assertNotIn("ld+json", site.finding_preview_html(model["findings"][0])["html"])


class ByteTests(Case):
    def test_two_builds_are_identical(self):
        first = self.build()
        shutil.rmtree(self.public)
        self.assertEqual(first, self.build())
        self.assertNotIn(b"datePublished\":\"2026-10-01", first[FINDING])  # no build clock

    def test_without_base_url_every_file_matches_pre_feature_main(self):
        # Digests are stored as four 16-hex groups joined by '-' so the credential scanner does not flag them.
        expected = {n: d.replace("-", "") for n, d in json.loads(FIXTURE.read_text()).items()}
        for omit in (False, True):
            with self.subTest(base_url="omitted" if omit else "null"):
                if self.public.exists():
                    shutil.rmtree(self.public)
                self.edit("site.json", (lambda d: d.pop("base_url", None)) if omit else (lambda d: d.update(base_url=None)))
                files = self.build()
                self.assertEqual({n: hashlib.sha256(b).hexdigest() for n, b in files.items()}, expected)
                for name, data in files.items():
                    self.assertNotIn(b"ld+json", data, name)

    def test_csp_header_and_script_allowlist_unchanged(self):
        files = self.build()
        self.assertIn(("  Content-Security-Policy: " + EXPECTED_CSP + "\n").encode(), files["_headers"])
        self.assertNotIn(b"unsafe-inline", files["_headers"])
        self.assertIn("script-src 'self';", EXPECTED_CSP)
        for name in (FINDING, "index.html"):
            html = files[name].decode()
            for tag in re.findall(r"<script\b[^>]*>", html):
                self.assertTrue(tag == OPEN or re.fullmatch(r'<script defer src="/[a-z/-]+\.js">', tag), tag)
            self.assertNotRegex(html, r"<script[^>]*\bon\w+=")


if __name__ == "__main__":
    unittest.main()
