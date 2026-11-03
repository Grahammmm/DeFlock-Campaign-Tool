"""DF1737-10: a verified agency shows where and when its contact was checked; verified:true without provenance stops the build."""
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
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SOURCE = "https://records.example.org/contact?a=1&b=2"
GOOD = {"source_url": SOURCE, "checked_on": "2026-01-15", "fields": ["records_email"]}
HINT = '<p class="archive-hint">Contact details unverified: confirm on the agency\'s official site.</p>'
LINE = ('<p class="contact-provenance">Contact checked 2026-01-15 on <a href="https://records.example.org/contact?a=1&amp;b=2" '
        'rel="noopener noreferrer">the official source</a>.</p>')


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


def agency(name, **fields):
    entry = {"agency_id": f"ca-example-{name}", "name": f"Example {name} (fictional)", "kind": "sheriff",
             "jurisdiction_name": "Example", "records_email": "records@example.org"}
    entry.update(fields)
    return entry


class ContactProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-contact-provenance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.base = json.loads(self.path.read_text())
        self.label = f"content/agencies.json[{len(self.base)}]"

    def write(self, *entries, path=None):
        target = path or self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(list(entries) + self.base if path else self.base + list(entries)))

    def build(self):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root)
        self.assertEqual(urlopen.call_count, 0)

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def article(self, slug):
        page = (self.public / "agencies.html").read_text(encoding="utf-8")
        return page.split(f'<article id="{slug}">', 1)[1].split("</article>", 1)[0]

    def assert_refused(self, entry, field, text=None):
        self.write(entry)
        before = self.snapshot()
        with self.assertRaises(ContentError) as caught:
            self.build()
        message = str(caught.exception)
        self.assertTrue(message.startswith(f"{self.label}{field}"), message)
        if text:
            self.assertIn(text, message)
        self.assertEqual(self.snapshot(), before)  # old public/ kept, nothing swapped

    def test_verified_agency_shows_checked_date_and_escaped_source_link(self):
        self.write(agency("police", verified=True, verification=GOOD), agency("council"))
        self.build()
        verified = self.article("example-police")
        self.assertEqual(verified.count(LINE), 1)
        self.assertNotIn(HINT, verified)
        self.assertNotIn("contact-provenance", self.article("example-council"))
        self.assertIn(HINT, self.article("example-council"))
        self.assertNotIn(SOURCE, (self.public / "agencies.html").read_text())  # raw & never reaches the HTML
        self.assertNotIn("records.example.org", (self.public / "site-data.js").read_text())  # card JS has no contact block

    def test_checked_today_and_fields_optional_are_accepted(self):
        today = date.today().isoformat()
        self.write(agency("police", verified=True, verification={"source_url": SOURCE, "checked_on": today}))
        self.build()
        self.assertIn(f"Contact checked {today} on", self.article("example-police"))

    def test_kit_verified_without_verification_stops_cli_build_and_keeps_public(self):
        self.path.unlink()  # no content/agencies.json: the kit list is used
        kit = self.root / "kit" / "agencies.json"
        self.write(agency("police", verified=True, verification=GOOD), path=kit)
        self.build()
        self.assertIn(LINE, self.article("example-police"))
        before = self.snapshot()
        for broken in ({"verified": True}, {"verified": True, "verification": None}):
            with self.subTest(broken=broken):
                self.write(agency("police", **broken), path=kit)
                result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root)],
                                        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Stopped: kit/agencies.json[0].verification: verified:true needs source_url and checked_on",
                              result.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse([p.name for p in self.root.iterdir() if p.name.startswith((".public.new-", ".public.old-"))])

    def test_content_verified_without_complete_verification_is_refused(self):
        self.write(agency("police", verified=True, verification=GOOD))
        self.build()
        self.assert_refused(agency("police", verified=True), ".verification: verified:true needs source_url and checked_on")
        for partial in ({"source_url": SOURCE}, {"checked_on": "2026-01-15"}, {}):
            with self.subTest(partial=partial):
                self.assert_refused(agency("police", verified=True, verification=partial), ".verification: missing field(s)")
        for wrong in ("https://records.example.org", [GOOD], True):
            with self.subTest(wrong=wrong):
                self.assert_refused(agency("police", verified=True, verification=wrong), ".verification: expected an object")

    def test_bad_source_url_is_refused_naming_the_field(self):
        self.build()
        for url in ("http://records.example.org/contact", "javascript:alert(1)", 'https://records.example.org/"<b>x',
                    "https://user:pw@records.example.org/", "", None, 7, " https://records.example.org/"):  # pragma: allowlist secret -- synthetic rejected URL
            with self.subTest(url=url):
                self.assert_refused(agency("police", verified=True, verification={**GOOD, "source_url": url}),
                                    ".verification.source_url")

    def test_bad_or_future_checked_on_is_refused_naming_the_field(self):
        self.build()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        for value in ("2026-02-30", tomorrow, "2999-01-01", "2026-1-5", "20260115", "2026-01-15T00:00:00", "", None, 20260115):
            with self.subTest(value=value):
                self.assert_refused(agency("police", verified=True, verification={**GOOD, "checked_on": value}),
                                    ".verification.checked_on")

    def test_unknown_key_and_unknown_or_duplicate_field_names_are_refused(self):
        self.build()
        self.assert_refused(agency("police", verified=True, verification={**GOOD, "phone": "555-0100"}),
                            ".verification: unknown field(s) ['phone']")
        for fields in (["phone"], ["records_email", "phone"], "records_email", ["portal", "portal"], [["portal"]],
                       [None], {"portal": True}):
            with self.subTest(fields=fields):
                self.assert_refused(agency("police", verified=True, verification={**GOOD, "fields": fields}),
                                    ".verification.fields")

    def test_verification_without_verified_true_is_validated_but_not_shown(self):
        for verified in (False, "true", 1, None):
            with self.subTest(verified=verified):
                self.write(agency("police", verified=verified, verification=GOOD))
                self.build()
                self.assertNotIn("contact-provenance", self.article("example-police"))
                self.assertNotIn("records.example.org", (self.public / "agencies.html").read_text())
                self.assertIn(HINT, self.article("example-police"))
        self.assert_refused(agency("police", verified=False, verification={**GOOD, "source_url": "http://x.example/"}),
                            ".verification.source_url")

    def test_cli_build_check_leak_scan_passes(self):
        self.write(agency("police", verified=True, verification=GOOD))
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public tree check passed", result.stdout)
        page = self.article("example-police")
        self.assertIn(LINE, page)
        self.assertNotIn("<", page.split('class="contact-provenance">', 1)[1].split("<a href=", 1)[1].split(">", 1)[0])


if __name__ == "__main__":
    unittest.main()
