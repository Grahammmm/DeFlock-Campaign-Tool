"""sync_campaign_resources: per-file states, named exit-2 refusals and atomic --write."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import sync_campaign_resources as tool
from tools.sync_campaign_resources import RESOURCES, main

ROOT = Path(__file__).resolve().parents[1]


def run(root, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv), root=root)
    return code, out.getvalue(), err.getvalue()


class SyncStatesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.bundle = self.root / "campaign_tool" / "_resources"
        for name in RESOURCES:
            for base in (self.root, self.bundle):
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, path)
                os.chmod(path, 0o644)

    def target(self, name):
        return self.bundle / name

    def snapshot(self):
        return {name: self.target(name).read_bytes() if self.target(name).is_file() else None for name in RESOURCES}

    def leftovers(self):
        return [p for p in self.root.rglob(".sync-*")]

    def assert_stopped(self, code, out, err, name):
        self.assertEqual(code, 2, out + err)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("Resource check stopped: "), err)
        self.assertIn(name, err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn(str(self.root), err)

    def test_real_repository_tree_is_clean(self):
        before = {name: (ROOT / "campaign_tool" / "_resources" / name).stat().st_mtime_ns for name in RESOURCES}
        code, out, err = run(None)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["ok " + name for name in RESOURCES]
                         + ["resources=5 ok=5 stale=0 missing-target=0"])
        after = {name: (ROOT / "campaign_tool" / "_resources" / name).stat().st_mtime_ns for name in RESOURCES}
        self.assertEqual(before, after)

    def test_stale_and_missing_target_are_named(self):
        self.target(RESOURCES[1]).write_bytes(b"edited copy")
        self.target(RESOURCES[3]).unlink()
        before = self.snapshot()
        code, out, err = run(self.root)
        self.assertEqual((code, err), (1, ""))
        lines = out.splitlines()
        self.assertIn("stale " + RESOURCES[1], lines)
        self.assertIn("missing-target " + RESOURCES[3], lines)
        self.assertEqual(lines.count("ok " + RESOURCES[0]), 1)
        self.assertEqual(lines[-1], "resources=5 ok=3 stale=1 missing-target=1")
        self.assertEqual(self.snapshot(), before, "check mode must not write")

    def test_single_stale_only(self):
        self.target(RESOURCES[4]).write_bytes(self.target(RESOURCES[4]).read_bytes() + b"\n")
        code, out, _ = run(self.root)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[-1], "resources=5 ok=4 stale=1 missing-target=0")
        self.assertIn("stale " + RESOURCES[4], out.splitlines())

    def test_write_repairs_stale_and_missing(self):
        self.target(RESOURCES[1]).write_bytes(b"edited copy")
        self.target(RESOURCES[3]).unlink()
        code, out, err = run(self.root, "--write")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-1], "resources=5 ok=5 stale=0 missing-target=0")
        for name in RESOURCES:
            self.assertEqual(self.target(name).read_bytes(), (self.root / name).read_bytes())
        self.assertEqual(self.leftovers(), [])

    def test_missing_source_is_named_exit_two(self):
        for write in ((), ("--write",)):
            with self.subTest(write=write):
                self.target(RESOURCES[0]).write_bytes(b"stale copy")
                source = self.root / RESOURCES[-1]
                saved = source.read_bytes()
                source.unlink()
                before = self.snapshot()
                self.assert_stopped(*run(self.root, *write), RESOURCES[-1])
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.leftovers(), [])
                source.write_bytes(saved)

    def test_unreadable_source_is_named_exit_two(self):
        source = self.root / RESOURCES[2]
        source.unlink()
        source.mkdir()
        before = self.snapshot()
        self.assert_stopped(*run(self.root, "--write"), RESOURCES[2])
        self.assertEqual(self.snapshot(), before)

    def test_symlinked_bundle_dir_stops_without_writing(self):
        real = self.root / "real-bundle"
        self.bundle.rename(real)
        self.bundle.symlink_to(real, target_is_directory=True)
        (self.root / RESOURCES[0]).write_bytes(b"changed canonical")
        before = (real / RESOURCES[0]).read_bytes()
        code, out, err = run(self.root, "--write")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "Resource check stopped: symlink resource directory\n")
        self.assertEqual((real / RESOURCES[0]).read_bytes(), before)

    def test_hard_linked_target_stops_without_writing(self):
        (self.root / "outside.txt").hardlink_to(self.target(RESOURCES[3]))
        (self.root / RESOURCES[0]).write_bytes(b"changed canonical")
        before = self.snapshot()
        self.assert_stopped(*run(self.root, "--write"), RESOURCES[3])
        self.assertEqual(self.snapshot(), before)

    def test_unexpected_entry_keeps_failure_line_and_writes_nothing(self):
        (self.bundle / "extra.bin").write_bytes(b"unapproved")
        (self.root / RESOURCES[0]).write_bytes(b"changed canonical")
        before = self.snapshot()
        for write in ((), ("--write",)):
            with self.subTest(write=write):
                code, out, err = run(self.root, *write)
                self.assertEqual((code, err), (1, ""))
                self.assertEqual(out, "Resource check failed: unexpected resource: extra.bin\n")
                self.assertEqual(self.snapshot(), before)

    def test_replace_failure_on_third_file_stops_cleanly(self):
        for name in RESOURCES:
            self.target(name).write_bytes(b"stale " + name.encode())
        real_replace = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 3:
                raise OSError("synthetic replace failure")
            return real_replace(src, dst)

        with mock.patch.object(tool.os, "replace", side_effect=flaky):
            self.assert_stopped(*run(self.root, "--write"), RESOURCES[2])
        self.assertEqual(len(calls), 3)
        for name in RESOURCES[:2]:
            self.assertEqual(self.target(name).read_bytes(), (self.root / name).read_bytes())
        for name in RESOURCES[2:]:
            self.assertEqual(self.target(name).read_bytes(), b"stale " + name.encode())
        self.assertEqual(self.leftovers(), [])

    def test_fsync_failure_removes_temp_and_keeps_target(self):
        self.target(RESOURCES[0]).write_bytes(b"stale copy")
        with mock.patch.object(tool.os, "fsync", side_effect=OSError(5, "Input/output error")):
            self.assert_stopped(*run(self.root, "--write"), RESOURCES[0])
        self.assertEqual(self.target(RESOURCES[0]).read_bytes(), b"stale copy")
        self.assertEqual(self.leftovers(), [])

    def test_write_uses_source_mode_not_mkstemp_0600(self):
        for mode in (0o644, 0o640):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.root / RESOURCES[0], mode)
                self.target(RESOURCES[0]).write_bytes(b"stale copy")
                os.chmod(self.target(RESOURCES[0]), 0o600)
                code, _, _ = run(self.root, "--write")
                self.assertEqual(code, 0)
                self.assertEqual(self.target(RESOURCES[0]).stat().st_mode & 0o7777, mode)

    def test_write_on_clean_tree_rewrites_nothing(self):
        old = 1_000_000_000_000_000_000
        for name in RESOURCES:
            os.utime(self.target(name), ns=(old, old))
        inodes = {name: self.target(name).stat().st_ino for name in RESOURCES}
        with mock.patch.object(tool.os, "replace", side_effect=AssertionError("rewrote a clean target")):
            code, out, err = run(self.root, "--write")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["ok " + name for name in RESOURCES]
                         + ["resources=5 ok=5 stale=0 missing-target=0"])
        for name in RESOURCES:
            self.assertEqual(self.target(name).stat().st_mtime_ns, old)
            self.assertEqual(self.target(name).stat().st_ino, inodes[name])


if __name__ == "__main__":
    unittest.main()
