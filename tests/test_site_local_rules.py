"""Agency page "Local rules on record" (DF1952-02): the validated organizer-transcribed local rules scoped to an agency,
with citation, duty as written, effective dates, source link, checked date and the not-legally-reviewed label, classified
against the build date. Synthetic data only; nothing is fetched; the state package is a synthetic copy under a temporary
jurisdictions base (``law.JURISDICTIONS`` patched). Run from the repository root:
python3 -B -m unittest tests.test_site_local_rules -v
"""
import copy
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import law, site
from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.legal_claims import legal_claims
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
POLICE, SHERIFF, COUNCIL = "ca-cedar-police", "ca-cedar-county-sheriff", "ca-cedar-city-council"
LOCAL, OTHER = "local-us-ca-cedar-retention", "local-us-ca-cedar-signage"
LABEL = "local rule, organizer-transcribed, not legally reviewed"
SOURCE = "https://library.example.org/cedar/municode/5-1"
HEADING = "<h3>Local rules on record</h3>"
NOTICE = "These are organizer transcriptions of local ordinances, not a determination about the agency's conduct."


def synthetic_package(status="draft"):
    package = copy.deepcopy(law.load_package("us-ca"))
    template = package["rules"][0]
    package["rules"] = [dict(copy.deepcopy(template), rule_id="ca-alpha", citation="Synthetic Code sec. 1",
                             effective_from="2020-01-01", effective_to=None)]
    for scope in package["request_scopes"]:
        scope["rule_ids"] = ["ca-alpha"]
    package.update(status=status, reviewed_by=["Synthetic Reviewer"] if status == "reviewed" else [],
                   reviewed_at="2026-02-03" if status == "reviewed" else None)
    return package


def rule(**changes):
    base = {"rule_id": LOCAL, "citation": "Cedar Municipal Code 5.1", "duty": "Delete plate reads after 30 days.",
            "source_url": SOURCE, "checked_on": "2026-01-02", "effective_from": "2020-01-01",
            "effective_to": None, "agency_ids": [POLICE]}
    base.update(changes)
    return base


def tree(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(Path(folder).rglob("*")) if p.is_file()}


def same_files(case, left, right, message):
    """Compare two public/ byte maps without building a failure diff of whole pages."""
    case.assertEqual(sorted(left), sorted(right), message + ": file set")
    for name in left:
        case.assertTrue(left[name] == right[name], f"{message}: {name} differs")


class LocalRulesCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-local-rules-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.base = self.tmp / "jurisdictions"
        (self.base / "us-ca").mkdir(parents=True)
        self.write_package(synthetic_package())
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        patcher = mock.patch.object(law, "JURISDICTIONS", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_package(self, package):
        (self.base / "us-ca" / "package.json").write_text(json.dumps(package), encoding="utf-8")

    def overlay(self, rules):
        (self.root / "law").mkdir(exist_ok=True)
        (self.root / "law" / "local-rules.json").write_text(
            json.dumps({"schema_version": 1, "jurisdiction": "us-ca", "rules": rules}), encoding="utf-8")

    def give_sheriff_a_page(self):
        path = self.root / "content" / "sources.json"
        sources = json.loads(path.read_text(encoding="utf-8"))
        sources.append({"sha256": "ab" * 32, "title": "Synthetic sheriff contract", "agency_id": SHERIFF,
                        "received_at": "2026-02-01T10:00:00+00:00", "public_url": None, "fragment": "sheriff-records"})
        path.write_text(json.dumps(sources), encoding="utf-8")

    def build(self, today=date(2021, 4, 1)):
        shutil.rmtree(self.root / "public", ignore_errors=True)
        self.assertTrue(build_site(self.root, today=today))
        return tree(self.root / "public")

    def agency_page(self, files, name="cedar"):
        return files[f"agencies/{name}.html"].decode("utf-8")

    @staticmethod
    def entries(html):
        """[(citation, state)] in page order."""
        return re.findall(r'<h4>(.*?)</h4><p><span class="status-pill">(.*?)</span>', html)

    @staticmethod
    def section(html):
        start = html.index('<section class="local-rules">')
        return html[start:html.index("</section>", start) + len("</section>")]


class ScopeAndFieldsTests(LocalRulesCase):
    def test_agency_scope_and_all_fields(self):
        self.give_sheriff_a_page()
        shared = rule(citation='Cedar Code 5.1 "A" & B', duty="Keep the log & show it.", agency_ids=[POLICE, SHERIFF])
        elsewhere = rule(rule_id=OTHER, citation="Cedar Sign Code 9", agency_ids=[COUNCIL])
        self.overlay([elsewhere, shared])
        files = self.build()
        for name in ("cedar", "cedar-county-sheriff"):
            html = self.agency_page(files, name)
            part = self.section(html)
            self.assertEqual(html.count(HEADING), 1, name)
            self.assertEqual(part.count("<h4>"), 1, name)
            self.assertIn("<h4>Cedar Code 5.1 &quot;A&quot; &amp; B</h4>", part)
            self.assertIn(f"<code>{LOCAL}</code>", part)
            self.assertIn("<p>Duty as written: Keep the log &amp; show it.</p>", part)
            self.assertIn('Effective from <time datetime="2020-01-01">2020-01-01</time> to open', part)
            self.assertIn(f'<a href="{SOURCE}" rel="noopener noreferrer">Official source (organizer-recorded)</a>', part)
            self.assertIn('checked <time datetime="2026-01-02">2026-01-02</time>', part)
            self.assertEqual(part.count(LABEL), 1)
            self.assertIn(NOTICE, part)
            self.assertNotIn("Cedar Sign Code 9", html)
            # placed after the existing agency blocks and before Published findings
            self.assertLess(html.index(HEADING), html.index("<h3>Published findings</h3>"))
        # the council has no page, and a rule scoped to it appears on no other page
        self.assertNotIn("agencies/cedar-city-council.html", files)
        # exact agency_id membership: nothing is matched by prefix, name or place
        self.overlay([rule(agency_ids=[SHERIFF])])
        self.assertNotIn(HEADING, self.agency_page(self.build()))


class BuildDateTests(LocalRulesCase):
    def versions(self):
        return [rule(citation="V1", effective_from="2020-01-01", effective_to="2021-06-30"),
                rule(citation="V2", effective_from="2021-07-01", effective_to="2022-12-31"),
                rule(citation="V3", effective_from="2023-01-01", effective_to=None)]

    def test_build_date_inclusive_past_future_versions(self):
        past, future, current = "Past — not in force on build date", "Not yet in force on build date", "In force on build date"
        cases = {
            date(2019, 12, 31): (future, future, future),
            date(2020, 1, 1): (current, future, future),      # start day is inclusive
            date(2021, 6, 30): (current, future, future),     # end day is inclusive
            date(2021, 7, 1): (past, current, future),
            date(2022, 12, 31): (past, current, future),
            date(2023, 1, 1): (past, past, current),
            date(2999, 1, 1): (past, past, current),          # an open end stays in force
        }
        for today, states in cases.items():
            with self.subTest(today=today.isoformat()):
                self.overlay(list(reversed(self.versions())))  # input order does not matter
                html = self.agency_page(self.build(today))
                self.assertEqual(self.entries(self.section(html)), list(zip(("V1", "V2", "V3"), states)))
        part = self.section(html)
        self.assertIn("to open", part)
        self.assertIn('to <time datetime="2021-06-30">2021-06-30</time>', part)
        # the three versions of one rule id are three entries, each with its own heading and label
        self.assertEqual((part.count("<h4>"), part.count(LABEL)), (3, 3))

    def test_order_is_stable_when_the_model_is_reordered(self):
        self.overlay(self.versions() + [rule(rule_id="local-us-ca-cedar-aaa", citation="A first", agency_ids=[POLICE])])
        model = load_content(self.root)
        agency = next(a for a in model["agencies"] if a["agency_id"] == POLICE)
        config = read_config(self.root)
        forward = Site(self.root, config, model, today=date(2021, 4, 1)).local_rules_section(agency)
        model["local_rules"] = list(reversed(model["local_rules"]))
        backward = Site(self.root, config, model, today=date(2021, 4, 1)).local_rules_section(agency)
        self.assertEqual(forward, backward)
        self.assertEqual([c for c, _ in self.entries(forward)], ["A first", "V1", "V2", "V3"])


class LegacyBytesTests(LocalRulesCase):
    def test_absent_empty_and_unrelated_bytes(self):
        absent = self.build()
        self.assertNotIn("local-rules", self.agency_page(absent))
        self.overlay([])
        same_files(self, absent, self.build(), "empty overlay")
        self.overlay([rule(agency_ids=[COUNCIL])])  # valid, but names no agency that has a page
        unrelated = self.build()
        with mock.patch.object(Site, "local_rules_section", lambda self, agency: ""):
            disabled = self.build()
        same_files(self, unrelated, disabled, "unrelated-only overlay vs helper disabled")
        same_files(self, unrelated, absent, "unrelated-only overlay vs no overlay")
        # hand-built legacy model without the key: "" and the package is not read
        model = load_content(self.root)
        model.pop("local_rules")
        built = Site(self.root, read_config(self.root), model, today=date(2021, 4, 1))
        agency = next(a for a in model["agencies"] if a["agency_id"] == POLICE)
        with mock.patch.object(law, "load_package", side_effect=AssertionError("package read")):
            self.assertEqual(built.local_rules_section(agency), "")
            model["local_rules"] = []
            self.assertEqual(Site(self.root, read_config(self.root), model, today=date(2021, 4, 1))
                             .local_rules_section(agency), "")


class EligibilityAndOpenDataTests(LocalRulesCase):
    def test_no_new_page_eligibility_or_open_data_fields(self):
        before_agencies = (self.root / "content" / "agencies.json").read_bytes()
        absent = self.build()
        # a local rule alone creates no page, no agencies.html entry and no sitemap line
        self.overlay([rule(agency_ids=[SHERIFF, COUNCIL])])
        local_only = self.build()
        self.assertNotIn("agencies/cedar-county-sheriff.html", local_only)
        self.assertNotIn("agencies/cedar-city-council.html", local_only)
        same_files(self, absent, local_only, "local rule on agencies without a page")
        # a rule on an agency that has a page changes only that page: the JSON, sitemap and index stay byte-identical
        self.overlay([rule()])
        shown = self.build()
        self.assertIn(HEADING, self.agency_page(shown))
        changed = [name for name in absent if absent[name] != shown[name]]
        self.assertEqual((sorted(shown) == sorted(absent), changed), (True, ["agencies/cedar.html"]))
        for name in ("data/agencies.json", "data/findings.json", "data/sources.json", "sitemap.xml", "agencies.html"):
            if name in absent:
                self.assertTrue(absent[name] == shown[name], name)
        self.assertEqual((self.root / "content" / "agencies.json").read_bytes(), before_agencies)
        for name in shown:
            if name.startswith("data/"):
                self.assertNotIn(b"local-us-ca", shown[name], name)
                self.assertNotIn(LABEL.encode(), shown[name], name)


class EscapingTests(LocalRulesCase):
    def test_escaped_transcription_and_source_link(self):
        self.overlay([rule(citation='Code "5" & six', duty="Say \"no\" & stop; it's 'fine'.",
                           source_url="https://library.example.org/a?b=1&c=2")])
        part = self.section(self.agency_page(self.build()))
        self.assertIn("<h4>Code &quot;5&quot; &amp; six</h4>", part)
        self.assertIn("Duty as written: Say &quot;no&quot; &amp; stop; it&#x27;s &#x27;fine&#x27;.", part)
        self.assertIn('<a href="https://library.example.org/a?b=1&amp;c=2" rel="noopener noreferrer">', part)
        self.assertNotIn("&amp;amp;", part)
        self.assertNotIn("&amp;quot;", part)
        # a hand-built hostile model is escaped by the renderer, which never trusts the loader alone
        model = load_content(self.root)
        hostile = dict(model["local_rules"][0], rule_id="<b>x</b>", citation="<script>alert(1)</script>",
                       duty='<img src=x onerror="alert(2)"> **bold** [link](javascript:alert(3))',
                       source_url='https://x.example/"><script>alert(4)</script>')
        model["local_rules"] = [hostile]
        agency = next(a for a in model["agencies"] if a["agency_id"] == POLICE)
        html = Site(self.root, read_config(self.root), model, today=date(2021, 4, 1)).local_rules_section(agency)
        for markup in ("<script", "<img", "<b>", '"><script'):
            self.assertNotIn(markup, html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("**bold** [link](javascript:alert(3))", html)  # plain text, never Markdown
        self.assertEqual(html.count('rel="noopener noreferrer"'), 1)

    def test_unsafe_source_urls_never_reach_a_build(self):
        for bad in ("http://library.example.org/x", "javascript:alert(1)", "https://user:pw@library.example.org/x",
                    "file:///etc/passwd", ""):
            with self.subTest(url=bad):
                self.overlay([rule(source_url=bad)])
                shutil.rmtree(self.root / "public", ignore_errors=True)
                with self.assertRaises(ContentError):
                    build_site(self.root, today=date(2021, 4, 1))
                self.assertFalse((self.root / "public").exists())


class NeutralWordingTests(LocalRulesCase):
    @staticmethod
    def text(html):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

    def test_neutral_wording_with_legal_claim_guard(self):
        self.overlay([rule(effective_to="2021-06-30"),
                      rule(citation="Cedar Municipal Code 5.1 (2022 text)", effective_from="2021-07-01"),
                      rule(rule_id=OTHER, citation="Cedar Sign Code 9", effective_from="2030-01-01",
                           duty="Post a sign at each camera.")])
        part = self.section(self.agency_page(self.build()))
        self.assertEqual(part.count("<h4>"), 3)
        self.assertEqual(legal_claims(self.text(part)), [])
        for word in ("violat", "complian", "illegal", "unlawful", "breach"):
            self.assertNotIn(word, part.lower())
        # positive control: the guard really flags a violation assertion
        self.assertNotEqual(legal_claims("Cedar Police violated the law."), [])
        self.assertNotEqual(legal_claims(self.text(part) + " Cedar Police violated the law."), [])


class ReviewedStateTests(LocalRulesCase):
    def test_reviewed_state_does_not_review_local(self):
        self.write_package(synthetic_package(status="reviewed"))
        self.overlay([rule(effective_to="2021-06-30"), rule(citation="Second", effective_from="2021-07-01")])
        files = self.build()
        part = self.section(self.agency_page(files))
        self.assertEqual((part.count("<h4>"), part.count(LABEL)), (2, 2))
        for absent in ("reviewed on", "Law package", "reviewed by", "Synthetic Reviewer", "verified", "approved"):
            self.assertNotIn(absent, part)
        self.assertNotIn("2026-02-03", part)  # the state package's review date is never attached to a local rule
        self.assertNotIn("violation", part.lower())
        # the local rule adds no finding, badge or review promotion anywhere else on the page
        html = self.agency_page(files)
        self.assertEqual(html.count(LABEL), 2)
        self.assertEqual(html.count(HEADING), 1)
        self.assertNotIn(LOCAL, files["findings/cedar-policy-posted.html"].decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
