"""DF2207-06: a published finding page lists the staged meetings whose comment kit cites its finding id.

Synthetic only: temp copies of examples/fictional-campaign; nothing is fetched or sent.
"""
import json
import random
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool import findings as findings_module
from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_cited_findings, load_content, load_meeting
from campaign_tool.meeting_stage import MeetingStageError, stage
from campaign_tool.site import OUTCOME_LABELS, Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 20)
POLICE, SHERIFF = "ca-cedar-police", "ca-cedar-county-sheriff"
SECTION = re.compile(r'<h3 id="discussed-at-public-meetings">.*?</ul>', re.S)


def fid(n):
    return f"fnd_{n:016d}"


def mid(n):
    return f"legistar-examplecity-{n}"


class FindingMeetingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-meetings-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.findings = self.root / "content" / "findings"
        self.meetings = self.root / "content" / "meetings"
        self.template = json.loads((self.findings / "cedar-policy-posted.json").read_text(encoding="utf-8"))
        (self.findings / "cedar-policy-posted.json").unlink()
        (self.meetings / "2026-10-cedar-council.json").unlink()
        (self.root / "kit").mkdir(exist_ok=True)
        self.kit = []

    def add_finding(self, n, event_date="2026-01-05", agency=POLICE):
        document = dict(self.template, id=fid(n), slug=f"syn-{n}", title=f"Synthetic finding {n}",
                        summary=f"Synthetic summary {n}.", event_date=event_date, agency_id=agency,
                        published_at="2026-02-01T18:00:00+00:00", corrections=[])
        (self.findings / f"syn-{n}.json").write_text(json.dumps(document), encoding="utf-8")

    def add_kit(self, n, starts_at="2026-11-18T18:00:00-08:00", body="Example City Council", **extra):
        self.kit.append({"id": mid(n), "body": body, "starts_at": starts_at, **extra})
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": self.kit}),
                                                         encoding="utf-8")

    def write_meeting(self, n, cited=None, **fields):
        document = {"id": mid(n), "body": "Example City Council", "starts_at": "2026-11-18T18:00:00-08:00", **fields}
        if cited is not None:
            document["cited_findings"] = cited
        (self.meetings / f"{mid(n)}.json").write_text(json.dumps(document), encoding="utf-8")

    def stored(self, n):
        return json.loads((self.meetings / f"{mid(n)}.json").read_text(encoding="utf-8"))

    def site(self):
        return Site(self.root, read_config(self.root), load_content(self.root, TODAY), today=TODAY)

    def page(self, n, site=None):
        site = site or self.site()
        finding = next(f for f in site.model["findings"] if f["id"] == fid(n))
        return site.finding_html(finding)["html"]

    def section(self, n):
        found = SECTION.search(self.page(n))
        return found.group(0) if found else None

    def hrefs(self, n):
        section = self.section(n)
        return re.findall(r'href="(/meetings/[^"]+\.html)"', section) if section else []

    # --- staging persists the exact selected ids ---------------------------------------------------------------
    def test_stage_persists_exact_selected_ids(self):
        for n, day in ((1, "2026-01-05"), (2, "2026-03-05"), (3, "2026-05-05"), (4, "2026-07-05")):
            self.add_finding(n, day, agency=POLICE if n != 2 else SHERIFF)
        self.add_kit(1)
        report = stage(self.root, mid(1), finding_ids=[fid(3)])
        self.assertEqual((report["findings"], self.stored(1)["cited_findings"]), ([fid(3)], [fid(3)]))
        report = stage(self.root, mid(1), finding_ids=[fid(3), fid(1), fid(2)], replace=True)
        self.assertEqual(report["findings"], [fid(3), fid(1), fid(2)])
        self.assertEqual(self.stored(1)["cited_findings"], report["findings"])
        # default: the meeting agency's findings first (newest first), then the rest, at most three
        report = stage(self.root, mid(1), agency_id=POLICE, replace=True)
        self.assertEqual(report["findings"], [fid(4), fid(3), fid(1)])
        document = self.stored(1)
        self.assertEqual(document["cited_findings"], report["findings"])
        for n, slug in ((4, "syn-4"), (3, "syn-3"), (1, "syn-1")):  # kit citations agree; the id is not the slug
            self.assertIn(f"/findings/{slug}.html", document["comment_kit_md"])
        self.assertNotIn("/findings/syn-2.html", document["comment_kit_md"])
        self.assertTrue(all(not value.startswith("syn-") for value in document["cited_findings"]))

    def test_zero_findings_omits_field(self):
        self.add_kit(1)
        report = stage(self.root, mid(1))
        self.assertEqual(report["findings_cited"], 0)
        document = self.stored(1)
        self.assertNotIn("cited_findings", document)
        self.assertIn("No published findings yet", document["comment_kit_md"])
        self.assertIsNone(self.section_or_none())

    def section_or_none(self):
        return "".join(self.site().finding_meetings_html(f) for f in self.site().model["findings"]) or None

    def test_replace_replaces_citations_and_preserves_outcome(self):
        for n in (1, 2, 3):
            self.add_finding(n)
        self.add_kit(1, starts_at="2026-09-01T18:00:00-07:00")
        stage(self.root, mid(1), finding_ids=[fid(1), fid(2)])
        outcome = {"result": "approved", "on": "2026-09-02", "source_url": "https://example.invalid/minutes",
                   "verified_by": "Pat Organizer", "verified_at": "2026-09-03"}
        document = self.stored(1)
        document["outcome"] = outcome
        (self.meetings / f"{mid(1)}.json").write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual((len(self.hrefs(1)), len(self.hrefs(2)), len(self.hrefs(3))), (1, 1, 0))
        stage(self.root, mid(1), finding_ids=[fid(3)], replace=True)  # A/B -> C: old reverse links go
        self.assertEqual(self.stored(1)["cited_findings"], [fid(3)])
        self.assertEqual(self.stored(1)["outcome"], outcome)
        self.assertEqual((len(self.hrefs(1)), len(self.hrefs(2)), len(self.hrefs(3))), (0, 0, 1))
        for path in self.findings.glob("*.json"):  # C -> none: the field is omitted, not emptied
            path.unlink()
        stage(self.root, mid(1), replace=True)
        self.assertNotIn("cited_findings", self.stored(1))
        self.assertEqual(self.stored(1)["outcome"], outcome)

    def test_replace_of_legacy_file_and_failures_leave_bytes(self):
        self.add_finding(1)
        self.add_kit(1)
        self.write_meeting(1)  # legacy file without the field
        legacy = (self.meetings / f"{mid(1)}.json").read_bytes()
        with self.assertRaises(MeetingStageError):  # no --replace
            stage(self.root, mid(1), finding_ids=[fid(1)])
        with self.assertRaises(MeetingStageError):  # failed selection
            stage(self.root, mid(1), finding_ids=[fid(9)], replace=True)
        self.assertEqual((self.meetings / f"{mid(1)}.json").read_bytes(), legacy)
        stage(self.root, mid(1), finding_ids=[fid(1)], replace=True)
        self.assertEqual(self.stored(1)["cited_findings"], [fid(1)])

    # --- validation --------------------------------------------------------------------------------------------
    def test_cited_findings_validation(self):
        base = {"body": "B", "starts_at": "2026-11-18T18:00:00-08:00"}
        self.assertNotIn("cited_findings", load_meeting(dict(base), "m.json"))  # legacy model shape
        for ids in (["a"], ["a", "b", "c"], ["fnd_0000000000000001", "x-y_Z", "9" * 64]):
            document = dict(base, cited_findings=ids)
            self.assertEqual(load_meeting(document, "m.json")["cited_findings"], ids)
            self.assertIsNot(load_meeting(document, "m.json")["cited_findings"], ids)
        bad = (None, [], ["a", "b", "c", "d"], ["a", "a"], [True], [1], [{"id": "a"}], ["a", None], "a", 7, {"a": 1},
               ["has space"], [" a"], ["a\n"], ["Ä"], ["a.b"], ["a/b"], [""], ["x" * 65])
        for ids in bad:
            document = dict(base, cited_findings=ids)
            before = json.dumps(document, sort_keys=True)
            with self.assertRaises(ContentError, msg=repr(ids)) as caught:
                load_meeting(document, "m.json")
            self.assertIn("m.json.cited_findings", str(caught.exception))
            self.assertEqual(json.dumps(document, sort_keys=True), before)
        with self.assertRaises(ContentError) as caught:
            load_cited_findings(["ok", "bad id"], "m.cited_findings")
        self.assertIn("m.cited_findings[1]", str(caught.exception))
        with self.assertRaises(ContentError) as caught:
            load_cited_findings(["ok", "ok"], "m.cited_findings")
        self.assertIn("m.cited_findings[1]", str(caught.exception))

    # --- the reverse index -------------------------------------------------------------------------------------
    def test_reverse_index_order_and_links(self):
        for n in (1, 2, 3):
            self.add_finding(n, agency=POLICE if n < 3 else SHERIFF)  # same agency is irrelevant
        # lexical order of starts_at differs from the instants: -08:00 18:00 is 02:00Z next day
        meetings = [(1, "2026-11-10T09:00:00-08:00", [fid(1), fid(2)]),   # 17:00Z Nov 10
                    (2, "2026-11-10T18:00:00+00:00", [fid(1)]),           # 18:00Z Nov 10
                    (3, "2026-11-10T20:00:00-08:00", [fid(1), fid(3)]),   # 04:00Z Nov 11
                    (4, "2026-11-10T10:00:00-08:00", [fid(1)]),           # 18:00Z Nov 10: tie with 2
                    (5, "2026-11-12T08:00:00Z", [fid(2)])]
        shuffled = list(meetings)
        random.Random(7).shuffle(shuffled)
        for n, starts, cited in shuffled:
            self.write_meeting(n, cited, starts_at=starts)
        self.assertEqual(self.hrefs(1), [f"/meetings/{mid(n)}.html" for n in (3, 2, 4, 1)])
        self.assertEqual(self.hrefs(2), [f"/meetings/{mid(n)}.html" for n in (5, 1)])
        self.assertEqual(self.hrefs(3), [f"/meetings/{mid(3)}.html"])
        site = self.site()
        self.assertEqual([m["id"] for m in site.meetings_by_finding[fid(1)]], [mid(n) for n in (3, 2, 4, 1)])
        model = load_content(self.root, TODAY)
        model["meetings"].reverse()  # model order does not matter either
        reversed_site = Site(self.root, read_config(self.root), model, today=TODAY)
        self.assertEqual([m["id"] for m in reversed_site.meetings_by_finding[fid(1)]], [mid(n) for n in (3, 2, 4, 1)])
        build_site(self.root, today=TODAY)
        for target in self.hrefs(1) + self.hrefs(2):
            self.assertTrue((self.root / "public" / target.lstrip("/")).is_file(), target)
        self.assertEqual(self.section(1).count("<li>"), 4)  # each meeting once per finding

    def test_meeting_summary_fields_are_escaped(self):
        self.add_finding(1)
        results = list(OUTCOME_LABELS)
        self.assertEqual(len(results), 6)
        for index, result in enumerate(results, start=1):
            self.write_meeting(index, [fid(1)], starts_at=f"2026-10-{index:02d}T18:30:00-07:00",
                               outcome={"result": result, "on": f"2026-10-{index:02d}",
                                        "source_url": "https://example.invalid/minutes",
                                        "verified_by": "Private Verifier", "verified_at": "2026-10-20"})
        self.write_meeting(7, [fid(1)], body='Board & "Friends" \'n more', agenda_item='Item 1 & "two"',
                           starts_at="2026-12-01T09:05:00+05:30")  # future
        self.write_meeting(8, [fid(1)], status="cancelled", starts_at="2026-11-01T10:00:00Z", body="Called Off")
        section = self.section(1)
        for result in results:
            self.assertIn(f"Outcome: {OUTCOME_LABELS[result]}</li>", section)
        self.assertIn("Board &amp; &quot;Friends&quot; &#x27;n more", section)
        self.assertIn("Item 1 &amp; &quot;two&quot;", section)
        self.assertIn("2026-12-01 09:05 (UTC+05:30)", section)
        self.assertIn('datetime="2026-12-01T09:05:00+05:30"', section)
        self.assertIn("2026-10-02 18:30 (UTC-07:00)", section)
        self.assertIn("2026-11-01 10:00 (UTC+00:00)", section)
        self.assertRegex(section, r'<a href="/meetings/legistar-examplecity-8.html">Cancelled: Called Off</a>')
        self.assertNotIn("Private Verifier", section)
        for forbidden in ("verified", "example.invalid", "<iframe", "<script", "comment"):
            self.assertNotIn(forbidden, section)
        plain = next(li for li in section.split("<li>") if "legistar-examplecity-1.html" in li)
        self.assertNotIn("Agenda item", plain)  # a missing agenda item adds nothing
        self.assertEqual(section.count("<li>"), 8)

    def test_legacy_markdown_is_never_inferred(self):
        self.add_finding(1)
        self.write_meeting(1, None, comment_kit_md="Cite https://example.invalid/findings/syn-1.html and /findings/syn-1.html.")
        self.assertIsNone(self.section(1))
        self.assertNotIn("Discussed at public meetings", self.page(1))
        self.write_meeting(2, [fid(1)])
        (self.findings / "syn-1.json").unlink()  # same id, new slug: the association follows the id
        document = dict(self.template, id=fid(1), slug="syn-renamed", title="Renamed", summary="Renamed.",
                        published_at="2026-02-01T18:00:00+00:00", corrections=[])
        (self.findings / "syn-renamed.json").write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.hrefs(1), [f"/meetings/{mid(2)}.html"])
        document.update(id=fid(2), slug="syn-1", title="Same slug")  # same slug, other id: no association
        (self.findings / "syn-renamed.json").unlink()
        (self.findings / "syn-1.json").write_text(json.dumps(document), encoding="utf-8")
        self.assertIsNone(self.section(2))

    def test_withdrawn_and_unknown_findings_do_not_break_build(self):
        self.add_finding(1)
        self.add_finding(2)
        self.write_meeting(1, [fid(1), fid(2)])
        self.write_meeting(2, [fid(1), "fnd_unknown000000001"])  # syntactically valid, never published
        drafts = self.root / "private" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / f"{fid(9)}.json").write_text(json.dumps(dict(self.template, id=fid(9), state="draft")))
        self.write_meeting(3, [fid(9)])
        (self.root / "private").chmod(0o700)
        findings_module.withdraw(self.root, fid(1), "Synthetic withdrawal reason.")
        build_site(self.root, today=TODAY)
        public = self.root / "public"
        self.assertFalse((public / "findings" / "syn-1.html").exists())
        self.assertEqual(self.hrefs(2), [f"/meetings/{mid(1)}.html"])  # the other cited finding still lists it
        for path in public.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("findings/syn-1.html", text, path.name)
            self.assertNotIn("fnd_unknown000000001", text, path.name)
        self.assertEqual(self.site().meetings_by_finding.keys(), {fid(2)})
        with self.assertRaises(ContentError):  # a draft left in content/findings is still refused
            (self.findings / "syn-2.json").write_text(json.dumps(dict(self.template, id=fid(2), slug="syn-2",
                                                                      state="draft")), encoding="utf-8")
            load_content(self.root)

    def test_unassociated_finding_is_byte_identical(self):
        self.add_finding(1)
        self.add_finding(2)
        self.add_finding(3)
        baseline = {n: self.page(n) for n in (1, 2, 3)}
        self.assertTrue(all("Discussed at public meetings" not in page for page in baseline.values()))
        self.write_meeting(1, None)  # legacy meeting
        self.write_meeting(2, [fid(2)], starts_at="2026-11-02T18:00:00-08:00")  # unrelated to findings 1 and 3
        feed_link = ' · <a href="/meetings.xml">Meetings feed</a>'
        staged = {n: self.page(n) for n in (1, 2, 3)}
        for n in (1, 2, 3):
            self.assertNotIn(feed_link, baseline[n])
            self.assertEqual(staged[n].count(feed_link), 1)
        # Only the shared footer changes for findings with no meeting association.
        self.assertEqual(staged[1].replace(feed_link, "", 1), baseline[1])
        self.assertEqual(staged[3].replace(feed_link, "", 1), baseline[3])
        associated = staged[2]
        self.assertEqual(SECTION.sub("", associated).replace(feed_link, "", 1), baseline[2])  # additive only
        self.assertEqual(associated.count("Discussed at public meetings"), 1)
        notice_at = associated.index('<p class="notice">')
        self.assertLess(associated.index('id="discussed-at-public-meetings"'), notice_at)
        self.assertLess(associated.index('href="/corrections.html#request"'), associated.index("discussed-at-public"))
        self.assertEqual(self.site().finding_meetings_html(next(f for f in self.site().model["findings"]
                                                              if f["id"] == fid(1))), "")


if __name__ == "__main__":
    unittest.main()
