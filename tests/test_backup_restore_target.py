"""Restore target ownership: unowned data survives every refusal and failure (synthetic data only)."""
import hashlib
import io
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from campaign_tool import backup

VERIFY_CHANGED = "restore target changed during verification; nothing restored"
RESTORE_CHANGED = "restore target changed during restore; refusing unowned entries"


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def files_under(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


def after_verify(action):
    real = backup.verify

    def wrapper(path, *args, **kwargs):
        report = real(path, *args, **kwargs)
        action()
        return report
    return patch.object(backup, "verify", wrapper)


def before_method(name, action):
    real = getattr(backup._RestoreTarget, name)

    def wrapper(self, *args, **kwargs):
        action(self, *args, **kwargs)
        return real(self, *args, **kwargs)
    return patch.object(backup._RestoreTarget, name, wrapper)


def before_metadata(action):
    """Run ``action`` once, after extraction and re-hashing but before the ledger is read.

    verify() also calls _metadata and _closure on its own temp copy, so those cannot carry the hook.
    """
    done = []

    def once(owner):
        if not done:
            done.append(1)
            action()
    return before_method("check_root", once)


def fail_at_finish(message="invalid_ledger: synthetic"):
    def fail(owner):
        raise backup.BackupError(message)
    return before_method("finish", fail)


class BrokenSource:
    def __init__(self, error):
        self.error = error

    def read(self, size=-1):
        raise self.error


def replace_by_rename(path, writer):
    """Put a different inode at ``path`` while the original is still alive (no inode reuse)."""
    path = Path(path)
    staged = path.with_name(path.name + ".staged")
    writer(staged)
    os.rename(staged, path)


class RestoreTargetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-restore-target-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "campaign"
        self.source.mkdir()
        (self.source / "campaign.json").write_text('{"schema_version": 1}')
        (self.source / "content").mkdir()
        (self.source / "content" / "page.md").write_text("Synthetic page\n")
        (self.source / "kit").mkdir()
        (self.source / "kit" / "agencies.json").write_text("[]\n")
        for number in (1, 2):
            self.add_original(number)
        self.archive = self.base / "backup.tar"
        backup.export(self.source, self.archive)
        self.target = self.base / "restored"
        self.elsewhere = self.base / "elsewhere"
        self.elsewhere.mkdir()

    def add_original(self, number):
        data = f"Synthetic original {number}\n".encode()
        sha = hashlib.sha256(data).hexdigest()
        objects = self.source / "private" / "objects"
        objects.mkdir(parents=True, exist_ok=True)
        (objects / sha).write_bytes(data)
        db = sqlite3.connect(self.source / "private" / "ledger.sqlite")
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS objects(sha256 TEXT PRIMARY KEY, byte_count INTEGER, stored_at TEXT);
                CREATE TABLE IF NOT EXISTS receipts(receipt_id TEXT PRIMARY KEY, object_sha256 TEXT,
                    source_id TEXT, received_at TEXT, original_name TEXT);
            """)
            db.execute("INSERT INTO objects VALUES(?,?,?)", (sha, len(data), "synthetic"))
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)", (str(number), sha, "fixture", "synthetic", "s.txt"))
            db.commit()
        finally:
            db.close()

    def sentinel(self, directory, name="sentinel.txt", data=b"unrelated synthetic bytes", perm=0o640):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        os.chmod(path, perm)
        return path

    def restore_fails(self, message, target=None):
        with self.assertRaisesRegex(backup.BackupError, message):
            backup.restore(self.archive, target or self.target)

    def open_fds(self):
        return len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None

    def assert_only_unowned(self, root, expected_files):
        """The tree holds exactly the expected files and only directories that lead to them."""
        found = files_under(root)
        self.assertEqual(found, expected_files)
        for directory in Path(root).rglob("*"):
            if directory.is_dir():
                self.assertTrue(any(f.startswith(str(directory.relative_to(root)) + "/") for f in found), directory)


class PopulatedDuringVerifyTests(RestoreTargetCase):
    def test_target_populated_during_verify_preserves_all_bytes(self):
        for initially_empty in (False, True):
            with self.subTest(initially_empty=initially_empty):
                target = self.base / f"populated-{initially_empty}"
                if initially_empty:
                    target.mkdir(mode=0o755)
                    os.chmod(target, 0o755)

                def arrive():
                    self.sentinel(target, "campaign.json", b"another process's campaign")
                    self.sentinel(target, "note.txt")
                with after_verify(arrive):
                    self.restore_fails(VERIFY_CHANGED, target)
                self.assertEqual(files_under(target), {"campaign.json": b"another process's campaign",
                                                       "note.txt": b"unrelated synthetic bytes"})
                self.assertEqual(mode(target / "note.txt"), 0o640)
                if initially_empty:
                    self.assertEqual(mode(target), 0o755)

    def test_empty_target_replaced_removed_or_linked_during_verify(self):
        outside = self.sentinel(self.elsewhere / "outside")
        outside_mode = mode(outside.parent)

        def different_dir(target):
            os.rename(target, self.base / "moved-original")
            target.mkdir(mode=0o755)
            os.chmod(target, 0o755)

        def disappears(target):
            target.rmdir()

        def dangling(target):
            target.rmdir()
            target.symlink_to(self.base / "does-not-exist")

        def to_outside(target):
            target.rmdir()
            target.symlink_to(outside.parent)
        cases = {"different": different_dir, "removed": disappears, "dangling": dangling, "symlink": to_outside}
        for name, mutate in cases.items():
            with self.subTest(name):
                target = self.base / f"empty-{name}"
                target.mkdir()
                os.chmod(target, 0o755)
                with after_verify(lambda: mutate(target)):
                    self.restore_fails(VERIFY_CHANGED, target)
                if name == "different":
                    self.assertEqual((os.listdir(target), mode(target)), ([], 0o755))
                elif name == "removed":
                    self.assertFalse(os.path.lexists(target))
                else:
                    self.assertTrue(target.is_symlink())
                    self.assertEqual(os.listdir(outside.parent), ["sentinel.txt"])
                    self.assertEqual((outside.read_bytes(), mode(outside)), (b"unrelated synthetic bytes", 0o640))
                    self.assertEqual(mode(outside.parent), outside_mode)
                self.assertEqual(os.listdir(outside.parent), ["sentinel.txt"])

    def test_empty_target_replaced_by_file_during_verify(self):
        target = self.base / "becomes-file"
        target.mkdir()

        def to_file():
            target.rmdir()
            target.write_bytes(b"a file now")
        with after_verify(to_file):
            self.restore_fails(VERIFY_CHANGED, target)
        self.assertEqual(target.read_bytes(), b"a file now")

    def test_exclusive_root_creation_loses_race(self):
        real_mkdir = os.mkdir
        calls = []

        def racing_mkdir(path, *args, **kwargs):
            if Path(path) == self.target and not calls:
                calls.append(path)
                real_mkdir(path, 0o755)    # the competitor wins; its directory is empty
                os.chmod(path, 0o755)
            return real_mkdir(path, *args, **kwargs)
        with patch.object(os, "mkdir", racing_mkdir):
            self.restore_fails(VERIFY_CHANGED)
        self.assertEqual(calls, [self.target])
        self.assertEqual((os.listdir(self.target), mode(self.target)), ([], 0o755))

    def test_competing_target_in_new_parent_is_not_adopted(self):
        target = self.base / "new-parent" / "restored"
        real_mkdir = os.mkdir

        def racing_mkdir(path, *args, **kwargs):
            if Path(path) == target:
                real_mkdir(path, 0o755)
                os.chmod(path, 0o755)
            return real_mkdir(path, *args, **kwargs)
        with patch.object(os, "mkdir", racing_mkdir):
            self.restore_fails(VERIFY_CHANGED, target)
        self.assertEqual(os.listdir(target), [])
        self.assertEqual(mode(target), 0o755)

    def test_original_refusals_unchanged(self):
        link = self.base / "link"
        link.symlink_to(self.elsewhere)
        self.restore_fails("must not be a symlink", link)
        full = self.base / "full"
        self.sentinel(full)
        self.restore_fails("is not an empty directory", full)
        plain_file = self.base / "plain-file"
        plain_file.write_bytes(b"file")
        self.restore_fails("is not an empty directory", plain_file)
        self.assertEqual((os.listdir(self.elsewhere), mode(full / "sentinel.txt")), ([], 0o640))


class ExtractionOwnershipTests(RestoreTargetCase):
    def test_collision_during_extract_rolls_back_only_owned_files(self):
        state = {"calls": 0, "collider": None}

        def collide(owner, rel, source):
            state["calls"] += 1
            parent = rel.rpartition("/")[0]
            if state["calls"] > 1 and parent and parent in owner.dirs and state["collider"] is None:
                state["collider"] = rel
                self.sentinel(self.target / parent, rel.rpartition("/")[2], b"colliding external file")
                self.sentinel(self.target, "unrelated.txt", b"separate sentinel")
        with before_method("write_file", collide):
            self.restore_fails(RESTORE_CHANGED)
        self.assertIsNotNone(state["collider"])
        self.assert_only_unowned(self.target, {state["collider"]: b"colliding external file",
                                               "unrelated.txt": b"separate sentinel"})
        self.assertEqual(mode(self.target / state["collider"]), 0o640)

    def test_external_directory_in_the_way_is_not_adopted(self):
        state = {"dir": None}

        def squat(owner, rel, source):
            top = rel.partition("/")[0]
            if "/" in rel and top not in owner.dirs and state["dir"] is None:
                state["dir"] = top
                self.sentinel(self.target / top, "inside.txt", b"external directory content")
        with before_method("write_file", squat):
            self.restore_fails(RESTORE_CHANGED)
        self.assertIsNotNone(state["dir"])
        self.assert_only_unowned(self.target, {state["dir"] + "/inside.txt": b"external directory content"})

    def test_unexpected_arrival_before_success_is_preserved(self):
        def arrive():
            self.sentinel(self.target, "foreign.txt", b"foreign file")
            self.sentinel(self.target / "foreign-dir", "inner.txt", b"foreign directory file", 0o600)
            os.chmod(self.target / "foreign-dir", 0o750)
        with before_metadata(arrive):
            self.restore_fails(RESTORE_CHANGED)
        self.assert_only_unowned(self.target, {"foreign.txt": b"foreign file",
                                               "foreign-dir/inner.txt": b"foreign directory file"})
        self.assertEqual(mode(self.target / "foreign.txt"), 0o640)
        self.assertEqual(mode(self.target / "foreign-dir"), 0o750)

    def test_arrival_inside_an_owned_directory_is_preserved(self):
        def arrive():
            self.sentinel(self.target / "private" / "objects", "foreign.bin", b"foreign object")
        with before_metadata(arrive):
            self.restore_fails(RESTORE_CHANGED)
        self.assert_only_unowned(self.target, {"private/objects/foreign.bin": b"foreign object"})

    def test_replaced_owned_entry_is_not_unlinked(self):
        outside = self.sentinel(self.elsewhere, "outside.txt", b"outside the target")
        for name in ("file", "symlink", "directory"):
            with self.subTest(name):
                target = self.base / f"replaced-{name}"

                def replace():
                    if name == "file":
                        replace_by_rename(target / "campaign.json", lambda p: p.write_bytes(b"replacement"))
                    elif name == "symlink":
                        replace_by_rename(target / "campaign.json", lambda p: p.symlink_to(outside))
                    else:
                        moved = self.base / "moved-objects"
                        os.rename(target / "private" / "objects", moved)
                        self.sentinel(target / "private" / "objects", "replacement.bin", b"replacement child")
                with before_metadata(replace):
                    self.restore_fails(RESTORE_CHANGED, target)
                self.assertEqual(outside.read_bytes(), b"outside the target")
                self.assertEqual(os.listdir(self.elsewhere), ["outside.txt"])
                if name == "file":
                    self.assertEqual(files_under(target), {"campaign.json": b"replacement"})
                elif name == "symlink":
                    self.assertTrue((target / "campaign.json").is_symlink())
                    self.assertEqual(os.listdir(target), ["campaign.json"])
                else:
                    self.assertEqual(files_under(target), {"private/objects/replacement.bin": b"replacement child"})
                    self.assertEqual(mode(target / "private" / "objects" / "replacement.bin"), 0o640)
                    shutil.rmtree(self.base / "moved-objects", ignore_errors=True)

    def test_owned_file_swapped_before_reverification_is_refused(self):
        def swap(owner, files):
            replace_by_rename(self.target / "campaign.json", lambda p: p.write_bytes(b"swapped"))
        with before_method("reverify", swap):
            self.restore_fails(RESTORE_CHANGED)
        self.assertEqual(files_under(self.target), {"campaign.json": b"swapped"})


class RootIdentityTests(RestoreTargetCase):
    def make_empty_root(self):
        self.target.mkdir()
        os.chmod(self.target, 0o755)
        return os.lstat(self.target)

    def assert_root_kept(self, before):
        after = os.lstat(self.target)
        self.assertEqual((after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode)),
                         (before.st_dev, before.st_ino, 0o755))
        self.assertEqual(os.listdir(self.target), [])

    def test_success_into_empty_caller_directory_is_private(self):
        before = self.make_empty_root()
        report = backup.restore(self.archive, self.target)
        self.assertEqual(report["restored_to"], str(self.target))
        after = os.lstat(self.target)
        self.assertEqual((after.st_ino, stat.S_IMODE(after.st_mode)), (before.st_ino, 0o700))
        for path in self.target.rglob("*"):
            self.assertEqual(mode(path), 0o700 if path.is_dir() else 0o600, path)
        self.assertTrue((self.target / "private").is_dir())

    def test_failures_preserve_existing_root_identity_and_mode(self):
        real_fchmod = os.fchmod
        real_write = backup._RestoreTarget.write_file

        def failing_write(error):
            calls = []

            def write(owner, rel, source):
                calls.append(1)
                return real_write(owner, rel, BrokenSource(error) if len(calls) == 2 else source)
            return patch.object(backup._RestoreTarget, "write_file", write)

        def failing_chmod():
            calls = []

            def chmod(fd, value):
                calls.append(1)
                if len(calls) == 3:
                    raise OSError("synthetic chmod failure")
                return real_fchmod(fd, value)
            return patch.object(os, "fchmod", chmod)

        def corrupt_write():
            calls = []

            def write(owner, rel, source):
                calls.append(1)
                return real_write(owner, rel, io.BytesIO(b"corrupted") if len(calls) == 2 else source)
            return patch.object(backup._RestoreTarget, "write_file", write)
        cases = [("write", failing_write(OSError("synthetic write failure")), OSError),
                 ("interrupt", failing_write(KeyboardInterrupt()), KeyboardInterrupt),
                 ("chmod", failing_chmod(), OSError),
                 ("hash", corrupt_write(), backup.BackupError),
                 ("metadata", fail_at_finish(), backup.BackupError)]
        for name, injector, error in cases:
            with self.subTest(name):
                before = self.make_empty_root()
                with injector, self.assertRaises(error):
                    backup.restore(self.archive, self.target)
                self.assert_root_kept(before)
                self.target.rmdir()

    def test_failure_with_arrival_restores_mode_of_existing_root(self):
        before = self.make_empty_root()

        def arrive():
            self.sentinel(self.target, "late.txt")
        with before_metadata(arrive):
            self.restore_fails(RESTORE_CHANGED)
        self.assertEqual(files_under(self.target), {"late.txt": b"unrelated synthetic bytes"})
        after = os.lstat(self.target)
        self.assertEqual((after.st_ino, stat.S_IMODE(after.st_mode)), (before.st_ino, 0o755))

    def test_root_replaced_before_success_keeps_the_replacement(self):
        self.make_empty_root()

        def swap():
            os.rename(self.target, self.base / "original-root")
            self.sentinel(self.target, "replacement.txt", b"replacement root")
            os.chmod(self.target, 0o755)
        with before_metadata(swap):
            self.restore_fails(RESTORE_CHANGED)
        self.assertEqual(files_under(self.target), {"replacement.txt": b"replacement root"})
        self.assertEqual(mode(self.target), 0o755)
        self.assertEqual(files_under(self.base / "original-root"), {})


class AbsentTargetTests(RestoreTargetCase):
    def test_new_root_replaced_between_lstat_and_open_is_not_mutated(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                target = self.base / f"open-race-{populated}"
                moved = self.base / f"owned-root-{populated}"
                replacement_ident = []

                def swap(owner, name, message, dir_fd=None):
                    if dir_fd is not None:
                        return
                    self.assertEqual(Path(name), target)
                    os.rename(target, moved)
                    target.mkdir()
                    os.chmod(target, 0o755)
                    replacement_ident.append(backup._identity(os.lstat(target)))
                    if populated:
                        self.sentinel(target)

                before = self.open_fds()
                with before_method("_open_dir", swap), patch.object(os, "fchmod", wraps=os.fchmod) as chmod:
                    self.restore_fails(RESTORE_CHANGED, target)
                self.assertEqual(backup._identity(os.lstat(target)), replacement_ident[0])
                self.assertEqual(mode(target), 0o755)
                self.assertEqual(files_under(target), {"sentinel.txt": b"unrelated synthetic bytes"} if populated else {})
                if populated:
                    self.assertEqual(mode(target / "sentinel.txt"), 0o640)
                chmod.assert_not_called()
                self.assertEqual(os.listdir(moved), [])
                self.assertEqual(self.open_fds(), before)

    def test_absent_target_and_nested_parent_cleanup(self):
        nested = self.base / "new-parent" / "deeper" / "restored"
        backup.restore(self.archive, nested)
        self.assertEqual(mode(nested), 0o700)
        for path in nested.rglob("*"):
            self.assertEqual(mode(path), 0o700 if path.is_dir() else 0o600, path)
        shutil.rmtree(self.base / "new-parent")

        with fail_at_finish():
            self.restore_fails("invalid_ledger", nested)
        self.assertFalse(os.path.lexists(self.base / "new-parent"))

    def test_caller_owned_parent_and_new_unowned_entries_remain(self):
        parent = self.base / "caller-parent"
        parent.mkdir()
        os.chmod(parent, 0o755)
        target = parent / "restored"
        with fail_at_finish():
            self.restore_fails("invalid_ledger", target)
        self.assertEqual((os.listdir(parent), mode(parent)), ([], 0o755))

        nested = self.base / "made-parent" / "restored"

        def arrive():
            self.sentinel(self.base / "made-parent", "neighbour.txt", b"arrived in our parent")
        with before_metadata(arrive), fail_at_finish("count_mismatch"):
            self.restore_fails("count_mismatch", nested)
        self.assertEqual(files_under(self.base / "made-parent"), {"neighbour.txt": b"arrived in our parent"})
        self.assertFalse(os.path.lexists(nested))

        inside = self.base / "arrival-inside" / "restored"

        def arrive_inside():
            self.sentinel(inside, "late.txt", b"arrived in the root")
        with before_metadata(arrive_inside):
            self.restore_fails(RESTORE_CHANGED, inside)
        self.assertEqual(files_under(self.base / "arrival-inside"), {"restored/late.txt": b"arrived in the root"})


class LegacyContractTests(RestoreTargetCase):
    def test_round_trip_report_and_modes_unchanged(self):
        before = self.open_fds()
        report = backup.restore(self.archive, self.target)
        self.assertEqual(set(report), {"path", "ok", "files", "verified", "problems", "manifest_sha256",
                                       "engine_version", "created_at", "counts", "restored_to"})
        self.assertTrue(report["ok"])
        self.assertEqual(files_under(self.target)["campaign.json"], b'{"schema_version": 1}')
        self.assertEqual(self.open_fds(), before)

    def test_refusals_and_failures_do_not_leak_descriptors(self):
        before = self.open_fds()
        with before_metadata(lambda: self.sentinel(self.target, "late.txt")):
            self.restore_fails(RESTORE_CHANGED)
        with after_verify(lambda: self.sentinel(self.base / "other", "x.txt")):
            self.restore_fails(VERIFY_CHANGED, self.base / "other")
        with fail_at_finish("count_mismatch"):
            self.restore_fails("count_mismatch", self.base / "third")
        self.assertEqual(self.open_fds(), before)

    def test_replaced_archive_still_refused_with_no_partial_tree(self):
        real = backup.verify

        self.add_original(3)
        backup.export(self.source, self.base / "other.tar")

        def swap_archive(path, *args, **kwargs):
            report = real(path, *args, **kwargs)
            os.replace(self.base / "other.tar", path)
            return report
        with patch.object(backup, "verify", swap_archive):
            self.restore_fails("manifest_digest_mismatch")
        self.assertFalse(os.path.lexists(self.target))


if __name__ == "__main__":
    unittest.main()
