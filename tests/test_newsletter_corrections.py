"""DF1822-04: findings published before ``since`` but corrected after it go under "Corrections".

Synthetic manifests and content only (fictional campaign, ``.invalid`` hosts); nothing is sent.
"""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.draft import DraftError, build_draft, write_draft  # noqa: E402
from campaign_tool.newsletter.from_content import manifest_from_content  # noqa: E402

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
BASE = "https://campaign.example.invalid"
SINCE = "2026-09-01T00:00:00Z"


def finding(slug, published_at, corrections=None, corrected=None):
    item = {"title": "Finding " + slug, "summary": "A synthetic record about " + slug + ".",
            "classification": "documented_fact", "confidence": "verified", "path": f"/findings/{slug}.html",
            "published_at": published_at, "corrected": bool(corrections) if corrected is None else corrected}
    if corrections is not None:
        item["corrections"] = [{"date": d, "note": n} for d, n in corrections]
    return item


def manifest(findings, since=SINCE):
    return {"schema_version": 1, "campaign": {"name": "Example Campaign", "base_url": BASE, "county_name": "Example"},
            "since": since, "findings": findings,
            "meetings": [{"body": "Example Council", "starts_at": "2026-10-06T18:00:00-07:00",
                          "agenda_item": "ALPR item", "agenda_url": BASE + "/agenda", "relevance": None}]}


# Worker-shaped manifest (no "corrections" key anywhere): one kept, one corrected but old.
WORKER = manifest([finding("kept", "2026-09-20T00:00:00Z", corrected=True),
                   finding("old", "2026-08-01T00:00:00Z", corrected=True)])
# Content-shaped manifest with corrections, rendered without `since`.
NO_SINCE = manifest([finding("old", "2026-08-01T00:00:00Z", [("2026-09-05", "Fixed the count.")]),
                     finding("plain", "2026-08-02T00:00:00Z", [])], since=None)
# sha256 of kit/newsletter-draft.{html,txt,json} produced by origin/main's draft.py (302710e1) for these manifests.
GOLDEN = {
    "worker": ("2efdf9124d8853459b4e3ea21686f4b18f107923a479e6f482510737c9299801",
               "685370fbb08a904af047b314cd6e1b5c038e5e51b3b90297ffbc5e278929de21",
               "337c69cdc1cab9d10f84f207c0236b7e8c5e8f913107ca89cedb8a43e1a2e206"),
    "no_since": ("71c1fbe67d56d94ef0f701eb1b9f3cbb21647c8f5b73b5f47ba12716be88dc1c",
                 "f0d338c2fc21235e58d3cd85c0f80e4e1f5b1d60353c9a33dda6f482b4c21f6a",
                 "1870fc6727994c0d0d6c3d5fd029ffaf46572eec750f05b2b96337d2661aa19a"),
}


def file_hashes(data):
    with tempfile.TemporaryDirectory(prefix="synthetic-newsletter-corrections-") as root:
        paths = write_draft(root, build_draft(copy.deepcopy(data), NOW))
        return tuple(hashlib.sha256(p.read_bytes()).hexdigest() for p in paths)


class CorrectionsTests(unittest.TestCase):
    def draft(self, findings, since=SINCE):
        return build_draft(manifest(findings, since), NOW)

    def test_published_before_corrected_after_listed_only_under_corrections(self):
        draft = self.draft([finding("old", "2026-08-01T00:00:00Z", [("2026-09-05", "Fixed the count.")])])
        line = (f"- **Finding old**: corrected 2026-09-05 — Fixed the count. "
                f"([Read the finding]({BASE}/findings/old.html))")
        self.assertIn("## Corrections\n" + line + "\n", draft["text"])
        self.assertNotIn("## New findings", draft["text"])
        self.assertEqual(draft["counts"]["findings"], 0)
        self.assertEqual(draft["subject"], "Example Campaign: since 2026-09-01 update, 1 meeting coming up")
        self.assertEqual((draft["excluded"]["findings_before_since"], draft["corrections_listed"]), (0, 1))
        self.assertIn(f'<a href="{BASE}/findings/old.html"', draft["html"])
        self.assertIn("<h3>Corrections</h3>", draft["html"])

    def test_correction_on_the_since_day_is_listed_and_the_day_before_is_not(self):
        draft = self.draft([finding("edge", "2026-08-01T00:00:00Z", [("2026-09-01", "Same-day fix.")]),
                            finding("prior", "2026-08-01T00:00:00Z", [("2026-08-31", "Day-before fix.")])])
        self.assertIn("corrected 2026-09-01 — Same-day fix.", draft["text"])
        self.assertNotIn("prior", draft["text"])
        self.assertEqual((draft["excluded"]["findings_before_since"], draft["corrections_listed"]), (1, 1))

    def test_corrected_before_since_stays_absent(self):
        draft = self.draft([finding("old", "2026-08-01T00:00:00Z", [("2026-08-05", "Early fix.")])])
        self.assertNotIn("Corrections", draft["text"])
        self.assertNotIn("Finding old", draft["text"])
        self.assertEqual(draft["excluded"]["findings_before_since"], 1)
        self.assertNotIn("corrections_listed", draft)

    def test_published_after_since_stays_new_with_corrected_and_is_not_duplicated(self):
        draft = self.draft([finding("new", "2026-09-10T00:00:00Z", [("2026-09-12", "Fixed a typo.")]),
                            finding("old", "2026-08-01T00:00:00Z", [("2026-09-05", "Fixed the count.")])])
        text = draft["text"]
        self.assertIn("- **Finding new** (corrected):", text)
        self.assertNotIn("Fixed a typo.", text)
        self.assertEqual(text.count("Finding new"), 1)
        self.assertEqual(text.count("Finding old"), 1)
        self.assertLess(text.index("## New findings"), text.index("## Corrections"))
        self.assertTrue(draft["subject"].startswith("Example Campaign: 1 new finding,"))
        self.assertEqual(draft["counts"]["findings"], 1)

    def test_latest_correction_after_since_shown_once_and_findings_ordered(self):
        draft = self.draft([
            finding("multi", "2026-08-01T00:00:00Z", [("2026-08-10", "Before."), ("2026-09-20", "Latest fix."),
                                                      ("2026-09-03", "Middle fix.")]),
            finding("beta", "2026-08-01T00:00:00Z", [("2026-09-03", "Beta fix.")]),
            finding("alpha", "2026-08-01T00:00:00Z", [("2026-09-03", "Alpha fix.")])])
        text = draft["text"]
        self.assertIn("**Finding multi**: corrected 2026-09-20 — Latest fix.", text)
        for absent in ("Before.", "Middle fix.", "2026-08-10"):
            self.assertNotIn(absent, text)
        self.assertEqual(text.count("Finding multi"), 1)
        self.assertLess(text.index("Finding multi"), text.index("Finding alpha"))
        self.assertLess(text.index("Finding alpha"), text.index("Finding beta"))
        self.assertEqual(draft["corrections_listed"], 3)

    def test_without_since_no_corrections_section_and_byte_identical_to_main(self):
        draft = build_draft(copy.deepcopy(NO_SINCE), NOW)
        self.assertNotIn("Corrections\n", draft["text"])
        self.assertNotIn("corrections_listed", draft)
        self.assertEqual(file_hashes(NO_SINCE), GOLDEN["no_since"])

    def test_worker_manifest_without_corrections_is_byte_identical_to_main(self):
        self.assertEqual(file_hashes(WORKER), GOLDEN["worker"])

    def test_malformed_corrections_name_the_index(self):
        good = finding("ok", "2026-09-10T00:00:00Z")
        cases = [("not a list", "findings[1].corrections"),
                 ([{"date": "2026-09-05", "note": "n"}] * 51, "findings[1].corrections"),
                 (["text"], "findings[1].corrections[0]"),
                 ([{"date": "2026-09-05", "note": "n"}, {"date": "2026-13-01", "note": "n"}],
                  "findings[1].corrections[1].date"),
                 ([{"date": "2026-09-05T00:00:00Z", "note": "n"}], "findings[1].corrections[0].date"),
                 ([{"date": 20260905, "note": "n"}], "findings[1].corrections[0].date"),
                 ([{"note": "n"}], "findings[1].corrections[0].date"),
                 ([{"date": "2026-09-05", "note": 7}], "findings[1].corrections[0].note")]
        for since in (SINCE, None):
            for value, label in cases:
                with self.subTest(since=since, label=label, value=str(value)[:40]):
                    bad = finding("bad", "2026-08-01T00:00:00Z")
                    bad["corrections"] = value
                    with self.assertRaises(ValueError) as caught:
                        self.draft([good, bad], since)
                    self.assertIsInstance(caught.exception, DraftError)
                    self.assertTrue(str(caught.exception).startswith(label + ":"), str(caught.exception))
        self.assertEqual(self.draft([good, finding("fifty", "2026-08-01T00:00:00Z",
                                                   [("2026-09-05", "n")] * 50)])["corrections_listed"], 1)

    def test_markup_note_escaped_and_long_note_capped_in_both_outputs(self):
        draft = self.draft([finding("markup", "2026-08-01T00:00:00Z", [("2026-09-05", "Fixed <b>bold</b> & co.")]),
                            finding("long", "2026-08-01T00:00:00Z", [("2026-09-04", "x" * 500)])])
        self.assertIn("Fixed &lt;b&gt;bold&lt;/b&gt; &amp; co.", draft["html"])
        self.assertNotIn("<b>bold", draft["html"])
        self.assertIn("Fixed <b>bold</b> & co.", draft["text"])
        for blob in (draft["text"], draft["html"]):
            run = blob[blob.index("x" * 10):]
            run = run[:run.index(" ")]
            self.assertEqual(len(run), 200)
            self.assertTrue(run.endswith("…"))


class FromContentCorrectionsTests(unittest.TestCase):
    def test_content_finding_corrected_after_since_reaches_corrections(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-corrections-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "campaign"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root), "--name",
                        "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        (root / "content" / "findings").mkdir(parents=True)
        (root / "content" / "site.json").write_text(json.dumps({
            "schema_version": 1, "tagline": "Synthetic records.", "about_md": "Synthetic campaign.",
            "base_url": "https://synthetic.example.invalid"}))
        record = {"id": "fnd_cedar", "title": "Synthetic finding cedar", "slug": "cedar",
                  "summary": "A synthetic record.", "classification": "documented_fact", "confidence": "verified",
                  "event_date": "2026-01-15", "published_at": "2026-02-01T18:00:00+00:00",
                  "sources": [{"sha256": "ab" * 32, "locator": "page:1", "title": "Synthetic record"}],
                  "corrections": [{"date": "2026-02-03", "note": "Early."}, {"date": "2026-03-04", "note": "Late."}],
                  "state": "published", "author": "synthetic-author", "limitations": ["Synthetic."],
                  "counterevidence": []}
        (root / "content" / "findings" / "cedar.json").write_text(json.dumps(record))
        built = manifest_from_content(root, {"name": "Synthetic", "county": "Synthetic"})
        self.assertEqual(built["findings"][0]["corrections"], record["corrections"])
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(root),
                                 "--from-content", "--since", "2026-03-01"], cwd=REPO_ROOT, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual((out["counts"]["findings"], out["excluded"]["findings_before_since"]), (0, 0))
        text = (root / "kit" / "newsletter-draft.txt").read_text()
        self.assertIn("## Corrections\n- **Synthetic finding cedar**: corrected 2026-03-04 — Late. "
                      "([Read the finding](https://synthetic.example.invalid/findings/cedar.html))", text)
        self.assertNotIn("Early.", text)
        self.assertEqual(json.loads((root / "kit" / "newsletter-draft.json").read_text())["corrections_listed"], 1)


if __name__ == "__main__":
    unittest.main()
