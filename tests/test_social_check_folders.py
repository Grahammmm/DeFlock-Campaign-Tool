"""DF1622-04: ``social --check`` covers platform, meeting, outcome and thread draft folders (synthetic only)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from campaign_tool.social import build_meeting_social, build_outcome_social, build_social, check_social
from campaign_tool.social_threads import build_thread_social

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
NOW_O = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)  # outcome dates may not be after the real "today"
FID, SLUG = "fnd_0000000000000001", "cedar-policy-posted"
FACTS = {"in_person": True, "remote": None, "written_comment": None, "source_url": "https://example.invalid/agenda",
         "verified_by": "Synthetic Organizer", "verified_at": "2026-11-02"}
SOURCE = "https://example.invalid/council/minutes"


def outcome(on="2026-11-05", result="approved"):
    return {"result": result, "on": on, "source_url": SOURCE, "verified_by": "Synthetic Organizer", "verified_at": "2026-11-05"}


class FolderCheckCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-social-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(os.path.realpath(temporary.name)) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        shutil.rmtree(self.meetings)
        self.meetings.mkdir()
        self.social = self.root / "kit" / "social"
        self.finding_path = self.root / "content" / "findings" / f"{SLUG}.json"

    def meeting(self, mid="m-one", starts="2026-11-15T09:00:00-08:00", facts=FACTS, **extra):
        document = {"id": mid, "body": "Cedar City Council (fictional)", "starts_at": starts, "agenda_item": "Item 7: ALPR", **extra}
        if facts is not None:
            document["participation"] = facts
        (self.meetings / f"{mid}.json").write_text(json.dumps(document), encoding="utf-8")

    def change_finding(self, **changes):
        document = json.loads(self.finding_path.read_text(encoding="utf-8"))
        document.update(changes)
        self.finding_path.write_text(json.dumps(document), encoding="utf-8")

    def check(self, now=NOW):
        return check_social(self.root, now=now)

    def folder(self, rel, now=NOW):
        return self.check(now)["folders"]["kit/social/" + rel]

    def states(self, rel, now=NOW):
        return {e["slug"]: (e["state"], e["problems"]) for e in self.folder(rel, now)["drafts"]}

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "social", "--directory", str(self.root), *extra],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=90)

    def tree(self):
        return {str(p): (p.read_bytes() if p.is_file() else b"", p.stat().st_mtime_ns) for p in sorted(self.root.rglob("*"))}


class LegacyAndPlatforms(FolderCheckCase):
    def test_legacy_only_campaign_has_no_folders_key_and_no_metadata_files(self):
        build_social(self.root)
        report = self.check()
        self.assertEqual(list(report), ["findings", "drafts", "stale", "problems", "posted"])
        self.assertEqual(report["problems"], 0)
        self.assertEqual([p.name for p in self.social.rglob(".check.json")], [])
        self.assertEqual(self.cli("--check").returncode, 0)

    def test_platform_folder_states_and_own_length_rule(self):
        build_social(self.root, ["x", "mastodon"])
        self.assertEqual(self.states("x"), {SLUG: ("ok", [])})
        draft = self.social / "x" / f"{SLUG}.md"
        text = draft.read_text(encoding="utf-8")
        draft.write_text(text + "y" * 200, encoding="utf-8")  # over X's 280 but within Mastodon's 500
        self.assertEqual(self.states("x"), {SLUG: ("edited", ["over_cap"])})
        (self.social / "mastodon" / f"{SLUG}.md").write_text(text + "y" * 200, encoding="utf-8")
        self.assertEqual(self.states("mastodon"), {SLUG: ("edited", [])})  # valid edit is not a problem
        self.assertEqual((self.folder("x")["problems"], self.folder("mastodon")["problems"]), (1, 0))
        draft.write_text(text.replace("(finding fnd", "(fnd"), encoding="utf-8")
        self.assertEqual(self.states("x"), {SLUG: ("edited", ["no_id"])})
        draft.unlink()
        self.assertEqual(self.states("x"), {SLUG: ("missing", ["missing"])})
        os.mkfifo(draft)
        self.assertEqual(self.states("x"), {SLUG: ("not_regular", ["not_regular"])})

    def second_finding(self):
        other = json.loads(self.finding_path.read_text(encoding="utf-8"))
        other.update(id="fnd_0000000000000002", slug="second-finding", title="Second synthetic finding", corrections=[])
        path = self.finding_path.parent / "second-finding.json"
        path.write_text(json.dumps(other), encoding="utf-8")
        return path

    def test_platform_draft_outdated_and_unpublished_finding_is_stale_once(self):
        gone = self.second_finding()
        build_social(self.root, ["bluesky"])
        self.change_finding(title="A changed synthetic title")
        self.assertEqual(self.states("bluesky")[SLUG], ("outdated", ["outdated"]))
        gone.unlink()  # a finding that is no longer published (removed, or its slug changed)
        block = self.folder("bluesky")
        self.assertEqual(sorted(block["stale"]), ["second-finding"])
        self.assertIn("unpublished", block["stale_reasons"]["second-finding"][0])
        self.assertEqual([e["slug"] for e in block["drafts"]], [SLUG])
        self.assertEqual(block["problems"], 2)  # the outdated draft and the one stale file, each once

    def test_suggested_platform_folder_is_checked_and_old_slug_is_stale(self):
        build_social(self.root, ["x"])
        (self.social / "x" / "old-slug.md").write_text("old", encoding="utf-8")
        (self.social / "x.suggested").mkdir()
        (self.social / "x.suggested" / f"{SLUG}.md").write_text("not current", encoding="utf-8")
        self.assertEqual(self.states("x.suggested"), {SLUG: ("outdated", ["outdated"])})
        self.assertEqual(self.folder("x")["stale"], ["old-slug"])

    def test_linked_folder_is_named_and_never_entered(self):
        canary = self.root / "outside"
        canary.mkdir()
        (canary / "evil.md").write_text("canary", encoding="utf-8")
        self.social.mkdir(parents=True)
        os.symlink(canary, self.social / "x")
        build_social(self.root, ["mastodon"])
        report = self.check()
        self.assertEqual(report["folders"]["kit/social/x"]["problems"], 1)
        self.assertEqual(report["folders"]["kit/social/x"]["drafts"], [])
        self.assertNotIn("canary", json.dumps(report))
        self.assertEqual(report["folders"]["kit/social/mastodon"]["problems"], 0)


class Meetings(FolderCheckCase):
    def built(self, **kwargs):
        self.meeting()
        build_social(self.root)
        build_meeting_social(self.root, now=NOW, **kwargs)

    def stale(self, now=NOW):
        block = self.folder("meetings", now)
        return block["stale_reasons"], block["problems"]

    def test_fresh_meeting_drafts_are_ok_and_written_with_private_provenance(self):
        self.built()
        build_meeting_social(self.root, ["x"], now=NOW)
        self.assertEqual(self.states("meetings"), {"m-one": ("ok", [])})
        self.assertEqual(self.states("meetings/x"), {"m-one": ("ok", [])})
        sidecar = self.social / "meetings" / ".check.json"
        entry = json.loads(sidecar.read_text(encoding="utf-8"))["drafts"]["m-one"]
        self.assertEqual((entry["kind"], entry["within_days"], entry["starts_at"]), ("meetings", 14, "2026-11-15T09:00:00-08:00"))
        self.assertEqual(oct(sidecar.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("Council", sidecar.read_text(encoding="utf-8"))

    def test_cancelled_rescheduled_past_unverified_and_removed_are_stale(self):
        self.built()
        self.meeting(status="cancelled")
        reasons, problems = self.stale()
        self.assertEqual(reasons, {"m-one": ["meeting cancelled"]})
        self.assertEqual(problems, 1)  # counted once
        self.assertEqual(self.states("meetings"), {})
        self.meeting(starts="2026-11-15T09:00:30-08:00")  # same rendered minute, different stored value
        reasons, _ = self.stale()
        self.assertEqual(reasons["m-one"], ["rescheduled (was 2026-11-15T09:00:00-08:00, now 2026-11-15T09:00:30-08:00)"])
        self.meeting()
        self.assertEqual(self.stale(datetime(2026, 11, 16, tzinfo=timezone.utc))[0]["m-one"], ["meeting is past"])
        self.meeting(facts=None)
        self.assertEqual(self.stale()[0]["m-one"], ["meeting participation no longer verified"])
        (self.meetings / "m-one.json").unlink()
        self.assertEqual(self.stale()[0]["m-one"], ["meeting removed or unreadable"])

    def test_boundary_start_equal_to_now_is_not_past(self):
        self.meeting(starts="2026-11-10T12:00:00+00:00")
        build_meeting_social(self.root, now=NOW)
        self.assertEqual(self.states("meetings"), {"m-one": ("ok", [])})

    def test_missing_edited_and_outdated_meeting_drafts(self):
        self.built()
        self.meeting("m-two", starts="2026-11-16T09:00:00-08:00")
        self.assertEqual(self.states("meetings")["m-two"], ("missing", ["missing"]))  # eligible, never drafted
        draft = self.social / "meetings" / "m-one.md"
        text = draft.read_text(encoding="utf-8")
        draft.write_text(text + "z" * 300, encoding="utf-8")
        self.assertEqual(self.states("meetings")["m-one"], ("edited", ["over_cap"]))
        draft.write_text(text.replace("Sun Nov 15", "Mon Nov 16"), encoding="utf-8")
        self.assertEqual(self.states("meetings")["m-one"], ("edited", ["outdated"]))
        draft.write_text(text, encoding="utf-8")
        self.meeting(starts="2026-11-15T10:00:00-08:00")  # rescheduled, but only the stored value tells
        self.assertIn("rescheduled", self.stale()[0]["m-one"][0])

    def test_kept_edit_keeps_original_provenance_and_suggestion_gets_fresh(self):
        self.built()
        draft = self.social / "meetings" / "m-one.md"
        draft.write_text(draft.read_text(encoding="utf-8") + " (organizer note)", encoding="utf-8")
        self.meeting(starts="2026-11-15T10:00:00-08:00")
        build_meeting_social(self.root, now=NOW)  # edit kept, fresh draft goes to meetings.suggested
        old = json.loads((self.social / "meetings" / ".check.json").read_text(encoding="utf-8"))["drafts"]["m-one"]
        new = json.loads((self.social / "meetings.suggested" / ".check.json").read_text(encoding="utf-8"))["drafts"]["m-one"]
        self.assertEqual((old["starts_at"], new["starts_at"]), ("2026-11-15T09:00:00-08:00", "2026-11-15T10:00:00-08:00"))
        self.assertIn("rescheduled", self.stale()[0]["m-one"][0])  # primary is still the old time
        self.assertEqual(self.states("meetings.suggested"), {"m-one": ("ok", [])})

    def test_historical_or_bad_provenance_is_never_trusted_and_check_writes_nothing(self):
        self.built()
        sidecar = self.social / "meetings" / ".check.json"
        sidecar.unlink()
        self.meeting(starts="2026-11-15T09:00:30-08:00")
        self.assertEqual(self.states("meetings"), {"m-one": ("ok", [])})  # identical bytes: fine, window unknown
        self.meeting(starts="2026-11-15T10:00:00-08:00")
        entry = self.folder("meetings")["drafts"][0]
        self.assertEqual((entry["state"], entry["problems"]), ("outdated", ["outdated"]))
        self.assertIn("cannot be checked; rebuild with social --meetings", entry["note"])
        self.assertFalse(sidecar.exists())
        sidecar.write_text(json.dumps({"schema_version": 2, "drafts": {}}), encoding="utf-8")
        block = self.folder("meetings")
        self.assertEqual(len(block["folder_problems"]), 1)
        self.assertEqual(block["stale"], [])
        before = self.tree()
        self.check()
        self.assertEqual(self.tree(), before)

    def test_metadata_target_that_is_not_a_regular_file_refuses_before_any_write(self):
        self.meeting()
        (self.social / "meetings").mkdir(parents=True)
        os.mkfifo(self.social / "meetings" / ".check.json")
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            build_meeting_social(self.root, now=NOW)
        self.assertEqual(sorted(p.name for p in (self.social / "meetings").iterdir()), [".check.json"])


class Outcomes(FolderCheckCase):
    def built(self, recorded=None, **kwargs):
        self.meeting(starts="2026-11-01T09:00:00-08:00", facts=None, outcome=recorded or outcome())
        build_social(self.root)
        build_outcome_social(self.root, now=NOW_O, **kwargs)

    def stale(self, now=NOW_O):
        return self.folder("outcomes", now)["stale_reasons"]

    def states(self, rel, now=NOW_O):
        return super().states(rel, now)

    def test_past_meeting_with_current_outcome_is_ok(self):
        self.built()
        self.assertEqual(self.states("outcomes"), {"m-one": ("ok", [])})

    def test_changed_removed_cancelled_and_missing_outcome_sources_are_stale(self):
        self.built()
        self.meeting(starts="2026-11-01T09:00:00-08:00", facts=None, outcome=outcome(result="denied"))
        self.assertEqual(self.stale()["m-one"], ["outcome changed"])
        self.meeting(starts="2026-11-01T09:00:00-08:00", facts=None)
        self.assertEqual(self.stale()["m-one"], ["outcome removed"])
        self.meeting(starts="2026-11-01T09:00:00-08:00", facts=None, status="cancelled")
        self.assertEqual(self.stale()["m-one"], ["meeting cancelled", "outcome removed"])
        (self.meetings / "m-one.json").unlink()
        self.assertEqual(self.stale()["m-one"], ["meeting removed or unreadable"])

    def test_original_window_is_inclusive_across_utc_midnight(self):
        self.built(within_days=1)
        entry = json.loads((self.social / "outcomes" / ".check.json").read_text(encoding="utf-8"))["drafts"]["m-one"]
        self.assertEqual((entry["within_days"], entry["outcome"]["on"]), (1, "2026-11-05"))
        self.assertEqual(self.states("outcomes"), {"m-one": ("ok", [])})
        later = datetime(2026, 11, 6, 0, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(self.stale(later)["m-one"], ["outcome left its 1-day window"])

    def test_edited_outcome_draft_must_keep_recorded_fields(self):
        self.built()
        draft = self.social / "outcomes" / "m-one.md"
        draft.write_text(draft.read_text(encoding="utf-8").replace("approved", "rejected"), encoding="utf-8")
        self.assertEqual(self.states("outcomes"), {"m-one": ("edited", ["outdated"])})


class Threads(FolderCheckCase):
    def test_threads_are_checked_per_post_and_only_the_requested_ones(self):
        build_social(self.root)
        build_thread_social(self.root, FID, ["bluesky", "mastodon"])
        self.assertEqual(self.states("threads/bluesky"), {SLUG: ("ok", [])})
        self.assertEqual(self.states("threads/mastodon"), {SLUG: ("ok", [])})
        draft = self.social / "threads" / "bluesky" / f"{SLUG}.md"
        text = draft.read_text(encoding="utf-8")
        draft.write_text(text.replace("(1/", "(9/", 1), encoding="utf-8")
        self.assertEqual(self.states("threads/bluesky"), {SLUG: ("edited", ["outdated"])})
        draft.write_text(text.replace("\n\n---\n\n", "\n\n---\n\n" + "w" * 400 + " ", 1), encoding="utf-8")
        self.assertIn("over_cap", self.states("threads/bluesky")[SLUG][1])
        draft.write_text(text.replace(f"https://cedar-records.example.invalid/findings/{SLUG}.html", "x"), encoding="utf-8")
        self.assertIn("no_link", self.states("threads/bluesky")[SLUG][1])
        draft.unlink()
        self.assertEqual(self.states("threads/bluesky"), {SLUG: ("missing", ["missing"])})  # recorded but gone

    def test_unpublished_finding_makes_its_thread_stale_and_other_findings_are_not_missing(self):
        build_social(self.root)
        other = json.loads(self.finding_path.read_text(encoding="utf-8"))
        other.update(id="fnd_0000000000000002", slug="second-finding", title="Second synthetic finding", corrections=[])
        path = self.finding_path.parent / "second-finding.json"
        path.write_text(json.dumps(other), encoding="utf-8")
        build_thread_social(self.root, "fnd_0000000000000002", ["bluesky"])
        self.assertEqual(self.states("threads/bluesky"), {"second-finding": ("ok", [])})  # the first finding has no thread: not missing
        path.unlink()
        block = self.folder("threads/bluesky")
        self.assertEqual((block["drafts"], block["stale"]), ([], ["second-finding"]))


class Cli(FolderCheckCase):
    def test_json_text_exit_codes_and_read_only(self):
        self.meeting()
        build_social(self.root)
        build_meeting_social(self.root, now=NOW)
        before = self.tree()
        result = self.cli("--check")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("kit/social/meetings", json.loads(result.stdout)["folders"])
        text = self.cli("--check", "--text")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("kit/social:\n", text.stdout)
        self.assertIn("kit/social/meetings:\n  m-one: ok\n", text.stdout)
        self.assertTrue(text.stdout.endswith("0 problem(s).\nRead-only. Nothing was posted.\n"))
        (self.social / "meetings" / "m-one.md").unlink()
        bad = self.cli("--check", "--text")
        self.assertEqual(bad.returncode, 3)
        self.assertIn("  m-one: missing (missing)\n", bad.stdout)
        self.assertEqual(self.cli("--check").returncode, 3)
        self.assertEqual(sorted(k for k in before if k.endswith("m-one.md")), [str(self.social / "meetings" / "m-one.md")])

    def test_text_needs_check_and_check_refuses_generation_selectors(self):
        self.assertEqual(self.cli("--text").returncode, 2)
        for extra in (["--platform", "x"], ["--meetings"], ["--outcomes"], ["--thread", "--finding", FID]):
            self.assertEqual(self.cli("--check", *extra).returncode, 2, extra)

    def test_legacy_only_text_has_only_the_legacy_group(self):
        build_social(self.root)
        text = self.cli("--check", "--text").stdout
        self.assertEqual(text, "Checked 1 published finding(s).\nkit/social:\n  cedar-policy-posted: ok\n0 problem(s).\nRead-only. Nothing was posted.\n")


if __name__ == "__main__":
    unittest.main()
