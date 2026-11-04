"""RF0210-1: plain local and hosted backups reach their final name only after verification.

Synthetic campaign data and a fake hosted client only; no network.
"""
import contextlib
import errno
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup  # noqa: E402

PREFIX = ".deflock-archive-"
EXIT = 73
FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
FIXED_NAME = "deflock-backup-20260102T030405Z.tar"
HOSTED_DATA = b"hosted synthetic original\n"
HOSTED_SHA = hashlib.sha256(HOSTED_DATA).hexdigest()


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=30)


def make_root(base):
    root = Path(base) / "campaign"
    r = cli("init", "--directory", root, "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
    assert r.returncode == 0, r.stderr
    fixture = Path(base) / "policy.txt"
    fixture.write_bytes(b"Synthetic policy text.\n")
    r = cli("ingest", "--directory", root, "--file", fixture, "--source-id", "demo-1")
    assert r.returncode == 0, r.stderr
    return root


def fake_client():
    from tests.test_backup import FakeWorkspace
    return FakeWorkspace({HOSTED_SHA: HOSTED_DATA})


def staging(parent):
    return sorted(p for p in Path(parent).iterdir() if p.name.startswith(PREFIX))


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def child_main(producer, point, work, record):
    """Run one producer with an abrupt os._exit(73) at ``point``; never returns normally."""
    from campaign_tool import backup as b
    work = Path(work)
    real_verify, real_link, real_add = b.verify, os.link, b._add_bytes

    def verify(path, expected_manifest_sha256=None, **kw):
        Path(record).write_text(expected_manifest_sha256 or "")
        if point == "pre-verify":
            os._exit(EXIT)
        return real_verify(path, expected_manifest_sha256=expected_manifest_sha256, **kw)

    def link(src, dst):
        if point == "pre-link":
            os._exit(EXIT)
        real_link(src, dst)
        if point == "post-commit":
            os._exit(EXIT)

    def add(*a, **kw):
        real_add(*a, **kw)
        if point == "manifest":
            os._exit(EXIT)

    b.verify, b._add_bytes = verify, add
    os.link = link
    if producer == "local":
        b.export(work / "campaign", work / "out.tar")
    elif producer == "hosted":
        b.export_hosted(fake_client(), work / "out.tar")
    else:
        b.export_into(work / "campaign", work / "folder", now=FIXED_NOW)
    os._exit(0)  # reaching here means the hook never fired


class PublishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="synthetic-publish-")
        cls.addClassCleanup(cls.shared.cleanup)
        cls.root = make_root(cls.shared.name)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-publish-case-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.out_dir = self.base / "out"
        self.out_dir.mkdir(mode=0o700)
        self.out = self.out_dir / "backup.tar"

    def producers(self):
        return {"local": lambda out: backup.export(self.root, out),
                "hosted": lambda out: backup.export_hosted(fake_client(), out)}

    def assertClean(self):
        self.assertEqual(staging(self.out_dir), [])

    def test_final_absent_until_verified_local_and_hosted(self):
        real_verify = backup.verify
        for name, run in self.producers().items():
            with self.subTest(producer=name):
                out = self.out_dir / f"{name}.tar"
                seen = []

                def spy(path, expected_manifest_sha256=None, **kw):
                    seen.append((Path(path), os.path.lexists(out), mode(Path(path).parent), mode(path),
                                 Path(path).stat().st_size > 0))
                    return real_verify(path, expected_manifest_sha256=expected_manifest_sha256, **kw)

                with mock.patch.object(backup, "verify", spy):
                    manifest = run(out)
                self.assertEqual(len(seen), 1)
                path, final_existed, dir_mode, file_mode, nonempty = seen[0]
                self.assertEqual(path.parent.parent, self.out_dir)
                self.assertTrue(path.parent.name.startswith(PREFIX))
                self.assertEqual(path.name, "archive.partial")
                self.assertFalse(path.name.endswith((".tar", ".tar.gpg")))
                self.assertFalse(final_existed)
                self.assertEqual((dir_mode, file_mode, nonempty), (0o700, 0o600, True))
                self.assertEqual(mode(out), 0o600)
                self.assertTrue(backup.verify(out, expected_manifest_sha256=manifest["manifest_sha256"])["ok"])
                target = self.base / f"restored-{name}"
                backup.restore(out, target)
                self.assertTrue((target / "campaign.json").is_file())
                self.assertClean()

    def test_write_verify_and_fsync_failures_leave_no_final(self):
        campaign = self.root / "campaign.json"
        before = campaign.read_bytes()

        class FailingFetch:
            def get(self, path):
                if "originals" in path:
                    raise RuntimeError("synthetic fetch failure")
                return fake_client().get(path)

        class WrongBytes:
            def get(self, path):
                return b"not the original" if "originals" in path else fake_client().get(path)

        cases = [
            ("local tar write", lambda: backup.export(self.root, self.out), mock.patch.object(backup, "_add_bytes", side_effect=OSError("disk full")), OSError, "disk full"),
            ("hosted tar write", lambda: backup.export_hosted(fake_client(), self.out), mock.patch.object(backup, "_add_bytes", side_effect=OSError("disk full")), OSError, "disk full"),
            ("hosted fetch", lambda: backup.export_hosted(FailingFetch(), self.out), contextlib.nullcontext(), RuntimeError, "synthetic fetch failure"),
            ("hosted digest", lambda: backup.export_hosted(WrongBytes(), self.out), contextlib.nullcontext(), backup.BackupError, "did not match its hash"),
            ("local verify", lambda: backup.export(self.root, self.out), mock.patch.object(backup, "verify", side_effect=backup.BackupError("manifest_digest_mismatch")), backup.BackupError, "manifest_digest_mismatch"),
            ("hosted verify", lambda: backup.export_hosted(fake_client(), self.out), mock.patch.object(backup, "verify", side_effect=backup.BackupError("closure_mismatch")), backup.BackupError, "closure_mismatch"),
            ("local fsync", lambda: backup.export(self.root, self.out), mock.patch.object(backup.os, "fsync", side_effect=OSError(errno.EIO, "fsync failed")), OSError, "fsync failed"),
            ("hosted fsync", lambda: backup.export_hosted(fake_client(), self.out), mock.patch.object(backup.os, "fsync", side_effect=OSError(errno.EIO, "fsync failed")), OSError, "fsync failed"),
            ("local interrupt", lambda: backup.export(self.root, self.out), mock.patch.object(backup, "verify", side_effect=KeyboardInterrupt), KeyboardInterrupt, ""),
            ("hosted interrupt", lambda: backup.export_hosted(fake_client(), self.out), mock.patch.object(backup, "verify", side_effect=KeyboardInterrupt), KeyboardInterrupt, ""),
            ("local exit", lambda: backup.export(self.root, self.out), mock.patch.object(backup, "verify", side_effect=SystemExit(3)), SystemExit, ""),
            ("hosted exit", lambda: backup.export_hosted(fake_client(), self.out), mock.patch.object(backup, "verify", side_effect=SystemExit(3)), SystemExit, ""),
        ]
        for label, run, patch, exc, text in cases:
            with self.subTest(case=label):
                with patch, self.assertRaises(exc) as ctx:
                    run()
                self.assertIn(text, str(ctx.exception))
                self.assertFalse(os.path.lexists(self.out))
                self.assertClean()
                self.assertEqual(campaign.read_bytes(), before)

    def test_concurrent_file_or_dangling_symlink_wins(self):
        real_verify = backup.verify
        for name, run in self.producers().items():
            for kind in ("file", "dangling-symlink"):
                with self.subTest(producer=name, winner=kind):
                    out = self.out_dir / f"{name}-{kind}.tar"
                    gone = self.base / "nowhere"

                    def verify_then_win(path, expected_manifest_sha256=None, **kw):
                        report = real_verify(path, expected_manifest_sha256=expected_manifest_sha256, **kw)
                        if kind == "file":
                            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
                            os.write(fd, b"winner bytes")
                            os.close(fd)
                            os.chmod(out, 0o640)
                        else:
                            os.symlink(gone, out)
                        return report

                    with mock.patch.object(backup, "verify", verify_then_win):
                        with self.assertRaises(backup.BackupError) as ctx:
                            run(out)
                    self.assertEqual(str(ctx.exception), "archive appeared before publication; choose a new file name")
                    if kind == "file":
                        self.assertEqual(out.read_bytes(), b"winner bytes")
                        self.assertEqual(mode(out), 0o640)
                    else:
                        self.assertTrue(os.path.islink(out))
                        self.assertEqual(os.readlink(out), str(gone))
                        self.assertFalse(gone.exists())
                    self.assertClean()

    def test_two_coordinated_writers_one_archive_one_refusal(self):
        real_verify = backup.verify
        for name, run in self.producers().items():
            with self.subTest(producer=name):
                out = self.out_dir / f"{name}-race.tar"
                barrier = threading.Barrier(2, timeout=30)
                results = []

                def verify_then_wait(path, expected_manifest_sha256=None, **kw):
                    report = real_verify(path, expected_manifest_sha256=expected_manifest_sha256, **kw)
                    barrier.wait()  # both writers have a fully verified staged archive
                    return report

                def worker():
                    try:
                        results.append(("ok", run(out)))
                    except backup.BackupError as exc:
                        results.append(("refused", str(exc)))

                with mock.patch.object(backup, "verify", verify_then_wait):
                    threads = [threading.Thread(target=worker) for _ in range(2)]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join(60)
                self.assertEqual(sorted(kind for kind, _ in results), ["ok", "refused"])
                refusal = next(value for kind, value in results if kind == "refused")
                winner = next(value for kind, value in results if kind == "ok")
                self.assertEqual(refusal, "archive appeared before publication; choose a new file name")
                self.assertTrue(backup.verify(out, expected_manifest_sha256=winner["manifest_sha256"])["ok"])
                self.assertClean()

    def test_preexisting_output_never_changes(self):
        target = self.base / "elsewhere.bin"
        target.write_bytes(b"keep me")
        for name, run in self.producers().items():
            for kind in ("file", "directory", "symlink", "dangling-symlink"):
                with self.subTest(producer=name, existing=kind):
                    out = self.out_dir / f"{name}-{kind}"
                    if kind == "file":
                        out.write_bytes(b"keep me")
                    elif kind == "directory":
                        out.mkdir()
                    elif kind == "symlink":
                        os.symlink(target, out)
                    else:
                        os.symlink(self.base / "missing", out)
                    before = (os.lstat(out).st_mode, os.lstat(out).st_ino)
                    with mock.patch.object(backup, "_staged_archive", side_effect=AssertionError("staged")):
                        with self.assertRaisesRegex(backup.BackupError, "exists; choose a new file name"):
                            run(out)
                    self.assertEqual((os.lstat(out).st_mode, os.lstat(out).st_ino), before)
                    self.assertEqual(target.read_bytes(), b"keep me")
                    self.assertClean()

    def test_link_error_has_no_copy_fallback(self):
        for name, run in self.producers().items():
            with self.subTest(producer=name):
                out = self.out_dir / f"{name}-nolink.tar"
                with mock.patch.object(backup.os, "link", side_effect=OSError(errno.EXDEV, "cross-device link")), \
                        mock.patch("shutil.copy") as copy, mock.patch("shutil.copyfile") as copyfile, \
                        mock.patch.object(backup.os, "replace") as replace, mock.patch.object(backup.os, "rename") as rename:
                    with self.assertRaises(OSError) as ctx:
                        run(out)
                self.assertEqual(ctx.exception.errno, errno.EXDEV)
                for fake in (copy, copyfile, replace, rename):
                    fake.assert_not_called()
                self.assertFalse(os.path.lexists(out))
                self.assertClean()

    def run_child(self, producer, point):
        work = self.base / f"child-{producer}-{point}"
        work.mkdir()
        os.symlink(self.root, work / "campaign")
        (work / "folder").mkdir(mode=0o700)
        record = self.base / f"digest-{producer}-{point}"
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--child", producer, point, str(work), str(record)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, EXIT, result.stderr)
        final_dir = work / "folder" if producer == "into" else work
        final = final_dir / (FIXED_NAME if producer == "into" else "out.tar")
        return final_dir, final, record

    def test_abrupt_exit_before_commit_leaves_no_final(self):
        for producer in ("local", "hosted", "into"):
            for point in ("manifest", "pre-verify", "pre-link"):
                with self.subTest(producer=producer, point=point):
                    final_dir, final, _ = self.run_child(producer, point)
                    self.assertFalse(os.path.lexists(final))
                    debris = staging(final_dir)
                    self.assertEqual(len(debris), 1)
                    self.assertEqual(mode(debris[0]), 0o700)
                    for item in debris[0].iterdir():
                        self.assertEqual((item.name, mode(item)), ("archive.partial", 0o600))
                    self.assertFalse(any(p.name.endswith((".tar", ".tar.gpg")) for p in final_dir.iterdir()))
                    if producer == "into":
                        listed = backup.list_archives(final_dir)
                        self.assertEqual((listed["archives"], listed["skipped"]), ([], []))
                        self.assertTrue((final_dir / ".deflock-backup.lock").exists())  # documented, never auto-reclaimed
                        with self.assertRaisesRegex(backup.BackupError, "another backup is running"):
                            backup.export_into(self.root, final_dir, now=FIXED_NOW)
                        self.assertTrue((final_dir / ".deflock-backup.lock").exists())

    def test_abrupt_exit_after_commit_leaves_valid_final(self):
        for producer in ("local", "hosted", "into"):
            with self.subTest(producer=producer):
                final_dir, final, record = self.run_child(producer, "post-commit")
                digest = record.read_text()
                self.assertEqual(len(digest), 64)
                self.assertEqual(mode(final), 0o600)
                self.assertTrue(backup.verify(final, expected_manifest_sha256=digest)["ok"])
                for debris in staging(final_dir):  # the parent removes only its own scratch debris
                    for item in debris.iterdir():
                        item.unlink()
                    debris.rmdir()
                self.assertTrue(backup.verify(final, expected_manifest_sha256=digest)["ok"])
                target = self.base / f"restored-{producer}"
                backup.restore(final, target)
                self.assertTrue((target / "campaign.json").is_file())

    def test_scheduled_plain_publication_and_encrypted_regression(self):
        folder = self.out_dir
        real_verify = backup.verify
        seen = []

        def spy(path, expected_manifest_sha256=None, **kw):
            seen.append((os.path.lexists(folder / FIXED_NAME), (folder / ".deflock-backup.lock").exists()))
            return real_verify(path, expected_manifest_sha256=expected_manifest_sha256, **kw)

        with mock.patch.object(backup, "verify", spy):
            name, manifest = backup.export_into(self.root, folder, now=FIXED_NOW)
        self.assertEqual(name, FIXED_NAME)
        self.assertEqual(seen, [(False, True)])
        self.assertTrue(backup.verify(folder / name, expected_manifest_sha256=manifest["manifest_sha256"])["ok"])
        self.assertFalse((folder / ".deflock-backup.lock").exists())
        self.assertClean()
        # an ordinary failure removes the lock and leaves neither archive nor staging
        later = datetime(2026, 1, 3, tzinfo=timezone.utc)
        with mock.patch.object(backup, "verify", side_effect=backup.BackupError("manifest_digest_mismatch")):
            with self.assertRaises(backup.BackupError):
                backup.export_into(self.root, folder, now=later)
        self.assertFalse((folder / ".deflock-backup.lock").exists())
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [FIXED_NAME])


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "--child":
        child_main(*sys.argv[2:])
    unittest.main()
