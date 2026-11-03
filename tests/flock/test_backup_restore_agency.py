"""RF0310-10: tools/flock_backup.py restore FILE --into DIR --agency SLUG restores one agency (synthetic stores only)."""
import hashlib
import importlib.util
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("backup_store_fixtures", HERE / "test_backup_store.py")
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)  # reuse run / tree / write_tar / manifest_bytes and the loaded tool
backup, run, tree = base.backup, base.run, base.tree
STORE_FILES = {"agencies/a-one/FACTS.csv": b"agency,fact\na-one,1\n", "agencies/a-one/runs/2026-11-01/RUN.json": b"{}\n",
               "agencies/b-two/FACTS.csv": b"agency,fact\nb-two,2\n", "regions/central-coast.json": b'{"r": 1}\n',
               "regions/central-coast/WATCHLIST.csv": b"w\n", "LAW.csv": b"rule,text\n"}
A_ONE = {n: d for n, d in STORE_FILES.items() if n.startswith("agencies/a-one/")}


class RestoreAgencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store, self.out, self.into = self.tmp / "store", self.tmp / "out" / "store.tar", self.tmp / "restored"
        self.out.parent.mkdir()
        for name, data in STORE_FILES.items():
            (self.store / name).parent.mkdir(parents=True, exist_ok=True)
            (self.store / name).write_bytes(data)
        code, _, err = run("create", self.store, "--out", self.out)
        self.assertEqual((code, err), (0, ""))

    def assert_private_modes(self, root):
        for path in [root, *root.rglob("*")]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600, path)

    def test_only_the_chosen_agency_is_extracted_private_and_matching_the_manifest(self):
        code, out, err = run("restore", self.out, "--into", self.into, "--agency", "a-one")
        self.assertEqual((code, out, err),
                         (0, "restored agency=a-one files=2 bytes=%d\n" % sum(map(len, A_ONE.values())), ""))
        self.assertEqual(tree(self.into), A_ONE)
        self.assertEqual(sorted(p.name for p in self.into.iterdir()), ["agencies"])
        self.assertEqual(sorted(p.name for p in (self.into / "agencies").iterdir()), ["a-one"])
        self.assertFalse((self.into / "regions").exists() or (self.into / "LAW.csv").exists())
        self.assert_private_modes(self.into)
        with tarfile.open(self.out) as tar:
            manifest = base.json.loads(tar.extractfile("manifest.json").read())
        for name, data in tree(self.into).items():
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest["files"][name]["sha256"], name)

    def test_returned_counts_cover_only_the_agency(self):
        self.assertEqual(backup.restore(self.out, self.into, agency="b-two"),
                         {"files": 1, "bytes": len(STORE_FILES["agencies/b-two/FACTS.csv"])})
        self.assertEqual(tree(self.into), {"agencies/b-two/FACTS.csv": STORE_FILES["agencies/b-two/FACTS.csv"]})

    def test_on_disk_rehash_names_a_bad_agency_file(self):
        real_copy = backup.shutil.copyfileobj

        def corrupt(source, target):  # the bytes written differ from the verified member
            real_copy(source, target)
            target.write(b"!")

        with mock.patch.object(backup.shutil, "copyfileobj", side_effect=corrupt):
            code, out, err = run("restore", self.out, "--into", self.into, "--agency", "a-one")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "restored files do not match: agencies/a-one/FACTS.csv: hash_mismatch_on_disk; "
                              "agencies/a-one/runs/2026-11-01/RUN.json: hash_mismatch_on_disk\n")

    def test_slug_prefix_includes_the_trailing_slash(self):
        for slug in ("b", "a", "a-on", "agencies"):
            with self.subTest(slug):
                self.assertEqual(run("restore", self.out, "--into", self.into, "--agency", slug),
                                 (1, "", "agency not in archive\n"))
                self.assertFalse(os.path.lexists(self.into))

    def test_unknown_agency_refused_and_into_left_as_it_was(self):
        self.assertEqual(run("restore", self.out, "--into", self.into, "--agency", "zz-nowhere"),
                         (1, "", "agency not in archive\n"))
        self.assertFalse(os.path.lexists(self.into))
        self.into.mkdir(mode=0o755)
        os.chmod(self.into, 0o755)
        self.assertEqual(run("restore", self.out, "--into", self.into, "--agency", "zz-nowhere"),
                         (1, "", "agency not in archive\n"))
        self.assertEqual((os.listdir(self.into), stat.S_IMODE(self.into.stat().st_mode)), ([], 0o755))

    def test_bad_slug_refused_before_into_checks_and_before_the_archive_is_opened(self):
        self.into.mkdir()
        (self.into / "LAW.csv").write_bytes(b"existing\n")
        bad = ("../x", "A-One", "", "a" * 61, "a-one/", "a-one\n", "a_one", "agencies/a-one", ".", "a one")
        for slug in bad:
            with self.subTest(slug=slug), mock.patch.object(backup.tarfile, "open") as opened:
                self.assertEqual(run("restore", self.out, "--into", self.into, "--agency", slug),
                                 (1, "", "--agency is not a slug\n"))
                self.assertEqual(run("restore", self.out, "--into", self.tmp / "absent", "--agency", slug),
                                 (1, "", "--agency is not a slug\n"))
                opened.assert_not_called()
        self.assertEqual(tree(self.into), {"LAW.csv": b"existing\n"})
        self.assertFalse(os.path.lexists(self.tmp / "absent"))
        self.assertEqual(run("restore", self.out, "--into", self.tmp / "sixty", "--agency", "a" * 60),
                         (1, "", "agency not in archive\n"))  # 60 characters is a slug; the archive is read

    def test_flipped_byte_in_another_agency_refuses_the_whole_restore(self):
        with tarfile.open(self.out) as tar:
            offset = tar.getmember("agencies/b-two/FACTS.csv").offset_data
        data = bytearray(self.out.read_bytes())
        data[offset] ^= 1
        flipped = self.tmp / "flipped.tar"
        flipped.write_bytes(bytes(data))
        self.assertEqual(run("restore", flipped, "--into", self.into, "--agency", "a-one"),
                         (1, "", "archive refused, nothing extracted: agencies/b-two/FACTS.csv: hash_mismatch\n"))
        self.assertFalse(os.path.lexists(self.into))

    def test_extra_member_outside_the_agency_still_refuses(self):
        files = dict(A_ONE)
        archive = self.tmp / "extra.tar"
        base.write_tar(archive, [(tarfile.TarInfo("manifest.json"), base.manifest_bytes(files)),
                                 *[(tarfile.TarInfo(n), d) for n, d in files.items()],
                                 (tarfile.TarInfo("agencies/b-two/EXTRA.csv"), b"x\n")])
        self.assertEqual(run("restore", archive, "--into", self.into, "--agency", "a-one"),
                         (1, "", "archive refused, nothing extracted: agencies/b-two/EXTRA.csv: not_in_manifest\n"))
        self.assertFalse(os.path.lexists(self.into))

    def test_non_empty_symlinked_or_repo_into_keep_existing_refusals(self):
        self.into.mkdir()
        (self.into / "keep.txt").write_bytes(b"k\n")
        self.assertEqual(run("restore", self.out, "--into", self.into, "--agency", "a-one"),
                         (1, "", "--into is not an empty folder\n"))
        self.assertEqual(tree(self.into), {"keep.txt": b"k\n"})
        (self.tmp / "empty-target").mkdir()
        os.symlink(self.tmp / "empty-target", self.tmp / "link-into")
        self.assertEqual(run("restore", self.out, "--into", self.tmp / "link-into", "--agency", "a-one"),
                         (1, "", "--into is a symlink\n"))
        self.assertEqual(os.listdir(self.tmp / "empty-target"), [])
        self.assertEqual(run("restore", self.out, "--into", base.ROOT / "restored-agency", "--agency", "a-one"),
                         (1, "", "--into inside repository\n"))
        self.assertFalse((base.ROOT / "restored-agency").exists())

    def test_full_restore_without_agency_is_unchanged(self):
        code, out, err = run("restore", self.out, "--into", self.into)
        self.assertEqual((code, out, err), (0, "restored files=6 bytes=%d\n" % sum(map(len, STORE_FILES.values())), ""))
        self.assertEqual(tree(self.into), STORE_FILES)
        self.assert_private_modes(self.into)
        with tarfile.open(self.out) as tar:
            counts = base.json.loads(tar.extractfile("manifest.json").read())["counts"]
        self.assertEqual(backup.restore(self.out, self.tmp / "again"), counts)


if __name__ == "__main__":
    unittest.main()
