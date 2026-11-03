"""RF0720-11: tools/flock_backup.py verify-all DIR re-verifies every *.tar in one folder (synthetic archives only)."""
import importlib.util
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("backup_store_fixtures", Path(__file__).with_name("test_backup_store.py"))
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)
backup, manifest_bytes, run, tree, write_tar = base.backup, base.manifest_bytes, base.run, base.tree, base.write_tar

FILES = {"agencies/slo-pd/FACTS.csv": b"agency,fact\nslo-pd,1\n", "regions/r.json": b'{"r": 1}\n', "LAW.csv": b"rule\n"}
OK = "ok | created_at=2026-10-31T00:00:00+00:00 files=3 bytes=%d" % sum(map(len, FILES.values()))


def archive(path, files=FILES, data=None, manifest_first=True):
    """A tar with a manifest of FILES; DATA overrides member bytes (same names) to simulate bit rot."""
    data = dict(files, **(data or {}))
    members = [(tarfile.TarInfo(name), body) for name, body in data.items()]
    head = (tarfile.TarInfo("manifest.json"), manifest_bytes(files))
    write_tar(path, [head] + members if manifest_first else members + [head])
    return path


class VerifyAllTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.dir = self.tmp / "backups"
        self.dir.mkdir()

    def check(self, code, out, err, rows, totals):
        self.assertEqual(err, "")
        self.assertEqual(out, "".join("backup | %s\n" % row for row in rows) + "verify-all | %s\n" % totals)
        return code

    def test_two_good_archives_are_ok_in_name_order(self):
        archive(self.dir / "b.tar")
        archive(self.dir / "a.tar")
        code = self.check(*run("verify-all", self.dir), ["a.tar | " + OK, "b.tar | " + OK],
                          "archives=2 ok=2 failed=0 skipped=0")
        self.assertEqual(code, 0)

    def test_flipped_byte_fails_that_archive_only(self):
        archive(self.dir / "a.tar")
        archive(self.dir / "b.tar", data={"LAW.csv": b"rulf\n"})
        code = self.check(*run("verify-all", self.dir), ["a.tar | " + OK, "b.tar | failed | LAW.csv: hash_mismatch"],
                          "archives=2 ok=1 failed=1 skipped=0")
        self.assertEqual(code, 1)

    def test_several_problems_show_the_first_and_a_count(self):
        archive(self.dir / "c.tar", data={"LAW.csv": b"rulf\n", "regions/r.json": b'{"r": 2}\n'})
        code = self.check(*run("verify-all", self.dir), ["c.tar | failed | regions/r.json: hash_mismatch (+1 more)"],
                          "archives=1 ok=0 failed=1 skipped=0")
        self.assertEqual(code, 1)

    def test_not_a_tar_and_manifest_not_first_fail_with_the_fail_text(self):
        archive(self.dir / "good.tar")
        (self.dir / "junk.tar").write_bytes(b"not a tar at all\n" * 40)
        archive(self.dir / "late.tar", manifest_first=False)
        code = self.check(*run("verify-all", self.dir), [
            "good.tar | " + OK, "junk.tar | failed | archive is not a readable uncompressed tar",
            "late.tar | failed | manifest.json missing or not the first member"], "archives=3 ok=1 failed=2 skipped=0")
        self.assertEqual(code, 1)

    def test_io_error_fails_one_archive_and_the_pass_continues(self):
        archive(self.dir / "a.tar")
        archive(self.dir / "b.tar")
        real = backup.verify
        def flaky(path, *rest):
            if Path(path).name == "a.tar": raise PermissionError()
            return real(path, *rest)
        with mock.patch.object(backup, "verify", side_effect=flaky):
            code = self.check(*run("verify-all", self.dir), ["a.tar | failed | io error: PermissionError",
                                                            "b.tar | " + OK], "archives=2 ok=1 failed=1 skipped=0")
        self.assertEqual(code, 1)

    def test_link_and_folder_named_tar_are_skipped_and_never_opened(self):
        other = self.tmp / "elsewhere"
        other.mkdir()
        archive(other / "target.tar")
        archive(self.dir / "a.tar")
        os.symlink(other / "target.tar", self.dir / "x.tar")
        (self.dir / "y.tar").mkdir()
        with mock.patch.object(backup, "verify", wraps=backup.verify) as spy:
            code = self.check(*run("verify-all", self.dir),
                              ["a.tar | " + OK, "x.tar | skipped | link", "y.tar | skipped | not-a-file"],
                              "archives=3 ok=1 failed=0 skipped=2")
        self.assertEqual([Path(c.args[0]).name for c in spy.call_args_list], ["a.tar"])
        self.assertEqual(code, 0)

    def test_no_archives_is_exit_1_with_a_message(self):
        for extra in (None, "notes.txt"):
            if extra: (self.dir / extra).write_text("n\n")
            self.assertEqual(run("verify-all", self.dir), (1, "", "verify-all: no *.tar archives in DIR\n"))

    def test_archives_in_subfolders_are_not_searched(self):
        (self.dir / "sub").mkdir()
        archive(self.dir / "sub" / "a.tar")
        self.assertEqual(run("verify-all", self.dir), (1, "", "verify-all: no *.tar archives in DIR\n"))

    def test_dir_missing_a_file_or_a_link_is_refused_without_usage(self):
        archive(self.dir / "a.tar")
        (self.tmp / "file").write_text("f\n")
        os.symlink(self.dir, self.tmp / "link")
        for target in (self.tmp / "missing", self.tmp / "file", self.tmp / "link"):
            self.assertEqual(run("verify-all", target), (1, "", "DIR is not a folder\n"))

    def test_control_character_in_name_is_escaped(self):
        archive(self.dir / "bad\x07.tar")
        code, out, err = run("verify-all", self.dir)
        self.assertNotIn("\x07", out)
        self.check(code, out, err, ["bad\\x07.tar | " + OK], "archives=1 ok=1 failed=0 skipped=0")
        self.assertEqual(code, 0)

    def test_folder_is_left_unchanged(self):
        archive(self.dir / "a.tar")
        archive(self.dir / "b.tar", data={"LAW.csv": b"rulf\n"})
        (self.dir / "y.tar").mkdir()
        before, names = tree(self.dir), sorted(os.listdir(self.dir))
        self.assertEqual(run("verify-all", self.dir)[0], 1)
        self.assertEqual((tree(self.dir), sorted(os.listdir(self.dir))), (before, names))
        self.assertEqual(os.listdir(self.dir / "y.tar"), [])


if __name__ == "__main__":
    unittest.main()
