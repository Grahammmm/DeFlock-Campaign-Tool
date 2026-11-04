"""DF2207-01: backup-diff reads encrypted archives (synthetic data; GnuPG keys made at run time in throwaway homes)."""
import contextlib
import hashlib
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
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup, backup_crypt  # noqa: E402
from campaign_tool import cli as campaign_cli  # noqa: E402
from tests.test_backup_diff import cli as run_cli, tree_state  # noqa: E402
from tests.test_backup_encrypt import GPG, make_home  # noqa: E402

NO_PATH_OR_PLAINTEXT = ("deflock-decrypt-", "archive.tar", "Traceback")


@unittest.skipUnless(GPG, "gpg is not installed")
class EncryptedDiffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys_tmp = tempfile.TemporaryDirectory(prefix="synthetic-df2207-keys-")
        base = Path(cls.keys_tmp.name)
        cls.home_a, cls.pub_a, _ = make_home(base, "organizer")
        cls.home_b, cls.pub_b, _ = make_home(base, "stranger")

    @classmethod
    def tearDownClass(cls):
        for home in (cls.home_a, cls.home_b):
            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], capture_output=True, timeout=30)
            shutil.rmtree(home, ignore_errors=True)
        cls.keys_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-df2207-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.parent = self.base / "tmp-parent"  # every temp dir made by the code under test lands here
        self.parent.mkdir()
        self.root = self.base / "campaign"
        r = run_cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.ingest(b"Synthetic policy text.\n", "demo-1")
        (self.root / "content").mkdir()
        (self.root / "content" / "site.json").write_text('{"schema_version":1,"tagline":"synthetic-tagline"}')
        (self.root / "content" / "gone.md").write_text("to be deleted\n")
        (self.root / "kit").mkdir()
        (self.root / "kit" / "summary.json").write_text("{}")
        self.plain = self.base / "plain.tar"
        self.enc = self.base / "enc.tar.gpg"
        # One created_at for both exports, so the two reports can be compared whole.
        with patch.object(backup, "_now", return_value=backup._now()):
            self.manifest = backup.export(self.root, self.plain)
            backup.export(self.root, self.enc, encrypt_to=self.pub_a)

    def ingest(self, data, source_id):
        fixture = self.base / (source_id + ".txt")
        fixture.write_bytes(data)
        r = run_cli("ingest", "--directory", self.root, "--file", fixture, "--source-id", source_id)
        self.assertEqual(r.returncode, 0, r.stderr)
        return hashlib.sha256(data).hexdigest()

    def main(self, *argv):
        """In-process CLI run with every temp dir under self.parent; returns (exit code, stdout, stderr)."""
        out, err, code = io.StringIO(), io.StringIO(), 0
        with patch.object(sys, "argv", ["campaign_tool", *map(str, argv)]), \
                patch.object(tempfile, "tempdir", str(self.parent)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                campaign_cli.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def diff(self, archive, *extra, directory=None, home="default"):
        home = self.home_a if isinstance(home, str) and home == "default" else home
        args = ["backup-diff", "--file", archive, "--directory", directory or self.root, *extra]
        if home is not None:
            args += ["--gnupg-home", home]
        return self.main(*args)

    def gpg_calls(self):
        calls, real = [], backup_crypt._gpg

        def counting(args, *a, **kw):
            calls.append(list(args))
            return real(args, *a, **kw)
        return calls, patch.object(backup_crypt, "_gpg", counting)

    def decrypts(self, calls):
        return [call for call in calls if "--decrypt" in call]

    def assert_failed_clean(self, result, text):
        code, out, err = result
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(text, err)
        for leak in NO_PATH_OR_PLAINTEXT:
            self.assertNotIn(leak, err)
        self.assertEqual(list(self.parent.iterdir()), [], "private temp copy left behind")

    def encrypt_bytes(self, data, name):
        raw = self.base / (name + ".raw")
        raw.write_bytes(data)
        out = self.base / (name + ".tar.gpg")
        backup_crypt.encrypt(raw, out, self.pub_a)
        return out

    def make_tar(self, name, members):
        path = self.base / name
        with tarfile.open(path, "w") as tar:
            for member, data in members:
                info = tarfile.TarInfo(member)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def make_drift(self):
        site = self.root / "content" / "site.json"
        old = site.read_bytes()
        site.write_bytes(old.replace(b"synthetic-tagline", b"synthetic-TAGLINE"))  # same size, new bytes
        (self.root / "kit" / "new.json").write_text("{}")
        (self.root / "content" / "gone.md").unlink()
        return self.ingest(b"Second synthetic record.\n", "demo-2")

    # --- acceptance behaviours -------------------------------------------------------------------

    def test_encrypted_current_and_drift_match_plain(self):
        code, out, err = self.diff(self.enc)
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertIs(report.pop("encrypted"), True)
        self.assertEqual(report, backup.diff(self.plain, self.root))
        self.assertTrue(report["current"])
        sha = self.make_drift()
        root_before, enc_before, mtime = tree_state(self.root), self.enc.read_bytes(), self.enc.stat().st_mtime_ns
        code, out, err = self.diff(self.enc)
        self.assertEqual(code, 3, err)
        report = json.loads(out)
        self.assertIs(report.pop("encrypted"), True)
        self.assertEqual(report, backup.diff(self.plain, self.root))
        self.assertEqual(report["changed"], ["content/site.json", "private/ledger.sqlite"])
        self.assertEqual(report["added"], ["kit/new.json", "private/objects/" + sha])
        self.assertEqual(report["removed"], ["content/gone.md"])
        self.assertEqual(tree_state(self.root), root_before)
        self.assertEqual((self.enc.read_bytes(), self.enc.stat().st_mtime_ns), (enc_before, mtime))
        api = backup.diff(self.enc, self.root, gnupg_home=self.home_a)
        self.assertEqual(api, dict(backup.diff(self.plain, self.root), encrypted=True))

    def test_content_detection_not_suffix(self):
        renamed = self.base / "renamed.tar"
        shutil.copy2(self.enc, renamed)
        self.assertEqual(json.loads(self.diff(renamed)[1])["encrypted"], True)
        plain_gpg = self.base / "plain.tar.gpg"
        shutil.copy2(self.plain, plain_gpg)
        calls, spy = self.gpg_calls()
        with spy:
            code, out, err = self.diff(plain_gpg)
        self.assertEqual(code, 0, err)
        self.assertNotIn("encrypted", json.loads(out))
        self.assertEqual(calls, [])

    def test_no_option_uses_normal_gnupg_selection_and_explicit_home_is_not_retried(self):
        with patch.dict(os.environ, {"GNUPGHOME": str(self.home_a)}):
            code, out, err = self.diff(self.enc, home=None)
            self.assertEqual(code, 0, err)
            self.assertIs(json.loads(out)["encrypted"], True)
            calls, spy = self.gpg_calls()
            with spy:  # the stranger's home was named, so the default key is never tried instead
                self.assert_failed_clean(self.diff(self.enc, home=self.home_b),
                                         "cannot decrypt: no matching private key or wrong passphrase")
            self.assertEqual(len(self.decrypts(calls)), 1)

    def test_expected_manifest_binding(self):
        digest = self.manifest["manifest_sha256"]
        code, out, err = self.diff(self.enc, "--expect-manifest-sha256", f"  {digest.upper()} ")
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out)["encrypted"], True)
        self.make_drift()
        self.assert_failed_clean(self.diff(self.enc, "--expect-manifest-sha256", "0" * 64), "manifest_digest_mismatch")
        with self.assertRaisesRegex(backup.BackupError, "manifest_digest_mismatch"):
            backup.diff(self.enc, self.root, expected_manifest_sha256="0" * 64, gnupg_home=self.home_a)
        # a member flipped inside the encrypted tar stops before any live drift is reported
        with tarfile.open(self.plain) as tar:
            offset = tar.getmember("content/site.json").offset_data
        data = bytearray(self.plain.read_bytes())
        data[offset] ^= 0x01
        tampered = self.encrypt_bytes(bytes(data), "tampered")
        for extra in ((), ("--expect-manifest-sha256", digest)):
            result = self.diff(tampered, *extra)
            self.assert_failed_clean(result, "verification failed")
            self.assertIn("hash_mismatch", result[2])

    def test_wrong_key_missing_gpg_and_bad_ciphertext(self):
        before = tree_state(self.root)
        wrong = "cannot decrypt: no matching private key or wrong passphrase"
        self.assert_failed_clean(self.diff(self.enc, home=self.home_b), wrong)
        data = self.enc.read_bytes()
        truncated = self.base / "truncated.tar.gpg"
        truncated.write_bytes(data[:len(data) // 2])
        self.assert_failed_clean(self.diff(truncated), wrong)
        malformed = self.base / "malformed.tar.gpg"
        malformed.write_bytes(b"\x85\xff\xfe synthetic malformed packet bytes")
        self.assert_failed_clean(self.diff(malformed), wrong)
        with patch.object(backup_crypt.shutil, "which", return_value=None):
            self.assert_failed_clean(self.diff(self.enc), "gpg not found")
        self.assertEqual(tree_state(self.root), before)

    def test_empty_or_malformed_decrypted_tar(self):
        bad_manifest = self.make_tar("badmanifest.tar", [("manifest.json", b"not json")])
        duplicate = self.make_tar("dup.tar", [("campaign.json", b"{}"), ("campaign.json", b"{}"),
                                              ("manifest.json", b"{}")])
        cases = [("empty", b"", "invalid encrypted archive"),
                 ("nontar", b"synthetic bytes that are not a tar archive\n" * 40, "invalid encrypted archive"),
                 ("truncated-tar", self.plain.read_bytes()[:700], "invalid encrypted archive"),
                 ("badmanifest", bad_manifest.read_bytes(), "invalid_manifest"),
                 ("dup", duplicate.read_bytes(), "duplicate_member")]
        for name, data, text in cases:
            with self.subTest(name):
                result = self.diff(self.encrypt_bytes(data, name))
                self.assert_failed_clean(result, text)
                if text == "invalid encrypted archive":
                    self.assertNotIn("ReadError", result[2])
                    self.assertNotIn("tar", result[2].split(text, 1)[1])
        # a malformed plain tar keeps its existing, untranslated behaviour
        plain_bad = self.base / "bad.tar"
        plain_bad.write_bytes(b"not a tar at all\n" * 100)
        with self.assertRaises(tarfile.TarError):
            backup.diff(plain_bad, self.root)

    def test_single_decrypt_private_modes_and_cleanup(self):
        seen = []
        real = backup._diff_tar

        def spy(archive, root, expected=None):
            archive = Path(archive)
            seen.append((stat.S_IMODE(archive.stat().st_mode), stat.S_IMODE(archive.parent.stat().st_mode),
                         archive.parent.name.startswith("deflock-decrypt-"), archive.parent.parent == self.parent))
            return real(archive, root, expected)
        calls, counting = self.gpg_calls()
        with counting, patch.object(backup, "_diff_tar", spy):
            code, out, err = self.diff(self.enc)
            self.assertEqual(code, 0, err)
        self.assertEqual(seen, [(0o600, 0o700, True, True)])
        self.assertEqual(len(self.decrypts(calls)), 1)
        self.assertEqual(calls[0][:2], ["--homedir", str(self.home_a)])
        self.assertEqual(list(self.parent.iterdir()), [])

        outside = self.base / "outside"
        outside.mkdir()
        (outside / "marker.txt").write_text("outside\n")
        outside_before = tree_state(outside)
        enc_before = self.enc.read_bytes()
        root_before = tree_state(self.root)

        def copy_root(name):
            copy = self.base / name
            shutil.copytree(self.root, copy, symlinks=True)
            return copy

        no_campaign = self.base / "not-a-campaign"
        no_campaign.mkdir()
        linked_private = copy_root("linked-private")
        (linked_private / "private").rename(outside / "private")
        os.symlink(outside / "private", linked_private / "private")
        linked_ledger = copy_root("linked-ledger")
        (linked_ledger / "private" / "ledger.sqlite").rename(outside / "ledger.sqlite")
        os.symlink(outside / "ledger.sqlite", linked_ledger / "private" / "ledger.sqlite")
        outside_before = tree_state(outside)
        cases = [("digest mismatch", ("--expect-manifest-sha256", "0" * 64), self.root, None, "manifest_digest_mismatch", 1),
                 ("no campaign.json", (), no_campaign, None, "campaign.json not found", 1),
                 ("symlinked private", (), linked_private, None, "unsafe_private_directory", 1),
                 ("symlinked ledger", (), linked_ledger, None, "invalid_ledger", 1),
                 ("live hash OSError", (), self.root, patch.object(backup, "_sha256", side_effect=OSError("synthetic read failure")),
                  "synthetic read failure", 1)]
        for name, extra, directory, injected, text, decrypt_count in cases:
            with self.subTest(name):
                calls, counting = self.gpg_calls()
                with counting, (injected or contextlib.nullcontext()):
                    result = self.diff(self.enc, *extra, directory=directory)
                self.assert_failed_clean(result, text)
                self.assertEqual(len(self.decrypts(calls)), decrypt_count)
        with self.subTest("plaintext creation PermissionError"):
            real_open = os.open

            def deny(path, *args, **kwargs):
                if str(path).endswith("archive.tar"):
                    raise PermissionError("synthetic permission denied")
                return real_open(path, *args, **kwargs)
            calls, counting = self.gpg_calls()
            with counting, patch.object(os, "open", deny):
                result = self.diff(self.enc)
            self.assert_failed_clean(result, "synthetic permission denied")
            self.assertEqual(self.decrypts(calls), [])
        self.assertEqual(tree_state(outside), outside_before)
        self.assertEqual(self.enc.read_bytes(), enc_before)
        self.assertEqual(tree_state(self.root), root_before)

    def test_encrypted_cap_boundary_and_empty_lists(self):
        code, out, err = self.diff(self.enc)
        report = json.loads(out)
        self.assertEqual((code, report["added"], report["removed"], report["changed"]), (0, [], [], []))
        self.assertEqual(report["unchanged"], len(self.manifest["files"]))
        self.assertNotIn("more", report)
        bulk = self.root / "content" / "bulk"
        bulk.mkdir()
        for i in range(200):
            (bulk / f"f{i:03d}.md").write_text(f"{i}\n")
        code, out, err = self.diff(self.enc)
        report = json.loads(out)
        self.assertEqual(code, 3, err)
        self.assertEqual(report["added"], [f"content/bulk/f{i:03d}.md" for i in range(200)])
        self.assertNotIn("more", report)
        (bulk / "f200.md").write_text("200\n")
        code, out, err = self.diff(self.enc)
        report = json.loads(out)
        self.assertEqual(code, 3, err)
        self.assertEqual(len(report["added"]), 200)
        self.assertEqual(report["more"], {"added": 1})
        for text in ("deflock-decrypt-", str(self.parent), self.manifest["manifest_sha256"], "synthetic-tagline", "sha256"):
            self.assertNotIn(text, out + err)


if __name__ == "__main__":
    unittest.main()
