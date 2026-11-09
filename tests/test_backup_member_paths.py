"""Backup manifests must name only members the archive format can restore and re-export (synthetic data only)."""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup, backup_crypt  # noqa: E402
from tests.test_backup import FakeWorkspace  # noqa: E402

UNSUPPORTED = "invalid_manifest: unsupported_member_path"
SHA_A = hashlib.sha256(b"synthetic original A\n").hexdigest()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run_cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


class MemberPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-member-paths-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def archive(self, files, name="crafted.tar", extra=None, manifest_files=None):
        """Self-consistent PAX tar: valid hashes and counts; ``extra`` members are physical-only."""
        entries = {"campaign.json": b"{}"}
        entries.update(files)
        manifest = {"schema_version": 1, "engine_version": "synthetic", "created_at": "2026-01-01T00:00:00+00:00",
                    "source": {"kind": "local", "root_name": "campaign"},
                    "files": {n: {"sha256": sha(d), "bytes": len(d)} for n, d in entries.items()}}
        if manifest_files:
            manifest["files"].update(manifest_files)
        manifest["counts"] = {"files": len(manifest["files"]), "objects": 0, "receipts": 0,
                              "bytes": sum(e["bytes"] for e in manifest["files"].values())}
        path = self.base / name
        with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
            for member_name, data in [(backup.MANIFEST, json.dumps(manifest).encode()), *entries.items(),
                                      *(extra or {}).items()]:
                info = tarfile.TarInfo(member_name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def assert_refused(self, path, pattern=UNSUPPORTED):
        with self.assertRaisesRegex(backup.BackupError, pattern):
            backup.verify(path)

    def test_reserved_directory_file_refused_before_restore_writes(self):
        for name in ("private", "content", "kit", "private/objects"):
            with self.subTest(member=name):
                path = self.archive({name: b"regular file in a directory slot"}, name=name.replace("/", "_") + ".tar")
                before = path.read_bytes()
                self.assert_refused(path)
                absent = self.base / ("absent-" + name.replace("/", "_"))
                with self.assertRaisesRegex(backup.BackupError, UNSUPPORTED):
                    backup.restore(path, absent)
                self.assertFalse(absent.exists())
                empty = self.base / ("empty-" + name.replace("/", "_"))
                empty.mkdir(mode=0o755)
                os.chmod(empty, 0o755)
                inode = os.lstat(empty).st_ino
                with self.assertRaisesRegex(backup.BackupError, UNSUPPORTED):
                    backup.restore(path, empty)
                self.assertEqual(os.lstat(empty).st_ino, inode)
                self.assertEqual(os.lstat(empty).st_mode & 0o777, 0o755)
                self.assertEqual(list(empty.iterdir()), [])
                self.assertEqual(path.read_bytes(), before)

    def test_unknown_and_prefix_lookalike_paths_refuse(self):
        cases = ["untracked.txt", "private/unknown.bin", "content-other/x", "kit-old/x", "contentx",
                 "private/ledger.sqlite/extra", "private/d1-export.json/extra", "private/objects-old/x",
                 "campaign.json/extra", "private/ledger.sqlite.bak", "private/objects/" + SHA_A.upper()[:64]]
        for name in cases:
            with self.subTest(member=name):
                path = self.archive({name: b"synthetic"}, name="case.tar")
                with self.assertRaises(backup.BackupError) as raised:
                    backup.verify(path)
                self.assertRegex(str(raised.exception), "unsupported_member_path|object_hash_mismatch|conflicting_member_paths")

    def test_allowed_names_pass_the_profile(self):
        files = {"content/a.md": b"x", "content/deep/er/b.md": b"", "kit/nested/notes.txt": b"y"}
        self.assertTrue(backup.verify(self.archive(files))["ok"])

    def test_object_path_validation_preserves_existing_codes(self):
        data = b"synthetic original A\n"
        for name, pattern in (("private/objects/not-a-hash", "object_hash_mismatch"),
                              ("private/objects/" + sha(data) + "/extra", "object_hash_mismatch"),
                              ("private/objects//" + sha(data), "unsafe_member_name"),
                              ("content//a", "unsafe_member_name"), ("content/../x", "unsafe_member_name"),
                              ("/content/a", "unsafe_member_name"), ("content\\a", "unsafe_member_name")):
            with self.subTest(member=name):
                self.assert_refused(self.archive({name: data}), pattern)
        # digest/name disagreement keeps its code
        self.assert_refused(self.archive({"private/objects/" + sha(b"other"): data}), "object_hash_mismatch")
        # an explicit parent conflict keeps its code
        self.assert_refused(self.archive({"content": b"x", "content/a": b"y"}), "conflicting_member_paths")
        self.assert_refused(self.archive({"private/objects": b"x", "private/objects/" + sha(data): data}),
                            "conflicting_member_paths")

    def test_all_reader_entry_points_refuse_unsupported_manifest(self):
        path = self.archive({"untracked.txt": b"synthetic"})
        before = path.read_bytes()
        root = self.base / "campaign"
        root.mkdir()
        (root / "campaign.json").write_text("{}")
        with self.assertRaisesRegex(backup.BackupError, UNSUPPORTED):
            backup.diff(path, root)
        for argv in (("verify", "--file", path), ("restore", "--file", path, "--directory", self.base / "cli-target"),
                     ("backup-diff", "--file", path, "--directory", root)):
            with self.subTest(command=argv[0]):
                result = run_cli(*argv)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("unsupported_member_path", result.stderr + result.stdout)
        self.assertFalse((self.base / "cli-target").exists())
        folder = self.base / "folder"
        folder.mkdir()
        (folder / "bad.tar").write_bytes(before)
        report = backup.list_archives(folder)
        self.assertEqual([(a["name"], a["verify"]) for a in report["archives"]], [("bad.tar", "failed")])
        self.assertIn("unsupported_member_path", report["archives"][0]["reason"])
        listed = run_cli("backup-list", "--dir", folder)
        self.assertEqual(listed.returncode, 3, listed.stderr)
        receipt = backup.drill(path, doctor=lambda root: {}, store_check=lambda root: {"problems": []})
        self.assertFalse(receipt["passed"])
        self.assertEqual([(s["step"], s["ok"]) for s in receipt["steps"]],
                         [("verify", False), ("restore", None), ("store_check", None), ("doctor", None)])
        self.assertEqual(path.read_bytes(), before)

    def test_encrypted_reader_plumbing_applies_the_same_refusal(self):
        path = self.archive({"untracked.txt": b"synthetic"})

        @contextlib.contextmanager
        def decrypted(_path, _home=None):
            yield path

        with patch.object(backup_crypt, "is_encrypted", return_value=True), \
                patch.object(backup_crypt, "decrypted", decrypted):
            with self.assertRaisesRegex(backup.BackupError, UNSUPPORTED):
                backup.verify(self.base / "pretend.tar.gpg")

    def test_unlisted_tar_member_keeps_not_in_manifest(self):
        path = self.archive({}, extra={"untracked.txt": b"physical only"})
        before = path.read_bytes()
        with self.assertRaisesRegex(backup.BackupError, "not_in_manifest"):
            backup.verify(path)
        self.assertEqual(path.read_bytes(), before)
        good = self.archive({"content/a.md": b"ok"}, name="good.tar")
        report = backup.verify(good)
        self.assertTrue(report["ok"])
        self.assertTrue(backup.verify(good, report["manifest_sha256"])["ok"])

    def members(self, path):
        with tarfile.open(path) as tar:
            return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}

    def test_allowed_local_and_hosted_paths_round_trip(self):
        data = b"synthetic original A\n"
        root = self.base / "local"
        (root / "content" / "deep").mkdir(parents=True)
        (root / "kit" / "nested").mkdir(parents=True)
        (root / "private" / "objects").mkdir(parents=True)
        (root / "campaign.json").write_text("{}")
        (root / "content" / "deep" / "empty.md").write_bytes(b"")
        (root / "content" / "page.md").write_bytes(b"page")
        (root / "kit" / "nested" / "notes.txt").write_bytes(b"notes")
        (root / "private" / "objects" / sha(data)).write_bytes(data)
        db = sqlite3.connect(root / "private" / "ledger.sqlite")
        try:
            db.executescript("""
                CREATE TABLE objects(sha256 TEXT PRIMARY KEY, byte_count INTEGER, stored_at TEXT);
                CREATE TABLE receipts(receipt_id TEXT PRIMARY KEY, object_sha256 TEXT,
                    source_id TEXT, received_at TEXT, original_name TEXT);
            """)
            db.execute("INSERT INTO objects VALUES(?,?,?)", (sha(data), len(data), "synthetic"))
            db.execute("INSERT INTO receipts VALUES('1',?,'fixture','synthetic','a.txt')", (sha(data),))
            db.commit()
        finally:
            db.close()
        first = self.base / "first.tar"
        backup.export(root, first)
        self.assertTrue(backup.verify(first)["ok"])
        restored = self.base / "restored"
        backup.restore(first, restored)
        second = self.base / "second.tar"
        backup.export(restored, second)
        one, two = self.members(first), self.members(second)
        one.pop(backup.MANIFEST)
        two.pop(backup.MANIFEST)
        self.assertEqual(set(one), set(two))
        self.assertIn("content/deep/empty.md", one)
        self.assertTrue(all(one[name] == two[name] for name in one if name != "private/ledger.sqlite"))

        hosted = self.base / "hosted.tar"
        manifest = backup.export_hosted(FakeWorkspace({sha(data): data}), hosted)
        self.assertTrue(backup.verify(hosted, manifest["manifest_sha256"])["ok"])
        hosted_root = self.base / "hosted-restored"
        backup.restore(hosted, hosted_root)
        again = self.base / "hosted-again.tar"
        result = backup.export(hosted_root, again)
        self.assertEqual(set(result["files"]), set(manifest["files"]) | set(result["files"]))
        self.assertIn("private/d1-export.json", result["files"])
        self.assertIn("private/objects/" + sha(data), result["files"])
        self.assertTrue(backup.verify(again)["ok"])


if __name__ == "__main__":
    unittest.main()
