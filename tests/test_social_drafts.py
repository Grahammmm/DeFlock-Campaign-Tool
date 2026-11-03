"""Private social drafts per published finding (synthetic fictional content; nothing is posted)."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.social import SOCIAL_MAX_CHARS, build_social, post_text

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
FIRST, SECOND = "cedar-policy-posted", "cedar-retention-gap"


class SocialDraftTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-social-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.findings = self.root / "content" / "findings"
        second = self.finding(FIRST)
        second.update({"id": "fnd_0000000000000002", "slug": SECOND, "classification": "information_gap",
                       "title": "Cedar Police policy names no audit schedule (fictional)", "corrections": []})
        self.save(second)
        self.social = self.root / "kit" / "social"

    def finding(self, slug):
        return json.loads((self.findings / f"{slug}.json").read_text(encoding="utf-8"))

    def save(self, document):
        (self.findings / f"{document['slug']}.json").write_text(json.dumps(document), encoding="utf-8")

    def edit_site(self, **changes):
        path = self.root / "content" / "site.json"
        site = json.loads(path.read_text(encoding="utf-8"))
        site.update(changes)
        if changes.get("base_url", "") is None:
            del site["base_url"]
        path.write_text(json.dumps(site), encoding="utf-8")

    def cli(self, command="social"):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root)]
                              + (["--check"] if command == "build" else []),
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def post(self, slug):
        lines = (self.social / f"{slug}.md").read_text(encoding="utf-8").split("\n")
        return lines[0], lines[2]

    def test_two_published_findings_two_drafts_within_cap(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout[:result.stdout.rindex("}") + 1])
        self.assertEqual(summary, {"written": [FIRST, SECOND], "kept_edited": [], "suggested": 0,
                                   "stale": [], "posted": False})
        self.assertTrue(result.stdout.rstrip().endswith("Drafts written to kit/social. Nothing was posted."))
        self.assertNotIn('"posted": true', result.stdout)
        self.assertEqual(sorted(p.name for p in self.social.glob("*.md")), [f"{FIRST}.md", f"{SECOND}.md"])
        comment, post = self.post(SECOND)
        self.assertEqual(comment, "<!-- DRAFT: not posted. Review before any use. finding fnd_0000000000000002 -->")
        self.assertEqual(post, "Information gap: Cedar Police policy names no audit schedule (fictional) "
                               f"{BASE}/findings/{SECOND}.html (finding fnd_0000000000000002)")
        comment, post = self.post(FIRST)
        self.assertIn("finding fnd_0000000000000001 -->", comment)
        self.assertTrue(post.startswith("Documented fact: Cedar Police produced a written ALPR usage policy"))
        self.assertIn(f"{BASE}/findings/{FIRST}.html (finding fnd_0000000000000001)", post)
        self.assertLessEqual(len(post), SOCIAL_MAX_CHARS)
        self.assertFalse((self.root / "kit" / "social.suggested").exists())

    def test_long_title_is_shortened_and_fixed_parts_kept(self):
        document = self.finding(SECOND)
        document["title"] = "x" * 400
        self.save(document)
        build_social(self.root)
        _, post = self.post(SECOND)
        self.assertEqual(len(post), SOCIAL_MAX_CHARS)
        self.assertTrue(post.startswith("Information gap: xxx"))
        self.assertIn("x\u2026 " + f"{BASE}/findings/{SECOND}.html (finding fnd_0000000000000002)", post)
        self.assertTrue(post.endswith("(finding fnd_0000000000000002)"))
        short = post_text({"slug": "s", "id": "fnd_1", "title": "Short", "classification": "redaction",
                           "corrections": []}, BASE + "/")
        self.assertEqual(short, f"Redaction: Short {BASE}/findings/s.html (finding fnd_1)")

    def test_fixed_parts_over_cap_stop_and_write_nothing(self):
        self.edit_site(base_url=BASE + "/" + "a" * 260)
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stderr, r"Stopped: finding fnd_000000000000000[12]: post cannot fit 280 characters")
        self.assertFalse(self.social.exists())
        self.assertNotIn("posted", result.stdout)

    def test_corrected_note_uses_latest_date_and_only_when_corrected(self):
        document = self.finding(FIRST)
        document["corrections"] = [{"date": "2026-03-01", "note": "Second fix."},
                                   {"date": "2026-02-03", "note": "First fix."}]
        self.save(document)
        build_social(self.root)
        self.assertTrue(self.post(FIRST)[1].endswith("(finding fnd_0000000000000001) Corrected 2026-03-01."))
        self.assertNotIn("Corrected", self.post(SECOND)[1])
        document["title"] = "y" * 400
        self.save(document)
        build_social(self.root)
        post = self.post(FIRST)[1]
        self.assertEqual(len(post), SOCIAL_MAX_CHARS)
        self.assertTrue(post.endswith("y\u2026 " + f"{BASE}/findings/{FIRST}.html (finding fnd_0000000000000001) "
                                      "Corrected 2026-03-01."))

    def test_missing_content_or_base_url_stops(self):
        self.edit_site(base_url=None)
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: social drafts need content/site.json base_url", result.stderr)
        self.assertFalse(self.social.exists())
        shutil.rmtree(self.root / "content")
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: social drafts need a content/ directory", result.stderr)
        self.assertFalse(self.social.exists())

    def test_rerun_identical_and_organizer_edit_kept(self):
        build_social(self.root)
        before = {p.name: p.read_bytes() for p in self.social.iterdir()}
        summary = build_social(self.root)
        self.assertEqual(summary["written"], [FIRST, SECOND])
        self.assertEqual({p.name: p.read_bytes() for p in self.social.iterdir()}, before)
        record = json.loads((self.social / ".written.json").read_text(encoding="utf-8"))
        self.assertEqual(record[SECOND], hashlib.sha256(before[f"{SECOND}.md"]).hexdigest())
        edited = "<!-- organizer edit -->\n\nMy own words.\n"
        (self.social / f"{FIRST}.md").write_text(edited, encoding="utf-8")
        for _ in range(2):  # a second run still keeps the edit (record keeps the original hash)
            summary = build_social(self.root)
            self.assertEqual((summary["written"], summary["kept_edited"], summary["suggested"]), ([SECOND], [FIRST], 1))
            self.assertEqual((self.social / f"{FIRST}.md").read_text(encoding="utf-8"), edited)
            self.assertEqual((self.root / "kit" / "social.suggested" / f"{FIRST}.md").read_bytes(), before[f"{FIRST}.md"])

    def test_unrecorded_differing_file_is_kept_but_identical_one_is_not_edited(self):
        self.social.mkdir(parents=True)
        (self.social / f"{FIRST}.md").write_text("hand written\n", encoding="utf-8")
        (self.social / ".written.json").write_text("not json", encoding="utf-8")
        summary = build_social(self.root)
        self.assertEqual((summary["kept_edited"], summary["written"]), ([FIRST], [SECOND]))
        self.assertEqual((self.social / f"{FIRST}.md").read_text(encoding="utf-8"), "hand written\n")
        (self.social / ".written.json").unlink()
        self.assertEqual(build_social(self.root)["kept_edited"], [FIRST])

    def test_unpublished_slug_listed_stale_and_kept(self):
        build_social(self.root)
        (self.findings / f"{SECOND}.json").unlink()
        (self.social / "never-published.md").write_text("old\n", encoding="utf-8")
        summary = build_social(self.root)
        self.assertEqual(summary["stale"], [SECOND, "never-published"])
        self.assertEqual(summary["written"], [FIRST])
        self.assertTrue((self.social / f"{SECOND}.md").is_file())
        self.assertEqual((self.social / "never-published.md").read_text(encoding="utf-8"), "old\n")

    def test_symlinked_social_folders_or_draft_are_refused(self):
        outside = Path(tempfile.mkdtemp(prefix="synthetic-social-outside-"))
        self.addCleanup(shutil.rmtree, outside)
        (self.root / "kit").mkdir()
        os.symlink(outside, self.social)
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: social/ must not be a symlink", result.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertNotIn("posted", result.stdout)
        self.social.unlink()
        self.social.mkdir()
        target = outside / "target.md"
        target.write_text("outside\n", encoding="utf-8")
        os.symlink(target, self.social / f"{FIRST}.md")
        os.symlink(outside, self.root / "kit" / "social.suggested")
        with self.assertRaisesRegex(ValueError, "social.suggested/ must not be a symlink"):
            build_social(self.root)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["target.md"])
        (self.root / "kit" / "social.suggested").unlink()
        self.assertEqual(build_social(self.root)["kept_edited"], [FIRST])
        self.assertEqual(target.read_text(encoding="utf-8"), "outside\n")
        self.assertTrue((self.social / f"{FIRST}.md").is_symlink())

    def test_build_never_publishes_social_drafts(self):
        self.assertEqual(self.cli().returncode, 0)
        result = self.cli("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public tree check passed", result.stdout)
        for path in (self.root / "public").rglob("*"):
            self.assertNotIn("social", path.name)
            if path.is_file():
                self.assertNotIn("DRAFT: not posted", path.read_text(encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
