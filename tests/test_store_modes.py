"""status --check names loose private modes; status --fix-modes tightens only those (RF2010-5).

Synthetic bytes in a temporary campaign only. Run from the repository root:
python3 -B -m unittest tests.test_store_modes -v
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import store_check  # noqa: E402

FIRST = b"synthetic first mode object\n"
SECOND = b"synthetic second mode object, longer\n"
FULL_HEX = re.compile(r"[0-9a-fA-F]{64}")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permission bits, so they are no boundary")
class StoreModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-store-modes-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(os.umask, os.umask(0o077))
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.private = self.root / "private"
        self.objects = self.private / "objects"
        self.ledger = self.private / "ledger.sqlite"
        self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic", "--state", "CA")
        for data, name in ((FIRST, "first.txt"), (SECOND, "second.txt")):
            path = self.base / name
            path.write_bytes(data)
            self.cli("ingest", "--file", path, "--source-id", "synthetic-source")

    def cli(self, command, *arguments, code=0):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root),
             *map(str, arguments)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def blocks(self, *arguments, code):
        result = self.cli("status", *arguments, code=code)
        decoder, text, found = json.JSONDecoder(), result.stdout, []
        while text.strip():
            block, end = decoder.raw_decode(text.lstrip())
            found.append(block)
            text = text.lstrip()[end:]
        return found, result

    def loose(self, code):
        blocks, _ = self.blocks("--check", code=code)
        return [p for p in blocks[-1]["check"]["problems"] if p["problem"] == "loose_mode"]

    def loosen_three(self):
        os.chmod(self.private, 0o755)
        os.chmod(self.ledger, 0o644)
        os.chmod(self.objects / sha(FIRST), 0o644)

    def test_fresh_init_and_ingest_have_no_loose_modes(self):
        self.assertEqual(mode(self.private), 0o700)
        self.assertEqual(mode(self.ledger), 0o600)
        self.assertEqual(self.loose(0), [])
        self.assertEqual(store_check.mode_problems(self.root), [])

    def test_three_loose_entries_named_with_modes_exit_3(self):
        self.loosen_three()
        self.assertEqual(self.loose(3), [
            {"name": ".", "problem": "loose_mode", "mode": "0o755"},
            {"name": "ledger.sqlite", "problem": "loose_mode", "mode": "0o644"},
            {"name": "objects/" + sha(FIRST)[:12], "problem": "loose_mode", "mode": "0o644"},
        ])

    def test_fix_modes_tightens_exactly_those_and_keeps_bytes(self):
        self.loosen_three()
        before = {p.name: p.read_bytes() for p in self.objects.iterdir()}
        outside = self.root / "campaign.json"
        os.chmod(outside, 0o644)
        blocks, result = self.blocks("--fix-modes", code=0)
        self.assertEqual(blocks[0], {"fixed": [".", "ledger.sqlite", "objects/" + sha(FIRST)[:12]], "skipped": []})
        self.assertEqual(blocks[-1]["check"]["problems"], [])
        self.assertEqual((mode(self.private), mode(self.ledger), mode(self.objects / sha(FIRST))),
                         (0o700, 0o600, 0o600))
        self.assertEqual(mode(outside), 0o644)
        self.assertEqual({p.name: p.read_bytes() for p in self.objects.iterdir()}, before)
        self.assertEqual({sha(data) for data in before.values()}, set(before))
        self.assertEqual(self.loose(0), [])
        self.assertIsNone(FULL_HEX.search(result.stdout + result.stderr))
        self.assertNotIn(str(self.base), result.stdout + result.stderr)

    def test_group_only_reported_owner_only_read_not(self):
        os.chmod(self.objects / sha(FIRST), 0o640)
        os.chmod(self.objects / sha(SECOND), 0o400)
        self.assertEqual(self.loose(3), [{"name": "objects/" + sha(FIRST)[:12], "problem": "loose_mode",
                                          "mode": "0o640"}])
        os.chmod(self.objects / sha(FIRST), 0o600)
        self.assertEqual(self.loose(0), [])

    def test_journal_sibling_reported_and_fixed(self):
        journal = self.private / "ledger.sqlite-journal"
        journal.write_bytes(b"")
        os.chmod(journal, 0o644)
        self.assertEqual(self.loose(3), [{"name": "ledger.sqlite-journal", "problem": "loose_mode", "mode": "0o644"}])
        blocks, _ = self.blocks("--fix-modes", code=0)
        self.assertEqual(blocks[0]["fixed"], ["ledger.sqlite-journal"])
        self.assertEqual(mode(journal), 0o600)

    def test_symlinked_entries_never_chmodded(self):
        outside = self.base / "outside.txt"
        outside.write_bytes(FIRST)
        os.chmod(outside, 0o644)
        (self.objects / "notes-link").symlink_to(outside)
        target = self.objects / sha(FIRST)
        target.unlink()
        target.symlink_to(outside)
        blocks, _ = self.blocks("--fix-modes", code=3)
        self.assertEqual(blocks[0], {"fixed": [], "skipped": []})
        self.assertEqual(blocks[-1]["check"]["problems"], [
            {"sha256": sha(FIRST), "problem": "not_a_regular_file"},
            {"name": "notes-link", "problem": "unexpected_entry"}])
        self.assertEqual(mode(outside), 0o644)
        self.assertTrue(target.is_symlink())

    def test_symlinked_private_stops_before_any_chmod(self):
        moved = self.base / "private-elsewhere"
        os.rename(self.private, moved)
        os.chmod(moved, 0o755)
        self.private.symlink_to(moved, target_is_directory=True)
        result = self.cli("status", "--fix-modes", code=1)
        self.assertIn("Stopped: private/objects must not be a symlink", result.stderr)
        self.assertEqual(mode(moved), 0o755)

    def test_private_absent_reports_nothing_and_creates_nothing(self):
        shutil.rmtree(self.private)
        self.assertEqual(store_check.mode_problems(self.root), [])
        blocks, _ = self.blocks("--fix-modes", code=0)
        self.assertEqual(blocks[0], {"fixed": [], "skipped": []})
        self.assertFalse(self.private.exists())

    def test_replaced_between_check_and_fix_is_skipped(self):
        self.loosen_three()
        target = self.objects / sha(FIRST)
        real, calls, real_chmod = store_check._lstat, [], os.chmod

        def swapped(path):
            calls.append(Path(path))
            if Path(path) == target and calls.count(target) > 1:
                target.unlink()
                target.mkdir()
                real_chmod(target, 0o755)
            return real(path)
        with mock.patch.object(store_check, "_lstat", swapped), \
                mock.patch.object(store_check.os, "chmod", wraps=os.chmod) as chmod:
            report = store_check.fix_modes(self.root)
        self.assertEqual(report, {"fixed": [".", "ledger.sqlite"], "skipped": ["objects/" + sha(FIRST)[:12]]})
        self.assertNotIn(target, [Path(c.args[0]) for c in chmod.call_args_list])
        self.assertEqual(mode(target), 0o755)

    def test_loose_output_has_no_full_hash_or_outside_path(self):
        self.loosen_three()
        os.chmod(self.objects / sha(SECOND), 0o604)
        _, result = self.blocks("--check", code=3)
        self.assertIsNone(FULL_HEX.search(result.stdout + result.stderr))
        self.assertNotIn(str(self.base), result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
