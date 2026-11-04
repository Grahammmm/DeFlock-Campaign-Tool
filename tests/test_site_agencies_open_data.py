"""DF1422-12: data/agencies.json open data for agencies that have a public page.

Synthetic content only (temp copies of the fictional example); no network.
"""
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import law
from campaign_tool.content import ContentError
from campaign_tool.site import (PUBLIC_AGENCY_FIELDS, PUBLIC_AGENCY_TOP_LEVEL, PUBLIC_ASKED_FIELDS,
                                PUBLIC_CONTRACT_FIELDS, PUBLIC_POLICY_FIELDS, PUBLIC_TRANSPARENCY_FIELDS, Site, build_site)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "agencies-open-data.schema.json").read_text(encoding="utf-8"))
BUILD_DAY = date(2026, 11, 1)
CEDAR_ID, SHERIFF_ID, COUNCIL_ID = "ca-cedar-police", "ca-cedar-county-sheriff", "ca-cedar-city-council"
PORTAL = "https://transparency.flocksafety.com/cedar-pd-example"
EMAIL = "synthetic-records@example.invalid"
KIT_EMAIL = "synthetic-kit-recipient@example.invalid"
POLICY = {"url": "https://example.invalid/cedar/alpr-policy.pdf", "source_url": "https://example.invalid/cedar/policies?a=1&b=2",
          "checked_on": "2026-10-20"}
CONTRACT = {"vendor": "Synthetic vendor", "start": "2025-01-01", "end": "2027-01-01", "renewal_note": "Renews quietly in January.",
            "source_url": "https://example.invalid/cedar/contract?x=1&y=2", "checked_on": "2026-10-21"}
NEW = {"checked_on": "2026-10-25", "source_url": PORTAL, "cameras": 42, "retention_days": 30,
       "searches_last_30_days": 910, "hotlist_alerts": None, "shared_with_agencies": 13}
OLD = {"checked_on": "2026-09-25", "source_url": PORTAL + "/", "cameras": 40, "retention_days": 30,
       "searches_last_30_days": 800, "hotlist_alerts": 5}
ASKED = [{"topic": "Retention policy", "asked_on": "2026-08-01", "state": "answered", "updated_on": "2026-09-01",
          "note": "Internal organizer remark."},
         {"topic": "Sharing list", "asked_on": "2026-10-01", "state": "awaiting_response"}]


class AgenciesOpenDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-agencies-json-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "agencies.json"
        self.public = self.root / "public"
        self.agencies = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self):
        self.path.write_text(json.dumps(self.agencies), encoding="utf-8")

    def rich(self):
        """Cedar PD with asked items, policy, contract, two snapshots, an unverified-looking contact and portal."""
        cedar = self.agencies[1]
        cedar.update(verified=True, verification={"source_url": "https://example.invalid/contact", "checked_on": "2026-01-15"},
                     flock_transparency_slug="cedar-pd-example", records_email=EMAIL, asked=ASKED, notes="organizer-only note",
                     portal={"vendor": "none", "url": "https://example.invalid/portal-link"})
        cedar["profile"].update(alpr_policy=POLICY, contract=CONTRACT, transparency=[NEW, OLD])
        self.save()

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def data(self, today=BUILD_DAY):
        build_site(self.root, today=today)
        return json.loads(self.read("data/agencies.json"))

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def by_id(self, doc, agency_id):
        return [a for a in doc["agencies"] if a["agency_id"] == agency_id][0]

    # 1. asked items, policy, contract and two snapshots: newest snapshot only, schema-valid, nothing else
    def test_card_facts_newest_snapshot_only_and_schema_valid(self):
        self.rich()
        doc = self.data()
        law.validate(doc, SCHEMA)
        self.assertEqual(set(doc), PUBLIC_AGENCY_TOP_LEVEL)
        self.assertEqual((doc["campaign"], doc["generated_from"], doc["url_kind"], doc["base_url"]),
                         (doc["campaign"], "content/agencies.json", "absolute", "https://cedar-records.example.invalid"))
        cedar = self.by_id(doc, CEDAR_ID)
        self.assertEqual(set(cedar), set(PUBLIC_AGENCY_FIELDS))
        self.assertEqual((cedar["name"], cedar["kind"]), ("Cedar Police Department (fictional)", "police"))
        self.assertEqual(cedar["asked"], [  # newest asked_on first, as the card lists them; note never leaves
            {"topic": "Sharing list", "state": "awaiting_response", "asked_on": "2026-10-01", "updated_on": None},
            {"topic": "Retention policy", "state": "answered", "asked_on": "2026-08-01", "updated_on": "2026-09-01"}])
        self.assertEqual(cedar["alpr_policy"], {"url": POLICY["url"], "checked_on": "2026-10-20"})
        self.assertEqual(cedar["contract"], {k: CONTRACT[k] for k in PUBLIC_CONTRACT_FIELDS})
        self.assertEqual(cedar["transparency"], {"checked_on": "2026-10-25", "source_url": PORTAL, "cameras": 42,
                                                 "retention_days": 30, "searches_last_30_days": 910, "shared_with_agencies": 13})
        self.assertNotIn("hotlist_alerts", cedar["transparency"])  # a null number is not shown on the page, so not exported
        text = self.read("data/agencies.json")
        self.assertNotIn("2026-09-25", text)  # the older snapshot
        self.assertNotIn('"cameras": 40', text)
        self.assertEqual(text, json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        page = self.read("agencies.html")
        for shown in ("Retention policy", "Sharing list", POLICY["url"].replace("&", "&amp;"), "Synthetic vendor"):
            self.assertIn(shown, page)  # the card already shows what the file carries

    # 2. unverified contacts, the portal link and kit files never appear
    def test_unverified_contacts_portal_and_kit_never_appear(self):
        self.rich()
        self.agencies[1]["records_url"] = "https://example.invalid/records-request-form"
        self.save()
        kit = self.root / "kit" / "cedar-police"
        kit.mkdir(parents=True)
        (kit / "sent.json").write_text(json.dumps([{"agency_id": CEDAR_ID, "to": KIT_EMAIL, "sent_at": "2026-10-02T10:00:00Z",
                                                    "subject": "Synthetic kit subject"}]), encoding="utf-8")
        doc = self.data()
        text = self.read("data/agencies.json")
        for leaked in (EMAIL, KIT_EMAIL, "sent.json", "Synthetic kit subject", "records_email", "records_url", "portal",
                       "example.invalid/portal-link", "records-request-form", "organizer-only note", "notes", "verification",
                       "mailto:"):
            self.assertNotIn(leaked, text)
        law.validate(doc, SCHEMA)

    # 3. renewal_note and the asked-item note are not in the allowlists: pinned
    def test_renewal_note_and_asked_note_are_absent_and_pinned(self):
        self.rich()
        self.data()
        text = self.read("data/agencies.json")
        self.assertNotIn("renewal_note", text)
        self.assertNotIn("Renews quietly", text)
        self.assertNotIn("Internal organizer remark", text)
        self.assertEqual(set(PUBLIC_CONTRACT_FIELDS), {"vendor", "start", "end", "source_url", "checked_on"})
        self.assertEqual(set(PUBLIC_ASKED_FIELDS), {"topic", "state", "asked_on", "updated_on"})
        self.assertEqual(set(PUBLIC_POLICY_FIELDS), {"url", "checked_on"})
        self.assertEqual(set(PUBLIC_AGENCY_FIELDS) & {"records_email", "records_url", "portal", "notes", "verification"}, set())
        self.assertEqual(set(PUBLIC_TRANSPARENCY_FIELDS), {"cameras", "retention_days", "searches_last_30_days", "hotlist_alerts",
                                                           "shared_with_agencies", "source_url", "checked_on"})
        self.assertIn("Renews quietly in January.", self.read("agencies.html"))  # the page shows it; the file does not

    # 4. an agency without a public page is not listed; no public pages gives an empty list
    def test_agency_without_page_not_listed_and_none_gives_empty_list(self):
        self.rich()
        self.agencies[0]["profile"]["contract"] = CONTRACT  # the sheriff has a card but no finding or source, so no page
        self.save()
        doc = self.data()
        self.assertEqual([a["agency_id"] for a in doc["agencies"]], [CEDAR_ID])
        self.assertNotIn(SHERIFF_ID, self.read("data/agencies.json"))
        self.assertNotIn(COUNCIL_ID, self.read("data/agencies.json"))
        (self.root / "content" / "sources.json").write_text("[]", encoding="utf-8")
        shutil.rmtree(self.root / "content" / "findings")
        empty = self.data()
        self.assertEqual(empty["agencies"], [])
        law.validate(empty, SCHEMA)
        self.assertIn('<a href="/data/agencies.json" type="application/json">', self.read("agencies.html"))
        self.assertIn('<a href="/data/agencies.json" type="application/json">data/agencies.json</a>', self.read("about.html"))

    # 5. a key injected into the builder is refused naming the file, nothing is written
    def test_injected_key_is_refused_naming_agencies_json(self):
        self.rich()
        build_site(self.root, today=BUILD_DAY)
        before = self.snapshot()
        real = Site._public_agency
        leaks = {"records_email": lambda v: v.update(records_email=EMAIL),
                 "portal": lambda v: v.update(portal={"vendor": "none"}),
                 "notes": lambda v: v.update(notes="x"),
                 "renewal_note": lambda v: v["contract"].update(renewal_note="x"),
                 "note": lambda v: v["asked"][0].update(note="x"),
                 "source_url": lambda v: v["alpr_policy"].update(source_url="https://example.invalid/x"),
                 "Name": lambda v: v.update(Name="case variant")}
        for key, leak in leaks.items():
            with self.subTest(key=key):
                def builder(self_, agency, cities, leak=leak):
                    value = real(self_, agency, cities)
                    if value["agency_id"] == CEDAR_ID:
                        leak(value)
                    return value
                with mock.patch.object(Site, "_public_agency", builder):
                    with self.assertRaisesRegex(ContentError, f"^data/agencies\\.json: non-public key {key}$"):
                        build_site(self.root, today=BUILD_DAY)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".public")), [])

    # 6. every url is a generated agency page; source_url values are copied verbatim; sorted by name then id
    def test_urls_resolve_links_present_and_values_verbatim(self):
        self.rich()
        self.agencies.append({"agency_id": "ca-aaa-agency", "name": "Aaa Synthetic Agency", "kind": "police", "jurisdiction_name": "Aaa",
                              "records_url": None, "records_email": None, "portal": {"vendor": "unknown", "url": None}, "verified": False})
        self.save()
        sources = json.loads((self.root / "content" / "sources.json").read_text(encoding="utf-8"))
        sources.append({**sources[0], "sha256": "ab" * 32, "agency_id": "ca-aaa-agency", "title": "Synthetic Aaa record"})
        (self.root / "content" / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
        doc = self.data()
        self.assertEqual([a["name"] for a in doc["agencies"]], ["Aaa Synthetic Agency", "Cedar Police Department (fictional)"])
        for agency in doc["agencies"]:
            self.assertTrue(agency["url"].startswith(doc["base_url"] + "/agencies/"), agency["url"])
            self.assertTrue((self.public / agency["url"][len(doc["base_url"]):].lstrip("/")).is_file(), agency["url"])
        cedar = self.by_id(doc, CEDAR_ID)
        self.assertEqual(cedar["contract"]["source_url"], CONTRACT["source_url"])  # the & is not escaped in the data
        self.assertEqual(cedar["transparency"]["source_url"], PORTAL)
        self.assertEqual(cedar["alpr_policy"]["url"], POLICY["url"])
        link = '<a href="/data/agencies.json" type="application/json">'
        self.assertIn(link + "Agencies as open data (JSON)</a>", self.read("agencies.html"))
        self.assertIn(link + "data/agencies.json</a>", self.read("about.html"))
        self.assertEqual(self.by_id(doc, "ca-aaa-agency")["asked"], [])
        self.assertEqual([self.by_id(doc, "ca-aaa-agency")[k] for k in ("alpr_policy", "contract", "transparency")], [None] * 3)

    # 7. two builds are byte-identical and no build-time field exists beyond the injected clock
    def test_two_builds_are_byte_identical(self):
        self.rich()
        build_site(self.root, today=BUILD_DAY)
        first = self.read("data/agencies.json")
        build_site(self.root, today=BUILD_DAY)
        self.assertEqual(self.read("data/agencies.json"), first)
        build_site(self.root, today=date(2026, 11, 2))
        self.assertEqual(self.read("data/agencies.json"), first)  # nothing in the file depends on the date
        written = {p.relative_to(self.public).as_posix() for p in build_site(self.root, today=BUILD_DAY)}
        self.assertIn("data/agencies.json", written)

    # 8. camera_count is the card's sentence: computed from the map unless an organizer typed one; relative urls without a base
    def test_camera_count_sentence_and_relative_urls(self):
        self.rich()
        doc = self.data()
        sentence = self.by_id(doc, CEDAR_ID)["camera_count"]
        self.assertRegex(sentence, r"^\d+ community-mapped points? inside city limits; ownership unconfirmed$")
        self.assertIn(sentence, self.read("site-data.js"))  # the card reads this exact string
        self.agencies[1]["profile"]["cameraCount"] = "3 synthetic points typed by an organizer"
        self.save()
        self.assertEqual(self.by_id(self.data(), CEDAR_ID)["camera_count"], "3 synthetic points typed by an organizer")
        site = json.loads((self.root / "content" / "site.json").read_text(encoding="utf-8"))
        site["base_url"] = None
        (self.root / "content" / "site.json").write_text(json.dumps(site), encoding="utf-8")
        relative = self.data()
        self.assertEqual((relative["base_url"], relative["url_kind"]), (None, "relative"))
        self.assertEqual(self.by_id(relative, CEDAR_ID)["url"], "/agencies/cedar.html")
        law.validate(relative, SCHEMA)


if __name__ == "__main__":
    unittest.main()
