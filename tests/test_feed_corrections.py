"""RF2030-1: a correction updates its feed.xml entry; correction and update dates before publication are refused.

Synthetic content only (a temp copy of the fictional example); no network.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from campaign_tool.content import ContentError
from campaign_tool.site import build_site, finding_updated

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
A = "{http://www.w3.org/2005/Atom}"
NAME = "findings/cedar-policy-posted.json"
PUBLISHED = "2026-02-01T18:00:00+00:00"
BRACKET = "[Documented fact; Verified against the record; event date 2026-01-15] "
# feed.xml built by origin/main (74bb0f8) for the fictional example with its corrections removed.
MAIN_FEED = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">'
    "<title>Cedar County Records Project findings</title><id>https://cedar-records.example.invalid/feed.xml</id>"
    '<link href="https://cedar-records.example.invalid/feed.xml" rel="self" />'
    '<link href="https://cedar-records.example.invalid/" /><updated>2026-02-01T18:00:00+00:00</updated>'
    "<author><name>Cedar County Records Project</name></author><entry>"
    "<title>Cedar Police produced a written ALPR usage policy (fictional)</title>"
    "<id>urn:deflock-finding:fnd_0000000000000001</id>"
    '<link href="https://cedar-records.example.invalid/findings/cedar-policy-posted.html" />'
    "<published>2026-02-01T18:00:00+00:00</published><updated>2026-02-01T18:00:00+00:00</updated>"
    "<summary>" + BRACKET + "In response to a synthetic records request, the fictional Cedar Police Department "
    "produced a two-page usage and privacy policy dated 2026-01-15.</summary>"
    '<category term="documented_fact" /></entry></feed>\n')


def snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def corrections(*dates):
    return [{"date": d, "note": f"Synthetic correction {i}."} for i, d in enumerate(dates)]


class FeedCorrectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-feed-corrections-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.findings = self.root / "content" / "findings"
        self.public = self.root / "public"

    def edit(self, slug="cedar-policy-posted", **changes):
        path = self.findings / f"{slug}.json"
        if not path.exists():  # a new synthetic finding: copy the example and give it its own id and slug
            changes = dict(id="fnd_" + slug.replace("-", "_"), slug=slug, title=f"Synthetic {slug}", **changes)
            path.write_text((self.findings / "cedar-policy-posted.json").read_text(encoding="utf-8"), encoding="utf-8")
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def feed(self, name="feed.xml"):
        build_site(self.root)
        return ET.fromstring((self.public / name).read_text(encoding="utf-8"))

    def entry(self, feed, finding_id="fnd_0000000000000001"):
        return next(e for e in feed.findall(f"{A}entry") if e.findtext(f"{A}id") == "urn:deflock-finding:" + finding_id)

    def assert_refused(self, needle, **changes):
        build_site(self.root)
        before = snapshot(self.public)
        self.edit(**changes)
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(snapshot(self.public), before, "public/ must be unchanged by a refused build")
        return str(caught.exception)

    # 1. no corrections, no updated_at: byte-identical to main
    def test_no_corrections_feed_is_byte_identical_to_main(self):
        self.edit(corrections=[], updated_at=None)
        build_site(self.root)
        self.assertEqual((self.public / "feed.xml").read_text(encoding="utf-8"), MAIN_FEED)

    # 2. updated_at later than publication (regression pin)
    def test_updated_at_later_is_used(self):
        self.edit(corrections=[], updated_at="2026-02-05T09:30:00+00:00")
        feed = self.feed()
        self.assertEqual(self.entry(feed).findtext(f"{A}updated"), "2026-02-05T09:30:00+00:00")
        self.assertEqual(feed.findtext(f"{A}updated"), "2026-02-05T09:30:00+00:00")
        self.assertTrue(self.entry(feed).findtext(f"{A}summary").startswith(BRACKET))

    # 3. correction after updated_at wins; summary leads with it; published/id/link/category unchanged
    def test_correction_after_updated_at_wins(self):
        self.edit(corrections=corrections("2026-02-10"), updated_at="2026-02-05T09:30:00+00:00")
        entry = self.entry(self.feed())
        self.assertEqual(entry.findtext(f"{A}updated"), "2026-02-10T00:00:00+00:00")
        self.assertTrue(entry.findtext(f"{A}summary").startswith("Corrected 2026-02-10. " + BRACKET))
        self.assertEqual(entry.findtext(f"{A}published"), PUBLISHED)
        self.assertEqual(entry.findtext(f"{A}id"), "urn:deflock-finding:fnd_0000000000000001")
        self.assertEqual(entry.find(f"{A}category").get("term"), "documented_fact")

    def test_updated_at_after_correction_keeps_updated_at_and_corrected_lead(self):
        self.edit(corrections=corrections("2026-02-03"), updated_at="2026-02-05T09:30:00-07:00")
        entry = self.entry(self.feed())
        self.assertEqual(entry.findtext(f"{A}updated"), "2026-02-05T09:30:00-07:00")
        self.assertTrue(entry.findtext(f"{A}summary").startswith("Corrected 2026-02-03. " + BRACKET))

    # 4. two corrections out of file order: the latest date in both places
    def test_out_of_order_corrections_use_latest(self):
        self.edit(corrections=corrections("2026-03-05", "2026-02-10"))
        entry = self.entry(self.feed())
        self.assertEqual(entry.findtext(f"{A}updated"), "2026-03-05T00:00:00+00:00")
        summary = entry.findtext(f"{A}summary")
        self.assertTrue(summary.startswith("Corrected 2026-03-05. " + BRACKET))
        self.assertEqual(summary.count("Corrected "), 1)

    # 5. same calendar day as a 23:30-07:00 publication: accepted; published_at (06:30Z next day) is the later moment
    def test_same_local_day_correction_accepted_and_ordered_as_utc(self):
        self.edit(published_at="2026-03-01T23:30:00-07:00", corrections=corrections("2026-03-01"))
        entry = self.entry(self.feed())
        self.assertEqual(entry.findtext(f"{A}updated"), "2026-03-01T23:30:00-07:00")
        self.assertTrue(entry.findtext(f"{A}summary").startswith("Corrected 2026-03-01. "))

    def test_next_day_correction_still_earlier_than_publication_in_utc(self):
        # 2026-03-02T00:00Z is before 2026-03-02T06:30Z (= 23:30-07:00 on 03-01): published_at stays the winner.
        self.edit(published_at="2026-03-01T23:30:00-07:00", corrections=corrections("2026-03-02"))
        entry = self.entry(self.feed())
        self.assertEqual(entry.findtext(f"{A}updated"), "2026-03-01T23:30:00-07:00")
        self.assertTrue(entry.findtext(f"{A}summary").startswith("Corrected 2026-03-02. "))

    def test_updated_at_later_calendar_date_but_earlier_instant_refused(self):
        self.assert_refused(NAME + ".updated_at", published_at="2026-03-01T23:30:00-07:00", corrections=[],
                            updated_at="2026-03-02T00:15:00+00:00")

    # 6. correction dated before publication: refused, path named, public/ unchanged
    def test_correction_day_before_publication_refused(self):
        message = self.assert_refused(NAME + ".corrections[1].date", corrections=corrections("2026-02-03", "2026-01-31"))
        self.assertIn("2026-02-01", message)

    def test_correction_before_local_publication_day_refused_despite_utc_day(self):
        # 2026-03-01T23:30-07:00 is 2026-03-02 in UTC, but its calendar date is 2026-03-01: 2026-02-28 is still refused.
        self.assert_refused(NAME + ".corrections[0].date", published_at="2026-03-01T23:30:00-07:00",
                            corrections=corrections("2026-02-28"))

    def test_correction_before_zoneless_or_z_publication_refused(self):
        for published in ("2026-02-01T18:00:00", "2026-02-01T18:00:00Z"):
            with self.subTest(published=published):
                self.edit(published_at=PUBLISHED, corrections=[])
                self.assert_refused(NAME + ".corrections[0].date", published_at=published,
                                    corrections=corrections("2026-01-31"))

    # 7. updated_at before published_at: refused naming updated_at; the same instant is allowed
    def test_updated_at_before_publication_refused(self):
        message = self.assert_refused(NAME + ".updated_at", updated_at="2026-02-01T10:59:59-07:00")
        self.assertNotIn("corrections", message)

    def test_updated_at_same_instant_in_other_zone_allowed(self):
        self.edit(corrections=[], updated_at="2026-02-01T11:00:00-07:00")
        self.assertEqual(self.entry(self.feed()).findtext(f"{A}updated"), "2026-02-01T11:00:00-07:00")

    # 8. zone-less published_at with a zoned updated_at: no TypeError, ordered as UTC
    def test_zoneless_publication_with_zoned_update(self):
        self.edit(published_at="2026-02-01T18:00:00", corrections=[], updated_at="2026-02-01T12:00:00-07:00")
        self.assertEqual(self.entry(self.feed()).findtext(f"{A}updated"), "2026-02-01T12:00:00-07:00")
        self.assert_refused(NAME + ".updated_at", updated_at="2026-02-01T10:00:00-07:00")

    def test_finding_updated_ties_keep_field_order(self):
        tie = {"published_at": "2026-02-03T00:00:00", "updated_at": "2026-02-03T00:00:00Z",
               "corrections": [{"date": "2026-02-03"}]}
        self.assertEqual(finding_updated(tie), "2026-02-03T00:00:00Z")
        self.assertEqual(finding_updated(dict(tie, updated_at=None)), "2026-02-03T00:00:00")

    # 9. feed-level updated follows the newest finding including a correction; empty campaign keeps the default
    def test_feed_updated_follows_newest_correction(self):
        self.edit(corrections=corrections("2026-04-01"))
        self.edit("newer", published_at="2026-03-01T18:00:00+00:00", corrections=[])
        feed = self.feed()
        self.assertEqual(feed.findtext(f"{A}updated"), "2026-04-01T00:00:00+00:00")
        self.assertEqual(self.entry(feed, "fnd_newer").findtext(f"{A}updated"), "2026-03-01T18:00:00+00:00")
        self.assertTrue(self.entry(feed, "fnd_newer").findtext(f"{A}summary").startswith(BRACKET))
        agency = self.feed("findings/agency/cedar.xml")  # per-agency feed uses the same rules
        self.assertEqual(agency.findtext(f"{A}updated"), "2026-04-01T00:00:00+00:00")
        self.assertEqual(self.entry(agency).findtext(f"{A}updated"), "2026-04-01T00:00:00+00:00")

    def test_empty_campaign_keeps_default_updated(self):
        (self.findings / "cedar-policy-posted.json").unlink()
        feed = self.feed()
        self.assertEqual(feed.findtext(f"{A}updated"), "2000-01-01T00:00:00+00:00")
        self.assertEqual(feed.findall(f"{A}entry"), [])

    # 10. parses and stays one entry per finding (a correction never duplicates an entry)
    def test_one_entry_per_finding(self):
        self.edit(corrections=corrections("2026-02-03", "2026-02-04", "2026-02-04"))
        self.edit("second", published_at="2026-02-02T18:00:00+00:00", corrections=corrections("2026-02-02"))
        ids = [e.findtext(f"{A}id") for e in self.feed().findall(f"{A}entry")]
        self.assertEqual(sorted(ids), ["urn:deflock-finding:fnd_0000000000000001", "urn:deflock-finding:fnd_second"])


if __name__ == "__main__":
    unittest.main()
