"""Finding pages list up to 5 related published findings: same agency or a shared cited record (RF1510-4).

Synthetic content only: a temporary copy of the fictional example; record hashes are computed at run time; no network.
"""
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.cli import read_config
from campaign_tool.content import load_content
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SHERIFF = "ca-cedar-county-sheriff"
COUNCIL = "ca-cedar-city-council"


def sha(label):
    return hashlib.sha256(f"synthetic rf1510-4 record {label}".encode()).hexdigest()


def related_items(html):
    """(slug, why) per related list item, or None when the page has no related section."""
    if 'id="related"' not in html:
        return None
    section = html.split('<h3 id="related">Related findings</h3>', 1)[1].split('<h3 id="corrections">', 1)[0]
    return re.findall(r'<li><a href="/findings/([^"]+)\.html">.*?<span class="related-why">([^<]*)</span></li>', section)


class RelatedFindingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-related-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.findings = self.root / "content" / "findings"
        self.base = json.loads((self.findings / "cedar-policy-posted.json").read_text())

    def add(self, slug, published_at, labels, agency_id=None, title=None, classification="documented_fact"):
        finding = dict(self.base, id="fnd_" + slug.replace("-", "_"), slug=slug, title=title or f"Synthetic {slug}",
                       published_at=published_at, agency_id=agency_id, classification=classification, corrections=[],
                       event_date="2026-01-20",
                       sources=[{"sha256": sha(label), "locator": f"page:{i + 1}", "title": "Synthetic record"}
                                for i, label in enumerate(labels)])
        (self.findings / f"{slug}.json").write_text(json.dumps(finding))

    def page(self, slug):
        return (self.root / "public" / "findings" / f"{slug}.html").read_text(encoding="utf-8")

    def site(self, model=None):
        return Site(self.root, read_config(self.root), model or load_content(self.root))

    def add_eight(self):
        self.add("a-finding", "2026-03-01T12:00:00+00:00", ["r1", "r2", "r3"], SHERIFF)
        self.add("b-finding", "2026-02-01T12:00:00+00:00", ["r1", "r2"])
        self.add("c-finding", "2026-02-02T12:00:00+00:00", ["r3", "r9"], COUNCIL)
        self.add("d-finding", "2026-02-10T12:00:00+00:00", ["d1"], SHERIFF)
        self.add("e-finding", "2026-02-12T12:00:00+00:00", ["e1"], SHERIFF)
        self.add("f-finding", "2026-02-11T12:00:00+00:00", ["f1"], SHERIFF)
        self.add("g-finding", "2026-02-09T12:00:00+00:00", ["g1"], SHERIFF)
        self.add("h-finding", "2026-03-05T12:00:00+00:00", ["h1"], COUNCIL)

    def test_single_finding_has_no_section_and_matches_baseline(self):
        build_site(self.root)
        page = self.page("cedar-policy-posted")
        self.assertIsNone(related_items(page))
        self.assertNotIn("Related findings", page)
        with mock.patch.object(Site, "related_findings", return_value=[]):
            build_site(self.root)
        self.assertEqual(self.page("cedar-policy-posted"), page)

    def test_shared_records_first_then_three_newest_same_agency(self):
        self.add_eight()
        build_site(self.root)
        items = related_items(self.page("a-finding"))
        self.assertEqual([slug for slug, _ in items], ["b-finding", "c-finding", "e-finding", "f-finding", "d-finding"])
        self.assertEqual([why for _, why in items], ["Shares 2 source records", "Shares 1 source record",
                                                     "Same agency", "Same agency", "Same agency"])
        page = self.page("a-finding")
        self.assertNotIn("h-finding", page)
        self.assertNotIn('href="/findings/a-finding.html"', page)
        self.assertEqual(related_items(self.page("h-finding")), [("c-finding", "Same agency")])
        self.assertEqual(related_items(self.page("b-finding")), [("a-finding", "Shares 2 source records")])

    def test_ties_ordered_by_slug_and_input_order_irrelevant(self):
        for slug in ("t-zulu", "t-alpha", "t-mike"):
            self.add(slug, "2026-02-15T12:00:00+00:00", [slug], SHERIFF)
        self.add("t-anchor", "2026-02-15T12:00:00+00:00", ["anchor"], SHERIFF)
        model = load_content(self.root)
        anchor = next(f for f in model["findings"] if f["slug"] == "t-anchor")
        self.assertEqual([o["slug"] for o, _, _ in self.site(model).related_findings(anchor)],
                         ["t-alpha", "t-mike", "t-zulu"])
        pages = {f["slug"]: self.site(model).finding_html(f)["html"] for f in model["findings"]}
        for seed in range(5):
            shuffled = dict(model, findings=list(model["findings"]))
            random.Random(seed).shuffle(shuffled["findings"])
            site = self.site(shuffled)
            self.assertEqual({f["slug"]: site.finding_html(f)["html"] for f in shuffled["findings"]}, pages, seed)

    def test_null_agency_is_not_same_agency(self):
        self.add("null-one", "2026-02-15T12:00:00+00:00", ["n1"])
        self.add("null-two", "2026-02-16T12:00:00+00:00", ["n2"])
        build_site(self.root)
        self.assertIsNone(related_items(self.page("null-one")))
        self.assertIsNone(related_items(self.page("null-two")))
        model = load_content(self.root)
        for finding in model["findings"]:
            self.assertEqual(self.site(model).related_findings(finding), [], finding["slug"])

    def test_reason_text_singular_plural_and_duplicate_citation_counted_once(self):
        self.add("p-anchor", "2026-02-20T12:00:00+00:00", ["s1", "s2"], SHERIFF)
        self.add("p-one", "2026-02-19T12:00:00+00:00", ["s1", "s1"], SHERIFF)
        self.add("p-two", "2026-02-18T12:00:00+00:00", ["s1", "s2"], SHERIFF)
        build_site(self.root)
        self.assertEqual(related_items(self.page("p-anchor")), [("p-two", "Same agency · Shares 2 source records"),
                                                                ("p-one", "Same agency · Shares 1 source record")])
        self.assertIn('<span class="related-why">Same agency · Shares 1 source record</span>', self.page("p-two"))

    def test_item_shape_escaping_check_and_single_corrections_anchor(self):
        self.add("x-anchor", "2026-02-20T12:00:00+00:00", ["x1"], SHERIFF)
        self.add("x-other", "2026-02-19T12:00:00+00:00", ["x2"], SHERIFF, title='Tricky & "quoted" title')
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                capture_output=True, text=True, cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public tree check passed", result.stdout)
        page = self.page("x-anchor")
        self.assertIn('<li><a href="/findings/x-other.html">Tricky &amp; &quot;quoted&quot; title</a> · '
                      '<span class="status-pill">Documented fact</span> · Event date '
                      '<time datetime="2026-01-20">2026-01-20</time> · <span class="related-why">Same agency</span></li>', page)
        self.assertLess(page.index('Counterevidence considered'), page.index('id="related"'))
        self.assertLess(page.index('id="related"'), page.index('id="corrections"'))
        for path in (self.root / "public" / "findings").glob("*.html"):
            if path.name != "index.html":
                self.assertEqual(path.read_text(encoding="utf-8").count('id="corrections"'), 1, path.name)
        model = load_content(self.root)
        other = next(f for f in model["findings"] if f["slug"] == "x-other")
        other["title"] = 'Raw <script>alert(1)</script> "q"'  # the loader refuses angle brackets; the renderer escapes anyway
        anchor = next(f for f in model["findings"] if f["slug"] == "x-anchor")
        html = self.site(model).finding_html(anchor)["html"]
        section = html.split('id="related"', 1)[1].split('id="corrections"', 1)[0]
        self.assertNotIn("<script>", section)
        self.assertIn("Raw &lt;script&gt;alert(1)&lt;/script&gt; &quot;q&quot;", section)


if __name__ == "__main__":
    unittest.main()
