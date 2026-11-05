"""content/officials.json "Who decides" (DF2237-13), built from a temp copy of the fictional example.

Fictional names and .example hosts only; nothing is fetched.
"""
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.content import ContentError, load_content
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BUILD_DAY = date(2026, 10, 20)
COUNCIL = "ca-cedar-city-council"
COUNCIL_PAGE = "agencies/cedar-city-council.html"
COUNCIL_MEETING = "meetings/cedar-council-2026-10.html"
POLICE_PAGE = "agencies/cedar.html"


def official(name="Rowan Example", role="Council member, District 2", **extra):
    entry = {"name": name, "role": role, "profile_url": "https://cedar.example/council/rowan",
             "source_url": "https://cedar.example/council", "checked_on": "2026-09-01"}
    entry.update(extra)
    return {key: value for key, value in entry.items() if value is not None}


class OfficialsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-officials-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.public = self.root / "public"
        sources = json.loads((self.content / "sources.json").read_text(encoding="utf-8"))
        sources.append({"sha256": "d00d" * 16, "title": "Synthetic council roster", "agency_id": COUNCIL,
                        "received_at": "2026-02-01T10:00:00+00:00", "public_url": None,
                        "fragment": "cedar-council-records", "pages": 1})  # gives the council its agency page
        (self.content / "sources.json").write_text(json.dumps(sources), encoding="utf-8")

    def officials(self, bodies, schema_version=1):
        (self.content / "officials.json").write_text(
            json.dumps({"schema_version": schema_version, "bodies": bodies}), encoding="utf-8")

    def council(self, *people):
        self.officials([{"agency_id": COUNCIL, "officials": list(people)}])

    def build(self, today=BUILD_DAY):
        return build_site(self.root, today=today)

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def refused(self, message, today=BUILD_DAY):
        with self.assertRaises(ContentError) as caught:
            self.build(today)
        self.assertIn(message, str(caught.exception))

    # 1 + 6. two officials on both pages; & escaped (< is refused as non-plain text); unlinked without profile_url
    def test_who_decides_on_agency_and_meeting_pages(self):
        before = (self.build(), self.snapshot())[1]
        self.council(official(name="Rowan Example & Co", checked_on="2026-08-02"),
                     official(name="Sage Example", role="Mayor", profile_url=None, checked_on="2026-09-15",
                              source_url="https://cedar.example/council/roster"))
        self.build()
        section = ('<section class="who-decides"><h3>Who decides</h3><ul><li><a href="https://cedar.example/council/rowan" '
                   'rel="noopener noreferrer">Rowan Example &amp; Co</a>, Council member, District 2</li>'
                   '<li>Sage Example, Mayor</li></ul><p>Listed from the body\'s official site '
                   '(<a href="https://cedar.example/council/roster">source</a>), checked <time>2026-09-15</time>. '
                   'Contact officials only through the channels on that site.</p></section>')
        for page in (COUNCIL_PAGE, COUNCIL_MEETING):
            self.assertEqual(self.read(page).count(section), 1, page)
        after = self.snapshot()
        self.assertEqual(set(after) - set(before), {"data/officials.json", "data/officials.csv"})  # DF1622-11
        self.assertEqual(set(before) - set(after), set())
        changed = {name for name in before if after[name] != before[name]}
        self.assertEqual(changed, {COUNCIL_PAGE, COUNCIL_MEETING, "agencies.html"})
        self.assertNotIn("who-decides", self.read(POLICE_PAGE))

    # labels: every officials.* string comes from Site.t(), so labels.json replaces each one
    def test_officials_labels_replaceable(self):
        self.council(official())
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "officials.heading": "XheadX", "officials.source": "XsrcX",
            "officials.listed": "XlistX {source} XonX {date} XendX"}}), encoding="utf-8")
        self.build()
        html = self.read(COUNCIL_MEETING)
        self.assertIn('<h3>XheadX</h3>', html)
        self.assertIn('<p>XlistX <a href="https://cedar.example/council">XsrcX</a> XonX <time>2026-09-01</time> XendX</p>', html)
        self.assertNotIn("Who decides", html)

    # 5. no officials.json: model empty, no section anywhere, and removing the file restores every byte
    def test_absent_file_is_byte_identical(self):
        self.build()
        without = self.snapshot()
        self.assertEqual(load_content(self.root, BUILD_DAY)["officials"], {})
        self.assertFalse(any(b"who-decides" in data or b"Who decides" in data for data in without.values()))
        self.council(official())
        self.build()
        self.assertNotEqual(self.snapshot(), without)
        (self.content / "officials.json").unlink()
        self.build()
        self.assertEqual(self.snapshot(), without)

    # 2. contact fields and contact-like text
    def test_contact_details_refused(self):
        for key in ("email", "phone"):
            with self.subTest(key=key):
                self.council(official(**{key: "x"}))
                self.refused(f"unknown field(s) ['{key}']")
        for field, text in (("name", "Rowan rowan@cedar.example"), ("role", "Clerk (555) 123-4567"),
                            ("role", "Clerk 555.123.4567"), ("name", "Rowan 5551234567"), ("role", "Clerk at cedar dot example @")):
            with self.subTest(field=field, text=text):
                self.council(official(**{field: text}))
                self.refused(f"officials[0].{field}: no contact details in officials.json")
        self.council(official(role="Council member, District 12, seat 3"))  # short numbers are not contact details
        self.build()
        self.council(official(name="Rowan <b>Example</b>"))
        self.refused("must be plain text")

    # 3. checked_on in the future, 366 days old, malformed; 365 days old and today pass
    def test_checked_on_window(self):
        for value, message in (("2026-10-21", "checked_on: must not be after the build date"),
                               ("2025-10-19", "checked_on: older than 365 days; re-check on the official site"),
                               ("2026-9-01", "checked_on: "), ("2026-02-30", "checked_on: "), ("yesterday", "checked_on: ")):
            with self.subTest(value=value):
                self.council(official(checked_on=value))
                self.refused(message)
        for value in ("2025-10-20", "2026-10-20"):
            with self.subTest(value=value):
                self.council(official(checked_on=value))
                self.build()
        self.council(official(checked_on="2026-09-01"))
        self.refused("older than 365 days", today=date(2027, 9, 2))  # stale against a later build date

    # 4. unknown or duplicate agency_id; mailto:, tel:, http:// URLs
    def test_agency_and_url_refusals(self):
        self.officials([{"agency_id": "ca-nowhere", "officials": [official()]}])
        self.refused("officials.json.bodies[0].agency_id: not a loaded agency")
        self.officials([{"agency_id": COUNCIL, "officials": [official()]},
                        {"agency_id": COUNCIL, "officials": [official()]}])
        self.refused("officials.json.bodies[1].agency_id: listed more than once")
        for key, url in (("profile_url", "mailto:rowan@cedar.example"), ("profile_url", "tel:5551234567"),
                         ("source_url", "http://cedar.example/council"), ("source_url", None)):
            with self.subTest(key=key, url=url):
                self.council(official(**{key: url}))
                self.refused(f"officials[0].{key}" if url else "missing field(s) ['source_url']")
        self.officials([{"agency_id": COUNCIL, "officials": [official()]}], schema_version=2)
        self.refused("schema_version: must be 1")

    # 7. 31 officials refused, 30 accepted, an empty list refused
    def test_official_count(self):
        self.council(*[official(name=f"Member {chr(65 + n // 26)}{chr(65 + n % 26)}") for n in range(31)])
        self.refused("officials: must list 1 to 30 officials")
        self.council()
        self.refused("officials: must list 1 to 30 officials")
        self.council(*[official(name=f"Member {chr(65 + n)}") for n in range(30)])
        self.build()
        self.assertEqual(self.read(COUNCIL_PAGE).count("rel=\"noopener noreferrer\">Member "), 30)

    # 8. a refusal leaves the previous public/ byte-identical
    def test_refusal_keeps_previous_public(self):
        self.council(official())
        self.build()
        before = self.snapshot()
        self.council(official(checked_on="2024-01-01"))
        self.refused("older than 365 days")
        self.assertEqual(self.snapshot(), before)
        (self.content / "officials.json").write_text("{not json", encoding="utf-8")
        self.refused("officials.json: not valid UTF-8 JSON")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
