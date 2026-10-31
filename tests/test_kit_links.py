"""kit/meetings: linked kit folder refused, atomic writes, stale removal only for real files. Offline."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit  # noqa: E402
from campaign_tool.meetings import LegistarClient, fixture_opener, relevant_events, write_meetings  # noqa: E402

FIXTURE = json.loads((REPO_ROOT / "tests" / "fixtures" / "legistar-events.json").read_text())
DRAFT = "ca-morro-bay-police.md"


def temps(folder):
    return list(Path(folder).rglob(".*.tmp"))


class KitLinkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-links-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        self.assertEqual(self.cli("init", "--directory", self.root, "--name", "Synthetic", "--county",
                                  "San Luis Obispo", "--state", "CA").returncode, 0)
        self.kit, self.outside = self.root / "kit", Path(temporary.name) / "outside"
        self.outside.mkdir()
        (self.outside / "keep.md").write_text("organizer note\n")

    def cli(self, *arguments):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)

    def refused(self, message):
        result = self.cli("kit", "--directory", self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: " + message, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summary = kit.build_kit(self.root)
        return summary, out.getvalue()

    def assert_outside_unchanged(self, *extra):
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), sorted(["keep.md", *extra]))
        self.assertEqual((self.outside / "keep.md").read_text(), "organizer note\n")

    def test_happy_path_then_deselected_regular_draft_removed(self):
        summary, _ = self.build()
        for name in ("agencies.json", "governing-bodies.json", "law.json", "summary.json", "README.md"):
            self.assertTrue((self.kit / name).is_file(), name)
        self.assertEqual(len(list((self.kit / "requests").glob("*.md"))), summary["requests_drafted"])
        self.assertEqual(temps(self.kit), [])
        path = self.kit / "agencies.json"
        agencies = json.loads(path.read_text())
        for entry in agencies:
            entry["selected"] = entry["agency_id"] + ".md" != DRAFT
        path.write_text(json.dumps(agencies))
        _, out = self.build()
        self.assertFalse((self.kit / "requests" / DRAFT).exists())
        self.assertEqual((out, temps(self.kit)), ("", []))

    def test_symlinked_or_dangling_kit_refused(self):
        self.kit.symlink_to(self.outside, target_is_directory=True)
        self.refused("kit/ must not be a symlink")
        self.assert_outside_unchanged()
        self.kit.unlink()
        self.kit.symlink_to(self.outside / "missing")
        self.refused("kit/ must not be a symlink")
        self.assert_outside_unchanged()

    def test_symlinked_requests_refused_and_other_md_kept(self):
        self.kit.mkdir()
        (self.outside / "other.md").write_text("other\n")
        (self.kit / "requests").symlink_to(self.outside, target_is_directory=True)
        self.refused("requests/ must not be a symlink")
        self.assert_outside_unchanged("other.md")
        self.assertEqual([p.name for p in self.kit.iterdir()], ["requests"])

    def test_kit_or_requests_as_regular_file_refused(self):
        self.kit.write_text("not a folder\n")
        self.refused("kit/ is not a folder")
        self.assertEqual(self.kit.read_text(), "not a folder\n")
        self.kit.unlink()
        self.kit.mkdir()
        (self.kit / "requests").write_text("x\n")
        self.refused("requests/ is not a folder")
        self.assertEqual([p.name for p in self.kit.iterdir()], ["requests"])

    def test_linked_stale_draft_kept_and_named_once(self):
        self.build()
        link = self.kit / "requests" / "old.md"
        link.symlink_to(self.outside / "keep.md")
        (self.kit / "requests" / "notes.md").mkdir()
        _, out = self.build()
        self.assertTrue(link.is_symlink() and (self.kit / "requests" / "notes.md").is_dir())
        self.assertEqual(out, "kept linked file requests/old.md\n")
        self.assert_outside_unchanged()

    def test_failed_draft_replace_keeps_previous_draft_and_no_temp(self):
        self.build()
        draft = self.kit / "requests" / DRAFT
        draft.write_text("previous draft\n")
        real_replace = os.replace

        def failing(src, dst):
            if Path(dst).name == DRAFT:
                raise OSError("synthetic replace failure")
            return real_replace(src, dst)

        argv = ["campaign_tool", "kit", "--directory", str(self.root)]
        with mock.patch.object(sys, "argv", argv), mock.patch("os.replace", side_effect=failing), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as caught:
            cli.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("Stopped: synthetic replace failure", err.getvalue())
        self.assertEqual(draft.read_text(), "previous draft\n")
        self.assertEqual(temps(self.kit), [])

    def test_meetings_linked_kit_refused_real_kit_written_atomically(self):
        meetings = relevant_events(LegistarClient("examplecity", opener=fixture_opener(FIXTURE)),
                                   since=date(2026, 9, 30))
        self.kit.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "kit/ must not be a symlink"):
            write_meetings(self.root, meetings, "examplecity")
        self.assert_outside_unchanged()
        self.kit.unlink()
        target = write_meetings(self.root, meetings, "examplecity")
        self.assertEqual(self.kit.stat().st_mode & 0o777, 0o700)
        before = target.read_text()
        self.assertEqual(len(json.loads(before)["meetings"]), 2)
        with mock.patch("os.replace", side_effect=OSError("synthetic")), self.assertRaises(OSError):
            write_meetings(self.root, [], "examplecity")
        self.assertEqual((target.read_text(), temps(self.kit)), (before, []))


if __name__ == "__main__":
    unittest.main()
