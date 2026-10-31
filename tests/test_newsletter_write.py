"""Newsletter draft writes: all three files or none; a linked kit/ is refused. Offline; nothing is sent."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter import build_draft, write_draft  # noqa: E402

MANIFEST = {"schema_version": 1, "campaign": {"name": "Synthetic Campaign", "base_url": None, "county_name": "Synthetic"},
            "since": None, "findings": [], "meetings": []}
NAMES = ("newsletter-draft.html", "newsletter-draft.txt", "newsletter-draft.json")
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


def failing_on(real, call):
    count = [0]

    def side_effect(*args, **kwargs):
        count[0] += 1
        if count[0] == call:
            raise OSError("synthetic disk full")
        return real(*args, **kwargs)
    return side_effect


class NewsletterWriteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-write-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(self.root), "--name",
                        "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        self.kit, self.outside = self.root / "kit", self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("organizer note\n")
        self.draft = build_draft(MANIFEST, now=NOW)

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(self.root), *extra],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)

    def contents(self):
        return {name: (self.kit / name).read_bytes() for name in NAMES if (self.kit / name).exists()}

    def temps(self):
        return sorted(p.name for p in self.tmp.rglob(".*.tmp"))

    def seed_previous(self):
        self.kit.mkdir(mode=0o700, exist_ok=True)
        for name in NAMES:
            (self.kit / name).write_text("previous " + name + "\n")
        return self.contents()

    def assert_refused(self, result, message):
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: " + message, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")

    def assert_outside_unchanged(self):
        self.assertEqual([p.name for p in self.outside.iterdir()], ["keep.txt"])
        self.assertEqual((self.outside / "keep.txt").read_text(), "organizer note\n")

    def test_happy_path_writes_three_files_with_the_old_bytes_and_mode_0600(self):
        self.seed_previous()
        paths = write_draft(self.root, self.draft)
        self.assertEqual(paths, [self.kit / name for name in NAMES])
        expected = {NAMES[0]: self.draft["html"], NAMES[1]: self.draft["text"],
                    NAMES[2]: json.dumps(self.draft, indent=2, ensure_ascii=False) + "\n"}
        self.assertEqual(self.contents(), {name: text.encode("utf-8") for name, text in expected.items()})
        for name in NAMES:
            self.assertEqual(stat.S_IMODE((self.kit / name).stat().st_mode), 0o600, name)
        self.assertEqual(self.temps(), [])
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertFalse(out["sent"])
        self.assertEqual(out["written"], [str(self.kit.resolve() / name) for name in NAMES])
        self.assertEqual(self.temps(), [])

    def test_symlinked_kit_is_refused_and_the_target_is_unchanged(self):
        self.kit.symlink_to(self.outside, target_is_directory=True)
        self.assert_refused(self.cli(), "kit/ must not be a symlink")
        self.assert_outside_unchanged()
        with self.assertRaisesRegex(ValueError, "kit/ must not be a symlink"):
            write_draft(self.root, self.draft)
        self.assert_outside_unchanged()
        self.assertEqual(self.temps(), [])

    def test_kit_as_a_regular_file_is_refused(self):
        self.kit.write_text("not a folder\n")
        self.assert_refused(self.cli(), "kit/ is not a folder")
        self.assertEqual(self.kit.read_text(), "not a folder\n")
        self.assertEqual(self.temps(), [])

    def test_failure_on_any_temp_write_keeps_the_previous_three_files(self):
        for call in (1, 2, 3):
            previous = self.seed_previous()
            with mock.patch("os.fdopen", side_effect=failing_on(os.fdopen, call)):
                with self.assertRaisesRegex(OSError, "^synthetic disk full$"):
                    write_draft(self.root, self.draft)
            self.assertEqual(self.contents(), previous, call)
            self.assertEqual(self.temps(), [], call)

    def test_failure_on_a_replace_changes_nothing_or_names_the_files_already_replaced(self):
        for call, replaced in ((1, None), (2, "newsletter-draft.html"), (3, "newsletter-draft.html, newsletter-draft.txt")):
            previous = self.seed_previous()
            with mock.patch("os.replace", side_effect=failing_on(os.replace, call)):
                with self.assertRaises(OSError) as caught:
                    write_draft(self.root, self.draft)
            self.assertEqual(self.temps(), [], call)
            if replaced is None:
                self.assertEqual((str(caught.exception), self.contents()), ("synthetic disk full", previous))
                continue
            self.assertEqual(str(caught.exception), "partial newsletter draft: " + replaced)
            self.assertEqual(self.contents()[NAMES[0]], self.draft["html"].encode("utf-8"))
            self.assertEqual(self.contents()[NAMES[2]], previous[NAMES[2]], "json is replaced last")

    def test_planted_symlink_is_replaced_never_written_through(self):
        self.kit.mkdir(mode=0o700, exist_ok=True)
        (self.kit / NAMES[1]).symlink_to(self.outside / "keep.txt")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(((self.kit / NAMES[1]).is_symlink(), (self.kit / NAMES[1]).is_file()), (False, True))
        self.assert_outside_unchanged()

    def test_bad_manifest_stops_and_writes_nothing(self):
        not_utf8 = self.tmp / "not-utf8.json"
        not_utf8.write_bytes(b'{"schema_version": 1, "campaign": {"name": "\xff"}}')
        too_large = self.tmp / "too-large.json"
        too_large.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        for manifest, message in ((not_utf8, "Stopped: "), (too_large, "Stopped: manifest too large")):
            result = self.cli("--manifest", str(manifest))
            self.assertEqual(result.returncode, 1, manifest.name)
            self.assertIn(message, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(self.contents(), {}, manifest.name)
            self.assertEqual(self.temps(), [])


if __name__ == "__main__":
    unittest.main()
