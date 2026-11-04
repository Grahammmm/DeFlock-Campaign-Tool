"""DF0137-07: meetings-outcome --votes FILE records organizer-verified per-member votes.

Synthetic names, .example/.invalid hosts and temp copies of the fictional example only; nothing is fetched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.content import ContentError, load_content
from campaign_tool.meeting_outcome import MeetingOutcomeError, record_outcome
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "cedar-council-2026-10"  # starts_at 2026-10-14, agency_id ca-cedar-city-council
COUNCIL = "ca-cedar-city-council"
TODAY = date(2026, 10, 20)
SOURCE = "https://example.invalid/cedar-council/minutes"
NAMES = ["Ash Example", "Birch Example", "Cedar Example", "Dale Example", "Zo\u00eb Example"]
FIVE = "name,vote\nAsh Example,yes\nBirch Example,no\nCedar Example,abstain\nDale Example,absent\nZo\u00eb Example,recused\n"
LINE = ('<p class="meeting-outcome">Outcome: Approved on <time datetime="2026-10-14">2026-10-14</time>'
        f' · <a href="{SOURCE}" rel="noopener noreferrer">Official record</a> · verified by Synthetic Organizer</p>')
PENDING = '<p class="meeting-minutes">Recorded by the campaign; minutes not yet approved</p>'  # DF2207-05


def official(name):
    return {"name": name, "role": "Council member", "source_url": "https://cedar.example/council",
            "checked_on": "2026-09-01"}


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


class MeetingVotesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-votes-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.meeting = self.content / "meetings" / "2026-10-cedar-council.json"
        self.officials(NAMES)

    def officials(self, names, agency=COUNCIL):
        (self.content / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [
            {"agency_id": agency, "officials": [official(name) for name in names]}]}), encoding="utf-8")

    def csv(self, text, name="votes.csv"):
        path = self.tmp / name
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    def record(self, votes=None, meeting_id=MID, vote=None, replace=False, on="2026-10-14"):
        return record_outcome(self.root, meeting_id, "approved", on, SOURCE, "Synthetic Organizer", vote, replace,
                              today=TODAY, votes_file=votes)

    def refused(self, needles, path=None, **kwargs):
        path = path or self.meeting
        before = path.read_bytes()
        with self.assertRaises(MeetingOutcomeError) as caught:
            self.record(**kwargs)
        for needle in needles:
            self.assertIn(needle, str(caught.exception))
        self.assertEqual(path.read_bytes(), before)
        return str(caught.exception)

    def cli(self, *extra):
        return run("meetings-outcome", "--directory", str(self.root), "--id", MID, "--result", "approved",
                   "--on", "2026-10-14", "--source", SOURCE, "--verified-by", "Synthetic Organizer", *extra)

    def page(self):
        return (self.root / "public" / "meetings" / f"{MID}.html").read_text(encoding="utf-8")

    def stored(self):
        return json.loads(self.meeting.read_text(encoding="utf-8"))["outcome"]

    # 1. five rows, each value once: stored in file order; page table after the outcome line; meetings list unchanged
    def test_five_votes_stored_in_order_and_shown_on_meeting_page_only(self):
        self.record()
        build_site(self.root, today=TODAY)
        listing = (self.root / "public" / "meetings.html").read_bytes()
        self.assertNotIn("recorded-votes", self.page())
        result = self.cli("--votes", str(self.csv(FIVE)), "--replace")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("votes: none", result.stdout)
        expected = [{"name": n, "vote": v} for n, v in zip(NAMES, ("yes", "no", "abstain", "absent", "recused"))]
        self.assertEqual(json.loads(result.stdout.rsplit("}", 1)[0] + "}")["votes"], expected)
        self.assertEqual(self.stored()["votes"], expected)
        self.assertEqual([m for m in load_content(self.root)["meetings"] if m["id"] == MID][0]["outcome"]["votes"], expected)
        build_site(self.root, today=TODAY)
        rows = "".join(f"<tr><td>{n}</td><td>{label}</td></tr>"
                       for n, label in zip(NAMES, ("Yes", "No", "Abstained", "Absent", "Recused")))
        table = ('<h3>Recorded votes</h3><table class="recorded-votes"><thead><tr><th scope="col">Member</th>'
                 f'<th scope="col">Vote</th></tr></thead><tbody>{rows}</tbody></table><p class="notice">'
                 f'From the official record: <a href="{SOURCE}" rel="noopener noreferrer">{SOURCE}</a></p>')
        self.assertEqual(self.page().count(LINE + PENDING + table), 1)
        self.assertEqual((self.root / "public" / "meetings.html").read_bytes(), listing)

    def test_names_match_after_nfc_and_whitespace_collapse_and_store_the_official_name(self):
        decomposed = unicodedata.normalize("NFD", "Zo\u00eb Example")
        self.record(self.csv(f"name,vote\n  Ash   Example ,YES\n{decomposed},No\n"))
        self.assertEqual(self.stored()["votes"], [{"name": "Ash Example", "vote": "yes"},
                                                  {"name": "Zo\u00eb Example", "vote": "no"}])

    # 2. every row problem reported together by row number; the meeting file is byte-identical
    def test_row_problems_are_listed_together_and_nothing_is_written(self):
        text = ("Ash Example,yes\nNobody Example,yes\nash example,no\nBirch Example,yes\nBirch Example,no\n"
                "Cedar Example,maybe\nDale Example,yes,extra\n")
        message = self.refused(['votes: header must be name,vote',
                                'votes row 2: unknown name "Nobody Example" (not in officials.json for ca-cedar-city-council)',
                                'votes row 3: unknown name "ash example"', 'votes row 5: name listed twice',
                                'votes row 6: vote must be one of yes, no, abstain, absent, recused',
                                'votes row 7: must have exactly two columns'], votes=self.csv(text))
        self.assertNotIn("row 4", message)
        result = self.cli("--votes", str(self.csv(text)))
        self.assertEqual(result.returncode, 1)
        self.assertIn("votes row 5: name listed twice", result.stderr)
        self.assertIn("votes row 7:", result.stderr)
        self.refused(["votes: must list 1 to 30 rows"], votes=self.csv("name,vote\n"))
        many = "name,vote\n" + "Ash Example,yes\n" * 31
        self.refused(["votes: must list 1 to 30 rows"], votes=self.csv(many))

    # 3. no agency_id, or an agency without an officials body: the named refusal, nothing written
    def test_meeting_without_officials_body_is_refused(self):
        needle = "--votes needs the meeting's agency_id to have a content/officials.json body; nothing was recorded"
        document = json.loads(self.meeting.read_text(encoding="utf-8"))
        del document["agency_id"]
        self.meeting.write_text(json.dumps(document), encoding="utf-8")
        self.refused([needle], votes=self.csv(FIVE))
        document["agency_id"] = COUNCIL
        self.meeting.write_text(json.dumps(document), encoding="utf-8")
        self.officials(NAMES, agency="ca-cedar-police")
        self.refused([needle], votes=self.csv(FIVE))
        (self.content / "officials.json").unlink()
        self.refused([needle], votes=self.csv(FIVE))

    # 4. --replace rules as today; --replace without --votes drops the stored votes and says so
    def test_replace_rules(self):
        self.record(self.csv(FIVE), vote="3-1")
        self.refused(["already has an outcome (approved)"], votes=self.csv("name,vote\nAsh Example,no\n"))
        self.record(self.csv("name,vote\nAsh Example,no\n"), replace=True)
        self.assertEqual(self.stored(), {"result": "approved", "on": "2026-10-14", "source_url": SOURCE,
                                         "verified_by": "Synthetic Organizer", "verified_at": "2026-10-20",
                                         "votes": [{"name": "Ash Example", "vote": "no"}]})
        result = self.cli("--replace")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.rsplit("}", 1)[1].split(), ["votes:", "none", "Recorded", "only.", "Nothing",
                                                                   "was", "published."])
        self.assertNotIn("votes", self.stored())
        build_site(self.root, today=TODAY)
        self.assertNotIn("recorded-votes", self.page())

    # 5. an officials edit dropping a voter stops the build; restoring the member builds again
    def test_officials_edit_removing_a_voter_stops_the_build(self):
        self.record(self.csv(FIVE))
        self.officials(NAMES[:2] + NAMES[3:])
        with self.assertRaises(ContentError) as caught:
            build_site(self.root, today=TODAY)
        self.assertEqual(str(caught.exception), "meetings/2026-10-cedar-council.json.outcome.votes[2].name: "
                                                "not in officials.json for ca-cedar-city-council")
        self.officials(NAMES)
        build_site(self.root, today=TODAY)
        self.assertIn("<td>Cedar Example</td><td>Abstained</td>", self.page())

    # 6. symlink, directory, 65 KiB and invalid UTF-8 are refused before the meeting is read (unknown id unseen)
    def test_bad_files_refused_before_the_meeting_is_read(self):
        link = self.tmp / "link.csv"
        link.symlink_to(self.csv(FIVE))
        directory = self.tmp / "folder.csv"
        directory.mkdir()
        big = self.csv("name,vote\n" + "Ash Example,yes\n" * 4200, "big.csv")
        self.assertGreater(big.stat().st_size, 64 * 1024)
        for path, needle in ((link, "votes: cannot read"), (directory, "votes: must be a regular file"),
                             (big, "votes: must be a regular file of at most 65536 bytes"),
                             (self.csv(b"name,vote\nAsh \xff,yes\n", "bad.csv"), "votes: not a valid UTF-8 CSV")):
            with self.subTest(path=path.name):
                message = self.refused([needle], votes=path, meeting_id="synthetic-missing")
                self.assertNotIn("no staged meeting", message)

    # 7. & " ' render escaped (< is refused by officials.json, so such a row can never match); future meeting refused
    def test_escaping_and_future_meeting(self):
        self.officials(['Rowan "Ro" O\'Example & Co'])
        self.record(self.csv('name,vote\n"Rowan ""Ro"" O\'Example & Co",yes\n'))
        build_site(self.root, today=TODAY)
        self.assertIn("<td>Rowan &quot;Ro&quot; O&#x27;Example &amp; Co</td><td>Yes</td>", self.page())
        self.refused(['votes row 2: unknown name "<b>Rowan</b>"'], votes=self.csv("name,vote\n<b>Rowan</b>,yes\n"),
                     replace=True)
        later = self.content / "meetings" / "synthetic-later.json"
        later.write_text(json.dumps({"id": "synthetic-later", "body": "Synthetic board (fictional)", "agency_id": COUNCIL,
                                     "starts_at": "2026-10-25T18:00:00-07:00"}), encoding="utf-8")
        self.refused(["has not happened yet"], later, votes=self.csv(FIVE), meeting_id="synthetic-later",
                     on="2026-10-25")

    # labels: every new string comes from Site.t(), so labels.json replaces each one
    def test_votes_labels_replaceable(self):
        self.record(self.csv(FIVE))
        keys = ["meeting.votes", "meeting.votes_member", "meeting.votes_vote", "meeting.votes_source",
                "vote.yes", "vote.no", "vote.abstain", "vote.absent", "vote.recused"]
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            key: f"X{key}X" for key in keys}}), encoding="utf-8")
        build_site(self.root, today=TODAY)
        html = self.page()
        self.assertTrue(all(f"X{key}X" in html for key in keys))
        self.assertNotIn("Recorded votes", html)

    # 8. stored shape checked on load; a site without votes has no votes markup
    def test_stored_votes_shape_is_validated(self):
        base = {"result": "approved", "on": "2026-10-14", "source_url": SOURCE, "verified_by": "Synthetic Organizer",
                "verified_at": "2026-10-20"}
        document = json.loads(self.meeting.read_text(encoding="utf-8"))
        cases = (([], "votes: must list 1 to 30 votes"), ("Ash Example", "votes: must list 1 to 30 votes"),
                 ([{"name": "Ash Example", "vote": "yes"}] * 31, "votes: must list 1 to 30 votes"),
                 ([{"name": "Ash Example", "vote": "yes", "x": 1}], "votes[0]"),
                 ([{"name": "Ash Example", "vote": "maybe"}], "votes[0].vote: must be one of yes"),
                 ([{"name": "Ash Example", "vote": "yes"}, {"name": "Ash Example", "vote": "no"}],
                  "votes[1].name: listed twice"),
                 ([{"name": "<b>Ash</b>", "vote": "yes"}], "votes[0].name: must be plain text"),
                 ([{"name": "A" * 121, "vote": "yes"}], "votes[0].name: at most 120"))
        for votes, needle in cases:
            with self.subTest(needle=needle):
                self.meeting.write_text(json.dumps({**document, "outcome": {**base, "votes": votes}}), encoding="utf-8")
                with self.assertRaises(ContentError) as caught:
                    load_content(self.root)
                self.assertIn(needle, str(caught.exception))
        self.meeting.write_text(json.dumps({**document, "outcome": base}), encoding="utf-8")
        build_site(self.root, today=TODAY)
        self.assertFalse(any("recorded-votes" in p.read_text(encoding="utf-8")
                             for p in (self.root / "public").rglob("*.html")))
        self.assertTrue(os.path.isfile(self.root / "public" / "meetings" / f"{MID}.html"))


if __name__ == "__main__":
    unittest.main()
