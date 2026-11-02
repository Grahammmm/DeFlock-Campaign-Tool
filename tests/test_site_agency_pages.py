"""Per-agency pages agencies/<slug>.html (DF2237-08), built from a temp copy of the fictional example.

Synthetic content only; no network.
"""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from campaign_tool.content import ContentError
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BUILD_DAY = date(2026, 10, 20)
BASE = "https://cedar-records.example.invalid"
S = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
CEDAR = "agencies/cedar.html"
SHERIFF = "agencies/cedar-county-sheriff.html"


class AgencyPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-agency-pages-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.public = self.root / "public"

    def edit(self, name, change):
        path = self.content / name
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def agency(self, agency_id, **changes):
        self.edit("agencies.json", lambda d: [a.update(changes) for a in d if a["agency_id"] == agency_id])

    def add_meeting(self, meeting_id, starts_at, **extra):
        record = {"id": meeting_id, "body": f"Board {meeting_id} (fictional)", "starts_at": starts_at, **extra}
        (self.content / "meetings" / f"{meeting_id}.json").write_text(json.dumps(record), encoding="utf-8")

    def build(self):
        return {p.relative_to(self.public).as_posix() for p in build_site(self.root, today=BUILD_DAY)}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def main(self, name):
        """The page's <main> only: the shared footer also links the county findings feed."""
        return self.read(name).split('<main id="top">')[1].split("</main>")[0]

    def article(self, slug):
        match = re.search(rf'<article id="{slug}">(.*?)</article>', self.read("agencies.html"), flags=re.S)
        self.assertIsNotNone(match, slug)
        return match.group(1)

    def sitemap(self):
        return [loc.text or "" for loc in ET.fromstring(self.read("sitemap.xml")).iter(f"{S}loc")]

    # 1. findings and sources: page lists both, feed link; finding page, agencies.html and sitemap link to it
    def test_agency_with_findings_and_sources(self):
        written = self.build()
        self.assertEqual({w for w in written if w.startswith("agencies/")}, {CEDAR})
        html = self.read(CEDAR)
        self.assertIn("<title>Cedar Police Department (fictional) | ", html)
        self.assertIn("<h2>Cedar Police Department (fictional)</h2>", html)
        self.assertIn('<p class="eyebrow">City police</p>', html)
        self.assertIn("Whether any agreement governs data sharing is an open information gap.", html)
        self.assertIn('<span class="status-pill">Fictional example</span>', html)
        self.assertIn('<a href="https://example.invalid/cedar-police/records" rel="noopener noreferrer">', html)
        self.assertIn('<h3>Published findings</h3>', html)
        self.assertIn('href="/findings/cedar-policy-posted.html"', html)
        self.assertIn('<a href="/findings/agency/cedar.xml" type="application/atom+xml">Findings feed</a>', html)
        self.assertNotIn("No published findings.", html)
        for title, short in (("Synthetic Cedar Police ALPR policy", "cafecafecafe"),
                             ("Synthetic Cedar Police vendor agreement excerpt", "beefbeefbeef")):
            self.assertIn(f"<li><p>{title}</p><p class=\"record-no\">sha256 <code>{short}</code>", html)
        self.assertEqual(html.count('<a href="/sources.html#cedar-police-records">In the source library</a>'), 2)
        self.assertIn("<code>cafecafecafe</code> · received 2026-01-20 · ", html)
        self.assertIn('aria-current="page"', html)
        self.assertIn('<a href="/agencies/cedar.html">Agency page</a>', self.article("cedar"))
        finding = self.read("findings/cedar-policy-posted.html")
        self.assertIn('Agency: <a href="/agencies/cedar.html">Cedar Police Department (fictional)</a>', finding)
        self.assertNotIn("/agencies.html#cedar", finding)
        self.assertIn(BASE + "/agencies/cedar.html", self.sitemap())
        self.assertIn('<link rel="canonical" href="https://cedar-records.example.invalid/agencies/cedar.html">', html)

    # 2. only sources: no-findings notice and no feed link; neither: no file, no link, not in the sitemap
    def test_only_sources_and_neither(self):
        source = {"sha256": "ab" * 32, "title": "Synthetic sheriff contract", "agency_id": "ca-cedar-county-sheriff",
                  "received_at": "2026-02-01T10:00:00+00:00", "public_url": None, "fragment": "sheriff-records"}
        self.edit("sources.json", lambda d: d.append(source))
        written = self.build()
        self.assertEqual({w for w in written if w.startswith("agencies/")}, {CEDAR, SHERIFF})
        html = self.read(SHERIFF)
        self.assertIn("<h3>Published findings</h3><p>No published findings.</p>", html)
        self.assertNotIn("Findings feed", self.main(SHERIFF))
        self.assertNotIn("findings/agency", html)
        self.assertIn("sha256 <code>abababababab</code>", html)
        self.assertIn('<a href="/sources.html#sheriff-records">In the source library</a>', html)
        self.assertIn('<a href="/agencies/cedar-county-sheriff.html">Agency page</a>', self.article("cedar-county-sheriff"))
        self.assertNotIn("Agency page", self.article("cedar-city-council"))
        self.assertNotIn("/agencies/", self.article("cedar-city-council"))
        self.assertFalse((self.public / "agencies" / "cedar-city-council.html").exists())
        locs = self.sitemap()
        self.assertEqual(sorted(loc for loc in locs if "/agencies/" in loc),
                         [BASE + "/agencies/cedar-county-sheriff.html", BASE + "/agencies/cedar.html"])

    # 3. only this body's upcoming meetings: past, cancelled, other-agency and agency-less meetings are left out
    def test_upcoming_meetings_for_this_body_only(self):
        self.add_meeting("cedar-up", "2026-11-05T18:00:00-07:00", agency_id="ca-cedar-police", status="scheduled")
        self.add_meeting("cedar-today", "2026-10-20T18:00:00-07:00", agency_id="ca-cedar-police")
        self.add_meeting("cedar-past", "2026-10-01T18:00:00-07:00", agency_id="ca-cedar-police")
        self.add_meeting("cedar-cancelled", "2026-11-06T18:00:00-07:00", agency_id="ca-cedar-police", status="cancelled")
        self.add_meeting("sheriff-up", "2026-11-07T18:00:00-07:00", agency_id="ca-cedar-county-sheriff")
        self.add_meeting("no-agency", "2026-11-08T18:00:00-07:00")
        self.build()
        html = self.read(CEDAR)
        self.assertIn("Board cedar-up (fictional)", html)
        self.assertIn("Board cedar-today (fictional)", html)
        for other in ("cedar-past", "cedar-cancelled", "sheriff-up", "no-agency", "cedar-council-2026-10"):
            self.assertNotIn(f"Board {other} (fictional)", html)
            self.assertNotIn(f"/meetings/{other}.html", html)
        self.assertNotIn("No upcoming meetings are listed for this body.", html)
        self.assertLess(html.index("Board cedar-today"), html.index("Board cedar-up"))

    def test_no_upcoming_meetings_notice(self):
        self.build()
        html = self.read(CEDAR)
        self.assertIn("<h3>Upcoming meetings</h3>", html)
        self.assertIn("No upcoming meetings are listed for this body.", html)
        self.assertNotIn("Cedar City Council (fictional)", html)

    # 4. markup characters in name and summary are escaped once in <title>, <h2>, description and og tags
    def test_name_and_summary_escaped(self):
        self.agency("ca-cedar-police", name='Cedar & "Police" (fictional)')
        self.edit("agencies.json", lambda d: [a["profile"].update(summary="Policy & 'agreement' \"excerpt\"")
                                              for a in d if a["agency_id"] == "ca-cedar-police"])
        self.build()
        html = self.read(CEDAR)
        name, summary = "Cedar &amp; &quot;Police&quot; (fictional)", "Policy &amp; &#x27;agreement&#x27; &quot;excerpt&quot;"
        self.assertIn(f"<title>{name} | ", html)
        self.assertIn(f"<h2>{name}</h2>", html)
        self.assertIn(f'<meta property="og:title" content="{name}">', html)
        self.assertIn(f'<meta name="description" content="{summary}">', html)
        self.assertIn(f'<meta property="og:description" content="{summary}">', html)
        self.assertNotIn('Cedar & "Police"', html)
        self.assertNotIn("&amp;amp;", html)
        self.agency("ca-cedar-police", name="Cedar <b>Police</b>")
        with self.assertRaises(ContentError):  # the loader refuses angle brackets, so the page is never rendered
            self.build()
        self.assertNotIn("<b>", self.read(CEDAR))

    def test_description_falls_back_without_summary(self):
        self.edit("agencies.json", lambda d: [a["profile"].update(summary="")
                                              for a in d if a["agency_id"] == "ca-cedar-police"])
        self.build()
        self.assertIn('<meta name="description" content="Records and findings for Cedar Police Department (fictional)">',
                      self.read(CEDAR))

    # 5. a finding whose agency is not loaded: no agency line, no page
    def test_finding_with_unknown_agency(self):
        document = json.loads((self.content / "findings" / "cedar-policy-posted.json").read_text(encoding="utf-8"))
        document.update(slug="orphan", id="fnd_0000000000000009", agency_id="ca-nowhere-police", corrections=[])
        (self.content / "findings" / "orphan.json").write_text(json.dumps(document), encoding="utf-8")
        written = self.build()
        self.assertEqual({w for w in written if w.startswith("agencies/")}, {CEDAR})
        orphan = self.read("findings/orphan.html")
        self.assertNotIn("Agency:", orphan)
        self.assertNotIn("/agencies/", orphan)
        self.assertNotIn("ca-nowhere-police", self.read(CEDAR))

    # 6. public-assets/agencies/ collides with the generated folder; public/ untouched
    def test_public_assets_agencies_collision(self):
        self.build()
        before = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        asset = self.content / "public-assets" / "agencies" / "x.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"\x89PNG synthetic")
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn("public-assets/agencies/x.png", str(caught.exception))
        self.assertIn("collides with a generated path", str(caught.exception))
        after = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    # 7. unverified contact: hint shown; verified: hint absent
    def test_unverified_hint(self):
        hint = '<p class="archive-hint">Contact details unverified: confirm on the agency\'s official site.</p>'
        self.build()
        self.assertIn(hint, self.main(CEDAR))
        self.agency("ca-cedar-police", verified=True,
                    verification={"source_url": "https://example.invalid/cedar-police/contact", "checked_on": "2026-01-05"})
        self.build()
        self.assertNotIn("Contact details unverified", self.read(CEDAR))

    # 8. no qualifying agency: no agencies/ folder, no agency-page link anywhere, output unchanged by the feature
    def test_no_qualifying_agency_leaves_output_unchanged(self):
        (self.content / "findings" / "cedar-policy-posted.json").unlink()
        (self.content / "sources.json").write_text("[]", encoding="utf-8")
        written = self.build()
        self.assertFalse((self.public / "agencies").exists())
        self.assertFalse(any(w.startswith("agencies/") for w in written))
        built = {name: (self.public / name).read_bytes() for name in written}
        for name, data in built.items():
            self.assertNotIn(b"/agencies/", data, name)
            self.assertNotIn(b"Agency page", data, name)
        with mock.patch.object(Site, "agency_pages", return_value={}), \
                mock.patch.object(Site, "agency_html", side_effect=AssertionError("agency page rendered")):
            again = self.build()
        self.assertEqual(again, written)
        self.assertTrue(all((self.public / name).read_bytes() == built[name] for name in written))


if __name__ == "__main__":
    unittest.main()
