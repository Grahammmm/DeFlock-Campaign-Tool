"""Encrypted backups (FDF-01): GnuPG keys are generated at run time in a short temp home; no key fixture."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup, backup_crypt  # noqa: E402

GPG = shutil.which("gpg")
TEXT = b"Synthetic policy text for encryption.\n"


def cli(*args, timeout=60):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=timeout)


def make_home(base, name):
    home = Path(tempfile.mkdtemp(prefix="dfg", dir="/tmp" if os.path.isdir("/tmp") else None))
    os.chmod(home, 0o700)
    uid = f"Synthetic {name} <{name}@example.invalid>"
    run = subprocess.run([GPG, "--homedir", str(home), "--batch", "--passphrase", "", "--quick-gen-key", uid,
                          "default", "default", "never"], capture_output=True, timeout=120)
    assert run.returncode == 0, run.stderr
    pub = base / f"{name}.asc"
    out = subprocess.run([GPG, "--homedir", str(home), "--batch", "--armor", "--export", uid], capture_output=True,
                         timeout=60)
    pub.write_bytes(out.stdout)
    listed = subprocess.run([GPG, "--homedir", str(home), "--batch", "--with-colons", "--list-keys", uid],
                            capture_output=True, text=True, timeout=60)
    fpr = next(line.split(":")[9] for line in listed.stdout.splitlines() if line.startswith("fpr:"))
    return home, pub, fpr


def tree(root):
    return {str(p.relative_to(root)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            for p in sorted(root.rglob("*")) if p.is_file()}


@unittest.skipUnless(GPG, "gpg is not installed")
class EncryptedBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys_tmp = tempfile.TemporaryDirectory(prefix="synthetic-keys-")
        base = Path(cls.keys_tmp.name)
        cls.home_a, cls.pub_a, cls.fpr_a = make_home(base, "organizer")
        cls.home_b, cls.pub_b, cls.fpr_b = make_home(base, "stranger")

    @classmethod
    def tearDownClass(cls):
        for home in (cls.home_a, cls.home_b):
            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], capture_output=True, timeout=30)
            shutil.rmtree(home, ignore_errors=True)
        cls.keys_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-encrypt-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "campaign"
        r = cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        self.assertEqual(r.returncode, 0, r.stderr)
        fixture = self.base / "policy.txt"
        fixture.write_bytes(TEXT)
        r = cli("ingest", "--directory", self.root, "--file", fixture, "--source-id", "demo-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.out = self.base / "out"
        self.out.mkdir()

    def encrypted(self, name="enc.tar.gpg"):
        path = self.out / name
        manifest = backup.export(self.root, path, encrypt_to=self.pub_a)
        return path, manifest

    def decrypt_dirs(self):
        return {p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith("deflock-decrypt-")}

    def test_round_trip_matches_plain(self):
        path, manifest = self.encrypted()
        self.assertTrue(manifest["encrypted"])
        self.assertEqual(manifest["recipient"], self.fpr_a)
        self.assertFalse(tarfile.is_tarfile(path))
        self.assertTrue(backup_crypt.is_encrypted(path))
        data = path.read_bytes()
        for clear in (b"manifest.json", b"campaign.json", b"ledger.sqlite", b"Synthetic policy text"):
            self.assertNotIn(clear, data)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["enc.tar.gpg"])
        report = backup.verify(path, gnupg_home=self.home_a)
        self.assertTrue(report["ok"])
        self.assertTrue(report["encrypted"])
        self.assertEqual(report["path"], str(path))
        self.assertEqual(report["manifest_sha256"], manifest["manifest_sha256"])
        plain = self.out / "plain.tar"
        backup.export(self.root, plain)
        backup.restore(plain, self.base / "r-plain")
        restored = backup.restore(path, self.base / "r-enc", gnupg_home=self.home_a)
        self.assertTrue(restored["encrypted"])
        self.assertEqual(tree(self.base / "r-enc"), tree(self.base / "r-plain"))
        self.assertIn(TEXT, [content for content, _ in tree(self.base / "r-enc").values()])

    def test_cli_round_trip_and_warnings(self):
        enc = self.out / "cli.tar.gpg"
        r = cli("backup", "--directory", self.root, "--out", enc, "--encrypt-to", self.pub_a)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("unencrypted", r.stdout)
        self.assertIn(f"Archive is encrypted to {self.fpr_a}; keep the private key", r.stdout)
        digest = json.loads(r.stdout[:r.stdout.rindex("}") + 1])["manifest_sha256"]
        r = cli("verify", "--file", enc, "--gnupg-home", self.home_a, "--expect-manifest-sha256", digest)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["encrypted"])
        r = cli("restore", "--file", enc, "--directory", self.base / "r", "--gnupg-home", self.home_a)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.base / "r" / "campaign.json").is_file())
        r = cli("backup", "--directory", self.root, "--out", self.out / "plain.tar")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Archive is unencrypted; encrypt it before storing it anywhere shared.", r.stdout)
        self.assertNotIn("Archive is encrypted", r.stdout)

    def test_plain_report_unchanged(self):
        plain = self.out / "plain.tar"
        backup.export(self.root, plain)
        report = backup.verify(plain)
        self.assertEqual(set(report), {"path", "ok", "files", "verified", "problems", "manifest_sha256",
                                       "engine_version", "created_at", "counts"})
        self.assertNotIn("encrypted", backup.restore(plain, self.base / "r"))

    def test_expected_digest_detects_rewritten_manifest_inside_encryption(self):
        plain = self.out / "plain.tar"
        original = backup.export(self.root, plain)
        tampered = self.out / "tampered.tar"
        with tarfile.open(plain) as src, tarfile.open(tampered, "w", format=tarfile.PAX_FORMAT) as dst:
            for member in src.getmembers():
                body = src.extractfile(member).read()
                if member.name == "manifest.json":
                    doc = json.loads(body)
                    doc["created_at"] = "2000-01-01T00:00:00Z"
                    body = json.dumps(doc, indent=2, sort_keys=True).encode() + b"\n"
                    member.size = len(body)
                dst.addfile(member, io.BytesIO(body))
        enc = self.out / "tampered.tar.gpg"
        backup_crypt.encrypt(tampered, enc, self.pub_a)
        self.assertTrue(backup.verify(enc, gnupg_home=self.home_a)["ok"])
        with self.assertRaisesRegex(backup.BackupError, "manifest_digest_mismatch"):
            backup.verify(enc, expected_manifest_sha256=original["manifest_sha256"], gnupg_home=self.home_a)

    def test_encrypted_restore_uses_same_target_guard(self):
        path, _ = self.encrypted()
        before = self.decrypt_dirs()
        raced = self.base / "raced"
        real_verify = backup.verify

        def arrive(archive, *args, **kwargs):
            report = real_verify(archive, *args, **kwargs)
            raced.mkdir()
            (raced / "campaign.json").write_bytes(b"another process's campaign")
            return report
        with mock.patch.object(backup, "verify", arrive):
            with self.assertRaisesRegex(backup.BackupError, "restore target changed during verification; nothing restored"):
                backup.restore(path, raced, gnupg_home=self.home_a)
        self.assertEqual([p.name for p in raced.iterdir()], ["campaign.json"])
        self.assertEqual((raced / "campaign.json").read_bytes(), b"another process's campaign")
        self.assertEqual(self.decrypt_dirs(), before)

        late = self.base / "late"
        real_finish = backup._RestoreTarget.finish

        def arrive_late(owner):
            (late / "foreign.txt").write_bytes(b"arrived after extraction")
            return real_finish(owner)
        with mock.patch.object(backup._RestoreTarget, "finish", arrive_late):
            with self.assertRaisesRegex(backup.BackupError, "refusing unowned entries"):
                backup.restore(path, late, gnupg_home=self.home_a)
        self.assertEqual(sorted(p.name for p in late.iterdir()), ["foreign.txt"])
        self.assertEqual((late / "foreign.txt").read_bytes(), b"arrived after extraction")
        self.assertEqual(self.decrypt_dirs(), before)

    def test_wrong_key_cannot_decrypt_and_leaves_no_temp(self):
        path, _ = self.encrypted()
        before = self.decrypt_dirs()
        with self.assertRaisesRegex(backup.BackupError, "cannot decrypt"):
            backup.verify(path, gnupg_home=self.home_b)
        with self.assertRaisesRegex(backup.BackupError, "cannot decrypt"):
            backup.restore(path, self.base / "r", gnupg_home=self.home_b)
        self.assertFalse((self.base / "r").exists())
        self.assertEqual(self.decrypt_dirs(), before)

    def test_bad_recipient_refused_before_any_archive(self):
        (self.base / "link.asc").symlink_to(self.pub_a)
        (self.base / "notkey.asc").write_text("not a key\n")
        (self.base / "folder").mkdir()
        cases = {"missing.asc": "regular public key file", "link.asc": "regular public key file",
                 "folder": "regular public key file", "notkey.asc": "not an OpenPGP public key"}
        for name, message in cases.items():
            with self.subTest(name), self.assertRaisesRegex(backup.BackupError, message):
                backup.export(self.root, self.out / "x.tar.gpg", encrypt_to=self.base / name)
            self.assertEqual(list(self.out.iterdir()), [])
        r = cli("backup", "--directory", self.root, "--out", self.out / "x.tar.gpg", "--encrypt-to", self.base / "notkey.asc")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_gpg_missing(self):
        path, _ = self.encrypted()
        with mock.patch.object(backup_crypt.shutil, "which", return_value=None):
            plain = self.out / "plain.tar"
            backup.export(self.root, plain)
            self.assertTrue(backup.verify(plain)["ok"])
            backup.restore(plain, self.base / "r")
            for call in (lambda: backup.export(self.root, self.out / "new.tar.gpg", encrypt_to=self.pub_a),
                         lambda: backup.verify(path, gnupg_home=self.home_a),
                         lambda: backup.restore(path, self.base / "r2", gnupg_home=self.home_a)):
                with self.assertRaisesRegex(backup.BackupError, "gpg not found; install GnuPG"):
                    call()
        self.assertFalse((self.out / "new.tar.gpg").exists())
        self.assertFalse((self.base / "r2").exists())

    def test_gpg_failing_mid_encrypt_leaves_nothing(self):
        real = backup_crypt._gpg

        def failing(args, timeout=backup_crypt.SHORT_TIMEOUT, stdout=subprocess.PIPE):
            if "--encrypt" in args:
                stdout.write(b"partial plaintext-ish bytes")
                return subprocess.CompletedProcess(args, 2, None, b"boom")
            return real(args, timeout=timeout, stdout=stdout)

        with mock.patch.object(backup_crypt, "_gpg", failing), \
                self.assertRaisesRegex(backup.BackupError, "gpg encryption failed"):
            backup.export(self.root, self.out / "x.tar.gpg", encrypt_to=self.pub_a)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_wrong_recipient_packet_refused(self):
        with mock.patch.object(backup_crypt, "recipient_ids", return_value=(self.fpr_b, {"0123456789ABCDEF"})), \
                self.assertRaisesRegex(backup.BackupError, "not addressed to the --encrypt-to key"):
            backup.export(self.root, self.out / "x.tar.gpg", encrypt_to=self.pub_a)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_existing_out_never_replaced(self):
        target = self.out / "x.tar.gpg"
        target.write_bytes(b"keep")
        with self.assertRaisesRegex(backup.BackupError, "exists"):
            backup.export(self.root, target, encrypt_to=self.pub_a)
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["x.tar.gpg"])

    def test_truncated_ciphertext(self):
        path, _ = self.encrypted()
        data = path.read_bytes()
        path.write_bytes(data[:len(data) // 2])
        before = self.decrypt_dirs()
        with self.assertRaisesRegex(backup.BackupError, "cannot decrypt"):
            backup.verify(path, gnupg_home=self.home_a)
        with self.assertRaisesRegex(backup.BackupError, "cannot decrypt"):
            backup.restore(path, self.base / "r", gnupg_home=self.home_a)
        self.assertFalse((self.base / "r").exists())
        self.assertEqual(self.decrypt_dirs(), before)


if __name__ == "__main__":
    unittest.main()
