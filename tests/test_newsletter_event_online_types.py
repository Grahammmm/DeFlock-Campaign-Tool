"""RF1831-1: event manifests accept ``online`` only as a literal boolean. Synthetic; nothing is sent."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter import build_draft  # noqa: E402
from campaign_tool.newsletter.draft import DraftError  # noqa: E402
from tests import test_newsletter_events as fixtures  # noqa: E402

NOW = fixtures.NOW
NAMES = ("newsletter-draft.html", "newsletter-draft.txt", "newsletter-draft.json")
CANARY = "canary-flag-value"
BAD_FLAGS = (0, 1, 0.0, 1.0, -1, 2, None, [], [1], {}, {"a": 1}, "", "true", CANARY)


def ev(**extra):
    return fixtures.event("one", "2027-03-02T10:00:00Z", **extra)


class OnlineTypeTests(unittest.TestCase):
    def refused(self, events):
        with self.assertRaises(DraftError) as ctx:
            build_draft(fixtures.manifest(events), NOW)
        return str(ctx.exception)

    def test_non_boolean_flags_refuse_with_fixed_text(self):
        for flag in BAD_FLAGS:
            for venue in ("Synthetic Hall", None):
                with self.subTest(flag=flag, venue=venue):
                    message = self.refused([ev(online=flag, venue=venue) if venue else ev(online=flag, venue=None)])
                    self.assertEqual(message, "events[0].online: true or false required")
                    self.assertNotIn(CANARY, message)

    def test_bad_flag_cannot_hide_outside_window_or_in_second_event(self):
        for starts in ("2020-01-01T10:00:00Z", "2035-01-01T10:00:00Z"):
            with self.subTest(starts=starts):
                self.assertIn("events[0].online", self.refused([fixtures.event("far", starts, online=1)]))
        message = self.refused([ev(), fixtures.event("two", "2027-03-03T10:00:00Z", online=None)])
        self.assertEqual(message, "events[1].online: true or false required")

    def test_literal_modes_unchanged(self):
        base = build_draft(fixtures.manifest([ev()]), NOW)
        self.assertEqual(build_draft(fixtures.manifest([ev(online=False)]), NOW), base)
        online = build_draft(fixtures.manifest([ev(online=True, venue=None)]), NOW)
        self.assertIn("Online", online["text"])
        self.assertEqual(online["counts"]["events"], 1)
        self.assertIn("events[0].venue", self.refused([ev(online=True)]))
        self.assertIn("events[0].venue", self.refused([ev(online=False, venue=None)]))

    def test_cli_refuses_without_replacing_saved_drafts(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-online-types-") as tmp:
            root = Path(tmp) / "campaign"
            subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root), "--name",
                            "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                           capture_output=True, timeout=30)
            kit = root / "kit"
            kit.mkdir(mode=0o700, exist_ok=True)
            for name in NAMES:
                (kit / name).write_bytes(b"previous " + name.encode())

            def snapshot():
                return {n: ((kit / n).read_bytes(), (kit / n).stat().st_mtime_ns, (kit / n).stat().st_mode)
                        for n in sorted(p.name for p in kit.iterdir())}

            before = snapshot()
            for flag in (1, 0.0, None):
                path = Path(tmp) / "manifest.json"
                path.write_text(json.dumps(fixtures.manifest([ev(online=flag)])))
                proc = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(root),
                                       "--manifest", str(path)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
                with self.subTest(flag=flag):
                    self.assertEqual((proc.returncode, proc.stdout), (1, ""), proc.stderr)
                    self.assertIn("Stopped:", proc.stderr)
                    self.assertIn("events[0].online: true or false required", proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertEqual(snapshot(), before)
            path.write_text(json.dumps(fixtures.manifest([ev(online=False)])))
            proc = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(root),
                                   "--manifest", str(path)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
