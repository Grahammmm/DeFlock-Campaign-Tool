"""backup-diff: names added/removed/changed since a backup, read-only, exit 0/3/1.

Synthetic campaign directories only.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup  # noqa: E402

HEX64 = re.compile(r"[0-9a-f]{64}")


def cli(*args, timeout=30):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=timeout)


def tree_state(root):
    """Every path under root with its lstat mtime/size and file bytes digest."""
    state = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
        state[path.relative_to(root).as_posix()] = (info.st_mtime_ns, info.st_size, digest)
    return state


class BackupDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-backup-diff-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "campaign"
        r = cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.ingest(b"Synthetic policy text.\n", "demo-1")
        (self.root / "content").mkdir()
        (self.root / "content" / "site.json").write_text('{"schema_version":1,"tagline":"synthetic-tagline"}')
        (self.root / "content" / "gone.md").write_text("to be deleted\n")
        (self.root / "kit").mkdir()
        (self.root / "kit" / "summary.json").write_text("{}")
        self.archive = self.base / "backup.tar"
        self.manifest = backup.export(self.root, self.archive)

    def ingest(self, data, source_id):
        fixture = self.base / (source_id + ".txt")
        fixture.write_bytes(data)
        r = cli("ingest", "--directory", self.root, "--file", fixture, "--source-id", source_id)
        self.assertEqual(r.returncode, 0, r.stderr)
        return hashlib.sha256(data).hexdigest()

    def diff_cli(self, *extra, directory=None):
        return cli("backup-diff", "--file", self.archive, "--directory", directory or self.root, *extra)

    def test_immediate_diff_is_current_exit_0(self):
        r = self.diff_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertTrue(report["current"])
        self.assertEqual((report["added"], report["removed"], report["changed"]), ([], [], []))
        self.assertEqual(report["unchanged"], len(self.manifest["files"]))
        self.assertEqual(report["archive_created_at"], self.manifest["created_at"])
        self.assertNotIn("more", report)
        # untouched ledger: the temp snapshot hashes the same as the archived one
        self.assertNotIn("private/ledger.sqlite", report["changed"])
        r = self.diff_cli("--expect-manifest-sha256", self.manifest["manifest_sha256"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_edit_add_delete_named_exactly_exit_3(self):
        site = self.root / "content" / "site.json"
        old = site.read_bytes()
        site.write_bytes(old.replace(b"synthetic-tagline", b"synthetic-TAGLINE"))  # same size, new bytes
        self.assertEqual(len(site.read_bytes()), len(old))
        (self.root / "kit" / "new.json").write_text("{}")
        (self.root / "content" / "gone.md").unlink()
        r = self.diff_cli()
        self.assertEqual(r.returncode, 3, r.stderr)
        report = json.loads(r.stdout)
        self.assertFalse(report["current"])
        self.assertEqual(report["changed"], ["content/site.json"])
        self.assertEqual(report["added"], ["kit/new.json"])
        self.assertEqual(report["removed"], ["content/gone.md"])
        self.assertEqual(report["unchanged"], len(self.manifest["files"]) - 2)

    def test_new_object_and_ledger_row(self):
        sha = self.ingest(b"Second synthetic record.\n", "demo-2")
        report = backup.diff(self.archive, self.root)
        self.assertFalse(report["current"])
        self.assertEqual(report["added"], ["private/objects/" + sha])
        self.assertEqual(report["changed"], ["private/ledger.sqlite"])
        self.assertEqual(report["removed"], [])

    def test_ledger_row_alone_is_changed(self):
        import sqlite3
        db = sqlite3.connect(self.root / "private" / "ledger.sqlite")
        try:
            db.execute("CREATE TABLE synthetic_extra (x INTEGER)")
            db.execute("INSERT INTO synthetic_extra VALUES (1)")
            db.commit()
        finally:
            db.close()
        report = backup.diff(self.archive, self.root)
        self.assertEqual((report["added"], report["removed"], report["changed"]), ([], [], ["private/ledger.sqlite"]))

    def test_symlink_and_non_hex_objects_ignored(self):
        objects = self.root / "private" / "objects"
        target = self.base / "outside.bin"
        target.write_bytes(b"outside\n")
        os.symlink(target, objects / hashlib.sha256(b"outside\n").hexdigest())
        (objects / "notes.txt").write_text("not an object\n")
        (objects / ("A" * 64)).write_text("uppercase is not hex here\n")
        report = backup.diff(self.archive, self.root)
        self.assertTrue(report["current"], report)
        self.assertEqual(report["added"], [])

    def test_byte_flipped_archive_stops_without_diff(self):
        with tarfile.open(self.archive) as tar:
            offset = tar.getmember("content/site.json").offset_data
        data = bytearray(self.archive.read_bytes())
        data[offset] ^= 0x01
        self.archive.write_bytes(bytes(data))
        (self.root / "kit" / "new.json").write_text("{}")  # drift that must not be reported
        r = self.diff_cli()
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.startswith("Stopped: verification failed"), r.stderr)
        self.assertIn("hash_mismatch", r.stderr)
        self.assertEqual(r.stdout, "")
        with self.assertRaises(backup.BackupError):
            backup.diff(self.archive, self.root)

    def test_expected_manifest_digest_mismatch_exit_1(self):
        r = self.diff_cli("--expect-manifest-sha256", "0" * 64)
        self.assertEqual(r.returncode, 1)
        self.assertIn("manifest_digest_mismatch", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_missing_archive_exit_1(self):
        r = cli("backup-diff", "--file", self.base / "absent.tar", "--directory", self.root)
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.startswith("Stopped: "), r.stderr)
        self.assertEqual(r.stdout, "")

    def test_directory_without_campaign_json(self):
        empty = self.base / "not-a-campaign"
        empty.mkdir()
        r = self.diff_cli(directory=empty)
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.startswith("Stopped: campaign.json not found"), r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(list(empty.iterdir()), [])

    def test_symlinked_private_refused(self):
        moved = self.base / "private-elsewhere"
        (self.root / "private").rename(moved)
        os.symlink(moved, self.root / "private")
        r = self.diff_cli()
        self.assertEqual(r.returncode, 1)
        self.assertIn("unsafe_private_directory", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_symlinked_ledger_refused(self):
        ledger = self.root / "private" / "ledger.sqlite"
        moved = self.base / "ledger-elsewhere.sqlite"
        ledger.rename(moved)
        os.symlink(moved, ledger)
        with self.assertRaisesRegex(backup.BackupError, "invalid_ledger"):
            backup.diff(self.archive, self.root)

    def test_name_lists_capped_at_200(self):
        bulk = self.root / "content" / "bulk"
        bulk.mkdir()
        for i in range(250):
            (bulk / f"f{i:03d}.md").write_text(f"{i}\n")
        report = backup.diff(self.archive, self.root)
        self.assertEqual(len(report["added"]), 200)
        self.assertEqual(report["added"], [f"content/bulk/f{i:03d}.md" for i in range(200)])
        self.assertEqual(report["more"], {"added": 50})
        self.assertEqual(report["removed"], [])

    def test_read_only_and_no_hashes_or_content_in_output(self):
        (self.root / "content" / "site.json").write_text('{"schema_version":1,"tagline":"edited-tagline-words"}')
        self.ingest(b"Third synthetic record.\n", "demo-3")
        archive_before = self.archive.read_bytes()
        archive_mtime = self.archive.stat().st_mtime_ns
        before = tree_state(self.root)
        r = self.diff_cli()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(tree_state(self.root), before)
        self.assertEqual(self.archive.read_bytes(), archive_before)
        self.assertEqual(self.archive.stat().st_mtime_ns, archive_mtime)
        # object members are hash-named by design; nothing else may carry a digest
        stripped = re.sub(r"private/objects/[0-9a-f]{64}", "private/objects/<name>", r.stdout)
        self.assertIsNone(HEX64.search(stripped), stripped)
        self.assertNotIn(self.manifest["manifest_sha256"], r.stdout)
        for text in ("edited-tagline-words", "synthetic-tagline", "Third synthetic record", "sha256", "bytes"):
            self.assertNotIn(text, r.stdout)


if __name__ == "__main__":
    unittest.main()
