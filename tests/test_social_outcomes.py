"""DF1952-14: private never-posted social drafts for meetings with a recorded outcome (synthetic only)."""
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.content import OUTCOME_RESULTS
from campaign_tool.social import (build_meeting_social, build_outcome_social, build_social, check_social,
                                  outcome_post_text, outcome_review_notes, post_length)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
MARK = "<!-- DRAFT: not posted. Review before any use. outcome {} -->\n\n"
SOURCE = "https://example.invalid/council/minutes"
BODY = "Cedar City Council (fictional)"
DEFAULT = object()
LONG_AGO = "2026-10-01T09:00:00-07:00"


def outcome(on="2026-11-08", result="approved", **extra):
    return {"result": result, "on": on, "source_url": SOURCE, "verified_by": "Synthetic Organizer",
            "verified_at": "2026-11-09", **extra}


class SocialOutcomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-social-outcomes-")
        self.addCleanup(temporary.cleanup)
        self.base_dir = Path(temporary.name)
        self.root = self.base_dir / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        shutil.rmtree(self.meetings)
        self.meetings.mkdir()
        self.out = self.root / "kit" / "social" / "outcomes"

    def meeting(self, mid, recorded=DEFAULT, starts_at="2026-11-01T09:00:00-08:00", **extra):
        document = {"id": mid, "body": BODY, "starts_at": starts_at, "agenda_item": "Item 7: ALPR", **extra}
        if recorded is DEFAULT:
            recorded = outcome()
        if recorded is not None:
            document["outcome"] = recorded
        (self.meetings / f"{mid}.json").write_text(json.dumps(document), encoding="utf-8")

    def text(self, path):
        return path.read_text(encoding="utf-8").split("\n", 2)[2].rstrip("\n")

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "social", "--directory", str(self.root),
                               *extra], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.joinpath("kit").rglob("*"))

    def hashes(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.joinpath("kit").rglob("*") if p.is_file()}

    def today(self, delta=0):
        return (datetime.now(timezone.utc) + timedelta(days=delta)).date().isoformat()

    def test_exact_recorded_result_vote_and_links(self):
        for token in OUTCOME_RESULTS:
            self.meeting(f"m-{token.replace('_', '-')}", outcome(result=token))
        self.meeting("m-vote", outcome(vote="  4-1, one absent  ", votes=[{"name": "Pat Synthetic", "vote": "yes"}]))
        self.meeting("m-bare-agenda", outcome(), agenda_item="")
        summary = build_outcome_social(self.root, now=NOW)
        self.assertEqual(summary["skipped"], dict.fromkeys(("cancelled", "no_outcome", "outside_window", "unreadable"), 0))
        self.assertFalse(summary["posted"])
        for token in OUTCOME_RESULTS:
            mid = f"m-{token.replace('_', '-')}"
            draft = (self.out / f"{mid}.md").read_text(encoding="utf-8")
            self.assertEqual(draft, MARK.format(mid) + f"{BODY} \u2014 2026-11-08: Item 7: ALPR. Result: {token}. "
                                                         f"Official record: {SOURCE} {BASE}/meetings/{mid}.html\n")
            self.assertNotIn("Vote:", draft)
            self.assertNotIn("decided", draft)
        self.assertEqual(self.text(self.out / "m-vote.md"),
                         f"{BODY} \u2014 2026-11-08: Item 7: ALPR. Result: approved. Vote:   4-1, one absent  . "
                         f"Official record: {SOURCE} {BASE}/meetings/m-vote.html")
        self.assertNotIn("Pat Synthetic", self.text(self.out / "m-vote.md"))  # no per-member names, no derived tally
        self.assertNotIn("verified_by", self.text(self.out / "m-vote.md"))
        self.assertEqual(self.text(self.out / "m-bare-agenda.md"),
                         f"{BODY} \u2014 2026-11-08. Result: approved. Official record: {SOURCE} {BASE}/meetings/m-bare-agenda.html")
        site_path = self.root / "content" / "site.json"
        site = json.loads(site_path.read_text(encoding="utf-8"))
        del site["base_url"]
        site_path.write_text(json.dumps(site), encoding="utf-8")
        build_outcome_social(self.root, now=NOW)
        self.assertEqual(self.text(self.out / "m-denied.md"),
                         f"{BODY} \u2014 2026-11-08: Item 7: ALPR. Result: denied. Official record: {SOURCE}")

    def test_recent_window_uses_outcome_date(self):
        self.meeting("m-today", outcome(on="2026-11-10", verified_at="2026-11-10"))
        self.meeting("m-edge", outcome(on="2026-10-28", verified_at="2026-10-28"), starts_at=LONG_AGO)  # N-1 = 13 days old: first date
        self.meeting("m-old", outcome(on="2026-10-27", verified_at="2026-10-28"), starts_at=LONG_AGO)  # N = 14 days old: out
        self.meeting("m-ancient-meeting-new-outcome", outcome(on="2026-11-10", verified_at="2026-11-10"),
                     starts_at="2026-08-01T09:00:00-07:00")
        self.meeting("m-old-recently-verified", outcome(on="2026-10-01", verified_at="2026-11-10"),
                     starts_at="2026-09-30T09:00:00-07:00")
        # starts_at 2026-11-10T23:30-08:00 is 2026-11-11 07:30Z, but the recorded date is what counts
        self.meeting("m-offset", outcome(on="2026-11-10", verified_at="2026-11-10"), starts_at="2026-11-10T00:30:00-08:00")
        summary = build_outcome_social(self.root, now=NOW)
        self.assertEqual(summary["outcomes"]["written"],
                         ["m-ancient-meeting-new-outcome", "m-edge", "m-offset", "m-today"])
        self.assertEqual(summary["skipped"]["outside_window"], 2)
        wider = build_outcome_social(self.root, within_days=60, now=NOW)
        self.assertEqual(wider["skipped"]["outside_window"], 0)
        self.assertEqual(build_outcome_social(self.root, within_days=1, now=NOW)["skipped"]["outside_window"], 3)
        naive = build_outcome_social(self.root, within_days=1, now=NOW.replace(tzinfo=None))  # naive now is read as UTC
        self.assertEqual(naive["skipped"]["outside_window"], 3)

    def test_no_outcome_never_decided(self):
        self.meeting("m-scheduled", None, starts_at="2026-11-20T09:00:00-08:00")
        self.meeting("m-past-no-outcome", None)
        self.meeting("m-cancelled", None, status="cancelled")
        self.meeting("m-decided")  # eligible without any participation facts
        self.meeting("m-future-outcome", outcome(on="2026-11-11", verified_at="2026-11-11"))
        self.meeting("m-bad-vote", outcome(vote="<b>5-0</b>"))
        (self.meetings / "m-broken.json").write_text("{not json", encoding="utf-8")
        copy = json.loads((self.meetings / "m-decided.json").read_text(encoding="utf-8"))
        (self.meetings / "m-decided-copy.json").write_text(json.dumps(copy), encoding="utf-8")
        summary = build_outcome_social(self.root, now=NOW)
        self.assertEqual(summary["outcomes"]["written"], ["m-decided"])
        self.assertEqual(summary["skipped"], {"cancelled": 1, "no_outcome": 2, "outside_window": 0, "unreadable": 4})
        self.assertEqual(summary["unreadable_files"],
                         ["m-bad-vote.json", "m-broken.json", "m-decided.json", "m-future-outcome.json"])
        self.assertEqual(sorted(p.name for p in self.out.glob("*.md")), ["m-decided.md"])
        self.assertEqual(build_outcome_social(self.root, now=NOW)["skipped"], summary["skipped"])  # counted once, stable
        # no meeting directory: an empty report, not an error; no content/: refused
        shutil.rmtree(self.meetings)
        empty = build_outcome_social(self.root, now=NOW)
        self.assertEqual((empty["unreadable_files"], empty["skipped"]["no_outcome"]), ([], 0))
        shutil.rmtree(self.root / "content")
        with self.assertRaisesRegex(ValueError, "content/ directory"):
            build_outcome_social(self.root, now=NOW)

    def test_platform_caps_only_trim_agenda(self):
        long_item = "Item 7: \u30a2\u30e9 " + "ALPR renewal " * 40
        self.meeting("m-long", outcome(vote="5-0"), agenda_item=long_item)
        summary = build_outcome_social(self.root, ["x", "bluesky", "mastodon", "x"], now=NOW)
        self.assertEqual(list(summary["outcomes"]["platforms"]), ["x", "bluesky", "mastodon"])
        shown = {}
        for platform, limit in (("x", 280), ("bluesky", 300), ("mastodon", 500)):
            text = self.text(self.out / platform / "m-long.md")
            self.assertLessEqual(post_length(text, platform), limit)
            self.assertTrue(text.startswith(f"{BODY} \u2014 2026-11-08: Item 7: "))
            self.assertTrue(text.endswith(f" Result: approved. Vote: 5-0. Official record: {SOURCE} {BASE}/meetings/m-long.html"))
            shown[platform] = text.split(". Result:")[0]
        self.assertTrue(shown["x"].endswith("\u2026") and shown["bluesky"].endswith("\u2026"))
        self.assertEqual(len(set(shown.values())), 3)  # each platform cuts by its own rule
        self.assertFalse(list(self.out.glob("*.md")))
        legacy = build_outcome_social(self.root, now=NOW)
        self.assertEqual(len(self.text(self.out / "m-long.md")), 280)
        self.assertTrue(legacy["outcomes"]["written"] == ["m-long"])
        # fixed fields alone too long: even an empty agenda fails; named; nothing written, later platform too
        shutil.rmtree(self.root / "kit")
        self.meeting("m-ok", outcome())
        self.meeting("m-fixed", outcome(vote="v" * 120), body="B" * 150)
        before = self.tree()
        for platforms, expected in ((["mastodon", "x"], "meeting m-fixed: outcome post cannot fit 280 characters on x"),
                                    (None, "meeting m-fixed: outcome post cannot fit 280 characters")):
            with self.assertRaisesRegex(ValueError, expected):
                build_outcome_social(self.root, platforms, now=NOW)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "kit").exists())
        with self.assertRaises(ValueError):  # the vote cannot be dropped to make room
            outcome_post_text({"id": "z", "body": "B" * 150, "agenda_item": "", "outcome": outcome(vote="v" * 120)}, BASE)
        self.assertEqual(self.tree(), before)
        mixed = [{"id": "z", "body": BODY, "agenda_item": "x" * 400, "outcome": outcome()}]
        self.assertLessEqual(len(outcome_post_text(mixed[0], BASE)), 280)

    def test_edits_suggestions_stale_and_idempotence(self):
        self.meeting("m-one")
        self.meeting("m-two")
        build_outcome_social(self.root, now=NOW)
        fresh = (self.out / "m-one.md").read_bytes()
        again = build_outcome_social(self.root, now=NOW)["outcomes"]
        self.assertEqual(again["written"], ["m-one", "m-two"])
        self.assertEqual((self.out / "m-one.md").read_bytes(), fresh)
        edited = "<!-- organizer edit -->\n\nMy own words.\n"
        (self.out / "m-one.md").write_text(edited, encoding="utf-8")
        for _ in range(2):
            report = build_outcome_social(self.root, now=NOW)["outcomes"]
            self.assertEqual((report["kept_edited"], report["written"], report["suggested"]), (["m-one"], ["m-two"], 1))
            self.assertEqual((self.out / "m-one.md").read_text(encoding="utf-8"), edited)
            self.assertEqual((self.root / "kit" / "social" / "outcomes.suggested" / "m-one.md").read_bytes(), fresh)
        # a corrected outcome updates the untouched draft only
        self.meeting("m-one", outcome(result="amended"))
        self.meeting("m-two", outcome(result="continued"))
        report = build_outcome_social(self.root, now=NOW)["outcomes"]
        self.assertEqual((report["kept_edited"], report["written"]), (["m-one"], ["m-two"]))
        self.assertIn("Result: continued.", self.text(self.out / "m-two.md"))
        self.assertIn("Result: amended.", self.text(self.root / "kit" / "social" / "outcomes.suggested" / "m-one.md"))
        self.assertEqual((self.out / "m-one.md").read_text(encoding="utf-8"), edited)
        # missing and corrupt records: every differing draft counts as edited
        for damage in (lambda p: p.unlink(), lambda p: p.write_text("{corrupt", encoding="utf-8")):
            damage(self.out / ".written.json")
            self.meeting("m-two", outcome(result="denied"))
            report = build_outcome_social(self.root, now=NOW)["outcomes"]
            self.assertEqual(report["kept_edited"], ["m-one", "m-two"])
            self.meeting("m-two", outcome(result="continued"))
            self.assertEqual(build_outcome_social(self.root, now=NOW)["outcomes"]["written"], ["m-two"])
        # leaving the window: stale, listed, never removed
        self.meeting("m-two", outcome(on="2026-10-01", verified_at="2026-10-02"))
        report = build_outcome_social(self.root, now=NOW)["outcomes"]
        self.assertEqual(report["stale"], ["m-two"])
        self.assertTrue((self.out / "m-two.md").is_file())

    def test_symlink_and_private_boundary(self):
        self.meeting("m-one")
        elsewhere = self.base_dir / "elsewhere"
        elsewhere.mkdir()
        social = self.root / "kit" / "social"

        def fresh_kit(*dirs):
            if (self.root / "kit").is_symlink():
                (self.root / "kit").unlink()
            elif (self.root / "kit").exists():
                shutil.rmtree(self.root / "kit")
            for d in dirs:
                (self.root / d).mkdir(parents=True, exist_ok=True)

        cases = [("kit", None, ["kit"]), ("kit/social", "kit", ["kit"]), ("kit/social/outcomes", "kit/social", ["kit/social"]),
                 ("kit/social/outcomes.suggested", "kit/social", ["kit/social"]),
                 ("kit/social/outcomes/x", "kit/social/outcomes", ["kit/social/outcomes"]),
                 ("kit/social/outcomes/x.suggested", "kit/social/outcomes", ["kit/social/outcomes"])]
        for link, _, parents in cases:
            for platforms in (None, ["bluesky", "x"]):
                if (platforms is None and link.endswith(("x", "x.suggested"))) or (platforms and link.endswith("outcomes.suggested")):
                    continue  # that folder is not used by this mode
                fresh_kit()
                if link != "kit":
                    for parent in {os.path.dirname(link)}:
                        (self.root / parent).mkdir(parents=True, exist_ok=True)
                os.symlink(elsewhere, self.root / link)
                with self.subTest(link=link, platforms=platforms):
                    with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                        build_outcome_social(self.root, platforms, now=NOW)
                self.assertEqual(list(elsewhere.iterdir()), [], (link, platforms))
                self.assertFalse((social / "outcomes" / "bluesky").exists(), (link, platforms))
        for folder in ("kit/social/outcomes", "kit/social/outcomes.suggested"):  # a plain file where a folder belongs
            fresh_kit("kit/social")
            (self.root / folder).write_text("file", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "is not a folder"):
                build_outcome_social(self.root, now=NOW)
            self.assertEqual((self.root / folder).read_text(encoding="utf-8"), "file")
        # a draft that is a symlink stays linked; its target is untouched; a safe suggestion is written
        fresh_kit("kit/social/outcomes")
        target = elsewhere / "target.md"
        target.write_text("outside\n", encoding="utf-8")
        os.symlink(target, self.out / "m-one.md")
        report = build_outcome_social(self.root, now=NOW)["outcomes"]
        self.assertEqual(report["kept_edited"], ["m-one"])
        self.assertTrue((self.out / "m-one.md").is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "outside\n")
        suggestion = social / "outcomes.suggested" / "m-one.md"
        self.assertTrue(suggestion.is_file() and not suggestion.is_symlink())
        self.assertTrue(self.text(suggestion).startswith(BODY))
        self.assertEqual(list(elsewhere.iterdir()), [target])
        # nothing outside kit/social/outcomes* changed: content, public and receipts are untouched
        self.assertFalse((self.root / "public").exists())
        self.assertEqual(stat.S_IMODE(os.lstat(self.out).st_mode), stat.S_IMODE(os.lstat(social).st_mode))  # inherited

    def test_cli_modes_and_report(self):
        self.meeting("m-cli", outcome(on=self.today(-2), verified_at=self.today(-1)), starts_at=(
            datetime.now(timezone.utc) - timedelta(days=4)).replace(microsecond=0).isoformat())
        before = self.tree()
        for args, message in ((("--outcomes", "--within-days", "0"), "--within-days must be 1 to 60"),
                              (("--outcomes", "--within-days", "61"), "--within-days must be 1 to 60"),
                              (("--outcomes", "--within-days", "five"), "invalid int value"),
                              (("--within-days", "5"), "needs --meetings or --outcomes"),
                              (("--outcomes", "--meetings"), "--outcomes cannot be combined with --meetings"),
                              (("--outcomes", "--check"), "--check")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2, (args, result.stderr))
            self.assertIn(message, result.stderr)
            self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "kit").exists())
        result = self.cli("--outcomes")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout[:result.stdout.rindex("}") + 1])
        self.assertEqual((summary["outcomes"]["written"], summary["posted"], summary["unreadable_files"]), (["m-cli"], False, []))
        self.assertEqual(list(summary["skipped"]), ["cancelled", "no_outcome", "outside_window", "unreadable"])
        self.assertTrue(result.stdout.rstrip().endswith("Drafts written to kit/social/outcomes. Nothing was posted."))
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.cli("--outcomes", "--within-days", "1").returncode, 0)
        narrow = json.loads(self.cli("--outcomes", "--within-days", "1").stdout.split("\nDrafts written")[0])
        self.assertEqual(narrow["skipped"]["outside_window"], 1)
        platform = self.cli("--outcomes", "--platform", "x", "--platform", "mastodon", "--within-days", "3")
        self.assertEqual(platform.returncode, 0, platform.stderr)
        self.assertTrue((self.out / "x" / "m-cli.md").is_file() and (self.out / "mastodon" / "m-cli.md").is_file())
        # python-level misuse
        for bad in (0, 61, -1, True, 14.0, "14"):
            with self.assertRaisesRegex(ValueError, "1 to 60"):
                build_outcome_social(self.root, within_days=bad, now=NOW)
        for bad in (["tiktok"], [], ["x", "tiktok"], [""]):
            with self.assertRaisesRegex(ValueError, "unknown platform"):
                build_outcome_social(self.root, bad, now=NOW)
        # legal-claim wording is an alert on stderr only; the draft is still written and the exit code is 0
        self.meeting("m-claim", outcome(on=self.today(-1), verified_at=self.today(0)), agenda_item="The city violated state law",
                     starts_at=(datetime.now(timezone.utc) - timedelta(days=3)).replace(microsecond=0).isoformat())
        noted = self.cli("--outcomes")
        self.assertEqual(noted.returncode, 0, noted.stderr)
        self.assertIn("review-required: social/outcomes/m-claim: legal-claim wording", noted.stderr)
        self.assertNotIn("review-required", noted.stdout)
        self.assertTrue((self.out / "m-claim.md").is_file())
        self.assertEqual(len(outcome_review_notes(self.root, now=datetime.now(timezone.utc))), 1)
        broken = self.cli("--outcomes", "--directory", str(self.root / "nowhere"))
        self.assertNotEqual(broken.returncode, 0)

    def test_legacy_modes_unchanged_and_no_network(self):
        now = datetime.now(timezone.utc)
        recent = outcome(on=self.today(-2), verified_at=self.today(-1))
        started = dict(starts_at=(now - timedelta(days=4)).replace(microsecond=0).isoformat())
        self.meeting("m-one", recent, **started)
        build_social(self.root)
        build_meeting_social(self.root, now=now)
        check_before = self.cli("--check")
        self.assertEqual(check_before.returncode, 0, check_before.stdout)
        social = self.root / "kit" / "social"
        findings = {p.name: p.read_bytes() for p in social.glob("*.md")}
        meetings_before = {str(p): p.read_bytes() for p in social.glob("meetings*/**/*") if p.is_file()}
        content_before = {str(p): p.read_bytes() for p in (self.root / "content").rglob("*") if p.is_file()}
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")):
            build_outcome_social(self.root, ["x"], now=now)
            build_outcome_social(self.root, now=now)
        self.assertEqual({p.name: p.read_bytes() for p in social.glob("*.md")}, findings)
        self.assertEqual({str(p): p.read_bytes() for p in social.glob("meetings*/**/*") if p.is_file()}, meetings_before)
        self.assertEqual({str(p): p.read_bytes() for p in (self.root / "content").rglob("*") if p.is_file()}, content_before)
        check_after = self.cli("--check")
        self.assertEqual((check_after.returncode, check_after.stdout), (check_before.returncode, check_before.stdout))
        self.assertEqual(check_social(self.root)["stale"], [])
        self.assertFalse((self.root / "public").exists())
        self.assertEqual(sorted(build_social(self.root)), ["kept_edited", "posted", "stale", "suggested", "written"])
        # stored text that the loader refuses (non-https source, markup) never reaches a draft; a recording link is not a draft link
        base = dict(on=self.today(-2), verified_at=self.today(-1))
        self.meeting("m-evil", outcome(source_url="http://example.invalid/x", **base), **started)
        self.meeting("m-evil2", recent, agenda_item="<script>x</script>", **started)
        self.meeting("m-evil3", outcome(recording_url="https://example.invalid/rec", recording_start="0:01:00", **base), **started)
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")):
            summary = build_outcome_social(self.root, now=now)
        self.assertEqual(summary["unreadable_files"], ["m-evil.json", "m-evil2.json"])
        self.assertFalse((self.out / "m-evil.md").exists() or (self.out / "m-evil2.md").exists())
        self.assertNotIn("example.invalid/rec", self.text(self.out / "m-evil3.md").replace(SOURCE, ""))


if __name__ == "__main__":
    unittest.main()
