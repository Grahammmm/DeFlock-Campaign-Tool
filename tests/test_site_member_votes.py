"""DF0137-08: agencies/<slug>.html "How members voted", built from a temp copy of the fictional example.

Synthetic names and .invalid hosts only; nothing is fetched. Meeting files are written directly so the page rule is
proved on its own, not through the recording command.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 10, 20)
COUNCIL = "ca-cedar-city-council"
PAGE = "agencies/cedar-city-council.html"
ASH, BIRCH, CEDAR = "Ash Example", "Birch Example", "Cedar Example"
SRC = "https://example.invalid/records/"


def official(name, profile=None):
    entry = {"name": name, "role": "Council member", "source_url": "https://cedar.example/council",
             "checked_on": "2026-09-01"}
    return {**entry, "profile_url": profile} if profile else entry


class MemberVotesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-member-votes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        sources = json.loads((self.content / "sources.json").read_text(encoding="utf-8"))
        sources.append({"sha256": "d00d" * 16, "title": "Synthetic council roster", "agency_id": COUNCIL,
                        "received_at": "2026-02-01T10:00:00+00:00", "public_url": None,
                        "fragment": "cedar-council-records", "pages": 1})  # gives the council its agency page
        (self.content / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
        self.officials([official(ASH), official(BIRCH), official(CEDAR)])

    def officials(self, people):
        (self.content / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [
            {"agency_id": COUNCIL, "officials": people}]}), encoding="utf-8")

    def meeting(self, mid, day, votes=None, source=None, status=None, body="Synthetic council", outcome=True):
        data = {"id": mid, "body": body, "agency_id": COUNCIL, "starts_at": f"{day}T18:00:00-07:00"}
        if status:
            data["status"] = status
        if outcome:
            data["outcome"] = {"result": "approved", "on": day, "source_url": source or f"{SRC}{mid}",
                               "verified_by": "Synthetic Organizer", "verified_at": day,
                               **({"votes": [{"name": n, "vote": v} for n, v in votes]} if votes else {})}
        (self.content / "meetings" / f"{mid}.json").write_text(json.dumps(data), encoding="utf-8")

    def page(self, today=TODAY):
        build_site(self.root, today=today)
        return (self.root / "public" / PAGE).read_text(encoding="utf-8")

    def member(self, html, name):
        """The HTML between this member's <h4> and the next <h4> or the end of the section."""
        start = html.index(f"<h4>{name}</h4>") if f"<h4>{name}</h4>" in html else html.index(f">{name}</a></h4>")
        end = html.index("</section>", start)
        following = html.find("<h4>", start + 4)
        return html[start:min(end, following) if following != -1 else end]

    # 1. newest first, one vote for the member missing from the second record, links to meeting page and record
    def test_two_meetings_newest_first_with_links(self):
        self.officials([official(ASH, "https://cedar.example/council/ash"), official(BIRCH), official(CEDAR)])
        self.meeting("synthetic-a", "2026-09-10", [(ASH, "yes"), (BIRCH, "no"), (CEDAR, "abstain")],
                     source=SRC + "september", body="Synthetic council A")
        self.meeting("synthetic-b", "2026-10-05", [(ASH, "no"), (BIRCH, "recused")], source=SRC + "october",
                     body="Synthetic council B")
        html = self.page()
        self.assertEqual(html.count('<section class="member-votes"><h3>How members voted</h3>'), 1)
        self.assertLess(html.index("who-decides"), html.index("member-votes"))
        self.assertLess(html.index("member-votes"), html.index("Published findings"))
        link = '<a href="/meetings/{}.html">{}, <time datetime="{}">{}</time></a> · <a href="{}" rel="noopener noreferrer">Official record</a></li>'
        newer = link.format("synthetic-b", "Synthetic council B", "2026-10-05", "2026-10-05", SRC + "october")
        older = link.format("synthetic-a", "Synthetic council A", "2026-09-10", "2026-09-10", SRC + "september")
        ash = self.member(html, "Ash Example")
        self.assertIn('<h4><a href="https://cedar.example/council/ash" rel="noopener noreferrer">Ash Example</a></h4>', html)
        self.assertEqual(ash.split("<ul>")[1], f"<li>No · {newer}<li>Yes · {older}</ul>")
        self.assertEqual(self.member(html, BIRCH).split("<ul>")[1], f"<li>Recused · {newer}<li>No · {older}</ul>")
        self.assertEqual(self.member(html, CEDAR).split("<ul>")[1], f"<li>Abstained · {older}</ul>")  # absent from record B
        self.assertTrue((self.root / "public" / "meetings" / "synthetic-a.html").is_file())

    # 2. a member with no vote; a body with officials but no votes has no section and an unchanged page
    def test_no_recorded_vote_and_no_section(self):
        before = self.page()
        self.meeting("synthetic-plain", "2026-09-10", outcome=False)
        self.meeting("synthetic-result-only", "2026-09-20")  # an outcome without votes
        self.assertEqual(self.page(), before)
        self.assertNotIn("member-votes", before)
        self.assertNotIn("How members voted", before)
        self.meeting("synthetic-a", "2026-09-10", [(ASH, "yes")])
        html = self.page()
        self.assertEqual(self.member(html, BIRCH), "<h4>Birch Example</h4><p>No recorded vote</p>")
        self.assertEqual(self.member(html, CEDAR), "<h4>Cedar Example</h4><p>No recorded vote</p>")
        self.assertEqual(html.count("<li>Yes · "), 1)

    def test_votes_of_another_body_are_not_shown(self):
        (self.content / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [
            {"agency_id": COUNCIL, "officials": [official(ASH)]},
            {"agency_id": "ca-cedar-police", "officials": [official("Dale Example")]}]}), encoding="utf-8")
        self.meeting("synthetic-a", "2026-09-10", [(ASH, "yes")])
        data = json.loads((self.content / "meetings" / "synthetic-a.json").read_text(encoding="utf-8"))
        data["agency_id"] = "ca-cedar-police"
        data["outcome"]["votes"] = [{"name": "Dale Example", "vote": "no"}]
        (self.content / "meetings" / "synthetic-a.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertNotIn("member-votes", self.page())  # the council page: the vote belongs to the police body
        police = (self.root / "public" / "agencies" / "cedar.html").read_text(encoding="utf-8")
        self.assertIn("<li>No · ", police)
        self.assertNotIn("Ash Example", police)

    # 3. a meeting not yet past is ignored (loads when held today); a cancelled one is ignored and cannot even load
    def test_future_and_cancelled_meetings_are_ignored(self):
        self.meeting("synthetic-today", "2026-10-20", [(ASH, "yes")])  # starts today: upcoming, not past
        self.assertNotIn("member-votes", self.page())
        self.assertIn("member-votes", self.page(today=TODAY + timedelta(days=1)))  # the next day it is past
        self.meeting("synthetic-gone", "2026-09-10", [(BIRCH, "no")], status="cancelled")
        with self.assertRaisesRegex(ContentError, "a cancelled meeting has no outcome"):
            load_content(self.root, TODAY)
        (self.content / "meetings" / "synthetic-gone.json").unlink()
        self.meeting("synthetic-held", "2026-09-10", [(ASH, "no")])
        model = load_content(self.root, TODAY)
        for item in model["meetings"]:  # the page rule alone, if a cancelled meeting ever carried votes
            if item["id"] == "synthetic-held":
                item["status"] = "cancelled"
        html = Site(self.root, read_config(self.root), model, today=TODAY).votes_section(COUNCIL)
        self.assertEqual(html, "")

    # 4. same-day meetings by id; more than 50 votes for one member
    def test_same_day_by_id_and_fifty_vote_cap(self):
        self.meeting("synthetic-b", "2026-09-10", [(ASH, "no")])
        self.meeting("synthetic-a", "2026-09-10", [(ASH, "yes")])
        ash = self.member(self.page(), ASH)
        self.assertLess(ash.index("/meetings/synthetic-a.html"), ash.index("/meetings/synthetic-b.html"))
        self.assertNotIn("earlier votes", ash)
        for i in range(51):
            self.meeting(f"synthetic-{i:02d}", (date(2026, 1, 1) + timedelta(days=i)).isoformat(), [(ASH, "yes")])
        (self.content / "meetings" / "synthetic-a.json").unlink()
        (self.content / "meetings" / "synthetic-b.json").unlink()
        ash = self.member(self.page(), ASH)
        self.assertEqual(ash.count("<li>"), 50)
        self.assertIn("/meetings/synthetic-50.html", ash)
        self.assertIn("/meetings/synthetic-01.html", ash)
        self.assertNotIn("/meetings/synthetic-00.html", ash)
        self.assertTrue(ash.endswith("</ul><p>\u2026 and 1 earlier votes on the meeting pages</p>"))

    # 5. escaping; the output passes build --check
    def test_escaping_and_build_check(self):
        name = 'Rowan "Ro" O\'Example & Co'
        self.officials([official(name)])
        self.meeting("synthetic-a", "2026-09-10", [(name, "yes")], source="https://example.invalid/r?a=1&b=2",
                     body="Synthetic Q&A board")
        html = self.page()
        self.assertIn("<h4>Rowan &quot;Ro&quot; O&#x27;Example &amp; Co</h4>", html)
        self.assertIn('href="https://example.invalid/r?a=1&amp;b=2" rel="noopener noreferrer">Official record</a>', html)
        self.assertIn("Synthetic Q&amp;A board, <time", html)
        self.assertNotIn("O'Example", html)
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root),
                                 "--check"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # 6. labels
    def test_labels_replaceable_and_defaults_used_without_them(self):
        for i in range(51):
            self.meeting(f"synthetic-{i:02d}", (date(2026, 1, 1) + timedelta(days=i)).isoformat(), [(ASH, "yes")])
        html = self.page()
        for text in ("How members voted", "No recorded vote", "Official record", "earlier votes on the meeting pages"):
            self.assertIn(text, html)
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "agency.votes_heading": "XheadingX", "agency.votes_none": "XnoneX", "agency.votes_source": "XsourceX",
            "agency.votes_more": "Xmore {count}X", "vote.yes": "XyesX"}}), encoding="utf-8")
        html = self.page()
        for text in ("XheadingX", "XnoneX", "XsourceX", "Xmore 1X", "XyesX"):
            self.assertIn(text, html)
        for text in ("How members voted", "No recorded vote", "Official record", "earlier votes", "Yes"):
            self.assertNotIn(text, html.split("member-votes")[1].split("</section>")[0])
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "agency.votes_more": "{other}"}}), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, "placeholder {other} is not allowed here"):
            load_content(self.root, TODAY)


if __name__ == "__main__":
    unittest.main()
