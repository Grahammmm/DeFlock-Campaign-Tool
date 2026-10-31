"""Newsletter window tests (RF1300-2): findings since ``since``, upcoming meetings only.

Synthetic manifests only (fictional campaign, ``.invalid`` hosts); nothing is sent.
"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter import build_draft  # noqa: E402
from campaign_tool.newsletter.draft import MAX_MEETINGS, DraftError  # noqa: E402

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
MANIFEST = {
    "schema_version": 1,
    "campaign": {"name": "Window Test Campaign", "base_url": "https://campaign.example.invalid", "county_name": "Example"},
    "since": "2026-09-01T00:00:00Z",
    "findings": [
        {"title": "Policy lacks a retention clause", "summary": "The 2025 policy sets no retention period.",
         "classification": "documented_fact", "confidence": "verified",
         "path": "/findings/retention.html", "published_at": "2026-09-20T00:00:00Z", "corrected": False},
        {"title": "Sharing list omits two agencies", "summary": "The export lists 14 partners; the policy names 12.",
         "classification": "apparent_conflict", "confidence": "likely",
         "path": "/findings/sharing.html", "published_at": "2026-09-25T00:00:00Z", "corrected": True},
    ],
    "meetings": [],
}


def iso(moment):
    return moment.isoformat()


def meeting(body, starts_at, item="ALPR item"):
    return {"body": body, "starts_at": starts_at, "agenda_item": item,
            "agenda_url": "https://legistar.example.invalid/a/1", "relevance": "alpr_item"}


def manifest_with(meetings=None, findings=None, since=MANIFEST["since"]):
    data = copy.deepcopy(MANIFEST)
    if meetings is not None:
        data["meetings"] = meetings
    if findings is not None:
        data["findings"] = findings
    data["since"] = since
    return data


def meeting_lines(text):
    if "## Upcoming meetings" not in text:
        return []
    section = text.split("## Upcoming meetings\n", 1)[1]
    return [line for line in section.split("\n\n", 1)[0].splitlines() if line.startswith("- ")]


class MeetingWindowTests(unittest.TestCase):
    def test_past_meeting_dropped(self):
        draft = build_draft(manifest_with([meeting("Past Council", iso(NOW - timedelta(days=1))),
                                           meeting("Future Council", iso(NOW + timedelta(days=2)))]), now=NOW)
        lines = meeting_lines(draft["text"])
        self.assertEqual(len(lines), 1)
        self.assertIn("Future Council", lines[0])
        self.assertNotIn("Past Council", draft["text"] + draft["html"])
        self.assertEqual(draft["counts"], {"findings": 2, "meetings": 1})
        self.assertEqual(draft["excluded"]["meetings_past"], 1)
        self.assertTrue(draft["subject"].endswith(", 1 meeting coming up"), draft["subject"])

    def test_all_past_omits_section_and_subject_phrase(self):
        draft = build_draft(manifest_with([meeting("Old Council", iso(NOW - timedelta(days=3))),
                                           meeting("Older Council", iso(NOW - timedelta(days=30)))]), now=NOW)
        self.assertNotIn("Upcoming meetings", draft["text"] + draft["html"])
        self.assertNotIn("meeting", draft["subject"])
        self.assertEqual(draft["counts"]["meetings"], 0)
        self.assertEqual(draft["excluded"], {"findings_before_since": 0, "meetings_past": 2})

    def test_meeting_starting_now_is_kept(self):
        draft = build_draft(manifest_with([meeting("Now Council", iso(NOW)),
                                           meeting("Second Ago", iso(NOW - timedelta(seconds=1)))]), now=NOW)
        self.assertEqual(len(meeting_lines(draft["text"])), 1)
        self.assertIn("Now Council", draft["text"])
        self.assertEqual(draft["excluded"]["meetings_past"], 1)

    def test_offsets_compared_as_instants(self):
        # 2026-09-29T20:00-07:00 is 2026-09-30T03:00Z: upcoming although its local date is earlier.
        draft = build_draft(manifest_with([meeting("Pacific Council", "2026-09-29T20:00:00-07:00"),
                                           meeting("Gone Council", "2026-09-30T01:00:00+02:00")]), now=NOW)
        self.assertIn("Pacific Council", draft["text"])
        self.assertNotIn("Gone Council", draft["text"])

    def test_soonest_first_and_ties_keep_manifest_order(self):
        given = [meeting("Twentieth", "2026-10-20T18:00:00Z"), meeting("Fifth", "2026-10-05T18:00:00Z"),
                 meeting("Twelfth A", "2026-10-12T18:00:00Z"), meeting("Twelfth B", "2026-10-12T11:00:00-07:00")]
        lines = meeting_lines(build_draft(manifest_with(given), now=NOW)["text"])
        self.assertEqual([line.split("**")[1] for line in lines], ["Fifth", "Twelfth A", "Twelfth B", "Twentieth"])
        swapped = [given[0], given[1], given[3], given[2]]
        lines = meeting_lines(build_draft(manifest_with(swapped), now=NOW)["text"])
        self.assertEqual([line.split("**")[1] for line in lines], ["Fifth", "Twelfth B", "Twelfth A", "Twentieth"])

    def test_cap_applies_after_the_window(self):
        past = [meeting(f"Past {i}", iso(NOW - timedelta(days=i + 1))) for i in range(10)]
        upcoming = [meeting(f"Next {i}", iso(NOW + timedelta(days=9 - i))) for i in range(9)]
        mixed = [m for pair in zip(past, upcoming) for m in pair] + past[9:]
        draft = build_draft(manifest_with(mixed), now=NOW)
        lines = meeting_lines(draft["text"])
        self.assertEqual(len(lines), MAX_MEETINGS)
        self.assertTrue(all("Next " in line for line in lines))
        self.assertIn("Next 8", lines[0])  # soonest (day +1) first
        self.assertNotIn("Next 0", draft["text"])  # ninth upcoming is cut by the cap
        self.assertEqual(draft["counts"]["meetings"], 9)
        self.assertEqual(draft["excluded"]["meetings_past"], 10)


class FindingWindowTests(unittest.TestCase):
    def test_finding_before_since_dropped(self):
        findings = copy.deepcopy(MANIFEST["findings"])
        findings[0]["published_at"] = "2026-08-31T23:59:59Z"
        draft = build_draft(manifest_with(findings=findings), now=NOW)
        self.assertIn("We published 1 reviewed finding since", draft["text"])
        self.assertNotIn("Policy lacks a retention clause", draft["text"] + draft["html"])
        self.assertEqual(draft["counts"]["findings"], 1)
        self.assertEqual(draft["excluded"]["findings_before_since"], 1)
        self.assertTrue(draft["subject"].startswith("Window Test Campaign: 1 new finding"), draft["subject"])
        self.assertNotIn("new findings", draft["subject"])

    def test_since_null_keeps_everything(self):
        findings = copy.deepcopy(MANIFEST["findings"])
        findings[0]["published_at"] = "2020-01-01T00:00:00Z"
        draft = build_draft(manifest_with(findings=findings, since=None), now=NOW)
        self.assertEqual(draft["counts"]["findings"], 2)
        self.assertEqual(draft["excluded"], {"findings_before_since": 0, "meetings_past": 0})

    def test_all_findings_before_since(self):
        findings = copy.deepcopy(MANIFEST["findings"])
        for finding in findings:
            finding["published_at"] = "2026-08-01T00:00:00Z"
        draft = build_draft(manifest_with(findings=findings), now=NOW)
        self.assertIn("No new findings were published", draft["text"])
        self.assertEqual(draft["subject"], "Window Test Campaign: since 2026-09-01 update")
        self.assertEqual(draft["excluded"]["findings_before_since"], 2)

    def test_published_at_equal_to_since_kept(self):
        findings = copy.deepcopy(MANIFEST["findings"])
        findings[0]["published_at"] = "2026-08-31T17:00:00-07:00"  # the same instant as since
        draft = build_draft(manifest_with(findings=findings), now=NOW)
        self.assertEqual(draft["counts"]["findings"], 2)
        self.assertEqual(draft["excluded"]["findings_before_since"], 0)

    def test_more_line_uses_kept_count(self):
        base = MANIFEST["findings"][1]
        findings = [dict(base, path=f"/findings/f{i}.html", published_at="2026-09-10T00:00:00Z") for i in range(13)]
        findings += [dict(base, path=f"/findings/old{i}.html", published_at="2026-08-10T00:00:00Z") for i in range(5)]
        draft = build_draft(manifest_with(findings=findings), now=NOW)
        self.assertIn("We published 13 reviewed findings", draft["text"])
        self.assertIn("- and 1 more on the site.", draft["text"])
        self.assertNotIn("/findings/old", draft["text"])


class BadEntryTests(unittest.TestCase):
    def assertRefused(self, manifest, message):
        with self.assertRaises(DraftError) as caught:
            build_draft(manifest, now=NOW)
        self.assertIn(message, str(caught.exception))

    def test_non_object_entries_named(self):
        self.assertRefused(manifest_with(findings=["x"]), "findings[0]: must be an object")
        self.assertRefused(manifest_with([5]), "meetings[0]: must be an object")
        later = [meeting("Fine", iso(NOW + timedelta(days=1))), None]
        self.assertRefused(manifest_with(later), "meetings[1]: must be an object")

    def test_missing_or_bad_published_at_named(self):
        findings = copy.deepcopy(MANIFEST["findings"])
        del findings[0]["published_at"]
        self.assertRefused(manifest_with(findings=findings), "findings[0].published_at")
        findings = copy.deepcopy(MANIFEST["findings"])
        findings[1]["published_at"] = "not a date"
        self.assertRefused(manifest_with(findings=findings), "findings[1].published_at: invalid timestamp")
        findings[1]["published_at"] = "2026-09-25T00:00:00"
        self.assertRefused(manifest_with(findings=findings, since=None), "findings[1].published_at: timestamp needs a UTC offset")

    def test_bad_starts_at_named_even_when_past_or_beyond_cap(self):
        self.assertRefused(manifest_with([meeting("No offset", "2026-10-14T18:00:00")]),
                           "meetings[0].starts_at: timestamp needs a UTC offset")
        upcoming = [meeting(f"Next {i}", iso(NOW + timedelta(days=i + 1))) for i in range(MAX_MEETINGS)]
        upcoming.append(meeting("Missing", None))
        self.assertRefused(manifest_with(upcoming), f"meetings[{MAX_MEETINGS}].starts_at")

    def test_naive_since_refused(self):
        self.assertRefused(manifest_with(since="2026-09-01T00:00:00"), "since: timestamp needs a UTC offset")

    def test_naive_now_refused(self):
        with self.assertRaisesRegex(ValueError, "now must be timezone-aware"):
            build_draft(MANIFEST, now=datetime(2026, 9, 30))


class CliTests(unittest.TestCase):
    def run_cli(self, root, manifest_path):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(root),
                               "--manifest", str(manifest_path)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)

    def test_cli_names_bad_entry_without_traceback_or_files(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-newsletter-") as tmp:
            root = Path(tmp) / "campaign"
            subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root), "--name",
                            "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                           capture_output=True, timeout=20)
            manifest_path = Path(tmp) / "manifest.json"
            for meetings, needle in (([5], "Stopped: meetings[0]: must be an object"),
                                     ([meeting("Naive", "2099-10-14T18:00:00")], "Stopped: meetings[0].starts_at")):
                manifest_path.write_text(json.dumps(manifest_with(meetings)))
                result = self.run_cli(root, manifest_path)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertTrue(result.stderr.startswith(needle), result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(sorted(p.name for p in (root / "kit").glob("newsletter-draft.*")), [])

    def test_cli_prints_excluded(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-newsletter-") as tmp:
            root = Path(tmp) / "campaign"
            subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root), "--name",
                            "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                           capture_output=True, timeout=20)
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest_with([meeting("Long Ago", "2000-01-01T00:00:00Z"),
                                                               meeting("Far Ahead", "2099-01-01T00:00:00Z")], since=None)))
            result = self.run_cli(root, manifest_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = json.loads(result.stdout)
            self.assertEqual(out["excluded"], {"findings_before_since": 0, "meetings_past": 1})
            self.assertEqual(out["counts"], {"findings": 2, "meetings": 1})
            self.assertFalse(out["sent"])


if __name__ == "__main__":
    unittest.main()
