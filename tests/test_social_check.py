"""`social --check`: read-only report of post-draft problems (synthetic fictional content; nothing is posted)."""
import builtins
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.social import SOCIAL_MAX_CHARS, build_social, check_social

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
FIRST, SECOND = "cedar-policy-posted", "cedar-retention-gap"
ID1, ID2 = "fnd_0000000000000001", "fnd_0000000000000002"


def tree(folder):
    """Every entry under ``folder`` (and itself) with its type, mtime and content hash."""
    seen = {}
    for path in [folder, *sorted(folder.rglob("*"))] if os.path.lexists(folder) else []:
        info = os.lstat(path)
        body = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
        seen[str(path)] = (info.st_mode, info.st_mtime_ns, body, os.readlink(path) if path.is_symlink() else None)
    return seen


class SocialCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-social-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.findings = self.root / "content" / "findings"
        self.add(SECOND, ID2)
        self.social = self.root / "kit" / "social"
        self.outside = Path(temporary.name) / "outside"
        self.outside.mkdir()

    def finding(self, slug):
        return json.loads((self.findings / f"{slug}.json").read_text(encoding="utf-8"))

    def save(self, document):
        (self.findings / f"{document['slug']}.json").write_text(json.dumps(document), encoding="utf-8")

    def add(self, slug, fid):
        document = self.finding(FIRST)
        document.update({"id": fid, "slug": slug, "classification": "information_gap", "corrections": [],
                         "title": f"Cedar Police policy gap {slug} (fictional)"})
        self.save(document)

    def correct(self, slug, date):
        document = self.finding(slug)
        document["corrections"].append({"date": date, "note": "Synthetic fix."})
        self.save(document)

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "social", "--directory", str(self.root),
                               *extra], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def check(self):
        result = self.cli("--check")
        self.assertIn(result.returncode, (0, 3), result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 3 if report["problems"] else 0)
        self.assertIs(report["posted"], False)
        self.assertNotIn("DRAFT", result.stdout)
        self.assertNotIn(BASE, result.stdout)
        return report

    def states(self, report):
        return {d["slug"]: (d["state"], d["problems"]) for d in report["drafts"]}

    def body(self, slug, text):
        (self.social / f"{slug}.md").write_text(f"<!-- organizer edit, finding {ID2} -->\n\n{text}\n", encoding="utf-8")

    def test_fresh_run_then_check_is_clean_and_writes_nothing(self):
        self.assertEqual(self.cli().returncode, 0)
        before = tree(self.root / "kit")
        self.assertIn(str(self.social / ".written.json"), before)
        report = self.check()
        self.assertEqual(report, {"findings": 2, "drafts": [{"slug": FIRST, "state": "ok", "problems": []},
                                                            {"slug": SECOND, "state": "ok", "problems": []}],
                                  "stale": [], "problems": 0, "posted": False})
        self.assertEqual(tree(self.root / "kit"), before)
        self.assertFalse((self.root / "kit" / "social.suggested").exists())

    def test_correction_after_run_is_outdated_until_social_reruns(self):
        build_social(self.root)
        self.correct(SECOND, "2026-04-01")
        report = self.check()
        self.assertEqual(self.states(report), {FIRST: ("ok", []), SECOND: ("outdated", ["outdated"])})
        self.assertEqual(report["problems"], 1)
        self.assertNotIn("Corrected", (self.social / f"{SECOND}.md").read_text(encoding="utf-8"))
        build_social(self.root)
        self.assertEqual(self.check()["problems"], 0)

    def test_hand_edits_are_checked_on_the_post_body(self):
        for n, slug in enumerate(["cap-ok", "cap-over", "edit-fine", "no-link", "no-id", "late-fix"], start=3):
            self.add(slug, f"fnd_000000000000000{n}")
        build_social(self.root)
        link = lambda slug: f"{BASE}/findings/{slug}.html"  # noqa: E731
        self.body(SECOND, f"Our own words {link(SECOND)} (finding {ID2})")
        fixed = f" {link('cap-ok')} (finding fnd_0000000000000003)"
        self.body("cap-ok", "x" * (SOCIAL_MAX_CHARS - len(fixed)) + fixed)
        fixed = f" {link('cap-over')} (finding fnd_0000000000000004)"
        self.body("cap-over", "x" * (SOCIAL_MAX_CHARS + 1 - len(fixed)) + fixed)
        self.body("edit-fine", f"Read it: {link('edit-fine')} — finding fnd_0000000000000005")
        self.body("no-link", f"See {link(SECOND)} or {BASE}/findings/ (finding fnd_0000000000000006)")
        (self.social / "no-id.md").write_text(f"<!-- finding fnd_0000000000000007 -->\n\nOnly {link('no-id')}\n",
                                              encoding="utf-8")
        self.body("late-fix", f"Mine {link('late-fix')} (finding fnd_0000000000000008) Corrected 2026-01-01.")
        self.correct("late-fix", "2026-05-01")
        report = self.check()
        self.assertEqual(self.states(report), {
            FIRST: ("ok", []), SECOND: ("edited", []), "cap-ok": ("edited", []), "cap-over": ("edited", ["over_cap"]),
            "edit-fine": ("edited", []), "no-link": ("edited", ["no_link"]), "no-id": ("edited", ["no_id"]),
            "late-fix": ("edited", ["missing_correction"])})
        self.assertEqual(report["problems"], 4)
        self.correct(FIRST, "2026-06-01")
        self.body(FIRST, f"Hand {link(FIRST)} (finding {ID1}) Corrected 2026-02-03.")
        self.assertEqual(self.states(check_social(self.root))[FIRST], ("edited", ["missing_correction"]))
        self.body(FIRST, f"Hand {link(FIRST)} (finding {ID1}) Corrected 2026-06-01.")
        self.assertEqual(self.states(check_social(self.root))[FIRST], ("edited", []))

    def test_missing_symlinked_folder_and_fifo_drafts_are_not_read(self):
        self.add("as-folder", "fnd_0000000000000003")
        self.add("as-fifo", "fnd_0000000000000004")
        build_social(self.root)
        (self.social / f"{FIRST}.md").unlink()
        target = self.outside / "target.md"
        target.write_text("outside words\n", encoding="utf-8")
        (self.social / f"{SECOND}.md").unlink()
        os.symlink(target, self.social / f"{SECOND}.md")
        (self.social / "as-folder.md").unlink()
        (self.social / "as-folder.md").mkdir()
        (self.social / "as-fifo.md").unlink()
        os.mkfifo(self.social / "as-fifo.md")
        real_open, real_os_open = builtins.open, os.open

        def guarded(opener):
            def wrapper(path, *args, **kwargs):
                if isinstance(path, (str, Path)) and os.path.realpath(path) == os.path.realpath(target):
                    raise AssertionError("symlink target opened")
                return opener(path, *args, **kwargs)
            return wrapper
        with mock.patch("builtins.open", guarded(real_open)), mock.patch("io.open", guarded(real_open)), \
                mock.patch("os.open", guarded(real_os_open)):
            report = check_social(self.root)
        self.assertEqual(self.states(report), {
            FIRST: ("missing", ["missing"]), SECOND: ("not_regular", ["not_regular"]),
            "as-folder": ("not_regular", ["not_regular"]), "as-fifo": ("not_regular", ["not_regular"])})
        self.assertEqual((report["problems"], report["stale"]), (4, []))
        self.assertNotIn("outside words", json.dumps(report))
        self.assertEqual(self.check()["problems"], 4)
        self.assertTrue((self.social / f"{SECOND}.md").is_symlink())
        self.assertFalse((self.social / f"{FIRST}.md").exists())

    def test_stale_drafts_listed_and_kept(self):
        build_social(self.root)
        (self.social / "old-slug.md").write_text("old\n", encoding="utf-8")
        os.symlink(self.outside / "gone.md", self.social / "old-link.md")
        (self.social / "a-folder.md").mkdir()
        (self.findings / f"{SECOND}.json").unlink()
        before = tree(self.root / "kit")
        report = self.check()
        self.assertEqual(report["stale"], [SECOND, "old-link", "old-slug"])
        self.assertEqual((report["findings"], report["problems"]), (1, 3))
        self.assertEqual(self.states(report), {FIRST: ("ok", [])})
        self.assertEqual(tree(self.root / "kit"), before)
        self.assertEqual((self.social / "old-slug.md").read_text(encoding="utf-8"), "old\n")

    def test_no_social_folder_is_all_missing_and_creates_nothing(self):
        report = self.check()
        self.assertEqual(self.states(report), {FIRST: ("missing", ["missing"]), SECOND: ("missing", ["missing"])})
        self.assertEqual(report["problems"], 2)
        self.assertFalse((self.root / "kit").exists())
        for slug in (FIRST, SECOND):
            (self.findings / f"{slug}.json").unlink()
        self.assertEqual(self.check(), {"findings": 0, "drafts": [], "stale": [], "problems": 0, "posted": False})
        self.assertFalse((self.root / "kit").exists())

    def test_stops_like_social_without_base_url_content_or_real_folder(self):
        (self.root / "kit").mkdir()
        os.symlink(self.outside, self.social)
        result = self.cli("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: social/ must not be a symlink", result.stderr)
        self.social.unlink()
        site_path = self.root / "content" / "site.json"
        site = json.loads(site_path.read_text(encoding="utf-8"))
        del site["base_url"]
        site_path.write_text(json.dumps(site), encoding="utf-8")
        result = self.cli("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: social drafts need content/site.json base_url", result.stderr)
        self.assertNotIn("posted", result.stdout)
        shutil.rmtree(self.root / "content")
        result = self.cli("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: social drafts need a content/ directory", result.stderr)
        self.assertEqual(list((self.root / "kit").iterdir()), [])

    def test_junk_record_is_empty(self):
        build_social(self.root)
        self.correct(SECOND, "2026-04-01")
        for junk in ("[1, 2]", "not json", '{"x": 1}'):
            (self.social / ".written.json").write_text(junk, encoding="utf-8")
            self.assertEqual(self.states(check_social(self.root)),
                             {FIRST: ("ok", []), SECOND: ("edited", ["missing_correction"])})
            self.assertEqual((self.social / ".written.json").read_text(encoding="utf-8"), junk)

    def test_help_names_check_and_plain_social_still_writes(self):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "social", "--help"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertIn("Report draft problems without writing (exit 3 on", " ".join(result.stdout.split()))
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.rstrip().endswith("Drafts written to kit/social. Nothing was posted."))
        self.assertTrue((self.social / f"{FIRST}.md").is_file())


if __name__ == "__main__":
    unittest.main()
