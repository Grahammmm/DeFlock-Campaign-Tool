"""DF1107-12: agency-brief writes a private printable pair kit/briefs/<slug>-<date>.html/.txt from published content only.

Synthetic data and .invalid hosts only; sockets are blocked. Run from the repository root:
python3 -B -m unittest tests.test_agency_brief -v
"""
import contextlib
import copy
import html as htmllib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool import agency_brief, cli, law
from campaign_tool.agency_brief import BriefError, build_brief, build_rows
from campaign_tool.content import ContentError, load_content
from tests.test_site_local_rules import rule, synthetic_package

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
POLICE, COUNCIL, OTHER, SHERIFF = "ca-cedar-police", "ca-cedar-city-council", "ca-cedar-other-council", "ca-cedar-county-sheriff"
SRC = "https://records.example.invalid/"
HTML, TXT = "cedar-2026-11-04.html", "cedar-2026-11-04.txt"
SENTINEL = "SYNTH-PRIVATE-SENTINEL"
VERIFIED = {"source_url": SRC + "verified", "checked_on": "2026-10-01"}


def tree(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(Path(folder).rglob("*")) if p.is_file()}


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def vote_meeting(mid, body, agency, start, votes=None, status=None, outcome=True, **more):
    data = {"id": mid, "body": body, "agency_id": agency, "starts_at": start, **more}
    if status:
        data["status"] = status
    if outcome:
        day = start[:10]
        data["outcome"] = {"result": "approved", "on": day, "source_url": SRC + mid, "verified_by": "Synthetic Organizer",
                           "verified_at": day, **({"votes": [{"name": n, "vote": v} for n, v in votes]} if votes else {"vote": "4-1"}),
                           "minutes_url": SRC + mid + "-minutes", "minutes_approved_on": day}
    return data


class BriefCase(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("socket.socket", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-agency-brief-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.base = self.tmp / "jurisdictions"
        (self.base / "us-ca").mkdir(parents=True)
        (self.base / "us-ca" / "package.json").write_text(json.dumps(synthetic_package()), encoding="utf-8")
        patcher = mock.patch.object(law, "JURISDICTIONS", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        (self.content / "meetings" / "2026-10-cedar-council.json").unlink()
        self.write("agencies.json", self.agencies_document())
        self.write("officials.json", {"schema_version": 1, "bodies": [{"agency_id": COUNCIL, "officials": [
            {"name": n, "role": "Council member", "source_url": "https://cedar.example/council", "checked_on": "2026-09-01"}
            for n in ("Ash Example", "Birch Example")]}]})
        site = self.read("site.json")
        site["comment_asks"] = ["Ask for the written sharing agreement.", "Ask for the search-audit schedule."]
        self.write("site.json", site)
        second = self.read("findings/cedar-policy-posted.json")
        second.update(id="fnd_0000000000000002", slug="council-note", title="Council received the policy (fictional)",
                      agency_id=COUNCIL, also_agency_ids=[POLICE], published_at="2026-03-01T18:00:00+00:00", corrections=[])
        self.write("findings/council-note.json", second)
        (self.root / "law").mkdir(exist_ok=True)
        (self.root / "law" / "local-rules.json").write_text(json.dumps({"schema_version": 1, "jurisdiction": "us-ca", "rules": [
            rule(rule_id="local-us-ca-b", citation="Cedar Code B", effective_from="2027-01-01", agency_ids=[POLICE]),
            rule(rule_id="local-us-ca-a", citation="Cedar Code A", effective_to="2025-12-31", agency_ids=[POLICE, COUNCIL]),
            rule(rule_id="local-us-ca-c", citation="Elsewhere Code", agency_ids=[SHERIFF])]}), encoding="utf-8")
        self.write("agency-actions.json", {"schema_version": 1, "actions": [
            {"agency_id": POLICE, "on": "2026-03-01", "kind": "policy_posted", "as_stated": "Policy was posted online.",
             "source_url": SRC + "a1", "checked_on": "2026-03-05"},
            {"agency_id": POLICE, "on": "2026-08-01", "kind": "sharing_ended", "as_stated": "Sharing with outside agencies ended.",
             "source_url": SRC + "a2", "checked_on": "2026-08-05"}]})
        self.meetings()

    # --- fixtures ---------------------------------------------------------------------------------------------
    def agencies_document(self):
        police = {"agency_id": POLICE, "slug": "cedar", "name": "Cedar Police Department (fictional)", "kind": "police",
                  "jurisdiction_name": "Cedar", "records_url": None, "records_email": "records@example.invalid", "verified": True,
                  "verification": VERIFIED, "flock_transparency_slug": "cedar-ca", "portal": {"vendor": "none", "url": None},
                  "asked": [{"topic": f"Topic {state}", "asked_on": "2026-05-0%d" % n, "state": state}
                            for n, state in enumerate(("awaiting_response", "acknowledged", "partly_answered", "answered",
                                                       "declined_in_writing", "withdrawn"), 1)]
                  + [{"topic": "Audit logs", "asked_on": "2026-06-01", "state": "answered", "updated_on": "2026-06-20",
                      "note": "Public note.", "request": "private-req-1"}],
                  "profile": {
                      "summary": SENTINEL + " summary", "facts": [SENTINEL + " fact"], "flags": [SENTINEL + " flag"],
                      "alpr_policy": {"url": SRC + "policy.pdf", "source_url": SRC + "policies", "checked_on": "2026-09-01"},
                      "contract": {"vendor": "Synthetic Vendor", "start": "2025-01-01", "end": "2027-01-01",
                                   "renewal_note": "Renews yearly.", "source_url": SRC + "contract", "checked_on": "2026-09-02",
                                   "cost": {"amount_usd": "12345.60", "period": "annual", "as_stated_in": "Agreement section 4"}},
                      "funding": [{"kind": "grant", "name": "State grant", "as_stated_in": "Budget p. 9", "source_url": SRC + "f1",
                                   "checked_on": "2026-09-03", "amount_usd": "1000.00", "period": "total", "fiscal_year": "2025"},
                                  {"kind": "general_fund", "name": "City fund", "as_stated_in": "Budget p. 12", "source_url": SRC + "f2",
                                   "checked_on": "2026-09-04"}],
                      "transparency": [{"checked_on": "2026-10-10", "source_url": "https://transparency.flocksafety.com/cedar-ca",
                                        "cameras": 12, "retention_days": 30, "searches_last_30_days": None, "hotlist_alerts": 0,
                                        "shared_with_agencies": 7},
                                       {"checked_on": "2026-09-10", "source_url": "https://transparency.flocksafety.com/cedar-ca",
                                        "cameras": 10}],
                      "sharing": {"checked_on": "2026-10-11", "source_url": "https://transparency.flocksafety.com/cedar-ca",
                                  "recipients": [{"name": "Synthetic PD", "state": "CA", "kind": "local"},
                                                 {"name": "Synthetic Fed", "state": "US", "kind": "federal"}]},
                      "pre_implementation_meeting": {"status": "found", "checked_on": "2026-09-05", "meeting_on": "2025-02-01",
                                                     "body": "Cedar City Council (fictional)", "source_url": SRC + "pre"}}}
        council = {"agency_id": COUNCIL, "name": "Cedar City Council (fictional)", "kind": "city_council", "jurisdiction_name": "Cedar",
                   "records_url": None, "records_email": None, "portal": {"vendor": "unknown", "url": None}, "verified": True,
                   "verification": VERIFIED}
        other = dict(council, agency_id=OTHER, name="Cedar Planning Council (fictional)")
        sheriff = {"agency_id": SHERIFF, "name": "Cedar County Sheriff (fictional)", "kind": "sheriff", "jurisdiction_name": "Cedar County",
                   "records_url": None, "records_email": None, "portal": {"vendor": "unknown", "url": None}, "verified": False}
        return [police, council, other, sheriff]

    def meetings(self, extra=()):
        for path in (self.content / "meetings").glob("*.json"):
            path.unlink()
        council = "Cedar City Council (fictional)"
        for data in (
                vote_meeting("past-vote", council, COUNCIL, "2026-09-02T18:00:00-07:00", [("Ash Example", "yes"), ("Birch Example", "no")]),
                vote_meeting("past-aggregate", council, COUNCIL, "2026-10-02T18:00:00-07:00"),
                vote_meeting("future-b", council, COUNCIL, "2026-12-02T18:00:00-08:00", outcome=False, agenda_item="Item 9",
                             location="Civic Center", agenda_url=SRC + "agenda"),
                vote_meeting("future-a", council, COUNCIL, "2026-11-20T18:00:00-08:00", outcome=False),
                vote_meeting("future-cancelled", council, COUNCIL, "2026-12-09T18:00:00-08:00", status="cancelled", outcome=False),
                vote_meeting("other-future", "Cedar Planning Council (fictional)", OTHER, "2026-12-03T18:00:00-08:00", outcome=False),
                vote_meeting("direct-police", "Police briefing", POLICE, "2026-12-04T18:00:00-08:00", outcome=False), *extra):
            (self.content / "meetings" / f"{data['id']}.json").write_text(json.dumps(data), encoding="utf-8")

    def read(self, name):
        return json.loads((self.content / name).read_text(encoding="utf-8"))

    def write(self, name, document):
        path = self.content / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")

    def edit_agencies(self, change):
        agencies = self.read("agencies.json")
        change(agencies[0])
        self.write("agencies.json", agencies)

    def brief(self, **kwargs):
        return build_brief(self.root, kwargs.pop("agency", POLICE), now=kwargs.pop("now", NOW), **kwargs)

    def pair(self):
        folder = self.root / "kit" / "briefs"
        return (folder / HTML).read_text(encoding="utf-8"), (folder / TXT).read_text(encoding="utf-8")

    def rows(self):
        model = load_content(self.root, today=NOW.date())
        return build_rows(model, next(a for a in model["agencies"] if a["agency_id"] == POLICE), NOW)


class ProjectionTests(BriefCase):
    def test_public_projection_and_provenance(self):
        result = self.brief()
        self.assertEqual(result["written"], ["kit/briefs/" + HTML, "kit/briefs/" + TXT])
        page, text = self.pair()
        for row in self.rows():  # one list of rows renders to both formats: text, every source and the date appear in each
            for part in [row["text"], row["when"], *row["sources"]]:
                if part:
                    self.assertIn(htmllib.escape(part, quote=True), page, part)
                    self.assertIn(part, text, part)
        for expected in ("$12,345.60 (per year)", "Agreement section 4", "State grant; $1,000.00 (total); fiscal year 2025", "City fund",
                         "Cameras reported: 12", "Hotlist alerts reported: 0", "Searches in the last 30 days reported: not recorded",
                         "Agencies reported as receiving shared data (portal number): 7",
                         "Recipients named in the transcribed vendor-portal sharing list: 2",
                         "Request: Audit logs (state: answered) Note: Public note.", "(state: withdrawn)", "(state: awaiting_response)",
                         "Council received the policy (fictional)", "Cedar Police produced a written ALPR usage policy", "correction 2026-02-03",
                         "roll call as recorded", "Ash Example: yes; Birch Example: no", "vote as stored (aggregate): 4-1",
                         "Campaign asks, not agency statements: Ask for the written sharing agreement.",
                         "Prepared 2026-11-04 (UTC); sources retain their own dates", agency_brief.FOOTER):
            self.assertIn(expected, text)
        self.assertEqual(text.count("https://cedar-records.example.invalid/findings/council-note.html"), 1)  # secondary agency, once
        self.assertEqual(text.count("Council received the policy (fictional) - "), 1)
        self.assertNotIn(SENTINEL, text + page)  # generic free-text profile fields have no scoped provenance
        self.assertNotIn("records@example.invalid", text + page)
        self.assertNotIn("private-req-1", text + page)
        self.assertLess(text.index("Sharing with outside"), text.index("Policy was posted"))  # actions newest first
        self.assertLess(text.index("Cedar Code A"), text.index("Cedar Code B"))  # rule_id order
        self.assertIn("past, not in force on the prepared date", text)
        self.assertIn("not yet in force on the prepared date", text)
        self.assertIn("not a determination about agency conduct", text)
        self.assertLess(text.index("starts 2026-11-20"), text.index("starts 2026-12-02"))  # upcoming by instant
        self.assertNotIn("Cedar Code C", text)
        self.assertNotIn("Elsewhere Code", text)
        self.assertNotIn("checked 2026-11-04", text)  # the generation date is never a checked date
        for row in self.rows():
            self.assertTrue(row["sources"], row["where"])
        stat_html = (self.root / "kit" / "briefs" / HTML).stat()
        self.assertEqual(stat.S_IMODE(stat_html.st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "kit").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "kit" / "briefs").stat().st_mode), 0o700)
        self.assertFalse((self.root / "public").exists())

    def test_governing_body_is_not_guessed(self):
        text = (self.brief(), self.pair())[1][1]
        self.assertIn("Governing body: Cedar City Council (fictional) (recorded as the body that met on 2025-02-01", text)
        self.assertIn("Ash Example: yes; Birch Example: no", text)
        self.assertIn("starts 2026-11-20", text)
        self.assertIn("Police briefing, starts 2026-12-04T18:00:00-08:00 [meeting tagged directly with this agency]", text)
        self.assertNotIn("Cedar Planning Council", text.split("== Upcoming staged meetings ==")[1])  # same jurisdiction, other body
        self.assertNotIn("2026-12-09", text)  # cancelled
        shutil.rmtree(self.root / "kit")

        def other(agency):
            agency["profile"]["pre_implementation_meeting"]["body"] = "Cedar Planning Council (fictional)"
        self.edit_agencies(other)
        text = (self.brief(), self.pair())[1][1]
        self.assertIn("Governing body: Cedar Planning Council (fictional)", text)
        self.assertIn("Cedar Planning Council (fictional), starts 2026-12-03", text)
        self.assertNotIn("Ash Example", text)
        for label, change in (
                ("absent", lambda a: a["profile"].pop("pre_implementation_meeting")),
                ("not found yet", lambda a: a["profile"].update(pre_implementation_meeting={"status": "not_found_yet", "checked_on": "2026-09-05"})),
                ("unknown body name", lambda a: a["profile"]["pre_implementation_meeting"].update(body="Cedar Council"))):
            with self.subTest(label):
                shutil.rmtree(self.root / "kit", ignore_errors=True)
                agencies = self.agencies_document()
                change(agencies[0])
                self.write("agencies.json", agencies)
                text = (self.brief(), self.pair())[1][1]
                self.assertIn("Governing body not established by published content", text)
                self.assertIn("content/agencies.json:ca-cedar-police.profile.pre_implementation_meeting", text)
                self.assertNotIn("Ash Example", text)
                self.assertNotIn("starts 2026-11-20", text)
                self.assertIn("Police briefing, starts 2026-12-04", text)  # directly tagged, labeled
        for label, edit in (("ambiguous", lambda a: a.append(dict(a[1], agency_id="ca-cedar-twin-council"))),
                            ("other jurisdiction", lambda a: a[1].update(jurisdiction_name="Elsewhere"))):
            with self.subTest(label):
                shutil.rmtree(self.root / "kit", ignore_errors=True)
                agencies = self.agencies_document()
                edit(agencies)
                self.write("agencies.json", agencies)
                self.assertIn("Governing body not established by published content", (self.brief(), self.pair())[1][1])

    def test_meeting_boundary_is_exact(self):
        self.meetings([vote_meeting("at-now", "Cedar City Council (fictional)", COUNCIL, "2026-11-04T04:00:00-08:00", outcome=False),
                       vote_meeting("just-past", "Cedar City Council (fictional)", COUNCIL, "2026-11-04T03:59:59-08:00",
                                    [("Ash Example", "no")])])
        text = (self.brief(), self.pair())[1][1]
        upcoming = text.split("== Upcoming staged meetings ==")[1]
        self.assertIn("starts 2026-11-04T04:00:00-08:00", upcoming)  # 12:00 UTC exactly: at or after now
        self.assertNotIn("03:59:59", upcoming)
        self.assertIn("03:59:59", text.split("== Recorded votes ==")[1].split("== Upcoming")[0])

    def test_optional_data_is_honest(self):
        agencies = self.agencies_document()
        agencies[0] = {k: v for k, v in agencies[0].items() if k not in ("profile", "asked", "flock_transparency_slug")}
        self.write("agencies.json", agencies)
        site = self.read("site.json")
        site.pop("comment_asks")
        site.pop("base_url")
        self.write("site.json", site)
        for name in ("agency-actions.json",):
            (self.content / name).unlink()
        (self.root / "law" / "local-rules.json").unlink()
        (self.content / "findings" / "council-note.json").unlink()
        (self.content / "findings" / "cedar-policy-posted.json").unlink()
        for path in (self.content / "meetings").glob("*.json"):
            path.unlink()
        self.brief()
        page, text = self.pair()
        for expected in ("Not recorded: posted ALPR usage policy link", "Not recorded: contract term and cost", "Not recorded: funding",
                         "Not recorded: vendor transparency-portal numbers", "Not recorded: transcribed sharing list",
                         "Not recorded: requests", "Not recorded: no published finding names this agency",
                         "Not recorded: no local rule names this agency", "Not recorded: actions",
                         "Governing body not established by published content", "Not recorded: no past outcome",
                         "Not recorded: no upcoming staged meeting", "Campaign asks not recorded",
                         "content/agencies.json:ca-cedar-police.profile.contract", "content/site.json:comment_asks"):
            self.assertIn(expected, text)
        self.assertNotIn("Ask for the written sharing agreement", text)  # no built-in or generated asks
        self.assertNotIn("Two minutes", text)
        # a record with no checked date keeps its own dates and shows today's date nowhere as a checked date
        self.assertNotIn("checked 2026-11-04", text)

    def test_relative_links_without_base_url(self):
        site = self.read("site.json")
        site.pop("base_url")
        self.write("site.json", site)
        self.brief()
        page, text = self.pair()
        self.assertIn("Source: /findings/council-note.html", text)
        self.assertIn('<a href="/findings/council-note.html" rel="noopener noreferrer">/findings/council-note.html</a>', page)
        self.assertIn("Source: /meetings/future-a.html", text)


class BoundaryTests(BriefCase):
    def snapshot(self):
        return tree(self.root)

    def refused(self, agency, pattern, **kwargs):
        before = self.snapshot()
        with self.assertRaisesRegex(BriefError, pattern) as raised:
            self.brief(agency=agency, **kwargs)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "kit" / "briefs").exists())
        self.assertNotIn(SENTINEL, str(raised.exception))
        return str(raised.exception)

    def test_no_private_fallback_or_unreviewed_text(self):
        kit = self.root / "kit"
        kit.mkdir()
        (kit / "agencies.json").write_text(json.dumps([{"agency_id": POLICE, "name": SENTINEL, "verified": True}]), encoding="utf-8")
        (kit / "sent.json").write_text(SENTINEL, encoding="utf-8")
        (self.root / "private").mkdir()
        (self.root / "private" / "drafts.txt").write_text(SENTINEL, encoding="utf-8")
        (self.content / "agencies.json").rename(self.tmp / "agencies.json")
        before = self.snapshot()
        with self.assertRaisesRegex(BriefError, r"content/agencies\.json is required"):
            self.brief()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((kit / "briefs").exists())
        (self.tmp / "agencies.json").rename(self.content / "agencies.json")
        self.assertIn("'ca-nowhere'", self.refused("ca-nowhere", "ca-nowhere"))
        self.refused(SHERIFF, "ca-cedar-county-sheriff is not organizer-verified")
        agencies = self.read("agencies.json")
        agencies.append({"agency_id": "ca-cedar-hidden", "name": "Hidden agency (fictional)", "selected": False, "verified": False})
        self.write("agencies.json", agencies)
        self.refused("ca-cedar-hidden", "not a selected agency")
        # an unreviewed finding in content/ is the ordinary validation refusal, never a preview or a bypass
        for label, change, message in (("draft", {"state": "draft"}, "only published findings"),
                                       ("attorney", {"confidence": "needs_attorney_review"}, "needs_attorney_review")):
            with self.subTest(label):
                document = self.read("findings/cedar-policy-posted.json")
                self.write("findings/cedar-policy-posted.json", {**document, **change})
                with self.assertRaisesRegex(ContentError, message):
                    self.brief()
                self.write("findings/cedar-policy-posted.json", document)
        self.assertFalse((kit / "briefs").exists())
        self.assertEqual(self.read("agencies.json")[0]["asked"][-1]["request"], "private-req-1")
        self.brief()
        page, text = self.pair()
        self.assertNotIn(SENTINEL, page + text)

    def test_no_replace_keeps_pair_and_replace_refreshes(self):
        briefs = self.root / "kit" / "briefs"
        self.brief()
        original = tree(briefs)
        for name in (HTML, TXT):  # either twin alone blocks a write without --replace, and keeps both
            (briefs / (TXT if name == HTML else HTML)).unlink()
            kept = (briefs / name).read_bytes()
            with self.assertRaisesRegex(BriefError, "already exists; both files were kept unchanged"):
                self.brief()
            self.assertEqual(tree(briefs), {name: kept})
            (briefs / (TXT if name == HTML else HTML)).write_bytes(original[TXT if name == HTML else HTML])
        (briefs / TXT).write_text("edited by hand", encoding="utf-8")
        with self.assertRaises(BriefError):
            self.brief()
        self.assertEqual((briefs / TXT).read_text(encoding="utf-8"), "edited by hand")
        self.brief(replace=True)
        self.assertEqual(tree(briefs), original)
        self.assertEqual(stat.S_IMODE((briefs / TXT).stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in briefs.iterdir()), [HTML, TXT])

    def test_unsafe_targets_refuse_even_with_replace(self):
        briefs = self.root / "kit" / "briefs"
        briefs.mkdir(parents=True)
        outside = self.tmp / "outside.txt"
        outside.write_text("do not touch", encoding="utf-8")
        for label, make in (("dangling symlink", lambda p: p.symlink_to(self.tmp / "missing")),
                            ("symlink", lambda p: p.symlink_to(outside)),
                            ("hardlink", lambda p: os.link(outside, p)),
                            ("fifo", lambda p: os.mkfifo(p)),
                            ("directory", lambda p: p.mkdir())):
            with self.subTest(label):
                target = briefs / TXT
                make(target)
                before = os.lstat(target)
                with self.assertRaisesRegex(BriefError, "not a regular single-link file"):
                    self.brief(replace=True)
                self.assertEqual(os.lstat(target).st_ino, before.st_ino)
                self.assertFalse((briefs / HTML).exists())  # nothing written for the other twin either
                self.assertEqual(outside.read_text(encoding="utf-8"), "do not touch")
                if target.is_dir() and not target.is_symlink():
                    target.rmdir()
                else:
                    target.unlink()
        for label, make in (("kit/briefs symlink", lambda: (shutil.rmtree(self.root / "kit"), (self.root / "kit").mkdir(),
                                                            (self.root / "kit" / "briefs").symlink_to(self.tmp))),
                            ("kit symlink", lambda: (shutil.rmtree(self.root / "kit"), (self.root / "kit").symlink_to(self.tmp)))):
            with self.subTest(label):
                make()
                with self.assertRaisesRegex(BriefError, "must be a real directory"):
                    self.brief(replace=True)
                self.assertFalse((self.tmp / HTML).exists())
                (self.root / "kit" / "briefs").unlink() if label.startswith("kit/briefs") else (self.root / "kit").unlink()

    def test_preserve_replace_and_failure(self):
        briefs = self.root / "kit" / "briefs"
        real = agency_brief._write_file
        calls = []

        def second_fails(directory, name, data, replace, expected):
            calls.append(name)
            if len(calls) == 2:
                raise agency_brief._WriteError(name, False, False, "target publication", False)
            return real(directory, name, data, replace, expected)
        with mock.patch.object(agency_brief, "_write_file", side_effect=second_fails):
            with self.assertRaisesRegex(BriefError, r"%s could not be written.*kit/briefs/%s was already "
                                        r"written, so the pair is incomplete.*same command again with --replace"
                                        % (TXT.replace(".", r"\."), HTML.replace(".", r"\."))):
                self.brief()
        self.assertEqual(sorted(p.name for p in briefs.iterdir()), [HTML])  # the partial pair stays; no temporary is left
        self.brief(replace=True)  # repairs it
        self.assertEqual(sorted(p.name for p in briefs.iterdir()), [HTML, TXT])
        shutil.rmtree(self.root / "kit")
        real_fsync = os.fsync

        def file_fsync_fails(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(5, "Input/output error")
            return real_fsync(fd)
        with mock.patch("os.fsync", side_effect=file_fsync_fails):
            with self.assertRaisesRegex(BriefError, "could not be written"):
                self.brief()
        self.assertEqual(list(briefs.iterdir()), [])  # the owned temporary is cleaned, nothing else is created
        # a file that arrives between preflight and commit is never overwritten without --replace
        arrival = b"arrived meanwhile"

        def arrives(directory, name, data, replace, expected):
            (briefs / name).write_bytes(arrival)
            return real(directory, name, data, replace, expected)
        with mock.patch.object(agency_brief, "_write_file", side_effect=arrives):
            with self.assertRaisesRegex(BriefError, "appeared or changed"):
                self.brief()
        self.assertEqual((briefs / HTML).read_bytes(), arrival)
        self.assertEqual(sorted(p.name for p in briefs.iterdir()), [HTML])
        # a clock without a zone is refused
        with self.assertRaisesRegex(BriefError, "timezone-aware"):
            build_brief(self.root, POLICE, now=datetime(2026, 11, 4))


class OutputTests(BriefCase):
    def test_inert_print_and_gate(self):
        nasty = '"q" & \'s\' onerror=y javascript:x'
        finding = self.read("findings/cedar-policy-posted.json")
        finding["title"] = "Title " + nasty
        self.write("findings/cedar-policy-posted.json", finding)
        before = tree(self.root)
        self.brief()
        page, text = self.pair()
        self.assertIn("Title " + nasty, text)  # plain text twin keeps the characters as data
        for banned in ("<script", "<img", "<iframe", "<form", "<svg", "<link", "<object", "<embed"):
            self.assertNotIn(banned, page.lower())
        self.assertNotIn(nasty, page)  # the page holds the escaped form only
        self.assertIn("Title &quot;q&quot; &amp; &#x27;s&#x27; onerror=y javascript:x", page)
        self.assertEqual(page.count("<a href="), page.count('rel="noopener noreferrer">'))
        self.assertNotIn(' href="javascript', page)
        self.assertIn("default-src 'none'; style-src 'unsafe-inline'", page)
        self.assertIn('<html lang="en">', page)
        self.assertIn(agency_brief.FOOTER, page)
        self.assertNotIn("<meta http-equiv=\"refresh\"", page)
        self.assertEqual([p for p in tree(self.root) if p not in before], ["kit/briefs/" + HTML, "kit/briefs/" + TXT])
        for name, data in before.items():
            self.assertEqual(tree(self.root)[name], data, name)

    def test_privacy_gate_refuses_each_public_row_without_leaking(self):
        values = {"email": "jane.doe@example.invalid", "phone": "805-555-0142", "plate": "7ABC123"}
        for category, value in values.items():
            for label, change in (
                    ("renewal note", lambda a, v: a["profile"]["contract"].update(renewal_note="Note %s end" % v)),
                    ("asked topic", lambda a, v: a["asked"][0].update(topic="Topic %s" % v)),
                    ("funding name", lambda a, v: a["profile"]["funding"][0].update(name="Fund %s" % v))):
                with self.subTest(category=category, row=label):
                    shutil.rmtree(self.root / "kit", ignore_errors=True)
                    agencies = self.agencies_document()
                    change(agencies[0], value)
                    self.write("agencies.json", agencies)
                    with self.assertRaises(BriefError) as raised:
                        self.brief()
                    self.assertIn(category, str(raised.exception))
                    self.assertNotIn(value, str(raised.exception))
                    self.assertFalse((self.root / "kit").exists())
            with self.subTest(category=category, row="finding summary"):
                self.write("agencies.json", self.agencies_document())
                finding = self.read("findings/cedar-policy-posted.json")
                self.write("findings/cedar-policy-posted.json", {**finding, "summary": "Summary %s end" % value})
                with self.assertRaisesRegex(BriefError, "brief row Published findings/findings/cedar-policy-posted: " + category):
                    self.brief()
                self.write("findings/cedar-policy-posted.json", finding)
                self.assertFalse((self.root / "kit").exists())
            with self.subTest(category=category, row="campaign ask"):
                self.write("agencies.json", self.agencies_document())
                site = self.read("site.json")
                self.write("site.json", {**site, "comment_asks": ["Ask %s end" % value]})
                with self.assertRaises(BriefError) as raised:
                    self.brief()
                self.assertNotIn(value, str(raised.exception))
                self.write("site.json", site)
        self.assertFalse((self.root / "kit").exists())

    def test_legal_wording_is_alert_only(self):
        site = self.read("site.json")
        site["comment_asks"] = ["Cedar Police violated state law and broke the law."]
        self.write("site.json", site)
        result = self.brief()
        self.assertTrue(any(note.startswith("review-required: brief row Campaign asks/comment_asks[1]") for note in result["flags"]))
        self.assertTrue(all("violated" not in note for note in result["flags"]))  # names the place and rule, never the text
        self.assertIn("Cedar Police violated state law and broke the law.", self.pair()[1])  # not paraphrased or blocked

    def test_command_line(self):
        code, out, err = run("agency-brief", "--directory", self.root, "--agency", POLICE)
        self.assertEqual((code, err.strip()), (0, ""), err)
        self.assertEqual(out.splitlines()[-1], "Private brief written. Nothing was sent or published.")
        self.assertEqual(out.splitlines()[:2], ["kit/briefs/" + HTML.replace("2026-11-04", datetime.now(timezone.utc).date().isoformat()),
                                                "kit/briefs/" + TXT.replace("2026-11-04", datetime.now(timezone.utc).date().isoformat())])
        code, out, err = run("agency-brief", "--directory", self.root, "--agency", POLICE)
        self.assertEqual(code, 1)
        self.assertIn("Stopped:", err)
        self.assertIn("already exists", err)
        self.assertEqual(out, "")
        code, out, err = run("agency-brief", "--directory", self.root, "--agency", "ca-nowhere")
        self.assertEqual(code, 1)
        self.assertIn("ca-nowhere", err)
        code, _, err = run("agency-brief", "--directory", self.root, "--agency", POLICE, "--replace")
        self.assertEqual(code, 0, err)
        self.assertEqual(run("agency-brief", "--directory", self.root)[0], 2)  # parser error: --agency is required

    def test_rows_are_deep_copy_safe(self):
        rows = self.rows()
        self.assertEqual(rows, copy.deepcopy(self.rows()))  # same input and clock give the same rows


class SetupPersistenceTests(BriefCase):
    FIXED = ("Stopped: kit/briefs: output directory setup could not be flushed; crash durability is unconfirmed; "
             "no brief files were written")

    def layout(self, layout):
        shutil.rmtree(self.root / "kit", ignore_errors=True)
        if layout in ("kit-only", "both", "replace"):
            (self.root / "kit").mkdir(mode=0o755)
        if layout in ("both", "replace"):
            (self.root / "kit" / "briefs").mkdir(mode=0o755)
        briefs = self.root / "kit" / "briefs"
        if layout == "replace":
            for name in (HTML, TXT):
                (briefs / name).write_bytes(b"OLD " + name.encode())
        return briefs

    def labelled_fsync(self, fail=None):
        """Patch os.fsync; events hold raw (dev, ino, is_regular); fail is 'root' or 'kit' by identity."""
        real = os.fsync
        events = []
        ids = {"root": self.root.stat(), "kit": None}

        def fsync(fd):
            info = os.fstat(fd)
            key = (info.st_dev, info.st_ino)
            events.append((key, stat.S_ISREG(info.st_mode)))
            kit = ids["kit"] or ((self.root / "kit").stat() if (self.root / "kit").exists() else None)
            for label, ident in (("root", ids["root"]), ("kit", kit)):
                if label == fail and ident is not None and key == (ident.st_dev, ident.st_ino) and not events[-1][1]:
                    raise OSError(5, SENTINEL + " " + str(self.root))
            return real(fd)
        return events, mock.patch.object(os, "fsync", side_effect=fsync)

    def names(self, events):
        root, kit, briefs = (self.root.stat(), (self.root / "kit").stat(), (self.root / "kit" / "briefs").stat())
        table = {(root.st_dev, root.st_ino): "root", (kit.st_dev, kit.st_ino): "kit", (briefs.st_dev, briefs.st_ino): "briefs"}
        return ["file" if regular else table[key] for key, regular in events]

    def cli(self, replace=False):
        with mock.patch.object(agency_brief, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            return run("agency-brief", "--directory", self.root, "--agency", POLICE, *(["--replace"] if replace else []))

    def test_setup_parent_barriers_precede_first_twin_in_every_layout(self):
        for layout in ("neither", "kit-only", "both", "replace"):
            with self.subTest(layout=layout):
                self.layout(layout)
                events, patch = self.labelled_fsync()
                with patch:
                    result = self.brief(replace=layout == "replace")
                self.assertEqual(self.names(events), ["root", "kit", "file", "briefs", "file", "briefs"])
                self.assertEqual(result["written"], ["kit/briefs/" + HTML, "kit/briefs/" + TXT])
                briefs = self.root / "kit" / "briefs"
                self.assertEqual(sorted(p.name for p in briefs.iterdir()), [HTML, TXT])
                for name in (HTML, TXT):
                    self.assertEqual(stat.S_IMODE((briefs / name).stat().st_mode), 0o600)
                if layout in ("neither", "kit-only"):
                    self.assertEqual(stat.S_IMODE(briefs.stat().st_mode), 0o700)
                if layout == "neither":
                    self.assertEqual(stat.S_IMODE((self.root / "kit").stat().st_mode), 0o700)

    def test_each_setup_barrier_failure_refuses_before_any_twin(self):
        for layout in ("neither", "both", "replace"):
            for fail in ("root", "kit"):
                with self.subTest(layout=layout, fail=fail):
                    briefs = self.layout(layout)
                    before = tree(briefs) if briefs.exists() else {}
                    sentinel = self.root / "kit" / "unrelated.txt"
                    sentinel.parent.mkdir(exist_ok=True)
                    sentinel.write_bytes(b"keep")
                    events, patch = self.labelled_fsync(fail)
                    with patch, mock.patch.object(agency_brief, "_write_file") as writer:
                        code, out, err = self.cli(layout == "replace")
                    self.assertEqual((code, out), (1, ""))
                    self.assertEqual(err.strip(), self.FIXED)
                    self.assertNotIn(SENTINEL, err)
                    self.assertNotIn(str(self.root), err)
                    writer.assert_not_called()
                    self.assertFalse(any(regular for _, regular in events))
                    self.assertEqual(tree(briefs) if briefs.exists() else {}, before)
                    self.assertEqual(sentinel.read_bytes(), b"keep")
                    self.assertFalse(list((self.root / "kit").rglob(".*.tmp")))

    def test_retry_flushes_visible_directories_left_by_failed_setup(self):
        for fail in ("root", "kit"):
            with self.subTest(fail=fail):
                self.layout("neither")
                _, patch = self.labelled_fsync(fail)
                with patch:
                    code, _, err = self.cli()
                self.assertEqual(code, 1, err)
                self.assertTrue((self.root / "kit").is_dir())
                events, patch = self.labelled_fsync()
                with patch:
                    code, out, err = self.cli()
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(self.names(events), ["root", "kit", "file", "briefs", "file", "briefs"])
                self.assertIn("Private brief written.", out)

    def test_descriptors_are_closed_on_success_and_setup_failure(self):
        def open_fds():
            return len(os.listdir("/proc/self/fd"))
        self.layout("neither")
        start = open_fds()
        self.brief()
        self.assertEqual(open_fds(), start)
        for fail in ("root", "kit"):
            self.layout("neither")
            _, patch = self.labelled_fsync(fail)
            with patch, self.assertRaisesRegex(BriefError, "setup could not be flushed"):
                self.brief()
            self.assertEqual(open_fds(), start)

    def test_validation_and_preflight_refuse_before_setup_fsync(self):
        briefs = self.layout("both")
        (briefs / HTML).write_bytes(b"OLD")
        events, patch = self.labelled_fsync()
        with patch:
            with self.assertRaisesRegex(BriefError, "already exists"):
                self.brief()
            with self.assertRaisesRegex(BriefError, "not organizer-verified"):
                self.brief(agency=SHERIFF)
        self.assertEqual(events, [])
        self.assertEqual(sorted(p.name for p in briefs.iterdir()), [HTML])


class PublicationFailureTests(BriefCase):
    def prepare(self, replace=False):
        shutil.rmtree(self.root / "kit", ignore_errors=True)
        self.brief()
        folder = self.root / "kit" / "briefs"
        expected = {name: (folder / name).read_bytes() for name in (HTML, TXT)}
        for name in expected:
            (folder / name).unlink()
        if replace:
            for name in expected:
                (folder / name).write_bytes(b"OLD " + name.encode())
        return folder, expected

    def cli(self, replace=False):
        # Exercise the real CLI boundary with the fixture's fixed, aware clock.
        with mock.patch.object(agency_brief, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            return run("agency-brief", "--directory", self.root, "--agency", POLICE,
                       *(["--replace"] if replace else []))

    def failed(self, replace=False):
        code, out, err = self.cli(replace)
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")
        self.assertIn("Stopped:", err)
        self.assertNotIn(SENTINEL, err)
        return err

    def repaired(self, folder, expected):
        code, out, err = self.cli(True)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Private brief written.", out)
        self.assertEqual(tree(folder), expected)
        for name in expected:
            self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600)
        result = self.brief(replace=True)
        self.assertEqual(result, {"written": ["kit/briefs/" + HTML, "kit/briefs/" + TXT], "flags": []})

    def directory_failure(self, twin, replace=False):
        folder, expected = self.prepare(replace)
        real = os.fsync
        calls = 0
        target = os.stat(folder)  # only the briefs directory descriptor counts, not the setup parents

        def fsync(fd):
            nonlocal calls
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                calls += 1
                if calls == twin:
                    raise OSError(5, SENTINEL)
            return real(fd)

        with mock.patch.object(os, "fsync", side_effect=fsync):
            err = self.failed(replace)
        self.assertEqual(calls, twin)  # never continue to the other twin after failure
        self.assertIn("kit/briefs/" + HTML, err)
        self.assertIn("durability could not be confirmed", err)
        self.assertIn("--replace", err)
        self.assertEqual((folder / HTML).read_bytes(), expected[HTML])
        if twin == 1:
            self.assertIn("pair is incomplete", err)
            if replace:
                self.assertEqual((folder / TXT).read_bytes(), b"OLD " + TXT.encode())
            else:
                self.assertFalse((folder / TXT).exists())
        else:
            self.assertEqual((folder / TXT).read_bytes(), expected[TXT])
            self.assertIn("both files were written", err)
            self.assertIn("kit/briefs/" + TXT, err)
            self.assertNotIn("incomplete", err)
            self.assertNotIn("could not be written", err)
        self.assertFalse(list(folder.glob(".*.tmp")))
        self.repaired(folder, expected)

    def test_first_create_directory_flush_failure_reports_published_html(self):
        self.directory_failure(1)

    def test_first_replace_directory_flush_failure_reports_mixed_pair(self):
        self.directory_failure(1, True)

    def test_second_directory_flush_failure_reports_both_written_not_incomplete(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                self.directory_failure(2, replace)

    def test_prepublication_failures_keep_target_state(self):
        for replace in (False, True):
            for twin in (1, 2):
                for phase in ("fchmod", "fsync", "publish", "open", "write", "fdopen"):
                    with self.subTest(replace=replace, twin=twin, phase=phase):
                        folder, expected = self.prepare(replace)
                        operation = (("replace" if replace else "link") if phase == "publish"
                                     else "fdopen" if phase == "write" else phase)
                        real = getattr(os, operation)
                        calls = 0

                        @contextlib.contextmanager
                        def failed_write(*args, **kwargs):
                            nonlocal calls
                            with real(*args, **kwargs) as stream:
                                if args[1] != "wb":
                                    yield stream
                                    return
                                calls += 1
                                wrapped = mock.Mock(wraps=stream)
                                if calls == twin:
                                    wrapped.write.side_effect = OSError(5, SENTINEL)
                                yield wrapped

                        def fail(*args, **kwargs):
                            nonlocal calls
                            selected = (phase != "fsync" or stat.S_ISREG(os.fstat(args[0]).st_mode))
                            selected = selected and (phase != "open" or str(args[0]).endswith(".tmp"))
                            selected = selected and (phase != "fdopen" or args[1] == "wb")
                            if selected:
                                calls += 1
                                if calls == twin:
                                    raise OSError(5, SENTINEL)
                            return real(*args, **kwargs)

                        with mock.patch.object(os, operation, side_effect=failed_write if phase == "write" else fail):
                            err = self.failed(replace)
                        name = (HTML, TXT)[twin - 1]
                        self.assertIn("kit/briefs/" + name + " could not be written", err)
                        self.assertEqual("pair is incomplete" in err, twin == 2)
                        if replace:
                            self.assertEqual((folder / name).read_bytes(), b"OLD " + name.encode())
                        else:
                            self.assertFalse((folder / name).exists())
                        if twin == 2:
                            self.assertEqual((folder / HTML).read_bytes(), expected[HTML])
                        self.assertFalse(list(folder.glob(".*.tmp")))
                        self.repaired(folder, expected)

    def test_create_temp_unlink_failure_after_link_is_postpublication(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                folder, expected = self.prepare()
                caller = folder / "caller.txt"
                caller.write_bytes(b"caller-owned")
                real = os.unlink
                calls = 0

                def unlink(name, **kwargs):
                    nonlocal calls
                    if str(name).endswith(".tmp"):
                        calls += 1
                        if persistent or calls == 1:
                            raise OSError(5, SENTINEL)
                    return real(name, **kwargs)

                with mock.patch.object(os, "unlink", side_effect=unlink):
                    err = self.failed()
                self.assertIn("temporary cleanup failed", err)
                self.assertIn("pair is incomplete", err)
                self.assertIn("--replace", err)
                self.assertNotIn("could not be written", err)
                self.assertEqual((folder / HTML).read_bytes(), expected[HTML])
                self.assertFalse((folder / TXT).exists())
                self.assertEqual(caller.read_bytes(), b"caller-owned")
                self.assertEqual(bool(list(folder.glob(".*.tmp"))), persistent)
                caller.unlink()
                # Persistent cleanup retains the owned link; explicit retry still refuses
                # its now-multilink target until the organizer resolves the named residue.
                if persistent:
                    with self.assertRaisesRegex(BriefError, "single-link"):
                        self.brief(replace=True)
                    for temporary in folder.glob(".*.tmp"):
                        temporary.unlink()
                self.repaired(folder, expected)

    def test_second_target_safety_refusal_retains_partial_warning(self):
        for kind in ("arrived", "symlink", "identity"):
            with self.subTest(kind=kind):
                folder, expected = self.prepare(kind == "identity")
                real = agency_brief._write_file
                outside = self.tmp / "outside"
                outside.write_bytes(b"caller-owned")

                def arrives(directory, name, data, replace, identity):
                    if name == TXT:
                        if kind == "symlink":
                            (folder / name).symlink_to(outside)
                        elif kind == "identity":
                            replacement = folder / "new"
                            replacement.write_bytes(b"arrived")
                            os.replace(replacement, folder / name)
                        else:
                            (folder / name).write_bytes(b"arrived")
                    return real(directory, name, data, replace, identity)

                with mock.patch.object(agency_brief, "_write_file", side_effect=arrives):
                    err = self.failed(kind == "identity")
                self.assertEqual(err.count("pair is incomplete"), 1)
                self.assertIn("appeared or changed", err)
                self.assertIn("--replace", err)
                self.assertEqual((folder / HTML).read_bytes(), expected[HTML])
                self.assertEqual(outside.read_bytes(), b"caller-owned")
                self.assertEqual((folder / TXT).read_bytes(), b"caller-owned" if kind == "symlink" else b"arrived")
                (folder / TXT).unlink()
                self.repaired(folder, expected)


if __name__ == "__main__":
    unittest.main()
