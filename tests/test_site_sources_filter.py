"""DF1103-12: sources.html filter (agency, received year, cited by a finding) and sources/filter.js.

Synthetic content only: temporary copies of the fictional example; no network, no private reads. The behaviours of the
generated script itself (combined filters, hash state, anchors, label transport) run in tests/site-sources-filter.test.mjs.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.site import EXTRA_CSS, SOURCES_FILTER_ASSET, Site, build_site
from tests.test_site_build import EXPECTED_CSP

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 3, 1)
POLICE, SHERIFF, UNKNOWN = "ca-cedar-police", "ca-cedar-county-sheriff", "ca-zz-unlisted"
CAFE, BEEF = "cafe" * 16, "beef" * 16
EDGE, OLD, LOST = "1a2b" * 16, "3c4d" * 16, "5e6f" * 16
NEW_LABELS = {"sources.filter_label": "Filtrar registros", "sources.filter_agency": "Agencia", "sources.filter_year": "Año de recibo",
              "sources.filter_all_agency": "Todas las agencias", "sources.filter_all_year": "Todos los años",
              "sources.filter_cited": "Citado por un hallazgo", "sources.filter_empty": "Ningún registro coincide."}
ENTRY_TAG = re.compile(r'<li class="source-entry" data-agency="([^"]*)" data-year="(\d{4})" data-cited="(true|false)" data-record="([0-9a-f]{12})">')
HEAD_SCRIPTS = re.compile(r'<script defer src="([^"]+)"></script>')


def entry(sha, fragment, agency, received, title):
    return {"sha256": sha, "title": title, "agency_id": agency, "received_at": received, "public_url": None, "fragment": fragment}


class SourcesFilterCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sources-filter-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"
        self.library = json.loads((self.content / "sources.json").read_text(encoding="utf-8"))

    def write_library(self, library):
        (self.content / "sources.json").write_text(json.dumps(library), encoding="utf-8")

    def rich(self):
        """Two police records (one cited), a sheriff record received 23:30 on 2025-12-31 at -08:00, a sheriff record cited by
        a second finding and an agency id the campaign never listed."""
        self.library += [
            entry(EDGE, "cedar-sheriff-records", SHERIFF, "2025-12-31T23:30:00-08:00", "Synthetic sheriff edge record"),
            entry(OLD, "cedar-sheriff-records", SHERIFF, "2024-06-01T10:00:00+00:00", "Synthetic sheriff older record"),
            entry(LOST, "unlisted-records", UNKNOWN, "2024-02-02T08:00:00+00:00", "Synthetic unlisted record")]
        self.write_library(self.library)
        base = json.loads((self.content / "findings" / "cedar-policy-posted.json").read_text(encoding="utf-8"))
        finding = dict(base, id="fnd_0000000000000002", slug="synthetic-sheriff-cites", title="Synthetic sheriff cites", corrections=[],
                       sources=[{"sha256": OLD, "locator": "page:1", "title": "Synthetic sheriff older record"}])
        (self.content / "findings" / "synthetic-sheriff-cites.json").write_text(json.dumps(finding), encoding="utf-8")

    def build(self):
        return {p.relative_to(self.public).as_posix() for p in build_site(self.root, today=TODAY)}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def labels(self, labels):
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")


class HandoffTests(SourcesFilterCase):
    def test_no_js_full_groups_and_handoff(self):
        self.rich()
        self.build()
        html = self.read("sources.html")
        entries = ENTRY_TAG.findall(html)
        # -11's hooks on every row, in library order inside each group (groups in first-appearance order)
        order = [s for fragment in dict.fromkeys(s["fragment"] for s in self.library) for s in self.library if s["fragment"] == fragment]
        self.assertEqual([e[3] for e in entries], [s["sha256"][:12] for s in order])
        self.assertEqual({e[3]: (e[0], e[1], e[2]) for e in entries}, {
            "cafecafecafe": (POLICE, "2026", "true"), "beefbeefbeef": (POLICE, "2026", "false"),
            "1a2b1a2b1a2b": (SHERIFF, "2025", "false"),  # stored calendar year; UTC would say 2026
            "3c4d3c4d3c4d": (SHERIFF, "2024", "true"), "5e6f5e6f5e6f": (UNKNOWN, "2024", "false")})
        for fragment in ("cedar-police-records", "cedar-sheriff-records", "unlisted-records"):
            self.assertIn(f'<section class="agency-document-group" id="{fragment}">', html)
        for source in self.library:
            self.assertIn(f'href="/sources/{source["sha256"][:12]}.html"', html)
        # the full list is unchanged without JavaScript: only the form and the empty message start hidden
        self.assertEqual(re.findall(r'<[a-z]+ [^>]*\bhidden\b[^>]*>', html), [
            re.search(r'<form [^>]*hidden>', html).group(0), re.search(r'<p [^>]*id="sources-filter-empty" hidden>', html).group(0)])
        self.assertNotRegex(html, r'<(?:li|section)\b[^>]*\bhidden')

    def test_list_is_byte_identical_to_the_page_without_the_filter(self):
        self.rich()
        self.build()
        html = self.read("sources.html")
        with mock.patch.object(Site, "sources_filter", lambda site: ""):
            self.build()
        plain = self.read("sources.html")
        stripped = re.sub(r'<form class="sources-filter".*?</form>', "", html, flags=re.S)
        stripped = re.sub(r'<p class="sources-filter-empty"[^>]*>[^<]*</p>', "", stripped)
        stripped = stripped.replace('  <script defer src="/site-data.js"></script>\n  <script defer src="/sources/filter.js"></script>\n', "")
        self.assertEqual(stripped, plain)
        self.assertNotIn("<script", plain)


class StructureTests(SourcesFilterCase):
    def options(self, html, key):
        found = re.search(rf'<label for="sources-filter-{key}">([^<]+)</label><select id="sources-filter-{key}" name="{key}">(.*?)</select>', html)
        return found.group(1), re.findall(r'<option value="([^"]*)">([^<]*)</option>', found.group(2))

    def test_empty_and_single_record(self):
        self.write_library([])
        written = self.build()
        html = self.read("sources.html")
        for needle in ("sources-filter", "filter.js", "<form", "site-data.js"):
            self.assertNotIn(needle, html)
        self.assertNotIn("sources/filter.js", written)
        self.assertNotIn("LABELS", self.read("site-data.js"))  # the old absence: no source-count transport either
        self.write_library(self.library[:1])
        written = self.build()
        html = self.read("sources.html")
        self.assertIn("sources/filter.js", written)
        self.assertIn("sources/cafecafecafe.html", written)
        self.assertEqual(self.options(html, "agency"), ("Agency", [("all", "All agencies"), (POLICE, "Cedar Police Department (fictional)")]))
        self.assertEqual(self.options(html, "year"), ("Received year", [("all", "All years"), ("2026", "2026")]))
        self.assertIn('<input type="checkbox" id="sources-filter-cited" name="cited" value="1"> Cited by a finding</label>', html)
        self.assertIn('<p class="sources-filter-count" id="sources-filter-count" aria-live="polite">Showing 1 of 1</p></form>', html)
        self.assertIn('aria-label="Filter source records"', html)
        self.assertIn('<p class="sources-filter-empty" id="sources-filter-empty" hidden>No source record matches these filters.</p>', html)
        self.assertEqual(self.read("site-data.js").count('var LABELS={"js.filter_count":"Showing {shown} of {total}"};\n'), 1)
        self.write_library([])  # an empty rebuild removes the stale asset and keeps the page honest
        written = self.build()
        self.assertNotIn("sources/filter.js", written)
        self.assertFalse((self.public / "sources" / "filter.js").exists())
        self.assertFalse((self.public / "sources" / "cafecafecafe.html").exists())

    def test_options_ordered_and_special_characters_escaped(self):
        self.rich()
        agencies = json.loads((self.content / "agencies.json").read_text(encoding="utf-8"))
        for agency in agencies:
            if agency["agency_id"] == SHERIFF:
                agency["name"] = 'Zeta "Sheriff" & Co'
        (self.content / "agencies.json").write_text(json.dumps(agencies), encoding="utf-8")
        self.build()
        html = self.read("sources.html")
        _, agency_options = self.options(html, "agency")
        # display name (case-folded) then id; an unlisted id is shown literally
        self.assertEqual(agency_options, [("all", "All agencies"), (UNKNOWN, UNKNOWN),
                                          (POLICE, "Cedar Police Department (fictional)"), (SHERIFF, "Zeta &quot;Sheriff&quot; &amp; Co")])
        self.assertEqual(self.options(html, "year")[1], [("all", "All years"), ("2026", "2026"), ("2025", "2025"), ("2024", "2024")])
        self.assertNotIn('"Sheriff"', html.split('<form class="sources-filter"')[1].split("</form>")[0])
        self.assertIn('Showing 5 of 5', html)

    def test_all_seven_labels_render_and_count_label_transports(self):
        self.rich()
        self.labels(dict(NEW_LABELS, **{"js.filter_count": 'Mostrando "{shown}" \\ de {total}'}))
        self.build()
        html = self.read("sources.html")
        for text in NEW_LABELS.values():
            self.assertIn(text, html)
        for english in ("Filter source records", "All agencies", "All years", "Cited by a finding", "No source record matches"):
            self.assertNotIn(english, html)
        self.assertIn('Mostrando &quot;5&quot; \\ de 5', html)
        data = self.read("site-data.js")
        self.assertIn('"js.filter_count":"Mostrando \\"{shown}\\" \\\\ de {total}"', data)
        self.assertLess(html.index("/site-data.js"), html.index("/sources/filter.js"))
        self.assertEqual(HEAD_SCRIPTS.findall(html), ["/site-data.js", "/sources/filter.js"])

    def test_omitted_placeholder_label_and_full_labels_when_relabelled(self):
        self.rich()
        self.labels({"js.filter_count": "{total} en total"})
        self.build()
        self.assertIn(">5 en total</p></form>", self.read("sources.html"))
        labels = json.loads(re.search(r"^var LABELS=(.*);$", self.read("site-data.js"), flags=re.M).group(1))
        self.assertEqual(labels["js.filter_count"], "{total} en total")
        self.assertGreater(len(labels), 1)  # a relabelled site keeps the existing full js.* transport


class ScriptAndIsolationTests(SourcesFilterCase):
    def test_script_is_small_text_only_and_the_template_verbatim(self):
        self.rich()
        self.build()
        script = self.read("sources/filter.js")
        self.assertEqual(script, SOURCES_FILTER_ASSET.read_text(encoding="utf-8"))
        self.assertLess(len(script.encode("utf-8")), 4096)
        for forbidden in (r"innerHTML", r"outerHTML", r"insertAdjacent", r"eval\b", r"Function\(", r"fetch", r"XMLHttpRequest", r"\bimport\b",
                          r"require\(", r"cookie", r"localStorage", r"sessionStorage", r"https?:", r"\bon[a-z]+=", r"location\.hash\s*="):
            self.assertNotRegex(script, forbidden)
        self.assertIn("textContent", script)
        self.assertIn('LABELS["js.filter_count"]', script)
        self.assertNotIn("Showing", script)  # never an English count literal
        self.assertIn(".source-table > li.source-entry", script)  # direct library rows only; nested citation items are not records

    def test_tag_only_on_sources_page_and_nothing_else_changes(self):
        self.rich()
        before = self.build()
        with_filter = self.tree()
        self.assertIn("sources/filter.js", before)
        for name in before:
            if name.endswith(".html"):
                self.assertEqual("/sources/filter.js" in self.read(name), name == "sources.html", name)
        with mock.patch.object(Site, "sources_filter", lambda site: ""), mock.patch.object(Site, "sources_labels", lambda site: None):
            self.build()
        without = self.tree()
        self.assertEqual(set(with_filter) - set(without), {"sources/filter.js"})
        self.assertEqual({n for n in without if with_filter[n] != without[n]}, {"sources.html", "site-data.js"})
        # findings filter bytes, JSON/CSV open data and the record pages are untouched by the library filter
        for name in ("data/sources.json", "data/sources.csv", "data/findings.json", "data/findings.csv", "sources/cafecafecafe.html"):
            self.assertEqual(with_filter[name], without[name], name)

    def test_csp_headers_exact_and_build_check_passes_with_exact_script_set(self):
        self.rich()
        self.build()
        headers = self.read("_headers")
        self.assertIn("Content-Security-Policy: " + EXPECTED_CSP + "\n", headers)
        self.assertNotIn("unsafe-inline", headers)
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                capture_output=True, text=True, cwd=REPO_ROOT, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sources/filter.js", result.stdout)
        sources = set()
        for page in self.public.rglob("*.html"):
            for src in HEAD_SCRIPTS.findall(page.read_text(encoding="utf-8")):
                self.assertTrue((self.public / src.lstrip("/")).is_file(), f"{page.name}: {src} is not built")  # local link integrity
                sources.add(src)
        self.assertEqual(sources, {"/site-data.js", "/agency-cards.js", "/app.js", "/findings/cite.js", "/search.js", "/sources/filter.js"})

    def test_css_screen_rules_before_footer_tail_and_print_block_restores_the_library(self):
        screen, printed = EXTRA_CSS.split("@media print{")
        self.assertIn(".sources-filter[hidden]{display:none}", screen)
        self.assertLess(screen.index(".sources-filter{"), screen.index(".site-footer p{margin:0}"))
        self.assertTrue(screen.endswith(".site-footer p{margin:0}.site-footer a{color:inherit}\n"))
        self.assertIn(".sources-filter,.sources-filter-empty{display:none!important}", printed)
        self.assertIn(".source-table li.source-entry[hidden]{display:list-item}", printed)
        self.assertIn(".agency-document-group[hidden]{display:block}", printed)
        self.assertEqual(EXTRA_CSS.count("@media print"), 1)


if __name__ == "__main__":
    unittest.main()
