"""Social drafts per platform, each by its own length rule (synthetic fictional content; nothing is posted)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.social import build_social, post_length

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
FIRST, SECOND = "cedar-policy-posted", "cedar-retention-gap"
SECOND_TAIL = f" {BASE}/findings/{SECOND}.html (finding fnd_0000000000000002)"
CHINESE = "市警察局未公布车牌识别数据保留期限" * 8  # 136 code points, X weight 272


class SocialPlatformTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-social-platform-")
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

    def retitle(self, title):
        document = self.finding(SECOND)
        document["title"] = title
        self.save(document)

    def cli(self, *extra, command="social"):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root),
                               *extra], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def summary(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.rstrip().endswith("Nothing was posted."))
        return json.loads(result.stdout[:result.stdout.rindex("}") + 1])

    def post(self, folder, slug):
        return (folder / f"{slug}.md").read_text(encoding="utf-8").split("\n")[2]

    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.joinpath("kit").rglob("*"))

    def test_default_run_is_legacy_len_cut_without_platform_folders(self):  # edge 1
        self.retitle("x" * 400)
        summary = self.summary(self.cli())
        self.assertEqual(summary, {"written": [FIRST, SECOND], "kept_edited": [], "suggested": 0,
                                   "stale": [], "posted": False})
        post = self.post(self.social, SECOND)
        self.assertEqual(post, "Information gap: " + "x" * 159 + "\u2026" + SECOND_TAIL)
        self.assertEqual(len(post), 280)
        self.assertEqual(self.tree(), ["kit/social", "kit/social/.written.json",
                                       f"kit/social/{FIRST}.md", f"kit/social/{SECOND}.md"])
        self.retitle(CHINESE)  # 256 by len(): whole title on the default even though X would weigh it 344
        build_social(self.root)
        self.assertEqual(self.post(self.social, SECOND), "Information gap: " + CHINESE + SECOND_TAIL)

    def test_post_length_rules(self):  # edge 2
        url = "https://e.example.invalid/" + "p" * 34
        self.assertEqual(len(url), 60)
        self.assertEqual([post_length(url, p) for p in ("x", "mastodon", "bluesky")], [23, 23, 60])
        self.assertEqual([post_length("\u4e2d\u6587", p) for p in ("x", "mastodon", "bluesky")], [4, 2, 2])
        self.assertEqual(post_length("\U0001F600", "x"), 2)
        self.assertEqual(post_length("\u2014 \u2019 \u2032", "x"), 5)  # dash, quote, prime weigh 1
        self.assertEqual(post_length(f"see {url} and http://a.invalid/b", "x"), 4 + 23 + 5 + 23)
        self.assertEqual(post_length(f"see {url} and http://a.invalid/b", "bluesky"), 4 + 60 + 5 + 18)
        self.assertEqual(post_length("plain text", "bluesky"), 10)

    def test_long_chinese_title_cut_on_x_kept_on_mastodon(self):  # edge 3
        self.retitle(CHINESE)
        summary = self.summary(self.cli("--platform", "x", "--platform", "mastodon"))
        self.assertEqual(list(summary["platforms"]), ["x", "mastodon"])
        x_post = self.post(self.social / "x", SECOND)
        self.assertLessEqual(post_length(x_post, "x"), 280)
        self.assertGreater(post_length(x_post, "x"), 278)  # cut no shorter than it has to be
        self.assertTrue(x_post.endswith("\u2026" + SECOND_TAIL))
        self.assertTrue(CHINESE.startswith(x_post[len("Information gap: "):-len(SECOND_TAIL) - 1]))
        mastodon_post = self.post(self.social / "mastodon", SECOND)
        self.assertEqual(mastodon_post, "Information gap: " + CHINESE + SECOND_TAIL)
        self.assertLessEqual(post_length(mastodon_post, "mastodon"), 500)
        self.retitle("x" * 400)  # Latin: X counts the link as 23, so more title fits than the legacy cut
        build_social(self.root, ["x"])
        self.assertEqual(self.post(self.social / "x", SECOND), "Information gap: " + "x" * 206 + "\u2026" + SECOND_TAIL)
        self.assertFalse(list(self.social.glob("*.md")))

    def test_fixed_parts_over_bluesky_limit_stop_every_platform(self):  # edge 4
        site_path = self.root / "content" / "site.json"
        site = json.loads(site_path.read_text(encoding="utf-8"))
        site["base_url"] = BASE + "/" + "a" * 250
        site_path.write_text(json.dumps(site), encoding="utf-8")
        result = self.cli("--platform", "x", "--platform", "mastodon", "--platform", "bluesky")
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stderr, r"Stopped: finding fnd_000000000000000[12]: post cannot fit 300 "
                                        r"characters on bluesky")
        self.assertFalse((self.root / "kit").exists())
        self.assertNotIn("posted", result.stdout)
        summary = build_social(self.root, ["x", "mastodon"])  # the link counts 23 there, so both fit
        self.assertEqual([summary["platforms"][p]["written"] for p in ("x", "mastodon")], [[FIRST, SECOND]] * 2)

    def test_duplicates_collapsed_order_kept_and_edits_kept_per_platform(self):  # edge 5
        args = ("--platform", "bluesky", "--platform", "bluesky", "--platform", "x")
        summary = self.summary(self.cli(*args))
        self.assertEqual(list(summary["platforms"]), ["bluesky", "x"])
        fresh = {"written": [FIRST, SECOND], "kept_edited": [], "suggested": 0, "stale": [], "posted": False}
        self.assertEqual(summary["platforms"], {"bluesky": fresh, "x": fresh})
        bluesky = self.social / "bluesky"
        before = (bluesky / f"{FIRST}.md").read_bytes()
        self.assertEqual(self.summary(self.cli(*args)), summary)  # rerun: same files, still "written"
        self.assertEqual((bluesky / f"{FIRST}.md").read_bytes(), before)
        edited = "<!-- organizer edit -->\n\nMy own bluesky words.\n"
        (bluesky / f"{FIRST}.md").write_text(edited, encoding="utf-8")
        for _ in range(2):
            platforms = self.summary(self.cli(*args))["platforms"]
            self.assertEqual((platforms["bluesky"]["kept_edited"], platforms["bluesky"]["written"]), ([FIRST], [SECOND]))
            self.assertEqual(platforms["x"], fresh)
            self.assertEqual((bluesky / f"{FIRST}.md").read_text(encoding="utf-8"), edited)
            self.assertEqual((self.social / "bluesky.suggested" / f"{FIRST}.md").read_bytes(), before)
        self.assertFalse((self.social / "x.suggested").exists())
        self.assertFalse((self.root / "kit" / "social.suggested").exists())
        self.assertFalse(list(self.social.glob("*.md")))

    def test_unknown_platform_and_check_with_platform_refused(self):
        result = self.cli("--platform", "tiktok")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice: 'tiktok'", result.stderr)
        result = self.cli("--check", "--platform", "x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--check reads every draft folder itself", result.stderr)
        for bad in (["tiktok"], []):
            with self.assertRaisesRegex(ValueError, "unknown platform"):
                build_social(self.root, bad)
        self.assertFalse((self.root / "kit").exists())

    def test_symlinked_platform_folder_refused_and_stale_listed_per_platform(self):  # edge 6
        outside = Path(tempfile.mkdtemp(prefix="synthetic-social-platform-outside-"))
        self.addCleanup(shutil.rmtree, outside)
        self.social.mkdir(parents=True)
        for name in ("bluesky", "mastodon.suggested"):
            os.symlink(outside, self.social / name)
            result = self.cli("--platform", "x", "--platform", name.split(".")[0])
            self.assertEqual(result.returncode, 1)
            self.assertIn(f"Stopped: {name}/ must not be a symlink", result.stderr)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(sorted(p.name for p in self.social.iterdir()), [name])
            (self.social / name).unlink()
        (self.social / "mastodon").mkdir()
        (self.social / "mastodon" / "retired-finding.md").write_text("old\n", encoding="utf-8")
        platforms = build_social(self.root, ["mastodon", "x"])["platforms"]
        self.assertEqual((platforms["mastodon"]["stale"], platforms["x"]["stale"]), (["retired-finding"], []))
        self.assertEqual((self.social / "mastodon" / "retired-finding.md").read_text(encoding="utf-8"), "old\n")
        record = json.loads((self.social / "mastodon" / ".written.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(record), [FIRST, SECOND])
        self.assertEqual(build_social(self.root)["stale"], [])  # subfolders are not slugs of kit/social

    def test_build_never_publishes_platform_drafts(self):  # edge 7
        self.summary(self.cli("--platform", "x", "--platform", "bluesky", "--platform", "mastodon"))
        self.summary(self.cli())
        result = self.cli("--check", command="build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public tree check passed", result.stdout)
        for path in (self.root / "public").rglob("*"):
            self.assertNotIn("social", path.name)
            self.assertFalse({"x", "bluesky", "mastodon"} & set(path.relative_to(self.root / "public").parts[:-1]))
            if path.is_file():
                self.assertNotIn("DRAFT: not posted", path.read_text(encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
