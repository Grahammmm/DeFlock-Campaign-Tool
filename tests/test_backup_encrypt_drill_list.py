"""FDF-02: backup-drill and backup-list read encrypted archives (synthetic data; keys made at run time)."""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup, backup_crypt, cli  # noqa: E402
from tests.test_backup_encrypt import GPG, make_home  # noqa: E402

FOOTER = "Report only; nothing was deleted."
STEPS = ["verify", "restore", "store_check", "doctor"]
# name -> (created_at, encrypted). Keep rule below mixes both kinds across days and ISO weeks.
TIMES = {"a": "2026-10-29T10:00:00+00:00", "b": "2026-10-29T18:00:00+00:00", "c": "2026-10-30T09:00:00+00:00",
         "d": "2026-10-30T12:00:00+00:00", "e": "2026-10-21T12:00:00+00:00", "f": "2026-10-14T12:00:00+00:00"}
ENCRYPTED = {"b", "d", "f"}


def run_cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


def listing(folder):
    return {p.name: (p.lstat().st_mtime_ns, p.lstat().st_size) for p in sorted(Path(folder).iterdir())}


@unittest.skipUnless(GPG, "gpg is not installed")
class EncryptedDrillListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="synthetic-fdf02-")
        cls.base = Path(cls.tmp.name)
        cls.home_a, cls.pub_a, _ = make_home(cls.base, "organizer")
        cls.home_b, cls.pub_b, _ = make_home(cls.base, "stranger")
        cls.root = cls.base / "campaign"
        r = run_cli("init", "--directory", cls.root, "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        assert r.returncode == 0, r.stderr
        (cls.base / "policy.txt").write_bytes(b"Synthetic FDF-02 policy text.\n")
        r = run_cli("ingest", "--directory", cls.root, "--file", cls.base / "policy.txt", "--source-id", "demo-1")
        assert r.returncode == 0, r.stderr
        cls.made = cls.base / "made"
        cls.made.mkdir()
        cls.plain = cls.made / "plain.tar"
        cls.enc = cls.made / "enc.tar.gpg"
        # One created_at for both exports: a manifest stamps it to the second, so a plain and an
        # encrypted export that straddle a second boundary carry different manifest_sha256 values.
        with patch.object(backup, "_now", return_value=backup._now()):
            cls.manifest_sha = backup.export(cls.root, cls.plain)["manifest_sha256"]
            backup.export(cls.root, cls.enc, encrypt_to=cls.pub_a)
        cls.for_stranger = cls.made / "stranger.tar.gpg"
        backup.export(cls.root, cls.for_stranger, encrypt_to=cls.pub_b)

    @classmethod
    def tearDownClass(cls):
        for home in (cls.home_a, cls.home_b):
            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], capture_output=True, timeout=30)
            shutil.rmtree(home, ignore_errors=True)
        cls.tmp.cleanup()

    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="synthetic-fdf02-case-")
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.parent = self.work / "tmp-parent"
        self.parent.mkdir()

    def main(self, *argv):
        """In-process CLI run with every temp dir under self.parent; returns (exit code, stdout, stderr)."""
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        out, err, code = io.StringIO(), io.StringIO(), 0
        with patch.object(sys, "argv", ["campaign_tool", *map(str, argv)]), \
                patch.object(tempfile, "tempdir", str(self.parent)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def drill(self, archive, *extra):
        code, out, err = self.main("backup-drill", "--file", archive, *extra)
        return code, json.loads(out), err

    def gpg_calls(self):
        calls = []
        real = backup_crypt._gpg

        def counting(args, *a, **kw):
            calls.append(list(args))
            return real(args, *a, **kw)
        return calls, patch.object(backup_crypt, "_gpg", counting)

    def folder(self, items):
        folder = self.work / "archives"
        folder.mkdir(exist_ok=True)
        for name, source in items.items():
            shutil.copy2(source, folder / name)
        return folder

    # 1 encrypted drill == plain drill; source never opened
    def test_encrypted_drill_passes_with_same_steps_as_plain(self):
        moved = self.work / "elsewhere"
        shutil.move(self.root, moved)
        self.addCleanup(shutil.move, moved, self.root)
        code, plain, err = self.drill(self.plain, "--expect-manifest-sha256", self.manifest_sha)
        self.assertEqual(code, 0, err)
        code, receipt, err = self.drill(self.enc, "--gnupg-home", self.home_a, "--expect-manifest-sha256", self.manifest_sha)
        self.assertEqual(code, 0, err)
        self.assertIs(plain["encrypted"], False)
        self.assertIs(receipt["encrypted"], True)
        self.assertIs(receipt["passed"], True)
        self.assertEqual(receipt["archive_name"], "enc.tar.gpg")
        self.assertEqual(receipt["manifest_sha256"], plain["manifest_sha256"])
        self.assertEqual(receipt["steps"], plain["steps"])
        self.assertEqual([s["step"] for s in receipt["steps"]], STEPS)
        self.assertEqual(list(self.parent.iterdir()), [], "temp copy left behind")
        self.assertFalse(self.root.exists(), "drill opened or recreated the source directory")

    # 2 wrong key home: failed verify, later steps not run, exit 3, receipt written, nothing left behind
    def test_wrong_key_home_fails_verify_and_still_writes_receipt(self):
        target = self.work / "receipt.json"
        code, receipt, _ = self.drill(self.enc, "--gnupg-home", self.home_b, "--receipt", target)
        self.assertEqual(code, 3)
        self.assertIs(receipt["encrypted"], True)
        self.assertIs(receipt["passed"], False)
        self.assertEqual([s["step"] for s in receipt["steps"]], STEPS)
        self.assertIs(receipt["steps"][0]["ok"], False)
        self.assertIn("cannot decrypt", receipt["steps"][0]["detail"])
        self.assertEqual([(s["ok"], s["detail"]) for s in receipt["steps"][1:]], [(None, "not run")] * 3)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), receipt)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertNotIn(str(self.home_b), json.dumps(receipt))
        self.assertEqual(list(self.parent.iterdir()), [], "decrypted copy left behind")

    # 3 the private key is used once per drill
    def test_one_decrypt_per_encrypted_drill_none_for_plain(self):
        calls, patched = self.gpg_calls()
        with patched:
            code, receipt, err = self.drill(self.enc, "--gnupg-home", self.home_a)
        self.assertEqual(code, 0, err)
        self.assertEqual(sum("--decrypt" in args for args in calls), 1, calls)
        calls, patched = self.gpg_calls()
        with patched:
            code, receipt, err = self.drill(self.plain)
        self.assertEqual((code, calls), (0, []))
        self.assertIs(receipt["encrypted"], False)

    def test_drill_of_missing_archive_still_exits_1(self):
        code, out, err = self.main("backup-drill", "--file", self.work / "none.tar.gpg", "--gnupg-home", self.home_a)
        self.assertEqual(code, 1)

    # 4 mixed folder
    def test_mixed_folder_flags_each_archive(self):
        (self.work / "target.tar.gpg").write_bytes(b"never opened")
        folder = self.folder({"plain.tar": self.plain, "enc.tar.gpg": self.enc, "disguised.tar.gpg": self.plain,
                              "stranger.tar.gpg": self.for_stranger})
        (folder / "link.tar.gpg").symlink_to(self.work / "target.tar.gpg")
        (folder / "notes.txt").write_text("not an archive\n")
        (folder / "sub.tar.gpg").mkdir()
        before = listing(folder)
        opened = []
        real = backup_crypt.is_encrypted
        with patch.object(backup_crypt, "is_encrypted", lambda p: opened.append(Path(p).name) or real(p)):
            code, out, err = self.main("backup-list", "--dir", folder, "--gnupg-home", self.home_a,
                                       "--keep-daily", 5, "--keep-weekly", 5)
        self.assertEqual(code, 3, err)
        text, footer = out.rstrip("\n").rsplit("\n", 1)
        self.assertEqual(footer, FOOTER)
        report = json.loads(text)
        items = {a["name"]: a for a in report["archives"]}
        self.assertEqual(set(items), {"plain.tar", "enc.tar.gpg", "disguised.tar.gpg", "stranger.tar.gpg"})
        self.assertEqual({n: (a["encrypted"], a["verify"]) for n, a in items.items()},
                         {"plain.tar": (False, "ok"), "enc.tar.gpg": (True, "ok"), "disguised.tar.gpg": (False, "ok"),
                          "stranger.tar.gpg": (True, "failed")})
        self.assertIn("cannot decrypt", items["stranger.tar.gpg"]["reason"])
        self.assertEqual(report["archives"][-1]["name"], "stranger.tar.gpg")
        self.assertEqual(report["skipped"], [{"name": "link.tar.gpg", "reason": "symlink"}])
        self.assertEqual(report["rule"]["failed_never_kept"], ["stranger.tar.gpg"])
        self.assertNotIn("stranger.tar.gpg", report["rule"]["keep"] + report["rule"]["would_not_keep"])
        self.assertNotIn("link.tar.gpg", opened)
        self.assertNotIn("notes.txt", opened)
        self.assertEqual(listing(folder), before, "backup-list wrote into the folder")
        self.assertEqual(list(self.parent.iterdir()), [], "decrypted copy left behind")

    def test_upper_case_gpg_name_is_listed(self):
        folder = self.folder({"ENC.TAR.GPG": self.enc})
        report = backup.list_archives(folder, gnupg_home=self.home_a)
        self.assertEqual([(a["name"], a["encrypted"], a["verify"]) for a in report["archives"]],
                         [("ENC.TAR.GPG", True, "ok")])

    # 5 keep rule is the same as for an all-plain folder of the same times
    def test_keep_rule_mixes_encrypted_and_plain_like_all_plain(self):
        mixed, control = self.work / "mixed", self.work / "control"
        mixed.mkdir()
        control.mkdir()
        for key, when in TIMES.items():
            with patch.object(backup, "_now", return_value=when):
                backup.export(self.root, control / f"{key}.tar")
                if key in ENCRYPTED:
                    backup.export(self.root, mixed / f"{key}.tar.gpg", encrypt_to=self.pub_a)
                else:
                    backup.export(self.root, mixed / f"{key}.tar")
        got = backup.list_archives(mixed, 2, 2, gnupg_home=self.home_a)
        want = backup.list_archives(control, 2, 2)

        def plainname(report):
            return {key: [n.replace(".tar.gpg", "").replace(".tar", "") for n in names] for key, names in report["rule"].items()
                    if isinstance(names, list)}
        self.assertEqual(plainname(got), plainname(want))
        self.assertEqual(plainname(got)["keep"], ["d", "b", "e"])  # two encrypted, one plain
        self.assertEqual([a["created_at"] for a in got["archives"]], [a["created_at"] for a in want["archives"]])
        self.assertEqual({a["name"][0]: a["encrypted"] for a in got["archives"]}, {k: k in ENCRYPTED for k in TIMES})

    # 6 gpg missing
    def test_gpg_missing_plain_still_works_encrypted_fail(self):
        folder = self.folder({"plain.tar": self.plain, "enc.tar.gpg": self.enc})
        with patch.object(backup_crypt.shutil, "which", return_value=None):
            report = backup.list_archives(folder, 1, 1, gnupg_home=self.home_a)
            plain_receipt = backup.drill(self.plain, tmp_parent=self.parent)
            enc_receipt = backup.drill(self.enc, tmp_parent=self.parent, gnupg_home=self.home_a)
        items = {a["name"]: a for a in report["archives"]}
        self.assertEqual((items["plain.tar"]["verify"], items["plain.tar"]["encrypted"]), ("ok", False))
        self.assertEqual((items["enc.tar.gpg"]["verify"], items["enc.tar.gpg"]["encrypted"]), ("failed", True))
        self.assertIn("gpg not found", items["enc.tar.gpg"]["reason"])
        self.assertEqual(report["rule"]["keep"], ["plain.tar"])
        self.assertEqual(report["rule"]["failed_never_kept"], ["enc.tar.gpg"])
        self.assertIs(plain_receipt["passed"], True)
        self.assertIs(enc_receipt["passed"], False)
        self.assertIs(enc_receipt["steps"][0]["ok"], False)
        self.assertIn("gpg not found", enc_receipt["steps"][0]["detail"])
        self.assertEqual([s["ok"] for s in enc_receipt["steps"][1:]], [None] * 3)
        self.assertEqual(list(self.parent.iterdir()), [])

    # 7 CLI surface
    def test_cli_help_and_report_only_footer(self):
        self.assertIn("*.tar and *.tar.gpg", " ".join(run_cli("--help").stdout.split()))
        self.assertIn("--gnupg-home", run_cli("backup-list", "--help").stdout)
        self.assertIn("--gnupg-home", run_cli("backup-drill", "--help").stdout)
        folder = self.folder({"plain.tar": self.plain, "enc.tar.gpg": self.enc})
        before = listing(folder)
        r = run_cli("backup-list", "--dir", folder, "--gnupg-home", self.home_a)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.rstrip("\n")
        self.assertTrue(out.endswith(FOOTER), out[-80:])
        self.assertEqual({a["name"]: a["encrypted"] for a in json.loads(out[:-len(FOOTER)])["archives"]},
                         {"plain.tar": False, "enc.tar.gpg": True})
        self.assertEqual(listing(folder), before)


if __name__ == "__main__":
    unittest.main()
