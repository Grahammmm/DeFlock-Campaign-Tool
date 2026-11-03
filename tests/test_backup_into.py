"""Scheduled backups (FDF-03): `backup --into DIR` writes one new timestamped archive and one status line."""
import contextlib
import io
import json
import os
import re
import shutil
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
from campaign_tool import backup, backup_crypt, cli  # noqa: E402

GPG = shutil.which("gpg")
FIXED = "deflock-backup-20261102T180000Z.tar"
LINE = re.compile(r"backup ok: (deflock-backup-\d{8}T\d{6}Z\.tar(?:\.gpg)?) (encrypted|UNENCRYPTED) files=\d+ bytes=\d+ manifest_sha256=[0-9a-f]{64}\n")


def run(*args):
    """Run the CLI in-process; returns (exit code, stdout, stderr)."""
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, args)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def make_key(base, name):
    home = Path(tempfile.mkdtemp(prefix="dfg", dir="/tmp" if os.path.isdir("/tmp") else None))
    os.chmod(home, 0o700)
    uid = f"Synthetic {name} <{name}@example.invalid>"
    done = subprocess.run([GPG, "--homedir", str(home), "--batch", "--passphrase", "", "--quick-gen-key", uid,
                           "default", "default", "never"], capture_output=True, timeout=120)
    assert done.returncode == 0, done.stderr
    pub = base / f"{name}.asc"
    pub.write_bytes(subprocess.run([GPG, "--homedir", str(home), "--batch", "--armor", "--export", uid],
                                   capture_output=True, timeout=60).stdout)
    return home, pub


class IntoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-into-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        self.assertEqual(code, 0, err)
        self.dest = self.base / "dest"
        self.dest.mkdir(mode=0o700)
        os.chmod(self.dest, 0o700)

    def go(self, *extra, dest=None):
        return run("backup", "--directory", self.root, "--into", dest or self.dest, *extra)

    def names(self, folder=None):
        return sorted(p.name for p in (folder or self.dest).iterdir())

    def set_backup_config(self, value):
        path = self.root / "campaign.json"
        doc = json.loads(path.read_text())
        doc["backup"] = value
        path.write_text(json.dumps(doc))


class PlainIntoTests(IntoBase):
    def test_plain_archive_one_line_mode_and_verify(self):
        with mock.patch.object(backup, "scheduled_name", return_value=FIXED):
            code, out, err = self.go()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.names(), [FIXED])
        self.assertEqual(len(out.splitlines()), 1)
        match = LINE.fullmatch(out)
        self.assertTrue(match, out)
        self.assertEqual(match.group(1, 2), (FIXED, "UNENCRYPTED"))
        self.assertEqual(stat.S_IMODE((self.dest / FIXED).stat().st_mode), 0o600)
        report = backup.verify(self.dest / FIXED)
        self.assertTrue(report["ok"])
        self.assertIn(f"manifest_sha256={report['manifest_sha256']}", out)

    def test_scheduled_name_is_utc_stamp(self):
        self.assertEqual(backup.scheduled_name(datetime(2026, 11, 2, 18, 0, 0, tzinfo=timezone.utc)), FIXED)
        self.assertRegex(backup.scheduled_name(), r"deflock-backup-\d{8}T\d{6}Z\.tar")

    def test_same_second_twice_never_overwrites(self):
        with mock.patch.object(backup, "scheduled_name", return_value=FIXED):
            self.assertEqual(self.go()[0], 0)
            first = (self.dest / FIXED).read_bytes()
            code, out, err = self.go()
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, f"backup: {FIXED} exists; not overwritten\n")
        self.assertEqual(self.names(), [FIXED])
        self.assertEqual((self.dest / FIXED).read_bytes(), first)

    def test_bad_destinations_create_nothing(self):
        (self.base / "afile").write_text("x")
        (self.base / "link").symlink_to(self.dest, target_is_directory=True)
        loose = self.base / "loose"
        loose.mkdir()
        os.chmod(loose, 0o770)
        for name in ("missing", "afile", "link", "loose"):
            with self.subTest(name):
                code, out, err = self.go(dest=self.base / name)
                self.assertEqual((code, out), (1, ""))
                self.assertRegex(err, r"\Abackup: --into: [^\n]+\n\Z")
                self.assertNotIn(str(self.base), err)
        self.assertEqual(self.names(), [])
        self.assertEqual(self.names(loose), [])
        self.assertFalse((self.base / "missing").exists())
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["afile", "campaign", "dest", "link", "loose"])

    def test_out_and_into_are_exclusive_and_one_is_required(self):
        both = run("backup", "--directory", self.root, "--out", self.base / "a.tar", "--into", self.dest)
        neither = run("backup", "--directory", self.root)
        self.assertEqual((both[0], neither[0]), (2, 2))
        self.assertEqual((self.names(), (self.base / "a.tar").exists()), ([], False))

    def test_failure_mid_export_leaves_nothing(self):
        with mock.patch.object(backup, "verify", side_effect=backup.BackupError("synthetic verify failure")):
            code, out, err = self.go()
        self.assertEqual((code, out, err), (1, "", "backup failed: synthetic verify failure\n"))
        self.assertEqual(self.names(), [])

    def test_unexpected_oserror_is_one_line_without_a_path(self):
        with mock.patch.object(backup, "export", side_effect=PermissionError(13, "Permission denied", str(self.dest / "secret"))):
            code, out, err = self.go()
        self.assertEqual((code, out, err), (1, "", "backup failed: Permission denied\n"))
        self.assertEqual(self.names(), [])

    def test_lock_file_stops_an_overlapping_run_and_is_kept(self):
        lock = self.dest / ".deflock-backup.lock"
        lock.write_text("")
        code, out, err = self.go()
        self.assertEqual((code, out, err), (1, "", "backup failed: another backup is running\n"))
        self.assertEqual(self.names(), [".deflock-backup.lock"])
        lock.unlink()
        self.assertEqual(self.go()[0], 0)
        self.assertEqual(len(self.names()), 1)  # the lock is gone after a finished run

    def test_bad_config_value_is_refused_before_anything_is_written(self):
        for value in ("a string", ["encrypt_to"], {"encrypt_to": 5}, {"encrypt_to": ["k.asc"]},
                      {"encrypt_to": "missing.asc"}, {"encrypt_to": ""}):
            with self.subTest(value=value):
                self.set_backup_config(value)
                code, out, err = self.go()
                self.assertEqual((code, out), (1, ""))
                self.assertRegex(err, r"\Abackup failed: campaign\.json backup\.encrypt_to[^\n]*\n\Z")
                self.assertEqual(self.names(), [])
        self.set_backup_config({})
        self.assertEqual(self.go()[0], 0)  # an empty backup object means plain


@unittest.skipUnless(GPG, "gpg is not installed")
class EncryptedIntoTests(IntoBase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix="synthetic-keys-")
        cls.home_a, cls.pub_a = make_key(Path(cls.keys.name), "organizer")
        cls.home_b, cls.pub_b = make_key(Path(cls.keys.name), "stranger")

    @classmethod
    def tearDownClass(cls):
        for home in (cls.home_a, cls.home_b):
            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], capture_output=True, timeout=30)
            shutil.rmtree(home, ignore_errors=True)
        cls.keys.cleanup()

    def test_config_key_gives_tar_gpg_and_flag_overrides_it(self):
        shutil.copy(self.pub_b, self.root / "organizer.asc")  # config names the stranger's key, relative to the campaign
        self.set_backup_config({"encrypt_to": "organizer.asc"})
        with mock.patch.object(backup, "scheduled_name", return_value=FIXED):
            code, out, err = self.go()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.names(), [FIXED + ".gpg"])
        self.assertEqual(LINE.fullmatch(out).group(1, 2), (FIXED + ".gpg", "encrypted"))
        self.assertTrue(backup_crypt.is_encrypted(self.dest / (FIXED + ".gpg")))
        self.assertTrue(backup.verify(self.dest / (FIXED + ".gpg"), gnupg_home=self.home_b)["ok"])
        other = self.base / "dest2"
        other.mkdir(mode=0o700)
        os.chmod(other, 0o700)
        code, out, err = self.go("--encrypt-to", self.pub_a, dest=other)
        self.assertEqual((code, err), (0, ""))
        archive = other / self.names(other)[0]
        self.assertTrue(backup.verify(archive, gnupg_home=self.home_a)["ok"])
        with self.assertRaisesRegex(backup.BackupError, "cannot decrypt"):
            backup.verify(archive, gnupg_home=self.home_b)

    def test_encrypted_failure_leaves_no_plaintext_partial_or_lock(self):
        with mock.patch.object(backup, "verify", side_effect=backup.BackupError("synthetic verify failure")):
            code, out, err = self.go("--encrypt-to", self.pub_a)
        self.assertEqual((code, out, err), (1, "", "backup failed: synthetic verify failure\n"))
        self.assertEqual(self.names(), [])

    def test_encrypted_name_taken_is_not_overwritten(self):
        with mock.patch.object(backup, "scheduled_name", return_value=FIXED):
            self.assertEqual(self.go("--encrypt-to", self.pub_a)[0], 0)
            first = (self.dest / (FIXED + ".gpg")).read_bytes()
            code, out, err = self.go("--encrypt-to", self.pub_a)
        self.assertEqual((code, out, err), (1, "", f"backup: {FIXED}.gpg exists; not overwritten\n"))
        self.assertEqual((self.dest / (FIXED + ".gpg")).read_bytes(), first)
        self.assertEqual(self.names(), [FIXED + ".gpg"])


class DocsCommandsParseTests(unittest.TestCase):
    def test_scheduled_section_commands_parse_and_install_nothing(self):
        import shlex
        text = (REPO_ROOT / "docs" / "BACKUP.md").read_text(encoding="utf-8")
        section = text.split("## Scheduled backups", 1)[1].split("\n## ", 1)[0]
        commands = [shlex.split(m.group(1)) for m in re.finditer(r"python3 -m campaign_tool (.+?)(?:`|$)", section, re.M)]
        kinds = [c[0] for c in commands]
        self.assertEqual(sorted(kinds), ["backup", "backup", "backup-list"])
        for argv in commands:
            seen = []
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", ["campaign_tool", *argv]), \
                    mock.patch.object(cli, "backup_cmd", side_effect=lambda a, r: seen.append(a)), \
                    mock.patch.object(cli, "backup_list_cmd", side_effect=seen.append), \
                    mock.patch.object(subprocess, "run", side_effect=AssertionError("nothing may be installed")):
                cli.main()
            self.assertEqual(len(seen), 1)
            if argv[0] == "backup":
                self.assertTrue(seen[0].into and seen[0].out is None)
            else:
                self.assertEqual((seen[0].keep_daily, seen[0].keep_weekly), (7, 8))
        self.assertNotRegex(section, r"(?m)^\s*(sudo |systemctl (?!--user)|crontab -)")


if __name__ == "__main__":
    unittest.main()
