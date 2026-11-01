"""newsletter --from-content: the manifest comes from the campaign's own content/. Synthetic; nothing is sent."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.from_content import manifest_from_content  # noqa: E402

SITE = {"schema_version": 1, "tagline": "Synthetic records.", "about_md": "Synthetic campaign.",
        "base_url": "https://synthetic.example.invalid"}
KEYS = {"subject", "counts", "excluded", "written", "sent", "source"}


def finding(slug, published_at, corrections=(), summary=None):
    return {"id": "fnd_" + slug.replace("-", "_"), "title": "Synthetic finding " + slug, "slug": slug,
            "summary": summary or "A synthetic record about " + slug + ".", "classification": "documented_fact",
            "confidence": "verified", "event_date": "2026-01-15",
            "sources": [{"sha256": "ab" * 32, "locator": "page:1", "title": "Synthetic record"}],
            "published_at": published_at, "corrections": list(corrections), "state": "published",
            "author": "synthetic-author", "limitations": ["Synthetic fixture."], "counterevidence": []}


def meeting(body, starts_at, status="scheduled"):
    return {"body": body, "starts_at": starts_at, "agenda_item": "Item for " + body,
            "agenda_url": "https://synthetic.example.invalid/agenda", "status": status}


class FromContentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-content-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(self.root), "--name",
                        "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        self.kit = self.root / "kit"

    def content(self, findings=(), meetings=(), site=SITE):
        content = self.root / "content"
        (content / "findings").mkdir(parents=True, exist_ok=True)
        (content / "meetings").mkdir(exist_ok=True)
        (content / "site.json").write_text(json.dumps(site))
        for item in findings:
            (content / "findings" / (item["slug"] + ".json")).write_text(json.dumps(item))
        for index, item in enumerate(meetings):
            (content / "meetings" / f"meeting-{index}.json").write_text(json.dumps(item))

    def standard(self, site=SITE):
        self.content([finding("cedar-policy", "2026-02-01T18:00:00+00:00",
                              [{"date": "2026-02-03", "note": "Fixed the policy date."}]),
                      finding("oak-retention", "2026-03-10T18:00:00+00:00")],
                     [meeting("Synthetic Council Late", "2099-06-02T18:00:00-07:00"),
                      meeting("Synthetic Council Soon", "2099-05-01T18:00:00-07:00")], site)

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(self.root),
                               *extra], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)

    def ok(self, *extra):
        result = self.cli(*extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), (self.kit / "newsletter-draft.txt").read_text()

    def assert_stopped(self, result, message):
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: " + message, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.kit.exists(), "nothing written")

    def test_findings_with_corrected_flag_and_meetings_soonest_first(self):
        self.standard()
        out, text = self.ok("--from-content")
        self.assertEqual((set(out), out["source"], out["sent"]), (KEYS, "content", False))
        self.assertEqual(out["counts"], {"findings": 2, "meetings": 2})
        self.assertEqual(out["written"], [str(self.kit.resolve() / ("newsletter-draft." + ext))
                                          for ext in ("html", "txt", "json")])
        self.assertIn("**Synthetic finding cedar-policy** (corrected):", text)
        self.assertIn("**Synthetic finding oak-retention**:", text)
        self.assertLess(text.index("Synthetic Council Soon"), text.index("Synthetic Council Late"))

    def test_since_excludes_older_findings(self):
        self.standard()
        out, text = self.ok("--from-content", "--since", "2026-03-01")
        self.assertEqual((out["counts"]["findings"], out["excluded"]["findings_before_since"]), (1, 1))
        self.assertNotIn("cedar-policy", text)
        self.assertIn("since 2026-03-01", text)

    def test_cancelled_meeting_absent_and_past_meeting_excluded_by_window(self):
        self.content([finding("cedar-policy", "2026-02-01T18:00:00+00:00")],
                     [meeting("Synthetic Cancelled", "2099-05-01T18:00:00-07:00", status="cancelled"),
                      meeting("Synthetic Past", "2000-05-01T18:00:00-07:00"),
                      meeting("Synthetic Upcoming", "2099-07-01T18:00:00-07:00")])
        manifest = manifest_from_content(self.root, {"name": "Synthetic", "county": "Synthetic"})
        self.assertEqual([m["body"] for m in manifest["meetings"]], ["Synthetic Past", "Synthetic Upcoming"])
        self.assertTrue(all(m["relevance"] is None for m in manifest["meetings"]))
        out, text = self.ok("--from-content")
        self.assertEqual((out["counts"]["meetings"], out["excluded"]["meetings_past"]), (1, 1))
        self.assertNotIn("Synthetic Cancelled", text)
        self.assertNotIn("Synthetic Past", text)

    def test_links_use_base_url_or_stay_path_only(self):
        self.standard()
        _, text = self.ok("--from-content")
        self.assertIn("(https://synthetic.example.invalid/findings/oak-retention.html)", text)
        self.standard({**SITE, "base_url": None})
        _, text = self.ok("--from-content")
        self.assertIn(" /findings/oak-retention.html", text)
        self.assertNotIn("https://synthetic.example.invalid/findings", text)

    def test_missing_content_directory_stops(self):
        self.assert_stopped(self.cli("--from-content"), "--from-content needs a content/ directory")

    def test_malformed_content_stops(self):
        (self.root / "content").mkdir()
        self.assert_stopped(self.cli("--from-content"), "content/site.json is required")

    def test_email_in_a_summary_stops_naming_the_field(self):
        self.content([finding("cedar-policy", "2026-02-01T18:00:00+00:00",
                              summary="Ask records@synthetic.example.invalid for the file.")])
        self.assert_stopped(self.cli("--from-content"), "findings[0].summary: contains an email address")

    def test_argument_conflicts_exit_2(self):
        self.standard()
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"schema_version": 1, "findings": [], "meetings": []}))
        for extra in (("--from-content", "--manifest", str(manifest)), ("--since", "2026-03-01"),
                      ("--manifest", str(manifest), "--since", "2026-03-01"),
                      ("--from-content", "--since", "2026-13-01"), ("--from-content", "--since", "20260301"),
                      ("--from-content", "--since", "2026-03-01T00:00:00+00:00")):
            with self.subTest(extra=extra):
                result = self.cli(*extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(self.kit.exists())

    def test_more_than_twelve_findings_and_template_source(self):
        self.content([finding(f"synthetic-{n:02d}", f"2026-02-{n + 1:02d}T18:00:00+00:00") for n in range(14)])
        out, text = self.ok("--from-content")
        self.assertEqual(out["counts"]["findings"], 14)
        self.assertEqual(text.count("[Read the finding]"), 12)
        self.assertIn("- and 2 more on the site.", text)
        self.assertNotIn("synthetic-00**", text)  # oldest two are the ones left for the site
        out, _ = self.ok()
        self.assertEqual((set(out), out["source"], out["counts"]), (KEYS, "template", {"findings": 0, "meetings": 0}))


if __name__ == "__main__":
    unittest.main()
