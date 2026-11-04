"""DF2207-08: private numbered finding-thread drafts (synthetic only; nothing is posted)."""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from campaign_tool import registry
from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.site import Site, build_site
from campaign_tool.social import PLATFORMS, build_meeting_social, build_social, check_social, post_length
from campaign_tool.social_threads import (build_thread_social, thread_notes, thread_posts, thread_review_notes)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
FID = "fnd_0000000000000001"
SLUG = "cedar-policy-posted"
CAFE, BEEF, PUB, GONE = "cafe" * 16, "beef" * 16, "a1a1" * 16, "d00d" * 16
MARK = f"<!-- DRAFT: not posted. Review before any use. thread finding {FID} -->\n\n"
SEP = "\n\n---\n\n"
LINK = f"{BASE}/findings/{SLUG}.html"


class ThreadCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-social-threads-")
        self.addCleanup(temporary.cleanup)
        self.base_dir = Path(os.path.realpath(temporary.name))
        self.root = self.base_dir / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.path = self.content / "findings" / f"{SLUG}.json"
        self.finding = json.loads(self.path.read_text(encoding="utf-8"))
        self.library = json.loads((self.content / "sources.json").read_text(encoding="utf-8"))
        self.threads = self.root / "kit" / "social" / "threads"

    # -- fixtures --
    def save(self, **changes):
        self.finding.update(changes)
        self.path.write_text(json.dumps(self.finding), encoding="utf-8")

    def add_library(self, sha=PUB, public="https://example.invalid/fixtures/pub", pages=20, fragment="cedar-police-records"):
        self.library.append({"sha256": sha, "title": "Synthetic public record", "agency_id": "ca-cedar-police",
                             "received_at": "2026-01-25T10:00:00+00:00", "public_url": public, "fragment": fragment,
                             "pages": pages})
        (self.content / "sources.json").write_text(json.dumps(self.library), encoding="utf-8")

    def second_finding(self, slug="second-finding", fid="fnd_0000000000000002"):
        other = dict(self.finding, id=fid, slug=slug, title="Second synthetic finding", corrections=[])
        (self.content / "findings" / f"{slug}.json").write_text(json.dumps(other), encoding="utf-8")

    def cite(self, sha, locator="page:1", title="Synthetic record", **extra):
        return {"sha256": sha, "locator": locator, "title": title, **extra}

    def cli(self, *extra, directory=None, env=None):
        args = ["--directory", str(directory or self.root)] if directory != "" else []
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "social", *args, *extra], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=90, env=env)

    def run_thread(self, *extra):
        return self.cli("--thread", "--finding", FID, *extra)

    def read(self, path):
        return Path(path).read_text(encoding="utf-8")

    def snapshot(self, root=None):
        root = Path(root or self.root)
        return {str(p.relative_to(root)): (p.is_symlink() and os.readlink(p) or (p.read_bytes() if p.is_file() else b""),
                                           stat.S_IMODE(p.lstat().st_mode)) for p in sorted((root / "kit").rglob("*"))} \
            if (root / "kit").exists() else {}

    def padded(self, platform, target, key="summary"):
        """Set ``key`` of the finding to filler so post 1 is exactly ``target`` long by ``platform``'s own count."""
        self.save(**{key: "x"})
        fixed = self.size_of_first(platform)
        self.save(**{key: "x" * (target - fixed + 1)})
        self.assertEqual(self.size_of_first(platform), target)

    def size_of_first(self, platform):
        from campaign_tool.social_threads import _bodies, _number
        model = load_content(self.root)
        record_pages = Site(self.root, read_config(self.root), model).record_pages()
        first = _number(_bodies(model["findings"][0], model["sources"], BASE, record_pages))[0]
        return post_length(first, platform) if platform else len(first)


class OrderAndCounters(ThreadCase):
    def test_order_and_exact_countered_posts(self):
        self.add_library()
        self.save(sources=[self.cite(CAFE, "PRIVATE-LOCATOR", "Private title"),
                           self.cite(BEEF, "page:1", "Vendor excerpt", page=1),
                           self.cite(PUB, "section 3", "Public record A"),
                           self.cite(PUB, "page:4", "Public record A", page=4),
                           self.cite(GONE, "page:9", "Uncatalogued title")],
                  limitations=["First limit.", "Second limit."],
                  corrections=[{"date": "2026-02-03", "note": "older note"}, {"date": "2026-02-09", "note": "newest note"},
                               {"date": "2026-02-05", "note": "middle note"}])
        # the example summary is long, so the legacy 280 rule refuses post 1: the same posts go on mastodon (500)
        with self.assertRaisesRegex(ValueError, r"post 1/6"):
            build_thread_social(self.root, FID)
        self.assertFalse((self.root / "kit").exists())
        result = build_thread_social(self.root, FID, ["mastodon"])
        self.assertEqual(result, {"finding": FID, "posts": 6, "posted": False, "threads": {"platforms": {"mastodon": {
            "written": [SLUG], "kept_edited": [], "suggested": 0, "stale": [], "posted": False}}}})
        title, summary = self.finding["title"], self.finding["summary"]
        expected = [f"(1/6) {title}\n{summary}\n{LINK}",
                    f"(2/6) Source: Vendor excerpt\nPage: 1\n{BASE}/sources/beefbeefbeef.html",
                    f"(3/6) Source: Public record A\nLocator: section 3\n{BASE}/sources/a1a1a1a1a1a1.html",
                    f"(4/6) Source: Public record A\nPage: 4\n{BASE}/sources/a1a1a1a1a1a1.html",
                    "(5/6) Limitations:\n- First limit.\n- Second limit.",
                    "(6/6) Correction 2026-02-09: newest note"]
        text = self.read(self.threads / "mastodon" / f"{SLUG}.md")
        self.assertEqual(text, MARK + SEP.join(expected) + "\n")
        for secret in ("PRIVATE-LOCATOR", "Private title", "Uncatalogued title", "page:9", CAFE, BEEF, PUB, GONE):
            self.assertNotIn(secret, text)

    def test_limitations_correction_variants(self):
        self.save(limitations=[], corrections=[])
        posts = self.thread(["x", "bluesky"][1])
        self.assertEqual([p.split(" ", 1)[1] for p in posts[1:]], ["Limitations: None stated."])
        self.assertTrue(posts[0].startswith("(1/2) ") and posts[1].startswith("(2/2) "))
        self.save(limitations=["Only one."],
                  corrections=[{"date": "2026-02-05", "note": "first"}, {"date": "2026-02-03", "note": "older"},
                               {"date": "2026-02-05", "note": "last stored wins"}])
        posts = self.thread("bluesky")
        self.assertEqual(posts[-1], "(3/3) Correction 2026-02-05: last stored wins")
        self.assertEqual(posts[1], "(2/3) Limitations:\n- Only one.")
        self.assertNotIn("older", "".join(posts))
        self.assertNotIn(self.finding["body_md"][:20], "".join(posts))
        self.assertNotIn(self.finding["author"], "".join(posts))

    def thread(self, platform):
        model = load_content(self.root)
        record_pages = Site(self.root, read_config(self.root), model).record_pages()
        return thread_posts(model["findings"][0], model["sources"], BASE, record_pages, platform)

    def test_public_source_boundary_and_record_mapping(self):
        self.add_library()
        self.save(sources=[self.cite(CAFE, "PRIVATE-LOCATOR", "PRIVATE-TITLE"), self.cite(GONE, "GONE-LOCATOR", "GONE-TITLE"),
                           self.cite(BEEF, "page:1", "Public excerpt")], limitations=[], corrections=[])
        build_thread_social(self.root, FID, ["mastodon"])
        text = self.read(self.threads / "mastodon" / f"{SLUG}.md")
        model = load_content(self.root)
        record_url = BASE + Site(self.root, read_config(self.root), model).record_pages()[BEEF]
        self.assertIn(f"Source: Public excerpt\nLocator: page:1\n{record_url}", text)
        self.assertEqual(record_url, f"{BASE}/sources/beefbeefbeef.html")
        for hidden in ("PRIVATE", "GONE-", "fixtures/agreement-excerpt", CAFE, BEEF, GONE, "d00d", "(4/"):
            self.assertNotIn(hidden, text)
        self.assertIn("(3/3) Limitations: None stated.", text)
        # no public source at all: title/summary/link and limitations only
        self.save(sources=[self.cite(CAFE, "PRIVATE-LOCATOR", "PRIVATE-TITLE")])
        self.assertEqual(len(self.thread("mastodon")), 2)

    def test_record_prefix_collision_refuses_and_leaves_bytes(self):
        self.add_library("beefbeefbeef" + "0" * 52, fragment="other-fragment")
        self.save(sources=[self.cite(BEEF, "page:1", "Public excerpt")])
        before = self.snapshot()
        with self.assertRaisesRegex(ContentError, "share record number beefbeefbeef"):
            build_thread_social(self.root, FID, ["mastodon"])
        result = self.run_thread("--platform", "mastodon")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "kit").exists())


class Caps(ThreadCase):
    def test_counter_platform_caps_and_no_truncation(self):
        self.save(title="T", limitations=[], corrections=[], sources=[self.cite(CAFE)])
        for platform in (None, "x", "bluesky", "mastodon"):
            limit = PLATFORMS[platform][0] if platform else 280
            self.padded(platform, limit)  # post 1 is exactly at the cap, counter included
            build_thread_social(self.root, FID, [platform] if platform else None)
            self.padded(platform, limit + 1)
            with self.assertRaisesRegex(ValueError, rf"{FID}: post 1/2 .*limit is {limit} on {platform or 'the legacy'}"):
                build_thread_social(self.root, FID, [platform] if platform else None)
            self.assertIn("x" * 10, self.finding["summary"])  # the stored text is intact; nothing was shortened
        # the counter alone tips a post that would fit without it: the body is exactly 300, the counter adds 6
        self.padded("bluesky", 300)
        self.save(summary=self.finding["summary"] + "y" * 6)
        self.assertEqual(post_length(f"{self.finding['title']}\n{self.finding['summary']}\n{LINK}", "bluesky"), 300)
        with self.assertRaisesRegex(ValueError, "post 1/2"):
            build_thread_social(self.root, FID, ["bluesky"])

    def test_weights_urls_and_unicode(self):
        self.save(title="T", limitations=[], corrections=[], sources=[self.cite(CAFE)], summary="x")
        fixed = self.size_of_first("x")
        self.save(summary="\u6f22" * ((280 - fixed + 1) // 2))  # X weighs these 2; the link is 23
        self.assertLessEqual(self.size_of_first("x"), 280)
        self.assertGreater(len(self.finding["summary"]) * 2 + len(LINK), 280)
        build_thread_social(self.root, FID, ["x"])
        self.save(summary="\u6f22" * ((280 - fixed + 1) // 2 + 1))
        self.assertGreater(self.size_of_first("x"), 280)
        with self.assertRaisesRegex(ValueError, "post 1/2 .*on x"):
            build_thread_social(self.root, FID, ["x"])
        # Mastodon counts the 70-character link as 23, Bluesky counts it in full
        self.save(summary="m" * (500 - post_length(f"(1/2) T\n\n{LINK}", "mastodon")))
        self.assertEqual(self.size_of_first("mastodon"), 500)
        self.assertGreater(self.size_of_first("bluesky"), 500 - 23 + len(LINK) - 1)
        build_thread_social(self.root, FID, ["mastodon"])

    def test_every_long_field_refuses_intact(self):
        self.add_library()
        long = "L" * 400
        base = dict(title=self.finding["title"], summary=self.finding["summary"],
                    sources=[self.cite(PUB, "page:1", "Public", page=1)], limitations=["short"],
                    corrections=[{"date": "2026-02-05", "note": "short"}])
        cases = {"title": ("post 1/4", {"title": long}), "summary": ("post 1/4", {"summary": long}),
                 "source title": ("post 2/4", {"sources": [self.cite(PUB, "page:1", long, page=1)]}),
                 "locator": ("post 2/4", {"sources": [self.cite(PUB, long, "Public")]}),
                 "limitation": ("post 3/4", {"limitations": [long]}),
                 "correction": ("post 4/4", {"corrections": [{"date": "2026-02-05", "note": long}]})}
        for name, (where, change) in cases.items():
            with self.subTest(name):
                self.save(**{**base, **change})
                with self.assertRaisesRegex(ValueError, rf"{FID}: {where} .*limit is 300 on bluesky"):
                    build_thread_social(self.root, FID, ["bluesky"])
                self.assertFalse((self.root / "kit").exists())
        self.save(**base)
        build_thread_social(self.root, FID, ["bluesky"])

    def test_ten_posts_and_all_platform_preflight(self):
        self.add_library()
        sources = [self.cite(PUB, f"page:{n}", f"S{n}", page=n) for n in range(1, 8)]
        self.save(title="T", summary="S", sources=sources, limitations=["l"],
                  corrections=[{"date": "2026-02-05", "note": "c"}])
        self.assertEqual(build_thread_social(self.root, FID, ["x"])["posts"], 10)
        self.assertIn("(10/10) Correction 2026-02-05: c", self.read(self.threads / "x" / f"{SLUG}.md"))
        self.save(sources=sources + [self.cite(PUB, "page:8", "S8", page=8)])
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, rf"{FID}: thread needs 11 posts; at most 10"):
            build_thread_social(self.root, FID, ["x"])
        self.assertEqual(self.snapshot(), before)
        # platform list rules
        self.save(sources=sources[:1])
        for bad in ([], ["tiktok"], ["x", "tiktok"], [""]):
            with self.assertRaisesRegex(ValueError, "unknown platform"):
                build_thread_social(self.root, FID, bad)
        self.assertEqual(list(build_thread_social(self.root, FID, ["x", "x", "bluesky", "x"])["threads"]["platforms"]),
                         ["x", "bluesky"])

    def test_later_platform_failure_writes_nothing(self):
        self.save(title="T", summary="m" * 350, limitations=[], corrections=[], sources=[self.cite(CAFE)])
        build_thread_social(self.root, FID, ["mastodon"])  # fits 500, not 300
        edited = self.threads / "mastodon" / f"{SLUG}.md"
        edited.write_text("my edit\n", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "post 1/2 .*on bluesky"):
            build_thread_social(self.root, FID, ["mastodon", "bluesky"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.threads / "bluesky").exists() or (self.threads / "mastodon.suggested").exists())
        self.assertEqual(self.read(edited), "my edit\n")
        fresh = self.base_dir / "fresh"
        shutil.copytree(self.root, fresh, ignore=shutil.ignore_patterns("kit"))
        with self.assertRaises(ValueError):
            build_thread_social(fresh, FID, ["mastodon", "bluesky"])
        self.assertFalse((fresh / "kit").exists())


class Selection(ThreadCase):
    def test_selected_identity_and_mode_refusals(self):
        refused = self.run_thread()
        self.assertEqual(refused.returncode, 1, "legacy 280 rule refuses the long example summary")
        self.assertIn(f"{FID}: post 1/3", refused.stderr)
        self.assertFalse((self.root / "kit").exists())
        ok = self.run_thread("--platform", "mastodon", "--platform", "bluesky", "--platform", "mastodon")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        report, footer = ok.stdout.split("\n}\n")
        self.assertEqual(json.loads(report + "\n}")["finding"], FID)
        self.assertEqual(json.loads(report + "\n}")["posts"], 3)
        self.assertEqual(list(json.loads(report + "\n}")["threads"]["platforms"]), ["mastodon", "bluesky"])
        self.assertEqual(footer.strip(), "Drafts written to kit/social/threads. Nothing was posted.")
        self.assertEqual(sorted(p.name for p in self.threads.iterdir()), ["bluesky", "mastodon"])
        for ident in ("fnd_nope", SLUG, FID[:-1], FID.upper(), "", "../x", FID + "/"):
            with self.subTest(ident):
                result = self.cli("--thread", "--finding", ident, "--platform", "mastodon")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertNotIn("Traceback", result.stderr)
        # unpublished: a draft-state finding file is refused by the loader (exit 1), nothing written
        (self.content / "findings" / "draft.json").write_text(json.dumps(dict(self.finding, id="fnd_draft", slug="draft", state="draft")),
                                                              encoding="utf-8")
        before = self.snapshot()
        drafted = self.cli("--thread", "--finding", "fnd_draft", "--platform", "mastodon")
        self.assertEqual(drafted.returncode, 1, drafted.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_missing_content_base_url_and_malformed_content(self):
        site_path = self.content / "site.json"
        site = json.loads(site_path.read_text(encoding="utf-8"))
        del site["base_url"]
        site_path.write_text(json.dumps(site), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "base_url"):
            build_thread_social(self.root, FID, ["mastodon"])
        self.assertEqual(self.run_thread("--platform", "mastodon").returncode, 1)
        site_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_thread("--platform", "mastodon").returncode, 1)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_thread("--platform", "mastodon").returncode, 1)
        bare = self.base_dir / "bare"
        bare.mkdir()
        (bare / "campaign.json").write_text((EXAMPLE / "campaign.json").read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "content/ directory"):
            build_thread_social(bare, FID, ["mastodon"])
        self.assertEqual(self.cli("--thread", "--finding", FID, "--platform", "mastodon", directory=bare).returncode, 1)
        self.assertFalse((bare / "kit").exists() or (self.root / "kit").exists())

    def test_argument_combinations_exit_2_before_writes(self):
        cases = [("--thread",), ("--thread", "--finding", FID, "--finding", FID), ("--finding", FID),
                 ("--thread", "--finding", FID, "--check"), ("--thread", "--finding", FID, "--meetings"),
                 ("--thread", "--finding", FID, "--outcomes"), ("--thread", "--finding", FID, "--within-days", "5"),
                 ("--thread", "--finding", FID, "--outcomes", "--meetings"),
                 ("--thread", "--finding", FID, "--check", "--platform", "x"),
                 ("--thread", "--finding", FID, "--platform", "tiktok")]
        for case in cases:
            with self.subTest(case):
                result = self.cli(*case)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.root / "kit").exists())
        self.assertIn("--thread", self.cli("--help").stdout)

    def test_campaign_resolution(self):
        register = self.base_dir / "campaigns.json"
        env = dict(os.environ, **{registry.ENV: str(register)})
        added = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "campaigns", "add", "demo", "--directory",
                                str(self.root)], cwd=REPO_ROOT, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(added.returncode, 0, added.stderr)
        result = self.cli("--thread", "--finding", FID, "--platform", "mastodon", "--campaign", "demo", directory="", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.threads / "mastodon" / f"{SLUG}.md").is_file())
        both = self.cli("--thread", "--finding", FID, "--campaign", "demo", env=env)
        self.assertEqual(both.returncode, 2)


class Records(ThreadCase):
    def setUp(self):
        super().setUp()
        self.save(summary="Short summary.", limitations=[], corrections=[], sources=[self.cite(CAFE)])
        self.draft = self.threads / "mastodon" / f"{SLUG}.md"
        self.record = self.threads / "mastodon" / ".written.json"
        self.suggested = self.threads / "mastodon.suggested" / f"{SLUG}.md"

    def test_edits_records_stale_and_single_selection(self):
        build_thread_social(self.root, FID, ["mastodon"])
        first, record = self.draft.read_bytes(), self.record.read_bytes()
        again = build_thread_social(self.root, FID, ["mastodon"])["threads"]["platforms"]["mastodon"]
        self.assertEqual((self.draft.read_bytes(), self.record.read_bytes(), again["kept_edited"]), (first, record, []))
        self.assertFalse(self.suggested.parent.exists())
        # published text changed, draft untouched: replaced, still recorded
        self.save(summary="Changed summary.")
        self.assertEqual(build_thread_social(self.root, FID, ["mastodon"])["threads"]["platforms"]["mastodon"]["written"], [SLUG])
        self.assertIn("Changed summary.", self.read(self.draft))
        # an edit is kept; the fresh text goes to the suggestion folder, again on rerun
        self.draft.write_text("organizer edit\n", encoding="utf-8")
        self.save(summary="Third summary.")
        kept = build_thread_social(self.root, FID, ["mastodon"])["threads"]["platforms"]["mastodon"]
        self.assertEqual((kept["kept_edited"], kept["suggested"], kept["written"]), ([SLUG], 1, []))
        self.assertEqual(self.read(self.draft), "organizer edit\n")
        self.assertIn("Third summary.", self.read(self.suggested))
        self.save(summary="Fourth summary.")
        build_thread_social(self.root, FID, ["mastodon"])
        self.assertIn("Fourth summary.", self.read(self.suggested))
        self.assertEqual(self.read(self.draft), "organizer edit\n")
        # corrupt or missing record: a differing draft is treated as edited, never overwritten
        for damage in ("corrupt", "missing"):
            self.draft.write_text("another edit\n", encoding="utf-8")
            if damage == "corrupt":
                self.record.write_text("{not json", encoding="utf-8")
            else:
                self.record.unlink()
            kept = build_thread_social(self.root, FID, ["mastodon"])["threads"]["platforms"]["mastodon"]
            self.assertEqual(kept["kept_edited"], [SLUG], damage)
            self.assertEqual(self.read(self.draft), "another edit\n")

    def test_other_published_slug_is_not_stale_and_keeps_its_hash(self):
        self.second_finding()
        build_thread_social(self.root, FID, ["mastodon"])
        recorded = json.loads(self.read(self.record))
        self.assertEqual(list(recorded), [SLUG])
        report = build_thread_social(self.root, "fnd_0000000000000002", ["mastodon"])
        self.assertEqual(report["threads"]["platforms"]["mastodon"]["stale"], [])
        recorded = json.loads(self.read(self.record))
        self.assertEqual(sorted(recorded), sorted(["second-finding", SLUG]))
        before = self.draft.read_bytes()
        self.assertEqual(build_thread_social(self.root, FID, ["mastodon"])["threads"]["platforms"]["mastodon"]["stale"], [])
        self.assertEqual(self.draft.read_bytes(), before)
        # the first finding is withdrawn (its file removed): its draft is stale, never deleted, hash kept
        (self.content / "findings" / f"{SLUG}.json").unlink()
        stale = build_thread_social(self.root, "fnd_0000000000000002", ["mastodon"])["threads"]["platforms"]["mastodon"]
        self.assertEqual(stale["stale"], [SLUG])
        self.assertEqual(self.draft.read_bytes(), before)
        self.assertEqual(json.loads(self.read(self.record))[SLUG], recorded[SLUG])

    def test_platform_isolation_and_legacy_folder(self):
        self.save(summary="Short.")
        build_thread_social(self.root, FID, ["mastodon", "bluesky"])
        self.draft.write_text("edit\n", encoding="utf-8")
        self.save(summary="Changed.")
        report = build_thread_social(self.root, FID, ["mastodon", "bluesky"])["threads"]["platforms"]
        self.assertEqual((report["mastodon"]["kept_edited"], report["bluesky"]["written"]), ([SLUG], [SLUG]))
        self.assertFalse((self.threads / "bluesky.suggested").exists())
        self.assertEqual(sorted(json.loads(self.read(self.threads / "bluesky" / ".written.json"))), [SLUG])
        legacy = build_thread_social(self.root, FID)["threads"]
        self.assertEqual(legacy["written"], [SLUG])
        self.assertTrue((self.threads / f"{SLUG}.md").is_file() and (self.threads / ".written.json").is_file())
        self.assertEqual(sorted(p.name for p in self.threads.iterdir()),
                         sorted([".written.json", "bluesky", "mastodon", "mastodon.suggested", f"{SLUG}.md"]))


class Safety(ThreadCase):
    def setUp(self):
        super().setUp()
        self.save(summary="Short summary.", limitations=[], corrections=[], sources=[self.cite(CAFE)])
        self.outside = self.base_dir / "outside"
        self.outside.mkdir()
        (self.outside / "keep.md").write_text("outside bytes\n", encoding="utf-8")

    def outside_intact(self):
        self.assertEqual(sorted(p.name for p in self.outside.rglob("*")), ["keep.md"])
        self.assertEqual((self.outside / "keep.md").read_text(encoding="utf-8"), "outside bytes\n")

    def fresh(self):
        """A new copy of the campaign (no kit/) so each refusal case starts clean."""
        root = self.base_dir / f"case-{len(list(self.base_dir.glob('case-*')))}"
        shutil.copytree(self.root, root, ignore=shutil.ignore_patterns("kit"))
        return root

    def test_symlink_ancestors_and_nonfolders_refuse_without_writes(self):
        # (path under the root that becomes a link or file, folders created first, platforms)
        cases = [("kit", [], ["mastodon"]), ("kit/social", ["kit"], ["mastodon"]),
                 ("kit/social/threads", ["kit/social"], ["mastodon"]),
                 ("kit/social/threads/mastodon", ["kit/social/threads"], ["mastodon"]),
                 ("kit/social/threads/bluesky", ["kit/social/threads"], ["mastodon", "bluesky"]),
                 ("kit/social/threads/bluesky.suggested", ["kit/social/threads"], ["mastodon", "bluesky"])]
        for kind in ("link", "file"):
            for target, parents, platforms in cases:
                with self.subTest(kind=kind, target=target):
                    root = self.fresh()
                    for parent in parents:
                        (root / parent).mkdir(parents=True, mode=0o700, exist_ok=True)
                    path = root / target
                    if kind == "link":
                        path.symlink_to(self.outside, target_is_directory=True)
                    else:
                        path.write_text("a file", encoding="utf-8")
                    before = self.snapshot(root)
                    with self.assertRaisesRegex(ValueError, "must not be a symlink" if kind == "link" else "is not a folder"):
                        build_thread_social(root, FID, platforms)
                    self.assertEqual(self.snapshot(root), before)  # a later platform's bad ancestor writes nothing for the first
                    self.outside_intact()

    def test_nonregular_record_and_suggestion_targets_refuse(self):
        for name, make in (("record dir", lambda t: (t / "mastodon" / ".written.json").mkdir()),
                           ("record fifo", lambda t: os.mkfifo(t / "mastodon" / ".written.json")),
                           ("suggestion dir", lambda t: (t / "mastodon.suggested" / f"{SLUG}.md").mkdir(parents=True))):
            with self.subTest(name):
                root = self.fresh()
                threads = root / "kit/social/threads"
                (threads / "mastodon").mkdir(parents=True, mode=0o700)
                make(threads)
                before = self.snapshot(root)
                with self.assertRaisesRegex(ValueError, "not a regular file"):
                    build_thread_social(root, FID, ["mastodon"])
                self.assertEqual(self.snapshot(root), before)
                self.assertFalse((threads / "mastodon" / f"{SLUG}.md").exists())

    def test_draft_symlink_keeps_and_suggests(self):
        (self.threads / "mastodon").mkdir(parents=True, mode=0o700)
        draft = self.threads / "mastodon" / f"{SLUG}.md"
        draft.symlink_to(self.outside / "keep.md")
        report = build_thread_social(self.root, FID, ["mastodon"])["threads"]["platforms"]["mastodon"]
        self.assertEqual((report["kept_edited"], report["written"]), ([SLUG], []))
        self.assertTrue(draft.is_symlink())
        self.assertIn("Short summary.", self.read(self.threads / "mastodon.suggested" / f"{SLUG}.md"))
        self.outside_intact()

    def test_fresh_modes_are_private_and_existing_modes_are_not_widened(self):
        build_thread_social(self.root, FID, ["mastodon"])
        for folder in (self.root / "kit", self.root / "kit" / "social", self.threads, self.threads / "mastodon"):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode) & 0o077, 0, folder)
        for name in (f"{SLUG}.md", ".written.json"):
            self.assertEqual(stat.S_IMODE((self.threads / "mastodon" / name).stat().st_mode), 0o600)
        (self.threads / "mastodon" / f"{SLUG}.md").write_text("edit\n", encoding="utf-8")
        self.save(summary="Changed.")
        build_thread_social(self.root, FID, ["mastodon"])
        self.assertEqual(stat.S_IMODE((self.threads / "mastodon.suggested").stat().st_mode) & 0o077, 0)
        self.assertEqual(stat.S_IMODE((self.threads / "mastodon.suggested" / f"{SLUG}.md").stat().st_mode), 0o600)
        os.chmod(self.threads, 0o750)
        build_thread_social(self.root, FID, ["mastodon"])
        self.assertEqual(stat.S_IMODE(self.threads.stat().st_mode), 0o750)


class Isolation(ThreadCase):
    def test_legacy_isolation_alerts_and_no_network(self):
        build_social(self.root)
        build_meeting_social(self.root)
        social = self.root / "kit" / "social"
        legacy = lambda: {str(p): p.read_bytes() for p in social.rglob("*") if p.is_file() and "threads" not in p.parts}
        before, check_before = legacy(), check_social(self.root)
        content_before = {str(p): p.read_bytes() for p in self.content.rglob("*") if p.is_file()}
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")):
            build_thread_social(self.root, FID, ["mastodon", "bluesky"])
            self.assertEqual(self.run_thread("--platform", "mastodon").returncode, 0)
        self.assertEqual(legacy(), before)
        self.assertEqual(check_social(self.root), check_before)
        self.assertEqual({str(p): p.read_bytes() for p in self.content.rglob("*") if p.is_file()}, content_before)
        self.assertEqual(sorted(p.name for p in social.glob("*.md")), [f"{SLUG}.md"])
        # threads never reach the public build
        build_site(self.root)
        for path in (self.root / "public").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"thread finding", path.read_bytes())
        # legal-claim wording is an alert on stderr only; the draft is still written
        self.save(limitations=["The city violated state law."])  # summary/title/body with this wording are refused by the loader
        result = self.run_thread("--platform", "mastodon")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"review-required: social/threads/{FID}/2: legal-claim wording", result.stderr)
        self.assertNotIn("review-required", result.stdout)
        self.assertIn("violated", self.read(self.threads / "mastodon" / f"{SLUG}.md"))
        self.assertEqual(len(thread_notes(self.root, FID)), 1)
        self.assertEqual(thread_review_notes(["(1/1) fine"], FID), [])
        self.assertEqual(thread_review_notes([None], FID), [])  # never raises on odd input
        self.assertTrue(thread_notes(self.base_dir / "nowhere", FID)[0].startswith("review-required"))


if __name__ == "__main__":
    unittest.main()
