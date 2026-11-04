"""DF2037-14: generated /methods.html defines every finding label; badges link to its anchors.

Synthetic content only (a temp copy of the fictional example); no network.
"""
import contextlib
import hashlib
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import site as site_module
from campaign_tool.content import ContentError
from campaign_tool.labels import CLASSIFICATION_LABELS, CONFIDENCE_LABELS
from campaign_tool.provenance_text import EXPLAINER
from campaign_tool.review import ROLES
from campaign_tool.site import CLASSIFICATION_DEFINITIONS, CONFIDENCE_DEFINITIONS, build_site, check_definitions

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
PILL = re.compile(r'<(a|span) class="status-pill"(?: href="([^"]*)")?>(.*?)</\1>')
METHODS_HREF = re.compile(r'href="/methods\.html#([^"]+)"')


class MethodsPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-methods-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.findings = self.root / "content" / "findings"

    def edit_json(self, path, **changes):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def second_finding(self, **changes):
        path = self.findings / "second-finding.json"
        shutil.copyfile(self.findings / "cedar-policy-posted.json", path)
        self.edit_json(path, id="fnd_second_finding", slug="second-finding", title="Synthetic second", **changes)

    def build(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return build_site(self.root)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def ids(self):
        return set(re.findall(r'\bid="([^"]+)"', self.read("methods.html")))

    # 1. one <dt> per label key with the specified anchors, the EXPLAINER and the three review roles
    def test_methods_page_defines_every_label(self):
        self.build()
        html = self.read("methods.html")
        self.assertIn("<h2>How to read our findings</h2>", html)
        dts = re.findall(r'<dt id="([^"]+)">([^<]*)</dt><dd>([^<]*)</dd>', html)
        expected = ([f"classification-{c}" for c in CLASSIFICATION_LABELS] + [f"confidence-{c}" for c in CONFIDENCE_LABELS])
        self.assertEqual([d[0] for d in dts], expected)  # table order, exactly one each
        self.assertEqual(html.count("<dt"), len(expected))
        self.assertIn('<dt id="classification-apparent_conflict">Apparent conflict</dt>', html)
        self.assertIn('<dt id="confidence-likely">Likely; awaiting further records</dt>', html)
        for code, text in CLASSIFICATION_DEFINITIONS.items():
            self.assertIn(f'<dt id="classification-{code}">', html)
            self.assertLessEqual(len(text), 300, code)
        self.assertIn(EXPLAINER, html)
        self.assertIn("factual, legal and privacy review by at least two people other than the author", html)
        for role in ROLES:
            self.assertIn(role, html)
        self.assertNotIn("<script", html)

    # DF0022-05: generic policy prose only, in its own paragraph after the pinned review sentence
    def test_methods_page_says_reviewers_declare_interests(self):
        self.build()
        html = self.read("methods.html")
        gate = "Until then the build refuses to publish it.</p>"
        paragraph = ("<p>Reviewers declare agency interests privately. A declared interest excludes their review "
                     "from findings about that agency.</p>")
        self.assertIn(gate + paragraph, html)
        self.assertEqual(html.count("declared interest"), 1)

    # 2. every label badge / confidence link on finding pages, the findings index and home resolves in methods.html
    def test_every_badge_links_to_an_existing_anchor(self):
        self.second_finding(classification="information_gap", confidence="likely")
        self.build()
        ids = self.ids()
        pages = ["findings/index.html", "index.html"] + [
            p.relative_to(self.public).as_posix() for p in (self.public / "findings").glob("*.html") if p.name != "index.html"]
        self.assertEqual(len(pages), 5)
        seen = set()
        for name in pages:
            html = self.read(name)
            for tag, href, text in PILL.findall(html):
                if text.startswith("Corrected"):
                    self.assertEqual(tag, "span", name)  # the correction date pill is not a label
                    continue
                self.assertEqual(tag, "a", f"{name}: unlinked badge {text}")
                self.assertRegex(href, r"^/methods\.html#classification-", name)
            anchors = METHODS_HREF.findall(html)
            self.assertTrue(anchors, name)
            for anchor in anchors:
                self.assertIn(anchor, ids, f"{name}: dangling #{anchor}")
            seen.update(anchors)
        self.assertTrue({"classification-documented_fact", "classification-information_gap", "confidence-verified",
                         "confidence-likely"} <= seen, seen)
        second = self.read("findings/second-finding.html")
        self.assertIn('<a class="status-pill" href="/methods.html#classification-information_gap">Information gap</a>', second)
        self.assertIn('Confidence: <a href="/methods.html#confidence-likely">Likely; awaiting further records</a>', second)
        # related-findings pill on the first finding names the second one's classification
        self.assertIn('Synthetic second</a> · <a class="status-pill" href="/methods.html#classification-information_gap">',
                      self.read("findings/cedar-policy-posted.html"))
        self.assertIn('<a href="/methods.html">How to read our findings</a> defines each', self.read("findings/index.html"))

    # 3. a definitions table whose keys differ from the label table is an error, never a silent missing entry
    def test_definition_keys_must_match_label_keys(self):
        check_definitions()
        missing = {k: v for k, v in CLASSIFICATION_DEFINITIONS.items() if k != "redaction"}
        extra = dict(CONFIDENCE_DEFINITIONS, unsure="Not a label.")
        for name, value, needle in (("CLASSIFICATION_DEFINITIONS", missing, "missing ['redaction']"),
                                    ("CONFIDENCE_DEFINITIONS", extra, "extra ['unsure']")):
            with self.subTest(name), mock.patch.object(site_module, name, value):
                with self.assertRaisesRegex(ValueError, re.escape(needle)):
                    check_definitions()
                with self.assertRaises(ValueError):
                    self.build()
                self.assertFalse(self.public.exists())  # refused at plan time: nothing written
        with mock.patch.dict(CLASSIFICATION_LABELS, {"new_label": "New label"}), self.assertRaisesRegex(ValueError, "new_label"):
            check_definitions()

    # 4. sitemap lists /methods.html with base_url; without base_url no sitemap but the footer link stays
    def test_sitemap_and_footer(self):
        self.edit_json(self.root / "content" / "site.json", base_url=BASE)
        self.build()
        self.assertIn(f"<loc>{BASE}/methods.html</loc>", self.read("sitemap.xml"))
        self.assertIn(f'<link rel="canonical" href="{BASE}/methods.html">', self.read("methods.html"))
        self.edit_json(self.root / "content" / "site.json", base_url=None)
        self.build()
        self.assertFalse((self.public / "sitemap.xml").exists())
        for name in ("index.html", "about.html", "methods.html", "404.html", "findings/cedar-policy-posted.html"):
            self.assertIn(' · <a href="/methods.html">How to read our findings</a></p></footer>', self.read(name), name)

    # 5. the slug and the generated path are reserved
    def test_slug_and_public_asset_reserved(self):
        pages = self.root / "content" / "pages"
        pages.mkdir()
        (pages / "methods.json").write_text(json.dumps({"title": "Ours", "body_md": "Text."}), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, "pages/methods.json: slug methods is reserved"):
            self.build()
        (pages / "methods.json").unlink()
        folder = self.root / "content" / "public-assets" / "methods.html"
        folder.mkdir(parents=True)
        (folder / "x.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
        with self.assertRaisesRegex(ContentError, r"public-assets/methods\.html/x\.svg: collides with a generated path"):
            self.build()
        self.assertFalse(self.public.exists())

    # 6. neutral wording: no legal conclusion in any definition
    def test_definitions_make_no_legal_conclusion(self):
        for code, text in {**CLASSIFICATION_DEFINITIONS, **CONFIDENCE_DEFINITIONS}.items():
            for word in ("violat", "illegal", "unlawful", "criminal"):
                self.assertNotIn(word, text.casefold(), code)

    # 7. feed.xml and data/findings.json keep plain labels: identical whatever the definitions or badge links say
    def test_feed_and_open_data_unchanged(self):
        self.build()
        before = {n: hashlib.sha256((self.public / n).read_bytes()).hexdigest() for n in ("feed.xml", "data/findings.json")}
        for name in before:
            self.assertNotIn("methods", self.read(name), name)
        self.assertIn("[Documented fact; Verified against the record;", self.read("feed.xml"))
        reworded = {k: "Reworded." for k in CLASSIFICATION_DEFINITIONS}
        with mock.patch.object(site_module, "CLASSIFICATION_DEFINITIONS", reworded):
            self.build()
        self.assertIn("<dd>Reworded.</dd>", self.read("methods.html"))
        self.assertEqual({n: hashlib.sha256((self.public / n).read_bytes()).hexdigest() for n in before}, before)

    # 8. starter site (no content/): unchanged, no methods page
    def test_starter_site_unchanged(self):
        shutil.rmtree(self.root / "content")
        written = self.build()
        self.assertEqual(sorted(p.name for p in written), ["_headers", "index.html", "style.css"])
        self.assertNotIn("methods", self.read("index.html"))


if __name__ == "__main__":
    unittest.main()
