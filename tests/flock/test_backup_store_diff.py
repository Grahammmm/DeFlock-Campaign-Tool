"""DF2022-16: tools/flock_backup.py diff ARCHIVE --store DIR names store drift since a backup (synthetic stores only)."""
import contextlib
import importlib.util
import io
import json
import os
import re
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("flock_backup", ROOT / "tools" / "flock_backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
STORE_FILES = {"agencies/slo-pd/FACTS.csv": b"agency,fact\nslo-pd,1\n", "agencies/slo-pd/POLICY.csv": b"p\n",
               "regions/r.json": b'{"r": 1}\n', "regions/r/WATCHLIST.csv": b"w\n", "LAW.csv": b"rule,text\n"}
SKIPPED_NONE = {"skipped_links": 0, "skipped_hidden": 0, "skipped_other": 0}
HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = backup.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def snapshot(root):
    return {p.relative_to(root).as_posix(): (os.lstat(p).st_mtime_ns, None if p.is_dir() or p.is_symlink()
                                             else p.read_bytes()) for p in sorted(Path(root).rglob("*"))}


class StoreDiffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store, self.out = self.tmp / "store", self.tmp / "out" / "store.tar"
        self.out.parent.mkdir()
        for name, data in STORE_FILES.items():
            self.put(name, data)
        code, out, err = run("create", self.store, "--out", self.out)
        self.assertEqual((code, err), (0, ""))
        self.digest = out.split("manifest_sha256=")[1].strip()

    def put(self, name, data):
        (self.store / name).parent.mkdir(parents=True, exist_ok=True)
        (self.store / name).write_bytes(data)

    def diff(self, *extra, code=3):
        got, out, err = run("diff", self.out, "--store", self.store, *extra)
        self.assertEqual((got, err), (code, ""))
        self.assertTrue(out.endswith("}\n"))
        return json.loads(out), out

    def assert_refused(self, archive, message, *extra, store=None):
        self.assertEqual(run("diff", archive, "--store", store or self.store, *extra), (1, "", message + "\n"))

    def test_no_change_is_current_exit_0(self):
        report, out = self.diff(code=0)
        self.assertEqual(report, {"archive_created_at": report["archive_created_at"], "current": True, "added": [],
                                  "removed": [], "changed": [], "unchanged": 5, "store_skipped": SKIPPED_NONE})
        self.assertRegex(report["archive_created_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(out, json.dumps(report, indent=2, sort_keys=True) + "\n")
        self.assertEqual(backup.diff(self.out, self.store, self.digest.upper()), report)

    def test_added_removed_changed_named_without_hashes_or_content_exit_3(self):
        self.put("agencies/x/new.json", b"NEW-SECRET-CONTENT\n")
        (self.store / "LAW.csv").unlink()
        self.put("regions/r.json", b'{"r": 2, "edited": "EDITED-CONTENT"}\n')
        report, out = self.diff()
        self.assertEqual((report["current"], report["added"], report["removed"], report["changed"], report["unchanged"]),
                         (False, ["agencies/x/new.json"], ["LAW.csv"], ["regions/r.json"], 3))
        self.assertNotIn("more", report)
        self.assertIsNone(HEX64.search(out))
        for content in (b"NEW-SECRET-CONTENT", b"EDITED-CONTENT", b"rule,text", b"agency,fact"):
            self.assertNotIn(content.decode(), out)
        self.assertNotIn("bytes", out)

    def test_same_size_one_byte_edit_is_changed(self):
        self.put("agencies/slo-pd/POLICY.csv", b"q\n")
        report, _ = self.diff()
        self.assertEqual((report["changed"], report["added"], report["removed"], report["unchanged"]),
                         (["agencies/slo-pd/POLICY.csv"], [], [], 4))

    def test_links_hidden_and_other_entries_skipped_as_create_skips_them(self):
        (self.tmp / "outside.csv").write_bytes(b"never compared\n")
        os.symlink(self.tmp / "outside.csv", self.store / "agencies" / "slo-pd" / "LINKED.csv")
        os.symlink(self.tmp, self.store / "agencies" / "linked-agency")
        self.put("regions/.WATCHLIST.csv.x.tmp", b"partial\n")
        self.put("notes.txt", b"root file\n")
        report, _ = self.diff(code=0)
        self.assertEqual((report["current"], report["added"], report["unchanged"]), (True, [], 5))
        self.assertEqual(report["store_skipped"], {"skipped_links": 2, "skipped_hidden": 1, "skipped_other": 1})
        (self.store / "LAW.csv").unlink()  # an archived file replaced by a link is no longer in the store
        os.symlink(self.tmp / "outside.csv", self.store / "LAW.csv")
        report, _ = self.diff()
        self.assertEqual((report["removed"], report["added"], report["store_skipped"]["skipped_links"]), (["LAW.csv"], [], 3))

    def test_corrupt_archive_or_digest_mismatch_refused_with_no_diff(self):
        self.put("agencies/x/new.json", b"{}\n")  # drift exists, but no diff may be printed against a bad archive
        with tarfile.open(self.out) as tar:
            offset = tar.getmember("LAW.csv").offset_data
        data = bytearray(self.out.read_bytes())
        data[offset] ^= 1
        flipped = self.tmp / "flipped.tar"
        flipped.write_bytes(bytes(data))
        self.assert_refused(flipped, "archive refused: LAW.csv: hash_mismatch")
        not_tar = self.tmp / "not.tar"
        not_tar.write_bytes(b"not a tar at all\n" * 64)
        self.assert_refused(not_tar, "archive is not a readable uncompressed tar")
        self.assert_refused(self.tmp / "absent.tar", "archive is not a readable uncompressed tar")
        wrong = "f" * 64 if self.digest != "f" * 64 else "e" * 64
        self.assert_refused(self.out, "archive refused: manifest.json: manifest_digest_mismatch", "--manifest-sha256", wrong)
        report, _ = self.diff("--manifest-sha256", " %s\n" % self.digest.upper())
        self.assertEqual(report["added"], ["agencies/x/new.json"])

    def test_store_inside_repo_missing_or_a_file_refused(self):
        self.assert_refused(self.out, "store inside repository", store=ROOT / "tests")
        self.assert_refused(self.out, "store inside repository", store=ROOT)
        self.assert_refused(self.out, "store is not a folder", store=self.tmp / "missing-store")
        self.assert_refused(self.out, "store is not a folder", store=self.store / "LAW.csv")

    def test_lists_capped_at_200_with_more_counts(self):
        for index in range(200):
            self.put("agencies/bulk/f%03d.json" % index, b"{}\n")
        report, _ = self.diff()
        self.assertEqual((len(report["added"]), "more" in report), (200, False))
        for index in range(200, 205):
            self.put("agencies/bulk/f%03d.json" % index, b"{}\n")
        (self.store / "LAW.csv").unlink()
        report, _ = self.diff()
        self.assertEqual(report["added"], ["agencies/bulk/f%03d.json" % i for i in range(200)])
        self.assertEqual((report["more"], report["removed"]), ({"added": 5, "removed": 0, "changed": 0}, ["LAW.csv"]))

    def test_control_character_name_printed_escaped(self):
        self.put("agencies/x/a\x07b\nc.json", b"{}\n")
        report, out = self.diff()
        self.assertEqual(report["added"], ["agencies/x/a\\x07b\\nc.json"])
        self.assertNotIn("\\u0007", out)

    def test_store_and_archive_untouched_by_diff(self):
        self.put("agencies/x/new.json", b"{}\n")
        self.put("LAW.csv", b"rule,text,changed\n")
        os.symlink(self.tmp / "nowhere", self.store / "regions" / "dangling")
        before = (snapshot(self.store), snapshot(self.out.parent), os.lstat(self.store).st_mtime_ns)
        self.diff()
        self.assertEqual(run("diff", self.out, "--store", self.store, "--manifest-sha256", "0" * 64)[0], 1)
        self.assertEqual((snapshot(self.store), snapshot(self.out.parent), os.lstat(self.store).st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()
