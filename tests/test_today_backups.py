"""DF0237-10: `today --backups DIR [--backup-days N]` verified-backup reminder. Synthetic campaigns and archives only.

Run from the repository root: python3 -B -m unittest tests.test_today_backups -v
"""
import contextlib
import hashlib
import io
import json
import shlex
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup, cli, today  # noqa: E402

AS_OF = date(2026, 3, 15)
KEYS = {"rank", "kind", "what", "due", "command"}


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def row(name, created, verify="ok"):
    return {"name": name, "verify": verify, "created_at": created if verify == "ok" else None}


def listing(*rows, skipped=()):
    return {"archives": list(rows), "skipped": list(skipped)}


def tree_state(root):
    state = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
        state[path.relative_to(root).as_posix()] = (info.st_mtime_ns, info.st_size, digest)
    return state


class TodayBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-today-backups-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        self.assertEqual(code, 0, err)
        self.dest = self.tmp / "my backups"
        self.dest.mkdir()

    def items(self, *rows, skipped=(), days=7, folder=None):
        with mock.patch.object(backup, "list_archives", return_value=listing(*rows, skipped=skipped)):
            return today.todo(self.root, AS_OF, backups=folder or self.dest, backup_days=days)

    def due(self, items):
        return [item for item in items if item["kind"] == "backup_due"]

    def test_no_backups_is_byte_identical_and_never_scans(self):
        with mock.patch.object(backup, "list_archives", side_effect=AssertionError("scanned")):
            plain = run("today", "--directory", self.root, "--as-of", "2026-03-15")
            plain_json = run("today", "--directory", self.root, "--as-of", "2026-03-15", "--json")
        self.assertEqual(plain[0], 0)
        self.assertEqual(plain[1], "Nothing due on 2026-03-15.\n")
        self.assertEqual(json.loads(plain_json[1]), [])
        self.assertEqual(today.todo(self.root, AS_OF), today.todo(self.root, AS_OF, backups=None))

    def test_none_old_exact_boundary_and_recent(self):
        command = f"backup --directory {shlex.quote(str(self.root))} --into {shlex.quote(str(self.dest))}"
        for rows, skipped in (((), ()), ((row("bad.tar", None, "failed"),), ()), ((), ({"name": "l.tar", "reason": "symlink"},))):
            found = self.due(self.items(*rows, skipped=skipped))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["what"], f"No verified backup found in {self.dest}.")
            self.assertIsNone(found[0]["due"])
            self.assertEqual(found[0]["command"], command)
        for days, created, expected in ((7, "2026-03-08T12:00:00+00:00", 0), (7, "2026-03-07T23:59:00+00:00", 1),
                                        (7, "2026-03-14T00:00:00+00:00", 0), (1, "2026-03-14T01:00:00+00:00", 0),
                                        (1, "2026-03-13T01:00:00+00:00", 1), (90, "2025-12-15T00:00:00+00:00", 0),
                                        (90, "2025-12-14T00:00:00+00:00", 1)):
            found = self.due(self.items(row("a.tar", created), days=days))
            self.assertEqual(len(found), expected, (days, created))
        found = self.due(self.items(row("a.tar", "2026-03-07T23:59:00+00:00")))[0]
        self.assertEqual(found["what"], "Last verified backup a.tar was created 2026-03-07 (8 days ago; limit 7 days).")
        self.assertEqual(found["due"], "2026-03-07")
        self.assertEqual(found["command"], command)

    def test_recorded_utc_time_not_mtime_or_name(self):
        # newest by recorded instant (UTC 2026-03-14 02:00) although its name sorts first and offsets differ
        rows = (row("z-2026-01-01.tar", "2026-03-01T00:00:00+00:00"),
                row("a-2020.tar", "2026-03-13T18:00:00-08:00"))
        self.assertEqual(self.due(self.items(*rows)), [])
        # a calendar date in UTC: -08:00 evening of the 7th is the 8th UTC -> exactly 7 days, not due
        self.assertEqual(self.due(self.items(row("w.tar", "2026-03-07T20:00:00-08:00"))), [])
        # equal instants: the first in list order is named
        found = self.due(self.items(row("b.tar", "2026-01-01T00:00:00+00:00"), row("c.tar", "2026-01-01T01:00:00+01:00")))
        self.assertIn("backup b.tar was created 2026-01-01", found[0]["what"])
        # future-created is never overdue; leap day ages
        self.assertEqual(self.due(self.items(row("f.tar", "2026-04-01T00:00:00+00:00"))), [])
        with_leap = today.todo(self.root, date(2028, 3, 8), backups=self.dest, backup_days=7)
        self.assertEqual(len(self.due(with_leap)), 1)

    def test_real_archive_age_comes_from_created_at_not_mtime(self):
        for name, when in (("new-name.tar", "2026-03-01T00:00:00+00:00"), ("old-name.tar", "2026-03-14T00:00:00+00:00")):
            with mock.patch.object(backup, "_now", return_value=when):
                backup.export(self.root, self.dest / name)
        before = tree_state(self.root), tree_state(self.dest)
        code, out, _ = run("today", "--directory", self.root, "--as-of", "2026-03-15", "--backups", self.dest)
        self.assertEqual((code, out), (0, "Nothing due on 2026-03-15.\n"))
        code, out, _ = run("today", "--directory", self.root, "--as-of", "2026-03-30", "--backups", self.dest, "--json")
        found = json.loads(out)
        self.assertEqual([item["kind"] for item in found], ["backup_due"])
        self.assertEqual(set(found[0]), KEYS)
        self.assertIn("old-name.tar was created 2026-03-14", found[0]["what"])
        self.assertEqual((tree_state(self.root), tree_state(self.dest)), before)

    def test_failed_newest_is_named_never_counts(self):
        bad = row("2099-newest.tar", None, "failed")
        items = self.items(bad, row("old.tar", "2026-01-01T00:00:00+00:00"), row("also-bad.tar.gpg", None, "failed"))
        self.assertEqual(len(self.due(items)), 1)
        names = [item for item in items if item["kind"] == "unreadable"]
        self.assertEqual([item["what"] for item in names], [
            f"fix {self.dest}/2099-newest.tar: archive could not be verified; not counted as a backup",
            f"fix {self.dest}/also-bad.tar.gpg: archive could not be verified; not counted as a backup"])
        self.assertEqual(names[0]["command"], f"backup-list --dir {shlex.quote(str(self.dest))}")
        fresh = self.items(bad, row("ok.tar", "2026-03-14T00:00:00+00:00"))
        self.assertEqual([item["kind"] for item in fresh], ["unreadable"])
        # a real corrupt archive and a mocked encrypted-reader failure both fail verification
        (self.dest / "corrupt.tar").write_bytes(b"not a tar")
        with mock.patch.object(backup, "verify", side_effect=backup.BackupError("gpg: no secret key")):
            real = today.todo(self.root, AS_OF, backups=self.dest)
        self.assertEqual(sorted(item["kind"] for item in real), ["backup_due", "unreadable"])
        self.assertNotIn("secret key", json.dumps(real))

    def test_bad_folder_and_failed_scan_preserve_other_items(self):
        (self.root / "content").mkdir(exist_ok=True)
        (self.root / "content" / "agencies.json").write_text("{broken")
        link = self.tmp / "link"
        link.symlink_to(self.dest)
        plain_file = self.tmp / "file"
        plain_file.write_text("x")
        for folder in (self.tmp / "missing", plain_file, link):
            found = today.todo(self.root, AS_OF, backups=folder)
            self.assertEqual(len(self.due(found)), 1, folder)
            self.assertEqual(self.due(found)[0]["what"], f"Could not establish a verified backup in {folder}.")
            self.assertTrue(any(i["what"] == f"fix {folder}: backup directory could not be read" for i in found))
            self.assertTrue(any("agencies.json" in i["what"] for i in found), "other source rows retained")
            self.assertFalse((self.tmp / "missing").exists())
        for exc in (PermissionError("denied"), FileNotFoundError("raced")):
            with mock.patch.object(backup, "list_archives", side_effect=exc):
                found = today.todo(self.root, AS_OF, backups=self.dest)
            self.assertEqual(len(self.due(found)), 1)

    def test_symlinks_ignored_entries_and_encryption_contract(self):
        with mock.patch.object(backup, "_now", return_value="2026-03-14T00:00:00+00:00"):
            backup.export(self.root, self.dest / "real.tar")
        (self.dest / "sub").mkdir()
        (self.dest / "notes.txt").write_text("x")
        (self.dest / "link.tar").symlink_to(self.dest / "real.tar")
        found = today.todo(self.root, AS_OF, backups=self.dest)
        self.assertEqual([item["what"] for item in found],
                         [f"fix {self.dest}/link.tar: symlink archive skipped; not counted as a backup"])
        # an encrypted archive counts when the reader verifies it (mocked; no keyring is touched)
        self.assertEqual(self.due(self.items(dict(row("a.tar.gpg", "2026-03-14T00:00:00+00:00"), encrypted=True))), [])

    def test_flags_help_and_shell_quoting(self):
        for good in ("1", "7", "90"):
            self.assertEqual(run("today", "--directory", self.root, "--backups", self.dest, "--backup-days", good)[0], 0)
        for bad in ("0", "91", "-1", "+7", "7.0", "", " 7", "\u0667"):
            self.assertEqual(run("today", "--directory", self.root, "--backups", self.dest, "--backup-days", bad)[0], 2, bad)
        code, _, err = run("today", "--directory", self.root, "--backup-days", "7")
        self.assertEqual(code, 2)
        self.assertIn("--backup-days requires --backups", err)
        self.assertEqual(run("today", "--directory", self.root, "--backups")[0], 2)
        for bad in (True, 7.0, "7", 0, 91):
            with self.assertRaises(backup.BackupError):
                today.todo(self.root, AS_OF, backups=self.dest, backup_days=bad)
        self.assertIn("Check verified campaign archives in DIR (read-only; optional)", run("today", "--help")[1].replace("\n", " ").replace("  ", ""))
        odd = self.tmp / "it's; $(x)\nname"
        found = self.due(today.todo(self.root, AS_OF, backups=odd))
        self.assertNotIn("\n", found[0]["what"])
        self.assertIn("\\n", found[0]["what"])
        self.assertEqual(shlex.split(found[0]["command"]), ["backup", "--directory", str(self.root), "--into", str(odd)])
        with mock.patch("subprocess.run", side_effect=AssertionError("executed")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("executed")):
            today.todo(self.root, AS_OF, backups=odd)

    def test_read_only_rank_and_json_contract(self):
        (self.root / "content").mkdir(exist_ok=True)
        (self.root / "content" / "agencies.json").write_text("{broken")
        (self.dest / "corrupt.tar").write_bytes(b"x")
        before = tree_state(self.root), tree_state(self.dest)
        self.assertEqual(today.KINDS[-2:], ("backup_due", "unreadable"))
        self.assertEqual(today.KINDS.index("backup_due"), today.KINDS.index("review_waiting") + 3)
        code, out, _ = run("today", "--directory", self.root, "--as-of", "2026-03-15", "--backups", self.dest, "--json")
        self.assertEqual(code, 0)
        found = json.loads(out)
        self.assertEqual([item["rank"] for item in found], list(range(1, len(found) + 1)))
        self.assertTrue(all(set(item) == KEYS for item in found))
        kinds = [item["kind"] for item in found]
        self.assertEqual(kinds[0], "backup_due")
        self.assertTrue(set(kinds[1:]) == {"unreadable"} and len(kinds) == 3, kinds)
        run("today", "--directory", self.root, "--backups", self.dest)
        self.assertEqual((tree_state(self.root), tree_state(self.dest)), before)
        self.assertEqual(run("today", "--directory", self.root, "--as-of", "nope", "--backups", self.dest)[0], 1)


if __name__ == "__main__":
    unittest.main()
