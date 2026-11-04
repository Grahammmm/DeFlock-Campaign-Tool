"""DF1952-04: content/glossary.json -> /glossary.html, a "Terms used on this page" list on each finding, refusals by name.

Builds temporary copies of the fictional example with synthetic terms; sockets are blocked, nothing is fetched.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.content import (ContentError, glossary_key, glossary_terms_used, load_content, load_glossary,
                                   read_glossary)
from campaign_tool.content_check import content_problems, problem_record
from campaign_tool.site import build_site
from campaign_tool.site_check import check_accessibility, check_links

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 10, 1)
FINDING_PAGE = "findings/cedar-policy-posted.html"
RULE = "ca-civ-1798.90.5-definitions"
RULE_URL = "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1798.90.5."
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))

# sha256 of the no-glossary fictional render with today=2026-10-01. Captured independently
# from PR #910 (4249cded72d58ec5f39305ced48d7cecbea32e7e) and origin/main
# (a918b28912ea859ee7030ca847e6144d0311f5b6), never from the merged candidate:
# source-record pages and links come from that PR; unrelated current-main output comes from main.
# DF2207-11 CSV baselines were captured from untouched origin/main (514d4f03).
# Funding parity: only agencies.csv gains an empty funding column; removing it restores its previous digest.
BASELINE = {
    "404.html": "de1094d7b8cb47ad4d2725c3bbc91e05c5fe550651ebecaca4271bf927531cc0",  # pragma: allowlist secret -- public rendered-file SHA-256
    "_headers": "d8b67b1ea84d17609529363319efc124bc818dfe7a85ae14508268369223978e",  # pragma: allowlist secret -- public rendered-file SHA-256
    "about.html": "4ec2875842d31c45031e6083471eef0367afa6f6c09678a8b49c7cbf1e4a6b59",  # pragma: allowlist secret -- public rendered-file SHA-256
    "agencies.html": "00850bffbeb148f7c255a89c09531aeda4031ddc8235f78ffd4d5c402faf29dd",  # pragma: allowlist secret -- public rendered-file SHA-256
    "agencies/cedar.html": "4971942b62513e828a8da52c32692cc61b75b28c00d600cfe82b9bcd3538ce23",  # pragma: allowlist secret -- public rendered-file SHA-256
    "agency-cards.js": "95d2218da220b6ec4ecd303342c0aaa090f5ea4999b0ffd05b3454c46719ca00",  # pragma: allowlist secret -- public rendered-file SHA-256
    "app.js": "ab63767de14fee8703a11531e24ebcfc4a8b845f986208772f242ef8d447db9e",  # pragma: allowlist secret -- public rendered-file SHA-256
    "cameras.html": "0b8fa66897e3b3e1a0e139758b4f53063c62d330f39be15c1bdd34fbfdb779db",  # pragma: allowlist secret -- public rendered-file SHA-256
    "corrections.html": "aa8cabda98e4f5294ea8b9ff0103a731cb45e69c181ba9552218f486badc72a1",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/agencies.csv": "de226f37600fdaac36bacba18473551b0a724fa89dcd3c8a743a7442cf1bb477",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/agencies.json": "9c3dd727cbd33c726007e50c84daab6bd9d38cffa53e4941aefdff67a707f7bb",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/cameras.geojson": "6778a5778765c0aa2460e0cddf3056e885318d86196a03097f545927165c47f6",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/city-boundaries.geojson": "68593cd8443c2ec42b01e4153cf28386727a9486503eccb5dc42ea1961511f19",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/findings.csv": "c0b05563d22729245783a2fc5953ddc60ed6defa42bc45556f4721f8d6d5880d",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/findings.json": "09d948c3ea4c0e3501f9f5b686f576c42c3f8c720dfb5ab8fcf61440396cc2b7",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/meetings.csv": "561e8d71686d8f37308263f983360308d381f2d39484997a99525a3cff5f49ae",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/meetings.json": "7a024cb7b2a582f5095553011fbc7062e6807a0c75cdb14336a58019959405ee",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/sources.csv": "74580b77eadf3e2b12b77074094208e73aeed7e67d3733dc6dad00d0f1cb0eb3",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/sources.json": "be46346e4e80de79575cc5250b27a921b329f22ffd8dd3a81b18a4a1bc603caf",  # pragma: allowlist secret -- public rendered-file SHA-256
    "feed.xml": "e6c4da912d1ea1bee41a34396d0dfb0187aeed960758830ad033f74afcbabf54",  # pragma: allowlist secret -- public rendered-file SHA-256
    "findings/agency/cedar.xml": "555f536dddc3d3c6134c4b69ce281cc387f75efafbdb1d2ea0dc1dc9fda45414",  # pragma: allowlist secret -- public rendered-file SHA-256
    "findings/cedar-policy-posted.html": "eefcc625293a9151390e6271af3b8d67a99c46bcd2d23ef252175e2e65815bc6",  # pragma: allowlist secret -- public rendered-file SHA-256
    "findings/cite.js": "13f4d137f53aaa98bcc1cf1662769871f5629dda97e5700cf97727fca110cb47",  # pragma: allowlist secret -- public rendered-file SHA-256
    "findings/index.html": "40cc854ea4e41c64ef83bea812f95175fabf625f1a60d2da14d2c9f2efc1722f",  # pragma: allowlist secret -- public rendered-file SHA-256
    "findings/packet-all.html": "621e8a02dbdb1a74803b293d2c5e2c9ccff14bbe34388e8b96d47a89af1726af",  # pragma: allowlist secret -- public rendered-file SHA-256
    "index.html": "b9ce605e67f58e96c0412ad858a3d3bab426730e0fd48f7857fa1032bd595d7a",  # pragma: allowlist secret -- public rendered-file SHA-256
    "meetings.html": "22dcf7bf39c650383e622fc00b8aaa6c189b9ea4a73ad83a1ed8f21e0e34be77",  # pragma: allowlist secret -- public rendered-file SHA-256
    "meetings/cedar-council-2026-10.html": "058ef8d946b7a7ef3388278f57e5f015b9f2e1af9d747700825a1fe90e2a5c95",  # pragma: allowlist secret -- public rendered-file SHA-256
    "methods.html": "e1469baebfdb46a8201ce605d290a270df8bfc71bd14fe55c44271653947c40b",  # pragma: allowlist secret -- public rendered-file SHA-256
    "privacy.html": "4303eda8bef4cc87170ae5864caac586431d2c4fec275dd464c2101cf9805518",  # pragma: allowlist secret -- public rendered-file SHA-256
    "robots.txt": "f520ce90da728c1ea72afb9c9f22a0976b5e2c16ac6aa7256e1218479b4f2b80",  # pragma: allowlist secret -- public rendered-file SHA-256
    "search.html": "c0e38535264dc884144c511d8f129b7041b49206feef8dcf4e48b08c91cefce6",  # pragma: allowlist secret -- public rendered-file SHA-256
    "search.js": "0f6185d1995caa68bb3292cf15417996ee917bf5b3c48c81d05cbd057d0da1f6",  # pragma: allowlist secret -- public rendered-file SHA-256
    "site-data.js": "bbce3111f93457ba0bc47b04ba0876e13bbcafa57305f61a9b046e72d62c0541",  # pragma: allowlist secret -- public rendered-file SHA-256
    "sitemap.xml": "ab05fa8c7d57a393d8997b194fcef21e0e444f2f4eef751c101df532b9fd522e",  # pragma: allowlist secret -- public rendered-file SHA-256
    "sources/beefbeefbeef.html": "e52a34a2d4b3a46eee5166200ca31f5157abe3db09bfadceddf839376b14989f",  # pragma: allowlist secret -- public rendered-file SHA-256
    "sources/cafecafecafe.html": "89576baba1ce994f6bbb7a7f34ebba77d8356254acb984b15f38a49988705e0c",  # pragma: allowlist secret -- public rendered-file SHA-256
    "sources.html": "74e85c0b93832d2d978dfe5c36b92d77700dc11880de2abea58b533f468ab083",  # pragma: allowlist secret -- public rendered-file SHA-256
    "style.css": "640f0eb310ea16f07977566cd01d4298d73fed80b89603ce3d711072ec4aabc0",  # pragma: allowlist secret -- public rendered-file SHA-256
    "timeline.html": "22a4670fa3c5d65575e84b32222b4a15bb8d4ba90168fb9ba1039de3ec80690c",  # pragma: allowlist secret -- public rendered-file SHA-256
}


def term(name, aliases=None, definition="Definition.", **extra):
    return {"term": name, "aliases": ["alias for " + str(name)] if aliases is None else list(aliases),
            "definition_md": definition, **extra}


def document(*terms, **extra):
    return {"schema_version": 1, "terms": list(terms), **extra}


class GlossaryCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-glossary-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.public = self.root / "public"
        self.path = self.content / "glossary.json"
        NO_SOCKETS.start()
        self.addCleanup(NO_SOCKETS.stop)

    def put(self, value, name="glossary.json"):
        path = self.content / name
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8"))
        return path

    def build(self):
        build_site(self.root, today=TODAY)
        return self.tree()

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}

    def digests(self, files=None):
        return {name: hashlib.sha256(data).hexdigest() for name, data in (files or self.tree()).items()}

    def refused(self, value, pattern):
        """build refuses with ``pattern`` and public/ keeps its bytes; content-check lists the same problem."""
        before = self.tree() if self.public.exists() else self.build()
        self.put(value)
        with self.assertRaisesRegex(ContentError, pattern) as caught:
            build_site(self.root, today=TODAY)
        self.assertEqual(self.tree(), before, "a refused build changed public/")
        problems = content_problems(self.root)
        self.assertIn(str(caught.exception), problems, "content-check does not name build's own refusal")
        return problems


class PageTests(GlossaryCase):
    def full(self):
        return document(
            term("Retention period", ["data retention", "retention"], "How long **plates** are kept <b>raw</b> & more."),
            term("ALPR", ["license plate reader", "Automated License Plate Reader"], "A camera that reads plates.",
                 source_url="https://example.invalid/alpr"),
            term("Usage policy", ["usage and privacy policy"], "Rules for use.", rule_id=RULE, source_url=RULE_URL),
            notice_md="Definitions are **plain-language**, not legal advice.")

    def test_sorted_page_anchors_and_footer_sitemap(self):
        self.put(self.full())
        files = self.build()
        page = files["glossary.html"].decode()
        ids = re.findall(r'<h3 id="(term-[a-z0-9-]+)">([^<]*)</h3>', page)
        self.assertEqual(ids, [("term-alpr", "ALPR"), ("term-retention-period", "Retention period"),
                               ("term-usage-policy", "Usage policy")])
        self.assertEqual(len(re.findall(r'\bid="term-', page)), 3)
        all_ids = re.findall(r'\bid="([^"]+)"', page)
        self.assertEqual(len(all_ids), len(set(all_ids)), "duplicate ids on glossary.html")
        for fragment in ("Also called: data retention, retention", "Also called: license plate reader, Automated License Plate Reader",
                         "Definitions are <strong>plain-language</strong>, not legal advice."):
            self.assertIn(fragment, page)
        self.assertLess(page.index("plain-language"), page.index('id="term-alpr"'), "the notice is rendered above the entries")
        self.assertIn("How long <strong>plates</strong> are kept &lt;b&gt;raw&lt;/b&gt; &amp; more.", page)  # markdown is text
        self.assertNotIn("<b>raw</b>", page)
        self.assertIn(f"Law package citation: Civ. Code § 1798.90.5", page)
        self.assertIn(f'<a href="{RULE_URL.replace("&", "&amp;")}" rel="noopener noreferrer">Source</a>', page)
        self.assertIn("Status of this law package: draft, not legally reviewed", page)
        self.assertNotIn("reviewed on", page)  # a draft package is never shown as reviewed
        self.assertIn('<a href="https://example.invalid/alpr" rel="noopener noreferrer">Source</a>', page)
        self.assertEqual(page.count("Law package citation"), 1)  # only the rule-linked term shows a citation
        self.assertIn('<link rel="canonical" href="https://cedar-records.example.invalid/glossary.html">', page)
        self.assertNotIn("<script", page)
        footer = lambda html: re.search(r'<footer class="site-footer">(.*?)</footer>', html, re.S).group(1)  # noqa: E731
        for name, data in files.items():
            if name.endswith(".html") and name != "glossary.html":
                self.assertIn('<a href="/privacy.html">Privacy</a> · <a href="/glossary.html">Glossary</a>', footer(data.decode()), name)
        self.assertEqual(re.findall(r"<loc>([^<]+glossary[^<]*)</loc>", files["sitemap.xml"].decode()),
                         ["https://cedar-records.example.invalid/glossary.html"])
        self.assertEqual(check_links(self.public), [])
        self.assertEqual(check_accessibility(self.public), [])
        self.assertFalse((self.public / "glossary.json").exists())  # the input file is never copied
        self.assertNotIn(b"plain-language**", b"".join(files.values()))  # nor its raw markdown

    def test_input_order_and_definition_edits_do_not_move_anchors(self):
        first = self.full()
        self.put(first)
        before = self.build()
        reordered = dict(first, terms=list(reversed(first["terms"])))
        self.put(reordered)
        self.assertEqual(self.build(), before)  # reordering the JSON changes no byte
        edited = dict(first, terms=[dict(t, definition_md="New words.", aliases=t["aliases"][:1]) for t in first["terms"]])
        self.put(edited)
        page = self.build()["glossary.html"].decode()
        self.assertEqual(re.findall(r'\bid="(term-[^"]+)"', page), ["term-alpr", "term-retention-period", "term-usage-policy"])
        self.assertEqual(self.build(), self.build())  # deterministic

    def test_no_base_url_keeps_page_and_footer_but_no_sitemap(self):
        site = json.loads((self.content / "site.json").read_text(encoding="utf-8"))
        site["base_url"] = None
        self.put(site, "site.json")
        self.put(self.full())
        files = self.build()
        self.assertIn("glossary.html", files)
        self.assertNotIn("sitemap.xml", files)
        self.assertIn('<a href="/glossary.html">Glossary</a>', files["index.html"].decode())
        self.assertNotIn("canonical", files["glossary.html"].decode())

    def test_terms_used_list_on_each_finding_page(self):
        self.put(self.full())
        page = self.build()[FINDING_PAGE].decode()
        # the finding says "ALPR" in its title and "retention period" in its body, "usage and privacy policy" in its summary
        block = re.search(r'<h3 id="glossary-terms">Terms used on this page</h3><ul>(.*?)</ul>', page).group(1)
        self.assertEqual(re.findall(r'<li><a href="(/glossary\.html#term-[a-z0-9-]+)">([^<]+)</a></li>', block),
                         [("/glossary.html#term-alpr", "ALPR"), ("/glossary.html#term-retention-period", "Retention period"),
                          ("/glossary.html#term-usage-policy", "Usage policy")])
        self.assertEqual(page.count("Terms used on this page"), 1)
        glossary_ids = set(re.findall(r'\bid="([^"]+)"', (self.public / "glossary.html").read_text(encoding="utf-8")))
        self.assertTrue(set(re.findall(r'href="/glossary\.html#([^"]+)"', block)) <= glossary_ids)
        self.assertNotIn("Terms used on this page", (self.public / "findings" / "packet-all.html").read_text(encoding="utf-8"))

    def test_no_match_means_no_heading_and_other_places_never_count(self):
        # "ALPR policy" is only in the finding's source title ("Synthetic Cedar Police ALPR policy"), "Zebra" nowhere;
        # a definition that mentions another term does not make that term "used".
        self.put(document(term("ALPR policy", ["alpr policy doc"], "Mentions Zebra."), term("Zebra", ["zebra crossing"], "Mentions ALPR policy."),
                          term("Sources", ["Source records"], "x"), term("Corrections", ["correction"], "x")))
        files = self.build()
        page = files[FINDING_PAGE].decode()
        self.assertNotIn("Terms used on this page", page)  # footer, nav, sources, corrections list never count
        self.assertNotIn("glossary-terms", page)
        glossary = files["glossary.html"].decode()
        self.assertEqual(len(re.findall(r'\bid="term-', glossary)), 4)

    def test_glossary_labels_come_from_labels_json(self):
        self.put(self.full())
        self.put({"schema_version": 1, "labels": {"footer.glossary": "Glosario", "finding.terms": "Términos", "heading.glossary": "Glosario aquí",
                                                   "glossary.also": "Alias: {aliases}", "title.glossary": "Glosario titulo"}}, "labels.json")
        files = self.build()
        self.assertIn("Glosario</a>", files["index.html"].decode())
        self.assertIn("<h2>Glosario aquí</h2>", files["glossary.html"].decode())
        self.assertIn("Alias: data retention, retention", files["glossary.html"].decode())
        self.assertIn("<title>Glosario titulo | ", files["glossary.html"].decode())
        self.assertIn('<h3 id="glossary-terms">Términos</h3>', files[FINDING_PAGE].decode())


class MatchingTests(GlossaryCase):
    def glossary(self, *terms):
        return load_glossary(document(*terms), self.root)

    def used(self, finding, *terms):
        base = {"title": "", "summary": "", "body_md": ""}
        return [t["term"] for t in glossary_terms_used(dict(base, **finding), self.glossary(*terms))]

    def test_whole_word_aliases_and_field_isolation(self):
        alpr = term("ALPR", ["license plate reader"])
        # mixed case, punctuation and markdown around the name
        self.assertEqual(self.used({"title": "An alpr, (ALPR) and **Alpr**."}, alpr), ["ALPR"])
        self.assertEqual(self.used({"body_md": "[link](https://example.invalid/ALPR-info)"}, alpr), ["ALPR"])  # destinations count
        # multiword alias across spaces and a line break, mixed case
        self.assertEqual(self.used({"body_md": "A License\n  Plate   READER was used."}, alpr), ["ALPR"])
        # word characters on either side: digits, underscore, Unicode letters
        for text in ("ALPRS", "ALPR2", "2ALPR", "ALPR_data", "_ALPR", "ÁALPR", "ALPRé", "xalpr", "non-ALPRx"):
            self.assertEqual(self.used({"title": text}, alpr), [], text)
        self.assertEqual(self.used({"title": "ALPRs and ALPR"}, alpr), ["ALPR"])  # a later whole-word hit still counts
        # Unicode names: NFC/NFD and case variants match, a longer word does not
        cafe = term("Café", ["Straße"])
        self.assertEqual(self.used({"title": "the cafe\u0301 opened"}, cafe), ["Café"])
        self.assertEqual(self.used({"summary": "STRASSE, closed"}, cafe), ["Café"])
        self.assertEqual(self.used({"title": "cafés and caféteria"}, cafe), [])
        # a name that starts or ends with punctuation still needs non-word neighbours
        dotted = term("C.A.L.P.R.", ["f.e.a.r."])
        self.assertEqual(self.used({"title": "see c.a.l.p.r. now"}, dotted), ["C.A.L.P.R."])
        self.assertEqual(self.used({"title": "xc.a.l.p.r."}, dotted), [])
        # repeats and alias plus canonical count once; no match, empty fields
        self.assertEqual(self.used({"title": "ALPR", "summary": "ALPR", "body_md": "license plate reader ALPR"}, alpr), ["ALPR"])
        self.assertEqual(self.used({}, alpr), [])
        # fields are searched separately: a phrase split across title and summary is not a match
        phrase = term("records request", ["open records"])
        self.assertEqual(self.used({"title": "Public records", "summary": "request filed"}, phrase), [])
        self.assertEqual(self.used({"summary": "Public records", "body_md": "request filed"}, phrase), [])
        self.assertEqual(self.used({"title": "records", "summary": "x", "body_md": "request"}, phrase), [])
        self.assertEqual(self.used({"body_md": "A records request was filed."}, phrase), ["records request"])  # body only
        self.assertEqual(self.used({"summary": "the Open Records law"}, phrase), ["records request"])  # alias, summary only
        # deterministic canonical order, whatever the order in the text or in the file
        many = [term("Zulu", ["zz"]), term("alpha", ["aa"]), term("Mike", ["mm"])]
        self.assertEqual(self.used({"title": "zulu Mike ALPHA"}, *many), ["alpha", "Mike", "Zulu"])
        self.assertEqual(self.used({"title": "zulu Mike ALPHA"}, *reversed(many)), ["alpha", "Mike", "Zulu"])
        self.assertEqual(glossary_key("  A\u00a0 B\tc "), "a b c")

    def test_names_are_matched_as_literals_not_patterns(self):
        regex = term("a.c", ["x+"])
        self.assertEqual(self.used({"title": "abc and xx"}, regex), [])
        self.assertEqual(self.used({"title": "a.c"}, regex), ["a.c"])
        self.assertEqual(self.used({"title": "x+"}, regex), ["a.c"])  # the alias x+ names the term a.c


class RefusalTests(GlossaryCase):
    def test_duplicate_names_and_anchor_collisions_refused_by_name(self):
        base = self.build()
        cases = [
            (document(term("ALPR", ["plate reader"]), term("Plate Reader", ["other"])),
             r"glossary\.json\.terms\[1\]\.term: name 'Plate Reader' repeats 'plate reader' \(glossary\.json\.terms\[0\]\.aliases\[0\]\)"),
            (document(term("ALPR", ["alpr"])),
             r"glossary\.json\.terms\[0\]\.aliases\[0\]: name 'alpr' repeats 'ALPR' \(glossary\.json\.terms\[0\]\.term\)"),
            (document(term("A", ["x", "X"])), r"terms\[0\]\.aliases\[1\]: name 'X' repeats 'x' \(glossary\.json\.terms\[0\]\.aliases\[0\]\)"),
            (document(term("Cafe\u0301", ["one"]), term("Caf\u00e9", ["two"])),
             r"terms\[1\]\.term: name 'Caf.{1,2}' repeats 'Caf.{1,2}' \(glossary\.json\.terms\[0\]\.term\)"),
            (document(term("Straße", ["one"]), term("STRASSE", ["two"])), r"terms\[1\]\.term: name 'STRASSE' repeats 'Straße'"),
            (document(term("Spaced  out", ["one"]), term("spaced out", ["two"])), r"terms\[1\]\.term: name 'spaced out' repeats 'Spaced  out'"),
            (document(term("A B", ["one"]), term("a-b", ["two"])), r"terms\[1\]\.term: 'a-b' gives anchor term-a-b, which 'A B' already has"),
            (document(term("車", ["one"])), r"terms\[0\]\.term: '車' has no letter or digit a-z/0-9 to build an anchor from"),
        ]
        for value, pattern in cases:
            with self.subTest(pattern):
                problems = self.refused(value, pattern)
                self.assertTrue(all(line.startswith("glossary.json") for line in problems), problems)
                self.assertEqual(self.tree(), base)
                self.path.unlink()
        # distinct names that merely look alike are fine
        self.put(document(term("ALPR", ["plate reader"]), term("ALPRs", ["plate readers"])))
        self.assertIn("glossary.html", self.build())

    def test_invalid_glossary_preserves_public_and_checker_parity(self):
        good = self.build()
        long_name, many = "n" * 101, [term(f"Term {i}", [f"alias {i}"]) for i in range(81)]
        cases = [
            (document(*many), r"glossary\.json\.terms: at most 80 terms"),
            (document(term("A", [])), r"terms\[0\]\.aliases: must be a list of 1 to 3 names"),
            (document(term("A", ["a1", "a2", "a3", "a4"])), r"terms\[0\]\.aliases: must be a list of 1 to 3 names"),
            (document(term("A", "alias")), r"terms\[0\]\.aliases: must be a list of 1 to 3 names"),
            (document(term("A", [7])), r"terms\[0\]\.aliases\[0\]: must be plain single-line text"),
            (document(term("A", [None])), r"terms\[0\]\.aliases\[0\]: must be plain single-line text"),
            (document(term("A", ["<b>"])), r"terms\[0\]\.aliases\[0\]: must be plain single-line text"),
            (document(term("A", ["two\nlines"])), r"terms\[0\]\.aliases\[0\]: must be plain single-line text"),
            (document(term("A", ["   "])), r"terms\[0\]\.aliases\[0\]: must be plain single-line text"),
            (document(term(long_name, ["a"])), r"terms\[0\]\.term: must be plain single-line text of 1-100 characters"),
            (document(term("<script>", ["a"])), r"terms\[0\]\.term: must be plain single-line text"),
            (document(term("bell\x07", ["a"])), r"terms\[0\]\.term: must be plain single-line text"),
            (document(term(7, ["a"])), r"terms\[0\]\.term: must be plain single-line text"),
            (document(term("A", ["aa"], definition="   ")), r"terms\[0\]\.definition_md: must be non-blank text"),
            (document(term("A", ["aa"], definition="x" * 8001)), r"terms\[0\]\.definition_md: must be non-blank text of at most 8000"),
            (document(term("A", ["aa"], definition=None)), r"terms\[0\]\.definition_md: must be non-blank text"),
            (document(term("A", ["aa"], definition="bad\x00byte")), r"terms\[0\]\.definition_md: must be non-blank text"),
            (document(term("A", ["aa"]), notice_md="x" * 1001), r"glossary\.json\.notice_md: must be text of at most 1000"),
            (document(term("A", ["aa"]), notice_md=None), r"glossary\.json\.notice_md: must be text"),
            (document(term("A", ["aa"]), notice_md=5), r"glossary\.json\.notice_md: must be text"),
            (document(term("A", ["aa"]), extra=1), r"glossary\.json: unknown field\(s\) \['extra'\]"),
            (document(term("A", ["aa"], color="red")), r"terms\[0\]: unknown field\(s\) \['color'\]"),
            ({"terms": []}, r"glossary\.json: missing field\(s\) \['schema_version'\]"),
            ({"schema_version": 1}, r"glossary\.json: missing field\(s\) \['terms'\]"),
            ({"schema_version": 2, "terms": []}, r"glossary\.json\.schema_version: must be 1"),
            ({"schema_version": True, "terms": []}, r"glossary\.json\.schema_version: must be 1"),
            ({"schema_version": 1.0, "terms": []}, r"glossary\.json\.schema_version: must be 1"),
            ({"schema_version": 1, "terms": {}}, r"glossary\.json\.terms: must be a list"),
            ([], r"glossary\.json: expected an object"),
            (None, r"glossary\.json: expected an object"),
            (document("not an object"), r"terms\[0\]: expected an object"),
            (document({"term": "A", "aliases": ["aa"]}), r"terms\[0\]: missing field\(s\) \['definition_md'\]"),
            (document(term("A", ["aa"], source_url="http://example.invalid/x")), r"terms\[0\]\.source_url: must be an https:// URL"),
            (document(term("A", ["aa"], source_url="https://user:pw@example.invalid/x")), r"terms\[0\]\.source_url"),  # pragma: allowlist secret -- synthetic rejected URL
            (document(term("A", ["aa"], source_url=None)), r"terms\[0\]\.source_url: must be an https:// URL"),
            (document(term("A", ["aa"], rule_id="")), r"terms\[0\]\.rule_id: must be a non-empty rule id"),
            (document(term("A", ["aa"], rule_id=None, source_url=RULE_URL)), r"terms\[0\]\.rule_id: must be a non-empty rule id"),
            (document(term("A", ["aa"], rule_id=RULE)), r"terms\[0\]\.source_url: is required with rule_id"),
            (document(term("A", ["aa"], rule_id="ca-no-such-rule", source_url=RULE_URL)),
             r"terms\[0\]\.rule_id: 'ca-no-such-rule' is not in the us-ca law package"),
            (document(term("A", ["aa"], rule_id=RULE.upper(), source_url=RULE_URL)), r"terms\[0\]\.rule_id: .* is not in the us-ca law package"),
            (document(term("A", ["aa"], rule_id=RULE, source_url="https://example.invalid/not-a-listed-source")),
             rf"terms\[0\]\.source_url: is not one of the sources the us-ca law package lists for {RULE}"),
        ]
        for value, pattern in cases:
            with self.subTest(pattern):
                self.refused(value, pattern)
                self.assertEqual(self.tree(), good)
        for payload, pattern in (
                (b"\xff\xfe not utf-8", r"glossary\.json: not valid UTF-8 JSON"),
                (b'{"schema_version": 1,', r"glossary\.json: not valid UTF-8 JSON"),
                (b"", r"glossary\.json: not valid UTF-8 JSON"),
                (json.dumps({"schema_version": 1, "terms": [], "notice_md": "x" * (2 * 1024 * 1024)}).encode(), r"glossary\.json: file too large")):
            with self.subTest(pattern):
                self.refused(payload, pattern)

    def test_file_symlink_dangling_link_and_folder_are_refused_not_skipped(self):
        good = self.build()
        real = self.content.parent / "elsewhere.json"
        real.write_text(json.dumps(document(term("A", ["aa"]))), encoding="utf-8")
        for make in (lambda: os.symlink(real, self.path), lambda: os.symlink(self.content.parent / "missing.json", self.path),
                     lambda: self.path.mkdir(), lambda: os.mkfifo(self.path)):
            with self.subTest(make):
                make()
                with self.assertRaisesRegex(ContentError, r"^glossary\.json: must be a regular file, not a symlink or folder$"):
                    build_site(self.root, today=TODAY)
                self.assertEqual(content_problems(self.root), ["glossary.json: must be a regular file, not a symlink or folder"])
                self.assertEqual(self.tree(), good)
                if self.path.is_dir() and not self.path.is_symlink():
                    self.path.rmdir()
                else:
                    self.path.unlink()

    def test_checker_names_every_independent_problem_with_the_file(self):
        self.put(document(term("A", ["aa"], rule_id="nope", source_url=RULE_URL), term("B", ["bb", "BB"]),
                          term("C", ["cc"], definition=" "), notice_md="x" * 1001, extra=1))
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 5, problems)
        self.assertEqual(problems, sorted(problems))
        for line in problems:
            record = problem_record(line)
            self.assertTrue(record["file"].startswith("glossary.json"), line)
            self.assertEqual(record["file"] + ": " + record["message"], line)
        with self.assertRaises(ContentError) as caught:  # build stops at its first refusal, which the checker also lists
            load_content(self.root)
        self.assertIn(str(caught.exception), problems)
        with self.assertRaises(ContentError):
            read_glossary(self.root)
        collected = []
        self.assertIsNone(read_glossary(self.root, collected))
        self.assertEqual(sorted(collected), problems)

    def test_rule_link_needs_a_usable_law_package(self):
        value = document(term("A", ["aa"], rule_id=RULE, source_url=RULE_URL))
        self.put(value)
        self.assertEqual(content_problems(self.root), [])
        config = json.loads((self.root / "campaign.json").read_text(encoding="utf-8"))
        (self.root / "campaign.json").write_text(json.dumps(dict(config, state="ZZ")), encoding="utf-8")
        self.assertEqual(content_problems(self.root),
                         [f"glossary.json.terms[0].rule_id: there is no usable us-zz law package"])
        with self.assertRaisesRegex(ContentError, "no usable us-zz law package"):
            load_content(self.root)
        (self.root / "campaign.json").unlink()
        self.assertEqual(content_problems(self.root),
                         ["glossary.json.terms[0].rule_id: there is no campaign.json, so no law package to check the rule against"])
        (self.root / "campaign.json").write_text("{not json", encoding="utf-8")
        problems = content_problems(self.root)
        self.assertTrue(problems and all(line.startswith("campaign.json") for line in problems), problems)  # named once, not per term
        self.assertEqual(len(problems), 1)

    def test_package_status_is_shown_as_is_and_a_term_without_a_rule_never_reads_the_package(self):
        package = {"status": "reviewed", "reviewed_at": "2026-09-30",
                   "rules": [{"rule_id": "r1", "citation": "Test Code § 1", "sources": [{"url": RULE_URL}]}]}
        self.put(document(term("A", ["aa"], rule_id="r1", source_url=RULE_URL)))
        with mock.patch("campaign_tool.law.load_package", return_value=package) as loader:
            loaded = read_glossary(self.root)
        self.assertEqual([(t["citation"], t["law_status"]) for t in loaded["terms"]], [("Test Code § 1", "reviewed on 2026-09-30")])
        self.assertEqual(loader.call_count, 1)
        self.put(document(term("A", ["aa"], source_url="https://example.invalid/free")))
        with mock.patch("campaign_tool.law.load_package", side_effect=AssertionError("package read")):
            loaded = read_glossary(self.root)
        self.assertNotIn("citation", loaded["terms"][0])
        self.assertNotIn("law_status", loaded["terms"][0])

    def test_validated_model_shape_and_input_cannot_supply_derived_keys(self):
        loaded = load_glossary(document(term("Zeta", ["z"], "d"), term("Alpha", ["a"], "d", rule_id=RULE, source_url=RULE_URL),
                                        notice_md="n"), self.root)
        self.assertEqual([t["term"] for t in loaded["terms"]], ["Alpha", "Zeta"])
        self.assertEqual(loaded["notice_md"], "n")
        self.assertEqual(sorted(loaded["terms"][0]), ["aliases", "anchor", "citation", "definition_md", "law_status", "rule_id",
                                                      "source_url", "term"])
        self.assertEqual(sorted(loaded["terms"][1]), ["aliases", "anchor", "definition_md", "term"])
        self.assertEqual(loaded["terms"][0]["anchor"], "term-alpha")
        for key in ("anchor", "citation", "law_status"):
            with self.assertRaisesRegex(ContentError, rf"unknown field\(s\) \['{key}'\]"):
                load_glossary(document(term("A", ["aa"], **{key: "x"})), self.root)
        self.assertEqual(load_glossary(document(), self.root), {"terms": [], "notice_md": ""})
        self.assertEqual(len(load_glossary(document(*[term(f"T{i}", [f"a{i}"]) for i in range(80)]), self.root)["terms"]), 80)


class OffAndReservedTests(GlossaryCase):
    def test_absent_empty_and_legacy_are_byte_identical(self):
        files = self.build()
        self.assertEqual(self.digests(files), BASELINE)  # the pinned pre-feature render, every file
        for off in (document(), document(notice_md="Not shown."), dict(document(), notice_md="")):
            with self.subTest(off):
                self.put(off)
                self.assertEqual(self.digests(self.build()), BASELINE)
                self.assertNotIn("glossary.html", self.tree())
        self.put(document(term("Zebra", ["zebra crossing"])))  # used by no page
        on = self.build()
        link = ' · <a href="/glossary.html">Glossary</a>'
        for name, data in on.items():  # with a glossary, every page differs from the baseline by its footer link alone
            if name == "glossary.html":
                continue
            text = data.decode()
            if name == "sitemap.xml":
                text = re.sub(r"<url><loc>[^<]*/glossary\.html</loc></url>", "", text)
            elif name.endswith(".html"):
                self.assertEqual(text.count(link), 1, name)
                text = text.replace(link, "")
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), BASELINE[name], name)
        self.assertEqual(sorted(on), sorted([*BASELINE, "glossary.html"]))

    def test_remove_glossary_removes_stale_output(self):
        self.put(document(term("ALPR", ["plate reader"])))
        self.assertIn("glossary.html", self.build())
        self.assertIn("/glossary.html#term-alpr", (self.public / FINDING_PAGE).read_text(encoding="utf-8"))
        self.path.unlink()
        files = self.build()
        self.assertEqual(self.digests(files), BASELINE)
        self.assertFalse((self.public / "glossary.html").exists())
        self.put(document(term("ALPR", ["plate reader"])))
        self.build()
        self.put({"schema_version": 1, "terms": [{}]})  # now invalid: the last good build stays as it was
        before = self.tree()
        with self.assertRaises(ContentError):
            build_site(self.root, today=TODAY)
        self.assertEqual(self.tree(), before)
        self.assertIn("glossary.html", before)

    def test_reserved_explainer_and_asset_collisions(self):
        pages = self.content / "pages"
        pages.mkdir()
        (pages / "glossary.json").write_text(json.dumps({"title": "Mine", "body_md": "Text."}), encoding="utf-8")
        for glossary in (None, document(term("ALPR", ["plate reader"]))):  # reserved with or without a glossary file
            with self.subTest(glossary):
                if glossary:
                    self.put(glossary)
                with self.assertRaisesRegex(ContentError, r"^pages/glossary\.json: slug glossary is reserved$"):
                    build_site(self.root, today=TODAY)
                self.assertEqual(content_problems(self.root), [])  # (the checker has no pages rules; build is the gate)
        (pages / "glossary.json").unlink()
        folder = self.content / "public-assets" / "glossary.html"
        folder.mkdir(parents=True)
        (folder / "logo.png").write_bytes(b"x")
        self.path.unlink()
        with self.assertRaisesRegex(ContentError, r"^public-assets/glossary\.html/logo\.png: collides with a generated path$"):
            build_site(self.root, today=TODAY)
        self.put(document(term("ALPR", ["plate reader"])))
        with self.assertRaisesRegex(ContentError, r"^public-assets/glossary\.html/logo\.png: collides with a generated path$"):
            build_site(self.root, today=TODAY)
        self.assertFalse(self.public.exists())

    def test_glossary_slug_is_reserved_for_findings(self):
        finding = json.loads((self.content / "findings" / "cedar-policy-posted.json").read_text(encoding="utf-8"))
        self.put(dict(finding, id="fnd_0000000000000009", slug="glossary"), "findings/glossary.json")
        with self.assertRaisesRegex(ContentError, "glossary"):
            load_content(self.root)
        self.assertTrue(any("findings/glossary.json" in line for line in content_problems(self.root)))


if __name__ == "__main__":
    unittest.main()
