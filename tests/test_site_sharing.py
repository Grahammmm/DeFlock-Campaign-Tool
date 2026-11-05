"""DF0237-04: /sharing.html lists every receiving agency named in the campaign agencies' transcribed sharing lists (DF1207-08).

Synthetic organizations only, on temporary copies of the fictional example; sockets are blocked and nothing is fetched.
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

from campaign_tool import law
from campaign_tool.content import ContentError, RESERVED_PAGE_SLUGS, STARTER_SLUGS
from campaign_tool.labels import DEFAULTS
from campaign_tool.legal_claims import legal_claims, site_notes
from campaign_tool.sharing_summary import recipient_key, summarize_sharing
from campaign_tool.site import GENERATED_TOP_LEVEL, build_site
from campaign_tool.site_check import check_accessibility, check_links

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
POLICE, SHERIFF, COUNCIL = "ca-cedar-police", "ca-cedar-county-sheriff", "ca-cedar-city-council"
RULE_ID = "ca-civ-1798.90.55-sharing-limits"
BULLETIN_ID = "ca-oag-2023-dle-06-out-of-state-sharing-guidance"
FORBIDDEN = ("compl", "violat", "unlawful", "illegal", "prohibited")
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
SOURCE = "https://transparency.flocksafety.com/"


def rows(*triples):
    return [{"name": n, "state": s, "kind": k} for n, s, k in triples]


def agency(agency_id, name, checked_on, *triples, slug=None):
    return {"agency_id": agency_id, "name": name, "slug": slug or agency_id,
            "sharing": {"checked_on": checked_on, "source_url": SOURCE + agency_id, "recipients": rows(*triples)}}


class KeyTests(unittest.TestCase):
    def test_identity_only_case_whitespace_one_period(self):
        same = ["Reno Test PD", "reno test pd", "  Reno   Test\u00a0PD ", "RENO TEST PD.", "Reno Test PD . ", "reno\ttest\npd."]
        keys = {recipient_key(name, "NV", "local") for name in same}
        self.assertEqual(len(keys), 1, keys)
        for other in ["Reno Test PD..", "Reno Test P.D.", "Reno-Test PD", "Reno Test Police Department", "Reno Test PD, NV",
                      "Réno Test PD", "Reno Test PD \u0301", "Reno Test P D", "Reno, Test PD"]:
            with self.subTest(other=other):
                self.assertNotEqual(recipient_key(other, "NV", "local"), recipient_key("Reno Test PD", "NV", "local"))
        self.assertNotEqual(recipient_key("Reno Test PD", "NV", "local"), recipient_key("Reno Test PD", "OR", "local"))
        self.assertNotEqual(recipient_key("Reno Test PD", "NV", "local"), recipient_key("Reno Test PD", "NV", "private"))
        # a name that normalizes to nothing never shares one empty key with another such name
        self.assertNotEqual(recipient_key(".", "NV", "local"), recipient_key("..", "NV", "local"))
        self.assertEqual(recipient_key(".", "NV", "local"), recipient_key(".", "NV", "local"))

    def test_aliases_count_once_per_sender_and_every_sender_keeps_its_own_provenance(self):
        agencies = [
            agency("a-two", "Zeta Agency", "2026-10-02", ("reno test pd.", "NV", "local"), ("Reno Test PD", "NV", "local")),
            agency("a-one", "alpha Agency", "2026-10-01", ("RENO TEST PD", "NV", "local"), ("Reno Test PD.", "NV", "private")),
            {"agency_id": "a-none", "name": "No List", "slug": "a-none"}]
        found = summarize_sharing(agencies, "CA")
        by_kind = {r["kind"]: r for r in found}
        local = by_kind["local"]
        self.assertEqual(local["name"], "RENO TEST PD")  # smallest code-point spelling, not the normalized key
        self.assertEqual(local["aliases"], ["Reno Test PD", "reno test pd."])
        self.assertEqual([s["agency_id"] for s in local["listed_by"]], ["a-one", "a-two"])  # name.casefold(), so alpha before Zeta
        self.assertEqual([(s["checked_on"], s["source_url"]) for s in local["listed_by"]],
                         [("2026-10-01", SOURCE + "a-one"), ("2026-10-02", SOURCE + "a-two")])  # a-two lists it twice, counted once
        self.assertEqual(by_kind["private"]["name"], "Reno Test PD.")  # other kind: separate identity
        self.assertEqual(len(by_kind["private"]["listed_by"]), 1)
        self.assertEqual(summarize_sharing([], "CA"), [])


class SiteCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sharing-page-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.base = json.loads(self.path.read_text(encoding="utf-8"))

    def lists(self, **by_agency):
        """Write ``profile.sharing`` for each ``agency_id=(checked_on, [triples])``; None removes it."""
        document = copy.deepcopy(self.base)
        for item in document:
            key = item["agency_id"].replace("-", "_")
            if key in by_agency:
                profile = item.setdefault("profile", {})
                profile.pop("sharing", None)
                if by_agency[key] is not None:
                    checked_on, triples = by_agency[key]
                    profile["sharing"] = {"checked_on": checked_on, "source_url": SOURCE + item["agency_id"], "recipients": rows(*triples)}
        self.path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def build(self):
        with NO_SOCKETS:
            build_site(self.root, today=TODAY)

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def read(self, name="sharing.html"):
        return (self.public / name).read_text(encoding="utf-8")

    def package_variant(self, mutate):
        base = self.tmp / "jurisdictions"
        (base / "us-ca").mkdir(parents=True, exist_ok=True)
        package = json.loads((REPO_ROOT / "jurisdictions" / "us-ca" / "package.json").read_text())
        mutate(package)
        (base / "us-ca" / "package.json").write_text(json.dumps(package))
        return mock.patch.object(law, "JURISDICTIONS", base)

    @staticmethod
    def drop_rules(*rule_ids):
        def mutate(package):
            package["rules"] = [r for r in package["rules"] if r["rule_id"] not in rule_ids]
            for scope in package["request_scopes"]:  # load_package refuses a scope naming a rule the package lacks
                scope["rule_ids"] = [i for i in scope["rule_ids"] if i not in rule_ids]
        return mutate

    @staticmethod
    def text(html):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class SharingPageTests(SiteCase):
    def test_grouping_order_no_rank(self):
        many = [(f"Receiver {i:03d}", "NV", "local") for i in range(150)]
        self.lists(ca_cedar_police=("2026-10-30", [("Zed Fed Office", "US", "federal"), ("Fed Field Office", "CA", "federal"),
                                                    ("Oak Valley PD", "CA", "local"), ("Reno Test PD", "NV", "local"),
                                                    ("Acme Plate Co", "CA", "private"), ("Mystery Group", "OR", "unknown"),
                                                    *many]),
                   ca_cedar_county_sheriff=("2026-10-29", [("Reno Test PD", "NV", "local"), ("Oak Valley PD", "CA", "local"),
                                                           ("Receiver 149", "NV", "local")]))
        self.build()
        page = self.read()
        captions = re.findall(r"<caption>(.*?)</caption>", page)
        self.assertEqual(captions, ["Out of state", "Federal", "In state"])
        groups = {c: t for c, t in zip(captions, re.findall(r"<table>.*?</table>", page, re.S))}
        receivers = lambda table: re.findall(r'<th scope="row">(.*?)(?:<br>.*?)?</th>', table)  # noqa: E731
        self.assertEqual(receivers(groups["Federal"]), ["Fed Field Office", "Zed Fed Office"])
        self.assertEqual(receivers(groups["In state"]), ["Acme Plate Co", "Oak Valley PD"])
        out = receivers(groups["Out of state"])
        self.assertEqual(out, ["Mystery Group"] + [f"Receiver {i:03d}" for i in range(150)] + ["Reno Test PD"])  # alphabetical, none cut
        counts = re.findall(r'<td>(\d+)</td><td><ul>', groups["Out of state"])
        self.assertEqual(len(counts), 152)
        self.assertEqual(counts[0], "1")
        self.assertEqual(counts[151], "2")  # Reno Test PD is listed by two agencies but still sorts by name
        self.assertEqual(counts[150], "2")  # Receiver 149 (two senders) did not move ahead of Receiver 000
        self.assertEqual(counts[1], "1")
        self.assertNotIn("more", page)
        for heading in ("Receiving agency", "State", "Kind", "Campaign agencies listing it", "Source lists"):
            self.assertIn(f'<th scope="col">{heading}</th>', page)
        self.assertIn("<td>Local</td>", page)
        self.assertIn("<td>Federal</td>", page)
        self.assertIn("<td>Private</td>", page)
        self.assertIn("<td>Unknown</td>", page)
        self.assertEqual(check_links(self.public), [])
        self.assertEqual([f for f in check_accessibility(self.public) if f[0] == "sharing.html"], [])
        for word in ("rank", "top ", "most ", "plate", "highest"):  # no ranking and no volume; "Acme Plate Co" is a name
            self.assertNotIn(word, self.text(page).replace("Acme Plate Co", "").lower())

    def test_agency_links_and_escaping(self):
        self.lists(ca_cedar_police=("2026-10-30", [('Smith & "Sons" <b>PD</b>', "NV", "private"), ("Shared PD", "NV", "local")]),
                   ca_cedar_county_sheriff=("2026-10-29", [("shared pd.", "NV", "local")]))
        document = json.loads(self.path.read_text())
        document[1]["records_email"], document[1]["notes"] = "sentinel-private@example.invalid", "SENTINEL-PRIVATE-NOTE"
        self.path.write_text(json.dumps(document), encoding="utf-8")
        self.build()
        page = self.read()
        exports = self.snapshot()
        for sentinel in ("sentinel-private", "SENTINEL-PRIVATE", "cedar-police/records"):
            self.assertNotIn(sentinel, page)
        self.assertIn('<a href="/agencies/cedar.html">Cedar Police Department (fictional)</a>', page)  # detail page preferred
        self.assertIn('<a href="/agencies.html#cedar-county-sheriff">Cedar County Sheriff&#x27;s Office (fictional)</a>', page)  # article fallback
        self.assertNotIn("agencies/cedar-county-sheriff.html", page)
        self.assertIn('id="cedar-county-sheriff"', self.read("agencies.html"))
        self.assertNotIn("agencies/cedar-county-sheriff.html", exports)  # no sharing-only detail page
        self.assertEqual(sorted(n for n in exports if n.startswith("agencies/")), ["agencies/cedar.html"])
        self.assertIn("Smith &amp; &quot;Sons&quot; &lt;b&gt;PD&lt;/b&gt;", page)
        self.assertNotIn("<b>PD</b>", page)
        self.assertNotIn("&amp;amp;", page)
        self.assertIn(f'<a href="{SOURCE}ca-cedar-police" rel="noopener noreferrer">source list</a>', page)
        self.assertIn(f'<a href="{SOURCE}ca-cedar-county-sheriff" rel="noopener noreferrer">source list</a>', page)
        self.assertIn('checked <time datetime="2026-10-30">2026-10-30</time>', page)
        self.assertIn('checked <time datetime="2026-10-29">2026-10-29</time>', page)
        self.assertIn('<th scope="row">Shared PD<br>Also transcribed as: shared pd.</th>', page)
        self.assertEqual(check_links(self.public), [])
        self.assertEqual([f for f in check_accessibility(self.public) if f[0] == "sharing.html"], [])
        for name, data in exports.items():  # no recipient surface in open data, feeds or card JS
            if name.startswith("data/") or name.endswith((".xml", ".js", ".ics")):
                self.assertNotIn(b"Shared PD", data, name)
                self.assertNotIn(b"Smith", data, name)

    def test_citations_without_findings(self):
        self.lists(ca_cedar_police=("2026-10-30", [("Reno Test PD", "NV", "local")]))
        self.build()
        page = self.read()
        package = json.loads((REPO_ROOT / "jurisdictions" / "us-ca" / "package.json").read_text())
        rules = {r["rule_id"]: r for r in package["rules"]}
        rule, bulletin = rules[RULE_ID], rules[BULLETIN_ID]
        first, second = page.index(rule["citation"].replace("§", "§")), page.index("2023-DLE-06")
        self.assertLess(first, second)  # the sharing rule, then the 2023 AG bulletin
        for item in (rule, bulletin):
            self.assertIn(f'<a href="{item["sources"][0]["url"].replace("&", "&amp;")}" rel="noopener noreferrer">', page)
        self.assertIn("<p>Law package: draft, not legally reviewed.</p>", page)
        for prose in (rule["duty"], bulletin["duty"], rule["remedy"], "exception", "shall not"):
            self.assertNotIn(prose, page)
        # reviewed package: the same label the agency pages show
        with self.package_variant(lambda p: p.update(status="reviewed", reviewed_by=["synthetic reviewer"], reviewed_at="2026-09-01")):
            self.build()
        self.assertIn("<p>Law package: reviewed on 2026-09-01.</p>", self.read())
        # one rule missing: only its citation goes
        with self.package_variant(self.drop_rules(BULLETIN_ID)):
            self.build()
        page = self.read()
        self.assertIn(rule["citation"].replace("'", "&#x27;"), page)
        self.assertNotIn("2023-DLE-06", page)
        self.assertIn("<p>Law package: draft, not legally reviewed.</p>", page)
        with self.package_variant(self.drop_rules(RULE_ID)):
            self.build()
        page = self.read()
        self.assertIn("2023-DLE-06", page)
        self.assertNotIn("Civ. Code § 1798.90.55(b)</a>", page.replace("interpreting Civ. Code § 1798.90.55(b)", ""))
        # unreadable package: no citation, an honest status, the build goes on
        base = self.tmp / "jurisdictions"
        (base / "us-ca" / "package.json").write_text("{not json")
        with mock.patch.object(law, "JURISDICTIONS", base):
            self.build()
        page = self.read()
        self.assertIn("<p>Law package: status unavailable.</p>", page)
        self.assertNotIn("1798.90.55", page)
        self.assertIn("Reno Test PD", page)

    def test_guard_wording_and_label_overrides(self):
        self.lists(ca_cedar_police=("2026-10-30", [("Reno Test PD", "NV", "local"), ("Fictional Federal Office", "US", "federal")]),
                   ca_cedar_county_sheriff=("2026-10-29", [("Reno Test PD", "NV", "local")]))
        self.build()
        page = self.read()
        plain = self.text(page)
        self.assertEqual(legal_claims(plain), [])
        self.assertEqual(site_notes(self.root), [])
        for word in FORBIDDEN:  # OUR copy never uses them (names and official titles are not our copy)
            self.assertNotIn(word, plain.lower(), word)
        for key, text in DEFAULTS.items():
            if key.startswith(("sharing.", "footer.sharing")):
                for word in FORBIDDEN:
                    self.assertNotIn(word, text.lower(), key)
        self.assertIn("A listing does not establish that a transfer occurred.", page)
        # an injected assertion in a label override is scanned, flagged, and does not fail the build
        labels = {"schema_version": 1, "labels": {"sharing.lead": "Cedar Police violated the law by sharing data."}}
        (self.root / "content" / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
        notes = site_notes(self.root)
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("review-required: sharing.html: legal-claim wording ("), notes)
        self.assertNotIn("Cedar Police violated", notes[0])  # names the place and rules, never the text
        self.build()  # alert-only
        self.assertIn("Cedar Police violated the law by sharing data.", self.read())
        # a scan that cannot run is a flag, not an exception
        with mock.patch("campaign_tool.site.Site.sharing_plaintext", side_effect=RuntimeError("boom")):
            self.assertEqual(site_notes(self.root), ["review-required: sharing.html: legal-claim wording (guard-error)"])

    def test_every_label_is_replaced_when_lists_exist(self):
        self.lists(ca_cedar_police=("2026-10-30", [("Reno Test PD", "NV", "local"), ("Fictional Federal Office", "US", "federal"),
                                                    ("Oak Valley PD", "CA", "private"), ("Mystery", "CA", "unknown"),
                                                    ("Albany State Office", "NY", "state"), ("reno test pd.", "NV", "local")]),
                   ca_cedar_county_sheriff=("2026-10-29", [("Reno Test PD", "NV", "local")]))
        labels = {key: f"X{key}X" for key in DEFAULTS}
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
        self.build()
        page = self.read()
        keys = [key for key in DEFAULTS if key.startswith("sharing.")] + ["footer.sharing"]
        self.assertEqual(len(keys), 20)
        for key in keys:
            self.assertIn(f"X{key}X", page, key)
        phrases = [chunk.strip() for key in keys for chunk in re.split(r"\{[^{}]*\}", DEFAULTS[key]) if len(chunk.split()) >= 2]
        for phrase in phrases:
            self.assertNotIn(phrase, page.replace("Law package", ""), phrase)

    def test_footer_link_sitemap_and_reserved_slug(self):
        self.build()
        before = self.snapshot()
        self.assertNotIn("sharing.html", before)
        self.assertFalse(any(b"Data-sharing lists" in data or b"sharing.html" in data for data in before.values()))
        self.lists(ca_cedar_police=("2026-10-30", [("Reno Test PD", "NV", "local")]))
        self.build()
        after = self.snapshot()
        self.assertEqual(after["sitemap.xml"].decode().count("<loc>https://cedar-records.example.invalid/sharing.html</loc>"), 1)
        entry = re.search(r"<url><loc>[^<]*sharing\.html</loc>.*?</url>", after["sitemap.xml"].decode())
        self.assertIsNotNone(entry)
        self.assertNotIn("lastmod", entry.group(0))
        footer = '<p><a href="/sharing.html">Data-sharing lists</a></p></footer>'
        changed = {n for n in set(before) | set(after) if before.get(n) != after.get(n)}
        self.assertEqual(changed, {"sharing.html", "sitemap.xml", "agencies.html"} | {n for n in changed if n.endswith(".html")})  # only pages and the sitemap
        self.assertIn("sharing.html", changed)
        section = re.compile(r'<section class="agency-sharing">.*?</section>', re.S)  # DF1207-08: the agency pages' own list section
        for name in changed - {"sitemap.xml"}:  # every changed page differs by the one footer paragraph (and the agency pages' list section) only
            self.assertEqual(after[name].decode().count(footer), 1, name)
            if name != "sharing.html":
                self.assertEqual(section.sub("", after[name].decode().replace(footer, "</footer>")), before[name].decode(), name)
        for name in before:  # open data, feeds, search, CSS and card JS stay byte-identical
            if not name.endswith(".html") and name != "sitemap.xml":
                self.assertEqual(before[name], after[name], name)
        self.assertEqual(after["sitemap.xml"].decode().replace('<url><loc>https://cedar-records.example.invalid/sharing.html</loc></url>', ""),
                         before["sitemap.xml"].decode())
        # no base_url: no sitemap at all, the page and the footer link remain
        site_json = self.root / "content" / "site.json"
        site = json.loads(site_json.read_text())
        site["base_url"] = None
        site_json.write_text(json.dumps(site))
        self.build()
        self.assertFalse((self.public / "sitemap.xml").exists())
        self.assertTrue((self.public / "sharing.html").exists())
        again = self.snapshot()
        self.build()
        self.assertTrue(self.snapshot() == again, "two feature builds differ")

    def test_reserved_slug_even_without_lists(self):
        self.assertIn("sharing", STARTER_SLUGS)
        self.assertIn("sharing", RESERVED_PAGE_SLUGS)
        self.assertIn("sharing.html", GENERATED_TOP_LEVEL)
        self.build()
        pages = self.root / "content" / "pages"
        pages.mkdir()
        (pages / "sharing.json").write_text(json.dumps({"title": "Mine", "body_md": "Text."}), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, r"pages/sharing\.json: slug sharing is reserved"):
            build_site(self.root, today=TODAY)
        (pages / "sharing.json").unlink()
        assets = self.root / "content" / "public-assets"
        assets.mkdir()
        (assets / "sharing.html").mkdir()
        (assets / "sharing.html" / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        with self.assertRaisesRegex(ContentError, "collides with a generated path"):
            build_site(self.root, today=TODAY)
        shutil.rmtree(assets / "sharing.html")
        (self.root / "content" / "redirects.json").write_text(json.dumps({"schema_version": 1, "redirects": [
            {"from": "/sharing.html", "to": "/about.html"}]}), encoding="utf-8")
        with self.assertRaises(ContentError):
            build_site(self.root, today=TODAY)

    def test_absence_last_removal_and_atomic_refusal(self):
        self.build()
        absent = self.snapshot()
        self.lists(ca_cedar_police=("2026-10-30", [("Reno Test PD", "NV", "local")]))
        self.build()
        first = self.snapshot()
        self.assertIn("sharing.html", first)
        self.build()
        self.assertTrue(self.snapshot() == first, "two feature builds differ")
        self.lists(ca_cedar_police=None)  # the last list goes: the page, link and sitemap entry go with it
        self.build()
        self.assertTrue(self.snapshot() == absent, "removing the last list left a trace")
        # an invalid hand-edited list refuses before any public write
        self.lists(ca_cedar_police=("2026-10-30", [("Reno Test PD", "NV", "local")]))
        self.build()
        before = self.snapshot()
        document = json.loads(self.path.read_text())
        document[1]["profile"]["sharing"]["recipients"][0]["kind"] = "bogus"
        self.path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ContentError):
            build_site(self.root, today=TODAY)
        self.assertTrue(self.snapshot() == before, "public/ changed after a refused build")
        self.assertEqual(list(self.root.glob(".public.*")), [])


if __name__ == "__main__":
    unittest.main()
