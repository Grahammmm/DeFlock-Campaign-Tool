"""RF0111-1: kit/assignments.json reads and writes are anchored to one admitted kit directory. Synthetic only.

A parent replacement (kit/ renamed away, a symlink to an outside directory put in its place) is injected at a chosen
syscall with the production code running for real. The outside directory carries a canary owner that must never be
read, shown or overwritten.
"""
import contextlib
import errno
import json
import os
import unittest
from unittest import mock

from campaign_tool import kit_assign
from tests import test_kit_assign as existing

CANARY = "Outside Canary"
LINKED = "kit/ must not be a symlink"
CHANGED = "kit/assignments.json unreadable: file changed or could not be read"
REAL_OPEN, REAL_REPLACE, REAL_FDOPEN = os.open, os.replace, os.fdopen


class ParentScopeTests(unittest.TestCase):
    setUp = existing.KitAssignTests.setUp
    ok = existing.KitAssignTests.ok
    put_sent = existing.KitAssignTests.put_sent
    write_store = existing.KitAssignTests.write_store

    def prepare(self):
        self.put_sent({self.a: existing.imported("2026-03-02"), self.b: existing.imported("2026-03-02")})
        self.write_store({self.a: "Local Owner"})
        self.outside_dir = self.tmp / "outside"
        self.outside_dir.mkdir()
        self.outside = self.outside_dir / "assignments.json"
        self.outside.write_text(json.dumps({"schema_version": 1, "assignments": {self.a: CANARY}}), encoding="utf-8")
        self.outside_snap = self.snap(self.outside_dir)
        self.original = self.tmp / "kit-original"

    @staticmethod
    def snap(directory):
        return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in directory.iterdir() if p.is_file()}

    def swap(self):
        os.rename(self.kit, self.original)
        self.kit.symlink_to(self.outside_dir)

    @contextlib.contextmanager
    def at(self, phase):
        """Run ``swap`` once, at ``phase``: before_kit, after_kit, before_leaf, after_leaf, before_temp,
        before_replace. Real syscalls are forwarded; opened directory descriptors are recorded."""
        rec = {"kit_fds": [], "creates": [], "swapped": False}

        def once(name):
            if name == phase and not rec["swapped"]:
                rec["swapped"] = True
                self.swap()

        def opened(path, flags, *args, **kwargs):
            if path == "kit" and kwargs.get("dir_fd") is not None:
                once("before_kit")
                fd = REAL_OPEN(path, flags, *args, **kwargs)
                rec["kit_fds"].append(fd)
                once("after_kit")
                return fd
            if path == "assignments.json" and kwargs.get("dir_fd") is not None:
                once("before_leaf")
                fd = REAL_OPEN(path, flags, *args, **kwargs)
                once("after_leaf")
                return fd
            if flags & os.O_CREAT:
                rec["creates"].append(path)
                once("before_temp")
            return REAL_OPEN(path, flags, *args, **kwargs)

        def replaced(src, dst, *args, **kwargs):
            once("before_replace")
            return REAL_REPLACE(src, dst, *args, **kwargs)

        with mock.patch.object(os, "open", opened), mock.patch.object(os, "replace", replaced):
            yield rec

    def assert_closed(self, rec):
        self.assertTrue(rec["kit_fds"])
        for fd in rec["kit_fds"]:
            with self.assertRaises(OSError) as caught:
                os.fstat(fd)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_parent_link_before_acquisition_refuses_without_target_io(self):
        self.prepare()
        with self.at("before_kit") as rec, mock.patch.object(kit_assign.json, "loads") as parsed:
            with self.assertRaises(ValueError) as caught:
                kit_assign.read_assignments(self.root)
        self.assertEqual(str(caught.exception), LINKED)
        parsed.assert_not_called()
        self.assertEqual(self.snap(self.outside_dir), self.outside_snap)
        os.unlink(self.kit)
        os.rename(self.original, self.kit)
        for arguments in (("kit-status",), ("kit-due", *existing.ASOF), ("kit-coverage",), ("kit-coverage", "--csv")):
            with self.subTest(arguments=arguments), self.at("before_kit"):
                code, out, err = existing.run(arguments[0], "--directory", self.root, *arguments[1:])
                self.assertEqual((code, out), (1, ""), err)
                self.assertIn(LINKED, err)
                self.assertNotIn(CANARY, err)
            os.unlink(self.kit)
            os.rename(self.original, self.kit)
        self.assertEqual(self.snap(self.outside_dir), self.outside_snap)

    def test_unusable_kit_directories_refuse_with_fixed_messages(self):
        self.prepare()
        for name, make, message in (
                ("dangling", lambda: self.kit.symlink_to(self.tmp / "nowhere"), LINKED),
                ("file", lambda: self.kit.write_bytes(b"x"), CHANGED),
                ("fifo", lambda: os.mkfifo(self.kit), CHANGED)):
            with self.subTest(parent=name):
                os.rename(self.kit, self.original)
                make()
                try:
                    with self.assertRaises(ValueError) as caught:
                        kit_assign.read_assignments(self.root)
                finally:
                    os.unlink(self.kit)
                    os.rename(self.original, self.kit)
                self.assertEqual(str(caught.exception), message)

    def test_missing_kit_is_an_empty_store_and_missing_root_too(self):
        self.prepare()
        os.rename(self.kit, self.original)
        self.assertEqual(kit_assign.read_assignments(self.root), {})
        self.assertEqual(kit_assign.read_assignments(self.tmp / "no-such-campaign"), {})

    def test_swap_after_acquisition_reads_original_directory(self):
        self.prepare()
        for phase in ("after_kit", "after_leaf"):
            with self.subTest(phase=phase):
                with self.at(phase) as rec:
                    self.assertEqual(kit_assign.read_assignments(self.root), {self.a: "Local Owner"})
                self.assertTrue(rec["swapped"])
                self.assert_closed(rec)
                self.assertEqual(self.snap(self.outside_dir), self.outside_snap)
                os.unlink(self.kit)
                os.rename(self.original, self.kit)

    def test_assign_parent_swap_cannot_overwrite_outside_store(self):
        self.prepare()
        for phase in ("after_kit", "before_leaf", "after_leaf", "before_temp", "before_replace"):
            for agency, request, action in ((self.a, "main", ("to", "Replacement")), (self.a, "main", ("clear", True)),
                                            (self.b, "main", ("to", "New Owner"))):
                with self.subTest(phase=phase, agency=agency, action=action):
                    with self.at(phase) as rec:
                        result = kit_assign.assign(self.root, agency, request=request, **dict([action]))
                    self.assertTrue(result["changed"])
                    self.assertTrue(rec["swapped"])
                    self.assert_closed(rec)
                    self.assertEqual(self.snap(self.outside_dir), self.outside_snap)  # bytes, mode, mtime, names
                    stored = json.loads((self.original / "assignments.json").read_text(encoding="utf-8"))
                    self.assertNotIn(CANARY, json.dumps(stored))
                    self.assertEqual(stored["assignments"].get(agency), result["owner"])
                    self.assertEqual(oct((self.original / "assignments.json").stat().st_mode & 0o777), "0o600")
                    self.assertEqual([p.name for p in self.original.iterdir() if p.name.endswith(".tmp")], [])
                    os.unlink(self.kit)
                    os.rename(self.original, self.kit)
                    self.write_store({self.a: "Local Owner"})

    def test_cli_set_with_swap_leaves_outside_store_alone(self):
        self.prepare()
        with self.at("before_temp"):
            code, out, err = existing.run("kit-assign", "--directory", self.root, "--agency", self.a, "--to", "Replacement")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snap(self.outside_dir), self.outside_snap)
        self.assertIn("Replacement", (self.original / "assignments.json").read_text(encoding="utf-8"))

    def test_missing_leaf_is_created_in_the_original_directory(self):
        self.prepare()
        self.store.unlink()
        with self.at("before_temp"):
            kit_assign.assign(self.root, self.a, to="Fresh")
        self.assertEqual(self.snap(self.outside_dir), self.outside_snap)
        self.assertEqual(json.loads((self.original / "assignments.json").read_bytes())["assignments"], {self.a: "Fresh"})

    def test_noop_with_swap_creates_nothing(self):
        self.prepare()
        for agency, action in ((self.a, {"to": "Local Owner"}), (self.b, {"clear": True})):
            with self.subTest(agency=agency):
                before = self.snap(self.kit)
                with self.at("after_kit") as rec, mock.patch.object(os, "replace", side_effect=AssertionError("replace")):
                    result = kit_assign.assign(self.root, agency, **action)
                self.assertFalse(result["changed"])
                self.assertEqual(rec["creates"], [])
                self.assertEqual(self.snap(self.original), before)
                self.assertEqual(self.snap(self.outside_dir), self.outside_snap)
                self.assert_closed(rec)
                os.unlink(self.kit)
                os.rename(self.original, self.kit)

    def test_failure_cleanup_stays_in_original_directory(self):
        self.prepare()
        sentinel = self.outside_dir / ".assignments.json.sentinel.tmp"
        sentinel.write_bytes(b"unrelated")
        self.outside_snap = self.snap(self.outside_dir)
        before = (self.kit / "assignments.json").read_bytes()
        failures = {
            "replace": ("os.replace", OSError(errno.EIO, "replace")),
            "fdopen": ("os.fdopen", OSError(errno.EMFILE, "fdopen")),
        }
        for name, (target, error) in failures.items():
            with self.subTest(failure=name):
                real = REAL_REPLACE if name == "replace" else REAL_FDOPEN

                def fail(*args, **kwargs):
                    if name == "fdopen" and "w" not in str(args[1:] or kwargs.get("mode", "")):
                        return real(*args, **kwargs)
                    raise error
                with self.at("before_temp") as rec, mock.patch(target, fail):
                    with self.assertRaises(OSError) as caught:
                        kit_assign.assign(self.root, self.a, to="Replacement")
                self.assertIs(caught.exception, error)
                self.assert_closed(rec)
                self.assertEqual((self.original / "assignments.json").read_bytes(), before)
                self.assertEqual([p.name for p in self.original.iterdir() if p.name.endswith(".tmp")], [])
                self.assertEqual(self.snap(self.outside_dir), self.outside_snap)
                os.unlink(self.kit)
                os.rename(self.original, self.kit)

    def test_write_failure_after_open_is_cleaned_and_original_error_kept(self):
        self.prepare()
        before = self.store.read_bytes()
        error = OSError(errno.ENOSPC, "write")

        class Failing:
            def __init__(inner, handle):
                inner.handle = handle

            def __enter__(inner):
                return inner

            def __exit__(inner, *exc):
                return inner.handle.__exit__(*exc)

            def write(inner, data):
                raise error

        def fdopened(fd, mode="r", *args, **kwargs):
            handle = REAL_FDOPEN(fd, mode, *args, **kwargs)
            return Failing(handle) if mode == "wb" else handle
        with mock.patch.object(os, "fdopen", fdopened), \
                mock.patch.object(os, "unlink", side_effect=OSError(errno.EACCES, "cleanup")):
            with self.assertRaises(OSError) as caught:
                kit_assign.assign(self.root, self.a, to="Replacement")
        self.assertIs(caught.exception, error)  # a cleanup failure does not replace the primary one
        self.assertEqual(self.store.read_bytes(), before)
        for leftover in self.kit.glob(".*.tmp"):
            leftover.unlink()
        with mock.patch.object(os, "fdopen", fdopened):
            with self.assertRaises(OSError):
                kit_assign.assign(self.root, self.a, to="Replacement")
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(list(self.kit.glob(".*.tmp")), [])

    def test_temporary_collisions_are_retried_and_never_cleaned(self):
        self.prepare()
        names = [f".assignments.json.{'0' * 15}{i}.tmp" for i in range(kit_assign.TEMP_TRIES)]
        for name in names:
            (self.kit / name).write_bytes(b"not ours")
        with mock.patch.object(kit_assign.secrets, "token_hex", side_effect=[n.split(".")[3] for n in names]):
            with self.assertRaisesRegex(ValueError, "^" + CHANGED + "$"):
                kit_assign.assign(self.root, self.a, to="Replacement")
        self.assertTrue(all((self.kit / n).read_bytes() == b"not ours" for n in names))
        self.assertEqual(json.loads(self.store.read_bytes())["assignments"], {self.a: "Local Owner"})
        # One collision, then a free name: succeeds and leaves the unowned file alone.
        with mock.patch.object(kit_assign.secrets, "token_hex", side_effect=[names[0].split(".")[3], "f" * 16]):
            kit_assign.assign(self.root, self.a, to="Replacement")
        self.assertEqual((self.kit / names[0]).read_bytes(), b"not ours")
        self.assertEqual(json.loads(self.store.read_bytes())["assignments"], {self.a: "Replacement"})

    def test_symlinked_campaign_root_keeps_link_semantics(self):
        self.prepare()
        linked = self.tmp / "linked-root"
        linked.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(kit_assign.read_assignments(linked), {self.a: "Local Owner"})
        kit_assign.assign(linked, self.b, to="Via Link")
        self.assertEqual(kit_assign.read_assignments(self.root), {self.a: "Local Owner", self.b: "Via Link"})
        self.assertEqual(self.snap(self.outside_dir), self.outside_snap)
        dangling = self.tmp / "dangling-root"
        dangling.symlink_to(self.tmp / "nowhere")
        self.assertEqual(kit_assign.read_assignments(dangling), {})

    def test_overflow_is_refused_before_any_creation(self):
        self.prepare()
        creates = []

        def opened(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                creates.append(path)
            return REAL_OPEN(path, flags, *args, **kwargs)
        with mock.patch.object(kit_assign, "MAX_BYTES", len(self.store.read_bytes()) + 5), mock.patch.object(os, "open", opened):
            with self.assertRaisesRegex(ValueError, "would be larger than 1 MiB"):
                kit_assign.assign(self.root, self.b, to="Another")
        self.assertEqual(creates, [])

    def test_ordinary_set_clear_and_0600_bytes(self):
        self.prepare()
        kit_assign.assign(self.root, self.b, to="Zoë")
        expected = json.dumps({"schema_version": 1, "assignments": {self.a: "Local Owner", self.b: "Zoë"}},
                              indent=2, ensure_ascii=False) + "\n"
        self.assertEqual(self.store.read_bytes(), expected.encode("utf-8"))
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        kit_assign.assign(self.root, self.b, clear=True)
        self.assertEqual(kit_assign.read_assignments(self.root), {self.a: "Local Owner"})
        self.assertEqual(list(self.kit.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
