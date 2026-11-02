"""backup-list: archive inventory and a read-only keep-rule report (exit 0/3/1/2).

Synthetic campaign directories only; archives are made with ``backup.export``.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup  # noqa: E402

FOOTER = "Report only; nothing was deleted."
# name -> created_at. ISO week 44 of 2026 is Mon 10-26 .. Sun 11-01; names deliberately not in time order.
TIMES = {
    "a-d1-early.tar": "2026-10-29T10:00:00+00:00",
    "m-d1-late.tar": "2026-10-29T18:00:00+00:00",
    "b-d2-early.tar": "2026-10-30T09:00:00+00:00",
    "k-d2-late.TAR": "2026-10-30T12:00:00+00:00",
    "z-week43.tar": "2026-10-21T12:00:00+00:00",
    "c-week42.tar": "2026-10-14T12:00:00+00:00",
}


def cli(*args, timeout=60):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=timeout)


def report_of(result):
    text, footer = result.stdout.rstrip("\n").rsplit("\n", 1)
    assert footer == FOOTER, result.stdout[-200:]
    return json.loads(text)


def tree_state(root):
    state = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
        state[path.relative_to(root).as_posix()] = (info.st_mtime_ns, info.st_size, digest)
    return state


class BackupListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="synthetic-backup-list-")
        cls.base = Path(cls.tmp.name)
        root = cls.base / "campaign"
        r = cli("init", "--directory", root, "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        assert r.returncode == 0, r.stderr
        (root / "content").mkdir()
        (root / "content" / "site.json").write_text('{"schema_version":1,"tagline":"synthetic-tagline"}')
        cls.made, cls.digests = cls.base / "made", {}
        cls.made.mkdir()
        extra = {"same-utc-day-west.tar": "2026-10-30T20:00:00-08:00", "same-utc-day-utc.tar": "2026-10-31T01:00:00+00:00",
                 "bad-word-time.tar": "yesterday", "bad-naive-time.tar": "2026-10-29T10:00:00"}
        for name, when in {**TIMES, **extra}.items():
            with mock.patch.object(backup, "_now", return_value=when):
                cls.digests[name] = backup.export(root, cls.made / name)["manifest_sha256"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def folder(self, *names):
        folder = Path(tempfile.mkdtemp(prefix="archives-", dir=self.base))
        for name in names:
            shutil.copy2(self.made / name, folder / name)
        return folder

    def corrupt(self, folder, name):
        """Copy an archive under ``name`` with one byte flipped inside a member's data."""
        target = folder / name
        shutil.copy2(self.made / "a-d1-early.tar", target)
        with tarfile.open(target) as tar:
            offset = tar.getmember("campaign.json").offset_data
        with open(target, "r+b") as handle:
            handle.seek(offset)
            byte = handle.read(1)
            handle.seek(offset)
            handle.write(bytes([byte[0] ^ 0x01]))

    def test_good_archives_newest_first_with_real_digests_exit_0(self):
        folder = self.folder("a-d1-early.tar", "k-d2-late.TAR", "z-week43.tar")
        result = cli("backup-list", "--dir", folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = report_of(result)
        self.assertEqual([a["name"] for a in report["archives"]], ["k-d2-late.TAR", "a-d1-early.tar", "z-week43.tar"])
        for item in report["archives"]:
            self.assertEqual(item["verify"], "ok")
            self.assertEqual(item["manifest_sha256"], self.digests[item["name"]])
            self.assertEqual(item["created_at"], TIMES[item["name"]])
            self.assertEqual(item["bytes"], (folder / item["name"]).stat().st_size)
            self.assertIsInstance(item["files"], int)
        self.assertEqual(report["skipped"], [])
        self.assertNotIn("rule", report)
        self.assertNotIn(str(folder), result.stdout)

    def test_corrupt_and_non_tar_fail_last_others_listed_exit_3(self):
        folder = self.folder("a-d1-early.tar", "z-week43.tar")
        self.corrupt(folder, "0-corrupt.tar")
        (folder / "x.tar").write_bytes(b"not a tar archive\n")
        result = cli("backup-list", "--dir", folder)
        self.assertEqual(result.returncode, 3, result.stderr)
        archives = report_of(result)["archives"]
        self.assertEqual([a["name"] for a in archives], ["a-d1-early.tar", "z-week43.tar", "0-corrupt.tar", "x.tar"])
        for item in archives[2:]:
            self.assertEqual(item["verify"], "failed")
            self.assertTrue(item["reason"])
            self.assertLessEqual(len(item["reason"]), 200)
            self.assertEqual([item[k] for k in ("created_at", "manifest_sha256", "files")], [None] * 3)
        self.assertIn("hash_mismatch", archives[2]["reason"])

    def test_bad_created_at_fails_even_though_members_verify(self):
        folder = self.folder("bad-word-time.tar", "bad-naive-time.tar", "a-d1-early.tar")
        report = backup.list_archives(folder, keep_daily=5)
        self.assertEqual([(a["name"], a["verify"]) for a in report["archives"]],
                         [("a-d1-early.tar", "ok"), ("bad-naive-time.tar", "failed"), ("bad-word-time.tar", "failed")])
        self.assertIn("created_at", report["archives"][1]["reason"])
        self.assertEqual(report["rule"]["keep"], ["a-d1-early.tar"])
        self.assertEqual(report["rule"]["failed_never_kept"], ["bad-naive-time.tar", "bad-word-time.tar"])

    def test_ignored_entries_and_symlink_skipped_never_opened(self):
        folder = self.folder("a-d1-early.tar")
        (folder / "notes.txt").write_text("not an archive\n")
        (folder / "sub.tar").mkdir()
        shutil.copy2(self.made / "z-week43.tar", folder / "sub.tar" / "nested.tar")
        os.symlink(self.made / "k-d2-late.TAR", folder / "link.tar")
        with mock.patch.object(backup, "verify", wraps=backup.verify) as spy:
            report = backup.list_archives(folder)
        self.assertEqual([Path(call.args[0]).name for call in spy.call_args_list], ["a-d1-early.tar"])
        self.assertEqual([a["name"] for a in report["archives"]], ["a-d1-early.tar"])
        self.assertEqual(report["skipped"], [{"name": "link.tar", "reason": "symlink"}])

    def test_keep_rule_daily_and_weekly_union(self):
        folder = self.folder(*TIMES)
        (folder / "x.tar").write_bytes(b"not a tar archive\n")
        result = cli("backup-list", "--dir", folder, "--keep-daily", "1", "--keep-weekly", "2")
        self.assertEqual(result.returncode, 3, result.stderr)
        rule = report_of(result)["rule"]
        self.assertEqual(rule, {"keep_daily": 1, "keep_weekly": 2, "keep": ["k-d2-late.TAR", "z-week43.tar"],
                                "would_not_keep": ["b-d2-early.tar", "m-d1-late.tar", "a-d1-early.tar", "c-week42.tar"],
                                "failed_never_kept": ["x.tar"]})

    def test_same_day_keeps_only_the_newer(self):
        folder = self.folder(*TIMES)
        rule = backup.list_archives(folder, keep_daily=2)["rule"]
        self.assertEqual(rule["keep"], ["k-d2-late.TAR", "m-d1-late.tar"])
        self.assertIsNone(rule["keep_weekly"])
        # Days are UTC: 20:00 at -08:00 on 10-30 is 04:00 UTC on 10-31, the same day as 01:00 UTC.
        folder = self.folder("same-utc-day-west.tar", "same-utc-day-utc.tar")
        rule = backup.list_archives(folder, keep_daily=366)["rule"]
        self.assertEqual((rule["keep"], rule["would_not_keep"]), (["same-utc-day-west.tar"], ["same-utc-day-utc.tar"]))
        # A byte-identical copy under another name: same time, the first by name is kept, not both.
        folder = self.folder("m-d1-late.tar")
        shutil.copy2(self.made / "m-d1-late.tar", folder / "copy.tar")
        rule = backup.list_archives(folder, keep_daily=1)["rule"]
        self.assertEqual((rule["keep"], rule["would_not_keep"]), (["copy.tar"], ["m-d1-late.tar"]))

    def test_zero_keeps_nothing_and_out_of_range_is_argparse_error(self):
        folder = self.folder(*TIMES)
        result = cli("backup-list", "--dir", folder, "--keep-daily", "0", "--keep-weekly", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        rule = report_of(result)["rule"]
        self.assertEqual(rule["keep"], [])
        self.assertEqual(sorted(rule["would_not_keep"]), sorted(TIMES))
        for flag, value in (("--keep-daily", "-1"), ("--keep-daily", "400"), ("--keep-daily", "367"),
                            ("--keep-weekly", "261"), ("--keep-weekly", "two")):
            result = cli("backup-list", "--dir", folder, flag, value)
            self.assertEqual(result.returncode, 2, (flag, value, result.stderr))
            self.assertEqual(result.stdout, "")
        for kwargs in ({"keep_daily": -1}, {"keep_weekly": 261}, {"keep_daily": True}, {"keep_daily": "1"}):
            with self.assertRaises(backup.BackupError):
                backup.list_archives(folder, **kwargs)

    def test_empty_folder_and_bad_dir(self):
        folder = self.folder()
        result = cli("backup-list", "--dir", folder, "--keep-daily", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report_of(result), {"archives": [], "skipped": [], "rule": {
            "keep_daily": 3, "keep_weekly": None, "keep": [], "would_not_keep": [], "failed_never_kept": []}})
        os.symlink(self.made, self.base / "made-link")
        for bad in (self.made / "a-d1-early.tar", self.base / "missing", self.base / "made-link"):
            result = cli("backup-list", "--dir", bad)
            self.assertEqual(result.returncode, 1, bad)
            self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
            self.assertEqual(result.stdout, "")

    def test_read_only_listing_bytes_and_mtimes_unchanged(self):
        folder = self.folder(*TIMES)
        self.corrupt(folder, "0-corrupt.tar")
        (folder / "x.tar").write_bytes(b"not a tar archive\n")
        os.symlink(self.made / "a-d1-early.tar", folder / "link.tar")
        before = tree_state(folder)
        result = cli("backup-list", "--dir", folder, "--keep-daily", "0", "--keep-weekly", "1")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(tree_state(folder), before)
        self.assertEqual(report_of(result)["rule"]["keep"], ["k-d2-late.TAR"])


if __name__ == "__main__":
    unittest.main()
