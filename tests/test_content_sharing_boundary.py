"""RF0804-4: ``content agency-sharing`` is confined to the selected campaign's own content directory.

Two disjoint fictional campaigns are copied from the repository example into a temporary directory; nothing outside it is
read or written, and no network, build or publication is involved.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import content_sharing
from campaign_tool.content import MAX_JSON_BYTES, ContentError
from campaign_tool.content_sharing import SharingImportError, import_sharing

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
AGENCY = "ca-cedar-police"
URL = "https://transparency.flocksafety.com/cedar-police"
CHECKED = "2026-10-31"
BASIC = "name,state,kind\nCedar Hills PD,CA,local\nReno Test PD,NV,local\nFictional Federal Office,US,federal\n"


def tree(root):
    """Every path under ``root`` (links not followed) with kind, mode, mtime, bytes or link target."""
    found = {}
    for here, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = os.path.join(here, name)
            info = os.lstat(path)
            body = os.readlink(path) if stat.S_ISLNK(info.st_mode) else (Path(path).read_bytes() if stat.S_ISREG(info.st_mode) else None)
            mtime = None if stat.S_ISDIR(info.st_mode) else info.st_mtime_ns  # a directory's own mtime moves with any entry change
            found[os.path.relpath(path, root)] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), mtime, body)
    return found


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sharing-boundary-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.a, self.b = self.tmp / "campaign-a", self.tmp / "campaign-b"
        for root in (self.a, self.b):
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
            path = root / "content" / "agencies.json"
            path.write_text(json.dumps(json.loads(path.read_text()), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.csv = self.tmp / "sharing.csv"
        self.csv.write_text(BASIC, encoding="utf-8", newline="")

    def run_import(self, root=None, **kwargs):
        arguments = dict(agency_id=AGENCY, csv_path=self.csv, checked_on=CHECKED, source_url=URL, today=TODAY)
        arguments.update(kwargs)
        return import_sharing(root or self.a, **arguments)

    def link_a_content_to_b(self):
        shutil.rmtree(self.a / "content")
        (self.a / "content").symlink_to(self.b / "content")

    def cli(self, root):
        arguments = [sys.executable, "-B", "-m", "campaign_tool", "content", "agency-sharing", "--directory", str(root), "--agency", AGENCY,
                     "--csv", str(self.csv), "--checked-on", CHECKED, "--source-url", URL]
        return subprocess.run(arguments, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def assertRefusedUntouched(self, root, *fragments, **kwargs):
        before = tree(self.tmp)
        with self.assertRaises(SharingImportError) as caught:
            self.run_import(root, **kwargs)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        self.assertNotIn(str(self.tmp), str(caught.exception))
        self.assertEqual(before, tree(self.tmp))
        return str(caught.exception)


class ContentDirectoryTests(Base):
    def test_content_symlink_refuses_without_reading_or_mutating_other_campaign(self):
        self.link_a_content_to_b()
        message = self.assertRefusedUntouched(self.a, "content/")
        self.assertNotIn("campaign-b", message)
        self.assertTrue((self.a / "content").is_symlink())
        self.assertNotIn(b"Reno Test PD", (self.b / "content" / "agencies.json").read_bytes())

    def test_dangling_and_non_directory_content_refuse(self):
        for make in (lambda p: p.symlink_to(self.tmp / "nowhere"),               # dangling link
                     lambda p: p.write_text("not a directory"),                  # a file
                     lambda p: os.mkfifo(p)):                                    # a special file: must not block
            with self.subTest(make=make):
                if (self.a / "content").is_dir() and not (self.a / "content").is_symlink():
                    shutil.rmtree(self.a / "content")
                elif os.path.lexists(self.a / "content"):
                    (self.a / "content").unlink()
                make(self.a / "content")
                self.assertRefusedUntouched(self.a, "content/")
        (self.a / "content").unlink()
        self.assertRefusedUntouched(self.a, "content/")  # missing

    def test_real_command_refuses_a_linked_content_directory_with_one_stopped_line(self):
        self.link_a_content_to_b()
        before = tree(self.tmp)
        done = self.cli(self.a)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.stderr.count("Stopped:"), 1, done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("content/", done.stderr)
        for leaked in ("Traceback", "campaign-b", URL, str(self.tmp)):
            self.assertNotIn(leaked, done.stderr + done.stdout)
        self.assertEqual(before, tree(self.tmp))

    def test_content_directory_swap_does_not_redirect_write(self):
        aside = self.a / "content-original"
        original = (self.a / "content" / "agencies.json").read_bytes()

        def swap():
            (self.a / "content").rename(aside)
            (self.a / "content").symlink_to(self.b / "content")
        before_b = tree(self.b)
        with mock.patch.object(content_sharing, "_before_install", side_effect=swap):
            with self.assertRaises(SharingImportError) as caught:
                self.run_import()
        self.assertIn("content/ changed", str(caught.exception))
        self.assertEqual(before_b, tree(self.b))  # the other campaign: nothing written, no temporary file
        self.assertEqual((aside / "agencies.json").read_bytes(), original)
        self.assertEqual(sorted(p.name for p in aside.iterdir() if p.name.endswith(".tmp")), [])
        self.assertTrue((self.a / "content").is_symlink())


class TargetFileTests(Base):
    def target(self):
        return self.a / "content" / "agencies.json"

    def test_leaf_nonregular_and_bounded_target_reads(self):
        target = self.target()
        good = target.read_bytes()
        outside = self.tmp / "outside.json"
        outside.write_bytes(good)

        def linked():
            target.unlink()
            target.symlink_to(outside)

        def dangling():
            target.unlink()
            target.symlink_to(self.tmp / "nowhere.json")

        def fifo():
            target.unlink()
            os.mkfifo(target)

        def directory():
            target.unlink()
            target.mkdir()

        def missing():
            target.unlink()
        cases = {"linked": (linked, "content/agencies.json"), "dangling": (dangling, "content/agencies.json"),
                 "fifo": (fifo, "regular file"), "directory": (directory, "regular file"), "missing": (missing, "not found"),
                 "bad utf-8": (lambda: target.write_bytes(b"[\xff]"), "UTF-8 JSON"), "bad json": (lambda: target.write_bytes(b"[{"), "UTF-8 JSON"),
                 "one over the cap": (lambda: target.write_bytes(good + b" " * (MAX_JSON_BYTES + 1 - len(good))), "too large")}
        for label, (make, fragment) in cases.items():
            with self.subTest(label):
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    target.rmdir()
                elif os.path.lexists(target):
                    target.unlink()
                target.write_bytes(good)
                make()
                self.assertRefusedUntouched(self.a, fragment)
        target.unlink()
        target.write_bytes(good + b" " * (MAX_JSON_BYTES - len(good)))  # exactly the cap is still read
        self.assertEqual(target.stat().st_size, MAX_JSON_BYTES)
        self.assertEqual(self.run_import()["recipients"], 3)

    def swap_cases(self):
        target = self.target()

        def replaced():
            new = target.with_name("other.json")
            new.write_text("[]\n")
            os.replace(new, target)
            return b"[]\n"

        def removed():
            target.unlink()
            return None

        def retargeted():
            outside = self.tmp / "outside.json"
            outside.write_text("[]\n")
            target.unlink()
            target.symlink_to(outside)
            return "link"

        def edited_in_place():  # same size, mtime put back: the change still shows in the bytes and ctime
            data, info = target.read_bytes(), target.stat()
            edited = data.replace(b"Cedar", b"Cadar", 1)
            with open(target, "r+b") as handle:
                handle.write(edited)
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
            return edited
        return {"replaced": replaced, "removed": removed, "retargeted": retargeted, "edited in place": edited_in_place}

    def test_target_swap_or_in_place_edit_preserves_newer_bytes(self):
        for replace in (False, True):
            for label, change in self.swap_cases().items():
                with self.subTest(label=label, replace=replace):
                    shutil.rmtree(self.a)
                    shutil.copytree(self.b, self.a)
                    if replace:
                        self.run_import()  # a list already there
                    held = {}
                    with mock.patch.object(content_sharing, "_before_install", side_effect=lambda c=change: held.update(now=c())):
                        with self.assertRaises(SharingImportError) as caught:
                            self.run_import(replace=replace)
                    self.assertIn("nothing was written", str(caught.exception))
                    now = held["now"]
                    if now is None:
                        self.assertFalse(os.path.lexists(self.target()))
                    elif now == "link":
                        self.assertTrue(self.target().is_symlink())
                        self.assertEqual((self.tmp / "outside.json").read_text(), "[]\n")
                    else:
                        self.assertEqual(self.target().read_bytes(), now)
                    self.assertEqual([p.name for p in self.target().parent.iterdir() if p.name.endswith(".tmp")], [])


class FailureTests(Base):
    def test_write_fsync_replace_failures_preserve_previous_file(self):
        target = self.a / "content" / "agencies.json"
        os.chmod(target, 0o640)
        bystander = self.a / "content" / ".agencies.json.bystander.tmp"
        bystander.write_text("not ours")
        before = tree(self.a)
        for name in ("write", "fsync", "replace"):
            with self.subTest(name):
                opened, real_open, real_close = set(), os.open, os.close

                def tracking_open(*args, **kwargs):
                    fd = real_open(*args, **kwargs)
                    opened.add(fd)
                    return fd

                def tracking_close(fd):
                    opened.discard(fd)
                    real_close(fd)
                with mock.patch.object(os, "open", tracking_open), mock.patch.object(os, "close", tracking_close), \
                        mock.patch.object(os, name, side_effect=OSError(28, "No space left on device")):
                    with self.assertRaises(OSError):
                        self.run_import()
                self.assertEqual(opened, set())  # every descriptor closed
                self.assertEqual(before, tree(self.a))  # bytes, mode and mtime kept; only the bystander temp file remains
                self.assertEqual(bystander.read_text(), "not ours")


class NormalImportTests(Base):
    def test_normal_import_and_explicit_replacement_are_unchanged(self):
        content_modes = {root: stat.S_IMODE(os.stat(root / "content").st_mode) for root in (self.a, self.b)}
        base = json.loads((self.a / "content" / "agencies.json").read_text())
        before_b = tree(self.b)
        self.csv.write_bytes(b"\xef\xbb\xbf" + "name,state,kind\nZulu PD,NV,local\nCaf\u00e9 PD,CA,local\n".encode("utf-8"))
        self.assertEqual(self.run_import(self.a), {"agency_id": AGENCY, "recipients": 2, "checked_on": CHECKED, "source_url": URL})
        self.assertEqual(before_b, tree(self.b))
        text = (self.a / "content" / "agencies.json").read_text(encoding="utf-8")
        document = json.loads(text)
        self.assertEqual(text, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual([r["name"] for r in next(a for a in document if a["agency_id"] == AGENCY)["profile"]["sharing"]["recipients"]],
                         ["Caf\u00e9 PD", "Zulu PD"])  # sorted by state then name
        self.assertEqual(stat.S_IMODE(os.stat(self.a / "content" / "agencies.json").st_mode), 0o600)
        self.run_import(self.b)  # the other campaign, on its own
        after_a = tree(self.a)
        with self.assertRaises(SharingImportError):
            self.run_import(self.a)  # a list exists: needs --replace
        self.csv.write_text(BASIC, encoding="utf-8", newline="")
        self.assertEqual(self.run_import(self.a, replace=True)["recipients"], 3)
        document = json.loads((self.a / "content" / "agencies.json").read_text(encoding="utf-8"))
        for agency in document:
            if agency["agency_id"] == AGENCY:
                del agency["profile"]["sharing"]
        for agency in base:
            if agency["agency_id"] == AGENCY:
                agency.get("profile", {}).pop("sharing", None)
        self.assertEqual(document, base)  # every other agency and field as it was
        self.assertNotEqual(after_a, tree(self.a))
        for root in (self.a, self.b):
            self.assertEqual(stat.S_IMODE(os.stat(root / "content").st_mode), content_modes[root])
        self.assertTrue(issubclass(SharingImportError, ContentError))


if __name__ == "__main__":
    unittest.main()
