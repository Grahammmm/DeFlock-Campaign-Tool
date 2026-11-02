"""DF1822-07: the comment kit's "## Written comment" letter when written comment is verified.

Synthetic only: fixture findings and temp copies of examples/fictional-campaign; nothing is fetched or sent.
"""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from campaign_tool.meeting_stage import stage, stale_kits
from campaign_tool.meetings import comment_kit_md
from tests.test_comment_kit_corrections import EXAMPLE, GOLDEN, MEETING, MID, SLUG, TWO, finding

BASE = "https://campaign.example.invalid"
PART = {"source_url": "https://example.invalid/council/rules", "verified_by": "Synthetic Organizer",
        "verified_at": "2026-10-10", "in_person": True, "remote": {"available": True, "how": "Zoom link on agenda"}}
WRITTEN = {"how": "Email clerk@example.invalid or mail 1 Main St", "deadline": "2026-10-14T12:00:00-07:00"}
# comment_kit_md output on origin/main 551800e for PART without written_comment, finding("policy"), "C", x.invalid.
REMOTE_GOLDEN = (
    "## Public comment: Example City Council, 2026-10-14\nAgenda item: Item 7\n\n## Two-minute comment\nGood "
    "evening. My name is [name] and I live in [city]. I am speaking with C about automated license plate readers."
    "\nWe requested public records and published 1 reviewed finding, each tied to a document hash:\n- No "
    "retention period is set. (documented fact, verified)\nWe are not asking you to take our word for it. Every "
    "claim links to the record it came from. Please read them before this item moves forward.\n\n## Three asks\n"
    "- Continue this item until the public has had at least 30 days with the written policy, the vendor agreement "
    "and the sharing list.\n- Require an annual public audit of searches, retention and outside-agency sharing, "
    "presented at a noticed meeting.\n- Answer in writing which agencies can query this data today and under what "
    "written agreement.\n\n## How to take part\n- In person: yes\n- Remote: yes, Zoom link on agenda\nSource: "
    "https://example.invalid/council/rules (checked 2026-10-10)\n\n## Sources\n- **Policy lacks retention clause** "
    "https://x.invalid/findings/policy.html\n- Policy, page 3, sha256 abababababababab\n\nFindings describe records "
    "and published rules as of the event date. This is not legal advice.\n")
ASKS = ["1. Continue this item until the public has had at least 30 days with the written policy, the vendor "
        "agreement and the sharing list.",
        "2. Require an annual public audit of searches, retention and outside-agency sharing, presented at a "
        "noticed meeting.",
        "3. Answer in writing which agencies can query this data today and under what written agreement."]


def kit_for(findings, written=WRITTEN, meeting=MEETING, base: "str | None" = BASE):
    return comment_kit_md(dict(meeting, participation=dict(PART, written_comment=written)), findings, "C", base)


def letter(kit):
    return kit.split("## Written comment\n", 1)[1].split("\n\n", 1)[0].splitlines()


def two_sources(slug, **kw):
    item = finding(slug, **kw)
    item["sources"] = [{"title": "Policy", "locator": "p 3", "sha256": "ab" * 32},
                       {"title": "Contract", "locator": "p 1", "sha256": "0123456789" + "cd" * 27}]
    return item


class WrittenCommentLetterTests(unittest.TestCase):
    def test_verified_written_comment_letter_and_deadline_as_in_how_to_take_part(self):
        kit = kit_for([two_sources("policy")])
        lines = letter(kit)
        self.assertEqual(lines[0], "Send by: 2026-10-14 12:00 (UTC-07:00)")
        self.assertIn("- Written comment: Email clerk@example.invalid or mail 1 Main St, by 2026-10-14 12:00 "
                      "(UTC-07:00)", kit.split("## How to take part\n", 1)[1])
        self.assertEqual(lines[1], "How: Email clerk@example.invalid or mail 1 Main St")
        self.assertEqual(lines[2:4], ["To the Example City Council,", "Re: Item 7"])
        self.assertIn("- Policy lacks retention clause: No retention period is set. (https://campaign.example.invalid"
                      "/findings/policy.html; sources abababababab, 0123456789cd)", lines)
        self.assertEqual(lines[-7:], ["I ask you to:"] + ASKS + ["Sincerely,", "[name]", "[city]"])
        order = [kit.index(h) for h in ("## Three asks", "## Written comment", "## How to take part", "## Sources")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(kit.count("## Written comment\n"), 1)

    def test_no_recipient_or_address_added_beyond_how(self):
        lines = letter(kit_for([finding("policy")]))
        hits = [line for line in lines if "clerk@" in line or "Main St" in line]
        self.assertEqual(hits, ["How: Email clerk@example.invalid or mail 1 Main St"])
        self.assertFalse(any(line.startswith(("To:", "Address", "Dear")) for line in lines))

    def test_null_deadline_says_not_stated(self):
        lines = letter(kit_for([finding("policy")], written={"how": "Email the clerk", "deadline": None}))
        self.assertEqual(lines[0], "Send by: deadline not stated by the source")

    def test_without_written_comment_kit_is_byte_identical_to_main(self):
        self.assertEqual(comment_kit_md(MEETING, [finding("policy")], "Example Campaign", BASE), GOLDEN)
        self.assertEqual(comment_kit_md(dict(MEETING, participation=PART), [finding("policy")], "C",
                                        "https://x.invalid"), REMOTE_GOLDEN)
        part = dict(PART, written_comment=None)
        self.assertEqual(comment_kit_md(dict(MEETING, participation=part), [finding("policy")], "C",
                                        "https://x.invalid"), REMOTE_GOLDEN)
        for meeting in (MEETING, dict(MEETING, participation=None), dict(MEETING, participation={})):
            self.assertNotIn("## Written comment", comment_kit_md(meeting, [finding("p")], "C", BASE))

    def test_no_findings_still_has_asks_and_closing(self):
        kit = kit_for([])
        lines = letter(kit)
        self.assertFalse([line for line in lines if line.startswith("- ")])
        self.assertEqual(lines[-7:], ["I ask you to:"] + ASKS + ["Sincerely,", "[name]", "[city]"])
        self.assertNotIn("Re: ", "\n".join(letter(kit_for([], meeting=dict(MEETING, agenda_item="")))))

    def test_five_long_findings_capped_at_400_words_dropping_from_the_end(self):
        long = " ".join(f"w{i}" for i in range(70)) + "."
        findings = [finding(f"f{n}", summary=f"F{n} {long}") for n in range(5)]
        lines = letter(kit_for(findings))
        bullets = [line for line in lines if line.startswith("- ")]
        self.assertLessEqual(sum(len(line.split()) for line in lines), 400)
        self.assertTrue(0 < len(bullets) < 5)
        self.assertEqual([b.split(": ", 1)[1][:3] for b in bullets], [f"F{n} " for n in range(len(bullets))])
        self.assertEqual(lines[-3:], ["Sincerely,", "[name]", "[city]"])
        short = letter(kit_for([finding(f"s{n}", summary="Short.") for n in range(5)]))
        self.assertEqual(len([line for line in short if line.startswith("- ")]), 5)

    def test_link_relative_without_base_url_and_no_double_slash_with_it(self):
        rel = letter(kit_for([finding("policy")], base=None))
        self.assertIn("(/findings/policy.html; sources abababababab)", "\n".join(rel))
        absolute = "\n".join(letter(kit_for([finding("policy")], base=BASE + "/")))
        self.assertIn("(https://campaign.example.invalid/findings/policy.html;", absolute)
        self.assertNotIn(".invalid//", absolute)

    def test_markup_and_newlines_cleaned(self):
        meeting = dict(MEETING, body="City <b>Council</b>\nEvil", agenda_item="Item\n<script>7")
        item = finding("policy", title="Bad\n<i>title</i>", summary="Sum\nmary <x>")
        kit = kit_for([item], meeting=meeting, written={"how": "Email\n<b>clerk</b>", "deadline": None})
        lines = letter(kit)
        text = "\n".join(lines)
        self.assertNotIn("<", text)
        self.assertEqual(lines[1:4], ["How: Email bclerk/b", "To the City bCouncil/b Evil,", "Re: Item script7"])
        self.assertIn("- Bad ititle/i: Sum mary x (", text)

    def test_corrected_finding_and_stale_kits_unchanged(self):
        kit = kit_for([finding(SLUG, corrections=TWO)])
        self.assertIn("sources abababababab) (corrected 2026-10-15)", "\n".join(letter(kit)))
        with tempfile.TemporaryDirectory(prefix="synthetic-comment-kit-written-") as tmp:
            root, today = Path(tmp) / "campaign", date.today()
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
            (root / "kit").mkdir(exist_ok=True)
            (root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
                {"id": MID, "body": "Example City Council", "agenda_item": "Item 7",
                 "starts_at": f"{today + timedelta(days=5)}T18:00:00-07:00"}]}), encoding="utf-8")
            path = root / "content" / "findings" / f"{SLUG}.json"
            corrected = lambda rows: path.write_text(json.dumps(  # noqa: E731
                dict(json.loads(path.read_text(encoding="utf-8")), corrections=rows)), encoding="utf-8")
            corrected(TWO)
            part = Path(tmp) / "participation.json"
            part.write_text(json.dumps(dict(PART, verified_at=str(today), written_comment=dict(
                WRITTEN, deadline=f"{today + timedelta(days=4)}T12:00:00-07:00"))), encoding="utf-8")
            stage(root, MID, participation=str(part), finding_ids=["fnd_0000000000000001"])  # the SLUG finding
            staged = json.loads((root / "content" / "meetings" / f"{MID}.json").read_text(encoding="utf-8"))
            self.assertIn("(corrected 2026-10-15)", "\n".join(letter(staged["comment_kit_md"])))
            self.assertEqual(stale_kits(root), [])
            corrected(TWO + [{"date": "2026-10-20", "note": "Third fix."}])
            self.assertEqual(stale_kits(root), [{"meeting": MID, "finding": SLUG, "latest_correction": "2026-10-20"}])

    def test_hash_prefixes_are_12_chars_and_no_full_hash(self):
        text = "\n".join(letter(kit_for([two_sources("a"), two_sources("b")])))
        self.assertIsNone(re.search(r"[0-9a-f]{13,}", text))
        found = re.findall(r"sources ([0-9a-f, ]+)\)", text)
        self.assertEqual(found, ["abababababab, 0123456789cd"] * 2)
        self.assertNotIn("ab" * 32, text)


if __name__ == "__main__":
    unittest.main()
