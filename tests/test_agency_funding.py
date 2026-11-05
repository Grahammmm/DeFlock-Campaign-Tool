"""DF2207-02: how an agency says it is paid for, as an organizer transcribed it from a document (``profile.funding``).

The card, the agency page and data/agencies.json show each entry with its own document, source and checked date; nothing
is summed, ranked or said about a donor beyond the name in the document. Offline, synthetic names and documents only.
"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from html import escape
from pathlib import Path
from unittest import mock

from campaign_tool import law
from campaign_tool.content import ContentError, load_agencies
from campaign_tool.content_check import content_problems
from campaign_tool.site import PUBLIC_AGENCY_FIELDS, PUBLIC_FUNDING_FIELDS, Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "agencies-open-data.schema.json").read_text(encoding="utf-8"))
BUILD_DAY = date(2026, 11, 1)
CEDAR, SHERIFF = 1, 0  # indexes in the example's agencies.json: Cedar PD has an agency page, the sheriff has none
LABEL = f"content/agencies.json[{CEDAR}].profile.funding"
ENTRY = {"kind": "grant", "name": "Synthetic Safety Program", "as_stated_in": "Synthetic grant award letter",
         "source_url": "https://example.invalid/cedar/award", "checked_on": "2026-10-21"}
LIST_ERROR = ": must be a list of 1 to 10 funding entries"
SHAPE_ERROR = (": needs kind, name, as_stated_in, source_url and checked_on, and may add only "
               "amount_usd with period, and fiscal_year")
KIND_ERROR = ".kind: choose one of ['grant', 'general_fund', 'asset_forfeiture', 'donation', 'other']"
NAME_ERROR = ".name: plain program or donor organization name of 1 to 120 characters"
TITLE_ERROR = ".as_stated_in: plain document title of 1 to 120 characters"
AMOUNT_ERROR = ".amount_usd: decimal string from 0 to 100000000 with at most 2 decimals"
PERIOD_ERROR = ".period: choose annual, total or one_time"
PAIR_ERROR = ": amount_usd and period go together"
YEAR_ERROR = ".fiscal_year: four digits as the document states it, e.g. 2026"
DAY_ERROR = ".checked_on: must be a YYYY-MM-DD date not after the build date"
SOURCE_ERRORS = (".source_url: must be an https:// URL", ".source_url: invalid URL host")
ABSENT = object()


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


def provenance(document, url, day):
    return (f'How it is paid for, as stated in {document} (<a href="{escape(url)}" rel="noopener noreferrer">source</a>, '
            f'checked <time datetime="{day}">{day}</time>)')


class FundingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-funding-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.agencies = json.loads(self.path.read_text(encoding="utf-8"))

    def fund(self, funding=ABSENT, index=CEDAR):
        if funding is not ABSENT:
            self.agencies[index].setdefault("profile", {})["funding"] = funding
        self.path.write_text(json.dumps(self.agencies), encoding="utf-8")

    def build(self, today=BUILD_DAY):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=today)
        self.assertEqual(urlopen.call_count, 0)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def snapshot(self, base=None):
        base = base or self.public
        return {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()}

    def card(self):
        return self.read("agencies.html").split('<article id="cedar">', 1)[1].split("</article>", 1)[0]

    def exported(self):
        doc = json.loads(self.read("data/agencies.json"))
        law.validate(doc, SCHEMA)
        return [a for a in doc["agencies"] if a["agency_id"] == "ca-cedar-police"][0]

    def refused(self, funding, tail, today=BUILD_DAY, alternatives=None, wall_clock=True):
        """Funding is refused with exactly ``LABEL + tail`` by build and content-check; public/ is kept as it was."""
        before = self.snapshot()
        self.fund(funding)
        with self.assertRaises(ContentError) as caught:
            self.build(today)
        message = str(caught.exception)
        self.assertIn(message, [LABEL + t for t in (alternatives or (tail,))])
        self.assertEqual(self.snapshot(), before)
        if wall_clock:  # content-check reads the wall clock; the frozen-clock test covers the build date
            self.assertTrue(any(line.startswith(LABEL) for line in content_problems(self.root)))
        return message

    def accepted(self, funding):
        self.fund(funding)
        self.build()
        return self.exported()["funding"]

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def test_entry_kinds_order_and_own_provenance(self):
        entries = [
            {"kind": "donation", "name": "Cañón & O'Brien Fund", "as_stated_in": "Annual report", "amount_usd": "2500",
             "period": "one_time", "fiscal_year": "2025", "source_url": "https://example.invalid/a?x=1&y=2",
             "checked_on": "2026-10-20"},
            {"kind": "grant", "name": "Zeta Program", "as_stated_in": "Annual report",
             "source_url": "https://example.invalid/b", "checked_on": "2026-10-20"},  # same title and date, other source
            {"kind": "other", "name": "Alpha Program", "as_stated_in": "Budget book p. 4",
             "source_url": "https://example.invalid/a?x=1&y=2", "checked_on": "2026-09-01", "fiscal_year": "2031"},
            {"kind": "general_fund", "name": "City general fund", "as_stated_in": "Budget book p. 4", "amount_usd": "1000.5",
             "period": "annual", "source_url": "https://example.invalid/c", "checked_on": "2026-11-01"},
            {"kind": "asset_forfeiture", "name": "Beta Program", "as_stated_in": "Council staff report",
             "source_url": "https://example.invalid/d", "checked_on": "2026-10-21"}]
        self.fund(entries)
        self.build()
        shown = {"grant": "Grant", "general_fund": "General fund", "asset_forfeiture": "Asset forfeiture",
                 "donation": "Donation", "other": "Other"}
        for html in (self.card(), self.read("agencies/cedar.html")):
            self.assertEqual(html.count("How it is paid for, as stated in"), 5)
            self.assertEqual(html.count('<ul class="agency-funding">'), 1)
            positions = []
            for entry in entries:
                line = provenance(escape(entry["as_stated_in"]), entry["source_url"], entry["checked_on"])
                self.assertEqual(html.count(line), 1, entry["name"])
                self.assertIn(f'<span class="funding-kind">{shown[entry["kind"]]}</span>', html)
                self.assertEqual(html.count(f'<strong class="funding-name">{escape(entry["name"])}</strong>'), 1)
                positions.append(html.index(line))
            self.assertEqual(positions, sorted(positions))  # the order written, never sorted by name, date or kind
        card = self.card()
        self.assertIn("$2,500.00 one time · Fiscal year 2025.", card)
        self.assertIn("$1,000.50 per year.", card)
        self.assertIn("Fiscal year 2031.", card)
        exported = self.exported()["funding"]
        self.assertEqual(exported, entries)  # the page and the file carry the same entries in the same order
        self.assertEqual([set(e) <= set(PUBLIC_FUNDING_FIELDS) for e in exported], [True] * 5)
        self.assertEqual(exported[3]["amount_usd"], "1000.5")  # the accepted string, never re-formatted or float-converted

    def test_page_places_funding_after_contract_and_before_transparency(self):
        cedar = self.agencies[CEDAR]
        cedar.update(verified=True, verification={"source_url": "https://example.invalid/contact", "checked_on": "2026-01-15"},
                     flock_transparency_slug="cedar-pd-example")
        cedar["profile"].update(contract={"vendor": "Synthetic vendor", "start": "2025-01-01", "end": "2027-01-01",
                                          "renewal_note": "", "source_url": "https://example.invalid/c", "checked_on": "2026-10-21",
                                          "cost": {"amount_usd": "5", "period": "total", "as_stated_in": "Agreement"}},
                                transparency=[{"checked_on": "2026-10-25", "source_url": "https://transparency.flocksafety.com/cedar-pd-example",
                                               "cameras": 3}])
        self.fund([ENTRY])
        self.build()
        for html in (self.card(), self.read("agencies/cedar.html")):
            order = [html.index(marker) for marker in ('class="contract-', 'class="agency-funding"', 'class="agency-transparency"')]
            self.assertEqual(order, sorted(order))

    def test_funding_list_and_shape_refusals(self):
        self.fund()
        self.build()
        without = load_agencies(self.agencies, "content/agencies.json", BUILD_DAY)
        self.assertTrue(all("funding" not in agency for agency in without))  # absent key adds no normalized key
        self.assertNotIn("funding", self.exported())
        for value in (None, [], "grant", 5, True, {}, dict(ENTRY), [ENTRY] * 11):
            with self.subTest(value=value):
                self.refused(value, LIST_ERROR)
        for entry in ("x", None, 5, ["a"]):
            with self.subTest(entry=entry):
                self.refused([entry], "[0]: expected an object")
        for missing in ENTRY:
            with self.subTest(missing=missing):
                self.refused([{k: v for k, v in ENTRY.items() if k != missing}], "[0]" + SHAPE_ERROR)
        for extra in ({"note": "x"}, {"contact": "Jane Roe"}, {"Name": "x"}, {"total_usd": "1"}, {"SECRETDONORKEY": 1}):
            with self.subTest(extra=extra):
                message = self.refused([{**ENTRY}, {**ENTRY, **extra}], "[1]" + SHAPE_ERROR)
                self.assertNotIn("SECRETDONORKEY", message)
                self.assertNotIn("Jane", message)
        self.assertEqual(len(self.accepted([ENTRY])), 1)
        self.assertEqual(len(self.accepted([{**ENTRY, "name": f"Program {n}"} for n in range(10)])), 10)
        message = self.refused([{**ENTRY, "name": "SECRETDONOR <x>"}], "[0]" + NAME_ERROR)
        self.assertNotIn("SECRETDONOR", message)  # a submitted value is never echoed
        message = self.refused([{**ENTRY, "kind": "SECRETKIND"}], "[0]" + KIND_ERROR)
        self.assertNotIn("SECRETKIND", message)
        for kind in ("Grant", "grants", "", None, 1, ["grant"], "forfeiture"):
            with self.subTest(kind=kind):
                self.refused([{**ENTRY, "kind": kind}], "[0]" + KIND_ERROR)
        self.refused([ENTRY, {**ENTRY, "kind": "nope"}], "[1]" + KIND_ERROR)  # the failing entry's index is named

    def test_name_document_and_escape_boundaries(self):
        for name in ("A", "N" * 120, "Cañón & O'Brien Fund", "Fondo de Seguridad (2025) - 50/50"):
            with self.subTest(name=name):
                self.assertEqual(self.accepted([{**ENTRY, "name": name}])[0]["name"], name)
        for name in ("", "N" * 121, " Name", "Name ", "  ", "<b>Name</b>", "A > B", "A < B", "Name\nTwo", "Name\tTwo", "Name\r",
                     "Name\x00", "Name\x7f", "Na\u200bme", "Na\u202eme", "Na\u2066me", "Na\u2028me", "Na\u2029me", "Na\ufeffme",
                     12, None, True, ["Name"], {"n": "x"}):
            with self.subTest(name=name):
                self.refused([{**ENTRY, "name": name}], "[0]" + NAME_ERROR)
        for title in ("D", "Doc " * 29 + "Doc", "Agreement No. 5 & Exhibit A (2025)", "Contrato de servicio, año 2025"):
            with self.subTest(title=title):
                self.assertEqual(self.accepted([{**ENTRY, "as_stated_in": title}])[0]["as_stated_in"], title)
        for title in ("", " ", " Doc", "Doc ", "D" * 121, "<i>Doc</i>", "A > B", "https://example.invalid/doc", "HTTP://example.invalid/d",
                      "www.example.invalid/doc", "Doc\nTwo", "Doc\tTwo", "Doc\x00", "Do\u200bc", "Do\u202ec", "Do\u2028c",
                      12, None, True, ["Doc"]):
            with self.subTest(title=title):
                self.refused([{**ENTRY, "as_stated_in": title}], "[0]" + TITLE_ERROR)
        self.fund([{**ENTRY, "name": "Tom & Jerry's Fund", "as_stated_in": "Q&A: Jerry's plan"}])
        self.build()
        card = self.card()
        self.assertIn("Tom &amp; Jerry&#x27;s Fund", card)  # escaped once, never twice, never interpreted as markup
        self.assertIn("as stated in Q&amp;A: Jerry&#x27;s plan (", card)
        self.assertNotIn("&amp;amp;", card)
        self.assertNotIn("&amp;#x27;", card)

    def test_amount_pair_and_fiscal_year(self):
        self.assertEqual({"amount_usd", "period", "fiscal_year"} & set(self.accepted([ENTRY])[0]), set())  # nothing inferred
        for amount, shown in (("0", "$0.00"), ("0.01", "$0.01"), ("1.2", "$1.20"), ("1000", "$1,000.00"),
                              ("100000000.00", "$100,000,000.00")):
            for period, words in (("annual", "per year"), ("total", "total"), ("one_time", "one time")):
                with self.subTest(amount=amount, period=period):
                    self.assertEqual(self.accepted([{**ENTRY, "amount_usd": amount, "period": period}])[0]["amount_usd"], amount)
                    self.assertIn(f"{shown} {words}.", self.card())
        for amount in ("100000000.01", "100000001", "1.234", "-1", "+5", "1e3", "1E3", "NaN", "Infinity", "", " 5", "1,000", "1_000",
                       "007", "00", "1.", ".5", "５", "5\n", 5, 5.5, True, None, ["5"]):
            with self.subTest(amount=amount):
                self.refused([{**ENTRY, "amount_usd": amount, "period": "annual"}], "[0]" + AMOUNT_ERROR)
        for period in ("yearly", "Annual", "", None, 1, ["annual"], True):
            with self.subTest(period=period):
                self.refused([{**ENTRY, "amount_usd": "5", "period": period}], "[0]" + PERIOD_ERROR)
        self.refused([{**ENTRY, "amount_usd": "5"}], "[0]" + PAIR_ERROR)  # a pair's halves go together
        self.refused([{**ENTRY, "period": "annual"}], "[0]" + PAIR_ERROR)
        self.refused([{**ENTRY, "amount_usd": None}], "[0]" + PAIR_ERROR)
        self.refused([{**ENTRY, "period": None}], "[0]" + PAIR_ERROR)
        for year in ("2026", "1999", "9999", "2031"):  # a document may state a future year; checked_on stays strict
            with self.subTest(year=year):
                self.assertEqual(self.accepted([{**ENTRY, "fiscal_year": year}])[0]["fiscal_year"], year)
                self.assertIn(f"Fiscal year {year}.", self.card())
        for year in (2026, True, False, None, "", "202", "20266", "２０２６", " 2026", "2026 ", "0999", "2026\n", "FY26", ["2026"], 2026.0):
            with self.subTest(year=year):
                self.refused([{**ENTRY, "fiscal_year": year}], "[0]" + YEAR_ERROR)
        two = [{**ENTRY, "amount_usd": "1000", "period": "annual"}, {**ENTRY, "name": "Other Program", "amount_usd": "2000", "period": "annual"}]
        self.fund(two)
        self.build()
        for page in (self.card(), self.read("agencies/cedar.html"), self.read("data/agencies.json")):
            self.assertNotIn("3,000", page)  # no totals are inferred
            self.assertNotIn("3000", page)

    def test_source_and_injected_build_date(self):
        url = "https://example.invalid/budget?a=1&b=2#p3"
        self.accepted([{**ENTRY, "source_url": url}])
        self.assertIn('href="https://example.invalid/budget?a=1&amp;b=2#p3"', self.card())
        self.assertEqual(self.exported()["funding"][0]["source_url"], url)
        # Assemble synthetic userinfo at runtime so credential scanners do not
        # mistake this rejection fixture for a checked-in credential.
        credential_url = "https://" + ":".join(("user", "pw")) + "@example.invalid/x"
        for bad in ("http://example.invalid/x", credential_url, "https://user@example.invalid/x", "https:///x",
                    "https://", "ftp://example.invalid/x", "example.invalid/x", "https://exa mple.invalid/x", "", None, 5, ["https://a.invalid"]):
            with self.subTest(source=bad):
                self.refused([{**ENTRY, "source_url": bad}], "[0]", alternatives=["[0]" + t for t in SOURCE_ERRORS])
        for day in ("2024-02-29", "2026-11-01"):  # a real leap day; the build date itself
            with self.subTest(day=day):
                self.assertEqual(self.accepted([{**ENTRY, "checked_on": day}])[0]["checked_on"], day)
        for day in ("2025-02-29", "2026-02-30", "2026-13-01", "2026-00-10", "0000-01-01", "2026-1-5", "20261101", "2026-11-01T00:00:00",
                    "2026-11-01 ", " 2026-11-01", "2026-11-01\n", "２０２６-11-01", "", 20261101, None, True, ["2026-11-01"]):
            with self.subTest(day=day):
                message = self.refused([{**ENTRY, "checked_on": day}], "[0]" + DAY_ERROR)
                self.assertNotIn("20261101", message)
        # the injected build date decides, not the wall clock: the next day fails though it is long past today
        self.refused([{**ENTRY, "checked_on": "2026-11-02"}], "[0]" + DAY_ERROR, today=BUILD_DAY, wall_clock=False)
        self.fund([{**ENTRY, "checked_on": "2026-11-02"}])
        self.build(today=date(2026, 11, 2))
        self.assertEqual(self.exported()["funding"][0]["checked_on"], "2026-11-02")

    def test_content_check_matches_build_under_a_frozen_clock(self):
        class Frozen(date):
            @classmethod
            def today(cls):
                return BUILD_DAY

        for day, ok in (("2026-11-01", True), ("2026-11-02", False)):
            with self.subTest(day=day):
                self.fund([{**ENTRY, "checked_on": day}])
                with mock.patch("campaign_tool.content.date", Frozen):
                    problems = content_problems(self.root) or []
                self.assertEqual(any(line == LABEL + "[0]" + DAY_ERROR for line in problems), not ok)
                if not ok:
                    self.assertEqual(len([p for p in problems if "funding" in p]), 1)
                    with self.assertRaises(ContentError) as caught:
                        self.build(today=BUILD_DAY)
                    self.assertEqual(str(caught.exception), LABEL + "[0]" + DAY_ERROR)
        self.fund([{**ENTRY, "kind": "nope"}])
        built, checked = self.cli("build"), self.cli("content-check")
        self.assertEqual(built.returncode, 1)
        self.assertIn("Stopped: " + LABEL + "[0]" + KIND_ERROR, built.stderr)
        self.assertEqual(checked.returncode, 3)
        self.assertIn(LABEL + "[0]" + KIND_ERROR, checked.stdout.splitlines())
        self.assertFalse(self.public.exists())

    def test_refusal_keeps_public_and_inputs(self):
        self.fund([ENTRY])
        self.build()
        before_public = self.snapshot()
        content = self.root / "content"

        def inputs():
            return self.snapshot(content)

        self.fund([{**ENTRY, "kind": "nope"}])
        before_inputs = inputs()
        with self.assertRaises(ContentError):
            self.build()
        self.assertEqual((self.snapshot(), inputs()), (before_public, before_inputs))  # bad funding: nothing written or changed
        self.path.write_text("{ not json", encoding="utf-8")
        before_inputs = inputs()
        with self.assertRaisesRegex(ContentError, "agencies.json: not valid UTF-8 JSON"):
            self.build()
        self.assertEqual((self.snapshot(), inputs()), (before_public, before_inputs))
        real = self.tmp / "real-agencies.json"
        real.write_text(json.dumps(self.agencies), encoding="utf-8")
        real_bytes = real.read_bytes()
        for target in (real, self.tmp / "missing.json"):  # a symlink, and a dangling one, are refused and never followed
            self.path.unlink()
            os.symlink(target, self.path)
            with self.subTest(target=target.name):
                before_inputs = inputs()
                # a live link is refused by name; a dangling one is not a file, so the build falls back as it always did
                with self.assertRaisesRegex(ContentError, "agencies.json: symlinks are not read" if target == real else ""):
                    self.build()
                self.assertEqual((self.snapshot(), real.read_bytes()), (before_public, real_bytes))
                self.assertEqual(inputs(), before_inputs)
        self.path.unlink()
        self.path.write_text(json.dumps(self.agencies), encoding="utf-8")
        original = Path.read_bytes

        def denied(path):
            if path.name == "agencies.json":
                raise PermissionError(13, "Permission denied")  # portable under privileged users: no chmod
            return original(path)
        before_inputs = inputs()
        with mock.patch.object(Path, "read_bytes", denied):
            with self.assertRaises(PermissionError):
                self.build()
        self.assertEqual((self.snapshot(), inputs()), (before_public, before_inputs))

    def test_export_allowlist_and_absence_bytes(self):
        self.build()
        plain = self.snapshot()
        self.build()
        self.assertEqual(self.snapshot(), plain)  # repeated builds are byte-identical
        self.assertEqual(set(self.exported()), set(PUBLIC_AGENCY_FIELDS))  # no funding key at all without funding
        for name, data in plain.items():
            self.assertNotIn(b"agency-funding", data, name)
            self.assertNotIn(b"How it is paid for", data, name)
            if name == "data/datapackage.json":
                document = json.loads(data)
                for resource in document["resources"]:
                    if resource["name"] == "agencies-csv":
                        fields = resource["schema"]["fields"]
                        field = {"name": "funding", "type": "string"}
                        self.assertEqual(fields.count(field), 1)
                        fields.remove(field)  # schema column metadata, not an exported funding value
                data = json.dumps(document).encode()
            self.assertNotIn(b'"funding"', data, name)
        entries = [{**ENTRY, "amount_usd": "10", "period": "annual", "fiscal_year": "2025"},
                   {**ENTRY, "kind": "donation", "name": "Synthetic Giving Org"}]
        self.fund(entries)
        self.fund(entries, SHERIFF)  # the sheriff has no finding or source, so no page and no export record
        self.build()
        funded = self.snapshot()
        self.build()
        self.assertEqual(self.snapshot(), funded)
        agency = self.exported()
        self.assertEqual(set(agency), set(PUBLIC_AGENCY_FIELDS) | {"funding"})
        self.assertEqual(agency["funding"], entries)
        doc = json.loads(self.read("data/agencies.json"))
        self.assertEqual([a["agency_id"] for a in doc["agencies"]], ["ca-cedar-police"])
        self.assertEqual(len([n for n in funded if n.startswith("agencies/")]), len([n for n in plain if n.startswith("agencies/")]))
        self.assertEqual(set(funded), set(plain))  # no new page or path because funding exists
        self.assertEqual(self.read("agencies.html").count("How it is paid for, as stated in"), 4)  # both cards still show it
        rows = list(csv.DictReader(io.StringIO(self.read("data/agencies.csv"))))
        self.assertEqual([row["agency_id"] for row in rows], ["ca-cedar-police"])
        self.assertEqual(rows[0]["funding"], "; ".join(json.dumps(entry, ensure_ascii=False, separators=(",", ":"),
                                                               sort_keys=True) for entry in entries))
        plain_rows = list(csv.DictReader(io.StringIO(plain["data/agencies.csv"].decode("utf-8"))))
        self.assertEqual([row["funding"] for row in plain_rows], [""])
        shown = {"agencies.html", "agencies/cedar.html", "data/agencies.json", "data/agencies.csv"}
        for name, data in funded.items():
            if name not in shown:
                self.assertNotIn(b"Synthetic Giving Org", data, name)  # not in feeds, scripts, search, maps or other pages
                if name in ("data.html", "data/datapackage.json"):
                    self.assertNotEqual(data, plain[name], name)  # checksums follow the changed agencies bytes
                    continue
                self.assertEqual(data, plain[name], name)  # every other file is byte for byte what it was
        page = self.read("agencies/cedar.html").lower()
        for implied in ("funded by", "sponsor", "influence", "donor", "total funding", "largest", "ranked"):
            self.assertNotIn(implied, page)
        real = Site._public_agency

        def leaky(self_, agency, cities):
            value = real(self_, agency, cities)
            if "funding" in value:
                value["funding"] = [{**value["funding"][0], "contact": "someone"}] + value["funding"][1:]
            return value
        before = self.snapshot()
        with mock.patch.object(Site, "_public_agency", leaky):
            with self.assertRaisesRegex(ContentError, "data/agencies.json: non-public key contact"):
                self.build()
        self.assertEqual(self.snapshot(), before)
        document = json.loads(self.read("data/agencies.json"))
        for broken in ({"kind": "loan"}, {"fiscal_year": "20x6"}, {"amount_usd": "1e3"}, {"amount_usd": "5", "period": None},
                       {"source_url": "http://example.invalid/x"}, {"extra": 1}):
            with self.subTest(broken=broken):
                copy = json.loads(json.dumps(document))
                copy["agencies"][0]["funding"][0].update(broken)
                with self.assertRaises(ValueError):
                    law.validate(copy, SCHEMA)
        for half in ("amount_usd", "period"):  # the schema mirrors the pair rule
            copy = json.loads(json.dumps(document))
            del copy["agencies"][0]["funding"][0][half]
            with self.assertRaises(ValueError):
                law.validate(copy, SCHEMA)
        copy = json.loads(json.dumps(document))
        copy["agencies"][0]["funding"] = []
        with self.assertRaises(ValueError):
            law.validate(copy, SCHEMA)


if __name__ == "__main__":
    unittest.main()
