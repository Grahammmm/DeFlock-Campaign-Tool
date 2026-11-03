"""DF1822-02: `finding preview DRAFT --directory D --out DIR`. Synthetic example campaign only.

Run from the repository root: python3 -m unittest tests.test_finding_preview -v
"""
import hashlib
import html
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law  # noqa: E402
from campaign_tool.cli import finding_preview  # noqa: E402
from campaign_tool.findings import MAX_DRAFT_BYTES  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
SLUG, DRAFT_ID = "cedar-draft-preview", "fnd_00000000000000d1"
BANNER = '    <p class="draft-banner" role="note">Draft - not published</p>'
NOINDEX = '  <meta name="robots" content="noindex">'
RULE_ID = "ca-civ-1798.90.51-usage-and-privacy-policy"
LIBRARY_TITLES = ("Synthetic Cedar Police ALPR policy", "Synthetic Cedar Police vendor agreement excerpt")


def draft(**changes):
    base = {"id": DRAFT_ID, "title": 'Cedar "draft" policy & retention', "slug": SLUG,
            "summary": "Synthetic summary with 'quotes' & an ampersand.", "body_md": "Synthetic **draft** body.",
            "classification": "documented_fact", "confidence": "verified", "event_date": "2026-01-16",
            "agency_id": "ca-cedar-police", "sources": [{"sha256": "cafe" * 16, "locator": "page:1"},
                                                        {"sha256": "beef" * 16, "locator": "page:1"}],
            "published_at": None, "corrections": [], "state": "draft", "author": "synthetic-author",
            "limitations": ["Synthetic limits."], "counterevidence": ["None found."]}
    base.update(changes)
    return base


def tree_hash(folder):
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(folder).rglob("*") if p.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


class FindingPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-preview-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.draft = self.root / "private" / "drafts" / f"{DRAFT_ID}.json"
        self.draft.parent.mkdir(parents=True)
        self.write_draft(draft())
        self.out = self.root / "private" / "preview"

    def write_draft(self, document):
        self.draft.write_text(json.dumps(document), encoding="utf-8")

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def preview(self, out=None, returncode=0):
        return self.cli("finding", "preview", self.draft, "--directory", self.root, "--out", out or self.out,
                        returncode=returncode)

    def refused(self, needle, out=None):
        result = self.preview(out, returncode=2)
        self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        self.assertIn(needle, result.stderr)
        self.assertFalse(self.out.exists())
        return result

    def page(self):
        return (self.out / "findings" / f"{SLUG}.html").read_text(encoding="utf-8")

    def test_happy_path_banner_noindex_text_and_private_modes(self):
        result = self.preview()
        report = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(report, {"slug": SLUG, "written": [str(self.out / "findings" / f"{SLUG}.html"),
                                                            str(self.out / "style.css")]})
        self.assertIn("public/ was not changed", result.stdout)
        html = self.page()
        self.assertEqual(html.count(BANNER), 1)
        self.assertIn('<main id="top">\n' + BANNER + "\n", html)
        self.assertEqual(html.count(NOINDEX), 1)
        for needle in ("Cedar &quot;draft&quot; policy &amp; retention",
                       "Synthetic summary with &#x27;quotes&#x27; &amp; an ampersand.") + LIBRARY_TITLES:
            self.assertIn(needle, html)
        self.assertNotIn('Cedar "draft"', html)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        self.assertEqual(sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*")),
                         ["findings", f"findings/{SLUG}.html", "style.css"])
        self.assertFalse(self.public.exists())

    def assert_preview_equals_build(self, document):
        """Preview ``document``, then publish the same finding into content/ and build: the pages differ only by
        the banner and noindex lines, and style.css is the build's own."""
        self.write_draft(document)
        finding_preview(self.root, self.draft, self.out, now=NOW)
        preview = self.page()
        build_site(self.root)
        css = (self.public / "style.css").read_text(encoding="utf-8")
        published = dict(document, state="published", published_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
        (self.root / "content" / "findings" / f"{SLUG}.json").write_text(json.dumps(published), encoding="utf-8")
        build_site(self.root)
        built = (self.public / "findings" / f"{SLUG}.html").read_text(encoding="utf-8").splitlines()
        self.assertEqual([line for line in preview.splitlines() if line not in (BANNER, NOINDEX)], built)
        self.assertEqual(len(preview.splitlines()), len(built) + 2)
        self.assertEqual((self.out / "style.css").read_text(encoding="utf-8"), css)
        return preview

    def test_page_equals_build_page_except_banner_and_noindex(self):
        self.assert_preview_equals_build(draft())

    def test_first_rule_citing_draft_shows_citation_and_package_status_as_build_will(self):
        # Review t_608228c8: no published example finding cites a rule, so the law package is read only because
        # the draft itself is in the preview's rendering model. Shipped us-ca package, read-only.
        package = law.load_package("us-ca")
        rule = next(r for r in package["rules"] if r["rule_id"] == RULE_ID)
        status = ("reviewed on " + package["reviewed_at"] if package["status"] == "reviewed"
                  else "draft, not legally reviewed")
        published = json.loads((self.root / "content" / "findings" / "cedar-policy-posted.json").read_text())
        self.assertFalse(published.get("rule_ids") or any(s.get("rule_id") for s in published["sources"]))
        preview = self.assert_preview_equals_build(draft(rule_ids=[RULE_ID]))
        self.assertIn(f"<li><code>{RULE_ID}</code> — {html.escape(rule['citation'])}</li>", preview)
        self.assertIn(f'<p class="notice">Law package status: {html.escape(status)}.</p>', preview)
        self.assertNotIn("citation unavailable", preview)

    def test_draft_replaces_its_published_copy_and_clashes_and_law_problems_are_refused(self):
        published = json.loads((self.root / "content" / "findings" / "cedar-policy-posted.json").read_text())
        self.write_draft(draft(slug=published["slug"]))
        self.refused(f"slug {published['slug']} is already used by finding {published['id']}; build would refuse")
        self.write_draft(draft(rule_ids=["ca-made-up"]))
        self.refused(f"draft findings/{SLUG}: rule ca-made-up is not in the us-ca law package; build would refuse it")
        self.write_draft(dict(published, summary="An edited synthetic summary."))
        self.preview()
        page = (self.out / "findings" / f"{published['slug']}.html").read_text(encoding="utf-8")
        self.assertIn("An edited synthetic summary.", page)
        self.assertNotIn('id="related"', page)  # its published copy is replaced, never listed as related to itself

    def test_public_untouched_and_rebuild_has_no_trace_of_draft(self):
        build_site(self.root)
        before = tree_hash(self.public)
        self.preview()
        self.assertEqual(tree_hash(self.public), before)
        build_site(self.root)
        self.assertEqual(tree_hash(self.public), before)
        for path in [p for p in self.public.rglob("*") if p.is_file()]:
            text = path.read_bytes()
            self.assertNotIn(SLUG.encode(), text, path)
            self.assertNotIn(DRAFT_ID.encode(), text, path)
        for name in ("findings/index.html", "sitemap.xml", "feed.xml", "data/findings.json"):
            self.assertTrue((self.public / name).is_file(), name)

    def test_load_finding_refusals_are_named_and_write_nothing(self):
        cases = [
            ({"confidence": "needs_attorney_review"}, "confidence needs_attorney_review blocks publication"),
            ({"classification": "rumour"}, ".classification: choose one of"),
            ({"sources": []}, "review blockers ['missing_evidence']"),
            ({"classification": "apparent_conflict"},
             "review blockers ['missing_duty', 'missing_exceptions', 'missing_rule_version']"),
            ({"title": "Cedar <b>bold</b> claim"}, ".title: must be plain text without angle brackets"),
            ({"slug": "findings"}, "'findings' is reserved"),
        ]
        for changes, needle in cases:
            with self.subTest(changes=changes):
                self.write_draft(draft(**changes))
                self.refused(needle)
        missing = draft()
        del missing["sources"]
        self.write_draft(missing)
        self.refused("missing field(s) ['sources']")

    def test_out_inside_public_or_content_or_symlink_refused(self):
        build_site(self.root)
        before = tree_hash(self.public)
        content_before = tree_hash(self.root / "content")
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        link = self.base / "link"
        link.symlink_to(elsewhere, target_is_directory=True)
        into_public = self.base / "into-public"
        into_public.symlink_to(self.public, target_is_directory=True)
        for out, needle in ((self.public, "inside public/"), (self.public / "preview", "inside public/"),
                            (self.root / "content", "inside content/"), (self.root / "content" / "x", "inside content/"),
                            (link, "--out is a symlink"), (into_public / "sub", "inside public/")):
            with self.subTest(out=out):
                result = self.preview(out, returncode=2)
                self.assertIn(needle, result.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertFalse((self.public / "preview").exists())
        self.assertFalse((self.root / "content" / "x").exists())
        self.assertEqual(tree_hash(self.public), before)
        self.assertEqual(tree_hash(self.root / "content"), content_before)

    def test_findings_subfolder_symlink_refused(self):
        self.out.mkdir()
        target = self.base / "target"
        target.mkdir()
        (self.out / "findings").symlink_to(target, target_is_directory=True)
        result = self.preview(returncode=2)
        self.assertIn("must be a real folder, not a symlink", result.stderr)
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((self.out / "style.css").exists())

    def test_existing_preview_is_replaced_and_symlinked_file_not_followed(self):
        self.preview()
        outside = self.base / "outside.css"
        outside.write_text("keep", encoding="utf-8")
        (self.out / "style.css").unlink()
        (self.out / "style.css").symlink_to(outside)
        self.write_draft(draft(summary="A second synthetic summary."))
        self.preview()
        self.assertIn("A second synthetic summary.", self.page())
        self.assertFalse((self.out / "style.css").is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_published_state_draft_previews_with_banner(self):
        published = json.loads((self.root / "content" / "findings" / "cedar-policy-posted.json").read_text())
        self.write_draft(published)
        self.preview()
        html = (self.out / "findings" / "cedar-policy-posted.html").read_text(encoding="utf-8")
        self.assertEqual(html.count(BANNER), 1)
        self.assertIn(NOINDEX, html)
        self.assertIn("Published 2026-02-01", html)
        self.assertIn("Corrected the policy date", html)

    def test_load_draft_refusals(self):
        self.draft.write_bytes(b" " * (MAX_DRAFT_BYTES + 1))
        self.refused(f"is larger than {MAX_DRAFT_BYTES} bytes")
        self.draft.write_text("{not json", encoding="utf-8")
        self.refused("draft is not valid JSON")
        real = self.base / "real.json"
        real.write_text(json.dumps(draft()), encoding="utf-8")
        self.draft.unlink()
        self.draft.symlink_to(real)
        self.refused("draft is a symlink; refusing to follow it")


if __name__ == "__main__":
    unittest.main()
