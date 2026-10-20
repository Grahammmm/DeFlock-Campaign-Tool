"""#137: secure_open and PrivateDir inside a workspace capability that refuses a read open of a parent directory.

Where the platform has O_NOFOLLOW_ANY (macOS 11+), both use one atomic open that the kernel refuses if any path
component is a symlink, and never open an ancestor. Elsewhere the original descriptor walk is used, unchanged; the
choice is made at import, never after a refusal.

Two kinds of case. The policy cases use a simulated kernel: a guarded os.open that honours a stand-in flag by refusing
(ELOOP) when any component of the path is a symlink at the moment of the call, and that refuses (EPERM) a directory
open outside the synthetic workspace or relative to a descriptor, as the recorded qualification denied `Documents`.
The kernel case, run only where os.O_NOFOLLOW_ANY exists, checks the real flag against the reviewer's schedule.
"""
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import container_host as host
from campaign_tool.records.intake import folder

FLAG = getattr(os, "O_NOFOLLOW_ANY", 0) or 0x20000000   # the stand-in is only ever seen by the simulated kernel


class WorkspaceCapabilityTests(unittest.TestCase):
    def setUp(self):
        # As HostFixture: PrivateDir requires trusted (not group- or world-writable) ancestors, so not /tmp; and
        # macOS /var is a symlink, which both secure_open and PrivateDir intentionally reject, so resolve it.
        parent = os.environ.get("TMPDIR", str(Path.home()))
        if parent in ("/tmp", "/var/tmp"):
            parent = str(Path.home())
        temp = tempfile.TemporaryDirectory(prefix="secure-open-capability-synthetic-", dir=parent)
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name).resolve() / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.workspace.chmod(0o700)
        self.exports = self.workspace / "exports"
        (self.exports / "objects").mkdir(parents=True, mode=0o700)
        self.exports.chmod(0o700)
        (self.exports / "objects").chmod(0o700)
        self.message = self.exports / "objects" / "message"
        self.message.write_bytes(b"SYNTHETIC_MESSAGE")
        self.message.chmod(0o600)
        self.outside = self.workspace / "outside"
        (self.outside).mkdir(mode=0o700)
        self.outside.chmod(0o700)
        (self.outside / "message").write_bytes(b"OUTSIDE")
        self.opens = []

    def kernel(self, before_open=None, deny_target=None):
        """A simulated kernel for the stand-in flag, plus the workspace capability; `before_open(path)` runs first."""
        real_open, workspace = os.open, str(self.workspace)
        def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
            name = os.fspath(path)
            if before_open:
                before_open(name, flags, dir_fd)
            self.opens.append((name, flags, dir_fd))
            inside = name == workspace or name.startswith(workspace + "/")
            if flags & os.O_DIRECTORY and (dir_fd is not None or not inside):
                raise PermissionError(errno.EPERM, "Operation not permitted", name)
            if deny_target is not None and name == str(deny_target):
                raise PermissionError(errno.EPERM, "Operation not permitted", name)
            if flags & FLAG and flags & os.O_NOFOLLOW:   # XNU vn_open_auth: the pair is EINVAL before any lookup
                raise OSError(errno.EINVAL, "Invalid argument", name)
            if flags & FLAG:   # the whole path is resolved in this one call: any symlink component refuses it
                prefix = ""
                for part in Path(name).parts[1:]:
                    prefix += "/" + part
                    if os.path.islink(prefix):
                        raise OSError(errno.ELOOP, "Too many levels of symbolic links", name)
                flags &= ~FLAG
            return real_open(path, flags, mode, dir_fd=dir_fd)
        return mock.patch.object(os, "open", side_effect=guarded_open)

    def with_flag(self, value=FLAG):
        return mock.patch.multiple(folder, NOFOLLOW_ANY=value), mock.patch.multiple(host, NOFOLLOW_ANY=value)

    def substitute(self):
        """The reviewer's CF137-1 schedule: `objects` renamed away and a symlink to `outside` put in its place."""
        objects = self.exports / "objects"
        objects.rename(self.exports / "objects-real")
        objects.symlink_to(self.outside, target_is_directory=True)

    def restore(self):
        objects = self.exports / "objects"
        objects.unlink()
        (self.exports / "objects-real").rename(objects)

    def test_secure_open_reads_a_workspace_file_with_one_atomic_open(self):
        a, b = self.with_flag()
        with a, b, self.kernel():
            with folder.secure_open(self.message) as stream:
                self.assertEqual(stream.read(), b"SYNTHETIC_MESSAGE")
        self.assertEqual(len(self.opens), 1, "only the target, never an ancestor")
        name, flags, dir_fd = self.opens[0]
        self.assertEqual((name, dir_fd), (str(self.message), None))
        self.assertEqual(flags, os.O_RDONLY | FLAG, "exactly the atomic form: O_NOFOLLOW_ANY alone, never with O_NOFOLLOW")

    def test_the_reviewers_substitute_then_restore_schedule_is_refused_at_the_atomic_open(self):
        # CF137-1: substitute before the open, restore after it. With one atomic open there is no check to restore
        # before; the open itself sees the symlink and refuses, and nothing outside is ever opened.
        def around(name, flags, dir_fd):
            if name == str(self.message):
                self.substitute()
        a, b = self.with_flag()
        with a, b, self.kernel(before_open=around):
            with self.assertRaises(OSError) as caught:
                folder.secure_open(self.message)
        self.restore()
        self.assertEqual(caught.exception.errno, errno.ELOOP)
        self.assertEqual(len(self.opens), 1)

    def test_without_the_flag_the_original_walk_is_used_and_still_refuses_the_schedule(self):
        def around(name, flags, dir_fd):
            if name == "objects" and dir_fd is not None and not (self.exports / "objects").is_symlink():
                self.substitute()
        a, b = self.with_flag(0)
        with a, b, mock.patch.object(os, "open", side_effect=self._recording_real_open(around)):
            with self.assertRaises(OSError) as caught:
                folder.secure_open(self.message)
        self.restore()
        self.assertEqual(caught.exception.errno, errno.ELOOP, "the descriptor walk's O_NOFOLLOW refuses the substituted component")
        self.assertTrue(any(dir_fd is not None for _, _, dir_fd in self.opens), "the original descriptor walk ran")

    def test_without_the_flag_the_capability_boundary_is_reported_not_routed_around(self):
        a, b = self.with_flag(0)
        with a, b, self.kernel():
            with self.assertRaises(PermissionError):
                folder.secure_open(self.message)
            with self.assertRaises(PermissionError):
                host.PrivateDir(str(self.exports))
        self.assertFalse(any(flags & FLAG for _, flags, _ in self.opens), "no flagged retry after EPERM")

    def test_the_simulated_kernel_rejects_the_incompatible_pair_and_nothing_retries_it(self):
        # Guards the fixture itself: a call combining O_NOFOLLOW_ANY with O_NOFOLLOW must fail as XNU fails it, so a
        # regression to the pair cannot pass behind the simulation; and an EINVAL propagates with no second open.
        with self.kernel():
            with self.assertRaises(OSError) as caught:
                os.open(str(self.message), os.O_RDONLY | os.O_NOFOLLOW | FLAG)
        self.assertEqual(caught.exception.errno, errno.EINVAL)
        def einval(path, flags, mode=0o777, *, dir_fd=None):
            self.opens.append((os.fspath(path), flags, dir_fd))
            raise OSError(errno.EINVAL, "Invalid argument")
        self.opens.clear()
        a, b = self.with_flag()
        with a, b, mock.patch.object(os, "open", side_effect=einval):
            with self.assertRaises(OSError) as caught:
                folder.secure_open(self.message)
        self.assertEqual((caught.exception.errno, len(self.opens)), (errno.EINVAL, 1), "no fallback after a refusal")

    def test_without_the_flag_the_original_walk_keeps_o_nofollow(self):
        a, b = self.with_flag(0)
        with a, b, mock.patch.object(os, "open", side_effect=self._recording_real_open(lambda *args: None)):
            with folder.secure_open(self.message) as stream:
                self.assertEqual(stream.read(), b"SYNTHETIC_MESSAGE")
        walked = [flags for _, flags, dir_fd in self.opens if dir_fd is not None]
        self.assertTrue(walked and all(flags & os.O_NOFOLLOW and not flags & FLAG for flags in walked))

    def test_a_refused_target_open_is_not_retried(self):
        a, b = self.with_flag()
        with a, b, self.kernel(deny_target=self.message):
            with self.assertRaises(PermissionError):
                folder.secure_open(self.message)
        self.assertEqual(len(self.opens), 1)

    def test_symlinks_and_non_regular_targets_are_still_refused(self):
        alias = self.workspace / "alias"
        alias.symlink_to(self.exports, target_is_directory=True)
        link = self.exports / "objects" / "link"
        link.symlink_to(self.message)
        a, b = self.with_flag()
        with a, b, self.kernel():
            for path in (alias / "objects" / "message", link):
                with self.subTest(path=path.name), self.assertRaises(OSError):
                    folder.secure_open(path)
            with self.assertRaises(ValueError):
                folder.secure_open(self.exports / "objects")
            with self.assertRaises(OSError):
                host.PrivateDir(str(alias))

    def test_private_dir_pins_the_workspace_with_one_atomic_open_and_keeps_its_trust_rules(self):
        writable = self.workspace / "writable"
        writable.mkdir(mode=0o700)
        writable.chmod(0o777)
        (writable / "inner").mkdir(mode=0o700)
        (writable / "inner").chmod(0o700)
        loose = self.workspace / "loose"
        loose.mkdir(mode=0o700)
        loose.chmod(0o750)
        a, b = self.with_flag()
        with a, b, self.kernel():
            with host.PrivateDir(str(self.exports)) as directory:
                directory.check()
                self.assertEqual(directory.inode, (self.exports.stat().st_dev, self.exports.stat().st_ino))
            self.assertEqual([n for n, f, _ in self.opens if f & os.O_DIRECTORY], [str(self.exports)], "only the final directory")
            self.assertEqual([f for n, f, _ in self.opens if f & os.O_DIRECTORY],
                             [os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | FLAG], "exactly the atomic form, without O_NOFOLLOW")
            for path in (writable / "inner", writable, loose):
                with self.subTest(path=path.name), self.assertRaises(host.Rejected):
                    host.PrivateDir(str(path))
            with host.PrivateDir(str(loose), private=False):
                pass

    def test_a_private_dir_replaced_before_its_open_is_refused(self):
        def around(name, flags, dir_fd):
            if name == str(self.exports) and flags & os.O_DIRECTORY:
                self.exports.rename(self.workspace / "old-exports")
                self.exports.mkdir(mode=0o700)
                self.exports.chmod(0o700)
        a, b = self.with_flag()
        with a, b, self.kernel(before_open=around):
            with self.assertRaises(host.Rejected):
                host.PrivateDir(str(self.exports))

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW_ANY"), "the real O_NOFOLLOW_ANY kernel flag exists only on macOS 11+")
    def test_the_real_kernel_flag_refuses_a_substituted_ancestor(self):
        with folder.secure_open(self.message) as stream:
            self.assertEqual(stream.read(), b"SYNTHETIC_MESSAGE")
        self.substitute()
        try:
            with self.assertRaises(OSError) as caught:
                folder.secure_open(self.message)
            self.assertEqual(caught.exception.errno, errno.ELOOP)
        finally:
            self.restore()

    def _recording_real_open(self, before_open):
        real_open = os.open
        def recording(path, flags, mode=0o777, *, dir_fd=None):
            before_open(os.fspath(path), flags, dir_fd)
            self.opens.append((os.fspath(path), flags, dir_fd))
            return real_open(path, flags, mode, dir_fd=dir_fd)
        return recording


if __name__ == "__main__":
    unittest.main()
