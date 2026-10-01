"""Newsletter draft tests: synthetic manifests only; nothing is sent."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter import UNSUBSCRIBE_PLACEHOLDER, build_draft, write_draft  # noqa: E402
from campaign_tool.newsletter.draft import DraftError  # noqa: E402

MANIFEST = {
    "schema_version": 1,
    "campaign": {"name": "Example County ALPR Records", "base_url": "https://campaign.example.invalid", "county_name": "Example"},
    "since": "2026-09-01T00:00:00Z",
    "findings": [
        {"title": "Policy lacks a retention clause", "summary": "The 2025 policy sets no retention period.",
         "classification": "documented_fact", "confidence": "verified",
         "path": "/findings/policy-lacks-a-retention-clause.html", "published_at": "2026-09-20T00:00:00Z", "corrected": False},
        {"title": "Sharing list omits two agencies", "summary": "The export lists 14 partners; the policy names 12.",
         "classification": "apparent_conflict", "confidence": "likely",
         "path": "/findings/sharing-list-omits-two-agencies.html", "published_at": "2026-09-25T00:00:00Z", "corrected": True},
    ],
    "meetings": [
        {"body": "Example City Council", "starts_at": "2026-10-14T18:00:00-07:00", "agenda_item": "Item 7: ALPR renewal",
         "agenda_url": "https://legistar.example.invalid/a/1", "relevance": "alpr_item"},
    ],
}
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


class NewsletterDraftTests(unittest.TestCase):
    def test_draft_shape_and_content(self):
        draft = build_draft(MANIFEST, now=NOW)
        self.assertEqual(sorted(draft), ["counts", "html", "subject", "text"])
        self.assertEqual(draft["subject"], "Example County ALPR Records: 2 new findings, 1 meeting coming up")
        self.assertEqual(draft["counts"], {"findings": 2, "meetings": 1})
        self.assertIn("Policy lacks a retention clause", draft["html"])
        self.assertIn("(corrected)", draft["html"])
        self.assertIn('href="https://campaign.example.invalid/findings/policy-lacks-a-retention-clause.html"', draft["html"])
        self.assertIn("Example City Council", draft["text"])
        self.assertIn("This is not legal advice.", draft["text"])

    def test_unsubscribe_placeholder_present_once_in_each_version(self):
        draft = build_draft(MANIFEST, now=NOW)
        self.assertEqual(draft["html"].count(UNSUBSCRIBE_PLACEHOLDER), 1)
        self.assertEqual(draft["text"].count(UNSUBSCRIBE_PLACEHOLDER), 1)
        self.assertIn('<a href="{{ unsubscribe }}">Unsubscribe</a>', draft["html"])

    def test_html_has_no_script_and_escapes_markup(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["findings"][0]["summary"] = "Summary with an ampersand & a quote \" inside"
        draft = build_draft(manifest, now=NOW)
        self.assertNotIn("<script", draft["html"].lower())
        self.assertIn("&amp;", draft["html"])
        manifest["findings"][0]["summary"] = "<script>alert(1)</script>"
        with self.assertRaises(DraftError):
            build_draft(manifest, now=NOW)

    def test_no_personal_data(self):
        draft = build_draft(MANIFEST, now=NOW)
        for blob in (draft["html"], draft["text"], draft["subject"]):
            self.assertNotRegex(blob, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
        manifest = copy.deepcopy(MANIFEST)
        manifest["findings"][0]["summary"] = "Contact records@example.invalid for the file."
        with self.assertRaises(DraftError):
            build_draft(manifest, now=NOW)
        manifest = copy.deepcopy(MANIFEST)
        manifest["meetings"][0]["agenda_item"] = "Call 805-555-0100 to speak"
        with self.assertRaises(DraftError):
            build_draft(manifest, now=NOW)

    def test_empty_period_and_relative_links(self):
        manifest = {"schema_version": 1, "campaign": {"name": "Quiet Campaign", "base_url": None}, "since": None,
                    "findings": [], "meetings": []}
        draft = build_draft(manifest, now=NOW)
        self.assertEqual(draft["subject"], "Quiet Campaign: September 2026 update")
        self.assertIn("No new findings were published", draft["text"])
        self.assertIn(UNSUBSCRIBE_PLACEHOLDER, draft["html"])
        manifest["findings"] = [{"title": "T", "summary": "S", "classification": "documented_fact", "confidence": "likely",
                                 "path": "/findings/t.html", "published_at": "2026-09-20T00:00:00Z"}]
        draft = build_draft(manifest, now=NOW)
        self.assertIn("/findings/t.html", draft["text"])
        manifest["findings"][0]["path"] = "/findings/../private/x"
        with self.assertRaises(DraftError):
            build_draft(manifest, now=NOW)
        with self.assertRaises(DraftError):
            build_draft({"schema_version": 2}, now=NOW)

    def test_cli_writes_kit_files(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-newsletter-") as tmp:
            root = Path(tmp) / "campaign"
            subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root), "--name",
                            "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                           capture_output=True, timeout=20)
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(json.dumps(MANIFEST))
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(root),
                                     "--manifest", str(manifest_path)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = json.loads(result.stdout)
            self.assertFalse(out["sent"])
            html = (root / "kit" / "newsletter-draft.html").read_text()
            self.assertIn(UNSUBSCRIBE_PLACEHOLDER, html)
            self.assertTrue((root / "kit" / "newsletter-draft.txt").is_file())
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(root)],
                                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Synthetic:", json.loads(result.stdout)["subject"])
            paths = write_draft(root, build_draft(MANIFEST, now=NOW))
            self.assertEqual(len(paths), 3)


if __name__ == "__main__":
    unittest.main()
