"""Host-side unit tests for the prototype anchor rule (no docker, no root). Synthetic stat/statvfs/mountinfo values model
the container cases; the host-mode cases use real files.  Run:  python3 test_anchor_rule.py
"""
import os
import stat
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import approval as ac

def check(path):
    try:
        ac._require_root(path)
        return {"ok": True, "mode": "host" if ac.init_userns() else "userns"}
    except ac.TrustUnavailable as error:
        return {"ok": False, "reason": str(error)}

OVF = 65534
F = "/etc/deflock/approvers.allowed_signers"
ROOTFS = {"dev": "0:101", "root": "/", "mountpoint": "/", "opts": ["ro", "relatime"], "fstype": "overlay"}
HOST_BIND = {"dev": "259:5", "root": "/etc/deflock", "mountpoint": "/etc/deflock", "opts": ["ro", "relatime"],
             "fstype": "ext4"}


def st(uid, mode):
    return types.SimpleNamespace(st_uid=uid, st_mode=mode)


class Box:
    """A fake container filesystem view: path -> stat, path -> read-only flag, and the mount table."""

    def __init__(self, stats, ro, mounts):
        self.stats, self.ro, self.mounts = stats, ro, mounts

    def __enter__(self):
        def lstat(p):
            p = str(p)
            if p not in self.stats:
                raise FileNotFoundError(p)
            return self.stats[p]

        def statvfs(p):
            return types.SimpleNamespace(f_flag=os.ST_RDONLY if self.ro.get(p, True) else 0)

        self.patches = [mock.patch.object(ac.os, "lstat", lstat), mock.patch.object(ac.os, "statvfs", statvfs),
                        mock.patch.object(ac, "mountinfo", lambda: self.mounts),
                        mock.patch.object(ac, "overflowuid", lambda: OVF),
                        mock.patch.object(ac, "init_userns", lambda: False)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


def good_stats(owner=OVF, dir_owner=OVF, mode=0o100644):
    return {F: st(owner, mode), "/etc/deflock": st(dir_owner, stat.S_IFDIR | 0o755),
            "/etc": st(0, stat.S_IFDIR | 0o755), "/": st(0, stat.S_IFDIR | 0o755)}


class NamespacedRule(unittest.TestCase):
    def verdict(self, stats, mounts, ro=None):
        with Box(stats, ro or {}, mounts):
            return check(F)

    def test_ro_bind_of_host_root_dir_is_accepted(self):
        got = self.verdict(good_stats(), [ROOTFS, HOST_BIND])
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mode"], "userns")

    def test_missing_trust_dir_is_refused(self):  # agentorange-flock today
        stats = {"/etc": st(0, stat.S_IFDIR | 0o755), "/": st(0, stat.S_IFDIR | 0o755)}
        self.assertIn("missing", self.verdict(stats, [ROOTFS])["reason"])

    def test_host_user_owned_file_shows_as_uid0_and_is_refused(self):
        got = self.verdict(good_stats(owner=0, dir_owner=0), [ROOTFS, dict(HOST_BIND, root="/home/user/x")])
        self.assertIn("is not the overflow uid", got["reason"])

    def test_writable_mount_is_refused(self):
        got = self.verdict(good_stats(), [ROOTFS, dict(HOST_BIND, opts=["rw"])], ro={F: False})
        self.assertIn("writable mount", got["reason"])

    def test_ro_flag_without_ro_mount_option_is_refused(self):
        got = self.verdict(good_stats(), [ROOTFS, dict(HOST_BIND, opts=["rw"])])
        self.assertIn("not read-only", got["reason"])

    def test_tmpfs_forgery_is_refused(self):
        tmpfs = {"dev": "0:180", "root": "/", "mountpoint": "/etc/deflock", "opts": ["ro"], "fstype": "tmpfs"}
        self.assertIn("trust mount root", self.verdict(good_stats(), [ROOTFS, tmpfs])["reason"])

    def test_tmpfs_subdir_bind_with_matching_root_is_refused(self):  # nested userns: tmpfs/etc/deflock bound in place
        fake = {"dev": "0:181", "root": "/etc/deflock", "mountpoint": "/etc/deflock", "opts": ["ro"], "fstype": "tmpfs"}
        self.assertIn("block-backed", self.verdict(good_stats(), [ROOTFS, fake])["reason"])

    def test_subuid_owned_forgery_is_refused_by_mount_root(self):  # owner shows 65534, ro, ext4: only the root differs
        forged = dict(HOST_BIND, root="/home/user/agent-work/forge")
        self.assertIn("trust mount root", self.verdict(good_stats(), [ROOTFS, forged])["reason"])

    def test_mount_over_the_file_is_refused(self):
        over = dict(HOST_BIND, mountpoint=F, root="/home/user/fake")
        got = self.verdict(good_stats(), [ROOTFS, HOST_BIND, over])
        self.assertFalse(got["ok"])

    def test_stacked_mount_on_top_of_the_bind_wins(self):
        top = dict(HOST_BIND, root="/home/user/fake")
        got = self.verdict(good_stats(), [ROOTFS, HOST_BIND, top])
        self.assertIn("trust mount root", got["reason"])

    def test_writable_file_mode_is_refused(self):
        self.assertIn("group/other writable", self.verdict(good_stats(mode=0o100666), [ROOTFS, HOST_BIND])["reason"])

    def test_symlink_is_refused(self):
        stats = good_stats()
        stats[F] = st(OVF, stat.S_IFLNK | 0o777)
        self.assertFalse(self.verdict(stats, [ROOTFS, HOST_BIND])["ok"])

    def test_writable_ancestor_is_refused(self):
        stats = good_stats()
        stats["/etc"] = st(0, stat.S_IFDIR | 0o777)
        self.assertIn("ancestor", self.verdict(stats, [ROOTFS, HOST_BIND])["reason"])

    def test_rw_rootfs_ancestor_is_refused(self):
        got = self.verdict(good_stats(), [dict(ROOTFS, opts=["rw"]), HOST_BIND], ro={"/etc": False})
        self.assertIn("ancestor", got["reason"])

    def test_wrong_directory_is_refused(self):
        with Box(good_stats(), {}, [ROOTFS, HOST_BIND]):
            self.assertFalse(check("/tmp/approvers.allowed_signers")["ok"])


class Mountinfo(unittest.TestCase):
    def test_parse_and_unescape(self):
        line = "8753 8712 259:5 /home/user/a\\040b /workspace/x rw,relatime - ext4 /dev/nvme0n1p2 rw\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=line)):
            rows = ac.mountinfo()
        self.assertEqual(rows[0]["root"], "/home/user/a b")
        self.assertEqual(rows[0]["mountpoint"], "/workspace/x")
        self.assertEqual(rows[0]["fstype"], "ext4")

    def test_prefix_is_not_a_cover(self):
        rows = [ROOTFS, dict(HOST_BIND, mountpoint="/etc/deflockX")]
        self.assertEqual(ac.covering(rows, F)["mountpoint"], "/")

    def test_real_host_table_parses(self):
        rows = ac.mountinfo()
        self.assertTrue(any(r["mountpoint"] == "/" for r in rows))


class HostMode(unittest.TestCase):
    """The initial user namespace keeps approval._require_root unchanged (real files on this host)."""

    def test_host_is_initial_userns(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="0 0 4294967295")):
            self.assertTrue(ac.init_userns())

    HOST_FILE = "/usr/share/deflock/host-anchor"

    def host_check(self, leaf_uid=0, ancestor_uid=0):
        """Run check() in host mode against a synthetic owner table (no real root-owned file needed)."""
        stats = {q: st(ancestor_uid, stat.S_IFDIR | 0o755) for q in ("/", "/usr", "/usr/share", "/usr/share/deflock")}
        stats["/"] = st(0, stat.S_IFDIR | 0o755)
        stats[self.HOST_FILE] = st(leaf_uid, stat.S_IFREG | 0o644)
        seen = []

        def lstat(q):
            seen.append(str(q))
            return stats[str(q)]

        with mock.patch.object(ac, "init_userns", lambda: True), mock.patch.object(ac.os, "lstat", lstat):
            got = check(self.HOST_FILE)
        # The fake owner table must really be what the rule read; otherwise this test proves nothing and fails.
        self.assertIn(self.HOST_FILE, seen)
        self.assertEqual(got.get("mode", "host"), "host", got)
        return got

    def test_root_owned_host_file_passes(self):
        got = self.host_check()
        self.assertTrue(got["ok"], got)

    def test_non_root_owned_host_file_is_refused(self):
        got = self.host_check(leaf_uid=1000)
        self.assertFalse(got["ok"], got)
        self.assertIn("not root-owned", got["reason"])
        self.assertIn(self.HOST_FILE, got["reason"])

    def test_non_root_owned_host_ancestor_is_refused(self):
        got = self.host_check(ancestor_uid=1000)
        self.assertFalse(got["ok"], got)
        self.assertIn("not root-owned", got["reason"])

    def test_root_owned_protected_chain_passes_with_synthetic_stats(self):
        # Hermetic positive case: host mode accepts a root-owned regular file under root-owned, non-writable ancestors.
        p = "/etc/deflock/approvers.allowed_signers"
        stats = {q: st(0, stat.S_IFDIR | 0o755) for q in ("/", "/etc", "/etc/deflock")}
        stats[p] = st(0, stat.S_IFREG | 0o644)
        with mock.patch.object(ac, "init_userns", lambda: True), mock.patch.object(ac.os, "lstat", lambda q: stats[str(q)]):
            self.assertTrue(ac._require_root_host(p) is None)
            stats["/etc"] = st(0, stat.S_IFDIR | 0o775)
            with self.assertRaises(ac.TrustUnavailable):
                ac._require_root_host(p)

    def test_missing_host_trust_file_is_refused(self):
        if os.path.lexists(F):
            self.skipTest("the host trust file exists now (HOSTA-1 step 11 done)")
        got = check(F)
        self.assertFalse(got["ok"])
        self.assertIn("missing", got["reason"])

    def test_user_owned_host_file_is_refused(self):
        got = check(str(Path(__file__).resolve()))
        self.assertFalse(got["ok"])
        self.assertIn("not root-owned", got["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
