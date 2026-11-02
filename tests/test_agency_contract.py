"""DF1507-14: an organizer-checked agency contract term shows on its card, on the home page when it ends within
180 days of the build date and in doctor within 90; a contract without its source or checked date stops the build.

Offline, synthetic agencies and vendors, fixed build date for the site.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool.cli import doctor_report
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 6, 1)  # fixed build date
SOURCE = "https://council.example.org/agenda?item=7&page=2"
GOOD = {"vendor": "Example Vendor Co (fictional)", "start": "2024-07-01", "end": "2026-11-27",
        "renewal_note": "Council vote expected Q3 & public comment open", "source_url": SOURCE, "checked_on": "2026-01-15"}
LINE = ('<p class="contract-term">Contract with Example Vendor Co (fictional), <time datetime="2024-07-01">2024-07-01</time>'
        ' – <time datetime="2026-11-27">2026-11-27</time>. Council vote expected Q3 &amp; public comment open (checked '
        '2026-01-15 on <a href="https://council.example.org/agenda?item=7&amp;page=2" rel="noopener noreferrer">the source'
        '</a>).</p>')


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


def day(offset, base=TODAY):
    return (base + timedelta(days=offset)).isoformat()


def agency(name, contract=None, **fields):
    entry = {"agency_id": f"ca-example-{name}", "name": f"Example {name} (fictional)", "kind": "sheriff",
             "jurisdiction_name": "Example"}
    if contract is not None:
        entry["profile"] = {"contract": contract}
    entry.update(fields)
    return entry


class AgencyContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-agency-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.base = json.loads(self.path.read_text())
        self.label = f"content/agencies.json[{len(self.base)}].profile.contract"

    def write(self, *entries):
        self.path.write_text(json.dumps(self.base + list(entries)))

    def build(self, today=TODAY):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=today)
        self.assertEqual(urlopen.call_count, 0)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def article(self, slug):
        return self.read("agencies.html").split(f'<article id="{slug}">', 1)[1].split("</article>", 1)[0]

    def home_list(self):
        page = self.read("index.html")
        if 'id="contracts"' not in page:
            return None
        section = page.split('<section class="section" id="contracts">', 1)[1].split("</section>", 1)[0]
        return [item.split('href="/agencies.html#', 1)[1].split('"', 1)[0] for item in section.split("<li>")[1:]]

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def test_valid_contract_renders_on_card_with_times_and_safe_source_link(self):
        self.write(agency("police", GOOD), agency("council"))
        self.build()
        card = self.article("example-police")
        self.assertEqual(card.count(LINE), 1)
        self.assertNotIn("contract-term", self.article("example-council"))
        self.assertNotIn(SOURCE, self.read("agencies.html"))  # raw & is escaped
        self.assertEqual(self.home_list(), ["example-police"])  # ends in 179 days
        self.assertIn('Example police (fictional)</a>: contract with Example Vendor Co (fictional) ends '
                      '<time datetime="2026-11-27">2026-11-27</time>', self.read("index.html"))

    def test_missing_or_bad_fields_stop_build_and_content_check_names_the_same_line(self):
        self.build()
        before = self.snapshot()
        tomorrow = day(1, date.today())
        cases = [({k: v for k, v in GOOD.items() if k != "source_url"}, ": needs source_url and checked_on"),
                 ({k: v for k, v in GOOD.items() if k != "checked_on"}, ": needs source_url and checked_on"),
                 ({}, ": needs source_url and checked_on"),
                 ({**GOOD, "checked_on": tomorrow}, ".checked_on: must not be after the build date"),
                 ({**GOOD, "start": "2026-12-01", "end": "2026-11-30"}, ".end: must not be before start"),
                 ({**GOOD, "start": "2026-02-30"}, ".start: invalid ISO-8601 value '2026-02-30'"),
                 ({**GOOD, "end": "2026-1-5"}, ".end: must be a YYYY-MM-DD date"),
                 ({**GOOD, "source_url": "http://council.example.org/"}, ".source_url: must be an https:// URL"),
                 ({**GOOD, "source_url": None}, ".source_url: must be an https:// URL"),
                 ({k: v for k, v in GOOD.items() if k != "vendor"}, ": missing field(s) ['vendor']"),
                 ({**GOOD, "vendor": "  "}, ".vendor: must not be empty"),
                 ({**GOOD, "vendor": "V" * 81}, ".vendor: at most 80 characters"),
                 ({**GOOD, "renewal_note": "n" * 201}, ".renewal_note: at most 200 characters"),
                 ({**GOOD, "amount": "1"}, ": unknown field(s) ['amount']"),
                 ("2026-11-27", ": expected an object")]
        for contract, tail in cases:
            with self.subTest(tail=tail, contract=contract):
                self.write(agency("police", contract))
                message = self.label + tail
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(self.snapshot(), before)  # old public/ kept
                self.assertIn(message, content_problems(self.root))
        for contract, tail in cases[:5]:  # the refusals the spec names, end to end through the CLI
            with self.subTest(cli=tail):
                self.write(agency("police", contract))
                built, checked = self.cli("build"), self.cli("content-check")
                self.assertEqual(built.returncode, 1)
                self.assertIn("Stopped: " + self.label + tail, built.stderr)
                self.assertEqual(checked.returncode, 3)
                self.assertIn(self.label + tail, checked.stdout.splitlines())

    def test_vendor_or_note_markup_is_refused_as_not_plain_text(self):
        self.build()
        for key in ("vendor", "renewal_note"):
            with self.subTest(key=key):
                self.write(agency("police", {**GOOD, key: "<script>alert(1)</script>"}))
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertEqual(str(caught.exception), f"{self.label}.{key}: must be plain text without angle brackets")

    def test_home_window_is_today_through_180_days_soonest_first(self):
        self.write(agency("far", {**GOOD, "end": day(181)}), agency("near", {**GOOD, "end": day(179)}),
                   agency("today", {**GOOD, "end": day(0)}), agency("ended", {**GOOD, "end": day(-1)}),
                   agency("edge", {**GOOD, "end": day(180)}))
        self.build()
        self.assertEqual(self.home_list(), ["example-today", "example-near", "example-edge"])
        ended = self.article("example-ended")
        self.assertIn(f'<time datetime="{day(-1)}">{day(-1)}</time>, ended. ', ended)
        for slug in ("example-today", "example-near", "example-far"):
            self.assertNotIn(", ended", self.article(slug))
        self.write(agency("ended", {**GOOD, "end": day(-1)}), agency("far", {**GOOD, "end": day(181)}))
        self.build()
        self.assertIsNone(self.home_list())  # no section at all when nothing ends in the window

    def test_no_contract_or_out_of_window_adds_nothing_and_card_js_never_changes(self):
        self.write(agency("police"))
        self.build()
        plain = self.snapshot()
        self.assertNotIn(b"contract", plain["index.html"] + plain["agencies.html"])
        self.write(agency("police", {**GOOD, "end": day(400)}))
        self.build()
        dated = self.snapshot()
        self.assertEqual(dated["index.html"], plain["index.html"])
        self.assertEqual(set(dated), set(plain))
        self.assertEqual({k: v for k, v in dated.items() if k != "agencies.html"},
                         {k: v for k, v in plain.items() if k != "agencies.html"})
        self.write(agency("police", GOOD))
        self.build()
        listed = self.snapshot()
        for name in ("agency-cards.js", "site-data.js"):
            self.assertEqual(listed[name], plain[name], name)  # contract never reaches the card JS config
        self.assertNotIn(b"Example Vendor", listed["site-data.js"] + listed["agency-cards.js"])

    def test_county_profile_has_no_contract_key(self):
        site_path = self.root / "content" / "site.json"
        site = json.loads(site_path.read_text())
        site.setdefault("county_profile", {})["contract"] = GOOD
        site_path.write_text(json.dumps(site))
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn("site.json.county_profile: unknown field(s) ['contract']", str(caught.exception))

    def test_doctor_names_contracts_ending_within_90_days(self):
        now = date.today()
        self.write(agency("soon", {**GOOD, "end": day(60, now)}), agency("later", {**GOOD, "end": day(120, now)}),
                   agency("ended", {**GOOD, "end": day(-1, now)}), agency("edge", {**GOOD, "end": day(90, now)}))
        report = doctor_report(self.root)
        self.assertEqual(report["contracts_ending"],
                         [{"agency_id": "ca-example-soon", "end": day(60, now), "days": 60},
                          {"agency_id": "ca-example-edge", "end": day(90, now), "days": 90}])
        self.assertIn(f"Contract terms ending within 90 days: ca-example-soon ({day(60, now)}), "
                      f"ca-example-edge ({day(90, now)})", report["next_actions"])
        self.write(agency("later", {**GOOD, "end": day(120, now)}))
        report = doctor_report(self.root)
        self.assertEqual(report["contracts_ending"], [])
        self.assertFalse([a for a in report["next_actions"] if a.startswith("Contract terms")])

    def test_doctor_key_absent_without_content_or_on_load_failure(self):
        baseline = doctor_report(self.root)
        self.write(agency("broken", {k: v for k, v in GOOD.items() if k != "source_url"}))
        broken = doctor_report(self.root)
        self.assertNotIn("contracts_ending", broken)
        shutil.rmtree(self.root / "content")
        absent = doctor_report(self.root)
        self.assertNotIn("contracts_ending", absent)
        baseline.pop("contracts_ending")
        self.assertEqual(broken, baseline)  # a broken content tree leaves doctor otherwise unchanged


if __name__ == "__main__":
    unittest.main()
