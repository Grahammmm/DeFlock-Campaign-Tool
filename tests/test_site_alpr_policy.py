"""DF1822-15: an organizer-verified profile.alpr_policy links a posted ALPR usage policy on the agency's card with its
checked date and where it was found; a half-filled or bad entry stops the build naming the field; no compliance wording.

Offline, synthetic agencies, example.org URLs only.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)  # fixed site build date
POLICY = {"url": "https://example.org/sheriff/policy?id=4&format=pdf", "source_url": "https://example.org/sheriff/policies",
          "checked_on": "2026-10-30"}
LINE = ('<p class="policy-link"><a href="https://example.org/sheriff/policy?id=4&amp;format=pdf" rel="noopener noreferrer">'
        'Posted ALPR usage policy</a> (checked 2026-10-30, <a href="https://example.org/sheriff/policies" '
        'rel="noopener noreferrer">where we found it</a>)</p>')
JUDGEMENT = re.compile(r"complian|violat|lawful|illegal", re.IGNORECASE)


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


def agency(name, policy=..., **fields):
    entry = {"agency_id": f"ca-example-{name}", "name": f"Example {name} (fictional)", "kind": "sheriff",
             "jurisdiction_name": "Example", "records_url": "https://example.org/records"}
    if policy is not ...:
        entry["profile"] = {"alpr_policy": policy}
    entry.update(fields)
    return entry


class AlprPolicyLinkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-alpr-policy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.base = json.loads(self.path.read_text())
        self.label = f"content/agencies.json[{len(self.base)}].profile.alpr_policy"

    def write(self, *entries):
        self.path.write_text(json.dumps(self.base + list(entries)))

    def build(self):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=TODAY)
        self.assertEqual(urlopen.call_count, 0)

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def article(self, slug):
        page = (self.public / "agencies.html").read_text(encoding="utf-8")
        return page.split(f'<article id="{slug}">', 1)[1].split("</article>", 1)[0]

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def test_full_entry_renders_exact_line_after_links_on_its_card_only(self):
        self.write(agency("sheriff", POLICY), agency("council"))
        self.build()
        card = self.article("example-sheriff")
        self.assertEqual(card.count(LINE), 1)
        links_end = card.index('rel="noopener noreferrer">', card.index("https://example.org/records"))
        self.assertLess(links_end, card.index(LINE))
        self.assertTrue(card.split(LINE, 1)[0].endswith("</p>"))  # right after the links paragraph
        self.assertNotIn("policy-link", self.article("example-council"))
        pages = self.snapshot()
        self.assertNotIn(POLICY["url"].encode(), pages["agencies.html"])  # raw & never reaches the page
        for name in ("index.html", "agency-cards.js", "site-data.js"):  # agencies page only
            self.assertNotIn(b"policy-link", pages[name], name)
            self.assertNotIn(b"example.org/sheriff/polic", pages[name], name)

    def test_bad_entries_stop_build_with_field_named_and_content_check_lists_them(self):
        self.build()
        before = self.snapshot()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        date_message = ".checked_on: must be a YYYY-MM-DD date not after the build date"
        cases = [({k: v for k, v in POLICY.items() if k != "source_url"}, ": needs url, source_url and checked_on"),
                 ({k: v for k, v in POLICY.items() if k != "checked_on"}, ": needs url, source_url and checked_on"),
                 ({k: v for k, v in POLICY.items() if k != "url"}, ": needs url, source_url and checked_on"),
                 ({}, ": needs url, source_url and checked_on"),
                 ({**POLICY, "status": "posted"}, ": unknown field(s) ['status']"),
                 ({**POLICY, "url": "http://example.org/sheriff/policy"}, ".url: must be an https:// URL"),
                 ({**POLICY, "url": None}, ".url: must be an https:// URL"),
                 ({**POLICY, "source_url": "http://example.org/"}, ".source_url: must be an https:// URL"),
                 ({**POLICY, "checked_on": tomorrow}, date_message),
                 ({**POLICY, "checked_on": "2026-02-30"}, date_message),
                 ({**POLICY, "checked_on": "30/10/2026"}, date_message),
                 ({**POLICY, "checked_on": "2026-1-5"}, date_message),
                 ({**POLICY, "checked_on": 20261030}, date_message),
                 ("https://example.org/sheriff/policy", ": expected an object")]
        for policy, tail in cases:
            with self.subTest(tail=tail, policy=policy):
                self.write(agency("sheriff", policy))
                message = self.label + tail
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(self.snapshot(), before)  # public/ untouched
                self.assertIn(message, content_problems(self.root))
        for policy, tail in (cases[0], cases[1], cases[9]):  # end to end through the CLI
            with self.subTest(cli=tail):
                self.write(agency("sheriff", policy))
                built, checked = self.cli("build"), self.cli("content-check")
                self.assertEqual(built.returncode, 1)
                self.assertIn("Stopped: " + self.label + tail, built.stderr)
                self.assertEqual(checked.returncode, 3)
                self.assertIn(self.label + tail, checked.stdout.splitlines())
                self.assertEqual(self.snapshot(), before)  # nothing written

    def test_null_or_absent_policy_adds_nothing(self):
        self.write(agency("sheriff"))
        self.build()
        absent = self.snapshot()
        self.assertNotIn("policy-link", self.article("example-sheriff"))
        self.write(agency("sheriff", None))
        self.build()
        self.assertEqual(self.snapshot(), absent)
        self.write(agency("sheriff", {}, profile={}))
        self.build()
        self.assertEqual(self.snapshot(), absent)

    def test_unselected_agency_with_broken_policy_is_skipped(self):
        self.write(agency("sheriff", {"url": "http://bad"}, selected=False), agency("council"))
        self.build()
        self.assertNotIn('id="example-sheriff"', (self.public / "agencies.html").read_text(encoding="utf-8"))
        self.assertFalse([p for p in content_problems(self.root) if "alpr_policy" in p])

    def test_no_judgement_wording_and_county_profile_refuses_the_key(self):
        self.write(agency("sheriff", POLICY))
        self.build()
        for name, data in self.snapshot().items():
            if name.endswith((".html", ".xml", ".js", ".json", ".txt")):
                self.assertIsNone(JUDGEMENT.search(data.decode("utf-8")), name)
        site_path = self.root / "content" / "site.json"
        site = json.loads(site_path.read_text())
        site.setdefault("county_profile", {})["alpr_policy"] = POLICY
        site_path.write_text(json.dumps(site))
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn("site.json.county_profile: unknown field(s) ['alpr_policy']", str(caught.exception))

    def test_example_campaign_builds_its_fictional_policy_link(self):
        self.build()
        card = self.article("cedar-county-sheriff")
        self.assertEqual(card.count('<p class="policy-link"><a href="https://example.org/cedar-county-sheriff/alpr-policy.pdf"'
                                    ' rel="noopener noreferrer">Posted ALPR usage policy</a> (checked 2026-10-30, '), 1)
        self.assertEqual((self.public / "agencies.html").read_text(encoding="utf-8").count("policy-link"), 1)
        built = self.cli("build", "--check")
        self.assertEqual(built.returncode, 0, built.stderr)


if __name__ == "__main__":
    unittest.main()
