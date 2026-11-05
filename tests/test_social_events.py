"""DF1407-10: private never-posted social drafts for campaign events (synthetic only)."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from campaign_tool.social import (build_event_social, build_meeting_social, build_social, event_post_text, event_review_notes,
                                  post_length)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
MARK = "<!-- DRAFT: not posted. Review before any use. event {} -->\n\n"
LIMITS = {"x": 280, "bluesky": 300, "mastodon": 500}


def event(eid, starts="2026-11-12T18:00:00-08:00", **extra):
    base = {"id": eid, "title": "Records teach-in", "starts_at": starts, "ends_at": "2026-12-30T20:00:00-08:00",
            "description_md": "Secret description text.", "venue": "Cedar Library room 1"}
    base.update(extra)
    return base


def online(eid, starts):
    value = event(eid, starts, online=True)
    del value["venue"]
    return value


class SocialEventTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-social-events-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.out = self.root / "kit" / "social" / "events"

    def put(self, events):
        (self.root / "content" / "events.json").write_text(json.dumps(events), encoding="utf-8")

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "social", "--directory", str(self.root), *extra],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def kit(self):
        kit = self.root / "kit"
        return sorted(str(p.relative_to(self.root)) for p in kit.rglob("*")) if kit.exists() else []

    def body(self, path):
        return path.read_text(encoding="utf-8").split("\n")[2]

    def test_exact_drafts_in_person_and_online_with_local_zone(self):
        self.put([event("teach-in"), online("webinar", "2026-11-13T09:30:00+14:00"),
                  online("utc", "2026-11-11T00:00:00Z")])
        summary = build_event_social(self.root, now=NOW)
        self.assertEqual(summary["events"]["written"], ["teach-in", "utc", "webinar"])
        self.assertEqual((summary["posted"], summary["skipped"]), (False, {"past": 0, "outside_window": 0}))
        self.assertEqual((self.out / "teach-in.md").read_text(encoding="utf-8"), MARK.format("teach-in") +
                         f"Records teach-in \u2014 Thu Nov 12, 2026, 6:00 pm UTC-08:00. Cedar Library room 1. {BASE}/events.html#event-teach-in\n")
        self.assertEqual(self.body(self.out / "webinar.md"),
                         f"Records teach-in \u2014 Fri Nov 13, 2026, 9:30 am UTC+14:00. Online. {BASE}/events.html#event-webinar")
        self.assertEqual(self.body(self.out / "utc.md"),
                         f"Records teach-in \u2014 Wed Nov 11, 2026, 12:00 am UTC+00:00. Online. {BASE}/events.html#event-utc")
        self.assertNotIn("Secret", (self.out / "teach-in.md").read_text(encoding="utf-8"))

    def test_window_inclusive_and_past_never_drafted(self):
        self.put([event("at-now", "2026-11-10T12:00:00Z"), event("at-edge", "2026-11-24T12:00:00Z"),
                  event("late", "2026-11-24T12:00:01Z"), event("early", "2026-11-10T11:59:59Z"),
                  event("underway", "2026-11-09T12:00:00Z", ends_at="2026-11-30T12:00:00Z"),
                  event("zoned", "2026-11-24T04:00:00-08:00")])  # = 12:00Z on the edge
        summary = build_event_social(self.root, now=NOW)
        self.assertEqual(summary["events"]["written"], ["at-edge", "at-now", "zoned"])
        self.assertEqual(summary["skipped"], {"past": 2, "outside_window": 1})
        wide = build_event_social(self.root, within_days=60, now=NOW)
        self.assertEqual(wide["events"]["written"], ["at-edge", "at-now", "late", "zoned"])
        self.assertEqual(wide["skipped"], {"past": 2, "outside_window": 0})
        for bad in (0, 61, -1, True, 1.5, "7"):  # type: ignore
            before = self.kit()
            with self.assertRaises(ValueError):
                build_event_social(self.root, within_days=bad, now=NOW)
            self.assertEqual(self.kit(), before)

    def test_platform_lengths_and_title_truncation(self):
        self.put([event("long", title="\u5b57\U0001F600" * 60)])
        result = build_event_social(self.root, ["x", "bluesky", "mastodon", "x"], now=NOW)
        self.assertEqual(list(result["events"]["platforms"]), ["x", "bluesky", "mastodon"])
        for platform, limit in LIMITS.items():
            body = self.body(self.out / platform / "long.md")
            self.assertLessEqual(post_length(body, platform), limit)
            self.assertTrue(body.endswith(f" \u2014 Thu Nov 12, 2026, 6:00 pm UTC-08:00. Cedar Library room 1. {BASE}/events.html#event-long"))
        self.assertIn("\u2026 \u2014", self.body(self.out / "x" / "long.md"))
        self.assertNotIn("\u2026", self.body(self.out / "mastodon" / "long.md"))
        self.assertTrue(self.body(self.out / "x" / "long.md").startswith("\u5b57"))
        for platforms in (["nope"], []):
            with self.assertRaises(ValueError):
                build_event_social(self.root, platforms, now=NOW)

    def test_no_fit_refuses_naming_event_and_platform_before_any_write(self):
        self.put([event("ok-one"), event("zz-big", venue="\u5b57" * 160)])
        with self.assertRaises(ValueError) as caught:
            build_event_social(self.root, ["mastodon", "x"], now=NOW)
        self.assertIn("event zz-big", str(caught.exception))
        self.assertIn("on x", str(caught.exception))
        self.assertEqual(self.kit(), [])
        with self.assertRaises(ValueError):
            event_post_text(event("zz-big", venue="\u5b57" * 160), BASE + "/" + "p" * 200)

    def test_base_url_required_even_for_no_events_and_missing_content(self):
        site_path = self.root / "content" / "site.json"
        site = json.loads(site_path.read_text(encoding="utf-8"))
        del site["base_url"]
        site_path.write_text(json.dumps(site), encoding="utf-8")
        self.put([])
        with self.assertRaises(ValueError) as caught:
            build_event_social(self.root, now=NOW)
        self.assertIn("base_url", str(caught.exception))
        self.assertEqual(self.kit(), [])
        shutil.rmtree(self.root / "content")
        with self.assertRaises(ValueError):
            build_event_social(self.root, now=NOW)

    def test_missing_and_empty_events_and_malformed_refuses_whole(self):
        self.assertEqual(build_event_social(self.root, now=NOW)["events"],
                         {"written": [], "kept_edited": [], "suggested": 0, "stale": [], "posted": False})
        self.put([])
        self.assertEqual(build_event_social(self.root, now=NOW)["skipped"], {"past": 0, "outside_window": 0})
        before = self.kit()
        for bad in ([event("good"), event("bad", "2026-11-12 18:00")], [event("dup"), event("dup")],
                    [event("naive", "2026-11-12T18:00:00")]):
            self.put(bad)
            with self.assertRaises(ValueError):
                build_event_social(self.root, now=NOW)
            self.assertEqual(self.kit(), before)
        path = self.root / "content" / "events.json"
        path.unlink()
        path.symlink_to(self.root / "content" / "site.json")
        with self.assertRaises(ValueError):
            build_event_social(self.root, now=NOW)
        self.assertEqual(self.kit(), before)

    def test_privacy_lint_refuses_before_write(self):
        self.put([event("one"), event("two", venue="Call 555-867-5309 to attend")])
        with self.assertRaises(ValueError):
            build_event_social(self.root, now=NOW)
        self.assertEqual(self.kit(), [])

    def test_edit_stale_suggestion_and_byte_identical_rerun(self):
        self.put([event("a"), event("b")])
        build_event_social(self.root, now=NOW)
        first = {p.name: p.read_bytes() for p in self.out.iterdir()}
        again = build_event_social(self.root, now=NOW)
        self.assertEqual(({p.name: p.read_bytes() for p in self.out.iterdir()}, again["events"]["kept_edited"]), (first, []))
        (self.out / "a.md").write_text("my edit\n", encoding="utf-8")
        self.put([event("a", venue="Cedar Hall"), event("b", venue="Cedar Hall")])
        result = build_event_social(self.root, now=NOW)
        self.assertEqual((result["events"]["written"], result["events"]["kept_edited"]), (["b"], ["a"]))
        self.assertEqual((self.out / "a.md").read_text(encoding="utf-8"), "my edit\n")
        self.assertIn("Cedar Hall", (self.root / "kit/social/events.suggested/a.md").read_text(encoding="utf-8"))
        self.put([event("b", venue="Cedar Hall")])
        stale = build_event_social(self.root, now=NOW)
        self.assertEqual(stale["events"]["stale"], ["a"])
        self.assertTrue((self.out / "a.md").is_file())

    def test_symlinked_output_folder_refused_without_touching_target(self):
        self.put([event("a")])
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir()
        (self.root / "kit" / "social").mkdir(parents=True)
        (self.root / "kit" / "social" / "events").symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            build_event_social(self.root, now=NOW)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_other_modes_untouched(self):
        build_social(self.root)
        build_meeting_social(self.root, now=NOW)
        before = {p: p.read_bytes() for p in (self.root / "kit").rglob("*") if p.is_file()}
        self.put([event("a")])
        build_event_social(self.root, now=NOW)
        self.assertEqual({p: p.read_bytes() for p in (self.root / "kit").rglob("*")
                          if p.is_file() and "events" not in p.relative_to(self.root).parts}, before)
        self.assertTrue((self.out / "a.md").is_file())

    def test_review_notes_alert_only(self):
        self.put([event("a", title="Police broke the law")])
        notes = event_review_notes(self.root, now=NOW)
        self.assertTrue(notes and all(n.startswith("review-required") for n in notes))
        self.assertTrue(any("social/events/a" in n for n in notes))
        self.assertTrue(event_review_notes(self.root, within_days=0, now=NOW)[0].startswith("review-required"))

    def test_cli_modes_exclusions_and_output(self):
        soon = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self.put([event("soon", soon)])
        before = self.kit()
        for args, message in (
                (("--events", "--meetings"), "--events cannot be combined"),
                (("--events", "--outcomes"), "--events cannot be combined"),
                (("--events", "--check"), "--events cannot be combined"),
                (("--events", "--thread", "--finding", "x"), "--events cannot be combined"),
                (("--events", "--thread"), "--events cannot be combined"),
                (("--events", "--within-days", "0"), "--within-days must be 1 to 60"),
                (("--events", "--within-days", "61"), "--within-days must be 1 to 60"),
                (("--events", "--within-days", "five"), "invalid int value"),
                (("--within-days", "5"), "needs --meetings or --outcomes")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2, (args, result.stderr))
            self.assertIn(message, result.stderr)
            self.assertEqual(self.kit(), before)
        result = self.cli("--events", "--within-days", "30", "--platform", "bluesky")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Drafts written to kit/social/events. Nothing was posted.", result.stdout)
        summary = json.loads(result.stdout[:result.stdout.rindex("}") + 1])
        self.assertEqual((summary["events"]["platforms"]["bluesky"]["written"], summary["posted"]), (["soon"], False))
        self.assertTrue((self.out / "bluesky" / "soon.md").is_file())


if __name__ == "__main__":
    unittest.main()
