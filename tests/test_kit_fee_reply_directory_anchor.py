"""RF1440-2: fee-reply writes stay anchored to the admitted private kit/followups folder. Offline, synthetic."""
import hashlib
import json
import os
import shutil
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_fee_reply, law  # noqa: E402
from tests import test_kit_fee_reply as base  # noqa: E402  (reuses the public synthetic fixture, unmodified)

A, NAME = base.A, f"{base.A}--main-fee-2026-03-10.md"
# Nonsecret SHA-256 pins of synthetic drafts/CLI output from main before RF1440-2.
# Rendering must stay byte-identical; only these exact fixture lines are annotated.
MAIN_SHA = "d9f4b977d6e861413a74d554e9447b8d620bb89dd94b659f5cfe853c57694a57"  # pragma: allowlist secret
NARROW_SHA = "492a76a7126556405d7bdef048882f6e0339cdc38dd347965626e4c2b7e5417f"  # pragma: allowlist secret
REVISED_SHA = "213cb5f81ca28e8eaf9c4b069a2ac6bd5c24787d5ff2b78519546ea98457e5c6"  # pragma: allowlist secret
CLI_SHA = "c49bf48ebe9acf8bbe8ba58f33f9e9a3ac4a3f7f945320e8d836569541922d9b"  # pragma: allowlist secret


def fds():
    return len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else 0


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class DirectoryAnchorTests(unittest.TestCase):  # borrows the fixture methods only, so its tests are not rerun
    FIXTURE = base.FeeReplyTests
    setUp, package, send, save, step, estimate, make, letter = (
        FIXTURE.setUp, FIXTURE.package, FIXTURE.send, FIXTURE.save, FIXTURE.step, FIXTURE.estimate, FIXTURE.make,
        FIXTURE.letter)

    def prepare(self):  # an estimate, a private followups folder and a 0755 outside folder with a sentinel
        self.estimate()
        self.folder = self.root / "kit" / "followups"
        self.folder.mkdir(mode=0o700)
        self.moved = self.root / "kit" / "followups.admitted"
        self.outside = self.tmp / "outside"
        self.outside.mkdir(mode=0o755)
        self.outside.chmod(0o755)
        (self.outside / "sentinel.txt").write_text("sentinel", encoding="utf-8")

    def swap(self):  # the admitted folder moves aside; its name now links to the outside folder
        os.rename(self.folder, self.moved)
        self.folder.symlink_to(self.outside, target_is_directory=True)

    def restore(self):  # fixture-only restoration before a genuine retry
        self.folder.unlink()
        os.rename(self.moved, self.folder)

    def after(self, seam, action=None):  # wrap a real writer seam: run ``action`` once, then the real seam
        real, fired = getattr(kit_fee_reply, seam), []

        def wrapper(*args, **kwargs):
            if not fired:
                fired.append(True)
                (action or self.swap)()
            return real(*args, **kwargs)
        return mock.patch.object(kit_fee_reply, seam, side_effect=wrapper), fired

    def outside_untouched(self):
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["sentinel.txt"])
        self.assertEqual((self.outside / "sentinel.txt").read_text(encoding="utf-8"), "sentinel")

    def dotfiles(self, folder):
        return [p.name for p in Path(folder).iterdir() if p.name.startswith(".")]

    def test_new_draft_directory_swap_cannot_redirect(self):
        self.prepare()
        opened = fds()
        patch, fired = self.after("_temp")
        with patch, self.assertRaises(ValueError) as caught:
            self.make()
        self.assertTrue(fired)
        self.assertNotIn("Fee basis", str(caught.exception))
        self.outside_untouched()
        self.assertEqual(list(self.moved.iterdir()), [])  # the call's own temporary file was cleaned up
        self.assertEqual(fds(), opened)
        self.restore()
        result = self.make()  # a genuine healthy retry
        self.assertEqual(sha(self.root / result["written"]), MAIN_SHA)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / result["written"]).st_mode), 0o600)

    def test_replace_directory_swap_cannot_redirect_even_same_target_inode(self):
        self.prepare()
        old = self.folder / NAME
        old.write_text("my edited draft\n", encoding="utf-8")
        old.chmod(0o600)
        inode = old.stat().st_ino

        def move_target():  # the target keeps its inode and single link, but now lives outside
            self.swap()
            os.rename(self.moved / NAME, self.outside / NAME)
        patch, fired = self.after("_temp", move_target)
        with patch, self.assertRaises(ValueError):
            self.make(replace=True)
        self.assertTrue(fired)
        moved = self.outside / NAME
        self.assertEqual((moved.read_text(encoding="utf-8"), moved.stat().st_ino), ("my edited draft\n", inode))
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), [NAME, "sentinel.txt"])
        self.assertEqual(list(self.moved.iterdir()), [])
        os.rename(moved, self.moved / NAME)
        self.restore()
        self.make(replace=True)
        self.assertNotIn("my edited draft", (self.folder / NAME).read_text(encoding="utf-8"))

    def test_swap_after_descriptor_admission_cleans_original_directory(self):
        self.prepare()
        opened = fds()
        for seam in ("_temp", "_same_folder"):  # before staging, then after staging but before publication
            patch, fired = self.after(seam)
            with patch, self.assertRaises(ValueError):
                self.make()
            self.assertTrue(fired, seam)
            self.outside_untouched()
            self.assertEqual(list(self.moved.iterdir()), [], seam)
            self.assertEqual(fds(), opened, seam)
            self.restore()
        real = kit_fee_reply._same_folder

        def check_then_swap(*args):  # the swap lands after the final path check passed
            result = real(*args)
            self.swap()
            return result
        with mock.patch.object(kit_fee_reply, "_same_folder", side_effect=check_then_swap):
            self.make()  # publication and cleanup still go through the admitted descriptor
        self.outside_untouched()
        self.assertEqual([p.name for p in self.moved.iterdir()], [NAME])
        self.assertEqual(sha(self.moved / NAME), MAIN_SHA)
        self.assertEqual(fds(), opened)
        self.restore()
        old = (self.folder / NAME).read_bytes()
        with mock.patch.object(kit_fee_reply, "_same_folder", side_effect=check_then_swap):
            self.make(replace=True)
        self.outside_untouched()
        self.assertEqual([p.name for p in self.moved.iterdir()], [NAME])
        self.assertEqual((self.moved / NAME).read_bytes(), old)  # identical regenerated bytes, replaced in place
        self.assertEqual(fds(), opened)

    def test_real_directory_substitution_and_nonprivate_admission(self):
        self.prepare()
        other = self.tmp / "other"
        real_open = os.open

        def substitute(mode):
            def action():
                os.rename(self.folder, self.moved)
                other.mkdir(mode=mode)
                other.chmod(mode)
                os.rename(other, self.folder)
            return action

        for mode in (0o700, 0o755):  # a different real directory between the admission stat and open
            action, fired = substitute(mode), []

            def opener(path, flags, *args, **kwargs):
                if not fired and Path(path) == self.folder and flags & os.O_DIRECTORY:
                    fired.append(True)
                    action()
                return real_open(path, flags, *args, **kwargs)
            with mock.patch.object(kit_fee_reply.os, "open", side_effect=opener), self.assertRaises(ValueError):
                self.make()
            self.assertTrue(fired, oct(mode))
            self.assertEqual((list(self.folder.iterdir()), list(self.moved.iterdir())), ([], []))
            self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), mode)  # never chmoded
            self.folder.rmdir()
            os.rename(self.moved, self.folder)
        patch, fired = self.after("_folder", substitute(0o755))  # a 0755 real folder before the admission stat
        with patch, self.assertRaises(ValueError):
            self.make()
        self.assertTrue(fired)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o755)
        self.assertEqual(list(self.folder.iterdir()), [])
        self.folder.rmdir()
        os.rename(self.moved, self.folder)
        self.folder.rmdir()
        for make_bad in (lambda: self.folder.symlink_to(self.outside, target_is_directory=True),
                         lambda: self.folder.symlink_to(self.tmp / "absent"), lambda: os.mkfifo(self.folder),
                         lambda: self.folder.write_text("x", encoding="utf-8")):
            make_bad()
            with self.assertRaises(ValueError):
                self.make()
            self.folder.unlink()
        self.outside_untouched()
        result = self.make()  # a missing folder is created private
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertEqual(sha(self.root / result["written"]), MAIN_SHA)

    def test_anchor_fault_cleanup_and_retry(self):
        self.prepare()
        real_fdopen, real_stat = os.fdopen, os.stat

        class BadWrite:
            def __init__(self, fd, *args, **kwargs):
                self.real = real_fdopen(fd, *args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.real.close()

            def write(self, text):
                raise OSError("synthetic write fault")

        def bad_stat(*args, **kwargs):  # only the descriptor-relative target stat fails
            if kwargs.get("dir_fd") is not None:
                raise OSError("synthetic stat fault")
            return real_stat(*args, **kwargs)
        faults = {
            "create": (kit_fee_reply, "_temp", OSError("synthetic create fault")),
            "fdopen": (kit_fee_reply.os, "fdopen", OSError("synthetic fdopen fault")),
            "write": (kit_fee_reply.os, "fdopen", BadWrite),
            "fsync": (kit_fee_reply.os, "fsync", OSError("synthetic fsync fault")),
            "stat": (kit_fee_reply.os, "stat", bad_stat),
            "link": (kit_fee_reply.os, "link", OSError("synthetic link fault")),
            "replace": (kit_fee_reply.os, "replace", OSError("synthetic replace fault")),
        }
        unrelated = self.folder / "unrelated-note.md"
        unrelated.write_text("keep me\n", encoding="utf-8")
        opened = fds()
        for replace in (False, True):
            if replace:
                (self.folder / NAME).write_text("pre-install draft\n", encoding="utf-8")
            before = sorted(p.name for p in self.folder.iterdir())
            target = (self.folder / NAME).read_bytes() if replace else None
            for label, (owner, attribute, effect) in faults.items():
                if label == ("replace" if not replace else "link"):
                    continue  # that primitive is not on this path
                with mock.patch.object(owner, attribute, side_effect=effect), self.assertRaises(OSError):
                    self.make(replace=replace)
                self.assertEqual(sorted(p.name for p in self.folder.iterdir()), before, label)
                self.assertEqual(self.dotfiles(self.folder), [], label)
                self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me\n", label)
                if replace:
                    self.assertEqual((self.folder / NAME).read_bytes(), target, label)
                self.assertEqual(fds(), opened, label)
            self.make(replace=replace)  # a genuine retry after every fault
            self.assertEqual(sha(self.folder / NAME), MAIN_SHA)
            self.assertEqual(fds(), opened)
        self.outside_untouched()

    def test_normal_writer_and_cli_contracts_unchanged(self):
        self.estimate()
        main = self.make()
        self.assertEqual(sha(self.root / main["written"]), MAIN_SHA)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / main["written"]).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "kit" / "followups").st_mode), 0o700)
        with self.assertRaises(ValueError) as caught:  # no overwrite without --replace
            self.make(narrow="some scope")
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(sha(self.root / main["written"]), MAIN_SHA)
        self.assertEqual(sha(self.root / self.make(narrow="only 2026", replace=True)["written"]), NARROW_SHA)
        self.estimate(7000, "2026-03-11")
        named = self.make()
        self.assertEqual(sha(self.root / named["written"]), REVISED_SHA)
        folder = self.root / "kit" / "followups"
        target = folder / named["written"].rsplit("/", 1)[1]
        appeared, real_link = [], os.link

        def collide(*args, **kwargs):  # the target appears after the absence check: never overwritten
            if not appeared:
                appeared.append(True)
                target.write_text("another writer\n", encoding="utf-8")
            return real_link(*args, **kwargs)
        target.unlink()
        with mock.patch.object(kit_fee_reply.os, "link", side_effect=collide), self.assertRaises(ValueError) as caught:
            self.make()
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(target.read_text(encoding="utf-8"), "another writer\n")
        self.assertEqual(self.dotfiles(folder), [])
        target.unlink()
        outside = self.tmp / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        target.symlink_to(outside)
        for replace in (False, True):
            with self.assertRaises(ValueError):
                self.make(replace=replace)
        target.unlink()
        os.mkfifo(target)
        with self.assertRaises(ValueError):
            self.make(replace=True)
        target.unlink()
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        for path in folder.iterdir():
            path.unlink()
        self.sent[A]["steps"].pop()
        self.save()
        folder.rmdir()
        inputs = {p: p.read_bytes() for p in (self.root / "campaign.json", self.root / "kit" / "sent.json",
                                              self.root / "kit" / "agencies.json")}
        with mock.patch.object(law, "JURISDICTIONS", self.base):
            code, out, err = base.run("kit", "fee-reply", "--directory", self.root, "--agency", A)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith(base.SENT + "\n"))
        self.assertEqual(json.loads(out[:-len(base.SENT) - 1])["written"], f"kit/followups/{NAME}")
        self.assertEqual(sha(folder / NAME), MAIN_SHA)
        self.assertEqual(hashlib.sha256(out.encode("utf-8")).hexdigest(), CLI_SHA)
        self.assertEqual({p: p.read_bytes() for p in inputs}, inputs)
        (folder / NAME).unlink()
        folder.chmod(0o755)
        with mock.patch.object(law, "JURISDICTIONS", self.base):
            code, out, err = base.run("kit", "fee-reply", "--directory", self.root, "--agency", A,
                                      "--narrow", "secret scope words")
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("Traceback", err)
        self.assertNotIn("secret scope words", err)
        self.assertEqual((stat.S_IMODE(folder.stat().st_mode), list(folder.iterdir())), (0o755, []))
        self.assertEqual({p: p.read_bytes() for p in inputs}, inputs)
        folder.chmod(0o700)
        code, out, err = base.run("kit", "fee-reply", "--directory", self.root, "--agency", A)  # shipped: NO_RULE
        self.assertEqual((code, out), (1, ""))
        self.assertIn(kit_fee_reply.NO_RULE, err)


if __name__ == "__main__":
    unittest.main()
