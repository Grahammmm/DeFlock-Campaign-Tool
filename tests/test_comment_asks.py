"""DF2207-07: the campaign's own public-comment asks in staged comment kits.

Synthetic only: temp copies of examples/fictional-campaign and fixture findings; nothing is fetched or sent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import cli
from campaign_tool.content import ContentError, load_comment_asks, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.meeting_outcome import record_outcome
from campaign_tool.meeting_pack import build as pack_build
from campaign_tool.meeting_stage import stage
from campaign_tool.meetings import comment_kit_md
from tests.test_comment_kit_corrections import BASE, EXAMPLE, GOLDEN, MEETING, REPO_ROOT, finding

MID, OTHER = "legistar-examplecity-4001", "legistar-examplecity-4002"
TODAY = date(2026, 10, 12)
PART = {"source_url": "https://example.invalid/council/rules", "verified_by": "Synthetic Organizer",
        "verified_at": "2026-10-10"}
WRITTEN = {"how": "Email the clerk", "deadline": "2026-10-14T12:00:00-07:00"}
ONE = ["Adopt a public ordinance before any renewal."]
TWO = ONE + ["Publish the retention schedule."]
THREE = TWO + ["Report searches by outside agencies each quarter."]
DEFAULT_FIRST = "Continue this item until the public has had at least 30 days"
BAD = [  # (value, position named in the refusal)
    ([""], "[1]"), ([" \u00a0 "], "[1]"), (["ok", "   "], "[2]"), (["x" * 201], "[1]"), (["ok", 5], "[2]"),
    (["ok", None], "[2]"), (["a", "b", "c", "d"], "[4]"), (["a<b>c"], "[1]"), (["tab\there"], "[1]"),
    (["ok", "line\nbreak"], "[2]"), (["cr\rhere"], "[1]"), (["nul\x00x"], "[1]"), (["del\x7fx"], "[1]"),
    (["bidi\u202ex"], "[1]"), (["zero\u200bwidth"], "[1]"), (["sep\u2028x"], "[1]"), (["sep\u2029x"], "[1]"),
    ([], "comment_asks"), (None, "comment_asks"), ("one ask", "comment_asks"), ({"a": "b"}, "comment_asks"),
]


def tree(root):
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(Path(root).rglob("*"))}


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


def section(kit, heading):
    return kit.split(heading + "\n", 1)[1].split("\n\n", 1)[0].splitlines()


def words(n, tag="w"):
    return " ".join(f"{tag}{i}" for i in range(n)) + "."


class AsksCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-comment-asks-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        (self.root / "kit").mkdir()
        self.write_kit()
        self.site_path = self.root / "content" / "site.json"
        self.target = self.root / "content" / "meetings" / f"{MID}.json"

    def write_kit(self):
        meetings = [{"id": i, "body": "Example City Council", "agenda_item": "Item 7: ALPR",
                     "starts_at": "2026-10-14T18:00:00-07:00", "relevance": "alpr_item"} for i in (MID, OTHER)]
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": meetings}),
                                                         encoding="utf-8")

    def set_site(self, asks):
        site = json.loads(self.site_path.read_text(encoding="utf-8"))
        site["comment_asks"] = asks
        self.site_path.write_text(json.dumps(site), encoding="utf-8")

    def part_file(self, **extra):
        path = self.tmp / "participation.json"
        path.write_text(json.dumps({**PART, **extra}), encoding="utf-8")
        return str(path)

    def stage(self, meeting_id=MID, **kwargs):
        return stage(self.root, meeting_id, today=kwargs.pop("today", TODAY), **kwargs)

    def model(self):
        model = load_content(self.root)
        self.assertIsNotNone(model)
        return model or {}

    def kit(self, meeting_id=MID):
        path = self.root / "content" / "meetings" / f"{meeting_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))["comment_kit_md"]


class PrecedenceTests(AsksCase):
    def test_site_and_override_precedence(self):
        for custom, heading in ((ONE, "## One ask"), (TWO, "## Two asks"), (THREE, "## Three asks")):
            self.set_site(custom)
            self.stage(replace=True)
            kit = self.kit()
            self.assertEqual(section(kit, heading), ["- " + ask for ask in custom])  # exact order, nothing added
            self.assertEqual(sum(kit.count(h) for h in ("## One ask", "## Two asks", "## Three asks")), 1)
            self.assertNotIn(DEFAULT_FIRST, kit)  # relevance no longer adds a default
            self.assertNotIn("annual public audit", kit)
        self.set_site(TWO)
        self.stage(asks=["Only this."], replace=True)  # the flag wins as a whole, never concatenated
        self.assertEqual(section(self.kit(), "## One ask"), ["- Only this."])
        self.assertNotIn(TWO[0], self.kit())
        self.stage(OTHER)  # another meeting keeps the site asks
        self.assertEqual(section(self.kit(OTHER), "## Two asks"), ["- " + ask for ask in TWO])
        self.assertEqual(section(self.kit(), "## One ask"), ["- Only this."])  # the first kit is untouched

    def test_no_setting_keeps_default_bytes(self):
        self.stage()
        entry = {**MEETING, "body": "Example City Council", "agenda_item": "Item 7: ALPR"}
        document = json.loads(self.target.read_text(encoding="utf-8"))
        model = self.model()
        cited = [{**f, "path": f"/findings/{f['slug']}.html"} for f in model["findings"]]
        self.assertEqual(document["comment_kit_md"],
                         comment_kit_md(entry, cited, "Cedar County Records Project", base_url=model["site"]["base_url"]))
        self.assertIn("## Three asks\n- " + DEFAULT_FIRST, document["comment_kit_md"])
        self.assertEqual(comment_kit_md(MEETING, [finding("policy")], "Example Campaign", BASE), GOLDEN)
        self.assertEqual(comment_kit_md(MEETING, [finding("policy")], "Example Campaign", BASE, asks=None), GOLDEN)


class InvalidAskTests(AsksCase):
    def test_invalid_ask_names_position_and_writes_nothing(self):
        before = tree(self.root)
        for value, where in BAD:
            with self.subTest(value=repr(value)):
                with self.assertRaises(ContentError) as caught:
                    load_comment_asks(value, "--ask")
                self.assertIn("--ask" + where if where.startswith("[") else "--ask", str(caught.exception))
                if isinstance(value, list) and value and isinstance(value[0], str) and len(value[0]) > 1:
                    self.assertNotIn(value[0], str(caught.exception))  # the text is never echoed
                if isinstance(value, list):
                    with self.assertRaises(ContentError):
                        self.stage(asks=value)
                    self.assertEqual(tree(self.root), before)
                    self.assertFalse(self.target.exists())
        for ok in (["x"], ["é" * 200], ["a", "b", "c"], ["same", "same"]):  # boundaries and duplicates are fine
            self.assertEqual(load_comment_asks(ok, "--ask"), ok)

    def test_cli_refuses_by_position_before_creating_anything(self):
        shutil.rmtree(self.root / "content" / "meetings")
        before = tree(self.root)
        for flags, needle in ((["--ask", ""], "--ask[1]"), (["--ask", "ok", "--ask", "x" * 201], "--ask[2]"),
                              (["--ask", "a", "--ask", "b", "--ask", "c", "--ask", "d"], "--ask[4]"),
                              (["--ask", "bad\nline"], "--ask[1]")):
            result = run("meetings-stage", "--directory", str(self.root), "--id", MID, *flags)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("Stopped:", result.stderr)
            self.assertIn(needle, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(tree(self.root), before)
            self.assertFalse((self.root / "content" / "meetings").exists())

    def test_invalid_site_refuses_even_with_a_valid_flag(self):
        for bad, needle in ((["ok", ""], "site.json.comment_asks[2]"), ([], "site.json.comment_asks"),
                            (None, "site.json.comment_asks"), (["a<b"], "site.json.comment_asks[1]")):
            self.set_site(bad)
            before = tree(self.root)
            with self.assertRaises(ContentError) as caught:
                self.stage(asks=["Fine."])
            self.assertIn(needle, str(caught.exception))
            self.assertEqual(tree(self.root), before)
            self.assertFalse(self.target.exists())


class SpokenTests(AsksCase):
    def spoken_kit(self, minutes, custom, findings):
        meeting = dict(MEETING, participation=dict(PART, speaker_minutes=minutes))
        return comment_kit_md(meeting, findings, "C", BASE, asks=custom)

    def spoken(self, kit):
        head = [l for l in kit.splitlines() if l.startswith("## Spoken comment (")]
        return head, section(kit, head[0]) if head else []

    def test_spoken_keeps_custom_asks_within_budget(self):
        findings = [finding(f"f{n}", summary=words(40, f"long{n}x")) for n in range(3)]
        counts = {}
        for minutes in (1, 3, 10):
            kit = self.spoken_kit(minutes, TWO, findings)
            head, lines = self.spoken(kit)
            budget = 130 * minutes
            self.assertEqual(head, [f"## Spoken comment ({minutes} minute{'s' * (minutes != 1)}, about {budget} words)"])
            at = lines.index("I ask you to:")
            self.assertEqual(lines[at + 1:-1], [f"{n}. {ask}" for n, ask in enumerate(TWO, 1)])  # in order, whole
            self.assertEqual(lines[-1], "Word count: %d of %d." % (sum(len(l.split()) for l in lines[:-1]), budget))
            self.assertLessEqual(sum(len(l.split()) for l in lines[:-1]), budget)
            full = [l for l in section(kit, "## Two-minute comment") if l.startswith("- ")]
            kept = [l for l in lines if l.startswith("- ")]
            self.assertEqual(kept, full[:len(kept)])  # only whole finding bullets, from the end
            counts[minutes] = len(kept)
            if len(kept) < len(full):  # the next bullet would not have fit
                self.assertGreater(sum(len(l.split()) for l in lines[:-1]) + len(full[len(kept)].split()), budget)
        self.assertLess(counts[1], 3)
        self.assertEqual(counts[10], 3)

    def test_oversized_minimal_body_gets_the_advisory(self):
        long_asks = [" ".join(["a"] * 100) for _ in range(3)]  # three 100-word, 199-character asks: 300 words > 130
        kit = self.spoken_kit(1, long_asks, [finding("policy")])
        head, lines = self.spoken(kit)
        self.assertEqual(lines[0], "The speaker limit is too short for this kit; use the first sentence and one ask.")
        self.assertIn("asks section", lines[1])
        self.assertFalse(any(l.startswith("Word count: ") for l in lines))
        self.assertEqual(section(kit, "## Three asks"), ["- " + a for a in long_asks])  # all asks still in the section

    def test_no_speaker_limit_means_no_spoken_section_and_default_is_unchanged(self):
        self.assertNotIn("## Spoken comment", comment_kit_md(MEETING, [finding("policy")], "C", BASE, asks=TWO))
        default = self.spoken_kit(2, None, [finding("policy")])
        _, lines = self.spoken(default)
        self.assertNotIn("I ask you to:", lines)  # default kits keep their old spoken text exactly
        self.assertEqual(lines[:-1], section(default, "## Two-minute comment"))


class WrittenTests(AsksCase):
    def letter(self, custom, findings=None, campaign="C", meeting=MEETING):
        meeting = dict(meeting, participation=dict(PART, written_comment=WRITTEN))
        kit = comment_kit_md(meeting, [finding("policy")] if findings is None else findings, campaign, BASE, asks=custom)
        return kit, section(kit, "## Written comment")

    def test_written_asks_and_counted_fallback(self):
        for custom in (ONE, TWO, THREE):
            kit, lines = self.letter(custom)
            at = lines.index("I ask you to:")
            self.assertEqual(lines[at + 1:at + 1 + len(custom)], [f"{n}. {a}" for n, a in enumerate(custom, 1)])
            self.assertEqual(lines[at + 1 + len(custom):], ["Sincerely,", "[name]", "[city]"])
            self.assertEqual(sum(1 for l in lines if l[:3] in ("1. ", "2. ", "3. ")), len(custom))  # each exactly once
            self.assertNotIn("To the Example City Council,\nDear", kit)  # still no recipient or address
            self.assertLessEqual(sum(len(l.split()) for l in lines), 400)
        corrected = [finding(f"f{n}", corrections=[{"date": "2026-10-15", "note": "Fix."}], summary=words(150, f"s{n}x"))
                     for n in range(3)]
        kit, lines = self.letter(THREE, corrected)  # whole finding bullets go, never an ask; dates survive
        bullets = [l for l in lines if l.startswith("- ")]
        self.assertTrue(0 < len(bullets) < 3)
        self.assertTrue(all("(corrected 2026-10-15)" in b for b in bullets))
        self.assertLessEqual(sum(len(l.split()) for l in lines), 400)
        self.assertEqual([l for l in lines if l[:3] in ("1. ", "2. ", "3. ")], [f"{n}. {a}" for n, a in enumerate(THREE, 1)])

    def test_over_cap_advisory_follows_the_count(self):
        campaign = " ".join(["Name"] * 401)  # the campaign name alone is over the cap
        for custom, said in ((ONE, "hand from the ask above."), (TWO, "hand from the two asks above."),
                             (THREE, "hand from the three asks above.")):
            _, lines = self.letter(custom, campaign=campaign)
            self.assertTrue(lines[-1].endswith(said), lines[-1])
            self.assertFalse(any(l.startswith("I ask you to:") for l in lines))
        _, lines = self.letter(None, campaign=campaign)  # default text is byte-identical to before
        self.assertEqual(lines[-1], "The letter would be over 400 words for this meeting; write a short one by "
                                    "hand from the three asks above.")


class LegalNoteTests(AsksCase):
    def test_legal_flags_are_advisory_and_redacted(self):
        neutral = self.stage(asks=["Pass an ordinance and do not renew the contract without a public hearing."])
        self.assertNotIn("review_flags", neutral)
        flagged = ["Neutral first ask.", "The police department violated SB 34 last year."]
        report = self.stage(asks=flagged, replace=True)
        self.assertEqual(report["review_flags"], ["review-required: --ask[2]: legal-claim wording (violation-verb)"])
        self.assertNotIn("violated", json.dumps(report))
        self.assertIn(flagged[1], self.kit())  # advisory: the kit is written as asked
        self.set_site(flagged)
        report = self.stage(replace=True)  # site asks are scanned at staging with their own positions
        self.assertEqual(report["review_flags"], ["review-required: site.json.comment_asks[2]: legal-claim wording (violation-verb)"])
        self.assertEqual(cli._site_claim_notes(self.root),
                         ["review-required: site.json.comment_asks[2]: legal-claim wording (violation-verb)"])
        self.set_site(ONE)
        self.assertEqual(cli._site_claim_notes(self.root), [])
        self.assertNotIn("review_flags", self.stage(replace=True))

    def test_guard_fault_is_a_note_not_a_crash(self):
        with mock.patch("campaign_tool.legal_claims.legal_claims", side_effect=RuntimeError("boom")):
            report = self.stage(asks=ONE)
        self.assertEqual(report["review_flags"], ["review-required: legal-claim wording (guard-error)"])
        self.assertTrue(self.target.is_file())

    def test_cli_prints_notices_on_stderr_and_json_on_stdout(self):
        result = run("meetings-stage", "--directory", str(self.root), "--id", MID,
                     "--ask", "The department broke the law.", "--ask", "Plain ask.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["review_flags"],
                         ["review-required: --ask[1]: legal-claim wording (broke-the-law)"])
        self.assertEqual(result.stderr.strip(), "review-required: --ask[1]: legal-claim wording (broke-the-law)")
        self.assertNotIn("broke the law", result.stderr)


class ReplaceTests(AsksCase):
    def test_replace_does_not_retain_override_or_change_site(self):
        self.set_site(TWO)
        site_before, kit_before = self.site_path.read_bytes(), (self.root / "kit" / "meetings.json").read_bytes()
        self.stage(asks=["Override."])
        self.assertIn("## One ask", self.kit())
        self.stage(OTHER)
        other_before = self.kit(OTHER)
        with self.assertRaises(Exception) as refused:  # an existing kit still needs --replace
            self.stage(asks=ONE)
        self.assertIn("--replace", str(refused.exception))
        self.assertIn("## One ask", self.kit())
        record_outcome(self.root, MID, "approved", "2026-10-14", "https://example.invalid/minutes", "Synthetic Organizer",
                       today=date(2026, 10, 15))
        outcome = json.loads(self.target.read_text(encoding="utf-8"))["outcome"]
        self.stage(replace=True, today=date(2026, 10, 15))  # no flag now: the site asks apply, yesterday's flag is gone
        document = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(section(document["comment_kit_md"], "## Two asks"), ["- " + a for a in TWO])
        self.assertNotIn("Override.", document["comment_kit_md"])
        self.assertEqual(document["outcome"], outcome)
        self.assertEqual(document["cited_findings"], [f["id"] for f in self.model()["findings"]][:1])
        site = json.loads(self.site_path.read_text(encoding="utf-8"))
        del site["comment_asks"]
        self.site_path.write_text(json.dumps(site), encoding="utf-8")
        self.stage(replace=True, today=date(2026, 10, 15))  # neither: back to the defaults
        self.assertIn("## Three asks\n- " + DEFAULT_FIRST, self.kit())
        self.assertEqual((self.root / "kit" / "meetings.json").read_bytes(), kit_before)
        self.assertEqual(self.kit(OTHER), other_before)
        self.assertEqual(json.loads(site_before), {**json.loads(self.site_path.read_bytes()), "comment_asks": TWO})

    def test_symlinked_target_still_refused(self):
        (self.root / "content" / "meetings").mkdir(exist_ok=True)
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text("{}", encoding="utf-8")
        os.symlink(elsewhere, self.target)
        with self.assertRaises(Exception) as refused:
            self.stage(asks=ONE, replace=True)
        self.assertIn("symlink", str(refused.exception))
        self.assertEqual(elsewhere.read_text(encoding="utf-8"), "{}")


class ParityTests(AsksCase):
    def test_load_site_build_content_check_parity(self):
        self.set_site(["Fine.", "bad\ttab"])
        before = tree(self.root)
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        problems = content_problems(self.root)
        self.assertIn("site.json.comment_asks[2]", str(caught.exception))
        self.assertTrue(any("site.json.comment_asks[2]" in p for p in problems), problems)
        shell = run("content-check", "--directory", str(self.root))
        self.assertEqual(shell.returncode, 3)
        self.assertIn("site.json.comment_asks[2]", shell.stdout + shell.stderr)
        built = run("build", "--directory", str(self.root))
        self.assertNotEqual(built.returncode, 0)
        self.assertIn("site.json.comment_asks[2]", built.stdout + built.stderr)
        self.assertEqual(tree(self.root), before)  # the checks are read-only and the failed build writes nothing
        self.set_site(['Say "no" & wait for the count.'])
        self.assertEqual(content_problems(self.root), [])

    def test_quotes_render_safely_and_pack_matches_the_stored_kit(self):
        ask = 'Say "no" & wait.'
        self.set_site([ask])
        self.stage()
        built = run("build", "--directory", str(self.root))
        self.assertEqual(built.returncode, 0, built.stderr)
        page = (self.root / "public" / "meetings.html").read_text(encoding="utf-8")
        self.assertIn("Say &quot;no&quot; &amp; wait.", page.replace("&#34;", "&quot;").replace("&#x27;", "'"))
        self.assertNotIn(ask, page)  # never raw in the HTML
        packed = pack_build(self.root, MID, 3, allow_past=True)
        self.assertEqual(packed["comment_sent"], None)
        self.assertEqual((self.root / "kit" / "meeting-packs" / MID / "comment.md").read_text(encoding="utf-8"),
                         self.kit())  # the pack copies the staged kit verbatim
        for path in sorted((self.root / "public").rglob("*.json")):  # no new public data shape
            self.assertNotIn("comment_asks", path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
