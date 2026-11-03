"""DF0137-11: organizer-transcribed Flock transparency-portal snapshots (``profile.transparency``) show on the agency
card and page as the vendor's own report with the date checked and the change since the previous snapshot; bad
snapshots stop build and content-check with the same message; never fetched, never in the card JS.

Offline, synthetic agencies, slugs and numbers only.
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

from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.labels import DEFAULTS
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
URL = "https://transparency.flocksafety.com/cedar-pd-example"
NEW = {"checked_on": "2026-02-01", "source_url": URL, "cameras": 4242, "retention_days": 30,
       "searches_last_30_days": 9100, "hotlist_alerts": 77, "shared_with_agencies": 13}
OLD = {"checked_on": "2025-11-15", "source_url": URL + "/", "cameras": 4236, "retention_days": 30,
       "searches_last_30_days": 9300, "hotlist_alerts": None}
LABEL = "content/agencies.json[1].profile.transparency"
BLOCK_HEADING = "Reported on the Flock transparency portal"


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


def since(date_value):
    return f'<time datetime="{date_value}">{date_value}</time>'


class AgencyTransparencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-agency-transparency-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.base = json.loads(self.path.read_text())
        assert self.base[1]["agency_id"] == "ca-cedar-police"

    def write(self, transparency=None, verified=True, slug="cedar-pd-example"):
        agencies = json.loads(json.dumps(self.base))
        cedar = agencies[1]
        if verified:
            cedar.update(verified=True, verification={"source_url": "https://example.invalid/contact",
                                                      "checked_on": "2026-01-15"})
        if slug:
            cedar["flock_transparency_slug"] = slug
        if transparency is not None:
            cedar["profile"]["transparency"] = transparency
        self.path.write_text(json.dumps(agencies))

    def build(self, today=None):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=today)
        self.assertEqual(urlopen.call_count, 0)

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def block(self, page):
        text = self.read(page)
        if "agency-transparency" not in text:
            return ""
        return text.split('<section class="agency-transparency">', 1)[1].split("</section>", 1)[0]

    def card(self):
        return self.read("agencies.html").split('<article id="cedar">', 1)[1].split("</article>", 1)[0]

    def refused(self, transparency, message, **fields):
        self.write(transparency, **fields)
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertEqual(str(caught.exception), message)
        self.assertIn(message, content_problems(self.root))

    def test_newest_shown_with_change_since_previous_on_card_and_page(self):
        self.write([NEW, OLD])
        self.build()
        expected = ("<h4>Reported on the Flock transparency portal</h4><ul>"
                    f"<li>Cameras: 4242 (up 6 since {since('2025-11-15')})</li>"
                    f"<li>Retention days: 30 (no change since {since('2025-11-15')})</li>"
                    f"<li>Searches in the last 30 days: 9100 (down 200 since {since('2025-11-15')})</li>"
                    "<li>Hotlist alerts: 77</li><li>Shared with agencies: 13</li></ul>"
                    f'<p><a href="{URL}" rel="noopener noreferrer">as reported by the vendor, checked '
                    f"{since('2026-02-01')}</a></p>")
        self.assertEqual(self.block("agencies.html"), expected)
        self.assertIn('<section class="agency-transparency">', self.card())
        self.assertEqual(self.block("agencies/cedar.html"), expected.replace("h4>", "h3>"))
        self.assertNotIn("9300", self.read("agencies.html"))  # the older snapshot's numbers are not shown

    def test_single_snapshot_has_no_change_phrase_and_all_null_shows_only_checked_line(self):
        self.write([NEW])
        self.build()
        self.assertNotIn("since", self.block("agencies.html"))
        self.write([{"checked_on": "2026-02-01", "source_url": URL, "cameras": None}, OLD])
        self.build()
        self.assertEqual(self.block("agencies.html"),
                         f'<h4>{BLOCK_HEADING}</h4><p><a href="{URL}" rel="noopener noreferrer">'
                         f"as reported by the vendor, checked {since('2026-02-01')}</a></p>")

    def test_checked_on_after_build_date_duplicate_order_and_count_refused(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.refused([{**NEW, "checked_on": tomorrow}], f"{LABEL}[0].checked_on: must not be after the build date")
        self.refused([NEW, {**OLD, "checked_on": NEW["checked_on"]}], f"{LABEL}[1].checked_on: duplicate date")
        self.refused([OLD, NEW], f"{LABEL}[1].checked_on: snapshots must be listed newest first")
        self.refused([{**NEW, "checked_on": "2026-2-1"}], f"{LABEL}[0].checked_on: must be a YYYY-MM-DD date")
        days = [(date(2026, 1, 25) - timedelta(days=n)).isoformat() for n in range(25)]
        self.refused([{"checked_on": d, "source_url": URL} for d in days], f"{LABEL}: must be a list of at most 24 snapshots")
        self.write([{"checked_on": d, "source_url": URL} for d in days[:24]])
        self.build()  # 24 is accepted
        self.refused({"checked_on": "2026-01-01"}, f"{LABEL}: must be a list of at most 24 snapshots")
        self.refused([{**NEW, "notes": "x"}], f"{LABEL}[0]: unknown field(s) ['notes']")
        self.refused([{"checked_on": "2026-01-01"}], f"{LABEL}[0]: missing field(s) ['source_url']")

    def test_build_date_is_the_limit_not_the_wall_clock(self):
        self.write([{**NEW, "checked_on": "2026-06-02"}])
        with self.assertRaises(ContentError) as caught:
            self.build(today=date(2026, 6, 1))
        self.assertEqual(str(caught.exception), f"{LABEL}[0].checked_on: must not be after the build date")
        self.write([{**NEW, "checked_on": "2026-06-01"}])
        self.build(today=date(2026, 6, 1))

    def test_source_url_must_be_the_agencys_own_portal_page_and_agency_verified(self):
        wrong = f"{LABEL}[0].source_url: must be {URL}"
        for url in ("https://transparency.flocksafety.com.example.org/cedar-pd-example",
                    "https://example.org/cedar-pd-example", "https://transparency.flocksafety.com/other-pd",
                    "https://transparency.flocksafety.com/cedar-pd-example?x=1", URL + "?", URL + "#top",
                    "https://transparency.flocksafety.com/cedar-pd-example/extra",
                    "https://transparency.flocksafety.com:8443/cedar-pd-example"):
            with self.subTest(url=url):
                self.refused([{**NEW, "source_url": url}], wrong)
        self.refused([{**NEW, "source_url": "http://transparency.flocksafety.com/cedar-pd-example"}],
                     f"{LABEL}[0].source_url: must be an https:// URL")
        needs = f"{LABEL}: needs a verified agency with a flock_transparency_slug"
        self.refused([NEW], needs, verified=False)
        self.refused([NEW], needs, slug=None)

    def test_numbers_must_be_null_or_non_negative_integers(self):
        cases = [("cameras", True), ("cameras", -1), ("cameras", "42"), ("cameras", 4.0),
                 ("shared_with_agencies", False), ("hotlist_alerts", [1])]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.refused([{**NEW, key: value}], f"{LABEL}[0].{key}: must be null or a non-negative integer")
        self.refused([{**NEW, "retention_days": 4000}], f"{LABEL}[0].retention_days: at most 3650")
        self.write([{**NEW, "retention_days": 3650, "cameras": 0}])
        self.build()
        self.assertIn("<li>Cameras: 0</li>", self.block("agencies.html"))

    def test_without_transparency_every_built_file_is_identical_to_a_build_without_this_code_path(self):
        self.write()
        self.build()
        plain = self.snapshot()
        with mock.patch.object(Site, "transparency_block", return_value=""):
            self.build()
        self.assertEqual(self.snapshot(), plain)  # the code path adds nothing when no snapshot exists
        self.write([])
        self.build()
        self.assertEqual(self.snapshot(), plain)
        self.path.write_text(json.dumps(self.base))  # the shipped example, untouched
        self.build()
        example = self.snapshot()
        with mock.patch.object(Site, "transparency_block", return_value=""):
            self.build()
        self.assertEqual(self.snapshot(), example)
        self.assertNotIn(b"agency-transparency", b"".join(example.values()))

    def test_card_js_never_carries_snapshots(self):
        self.write()
        self.build()
        plain = self.snapshot()
        self.write([NEW, OLD])
        self.build()
        listed = self.snapshot()
        for name in ("site-data.js", "agency-cards.js"):
            self.assertEqual(listed[name], plain[name], name)
            for needle in (b"4242", b"9100", b"2026-02-01", b"\"transparency\"", b"hotlist"):
                self.assertNotIn(needle, listed[name], name)

    def test_content_check_cli_lists_the_same_message_as_build(self):
        self.write([{**NEW, "cameras": -1}])
        message = f"{LABEL}[0].cameras: must be null or a non-negative integer"
        run = [sys.executable, "-B", "-m", "campaign_tool"]
        built = subprocess.run(run + ["build", "--directory", str(self.root)], cwd=REPO_ROOT,
                               capture_output=True, text=True, timeout=120)
        checked = subprocess.run(run + ["content-check", "--directory", str(self.root)], cwd=REPO_ROOT,
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(built.returncode, 1)
        self.assertIn("Stopped: " + message, built.stderr)
        self.assertEqual(checked.returncode, 3)
        self.assertIn(message, checked.stdout.splitlines())

    def test_default_wording_never_claims_verification(self):
        wording = " ".join(text for key, text in DEFAULTS.items() if key.startswith("agency.transparency_")).lower()
        self.assertIn("as reported by the vendor", wording)
        for word in ("verified", "confirmed", "fact"):
            self.assertNotIn(word, wording)


if __name__ == "__main__":
    unittest.main()
