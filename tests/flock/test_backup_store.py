"""NB-DF-16: tools/flock_backup.py create / verify / restore of a private flock store (synthetic stores only)."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("flock_backup", ROOT / "tools" / "flock_backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
STORE_FILES = {"agencies/slo-pd/FACTS.csv": b"agency,fact\r\nslo-pd,1\r\n", "agencies/slo-pd/POLICY.csv": b"p\n",
               "agencies/slo-pd/runs/2026-10-30/RUN.json": b"{}\n", "regions/central-coast.json": b'{"r": 1}\n',
               "regions/central-coast/WATCHLIST.csv": b"w\n", "regions/central-coast/PLAN.md": b"# plan\n",
               "LAW.csv": b"rule,text\n"}


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = backup.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


def write_tar(path, members):  # [(TarInfo, bytes or None)], manifest first when given
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in members:
            if data is not None: info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data is not None else None)


def manifest_bytes(files):
    entries = {n: {"sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d)} for n, d in files.items()}
    manifest = {"schema_version": 1, "kind": "flock-store", "created_at": "2026-10-31T00:00:00+00:00", "files": entries,
                "counts": {"files": len(entries), "bytes": sum(len(d) for d in files.values()),
                           "skipped_links": 0, "skipped_hidden": 0, "skipped_other": 0}}
    return json.dumps(manifest).encode()


class StoreBackupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store, self.out, self.into = self.tmp / "store", self.tmp / "out" / "store.tar", self.tmp / "restored"
        self.out.parent.mkdir()
        for name, data in STORE_FILES.items():
            (self.store / name).parent.mkdir(parents=True, exist_ok=True)
            (self.store / name).write_bytes(data)

    def create(self):
        code, out, err = run("create", self.store, "--out", self.out)
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"^created files=\d+ bytes=\d+ manifest_sha256=[0-9a-f]{64}\n$")
        return out.split("manifest_sha256=")[1].strip()

    def manifest(self, path=None):
        with tarfile.open(path or self.out) as tar:
            self.assertEqual(tar.getmembers()[0].name, "manifest.json")
            return json.loads(tar.extractfile("manifest.json").read())

    def assert_refused_restore(self, archive):
        code, out, err = run("restore", archive, "--into", self.into)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("nothing extracted", err)
        self.assertFalse(os.path.lexists(self.into))
        return err

    def test_round_trip_verifies_with_manifest_sha_and_restores_private_modes(self):
        digest = self.create()
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        self.assertEqual(sorted(self.manifest()["files"]), sorted(STORE_FILES))
        with tarfile.open(self.out) as tar:
            self.assertTrue(all(m.mode == 0o600 and m.uid == m.gid == 0 and not m.uname for m in tar.getmembers()))
        self.assertEqual(run("verify", self.out, "--manifest-sha256", digest.upper()),
                         (0, "verified files=7 bytes=%d\n" % sum(map(len, STORE_FILES.values())), ""))
        code, out, err = run("restore", self.out, "--into", self.into)
        self.assertEqual((code, out, err), (0, "restored files=7 bytes=%d\n" % sum(map(len, STORE_FILES.values())), ""))
        self.assertEqual(tree(self.into), tree(self.store))
        for path in [self.into, *self.into.rglob("*")]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600, path)

    def test_links_hidden_files_and_other_root_entries_are_skipped_and_counted(self):
        outside = self.tmp / "outside"
        (outside / "secret").mkdir(parents=True)
        (outside / "secret" / "FACTS.csv").write_bytes(b"never archived\n")
        os.symlink(outside / "secret", self.store / "agencies" / "linked-agency")
        os.symlink(outside / "secret" / "FACTS.csv", self.store / "agencies" / "slo-pd" / "LINKED.csv")
        (self.store / ".monitor.lock").write_bytes(b"")
        (self.store / "regions" / ".WATCHLIST.csv.x.tmp").write_bytes(b"partial\n")
        (self.store / "notes.txt").write_bytes(b"root file\n")
        self.create()
        manifest = self.manifest()
        self.assertEqual(sorted(manifest["files"]), sorted(STORE_FILES))
        self.assertEqual(manifest["counts"], {"files": 7, "bytes": sum(map(len, STORE_FILES.values())),
                                              "skipped_links": 2, "skipped_hidden": 2, "skipped_other": 1})
        with tarfile.open(self.out) as tar:
            self.assertNotIn(b"never archived", b"".join(tar.extractfile(m).read() for m in tar.getmembers()))

    def test_flipped_byte_named_and_consistent_manifest_rewrite_caught_only_with_digest(self):
        digest = self.create()
        with tarfile.open(self.out) as tar:
            offset = tar.getmember("LAW.csv").offset_data
        data = bytearray(self.out.read_bytes())
        data[offset] ^= 1
        flipped = self.tmp / "flipped.tar"
        flipped.write_bytes(bytes(data))
        self.assertEqual(run("verify", flipped), (1, "", "LAW.csv: hash_mismatch\n"))
        self.assert_refused_restore(flipped)
        files = dict(STORE_FILES, **{"LAW.csv": b"rule,text\nrewritten\n"})
        rewritten = self.tmp / "rewritten.tar"
        write_tar(rewritten, [(tarfile.TarInfo(n), d) for n, d in [("manifest.json", manifest_bytes(files)), *files.items()]])
        self.assertEqual(run("verify", rewritten)[0], 0)
        self.assertEqual(run("verify", rewritten, "--manifest-sha256", digest),
                         (1, "", "manifest.json: manifest_digest_mismatch\n"))

    def test_unsafe_members_refused_by_verify_and_restore_with_nothing_extracted(self):
        link, device = tarfile.TarInfo("agencies/x/link"), tarfile.TarInfo("agencies/x/dev")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        device.type, device.devmajor, device.devminor = tarfile.CHRTYPE, 1, 3
        cases = {"../x": (tarfile.TarInfo("../x"), b"x"), "/abs": (tarfile.TarInfo("/abs/LAW.csv"), b"x"),
                 "link": (link, None), "device": (device, None)}
        expected = {"../x": "../x: unsafe_member_name", "/abs": "/abs/LAW.csv: unsafe_member_name",
                    "link": "agencies/x/link: unsafe_member_type", "device": "agencies/x/dev: unsafe_member_type"}
        for index, (label, member) in enumerate(cases.items()):
            with self.subTest(label):
                archive = self.tmp / ("bad-%d.tar" % index)
                write_tar(archive, [(tarfile.TarInfo("manifest.json"), manifest_bytes({"LAW.csv": b"r\n"})),
                                    (tarfile.TarInfo("LAW.csv"), b"r\n"), member])
                self.assertEqual(run("verify", archive), (1, "", expected[label] + "\n"))
                self.assertIn(expected[label], self.assert_refused_restore(archive))
                self.assertFalse((self.tmp / "x").exists() or (self.tmp / "abs").exists())

    def test_missing_extra_and_duplicate_members_named(self):
        files = {"LAW.csv": b"r\n", "regions/a.json": b"{}\n"}
        archive = self.tmp / "odd.tar"
        write_tar(archive, [(tarfile.TarInfo("manifest.json"), manifest_bytes(files)), (tarfile.TarInfo("LAW.csv"), b"r\n"),
                            (tarfile.TarInfo("LAW.csv"), b"r\n"), (tarfile.TarInfo("regions/extra.json"), b"{}\n")])
        self.assertEqual(run("verify", archive), (1, "", "LAW.csv: duplicate_member\nregions/extra.json: not_in_manifest\n"
                                                         "regions/a.json: missing_member\n"))
        self.assert_refused_restore(archive)
        unsafe_manifest = self.tmp / "unsafe-manifest.tar"
        write_tar(unsafe_manifest, [(tarfile.TarInfo("manifest.json"), manifest_bytes({"secrets/key.txt": b"k"}))])
        self.assertEqual(run("verify", unsafe_manifest), (1, "", "manifest.json: unsafe_member_name secrets/key.txt\n"))

    def test_out_existing_inside_store_or_repo_refused(self):
        self.out.write_bytes(b"older backup\n")
        self.assertEqual(run("create", self.store, "--out", self.out), (1, "", "--out exists; refusing to overwrite\n"))
        self.assertEqual(self.out.read_bytes(), b"older backup\n")
        for out in (self.store / "store.tar", self.store / "agencies" / "b.tar", ROOT / "flock-store-backup.tar"):
            with self.subTest(out=out):
                self.assertEqual(run("create", self.store, "--out", out),
                                 (1, "", "--out inside the store or the repository\n"))
                self.assertFalse(os.path.lexists(out))
        self.assertEqual(run("create", ROOT / "tests", "--out", self.tmp / "repo.tar"), (1, "", "store inside repository\n"))
        self.assertFalse((self.tmp / "repo.tar").exists())

    def test_injected_write_failures_leave_no_out_and_no_temp_file(self):
        self.out.parent.joinpath("keep.txt").write_bytes(b"k")
        real_hash, calls = backup._hash, []

        def stale_hash(handle):  # first file "changes" between hashing and archiving
            calls.append(1)
            return "0" * 64 if len(calls) == 1 else real_hash(handle)

        failures = {"link": mock.patch.object(backup.os, "link", side_effect=OSError("disk full")),
                    "addfile": mock.patch.object(tarfile.TarFile, "addfile", side_effect=OSError("disk full")),
                    "stale": mock.patch.object(backup, "_hash", side_effect=stale_hash)}
        for label, patch in failures.items():
            with self.subTest(label), patch:
                code, out, err = run("create", self.store, "--out", self.out)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("store changed while archiving" if label == "stale" else "io error: OSError", err)
                self.assertEqual(sorted(os.listdir(self.out.parent)), ["keep.txt"])

    def test_restore_into_non_empty_symlink_or_repo_refused_and_untouched(self):
        self.create()
        self.into.mkdir()
        (self.into / "LAW.csv").write_bytes(b"existing\n")
        self.assertEqual(run("restore", self.out, "--into", self.into), (1, "", "--into is not an empty folder\n"))
        self.assertEqual(tree(self.into), {"LAW.csv": b"existing\n"})
        os.symlink(self.tmp / "empty-target", self.tmp / "link-into")
        (self.tmp / "empty-target").mkdir()
        self.assertEqual(run("restore", self.out, "--into", self.tmp / "link-into"), (1, "", "--into is a symlink\n"))
        self.assertEqual(os.listdir(self.tmp / "empty-target"), [])
        self.assertEqual(run("restore", self.out, "--into", ROOT / "restored-store"), (1, "", "--into inside repository\n"))
        self.assertFalse((ROOT / "restored-store").exists())

    def test_empty_store_round_trips_with_zero_files(self):
        empty = self.tmp / "empty-store"
        empty.mkdir()
        code, out, _ = run("create", empty, "--out", self.out)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("created files=0 bytes=0 manifest_sha256="))
        digest = out.split("=")[-1].strip()
        self.assertEqual(run("verify", self.out, "--manifest-sha256", digest), (0, "verified files=0 bytes=0\n", ""))
        self.assertEqual(run("restore", self.out, "--into", self.into), (0, "restored files=0 bytes=0\n", ""))
        self.assertEqual(os.listdir(self.into), [])
        self.assertEqual(stat.S_IMODE(self.into.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
