"""DF1737-10: agency cards show where and when a verified contact was checked; verified:true without it stops the build."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_agencies, load_content
from campaign_tool.site import Site, build_site
from tests.test_site_build import EXAMPLE, REPO_ROOT
from tests.test_site_build_atomic import snapshot

TODAY = date.today()  # the build date: content checks run against the day the build runs
TOMORROW = (TODAY + timedelta(days=1)).isoformat()
SOURCE = "https://example.invalid/cedar-police/contact?page=records&lang=en"
HINT = "Contact details unverified"
CHECK = {"source_url": SOURCE, "checked_on": "2026-10-01", "fields": ["records_url"]}


def article(html, slug):
    found = re.search(rf'<article id="{re.escape(slug)}">(.*?)</article>', html, re.S)
    assert found, f"no article for {slug}"
    return found.group(1)


class ContactProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-provenance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content_path = self.root / "content" / "agencies.json"
        self.agencies = json.loads(self.content_path.read_text())

    def write(self, kit=False):
        if kit:
            (self.root / "kit").mkdir(exist_ok=True)
            (self.root / "kit" / "agencies.json").write_text(json.dumps(self.agencies))
            if self.content_path.exists():
                self.content_path.unlink()
        else:
            self.content_path.write_text(json.dumps(self.agencies))

    def cli_build(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), *extra],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def agencies_page(self):
        build_site(self.root, today=TODAY)
        return (self.root / "public" / "agencies.html").read_text(encoding="utf-8")

    def assert_refused(self, field, kit=False):
        public = self.root / "public"
        before = snapshot(public) if public.exists() else None
        self.write(kit)
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        self.assertIn(field, str(caught.exception))
        with self.assertRaises(ContentError):
            build_site(self.root, today=TODAY)
        self.assertEqual(snapshot(public) if public.exists() else None, before)

    def test_verified_agency_shows_checked_date_and_escaped_source_link(self):
        self.agencies[1].update(verified=True, verification=dict(CHECK))
        self.write()
        html = self.agencies_page()
        card = article(html, "cedar")
        self.assertIn('<p class="contact-provenance">Contact checked 2026-10-01 on <a href="https://example.invalid/'
                      'cedar-police/contact?page=records&amp;lang=en" rel="noopener noreferrer">the official source</a>.</p>',
                      card)
        self.assertNotIn(HINT, card)
        sibling = article(html, "cedar-county-sheriff")
        self.assertIn(HINT, sibling)
        self.assertNotIn("Contact checked", sibling)

    def test_checked_on_the_build_date_is_accepted(self):
        self.agencies[1].update(verified=True, verification={"source_url": SOURCE, "checked_on": TODAY.isoformat()})
        self.write()
        self.assertIn(f"Contact checked {TODAY.isoformat()} on", article(self.agencies_page(), "cedar"))
        entry = {"agency_id": "ca-example-police", "name": "Example Police (fictional)", "verified": True,
                 "verification": {"source_url": SOURCE, "checked_on": "2026-10-02"}}
        self.assertEqual(load_agencies([entry], "agencies", today=date(2026, 10, 2))[0]["verification"],
                         {"source_url": SOURCE, "checked_on": "2026-10-02", "fields": []})
        with self.assertRaisesRegex(ContentError, r"^agencies\[0\]\.verification\.checked_on: "):
            load_agencies([entry], "agencies", today=date(2026, 10, 1))

    def test_kit_verified_without_verification_stops_cli_build_and_keeps_old_tree(self):
        self.write(kit=True)
        self.assertEqual(self.cli_build().returncode, 0)
        (self.root / "public" / "marker.txt").write_text("previous site\n")
        before = snapshot(self.root / "public")
        self.agencies[0]["verified"] = True
        self.write(kit=True)
        result = self.cli_build()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: kit/agencies.json[0].verification: verified:true needs source_url and checked_on",
                      result.stderr)
        self.assertEqual(snapshot(self.root / "public"), before)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public.")], [])

    def test_kit_verified_with_verification_renders(self):
        self.agencies[1].update(verified=True, verification=dict(CHECK))
        self.write(kit=True)
        self.assertIn("Contact checked 2026-10-01 on", article(self.agencies_page(), "cedar"))

    def test_invalid_verification_is_refused_naming_the_field(self):
        field = "content/agencies.json[1].verification"
        cases = {
            "http source": ({"source_url": "http://example.invalid/contact"}, f"{field}.source_url"),
            "javascript source": ({"source_url": "javascript:alert(1)"}, f"{field}.source_url"),
            "quote and angle source": ({"source_url": 'https://example.invalid/a"><script>'}, f"{field}.source_url"),
            "empty source": ({"source_url": ""}, f"{field}.source_url"),
            "impossible date": ({"checked_on": "2026-02-30"}, f"{field}.checked_on"),
            "after build date": ({"checked_on": TOMORROW}, f"{field}.checked_on"),
            "short date": ({"checked_on": "2026-1-5"}, f"{field}.checked_on"),
            "datetime": ({"checked_on": "2026-10-01T10:00:00Z"}, f"{field}.checked_on"),
            "non-string date": ({"checked_on": 20261001}, f"{field}.checked_on"),
            "unknown key": ({"checked_by": "organizer"}, f"{field}: unknown field(s) ['checked_by']"),
            "phone field": ({"fields": ["records_email", "phone"]}, f"{field}.fields"),
            "fields not a list": ({"fields": "records_email"}, f"{field}.fields"),
        }
        for name, (change, expected) in cases.items():
            with self.subTest(name):
                self.agencies[1].update(verified=True, verification={**CHECK, **change})
                self.assert_refused(expected)

    def test_missing_or_null_parts_are_refused(self):
        for name, verification, expected in (
                ("no source", {"checked_on": "2026-10-01"}, "missing field(s) ['source_url']"),
                ("no date", {"source_url": SOURCE}, "missing field(s) ['checked_on']"),
                ("null", None, "content/agencies.json[1].verification: expected an object"),
                ("list", [SOURCE, "2026-10-01"], "content/agencies.json[1].verification: expected an object")):
            with self.subTest(name):
                self.agencies[1].update(verified=True, verification=verification)
                self.assert_refused(expected)
        with self.subTest("verified without verification"):
            self.agencies[1].pop("verification")
            self.assert_refused("content/agencies.json[1].verification: verified:true needs source_url and checked_on")

    def test_verification_without_verified_true_is_validated_but_not_shown(self):
        for verified in (False, "true", 1):
            with self.subTest(verified=verified):
                self.agencies[1].update(verified=verified, verification=dict(CHECK))
                self.write()
                card = article(self.agencies_page(), "cedar")
                self.assertNotIn("Contact checked", card)
                self.assertIn(HINT, card)
        self.agencies[1].update(verified=False, verification={**CHECK, "source_url": "http://example.invalid/"})
        self.assert_refused("content/agencies.json[1].verification.source_url")

    def test_renderer_escapes_quote_and_angle_in_source_url(self):
        self.agencies[1].update(verified=True, verification=dict(CHECK))
        self.write()
        model = load_content(self.root)
        assert model is not None
        model["agencies"][1]["verification"]["source_url"] = 'https://example.invalid/a"><script>x</script>'
        html = Site(self.root, read_config(self.root), model, today=TODAY).agencies_html()["html"]
        card = article(html, "cedar")
        self.assertNotIn("<script>", card)
        self.assertIn('href="https://example.invalid/a&quot;&gt;&lt;script&gt;x&lt;/script&gt;"', card)

    def test_check_leak_scan_passes_with_provenance(self):
        self.agencies[1].update(verified=True, verification=dict(CHECK))
        self.write()
        result = self.cli_build("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public tree check passed", result.stdout)
        self.assertIn("Contact checked", (self.root / "public" / "agencies.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
