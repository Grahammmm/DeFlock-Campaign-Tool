"""DF1207-11: private never-posted social drafts for upcoming meetings with verified participation (synthetic only)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from campaign_tool.social import build_meeting_social, build_social, check_social, post_length

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
MARK = "<!-- DRAFT: not posted. Review before any use. meeting {} -->\n\n"
FACTS = {"in_person": True, "remote": {"available": True, "how": "Zoom link on the agenda"},
         "written_comment": {"how": "Email the clerk", "deadline": "2026-11-13T12:00:00-08:00"},
         "source_url": "https://example.invalid/council/agenda", "verified_by": "Synthetic Organizer",
         "verified_at": "2026-11-02"}
FIVE_DAYS = "2026-11-15T09:00:00-08:00"
EXPECTED = ("Cedar City Council (fictional) meets Sun Nov 15, 9:00 am UTC-08:00: Item 7: ALPR. "
            "Comment in writing by Fri Nov 13, 12:00 pm UTC-08:00: Email the clerk. "
            "Attend: in person or remotely: Zoom link on the agenda. "
            f"{BASE}/meetings/cedar-verified.html")


class SocialMeetingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-social-meetings-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        shutil.rmtree(self.meetings)
        self.meetings.mkdir()
        self.out = self.root / "kit" / "social" / "meetings"

    def meeting(self, mid, starts_at=FIVE_DAYS, participation=FACTS, **extra):
        document = {"id": mid, "body": "Cedar City Council (fictional)", "starts_at": starts_at,
                    "agenda_item": "Item 7: ALPR", "participation": participation, **extra}
        (self.meetings / f"{mid}.json").write_text(json.dumps(document), encoding="utf-8")

    def facts(self, **change):
        return {**FACTS, **change}

    def text(self, path):
        return path.read_text(encoding="utf-8").split("\n", 2)[2].rstrip("\n")

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "social", "--directory", str(self.root),
                               *extra], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.joinpath("kit").rglob("*"))

    def test_verified_meeting_in_five_days_gets_one_exact_draft(self):  # edge 1
        self.meeting("cedar-verified")
        summary = build_meeting_social(self.root, now=NOW)
        self.assertEqual(summary["meetings"], {"written": ["cedar-verified"], "kept_edited": [], "suggested": 0,
                                               "stale": [], "posted": False})
        self.assertEqual(summary["skipped"], dict.fromkeys(("cancelled", "past", "outside_window", "unverified",
                                                            "unreadable"), 0))
        self.assertFalse(summary["posted"])
        draft = (self.out / "cedar-verified.md").read_text(encoding="utf-8")
        self.assertEqual(draft, MARK.format("cedar-verified") + EXPECTED + "\n")
        self.assertEqual(self.tree(), ["kit/social", "kit/social/meetings", "kit/social/meetings/.written.json",
                                       "kit/social/meetings/cedar-verified.md"])
        self.assertFalse((self.root / "public").exists())

    def test_cancelled_past_far_and_unverified_skipped_and_counted(self):  # edge 2
        self.meeting("cedar-verified")
        self.meeting("cedar-cancelled", status="cancelled")
        self.meeting("cedar-past", starts_at="2026-11-09T09:00:00-08:00", participation=self.facts(written_comment=None))
        self.meeting("cedar-far", starts_at="2026-11-30T09:00:00-08:00")
        self.meeting("cedar-unverified", participation=None)
        (self.meetings / "cedar-broken.json").write_text("{not json", encoding="utf-8")
        summary = build_meeting_social(self.root, now=NOW)
        self.assertEqual(summary["meetings"]["written"], ["cedar-verified"])
        self.assertEqual(summary["skipped"], {"cancelled": 1, "past": 1, "outside_window": 1, "unverified": 1,
                                              "unreadable": 1})
        self.assertEqual(summary["unreadable_files"], ["cedar-broken.json"])
        self.assertEqual(sorted(p.name for p in self.out.glob("*.md")), ["cedar-verified.md"])

    def test_duplicate_meeting_id_is_counted_unreadable_not_fatal(self):
        self.meeting("cedar-verified")
        document = json.loads((self.meetings / "cedar-verified.json").read_text(encoding="utf-8"))
        (self.meetings / "cedar-verified-copy.json").write_text(json.dumps(document), encoding="utf-8")
        summary = build_meeting_social(self.root, now=NOW)
        self.assertEqual(summary["meetings"]["written"], ["cedar-verified"])
        self.assertEqual((summary["skipped"]["unreadable"], summary["unreadable_files"]), (1, ["cedar-verified.json"]))

    def test_window_is_inclusive_of_now_and_cancelled_past_counts_once(self):
        self.meeting("cedar-now", starts_at="2026-11-10T04:00:00-08:00", participation=self.facts(written_comment=None))  # exactly now (12:00Z)
        self.meeting("cedar-edge", starts_at="2026-11-24T04:00:00-08:00")  # exactly now + 14 days
        self.meeting("cedar-late", starts_at="2026-11-24T04:00:01-08:00")
        self.meeting("cedar-old-cancelled", starts_at="2026-11-01T09:00:00-08:00", status="cancelled",
                     participation=self.facts(written_comment=None, verified_at="2026-10-30"))
        summary = build_meeting_social(self.root, now=NOW)
        self.assertEqual(summary["meetings"]["written"], ["cedar-edge", "cedar-now"])
        self.assertEqual((summary["skipped"]["outside_window"], summary["skipped"]["cancelled"],
                          summary["skipped"]["past"]), (1, 1, 0))

    def test_within_days_30_includes_twenty_day_meeting_and_bad_values_refused(self):  # edge 3
        self.meeting("cedar-twenty", starts_at="2026-11-30T09:00:00-08:00")
        self.assertEqual(build_meeting_social(self.root, now=NOW)["meetings"]["written"], [])
        self.assertEqual(build_meeting_social(self.root, within_days=30, now=NOW)["meetings"]["written"],
                         ["cedar-twenty"])
        before = self.tree()
        for bad in (0, 61, -1, True, 14.0):
            with self.assertRaisesRegex(ValueError, "1 to 60"):
                build_meeting_social(self.root, within_days=bad, now=NOW)
        self.assertEqual(self.tree(), before)
        # CLI: real clock, so dates are relative to today
        (self.meetings / "cedar-twenty.json").unlink()
        soon = (datetime.now(timezone.utc) + timedelta(days=20)).replace(microsecond=0)
        self.meeting("cedar-cli", starts_at=soon.isoformat(),
                     participation=self.facts(written_comment=None, verified_at=datetime.now(timezone.utc).date().isoformat()))
        shutil.rmtree(self.root / "kit")
        for bad in ("0", "61"):
            result = self.cli("--meetings", "--within-days", bad)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--within-days must be 1 to 60", result.stderr)
        self.assertFalse((self.root / "kit").exists())
        default = self.cli("--meetings")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(json.loads(default.stdout[:default.stdout.rindex("}") + 1])["skipped"]["outside_window"], 1)
        self.assertTrue(default.stdout.rstrip().endswith("Nothing was posted."))
        result = self.cli("--meetings", "--within-days", "30")
        summary = json.loads(result.stdout[:result.stdout.rindex("}") + 1])
        self.assertEqual((summary["meetings"]["written"], summary["posted"]), (["cedar-cli"], False))
        self.assertEqual(summary["skipped"]["outside_window"], 0)

    def test_platform_rules_shorten_only_the_agenda_item(self):  # edge 4
        item = "Item 7: " + "ALPR renewal " * 25
        self.assertGreater(len(item), 300)
        self.meeting("cedar-verified", agenda_item=item[:300])
        summary = build_meeting_social(self.root, ["x", "bluesky", "mastodon"], now=NOW)
        self.assertEqual(list(summary["meetings"]["platforms"]), ["x", "bluesky", "mastodon"])
        shown = {}
        for platform, limit in (("x", 280), ("bluesky", 300), ("mastodon", 500)):
            text = self.text(self.out / platform / "cedar-verified.md")
            self.assertLessEqual(post_length(text, platform), limit)
            self.assertTrue(text.startswith("Cedar City Council (fictional) meets Sun Nov 15, 9:00 am UTC-08:00: Item 7: "))
            self.assertTrue(text.endswith(f" Attend: in person or remotely: Zoom link on the agenda. "
                                          f"{BASE}/meetings/cedar-verified.html"))
            self.assertIn("Comment in writing by Fri Nov 13, 12:00 pm UTC-08:00: Email the clerk.", text)
            shown[platform] = text.split(": ", 2)[2].split(". Comment in writing")[0]
        self.assertTrue(shown["x"].endswith("\u2026"))
        self.assertTrue(shown["bluesky"].endswith("\u2026"))
        self.assertGreater(post_length(self.text(self.out / "x" / "cedar-verified.md"), "x"), 278)
        self.assertGreater(post_length(self.text(self.out / "bluesky" / "cedar-verified.md"), "bluesky"), 298)
        self.assertEqual(len({shown["x"], shown["bluesky"], shown["mastodon"]}), 3)  # each platform cuts by its own rule
        self.assertGreater(len(shown["mastodon"]), len(shown["bluesky"]))  # Mastodon has the most room
        self.assertFalse(list(self.out.glob("*.md")))

    def test_legacy_cap_and_post_that_cannot_fit_names_the_meeting(self):
        self.meeting("cedar-verified", agenda_item="a" * 400)
        build_meeting_social(self.root, now=NOW)
        text = self.text(self.out / "cedar-verified.md")
        self.assertEqual(len(text), 280)
        self.assertIn("\u2026. Comment in writing", text)
        self.meeting("cedar-long", participation=self.facts(remote={"available": True, "how": "z" * 200},
                                                            written_comment={"how": "w" * 200, "deadline": None}))
        before = self.tree()
        with self.assertRaisesRegex(ValueError, "meeting cedar-long: post cannot fit 300 characters on bluesky"):
            build_meeting_social(self.root, ["bluesky", "x"], now=NOW)
        self.assertEqual(self.tree(), before)

    def test_unknown_platform_refused(self):
        self.meeting("cedar-verified")
        for bad in (["tiktok"], []):
            with self.assertRaisesRegex(ValueError, "unknown platform"):
                build_meeting_social(self.root, bad, now=NOW)
        self.assertFalse((self.root / "kit").exists())

    def test_no_base_url_and_no_deadline_omit_their_parts(self):  # edge 5
        self.meeting("cedar-verified", participation=self.facts(
            written_comment={"how": "Email the clerk", "deadline": None}, remote=None))
        site_path = self.root / "content" / "site.json"
        site = json.loads(site_path.read_text(encoding="utf-8"))
        del site["base_url"]
        site_path.write_text(json.dumps(site), encoding="utf-8")
        build_meeting_social(self.root, now=NOW)
        self.assertEqual(self.text(self.out / "cedar-verified.md"),
                         "Cedar City Council (fictional) meets Sun Nov 15, 9:00 am UTC-08:00: Item 7: ALPR. "
                         "Attend: in person.")

    def test_attend_sentence_states_only_verified_facts(self):
        cases = {
            "remote-only": (self.facts(in_person=None), " Attend: remotely: Zoom link on the agenda."),
            "not-in-person": (self.facts(in_person=False), " Attend: remotely: Zoom link on the agenda."),
            "remote-unavailable": (self.facts(in_person=True, remote={"available": False, "how": "No remote"}),
                                   " Attend: in person."),
            "unstated": (self.facts(in_person=None, remote=None), ""),
        }
        for mid, (facts, attend) in cases.items():
            self.meeting(mid, participation={**facts, "written_comment": None})
        build_meeting_social(self.root, now=NOW)
        for mid, (_, attend) in cases.items():
            self.assertEqual(self.text(self.out / f"{mid}.md"),
                             f"Cedar City Council (fictional) meets Sun Nov 15, 9:00 am UTC-08:00: Item 7: ALPR."
                             f"{attend} {BASE}/meetings/{mid}.html")

    def test_edited_draft_kept_fresh_one_suggested_and_rerun_idempotent(self):  # edge 6
        self.meeting("cedar-verified")
        self.meeting("cedar-second", starts_at="2026-11-16T09:00:00-08:00")
        build_meeting_social(self.root, now=NOW)
        fresh = (self.out / "cedar-verified.md").read_bytes()
        again = build_meeting_social(self.root, now=NOW)
        self.assertEqual(again["meetings"]["written"], ["cedar-second", "cedar-verified"])  # unedited: rewritten, same bytes
        self.assertEqual((self.out / "cedar-verified.md").read_bytes(), fresh)
        edited = "<!-- organizer edit -->\n\nMy own words.\n"
        (self.out / "cedar-verified.md").write_text(edited, encoding="utf-8")
        for _ in range(2):
            summary = build_meeting_social(self.root, now=NOW)["meetings"]
            self.assertEqual((summary["kept_edited"], summary["written"], summary["suggested"]),
                             (["cedar-verified"], ["cedar-second"], 1))
            self.assertEqual((self.out / "cedar-verified.md").read_text(encoding="utf-8"), edited)
            self.assertEqual((self.root / "kit" / "social" / "meetings.suggested" / "cedar-verified.md").read_bytes(), fresh)
        self.meeting("cedar-second", starts_at="2026-11-16T09:00:00-08:00", status="cancelled")  # no longer drafted: stale, never deleted
        summary = build_meeting_social(self.root, now=NOW)["meetings"]
        self.assertEqual(summary["stale"], ["cedar-second"])
        self.assertTrue((self.out / "cedar-second.md").is_file())

    def test_edited_platform_draft_goes_to_that_platforms_suggested_folder(self):
        self.meeting("cedar-verified")
        build_meeting_social(self.root, ["x", "mastodon"], now=NOW)
        fresh = (self.out / "x" / "cedar-verified.md").read_bytes()
        (self.out / "x" / "cedar-verified.md").write_text("mine\n", encoding="utf-8")
        platforms = build_meeting_social(self.root, ["x", "mastodon"], now=NOW)["meetings"]["platforms"]
        self.assertEqual((platforms["x"]["kept_edited"], platforms["mastodon"]["kept_edited"]), (["cedar-verified"], []))
        self.assertEqual((self.out / "x.suggested" / "cedar-verified.md").read_bytes(), fresh)
        self.assertFalse((self.out / "mastodon.suggested").exists())

    def test_symlinked_meetings_folder_refused_before_any_write(self):  # edge 7
        self.meeting("cedar-verified")
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir()
        (self.root / "kit" / "social").mkdir(parents=True)
        os.symlink(elsewhere, self.out)
        for platforms in (None, ["x"]):
            with self.assertRaisesRegex(ValueError, "meetings/ must not be a symlink"):
                build_meeting_social(self.root, platforms, now=NOW)
        self.assertEqual(list(elsewhere.iterdir()), [])
        result = self.cli("--meetings")
        self.assertEqual(result.returncode, 1)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.out.unlink()
        os.symlink(elsewhere, self.root / "kit" / "social" / "meetings.suggested")
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            build_meeting_social(self.root, now=NOW)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertFalse(self.out.exists())
        (self.root / "kit" / "social" / "meetings.suggested").unlink()
        self.out.mkdir()
        os.symlink(elsewhere, self.out / "x.suggested")
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            build_meeting_social(self.root, ["bluesky", "x"], now=NOW)
        self.assertFalse((self.out / "bluesky").exists())

    def test_finding_drafts_and_check_are_unchanged_by_meeting_drafts(self):  # edge 8
        self.meeting("cedar-verified")
        build_social(self.root)
        findings_before = {p.name: p.read_bytes() for p in (self.root / "kit" / "social").glob("*.md")}
        check_before = self.cli("--check")
        self.assertEqual(check_before.returncode, 0, check_before.stdout)
        build_meeting_social(self.root, ["x"], now=NOW)
        build_meeting_social(self.root, now=NOW)
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / "kit" / "social").glob("*.md")}, findings_before)
        check_after = self.cli("--check")
        self.assertEqual((check_after.returncode, check_after.stdout), (check_before.returncode, check_before.stdout))
        self.assertEqual(json.loads(check_after.stdout)["stale"], [])
        summary = build_social(self.root)  # finding drafts rerun: nothing about meetings
        self.assertEqual(sorted(summary), ["kept_edited", "posted", "stale", "suggested", "written"])
        self.assertEqual(summary["stale"], [])
        self.assertEqual(check_social(self.root)["problems"], 0)

    def test_check_with_meetings_refused_and_within_days_needs_meetings(self):
        for args in (("--check", "--meetings"), ("--within-days", "5")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--check", self.cli("--check", "--meetings").stderr)
        self.assertIn("needs --meetings", self.cli("--within-days", "5").stderr)
        self.assertFalse((self.root / "kit").exists())

    def test_legal_claim_wording_in_a_meeting_post_is_noted_on_stderr_only(self):
        soon = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
        today = datetime.now(timezone.utc).date().isoformat()
        self.meeting("cedar-verified", starts_at=soon.isoformat(), agenda_item="The city violated state law",
                     participation=self.facts(written_comment=None, verified_at=today))
        result = self.cli("--meetings")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("review-required: social/meetings/cedar-verified: legal-claim wording", result.stderr)
        self.assertNotIn("review-required", result.stdout)
        self.assertTrue((self.out / "cedar-verified.md").is_file())  # alert only, the draft is still written


if __name__ == "__main__":
    unittest.main()
