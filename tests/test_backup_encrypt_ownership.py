"""RF0520-3: encrypted-backup staging ownership. Synthetic GnuPG keys are generated at run time; no key fixture."""
import os
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
from campaign_tool import backup, backup_crypt  # noqa: E402

GPG = shutil.which("gpg")
STAGING_CHANGED = "encrypted archive staging changed; nothing published"
REAL_GPG = backup_crypt._gpg
FIXED = "aaaaaaaaaaaa"


def open_fds():
    return len(os.listdir("/proc/self/fd"))


def fd_inodes():
    found = set()
    for name in os.listdir("/proc/self/fd"):
        try:
            info = os.fstat(int(name))
        except OSError:
            continue
        found.add((info.st_dev, info.st_ino))
    return found


@unittest.skipUnless(GPG and os.path.isdir("/proc/self/fd"), "gpg or /proc/self/fd is not available")
class EncryptOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix="synthetic-own-keys-")
        cls.home = Path(cls.keys.name) / "home"
        cls.home.mkdir(mode=0o700)
        uid = "Synthetic Owner <owner@example.invalid>"
        run = subprocess.run([GPG, "--homedir", str(cls.home), "--batch", "--passphrase", "", "--quick-gen-key", uid,
                              "default", "default", "never"], capture_output=True, timeout=120)
        assert run.returncode == 0, run.stderr
        exported = subprocess.run([GPG, "--homedir", str(cls.home), "--batch", "--armor", "--export", uid],
                                  capture_output=True, timeout=60)
        cls.pub = Path(cls.keys.name) / "owner.asc"
        cls.pub.write_bytes(exported.stdout)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--homedir", str(cls.home), "--kill", "all"], capture_output=True, timeout=30)
        cls.keys.cleanup()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-own-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.out_dir = self.base / "out"
        self.out_dir.mkdir()
        self.out = self.out_dir / "enc.tar.gpg"
        self.plain = self.base / "plain.tar"
        self.plain.write_bytes(b"Synthetic archive bytes.\n" * 50)
        self.outside = self.base / "outside.txt"
        self.outside.write_bytes(b"outside sentinel")
        self.calls = []

    def partials(self):
        return sorted(self.out_dir.glob(".*.partial"))

    def encrypt(self):
        return backup_crypt.encrypt(self.plain, self.out, self.pub)

    def swap(self, kind, partial):
        """Replace ``partial`` by synthetic foreign content of the given kind; return a description to compare."""
        if partial.is_symlink() or partial.is_file():
            partial.unlink()
        elif partial.is_dir():
            partial.rmdir()
        if kind == "missing":
            return None
        if kind == "file":
            partial.write_bytes(b"foreign bytes")
        elif kind == "symlink":
            partial.symlink_to(self.outside)
        elif kind == "dir":
            partial.mkdir()
        return kind

    def forwarding(self, after_encrypt=None, after_list=None, encrypt_rc=None):
        def fake(args, timeout=backup_crypt.SHORT_TIMEOUT, stdout=subprocess.PIPE):
            if "--encrypt" in args:
                self.calls.append("encrypt")
                result = REAL_GPG(args, timeout=timeout, stdout=stdout)
                if after_encrypt:
                    after_encrypt(self.partials()[0] if self.partials() else None)
                if encrypt_rc is not None:
                    return subprocess.CompletedProcess(args, encrypt_rc, b"", b"")
                return result
            if "--list-packets" in args:
                self.calls.append("list")
                result = REAL_GPG(args, timeout=timeout, stdout=stdout)
                if after_list:
                    after_list(Path(args[-1]))
                return result
            return REAL_GPG(args, timeout=timeout, stdout=stdout)
        return fake

    def assert_foreign_kept(self, kind, partial):
        if kind == "file":
            self.assertEqual(partial.read_bytes(), b"foreign bytes")
        elif kind == "symlink":
            self.assertTrue(partial.is_symlink())
            self.assertEqual(os.readlink(partial), str(self.outside))
        elif kind == "dir":
            self.assertTrue(partial.is_dir() and not partial.is_symlink())
        self.assertEqual(self.outside.read_bytes(), b"outside sentinel")
        self.assertFalse(self.out.exists() or self.out.is_symlink())

    def test_healthy_encrypt_publishes_and_cleans_partial(self):
        before = open_fds()
        fingerprint = self.encrypt()
        self.assertTrue(fingerprint)
        self.assertTrue(backup_crypt.is_encrypted(self.out))
        self.assertEqual(self.partials(), [])
        self.assertEqual(open_fds(), before)

    def test_partial_collision_preserves_unowned_entries(self):
        for kind in ("file", "dir", "symlink", "dangling"):
            with self.subTest(kind=kind):
                partial = self.out_dir / f".{self.out.name}.{FIXED}.partial"
                if kind == "file":
                    partial.write_bytes(b"existing")
                elif kind == "dir":
                    partial.mkdir()
                elif kind == "symlink":
                    partial.symlink_to(self.outside)
                else:
                    partial.symlink_to(self.base / "nowhere")
                before = open_fds()
                with mock.patch.object(backup_crypt.secrets, "token_hex", return_value=FIXED), \
                        mock.patch.object(backup_crypt, "_gpg", side_effect=self.forwarding()):
                    with self.assertRaises(FileExistsError):
                        self.encrypt()
                self.assertNotIn("encrypt", self.calls)
                if kind == "file":
                    self.assertEqual(partial.read_bytes(), b"existing")
                elif kind == "dir":
                    self.assertTrue(partial.is_dir() and not partial.is_symlink())
                elif kind == "symlink":
                    self.assertEqual(os.readlink(partial), str(self.outside))
                else:
                    self.assertEqual(os.readlink(partial), str(self.base / "nowhere"))
                self.assertEqual(self.outside.read_bytes(), b"outside sentinel")
                self.assertFalse(self.out.exists())
                self.assertEqual(open_fds(), before)
                if partial.is_dir() and not partial.is_symlink():
                    partial.rmdir()
                else:
                    partial.unlink()

    def test_failed_encrypt_preserves_replacement_partial(self):
        for kind in ("file", "symlink", "dir"):
            with self.subTest(kind=kind):
                holder = {}

                def replace(partial, kind=kind):
                    holder["p"] = partial
                    self.swap(kind, partial)
                with mock.patch.object(backup_crypt, "_gpg", side_effect=self.forwarding(replace, encrypt_rc=2)):
                    with self.assertRaises(backup.BackupError) as ctx:
                        self.encrypt()
                self.assertEqual(str(ctx.exception), "gpg encryption failed")
                self.assert_foreign_kept(kind, holder["p"])
                if kind == "dir":
                    holder["p"].rmdir()
                else:
                    holder["p"].unlink()

    def test_packet_rejection_cleans_owned_partial(self):
        with mock.patch.object(backup_crypt, "recipient_ids", return_value=("F", {"DEADBEEF"})), \
                mock.patch.object(backup_crypt, "_gpg", side_effect=self.forwarding()):
            with self.assertRaises(backup.BackupError) as ctx:
                self.encrypt()
        self.assertIn("not addressed", str(ctx.exception))
        self.assertEqual(self.partials(), [])
        self.assertFalse(self.out.exists())

    def test_packet_failure_preserves_classification_when_partial_is_replaced(self):
        for failure in ("inspection", "recipient"):
            for kind in ("file", "symlink", "dir"):
                with self.subTest(failure=failure, kind=kind):
                    self.calls.clear()
                    holder = {}
                    before = open_fds()

                    def replace(partial):
                        holder["p"] = partial
                        self.swap(kind, partial)

                    forward = self.forwarding(after_list=replace)

                    def failed_packets(args, **kwargs):
                        result = forward(args, **kwargs)
                        if "--list-packets" in args:
                            if failure == "inspection":
                                result.returncode = 2
                            else:
                                result.stdout = b""
                        return result

                    with mock.patch.object(backup_crypt, "_gpg", side_effect=failed_packets):
                        with self.assertRaises(backup.BackupError) as ctx:
                            self.encrypt()
                    self.assertEqual(str(ctx.exception),
                                     "encrypted archive is not addressed to the --encrypt-to key")
                    self.assertEqual(self.calls, ["encrypt", "list"])
                    self.assert_foreign_kept(kind, holder["p"])
                    self.assertEqual(open_fds(), before)
                    if kind == "dir":
                        holder["p"].rmdir()
                    else:
                        holder["p"].unlink()

    def test_replaced_partial_refuses_before_packet_read_or_publication(self):
        for kind in ("missing", "file", "symlink", "dir"):
            with self.subTest(phase="before-packet", kind=kind):
                self.calls.clear()
                holder = {}

                def replace(partial, kind=kind):
                    holder["p"] = partial
                    self.swap(kind, partial)
                with mock.patch.object(backup_crypt, "_gpg", side_effect=self.forwarding(replace)):
                    with self.assertRaises(backup.BackupError) as ctx:
                        self.encrypt()
                self.assertEqual(str(ctx.exception), STAGING_CHANGED)
                self.assertEqual(self.calls, ["encrypt"])
                if kind != "missing":
                    self.assert_foreign_kept(kind, holder["p"])
                    if kind == "dir":
                        holder["p"].rmdir()
                    else:
                        holder["p"].unlink()
                self.assertFalse(self.out.exists())
            with self.subTest(phase="after-packet", kind=kind):
                self.calls.clear()
                holder = {}

                def replace_after(partial, kind=kind):
                    holder["p"] = partial
                    self.swap(kind, partial)
                with mock.patch.object(backup_crypt, "_gpg", side_effect=self.forwarding(after_list=replace_after)):
                    with self.assertRaises(backup.BackupError) as ctx:
                        self.encrypt()
                self.assertEqual(str(ctx.exception), STAGING_CHANGED)
                self.assertEqual(self.calls, ["encrypt", "list"])
                if kind != "missing":
                    self.assert_foreign_kept(kind, holder["p"])
                    if kind == "dir":
                        holder["p"].rmdir()
                    else:
                        holder["p"].unlink()
                self.assertFalse(self.out.exists())

    def test_retained_descriptor_pins_stage_identity_until_cleanup(self):
        seen = {}

        def replace(partial):
            seen["ident"] = None
            info = os.stat(partial)
            seen["ident"] = (info.st_dev, info.st_ino)
            self.swap("file", partial)
            seen["pinned_after_unlink"] = seen["ident"] in fd_inodes()
            seen["path"] = partial
        with mock.patch.object(backup_crypt, "_gpg", side_effect=self.forwarding(replace)):
            with self.assertRaises(backup.BackupError):
                self.encrypt()
        self.assertTrue(seen["pinned_after_unlink"])
        self.assertNotIn(seen["ident"], fd_inodes())  # released exactly once, nothing leaked
        self.assertEqual(seen["path"].read_bytes(), b"foreign bytes")

    def test_acquisition_and_handoff_failures_do_not_gain_cleanup_authority(self):
        before = open_fds()
        # identity capture fails: refuse, never delete the uncertain entry (a private partial may remain)
        real_fstat = os.fstat

        def failing_fstat(fd):
            if os.readlink(f"/proc/self/fd/{fd}").endswith(".partial"):
                raise OSError("boom")
            return real_fstat(fd)
        with mock.patch.object(backup_crypt.os, "fstat", failing_fstat):
            with self.assertRaises(backup.BackupError) as ctx:
                self.encrypt()
        self.assertIn("could not be verified", str(ctx.exception))
        self.assertEqual(len(self.partials()), 1)
        self.assertEqual(stat.S_IMODE(self.partials()[0].stat().st_mode), 0o600)
        self.assertEqual(open_fds(), before)
        self.partials()[0].unlink()
        # duplicate and fdopen failures happen after identity: owned partial removed, no descriptor leak
        with mock.patch.object(backup_crypt.os, "dup", side_effect=OSError("dup")):
            with self.assertRaises(OSError):
                self.encrypt()
        self.assertEqual(self.partials(), [])
        self.assertEqual(open_fds(), before)
        with mock.patch.object(backup_crypt.os, "fdopen", side_effect=OSError("fdopen")):
            with self.assertRaises(OSError):
                self.encrypt()
        self.assertEqual(self.partials(), [])
        self.assertEqual(open_fds(), before)
        self.assertFalse(self.out.exists())

    def test_owned_stage_cleanup_and_interruptions(self):
        for exc in (KeyboardInterrupt, SystemExit, RuntimeError):
            with self.subTest(exc=exc.__name__):
                before = open_fds()
                with mock.patch.object(backup_crypt, "_gpg", side_effect=exc()):
                    with self.assertRaises(exc):
                        self.encrypt()
                self.assertEqual(self.partials(), [])
                self.assertFalse(self.out.exists())
                self.assertEqual(open_fds(), before)
        real_unlink = Path.unlink

        def failing_unlink(path, missing_ok=False):
            if path.name.endswith(".partial"):
                raise PermissionError("cleanup refused")
            return real_unlink(path, missing_ok=missing_ok)
        # primary error survives a cleanup failure
        with mock.patch.object(backup_crypt, "_gpg", side_effect=self.forwarding(encrypt_rc=2)), \
                mock.patch.object(Path, "unlink", failing_unlink):
            with self.assertRaises(backup.BackupError) as ctx:
                self.encrypt()
        self.assertEqual(str(ctx.exception), "gpg encryption failed")
        for leftover in self.partials():
            leftover.unlink()
        # cleanup-only failure never reports success; the already-linked final stays complete
        with mock.patch.object(Path, "unlink", failing_unlink):
            with self.assertRaises(PermissionError):
                self.encrypt()
        self.assertTrue(backup_crypt.is_encrypted(self.out))
        self.assertEqual(len(self.partials()), 1)

    def test_final_winner_and_committed_archive_are_never_removed(self):
        for kind in ("file", "dangling"):
            with self.subTest(kind=kind):
                def win(partial, kind=kind):
                    if kind == "file":
                        self.out.write_bytes(b"winner")
                    else:
                        self.out.symlink_to(self.base / "nowhere")
                with mock.patch.object(backup_crypt, "_gpg", side_effect=self.forwarding(after_list=win)):
                    with self.assertRaises(backup.BackupError) as ctx:
                        self.encrypt()
                self.assertEqual(str(ctx.exception), "archive appeared during encryption; choose a new file name")
                self.assertEqual(self.partials(), [])
                if kind == "file":
                    self.assertEqual(self.out.read_bytes(), b"winner")
                else:
                    self.assertEqual(os.readlink(self.out), str(self.base / "nowhere"))
                self.out.unlink()

    def test_export_inherits_ownership_refusal(self):
        root = self.base / "campaign"
        run = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root), "--name",
                              "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        target = self.out_dir / "export.tar.gpg"
        holder = {}

        def replace(partial):
            holder["p"] = partial
            self.swap("file", partial)
        with mock.patch.object(backup_crypt, "_gpg", side_effect=self.forwarding(after_list=replace)):
            with self.assertRaises(backup.BackupError) as ctx:
                backup.export(root, target, encrypt_to=self.pub)
        self.assertEqual(str(ctx.exception), STAGING_CHANGED)
        self.assertFalse(target.exists())
        self.assertEqual(holder["p"].read_bytes(), b"foreign bytes")
        holder["p"].unlink()
        manifest = backup.export(root, target, encrypt_to=self.pub)
        self.assertTrue(manifest["encrypted"])
        self.assertTrue(backup.verify(target, gnupg_home=self.home)["ok"])
        self.assertEqual(self.partials(), [])


if __name__ == "__main__":
    unittest.main()
