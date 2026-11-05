"""DF0237-03: organizer-recorded archived copies (``archived_url`` + ``archived_on``) of agency source links.

Five link objects may carry the pair: profile.alpr_policy, profile.contract, each profile.transparency snapshot, each
profile.funding entry and each profile.reports entry. The card and page show ``archived copy (DATE)`` after the source
link and data/agencies.json carries both fields. The tool fetches nothing and submits nothing to an archive.
Offline, synthetic agencies and URLs only.
"""
import copy
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

from campaign_tool import law
from campaign_tool.content import ContentError, _archive_fields, default_agencies, load_agencies
from campaign_tool.site import PUBLIC_ARCHIVE_FIELDS, archive_copy, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "agencies-open-data.schema.json").read_text(encoding="utf-8"))
ES = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))["labels"]
BUILD_DAY = date(2026, 11, 1)
CEDAR = 1
WAY = "https://web.archive.org/web/20261020120000/https://example.invalid/cedar/doc?a=1&b=2"
PAIR = {"archived_url": WAY, "archived_on": "2026-10-20"}
SENTINEL = "Zq-private-sentinel-77"
BASE = "content/agencies.json[1].profile"


def pair(url, on="2026-10-20"):
    return {"archived_url": url, "archived_on": on}


def way(stamp, target="https://example.invalid/x"):
    return f"https://web.archive.org/web/{stamp}/{target}"


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


def refusal(value, today=BUILD_DAY):
    try:
        _archive_fields(value, "L", today)
    except ContentError as caught:
        return str(caught)
    return ""


def profile(**parts):
    """A Cedar PD entry (verified, with a transparency slug) whose profile holds ``parts``."""
    return [{"agency_id": "ca-cedar-police", "slug": "cedar", "name": "Cedar PD", "kind": "police", "verified": True,
             "verification": {"source_url": "https://example.invalid/contact", "checked_on": "2026-01-15"},
             "flock_transparency_slug": "cedar-pd-example", "profile": parts}]


POLICY = {"url": "https://example.invalid/cedar/policy.pdf", "source_url": "https://example.invalid/cedar/found",
          "checked_on": "2026-10-21"}
CONTRACT = {"vendor": "Synthetic vendor", "start": "2025-01-01", "end": "2027-01-01", "renewal_note": "",
            "source_url": "https://example.invalid/cedar/contract", "checked_on": "2026-10-21"}
SNAPSHOT = {"checked_on": "2026-10-21", "source_url": "https://transparency.flocksafety.com/cedar-pd-example", "cameras": 4}
FUNDING = {"kind": "grant", "name": "Synthetic grant", "as_stated_in": "Grant letter",
           "source_url": "https://example.invalid/cedar/grant", "checked_on": "2026-10-21"}
REPORT = {"title": "Synthetic annual report", "kind": "annual_report", "period_start": "2025-01-01",
          "period_end": "2025-12-31", "source_url": "https://example.invalid/cedar/report", "checked_on": "2026-10-21"}


class ArchiveValidationTests(unittest.TestCase):
    def test_pair_shapes_for_every_location(self):
        locations = {"alpr_policy": (POLICY, lambda o: o), "contract": (CONTRACT, lambda o: o),
                     "transparency": (SNAPSHOT, lambda o: [o]), "funding": (FUNDING, lambda o: [o]),
                     "reports": (REPORT, lambda o: [o])}
        for name, (base, wrap) in locations.items():
            def load(**extra):
                return load_agencies(profile(**{name: wrap({**base, **extra})}), "content/agencies.json", BUILD_DAY)[0]
            suffix = "" if name in ("alpr_policy", "contract") else "[0]"
            plain = load()
            self.assertNotIn("archived_url", json.dumps(plain))
            with self.subTest(location=name):
                got = load(**PAIR)
                holder = got["transparency"][0] if name == "transparency" else (
                    got[name][0] if name in ("funding", "reports") else got[name])
                self.assertEqual((holder["archived_url"], holder["archived_on"]), (WAY, "2026-10-20"))
                for bad, field in (({"archived_url": WAY}, "archived_on"), ({"archived_on": "2026-10-20"}, "archived_url")):
                    with self.assertRaises(ContentError) as caught:
                        load(**bad)
                    self.assertIn(f".profile.{name}{suffix}.{field}", str(caught.exception))
                for junk in (None, "", True, 5, ["x"], {"a": 1}):
                    with self.assertRaises(ContentError) as caught:
                        load(archived_url=junk, archived_on="2026-10-20")
                    self.assertIn(f".profile.{name}{suffix}.archived_url", str(caught.exception))
                    with self.assertRaises(ContentError) as caught:
                        load(archived_url=WAY, archived_on=junk)
                    self.assertIn(f".profile.{name}{suffix}.archived_on", str(caught.exception))
        # required sets are unchanged: the pair never stands in for a missing source or date
        with self.assertRaises(ContentError):
            load_agencies(profile(contract={k: v for k, v in CONTRACT.items() if k != "source_url"} | PAIR),
                          "content/agencies.json", BUILD_DAY)
        self.assertIsNone(load_agencies(profile(contract=None), "content/agencies.json", BUILD_DAY)[0]["contract"])

    def test_host_parser_is_exact(self):
        for url in (WAY, "https://archive.ph/AbCd1", "https://archive.today/2026.10.20-120000/https://example.invalid/x",
                    "https://ARCHIVE.PH/AbCd1"):
            self.assertEqual("", refusal(pair(url)), url)
        for url in ("https://web.archive.org.evil.invalid/web/20261020120000/https://example.invalid/x",
                    "https://evilweb.archive.org/web/20261020120000/https://example.invalid/x",
                    "https://sub.archive.ph/AbCd1", "https://archive.is/AbCd1", "https://archive.ph./AbCd1",
                    "https://notarchive.ph/AbCd1", "https://archive.ph.evil.invalid/AbCd1",
                    "https://user@archive.ph/AbCd1", "https://archive.ph:443/AbCd1", "https://archive.ph:99999/AbCd1",
                    "https://archive.ph:/AbCd1", "http://archive.ph/AbCd1", "//archive.ph/AbCd1",
                    "https://archive.ph/ab cd", "https://archive.ph/ab\ncd", "https://archive.ph/ab%2Fcd@x",
                    "https://example.invalid/web/20261020120000/https://example.invalid/x",
                    "https://archive.ph", "https://archive.ph/", "https://archive.ph/?x=1"):
            message = refusal(pair(url))
            with self.subTest(url=url):
                self.assertNotEqual(message, "")
                self.assertTrue(message.startswith("L.archived_url: "), message)
                self.assertNotIn("evil", message)
                self.assertNotIn("AbCd1", message)

    def test_wayback_timestamp_and_target(self):
        for stamp, day in (("20240229000000", "2024-02-29"), ("20261020235959", "2026-10-20"), ("20261020000000", "2026-10-20")):
            self.assertEqual("", refusal(pair(way(stamp), day)), stamp)
        self.assertEqual("", refusal(pair(way("20261020120000", "http://example.invalid:8080/x"))))
        for stamp in ("2026102012000", "202610201200000", "２０２６１０２０１２００００", "20261020120000id_", "2026*",
                      "20261320120000", "20230229120000", "20261032120000", "20261020250000", "20261020126000",
                      "20261020120060", "00000000000000", "latest", "%32026102012000"):
            message = refusal(pair(way(stamp)))
            with self.subTest(stamp=stamp):
                self.assertTrue(message.startswith("L.archived_url: "), message)
        for url in ("https://web.archive.org/web/20261020120000/", "https://web.archive.org/web/20261020120000",
                    "https://web.archive.org/web/20261020120000/example.invalid/x",
                    "https://web.archive.org/web/20261020120000/ftp://example.invalid/x",
                    "https://web.archive.org/web/20261020120000/https:///x",
                    "https://web.archive.org/web/20261020120000/https://user@example.invalid/x",
                    "https://web.archive.org/save/https://example.invalid/x", "https://web.archive.org/"):
            self.assertTrue(str(refusal(pair(url))).startswith("L.archived_url: "), url)
        # the date must be the timestamp's own date, and the error names archived_on, not archived_url
        self.assertEqual(refusal(pair(way("20261020120000"), "2026-10-19")),
                         "L.archived_on: must equal the date of the archived_url timestamp")
        self.assertEqual("", refusal(pair("https://archive.ph/AbCd1", "2020-01-01")))  # other hosts: date supplied by hand

    def test_archive_calendar_uses_build_day(self):
        self.assertEqual("", refusal(pair("https://archive.ph/AbCd1", "2026-11-01")))
        self.assertEqual(refusal(pair("https://archive.ph/AbCd1", "2026-11-02")), "L.archived_on: must not be after the build date")
        self.assertNotEqual("", refusal(pair("https://archive.ph/AbCd1", "2026-11-02"), today=date(2026, 1, 1)))
        self.assertEqual(refusal(pair(way("20261102000000"), "2026-11-02")), "L.archived_on: must not be after the build date")
        for on in ("2026-02-29", "2026-2-1", "20261020", "2026-10-20T00:00", " 2026-10-20", "２０２６-10-20", "2026-10-20\n"):
            self.assertEqual(refusal(pair("https://archive.ph/AbCd1", on)), "L.archived_on: must be a real YYYY-MM-DD calendar date", on)
        self.assertEqual("", refusal(pair("https://archive.ph/AbCd1", "2024-02-29")))
        # no relation to checked_on is invented: older and newer than it are both fine
        for on in ("2026-01-01", "2026-10-30"):
            got = load_agencies(profile(contract={**CONTRACT, **pair("https://archive.ph/AbCd1", on)}), "content/agencies.json", BUILD_DAY)
            self.assertEqual(got[0]["contract"]["archived_on"], on)

    def test_no_fetch_and_input_not_mutated(self):
        source = profile(funding=[{**FUNDING, **PAIR}])
        before = copy.deepcopy(source)
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network), \
                mock.patch("socket.create_connection", side_effect=refuse_network):
            load_agencies(source, "content/agencies.json", BUILD_DAY)
        self.assertEqual(source, before)

    def test_kit_fallback_uses_the_injected_day(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-kit-") as tmp:
            (Path(tmp) / "kit").mkdir()
            entry = profile(contract={**CONTRACT, **pair("https://archive.ph/AbCd1", "2026-11-01")})[0]
            entry.pop("verified"), entry.pop("verification"), entry.pop("flock_transparency_slug")
            (Path(tmp) / "kit" / "agencies.json").write_text(json.dumps([entry]), encoding="utf-8")
            self.assertEqual(default_agencies(tmp, date(2026, 11, 1))[0]["contract"]["archived_on"], "2026-11-01")
            with self.assertRaises(ContentError) as caught:
                default_agencies(tmp, date(2026, 10, 31))
            self.assertIn("kit/agencies.json[0].profile.contract.archived_on", str(caught.exception))


class ArchiveSiteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-archives-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.agencies = json.loads(self.path.read_text(encoding="utf-8"))

    def record(self, **parts):
        cedar = self.agencies[CEDAR]
        cedar.update(verified=True, verification={"source_url": "https://example.invalid/contact", "checked_on": "2026-01-15"},
                     flock_transparency_slug="cedar-pd-example")
        cedar["profile"].update(parts)
        self.path.write_text(json.dumps(self.agencies), encoding="utf-8")

    def build(self, today=BUILD_DAY):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=today)
        self.assertEqual(urlopen.call_count, 0)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def card_and_page(self):
        card = dict(re.findall(r'<article id="([^"]+)">(.*?)</article>', self.read("agencies.html"), re.S))["cedar"]
        return card, self.read("agencies/cedar.html")

    def exported(self):
        doc = json.loads(self.read("data/agencies.json"))
        law.validate(doc, SCHEMA)
        return [a for a in doc["agencies"] if a["agency_id"] == "ca-cedar-police"][0]

    def full(self):
        return dict(
            alpr_policy={**POLICY, **pair("https://archive.ph/PoLiCy", "2026-10-22")},
            contract={**CONTRACT, **pair(way("20261022000000", "https://example.invalid/c"), "2026-10-22")},
            transparency=[{**SNAPSHOT, "checked_on": "2026-10-21", **pair("https://archive.today/Newest", "2026-10-22")},
                          {**SNAPSHOT, "checked_on": "2026-09-21", **pair("https://archive.today/Older", "2026-09-22")}],
            funding=[{**FUNDING, **pair("https://archive.ph/Fund1", "2026-10-23")}, {**FUNDING, "name": "Plain"}],
            reports=[{**REPORT, **pair("https://archive.ph/Rep1", "2026-10-24")},
                     {**REPORT, "title": "Older plain report", "period_start": "2024-01-01", "period_end": "2024-12-31"}])

    def test_each_link_on_card_and_page(self):
        self.record(**self.full())
        self.build()
        card, page = self.card_and_page()

        def suffix(url, day):
            return f' <a href="{url}" rel="noopener noreferrer">archived copy ({day})</a>'
        for html in (card, page):
            self.assertIn(suffix(way("20261022000000", "https://example.invalid/c"), "2026-10-22") + ").", html)
            self.assertIn(suffix("https://archive.ph/Fund1", "2026-10-23"), html)
            self.assertIn(suffix("https://archive.ph/Rep1", "2026-10-24"), html)
            # newest snapshot only; the older pair is validated but not shown
            self.assertIn(suffix("https://archive.today/Newest", "2026-10-22") + "</p></section>", html)
            self.assertNotIn("archive.today/Older", html)
            self.assertNotIn("authentic", html)
            self.assertIn("Posted ALPR usage policy</a>" + suffix("https://archive.ph/PoLiCy", "2026-10-22"), html)
            # policy, contract, funding, report and newest snapshot: five links; the plain entries carry none
            self.assertEqual(html.count("archived copy ("), 5)
        self.assertEqual(self.read("agencies.html").count("archived copy ("), 5)  # no other agency, glance, feed or popup

    def test_page_policy_and_contract_only_with_a_pair(self):
        self.record(alpr_policy=POLICY, contract=CONTRACT)
        self.build()
        card, page = self.card_and_page()
        self.assertIn("Posted ALPR usage policy", card)
        self.assertNotIn("Posted ALPR usage policy", page)  # unchanged: no pair, no policy line on the page
        self.assertNotIn("contract-term", page)
        self.assertNotIn("archived copy", card + page)
        self.record(alpr_policy={**POLICY, **PAIR})
        self.build()
        self.assertIn("Posted ALPR usage policy", self.card_and_page()[1])

    def test_labels_english_override_and_spanish(self):
        self.record(contract={**CONTRACT, **PAIR, "cost": {"amount_usd": "10", "period": "annual", "as_stated_in": "Doc"}})
        self.build()
        card, page = self.card_and_page()
        self.assertEqual(card.count("archived copy (2026-10-20)"), 2)  # contract line and its cost line
        self.assertIn('href="https://web.archive.org/web/20261020120000/https://example.invalid/cedar/doc?a=1&amp;b=2"', card)
        self.assertNotIn("&amp;amp;", card)
        labels = self.root / "content" / "labels.json"
        labels.write_text(json.dumps({"schema_version": 1, "labels": {"agency.archived_copy": "saved & kept {date}"}}), encoding="utf-8")
        self.build()
        card, _ = self.card_and_page()
        self.assertIn("saved &amp; kept 2026-10-20</a>", card)
        labels.write_text(json.dumps({"schema_version": 1, "labels": {"agency.archived_copy": ES["agency.archived_copy"]}}), encoding="utf-8")
        self.build()
        self.assertIn("copia archivada (2026-10-20)</a>", self.card_and_page()[0])
        self.assertEqual(ES["agency.archived_copy"], "copia archivada ({date})")
        self.assertEqual(archive_copy({}), "")
        self.assertEqual(archive_copy(None), "")

    def test_public_json_csv_and_schema(self):
        self.record(**self.full())
        self.build()
        item = self.exported()
        self.assertEqual(item["alpr_policy"], {"url": POLICY["url"], "checked_on": "2026-10-21", **pair("https://archive.ph/PoLiCy", "2026-10-22")})
        self.assertEqual(item["contract"]["archived_on"], "2026-10-22")
        self.assertEqual(item["transparency"]["archived_url"], "https://archive.today/Newest")
        self.assertEqual([f.get("archived_url") for f in item["funding"]], ["https://archive.ph/Fund1", None])
        self.assertEqual([r.get("archived_on") for r in item["reports"]], ["2026-10-24", None])
        self.assertNotIn("source_url", item["alpr_policy"])  # the private discovery page stays private
        csv_text = self.read("data/agencies.csv")
        self.assertEqual(csv_text.count("https://archive.ph/PoLiCy"), 1)
        self.assertIn("https://archive.today/Newest", csv_text)
        self.assertNotIn("https://archive.today/Older", self.read("data/agencies.json") + csv_text)
        header = csv_text.splitlines()[0]
        self.assertNotIn("archived", header)
        # the schema refuses a half pair, a foreign host and a stray key
        doc = json.loads(self.read("data/agencies.json"))
        for change in (lambda a: a["alpr_policy"].pop("archived_on"), lambda a: a["contract"].update(archived_url="https://example.invalid/x"),
                       lambda a: a["funding"][0].update(archived_url="http://archive.ph/x"),
                       lambda a: a["reports"][0].update(archived_url="https://archive.ph/"),
                       lambda a: a["transparency"].update(archived_url=way("2026102012000"))):
            broken = copy.deepcopy(doc)
            change([x for x in broken["agencies"] if x["agency_id"] == "ca-cedar-police"][0])
            with self.assertRaises(Exception):
                law.validate(broken, SCHEMA)

    def test_absent_pairs_leave_no_trace(self):
        self.record(alpr_policy=POLICY, contract=CONTRACT, transparency=[SNAPSHOT], funding=[FUNDING], reports=[REPORT])
        self.build()
        first = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        shutil.rmtree(self.public)
        self.build()
        second = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertEqual(first, second)
        for name, data in first.items():
            self.assertNotIn(b"archived", data, name)
        self.assertEqual(PUBLIC_ARCHIVE_FIELDS, ("archived_url", "archived_on"))

    def test_invalid_archive_is_atomic_and_offline(self):
        funding = [{**FUNDING}, {**FUNDING, "name": "Second", **pair("https://archive.ph.evil.invalid/" + SENTINEL)}]
        self.record(funding=funding)
        for args, code in (("build", 1), ("content-check", 3)):
            done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", args, "--directory", str(self.root)],
                                  cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, code, done.stderr)
            self.assertIn(f"{BASE}.funding[1].archived_url: host must be exactly web.archive.org, archive.ph or archive.today",
                          done.stdout + done.stderr)
            self.assertNotIn(SENTINEL, done.stdout + done.stderr)
        self.assertFalse(self.public.exists())
        self.record(funding=[{**FUNDING, **pair(way("20261020120000"), "2026-10-19")}])
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertEqual(str(caught.exception), f"{BASE}.funding[0].archived_on: must equal the date of the archived_url timestamp")
        self.assertFalse(self.public.exists())


if __name__ == "__main__":
    unittest.main()
