"""backup-drill: restore into a temp copy, verify, store check and doctor there; one receipt (synthetic data only)."""
import contextlib
import hashlib
import io
import json
import os
import re
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
from campaign_tool import backup, cli  # noqa: E402

DATA = b"Synthetic drill policy text.\n"
STEPS = ["verify", "restore", "store_check", "doctor"]


def run_cli(*args, env=None):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60, env=env)


class BackupDrillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="synthetic-drill-")
        cls.base = Path(cls.tmp.name)
        cls.root = cls.base / "campaign"
        r = run_cli("init", "--directory", cls.root, "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        assert r.returncode == 0, r.stderr
        (cls.base / "policy.txt").write_bytes(DATA)
        r = run_cli("ingest", "--directory", cls.root, "--file", cls.base / "policy.txt", "--source-id", "demo-1")
        assert r.returncode == 0, r.stderr
        cls.sha = hashlib.sha256(DATA).hexdigest()
        cls.archive = cls.base / "good.tar"
        cls.manifest_sha = backup.export(cls.root, cls.archive)["manifest_sha256"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="synthetic-drill-case-")
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.parent = self.work / "tmp-parent"
        self.parent.mkdir()

    def main(self, *argv):
        """In-process CLI run with temp copies under self.parent; returns (exit code, stdout, stderr)."""
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch.object(sys, "argv", ["campaign_tool", *map(str, argv)]), \
                patch.object(tempfile, "tempdir", str(self.parent)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def assert_failed_at(self, receipt, failed):
        index = STEPS.index(failed)
        self.assertEqual([s["step"] for s in receipt["steps"]], STEPS)
        self.assertTrue(all(s["ok"] is True for s in receipt["steps"][:index]), receipt["steps"])
        self.assertIs(receipt["steps"][index]["ok"], False)
        self.assertEqual([(s["ok"], s["detail"]) for s in receipt["steps"][index + 1:]],
                         [(None, "not run")] * (len(STEPS) - index - 1))
        self.assertIs(receipt["passed"], False)
        self.assertEqual(list(self.parent.iterdir()), [], "temp copy left behind")

    def flipped_archive(self):
        target = self.work / "flipped.tar"
        shutil.copyfile(self.archive, target)
        with tarfile.open(target) as tar:
            offset = tar.getmember("private/objects/" + self.sha).offset_data
        with target.open("r+b") as handle:
            handle.seek(offset)
            byte = handle.read(1)
            handle.seek(offset)
            handle.write(bytes([byte[0] ^ 0x01]))
        return target

    # 1 good backup; the source campaign is never opened
    def test_good_backup_passes_and_leaves_no_copy(self):
        moved = self.work / "elsewhere"
        shutil.move(self.root, moved)
        self.addCleanup(shutil.move, moved, self.root)
        code, out, err = self.main("backup-drill", "--file", self.archive, "--expect-manifest-sha256", self.manifest_sha)
        self.assertEqual(code, 0, err)
        receipt = json.loads(out)
        self.assertEqual(receipt["schema"], "deflock-backup-drill-v1")
        self.assertEqual(receipt["archive_name"], "good.tar")
        self.assertEqual(receipt["manifest_sha256"], self.manifest_sha)
        self.assertIs(receipt["passed"], True)
        self.assertEqual([(s["step"], s["ok"]) for s in receipt["steps"]], [(name, True) for name in STEPS])
        self.assertEqual(receipt["steps"][2]["detail"], "0 problems")
        self.assertEqual(receipt["steps"][3]["detail"]["agencies_selected"], 0)
        self.assertIn("law_package_status", receipt["steps"][3]["detail"])
        self.assertEqual(list(self.parent.iterdir()), [], "temp copy left behind")
        self.assertFalse(self.root.exists(), "drill recreated or opened the source directory")

    def test_real_cli_uses_system_temp_and_removes_copy(self):
        env = dict(os.environ, TMPDIR=str(self.parent))
        r = run_cli("backup-drill", "--file", self.archive, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIs(json.loads(r.stdout)["passed"], True)
        self.assertEqual(list(self.parent.iterdir()), [])

    # 2 byte-flipped member; a failed drill still writes its receipt
    def test_byte_flipped_member_fails_verify(self):
        target = self.work / "receipt.json"
        code, out, _ = self.main("backup-drill", "--file", self.flipped_archive(), "--receipt", target)
        self.assertEqual(code, 3)
        receipt = json.loads(out)
        self.assert_failed_at(receipt, "verify")
        self.assertIn("hash_mismatch", receipt["steps"][0]["detail"])
        self.assertNotIn(self.sha, out)
        self.assertEqual(target.read_text(encoding="utf-8"), out)

    # 3 wrong expected manifest digest, including a near-miss
    def test_wrong_expected_digest_fails_verify(self):
        near = self.manifest_sha[:-1] + ("0" if self.manifest_sha[-1] != "0" else "1")
        for digest in ("0" * 64, near):
            with self.subTest(digest=digest[:8]):
                code, out, _ = self.main("backup-drill", "--file", self.archive, "--expect-manifest-sha256", digest)
                self.assertEqual(code, 3)
                receipt = json.loads(out)
                self.assert_failed_at(receipt, "verify")
                self.assertIn("manifest_digest_mismatch", receipt["steps"][0]["detail"])

    # 4 store check finds one problem
    def test_store_check_problem_fails_and_skips_doctor(self):
        seen = []

        def one_problem(root):
            seen.append(Path(root).is_dir() and (Path(root) / "campaign.json").is_file())
            return {"checked_objects": 1, "problems": [{"name": "x", "problem": "no_ledger_row"}]}

        with patch("campaign_tool.store_check.check_store", one_problem):
            code, out, _ = self.main("backup-drill", "--file", self.archive)
        self.assertEqual(code, 3)
        receipt = json.loads(out)
        self.assert_failed_at(receipt, "store_check")
        self.assertEqual(receipt["steps"][2]["detail"], "1 problem")
        self.assertEqual(seen, [True], "store check did not run on the restored copy")

    def test_doctor_unreadable_config_fails(self):
        for doctor in (lambda root: {"config_readable": False}, lambda root: cli.read_config(Path(root) / "absent")):
            self.assert_failed_at(backup.drill(self.archive, doctor=doctor, tmp_parent=self.parent), "doctor")

    # 5 restore raises mid-extract
    def test_restore_failure_mid_extract(self):
        real = shutil.copyfileobj
        calls = []

        def failing(source, target, *args, **kwargs):
            if isinstance(getattr(target, "name", None), int):  # restore writes through an fd, verify through a path
                calls.append(1)
                if len(calls) == 2:
                    raise OSError("synthetic disk full")
            return real(source, target, *args, **kwargs)

        with patch.object(backup.shutil, "copyfileobj", failing):
            code, out, _ = self.main("backup-drill", "--file", self.archive)
        self.assertEqual(code, 3)
        receipt = json.loads(out)
        self.assert_failed_at(receipt, "restore")
        self.assertIn("synthetic disk full", receipt["steps"][1]["detail"])
        self.assertEqual(len(calls), 2)

    # 6 --receipt new path and existing path (file or dangling symlink)
    def test_receipt_file_is_private_and_matches_stdout(self):
        target = self.work / "receipt.json"
        code, out, err = self.main("backup-drill", "--file", self.archive, "--receipt", target)
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.read_text(encoding="utf-8"), out)

    def test_existing_receipt_is_refused_unchanged(self):
        existing = self.work / "existing.json"
        existing.write_text("keep me\n")
        dangling = self.work / "dangling.json"
        os.symlink(self.work / "missing-target.json", dangling)
        for target in (existing, dangling):
            with self.subTest(target=target.name):
                code, out, err = self.main("backup-drill", "--file", self.archive, "--receipt", target)
                self.assertEqual((code, out, list(self.parent.iterdir())), (1, "", []))
                self.assertIn("Stopped:", err)
                self.assertIn("exists", err)
        self.assertEqual(existing.read_text(), "keep me\n")
        self.assertFalse((self.work / "missing-target.json").exists())

    # 7 missing archive; the receipt carries no absolute path and no member digests
    def test_missing_archive_stops(self):
        for path in (self.work / "absent.tar", self.work):
            code, out, err = self.main("backup-drill", "--file", path)
            self.assertEqual((code, out, list(self.parent.iterdir())), (1, "", []))
            self.assertIn("Stopped:", err)

    def test_receipt_has_no_paths_or_member_digests(self):
        code, out, _ = self.main("backup-drill", "--file", self.archive)
        self.assertEqual(code, 0)
        for text in (str(self.base), str(self.parent), "/deflock-drill-", "deflock-drill-", self.sha):
            self.assertNotIn(text, out)
        hexes = set(re.findall(r"[0-9a-f]{64}", out))
        self.assertEqual(hexes, {self.manifest_sha})
        self.assertEqual(set(json.loads(out)), {"schema", "archive_name", "manifest_sha256", "created_at", "drilled_at",
                                                "engine_version", "files", "passed", "steps"})

    def test_failure_detail_masks_temp_path_and_digests(self):
        def leaky(root):
            raise ValueError(f"{root}/private/objects/{self.sha}: bad")

        detail = backup.drill(self.archive, store_check=leaky, tmp_parent=self.parent)["steps"][2]["detail"]
        self.assertEqual(detail, "ValueError: <copy>/campaign/private/objects/<sha256>: bad")

    # 7b failed-drill receipts carry no absolute path from an unreadable archive or an unsafe member name
    def assert_no_abs_paths(self, text, *extra):
        for needle in (str(self.work), str(self.base), str(self.parent), "/unsafe-abs-dir", *extra):
            self.assertNotIn(needle, text)

    def test_unreadable_archive_receipt_has_no_source_path(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a mode-000 file")
        locked = self.work / "locked-archive.tar"
        shutil.copyfile(self.archive, locked)
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        target = self.work / "receipt.json"
        code, out, _ = self.main("backup-drill", "--file", locked, "--receipt", target)
        self.assertEqual(code, 3)
        self.assert_failed_at(json.loads(out), "verify")
        self.assertIn("PermissionError", out)
        self.assert_no_abs_paths(out)
        self.assert_no_abs_paths(target.read_text(encoding="utf-8"))

    def test_unsafe_absolute_member_name_receipt_has_no_path(self):
        evil = self.work / "evil.tar"
        payload = self.work / "payload.txt"
        payload.write_bytes(b"x")
        with tarfile.open(evil, "w") as tar:
            info = tar.gettarinfo(str(payload), arcname="/unsafe-abs-dir/etc/passwd")
            with payload.open("rb") as handle:
                tar.addfile(info, handle)
        target = self.work / "receipt.json"
        code, out, _ = self.main("backup-drill", "--file", evil, "--receipt", target)
        self.assertEqual(code, 3)
        self.assert_failed_at(json.loads(out), "verify")
        self.assert_no_abs_paths(out)
        self.assert_no_abs_paths(target.read_text(encoding="utf-8"))

    def test_failure_detail_redacts_member_and_archive_paths(self):
        def hostile(*_args, **_kwargs):
            raise backup.BackupError(f"/unsafe-abs-dir/etc/passwd: unsafe_member_name ({self.archive})")

        with patch.object(backup, "verify", hostile):
            detail = backup.drill(self.archive, tmp_parent=self.parent)["steps"][0]["detail"]
        self.assertEqual(detail, f"BackupError: <path>: unsafe_member_name ({self.archive.name})")

    # 8 doctor output unchanged by the doctor_report factor-out
    def test_doctor_output_is_doctor_report(self):
        r = run_cli("doctor", "--directory", self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, json.dumps(cli.doctor_report(self.root), indent=2) + "\n")
        self.assertEqual(list(json.loads(r.stdout))[:8], [
            "config_readable", "jurisdiction_verified", "reviewed_law_package", "law_package_status",
            "public_deployment_checked", "newsletter_checked", "safe_to_send_automatically", "production_ready"])


if __name__ == "__main__":
    unittest.main()
