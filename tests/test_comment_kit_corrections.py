"""DF1737-07: a cited finding with corrections says so in the comment kit; doctor names stale staged kits.

Synthetic only: temp copies of examples/fictional-campaign and fixture findings; nothing is fetched or sent.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.meeting_stage import stage, stale_kits
from campaign_tool.meetings import comment_kit_md

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://campaign.example.invalid"
MEETING = {"body": "Example City Council", "starts_at": "2026-10-14T18:00:00-07:00", "agenda_item": "Item 7",
           "relevance": "alpr_item"}
MID = "legistar-examplecity-2001"
SLUG = "cedar-policy-posted"
TWO = [{"date": "2026-09-01", "note": "First fix."}, {"date": "2026-10-15", "note": "Second fix."}]

# comment_kit_md output on origin/main 634d989 for finding("policy") with no corrections.
GOLDEN = (
    "## Public comment: Example City Council, 2026-10-14\nAgenda item: Item 7\n\n## Two-minute comment\n"
    "Good evening. My name is [name] and I live in [city]. I am speaking with Example Campaign about automated "
    "license plate readers.\nWe requested public records and published 1 reviewed finding, each tied to a "
    "document hash:\n- No retention period is set. (documented fact, verified)\nWe are not asking you to take "
    "our word for it. Every claim links to the record it came from. Please read them before this item moves "
    "forward.\n\n## Three asks\n- Continue this item until the public has had at least 30 days with the "
    "written policy, the vendor agreement and the sharing list.\n- Require an annual public audit of searches, "
    "retention and outside-agency sharing, presented at a noticed meeting.\n- Answer in writing which agencies "
    "can query this data today and under what written agreement.\n\n## How to take part\nCheck the posted "
    "agenda for how and when to comment; participation details were not verified.\n\n## Sources\n"
    "- **Policy lacks retention clause** https://campaign.example.invalid/findings/policy.html\n"
    "- Policy, page 3, sha256 abababababababab\n\nFindings describe records and published rules as of the "
    "event date. This is not legal advice.\n")


def finding(slug, corrections=(), summary="No retention period is set.", title="Policy lacks retention clause"):
    return {"title": title, "summary": summary, "classification": "documented_fact", "confidence": "verified",
            "path": f"/findings/{slug}.html", "sources": [{"title": "Policy", "locator": "page 3", "sha256": "ab" * 32}],
            "corrections": list(corrections)}


def bullets(kit):
    return [line for line in kit.split("## Two-minute comment\n", 1)[1].split("\n\n", 1)[0].splitlines()
            if line.startswith("- ")]


def source_links(kit):
    return [line for line in kit.split("## Sources\n", 1)[1].splitlines() if line.startswith("- **")]


class CommentKitCorrectionTests(unittest.TestCase):
    def test_two_corrections_use_the_latest_date_and_link_the_anchor(self):
        kit = comment_kit_md(MEETING, [finding("policy", list(reversed(TWO)))], "Example Campaign", BASE)
        self.assertEqual(bullets(kit), ["- No retention period is set. (documented fact, verified) (corrected 2026-10-15)"])
        self.assertEqual(source_links(kit), [
            f"- **Policy lacks retention clause** {BASE}/findings/policy.html "
            f"(corrected 2026-10-15; corrections: {BASE}/findings/policy.html#corrections)"])
        self.assertNotIn("2026-09-01", kit)
        self.assertNotIn("First fix", kit)  # current summary only, no correction history text

    def test_uncorrected_finding_is_byte_identical_to_main(self):
        self.assertEqual(comment_kit_md(MEETING, [finding("policy")], "Example Campaign", BASE), GOLDEN)
        missing = finding("policy")
        del missing["corrections"]
        self.assertEqual(comment_kit_md(MEETING, [missing], "Example Campaign", BASE), GOLDEN)

    def test_mixed_only_the_corrected_finding_is_marked(self):
        kit = comment_kit_md(MEETING, [finding("a", TWO, summary="A."), finding("b", summary="B.")], "C", BASE)
        self.assertEqual(bullets(kit), ["- A. (documented fact, verified) (corrected 2026-10-15)",
                                        "- B. (documented fact, verified)"])
        links = source_links(kit)
        self.assertTrue(links[0].endswith(f"(corrected 2026-10-15; corrections: {BASE}/findings/a.html#corrections)"))
        self.assertEqual(links[1], f"- **Policy lacks retention clause** {BASE}/findings/b.html")
        self.assertEqual(kit.count("#corrections"), 1)

    def test_relative_and_trailing_slash_links(self):
        relative = comment_kit_md(MEETING, [finding("p", TWO)], "C")
        self.assertIn(" /findings/p.html (corrected 2026-10-15; corrections: /findings/p.html#corrections)", relative)
        slashed = comment_kit_md(MEETING, [finding("p", TWO)], "C", BASE + "/")
        self.assertIn(f"corrections: {BASE}/findings/p.html#corrections)", slashed)
        self.assertNotIn("invalid//", slashed)

    def test_more_than_three_findings_every_corrected_source_is_marked(self):
        findings = [finding(f"f{n}", TWO if n in (1, 4) else (), summary=f"S{n}.") for n in range(5)]
        kit = comment_kit_md(MEETING, findings, "C", BASE)
        self.assertEqual([line.split(" (")[0] for line in bullets(kit)], ["- S0.", "- S1.", "- S2."])
        self.assertEqual(sum("(corrected" in line for line in bullets(kit)), 1)
        marked = [line for line in source_links(kit) if "#corrections" in line]
        self.assertEqual(len(source_links(kit)), 5)
        self.assertEqual([line.split("/findings/")[1].split(".html")[0] for line in marked], ["f1", "f4"])

    def test_angle_brackets_are_cleaned_in_the_new_suffix(self):
        dirty = finding("p", [{"date": "<b>2026-10-15</b>", "note": "x"}], summary="<i>Now</i> fixed.")
        kit = comment_kit_md(MEETING, [dirty], "C", BASE + "/<x>")
        self.assertNotIn("<", kit.split("## Sources")[0])
        self.assertIn("(corrected b2026-10-15/b)", kit)
        self.assertIn("corrections: https://campaign.example.invalid/x/findings/p.html#corrections)", kit)
        self.assertEqual(bullets(kit)[0], "- iNow/i fixed. (documented fact, verified) (corrected b2026-10-15/b)")


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


class StaleKitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-comment-kit-corrections-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": "2026-11-18T18:00:00-08:00"}]}), encoding="utf-8")
        self.finding_path = self.root / "content" / "findings" / f"{SLUG}.json"
        self.meetings = self.root / "content" / "meetings"

    def set_corrections(self, corrections):
        document = json.loads(self.finding_path.read_text(encoding="utf-8"))
        document["corrections"] = corrections
        self.finding_path.write_text(json.dumps(document), encoding="utf-8")

    def doctor(self):
        result = run("doctor", "--directory", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_kit_staged_before_a_correction_is_stale_until_restaged(self):
        self.set_corrections([])
        stage(self.root, MID)
        self.assertEqual(stale_kits(self.root), [])
        self.set_corrections(TWO)
        entry = {"meeting": MID, "finding": SLUG, "latest_correction": "2026-10-15"}
        self.assertEqual(stale_kits(self.root), [entry])
        report = self.doctor()
        self.assertEqual(report["stale_comment_kits"], [entry])
        self.assertIn(f"Re-stage meetings with corrected findings: meetings-stage --id {MID} --replace",
                      report["next_actions"])
        restaged = run("meetings-stage", "--directory", str(self.root), "--id", MID, "--replace")
        self.assertEqual(restaged.returncode, 0, restaged.stderr)
        self.assertEqual(stale_kits(self.root), [])
        kit = json.loads((self.meetings / f"{MID}.json").read_text(encoding="utf-8"))["comment_kit_md"]
        self.assertIn(f"/findings/{SLUG}.html (corrected 2026-10-15; corrections: ", kit)

    def test_an_older_correction_date_or_a_date_elsewhere_is_still_stale(self):
        stage(self.root, MID)  # example finding already corrected 2026-02-03
        self.assertEqual(stale_kits(self.root), [])
        self.set_corrections([{"date": "2026-02-03", "note": "old"}, {"date": "2026-10-15", "note": "new"}])
        self.assertEqual([e["latest_correction"] for e in stale_kits(self.root)], ["2026-10-15"])
        target = self.meetings / f"{MID}.json"
        document = json.loads(target.read_text(encoding="utf-8"))
        document["comment_kit_md"] += "\nThe budget was corrected 2026-10-15 at a prior meeting.\n"
        target.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(len(stale_kits(self.root)), 1)  # wording elsewhere does not clear the finding

    def test_uncited_and_invalid_meetings_give_no_entry(self):
        self.set_corrections(TWO)
        (self.meetings / "broken.json").write_text("{not json", encoding="utf-8")
        (self.meetings / "wrong.json").write_text(json.dumps({"body": "B"}), encoding="utf-8")
        self.assertEqual(stale_kits(self.root), [])  # example meeting cites no finding path
        report = self.doctor()
        self.assertEqual(report["stale_comment_kits"], [])

    def test_entries_sorted_by_meeting_then_finding(self):
        self.set_corrections(TWO)
        kit = "- **A** /findings/zeta.html\n- **B** /findings/" + SLUG + ".html\n"
        for mid in ("m-b", "m-a"):
            (self.meetings / f"{mid}.json").write_text(json.dumps(
                {"id": mid, "body": "B", "starts_at": "2026-11-18T18:00:00-08:00", "comment_kit_md": kit}),
                encoding="utf-8")
        self.assertEqual([(e["meeting"], e["finding"]) for e in stale_kits(self.root)],
                         [("m-a", SLUG), ("m-b", SLUG)])
        actions = [a for a in self.doctor()["next_actions"] if a.startswith("Re-stage")]
        self.assertEqual(actions, ["Re-stage meetings with corrected findings: meetings-stage --id m-a, m-b --replace"])

    def test_clean_campaign_doctor_has_empty_list_and_no_action(self):
        report = self.doctor()
        self.assertEqual(report["stale_comment_kits"], [])
        self.assertFalse([a for a in report["next_actions"] if a.startswith("Re-stage")])
        no_content = Path(self.root.parent) / "bare"
        shutil.copytree(EXAMPLE, no_content, ignore=shutil.ignore_patterns("public", "content"))
        self.assertEqual(stale_kits(no_content), [])


if __name__ == "__main__":
    unittest.main()
