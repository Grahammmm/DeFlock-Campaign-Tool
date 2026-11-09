"""DF1207-07: an organizer-verified profile.pre_implementation_meeting records the public meeting found before an ALPR
program started (or that none has been found yet); the agency page shows it with its official record and never says a
meeting was missed or required. Offline, synthetic agencies, example.org URLs only.
"""
import copy
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import law
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.legal_claims import legal_claims
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)  # fixed site build date
HEADING = "<h4>Public meeting before the camera program</h4>"
RULE_ID = "ca-civ-1798.90.55-public-comment-before-implementation"
FORBIDDEN = ("compl", "violat", "unlawful", "illegal", "required meeting was")
LABEL = "profile.pre_implementation_meeting"
FOUND = {"status": "found", "checked_on": "2026-10-31", "meeting_on": "2026-10-20", "body": "Cedar City Council",
         "source_url": "https://example.org/cedar/minutes-2026-10-20.pdf"}
NOT_FOUND = {"status": "not_found_yet", "checked_on": "2026-10-31"}
ABSENT = object()


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


class PreMeetingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-pre-meeting-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.base = json.loads(self.path.read_text())
        self.police = next(a for a in self.base if a["agency_id"] == "ca-cedar-police")  # has sources, so an agency page
        self.label = f"content/agencies.json[{self.base.index(self.police)}].{LABEL}"

    def write(self, meeting=ABSENT):
        document = copy.deepcopy(self.base)
        entry = next(a for a in document if a["agency_id"] == "ca-cedar-police")
        if meeting is not ABSENT:
            entry.setdefault("profile", {})["pre_implementation_meeting"] = meeting
        self.path.write_text(json.dumps(document))

    def build(self):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=TODAY)
        self.assertEqual(urlopen.call_count, 0)

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def section(self, page):
        return '<section class="pre-meeting"' + page.split('<section class="pre-meeting"', 1)[1].split("</section>", 1)[0] + "</section>"

    def refusal(self, meeting):  # the build's first refusal is also a content-check line
        self.write(meeting)
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn(self.label, str(caught.exception))
        self.assertIn(str(caught.exception), content_problems(self.root))  # content-check names the same text
        return str(caught.exception)

    def problems(self, meeting):
        self.write(meeting)
        lines = content_problems(self.root)
        assert lines is not None
        return "\n".join(lines)

    # 1
    def test_found_meeting_shows_body_date_official_record_and_law_status(self):
        self.write(dict(FOUND, note="Item 7 on the consent calendar."))
        self.build()
        package = law.load_package("us-ca")
        citation = next(r["citation"] for r in package["rules"] if r["rule_id"] == RULE_ID)
        self.assertEqual(citation, "Civ. Code § 1798.90.55(a)")
        expected = (f'<section class="pre-meeting">{HEADING}<p>Cedar City Council discussed it on 2026-10-20 '
                    '(<a href="https://example.org/cedar/minutes-2026-10-20.pdf" rel="noopener noreferrer">official record</a>). '
                    'Checked 2026-10-31.</p><p>Rule: Civ. Code § 1798.90.55(a). Law package: draft, not legally reviewed.</p>'
                    '<p>Item 7 on the consent calendar.</p></section>')
        for name in ("agencies/cedar.html", "agencies.html"):
            self.assertEqual(self.read(name).count('class="pre-meeting"'), 1, name)
            self.assertEqual(self.section(self.read(name)), expected, name)

    # 2
    def test_found_needs_an_https_source_and_the_other_found_fields(self):
        no_source = {k: v for k, v in FOUND.items() if k != "source_url"}
        self.assertIn(f"{self.label}.source_url: required when status is found", self.refusal(no_source))
        self.assertIn(f"{self.label}.source_url: must be an https:// URL",
                      self.refusal(dict(FOUND, source_url="http://example.org/minutes.pdf")))
        self.assertIn(f"{self.label}.source_url: must be an https:// URL", self.refusal(dict(FOUND, source_url=None)))
        for key in ("meeting_on", "body"):
            with self.subTest(key=key):
                self.assertIn(f"{self.label}.{key}: required when status is found",
                              self.refusal({k: v for k, v in FOUND.items() if k != key}))
        self.assertIn(f"{self.label}.body: 1-120 characters", self.refusal(dict(FOUND, body="")))
        self.assertIn(f"{self.label}.body: 1-120 characters", self.refusal(dict(FOUND, body="x" * 121)))
        self.write(dict(FOUND, body="x" * 120))
        self.build()

    # 3
    def test_dates_must_be_real_not_in_the_future_and_in_order(self):
        future = "2999-01-01"  # content-check reads the real clock, the build the fixed TODAY: far-off dates fail in both
        self.assertIn(f"{self.label}.meeting_on: must not be after the build date", self.refusal(dict(FOUND, meeting_on=future)))
        self.assertIn(f"{self.label}.checked_on: must not be after the build date", self.refusal(dict(FOUND, checked_on=future)))
        self.assertIn(f"{self.label}.checked_on: must not be after the build date", self.refusal(dict(NOT_FOUND, checked_on=future)))
        self.assertIn(f"{self.label}.checked_on: must not be before meeting_on",
                      self.refusal(dict(FOUND, meeting_on="2026-10-25", checked_on="2026-10-24")))
        self.assertIn(f"{self.label}.meeting_on", self.refusal(dict(FOUND, meeting_on="2026-02-30")))
        self.assertIn(f"{self.label}.checked_on: required", self.refusal({"status": "not_found_yet"}))
        self.write(dict(FOUND, meeting_on="2026-11-02", checked_on="2026-11-02"))  # the day after the build date
        with self.assertRaisesRegex(ContentError, r"pre_implementation_meeting\.meeting_on: must not be after the build date"):
            self.build()
        self.write(dict(FOUND, meeting_on="2026-10-31", checked_on="2026-10-31"))  # same day is allowed
        self.build()
        self.write(dict(NOT_FOUND, checked_on=TODAY.isoformat()))  # the build date itself is allowed
        self.build()

    # 4
    def test_not_found_yet_allows_no_meeting_fields_and_says_so_plainly(self):
        for key, value in (("source_url", FOUND["source_url"]), ("body", "Cedar City Council"), ("meeting_on", "2026-10-20")):
            with self.subTest(key=key):
                self.assertIn(f"{self.label}.{key}: not allowed when status is not_found_yet",
                              self.refusal(dict(NOT_FOUND, **{key: value})))
        self.write(NOT_FOUND)
        self.build()
        for name in ("agencies/cedar.html", "agencies.html"):
            html = self.section(self.read(name))
            self.assertIn(HEADING + "<p>We have not found a record of one yet (checked 2026-10-31).</p>", html)
            self.assertNotIn("official record", html)
            self.assertNotIn("<a ", html)
            self.assertIn("<p>Rule: Civ. Code § 1798.90.55(a). Law package: draft, not legally reviewed.</p>", html)

    # 5
    def test_unknown_key_bad_status_and_long_note_are_refused_and_content_check_lists_all(self):
        self.assertIn(f"{self.label}: unknown field 'verdict'", self.refusal(dict(FOUND, verdict="held")))
        self.assertIn(f"{self.label}.status: choose one of", self.refusal(dict(FOUND, status="held")))
        self.assertIn(f"{self.label}.status: choose one of", self.refusal({k: v for k, v in FOUND.items() if k != "status"}))
        self.assertIn(f"{self.label}.note: at most 200 characters", self.refusal(dict(FOUND, note="n" * 201)))
        self.assertIn(f"{self.label}.note: must be plain text", self.refusal(dict(FOUND, note="see <b>this</b>")))
        self.assertIn(f"{self.label}: must be an object", self.refusal("found"))
        self.write(dict(FOUND, note="n" * 200))
        self.build()
        everything = self.problems({"status": "found", "checked_on": "2999-01-01", "extra": 1, "note": "n" * 201,
                                    "source_url": "http://example.org/x"})
        for fragment in (": unknown field 'extra'", ".checked_on: must not be after the build date",
                         ".meeting_on: required when status is found", ".body: required when status is found",
                         ".source_url: must be an https:// URL", ".note: at most 200 characters"):
            self.assertIn(self.label + fragment, everything)

    # 6
    def test_a_package_without_the_rule_shows_the_section_without_the_rule_line(self):
        base = self.tmp / "jurisdictions"
        (base / "us-ca").mkdir(parents=True)
        package = json.loads((REPO_ROOT / "jurisdictions" / "us-ca" / "package.json").read_text())
        package["rules"] = [r for r in package["rules"] if r["rule_id"] != RULE_ID]
        (base / "us-ca" / "package.json").write_text(json.dumps(package))
        self.write(FOUND)
        with mock.patch.object(law, "JURISDICTIONS", base):
            self.build()
        for name in ("agencies/cedar.html", "agencies.html"):
            html = self.section(self.read(name))
            self.assertIn("Cedar City Council discussed it on 2026-10-20", html)
            self.assertNotIn("Rule:", html)
            self.assertNotIn("Law package", html)
            self.assertNotIn("1798.90.55", html)
        (base / "us-ca" / "package.json").write_text("{not json")  # an unreadable package is the same: no Rule line, no refusal
        with mock.patch.object(law, "JURISDICTIONS", base):
            self.build()
        self.assertNotIn("Rule:", self.read("agencies/cedar.html"))

    # 7
    def test_wording_guard_on_both_states_and_html_is_escaped(self):
        for meeting in (dict(FOUND, note="Agenda item only."), NOT_FOUND, dict(NOT_FOUND, note="Minutes not yet posted.")):
            with self.subTest(meeting=meeting):
                self.write(meeting)
                self.build()
                for name in ("agencies/cedar.html", "agencies.html"):
                    html = self.section(self.read(name))
                    self.assertEqual(legal_claims(re.sub(r"<[^>]+>", " ", html)), [])
                    for word in FORBIDDEN:
                        self.assertNotIn(word, html.lower())
                    self.assertNotIn("did not hold", html.lower())
        self.write(dict(FOUND, body="Board <script>alert(1)</script> & Co"))
        self.build()
        html = self.section(self.read("agencies/cedar.html"))
        self.assertIn("Board &lt;script&gt;alert(1)&lt;/script&gt; &amp; Co discussed it", html)
        self.assertNotIn("<script>", html)

    # 8
    def test_agency_without_the_field_changes_no_page(self):
        self.write()
        self.build()
        plain = self.snapshot()
        self.assertFalse([n for n, b in plain.items() if b"pre-meeting" in b or b"Public meeting before" in b])
        self.write(None)  # null reads as absent
        self.build()
        self.assertEqual(self.snapshot(), plain)
        self.write(FOUND)
        self.build()
        first = self.snapshot()
        changed = {n for n in plain.keys() | first.keys() if plain.get(n) != first.get(n)}
        self.assertEqual(changed, {"agencies.html", "agencies/cedar.html"})  # no card JS, open data, feed or other page
        for name, data in first.items():
            if name not in changed:
                self.assertNotIn(b"Public meeting before", data, name)
        self.build()
        self.assertEqual(self.snapshot(), first)  # two builds are byte-identical

    def test_content_check_skips_an_unselected_agency_and_a_clean_field_has_no_problem(self):
        self.write(FOUND)
        lines = content_problems(self.root)
        self.assertFalse([line for line in lines or [] if "pre_implementation_meeting" in line])
        document = json.loads(self.path.read_text())
        for entry in document:
            if entry["agency_id"] == "ca-cedar-police":
                entry["selected"] = False
                entry["profile"]["pre_implementation_meeting"] = {"status": "bogus"}
        self.path.write_text(json.dumps(document))
        self.assertFalse([line for line in content_problems(self.root) or [] if "pre_implementation_meeting" in line])


if __name__ == "__main__":
    unittest.main()
