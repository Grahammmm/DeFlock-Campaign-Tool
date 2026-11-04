"""DF1103-07: an organizer-transcribed contract cost, shown exactly as the document states it beside the contract's own
source and checked date (card, agency page, "At a glance" cell, data/agencies.json); never summed, ranked or divided.

Offline, synthetic agencies, vendors and documents only; the network is blocked.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from html import escape
from pathlib import Path
from unittest import mock

from campaign_tool import law
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.site import PUBLIC_COST_FIELDS, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "agencies-open-data.schema.json").read_text(encoding="utf-8"))
BUILD_DAY = date(2026, 11, 1)
CEDAR = 1  # index of Cedar PD in the example's agencies.json: it has an agency page
SOURCE = "https://example.invalid/cedar/contract?x=1&y=2"
CONTRACT = {"vendor": "Synthetic vendor", "start": "2025-01-01", "end": "2027-01-01", "renewal_note": "Private renewal remark.",
            "source_url": SOURCE, "checked_on": "2026-10-21"}
COST = {"amount_usd": "12345.6", "period": "annual", "as_stated_in": "Example agreement"}
SOURCE_LINK = '<a href="https://example.invalid/cedar/contract?x=1&amp;y=2" rel="noopener noreferrer">source</a>'
LABEL = f"content/agencies.json[{CEDAR}].profile.contract"
AMOUNT_ERROR = ".cost.amount_usd: decimal string from 0 to 100000000 with at most 2 decimals"
PERIOD_ERROR = ".cost.period: choose annual, total or one_time"
TITLE_ERROR = ".cost.as_stated_in: plain document title of 1 to 120 characters"
SHAPE_ERROR = ".cost: needs only amount_usd, period and as_stated_in"


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


class ContractCostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-contract-cost-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.agencies = json.loads(self.path.read_text(encoding="utf-8"))

    def contract(self, cost: object = "absent", **fields):
        """Write Cedar PD with the contract (and ``cost`` unless 'absent'); returns the contract written."""
        contract = {**CONTRACT, **fields}
        if cost != "absent":
            contract["cost"] = cost
        self.agencies[CEDAR].setdefault("profile", {})["contract"] = contract
        self.path.write_text(json.dumps(self.agencies), encoding="utf-8")
        return contract

    def build(self):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=BUILD_DAY)
        self.assertEqual(urlopen.call_count, 0)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def card(self):
        return self.read("agencies.html").split('<article id="cedar">', 1)[1].split("</article>", 1)[0]

    def glance_row(self):
        glance = self.read("agencies.html").split('id="at-a-glance"', 1)[1].split("</section>", 1)[0]
        return [r for r in glance.split("<tr>") if "Cedar Police" in r][0]

    def agency_entry(self):
        doc = json.loads(self.read("data/agencies.json"))
        law.validate(doc, SCHEMA)
        return [a for a in doc["agencies"] if a["agency_id"] == "ca-cedar-police"][0]

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def test_amount_boundaries_and_decimal_format(self):
        for amount, shown in (("0", "$0.00"), ("0.01", "$0.01"), ("1", "$1.00"), ("12345.6", "$12,345.60"),
                              ("12345.60", "$12,345.60"), ("999.99", "$999.99"), ("1000", "$1,000.00"),
                              ("100000000", "$100,000,000.00"), ("100000000.00", "$100,000,000.00")):
            for period, words in (("annual", "per year"), ("total", "total"), ("one_time", "one time")):
                with self.subTest(amount=amount, period=period):
                    self.contract({"amount_usd": amount, "period": period, "as_stated_in": "Example agreement"})
                    self.build()
                    line = (f'<p class="contract-cost">Contract cost as stated: {shown} {words} '
                            f'(Example agreement; {SOURCE_LINK}, checked 2026-10-21).</p>')
                    self.assertEqual(self.card().count(line), 1)
                    self.assertEqual(self.agency_entry()["contract"]["cost"],  # the accepted string, not a float
                                     {"amount_usd": amount, "period": period, "as_stated_in": "Example agreement"})
        self.contract({"amount_usd": "0.10", "period": "total", "as_stated_in": "Example agreement"})
        self.build()
        self.assertIn('"amount_usd": "0.10"', self.read("data/agencies.json"))  # trailing zero kept as transcribed

    def test_amount_refusals_leave_public_unchanged(self):
        self.contract(COST)
        self.build()
        before = self.snapshot()
        bad = ["100000000.01", "100000001", "1" + "0" * 30, "-1", "-0", "+5", "1e3", "1E3", "NaN", "Infinity", "", " 5", "5 ",
               "1,000", "1_000", "007", "00", "1.", ".5", "1.234", "1.5.2", "５", "5\n", "0x10", 5, 5.5, True, False, None,
               ["5"], {"v": "5"}]
        for amount in bad:
            with self.subTest(amount=amount):
                self.contract({**COST, "amount_usd": amount})
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertEqual(str(caught.exception), LABEL + AMOUNT_ERROR)
                self.assertEqual(self.snapshot(), before)  # old public/ kept, nothing written
                self.assertIn(LABEL + AMOUNT_ERROR, content_problems(self.root))
                self.assertNotIn(str(amount) + "\n", str(caught.exception))  # the supplied value is never echoed back
        for period in ("yearly", "Annual", "monthly", "", None, 1, ["annual"], True):
            with self.subTest(period=period):
                self.contract({**COST, "period": period})
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertEqual(str(caught.exception), LABEL + PERIOD_ERROR)
                self.assertEqual(self.snapshot(), before)
                self.assertIn(LABEL + PERIOD_ERROR, content_problems(self.root))

    def test_cost_shape_and_document_title(self):
        self.contract(COST)
        self.build()
        before = self.snapshot()
        for cost in (None, [], "12", 12, True, {}, {"amount_usd": "1", "period": "total"},
                     {"amount_usd": "1", "as_stated_in": "Doc"}, {"period": "total", "as_stated_in": "Doc"},
                     {**COST, "extra": "x"}, {**COST, "Amount_USD": "1"}, {**COST, "total_usd": "1"}):
            with self.subTest(cost=cost):
                self.contract(cost)
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertEqual(str(caught.exception), LABEL + SHAPE_ERROR)  # no raw unknown-key names
                self.assertEqual(self.snapshot(), before)
                self.assertIn(LABEL + SHAPE_ERROR, content_problems(self.root))
        for title in ("", " ", "   ", " Doc", "Doc ", "D" * 121, "<b>Doc</b>", "Doc <1>", "A > B", "https://example.invalid/doc",
                      "HTTP://example.invalid/doc", "www.example.invalid/doc", "Doc\nTwo", "Doc\tTwo", "Doc\x00", "Doc\x7f",
                      "Do\u200bc", "Do\u202ec", "Do\u2028c", "Do\ufeffc", 12, None, True, ["Doc"]):
            with self.subTest(title=title):
                self.contract({**COST, "as_stated_in": title})
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertEqual(str(caught.exception), LABEL + TITLE_ERROR)
                self.assertEqual(self.snapshot(), before)
                self.assertIn(LABEL + TITLE_ERROR, content_problems(self.root))
        for title in ("D", "Doc " * 29 + "Doc", "Agreement No. 5 & Exhibit A (2025)", "Contrato de servicio, año 2025"):
            with self.subTest(title=title):
                self.contract({**COST, "as_stated_in": title})
                self.build()
                self.assertEqual(self.agency_entry()["contract"]["cost"]["as_stated_in"], title)

    def test_build_and_content_check_same_error(self):
        self.contract(COST)
        self.build()
        before = self.snapshot()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        no_source = {k: v for k, v in CONTRACT.items() if k != "source_url"}
        no_checked = {k: v for k, v in CONTRACT.items() if k != "checked_on"}
        cases = [({**no_source, "cost": COST}, ": needs source_url and checked_on"),  # cost adds no second provenance
                 ({**no_checked, "cost": COST}, ": needs source_url and checked_on"),
                 ({**CONTRACT, "cost": COST, "checked_on": tomorrow}, ".checked_on: must not be after the build date"),
                 ({**CONTRACT, "cost": COST, "checked_on": "2026-13-01"}, ".checked_on: invalid ISO-8601 value '2026-13-01'"),
                 ({**CONTRACT, "cost": COST, "source_url": "http://example.invalid/c"}, ".source_url: must be an https:// URL"),
                 ({**CONTRACT, "cost": {**COST, "amount_usd": "1e3"}}, AMOUNT_ERROR),
                 ({**CONTRACT, "cost": {**COST, "amount_usd": 12}}, AMOUNT_ERROR),
                 ({**CONTRACT, "cost": {**COST, "period": "weekly"}}, PERIOD_ERROR),
                 ({**CONTRACT, "cost": {**COST, "as_stated_in": "<i>x</i>"}}, TITLE_ERROR),
                 ({**CONTRACT, "cost": None}, SHAPE_ERROR),
                 ({**CONTRACT, "cost": {**COST, "source_url": SOURCE}}, SHAPE_ERROR)]
        for contract, tail in cases:
            with self.subTest(tail=tail, contract=contract):
                self.agencies[CEDAR]["profile"]["contract"] = contract
                self.path.write_text(json.dumps(self.agencies), encoding="utf-8")
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertEqual(str(caught.exception), LABEL + tail)
                self.assertEqual(self.snapshot(), before)
                self.assertIn(LABEL + tail, content_problems(self.root))
        for contract, tail in (cases[0], cases[2], cases[5], cases[7], cases[8], cases[9]):  # end to end through the CLI
            with self.subTest(cli=tail):
                self.agencies[CEDAR]["profile"]["contract"] = contract
                self.path.write_text(json.dumps(self.agencies), encoding="utf-8")
                built, checked = self.cli("build"), self.cli("content-check")
                self.assertEqual(built.returncode, 1)
                self.assertIn("Stopped: " + LABEL + tail, built.stderr)
                self.assertEqual(checked.returncode, 3)
                self.assertIn(LABEL + tail, checked.stdout.splitlines())
                self.assertEqual(self.snapshot(), before)

    def test_card_page_glance_and_open_data_agree(self):
        title = 'Council "Item 7" & <exhibit>'.replace("<exhibit>", "(Exhibit A)")
        self.contract({"amount_usd": "250000", "period": "total", "as_stated_in": title})
        self.build()
        shown = "$250,000.00 total"
        line = (f'Contract cost as stated: {shown} (Council &quot;Item 7&quot; &amp; (Exhibit A); {SOURCE_LINK}, '
                'checked 2026-10-21).')
        page = self.read("agencies/cedar.html")
        self.assertEqual(self.card().count(line), 1)  # the card
        self.assertEqual(page.count(line), 1)  # the agency page, once, beside the contract term
        self.assertIn('<p class="contract-term">Contract with Synthetic vendor', page)
        self.assertLess(page.index("contract-term"), page.index("contract-cost"))
        self.assertNotIn(SOURCE, self.read("agencies.html") + page)  # the raw & is escaped everywhere
        self.assertNotIn('"Item 7"', self.read("agencies.html") + page)
        cell = self.glance_row().split('data-label="Contract cost as stated">', 1)[1].split("</td>", 1)[0]
        self.assertEqual(cell, f'<a href="https://example.invalid/cedar/contract?x=1&amp;y=2" rel="noopener noreferrer">{shown}</a> '
                               '<small>checked <time datetime="2026-10-21">2026-10-21</time></small>')
        self.assertIn('<th scope="col">Contract cost as stated</th>', self.read("agencies.html"))
        heads = re.findall(r'<th scope="col">([^<]*)</th>', self.read("agencies.html").split('id="at-a-glance"', 1)[1])
        self.assertEqual(heads[5:8], ["Contract ends", "Contract cost as stated", "Records items asked"])
        entry = self.agency_entry()  # validated against the schema above; the exact three-field allowlist
        self.assertEqual(entry["contract"]["cost"], {"amount_usd": "250000", "period": "total", "as_stated_in": title})
        self.assertEqual(set(entry["contract"]["cost"]), set(PUBLIC_COST_FIELDS))
        self.assertNotIn("renewal_note", entry["contract"])
        self.assertNotIn("Private renewal remark", self.read("data/agencies.json"))
        cost_schema = SCHEMA["$defs"]["cost"]  # the schema refuses any other key or a number in place of the string
        self.assertFalse(cost_schema["additionalProperties"])
        for broken in ({**entry["contract"]["cost"], "total": "1"}, {**entry["contract"]["cost"], "amount_usd": 250000},
                       {**entry["contract"]["cost"], "amount_usd": "1e3"}, {**entry["contract"]["cost"], "period": "monthly"},
                       {"amount_usd": "1", "period": "total"}):
            doc = json.loads(self.read("data/agencies.json"))
            doc["agencies"][[a["agency_id"] for a in doc["agencies"]].index("ca-cedar-police")]["contract"]["cost"] = broken
            with self.subTest(broken=broken), self.assertRaises(Exception):
                law.validate(doc, SCHEMA)

    def test_old_contracts_and_private_boundaries(self):
        self.contract()  # a contract without cost: reads and builds as before
        self.build()
        old = self.snapshot()
        self.assertNotIn("contract-cost", self.card())
        self.assertNotIn("cost", self.agency_entry()["contract"])
        self.assertNotIn(b"Contract cost as stated:", b"".join(old.values()))
        self.assertIn('data-label="Contract cost as stated">not checked</td>', self.glance_row())
        page_with_term_only = old["agencies/cedar.html"]
        del self.agencies[CEDAR]["profile"]["contract"]  # no contract at all
        self.path.write_text(json.dumps(self.agencies), encoding="utf-8")
        self.build()
        none = self.snapshot()
        self.assertEqual(none["agencies/cedar.html"], page_with_term_only)  # a no-cost page is byte-identical, as before
        self.assertIn('data-label="Contract cost as stated">not checked</td>', self.glance_row())
        self.assertIn('data-label="Contract ends">not checked</td>', self.glance_row())
        self.assertEqual({k for k in old if old[k] != none[k]}, {"agencies.html", "data/agencies.json", "index.html"} & set(old))
        # a cost on a contract with the optional renewal_note omitted is valid, and the private note never leaves
        self.contract(COST, renewal_note="")
        no_note = {k: v for k, v in self.agencies[CEDAR]["profile"]["contract"].items() if k != "renewal_note"}
        self.agencies[CEDAR]["profile"]["contract"] = no_note
        self.path.write_text(json.dumps(self.agencies), encoding="utf-8")
        self.build()
        # the amount appears on the card, the agency page and the open data only: never a feed, map, sitemap or the card JS
        carriers = sorted(name for name, data in self.snapshot().items() if b"12,345.60" in data or b"12345.6" in data)
        self.assertEqual(carriers, ["agencies.html", "agencies/cedar.html", "data/agencies.json"])
        for name, data in self.snapshot().items():
            self.assertNotIn(b"Example agreement", data if name not in carriers else b"")
        # an agency whose card exists but has no page is not exported, cost included
        self.agencies[0].setdefault("profile", {})["contract"] = {**CONTRACT, "cost": {**COST, "amount_usd": "777"}}
        self.agencies[0].setdefault("profile", {}).setdefault("status", "Listed")
        self.path.write_text(json.dumps(self.agencies), encoding="utf-8")
        self.build()
        self.assertNotIn("777", self.read("data/agencies.json"))
        self.assertIn("$777.00 per year", self.read("agencies.html"))
        # nothing says it adds up, ranks or divides
        text = self.read("agencies.html") + self.read("agencies/cedar.html")
        for banned in ("per camera", "per-camera", "combined", "all agencies", "cheapest", "most expensive"):
            self.assertNotIn(banned, text.lower())

    def test_model_order_not_cost_order_and_rebuild(self):
        costs = {0: "9000000", CEDAR: "0", 2: None}  # expensive, zero, missing
        for index, amount in costs.items():
            profile = self.agencies[index].setdefault("profile", {})
            profile["contract"] = {**CONTRACT, "cost": {**COST, "amount_usd": amount}} if amount else dict(CONTRACT)
        self.path.write_text(json.dumps(self.agencies), encoding="utf-8")
        self.build()
        first = self.snapshot()
        names = [escape(a["name"]) for a in self.agencies]
        rows = [r for r in self.read("agencies.html").split('id="at-a-glance"', 1)[1].split("</tbody>", 1)[0].split("<tr>")[2:]]
        self.assertEqual([next(n for n in names if n in row) for row in rows], names)  # the model's order, never the amounts'
        cells = [row.split('data-label="Contract cost as stated">', 1)[1].split("</td>", 1)[0] for row in rows]
        self.assertIn("$9,000,000.00 per year", cells[0])
        self.assertIn("$0.00 per year", cells[1])
        self.assertEqual(cells[2], "not checked")
        self.build()
        self.assertEqual(self.snapshot(), first)  # repeated builds are byte-identical
        agencies = json.loads(self.read("data/agencies.json"))["agencies"]
        self.assertEqual([(a["agency_id"], a["contract"]["cost"]["amount_usd"]) for a in agencies], [("ca-cedar-police", "0")])


if __name__ == "__main__":
    unittest.main()
