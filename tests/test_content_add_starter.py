"""DF1109-11: `content add-starter NAME|--list` adds one packaged starter to an existing content/ (synthetic only)."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import content_init
from campaign_tool.content_init import StarterError, add_starter, starter_status

REPO_ROOT = Path(__file__).resolve().parents[1]
WARNING = "This content is published by the next build until edited or deleted. Nothing was built, sent or deployed."
TARGETS = {"faq": "faq.json", "help-map": "pages/help-map.json", "background": "pages/background.json"}


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=180)


def snapshot(root):
    """Every entry under root with mode, link target or file hash; links are never followed."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = path.lstat()
            detail = ("link:" + os.readlink(path) if path.is_symlink() else
                      hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else
                      "dir" if path.is_dir() else "other")
            entries[str(path.relative_to(root))] = (oct(info.st_mode), detail)
    return entries


class AddStarterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-add-starter-")
        cls.addClassCleanup(temporary.cleanup)
        cls.base = Path(temporary.name)
        cls.fresh = {}
        for state in ("CA", "OR"):  # a fresh campaign with content init's own output, the byte reference
            root = cls.base / ("fresh-" + state)
            for args in (("init", "--directory", root, "--name", "Synthetic Cedar Watch", "--county", "Cedar",
                          "--state", state), ("content", "init", "--directory", root)):
                result = cli(*args)
                assert result.returncode == 0, result.stderr
            cls.fresh[state] = root

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-add-starter-case-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.outside = self.work / "outside"
        self.outside.mkdir()

    def older(self, state="CA", tag=""):
        """A campaign whose content/ predates the starters: no faq.json and no pages/."""
        root = self.work / ("campaign-" + state + tag)
        shutil.copytree(self.fresh[state], root, symlinks=True)
        (root / "content" / "faq.json").unlink()
        shutil.rmtree(root / "content" / "pages")
        return root

    def refused(self, root, name, message):
        before = snapshot(root.parent)
        with self.assertRaises(StarterError) as caught:
            add_starter(root, name)
        self.assertIn(message, str(caught.exception))
        self.assertEqual(snapshot(root.parent), before)

    def test_each_starter_matches_fresh_init_bytes(self):
        for state in ("CA", "OR"):
            root = self.older(state)
            for name, target in TARGETS.items():
                with self.subTest(state=state, name=name):
                    result = cli("content", "add-starter", name, "--directory", root)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout.startswith(f"content/{target} written from the packaged {name} starter."))
                    self.assertTrue(result.stdout.endswith(WARNING + "\n"))
                    written, reference = root / "content" / target, self.fresh[state] / "content" / target
                    self.assertEqual(written.read_bytes(), reference.read_bytes())
                    self.assertEqual(written.stat().st_mode, reference.stat().st_mode)
            self.assertEqual(snapshot(root / "content"), snapshot(self.fresh[state] / "content"))
            self.assertEqual(sorted(p.name for p in (root / "content" / "pages").iterdir()), ["background.json", "help-map.json"])
        # the generic deadline wording outside California, the shipped rule in California
        self.assertNotIn("rule_id", (self.work / "campaign-OR" / "content" / "faq.json").read_text(encoding="utf-8"))
        self.assertIn("rule_id", (self.work / "campaign-CA" / "content" / "faq.json").read_text(encoding="utf-8"))

    def test_list_is_read_only_and_mixed_mode_refuses(self):
        root = self.older()
        before = snapshot(self.work)
        result = cli("content", "add-starter", "--list", "--directory", root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "faq\tcontent/faq.json\tabsent\nhelp-map\tcontent/pages/help-map.json\tabsent\n"
                                        "background\tcontent/pages/background.json\tabsent\n")
        self.assertEqual(snapshot(self.work), before)  # no pages/ created
        (root / "content" / "faq.json").write_text("{}", encoding="utf-8")  # any file counts, even an invalid one
        (root / "content" / "pages").mkdir()
        (root / "content" / "pages" / "help-map.json").symlink_to(self.work / "missing")  # dangling
        self.assertEqual(starter_status(root), [("faq", "content/faq.json", True),
                                                ("help-map", "content/pages/help-map.json", True),
                                                ("background", "content/pages/background.json", False)])
        before = snapshot(self.work)
        for args, needle in (((), "give one starter NAME"), (("faq", "--list"), "give one starter NAME"),
                             (("readme",), "invalid choice"), (("../site",), "invalid choice")):
            with self.subTest(args=args):
                result = cli("content", "add-starter", *args, "--directory", root)
                self.assertEqual(result.returncode, 2)
                self.assertIn(needle, result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertEqual(snapshot(self.work), before)
        with self.assertRaises(StarterError):
            add_starter(root, "../faq")

    def test_existing_target_and_unsafe_parents_refuse_unchanged(self):
        root = self.older()
        pages = root / "content" / "pages"
        pages.mkdir()
        for kind in ("file", "folder", "fifo", "symlink"):
            target = pages / "background.json"
            with self.subTest(kind=kind):
                if kind == "file":
                    target.write_bytes(b"organizer bytes")
                elif kind == "folder":
                    target.mkdir()
                elif kind == "fifo":
                    os.mkfifo(target)
                else:
                    target.symlink_to(self.outside / "background.json")
                self.refused(root, "background", "content/pages/background.json already exists")
                self.assertEqual(list(self.outside.iterdir()), [])
                target.rmdir() if kind == "folder" else target.unlink()
        pages.rmdir()
        pages.symlink_to(self.outside, target_is_directory=True)  # a symlinked parent is never followed
        self.refused(root, "help-map", "content/pages/ must be a real folder")
        with self.assertRaises(StarterError):
            starter_status(root)  # the whole listing refuses
        result = cli("content", "add-starter", "--list", "--directory", root)
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertNotIn("Traceback", result.stderr)
        pages.unlink()
        pages.write_text("not a folder", encoding="utf-8")
        self.refused(root, "background", "content/pages/ must be a real folder")
        pages.unlink()
        site = root / "content" / "site.json"
        original = site.read_bytes()
        site.write_text("{", encoding="utf-8")
        self.refused(root, "faq", "content/site.json is missing or not valid")
        site.write_bytes(original)
        config = root / "campaign.json"
        original = config.read_bytes()
        config.write_text("[]", encoding="utf-8")
        self.refused(root, "faq", "campaign.json is missing or not a valid campaign file")
        config.write_bytes(original)
        content = root / "content"
        content.rename(root / "content.moved")
        self.refused(root, "faq", "content/ does not exist")
        content.symlink_to(root / "content.moved", target_is_directory=True)
        self.refused(root, "faq", "content/ must be a real folder")
        result = cli("content", "add-starter", "faq", "--directory", root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Stopped: content/ must be a real folder", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((root / "content.moved" / "faq.json").exists())

    def test_symlinked_campaign_root_is_refused_unchanged(self):
        root = self.older()
        link = self.work / "campaign-link"
        link.symlink_to(root, target_is_directory=True)
        before = snapshot(root)
        for name in TARGETS:
            with self.subTest(name=name):
                with self.assertRaises(StarterError) as caught:
                    add_starter(link, name)
                self.assertIn("campaign folder must be a real folder", str(caught.exception))
                result = cli("content", "add-starter", name, "--directory", link)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Stopped: the campaign folder must be a real folder", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        with self.assertRaises(StarterError):
            starter_status(link)
        result = cli("content", "add-starter", "--list", "--directory", link)
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertEqual(snapshot(root), before)  # nothing was written in the target
        self.assertFalse((root / "content" / "faq.json").exists())
        result = cli("content", "add-starter", "faq", "--directory", root)  # the real folder still works
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_corrupt_resource_or_selfcheck_failure_writes_nothing(self):
        root = self.older()
        broken = self.work / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        for attribute, name in (("FAQ_STARTER", "faq"), ("HELP_MAP_STARTER", "help-map"),
                                ("BACKGROUND_STARTER", "background")):
            for path in (broken, self.work / "missing.json"):
                with self.subTest(name=name, path=path.name), mock.patch.object(content_init, attribute, path):
                    self.refused(root, name, "the packaged starter failed its self-check")
        with mock.patch.object(content_init, "lint", return_value=["degraded"]):
            for name in TARGETS:
                self.refused(root, name, "the packaged starter failed its self-check")
        with mock.patch.object(content_init, "load_pages", return_value=[]):
            self.refused(root, "help-map", "the packaged starter failed its self-check")
        with mock.patch.object(content_init.json, "dumps", side_effect=TypeError("unserializable")):
            self.refused(root, "background", "the packaged starter failed its self-check")
        for name in ("help-map", "faq"):  # an IO failure at publication: no target, no temp, no new pages/
            with self.subTest(io=name), mock.patch.object(content_init.os, "link", side_effect=OSError(5, "I/O error")):
                self.refused(root, name, f"content/{TARGETS[name]} could not be written")
        with mock.patch.object(content_init.os, "fsync", side_effect=OSError(28, "No space left on device")):
            self.refused(root, "help-map", "content/pages/help-map.json could not be written")

    def test_target_arrival_and_parent_swap_never_overwrite(self):
        root = self.older()
        real_link = os.link

        def arrive(source, target, **kwargs):  # someone else writes the target between the check and publication
            (root / "content" / "pages" / "help-map.json").write_bytes(b"arrived first")
            return real_link(source, target, **kwargs)

        with mock.patch.object(content_init.os, "link", arrive):
            with self.assertRaises(StarterError) as caught:
                add_starter(root, "help-map")
        self.assertIn("already exists", str(caught.exception))
        pages = root / "content" / "pages"
        self.assertEqual([p.name for p in pages.iterdir()], ["help-map.json"])  # the arriver only, no temp litter
        self.assertEqual((pages / "help-map.json").read_bytes(), b"arrived first")

        def swap(source, target, **kwargs):  # pages/ is renamed and replaced by a symlink to outside mid-write
            pages.rename(root / "content" / "pages.moved")
            pages.symlink_to(self.outside, target_is_directory=True)
            return real_link(source, target, **kwargs)

        with mock.patch.object(content_init.os, "link", swap):
            done = add_starter(root, "background")
        self.assertEqual(done["written"], "content/pages/background.json")
        self.assertEqual(list(self.outside.iterdir()), [])  # the write stayed in the folder that was opened
        moved = root / "content" / "pages.moved"
        self.assertEqual(sorted(p.name for p in moved.iterdir()), ["background.json", "help-map.json"])
        self.assertEqual((moved / "background.json").read_bytes(),
                         (self.fresh["CA"] / "content" / "pages" / "background.json").read_bytes())

    def test_failure_cleanup_owns_only_empty_new_parent(self):
        root = self.older()
        pages = root / "content" / "pages"
        before = snapshot(root)
        with mock.patch.object(content_init.os, "link", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(StarterError):
                add_starter(root, "help-map")
        self.assertFalse(os.path.lexists(pages))  # its own new empty folder is removed
        self.assertEqual(snapshot(root), before)

        def raced(source, target, **kwargs):  # a file arrives in the new folder, then publication fails
            (pages / "notes.txt").write_bytes(b"theirs")
            raise OSError(5, "I/O error")

        with mock.patch.object(content_init.os, "link", raced):
            with self.assertRaises(StarterError):
                add_starter(root, "help-map")
        self.assertEqual([p.name for p in pages.iterdir()], ["notes.txt"])  # their file and the folder stay
        self.assertEqual((pages / "notes.txt").read_bytes(), b"theirs")
        os.chmod(pages, 0o750)  # a folder that was already there keeps its mode and stays
        before = snapshot(root)
        with mock.patch.object(content_init.os, "link", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(StarterError):
                add_starter(root, "background")
        self.assertEqual(snapshot(root), before)

    # RF2235-3: site.json is validated whole and bounded at 256 KiB (descriptor size and returned bytes)
    LIMIT = 256 * 1024
    SITE_REFUSAL = "content/site.json is missing or not valid (run content-check); nothing was written"

    def sized(self, root, size=None, suffix=b"", document=None):
        """Write a valid site.json padded with trailing whitespace to exactly ``size`` bytes, then ``suffix``."""
        site = root / "content" / "site.json"
        document = document if document is not None else json.loads(site.read_text(encoding="utf-8"))
        data = json.dumps(document, ensure_ascii=False).encode("utf-8")
        if size is not None:
            self.assertLessEqual(len(data), size)
            data += b" " * (size - len(data))
        site.write_bytes(data + suffix)
        return site

    def assert_site_refused(self, root, name, snapshot_root=None, ignore=None):
        """add_starter/starter_status refuse with the one private message; nothing appears anywhere (bar ``ignore``)."""
        def state():
            entries = snapshot(snapshot_root or root.parent)
            entries.pop(ignore, None)
            return entries
        before = state()
        with self.assertRaises(StarterError) as caught:
            starter_status(root) if name is None else add_starter(root, name)
        self.assertEqual(str(caught.exception), self.SITE_REFUSAL)
        self.assertEqual(state(), before)

    def spy_site_read(self, hook=None, fail=False):
        """Record every read of the site.json stream; ``hook`` runs after the real fstat, just before the read."""
        log = {"reads": [], "returned": [], "streams": []}
        real_fdopen = os.fdopen

        class Stream:
            def __init__(self, stream):
                self.stream = stream
                log["streams"].append(stream)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.stream.close()

            def fileno(self):
                return self.stream.fileno()

            def read(self, size=-1):
                log["reads"].append(size)
                if hook:
                    hook()
                if fail:
                    raise OSError(5, "I/O error")
                data = self.stream.read(size)
                log["returned"].append(len(data))
                return data

        def fdopen(fd, mode="r", *args, **kwargs):
            stream = real_fdopen(fd, mode, *args, **kwargs)
            return Stream(stream) if mode == "rb" else stream

        patch = mock.patch.object(content_init.os, "fdopen", fdopen)
        return log, patch

    def test_valid_prefix_with_malformed_suffix_is_refused_by_list_and_all_starters(self):
        suffix = b"!PRIVATE_SUFFIX_SENTINEL"
        for action in ("list", *TARGETS):
            root = self.older(tag="-" + action)
            self.sized(root, self.LIMIT + 1, suffix=suffix)  # a valid object filling the whole old read window
            site_size = (root / "content" / "site.json").stat().st_size
            self.assertGreater(site_size, self.LIMIT)
            before = snapshot(self.work)
            with self.subTest(action=action):
                self.assert_site_refused(root, None if action == "list" else action, self.work)
                args = ("--list",) if action == "list" else (action,)
                result = cli("content", "add-starter", *args, "--directory", root)
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertIn("Stopped: " + self.SITE_REFUSAL, result.stderr)
                self.assertNotIn("PRIVATE_SUFFIX_SENTINEL", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(snapshot(self.work), before)
                self.assertFalse((root / "content" / "pages").exists())

    def test_site_byte_boundary_includes_trailing_whitespace(self):
        for size, admitted in ((self.LIMIT - 1, True), (self.LIMIT, True), (self.LIMIT + 1, False), (self.LIMIT * 4, False)):
            for action in ("list", "faq"):
                with self.subTest(size=size, action=action):
                    root = self.older(tag=f"-{size}-{action}")  # an independent campaign per case: a write never contaminates a refusal
                    self.sized(root, size)
                    if admitted and action == "list":
                        self.assertEqual([row[2] for row in starter_status(root)], [False, False, False])
                    elif admitted:
                        self.assertEqual(add_starter(root, "faq")["written"], "content/faq.json")
                    else:
                        self.assert_site_refused(root, None if action == "list" else action, self.work)

    def test_site_limit_is_utf8_bytes_not_characters(self):
        document = json.loads((self.fresh["CA"] / "content" / "site.json").read_text(encoding="utf-8"))
        document["county_profile"] = {"facts": ["é" * 20000] * 6}  # 2 bytes per character; a text field holds 20000
        root = self.older(tag="-exact")
        site = self.sized(root, self.LIMIT, document=document)
        self.assertEqual(site.stat().st_size, self.LIMIT)
        self.assertLess(len(site.read_text(encoding="utf-8")), self.LIMIT)
        self.assertEqual(add_starter(root, "faq")["written"], "content/faq.json")  # exactly the limit in bytes
        document["county_profile"] = {"facts": ["é" * 20000] * 7}  # fewer characters than the limit, more bytes than the limit
        root = self.older(tag="-over")
        site = self.sized(root, None, document=document)
        self.assertLess(len(site.read_text(encoding="utf-8")), self.LIMIT)
        self.assertGreater(site.stat().st_size, self.LIMIT)
        self.assert_site_refused(root, "faq", self.work)
        self.assert_site_refused(root, None, self.work)

    def test_oversized_site_fstat_refuses_before_any_read_or_parse(self):
        root = self.older()
        self.sized(root, self.LIMIT + 1)
        log, patch = self.spy_site_read()
        with patch, mock.patch.object(content_init, "load_site") as loaded, \
                mock.patch.object(content_init, "starter_text") as text:
            self.assert_site_refused(root, "faq", self.work)
            self.assert_site_refused(root, None, self.work)
        self.assertEqual(log["reads"], [])
        loaded.assert_not_called()
        text.assert_not_called()
        self.assertTrue(log["streams"] and all(stream.closed for stream in log["streams"]))

    def test_site_growth_after_real_fstat_is_refused_before_parse(self):
        for name in (None, "faq", "help-map", "background"):
            root = self.older(tag=f"-{name}")
            site = self.sized(root)  # a small valid site: its real fstat is in bound

            def grow(site=site):
                with open(site, "ab") as handle:  # the same inode, after the real fstat, before the read
                    handle.write(b" " * (self.LIMIT + 10) + b"!PRIVATE_SUFFIX_SENTINEL")

            log, patch = self.spy_site_read(hook=grow)
            with self.subTest(name=name), patch, mock.patch.object(content_init, "load_site") as loaded, \
                    mock.patch.object(content_init, "starter_text") as text:
                self.assert_site_refused(root, name, self.work, ignore=str(site.relative_to(self.work)))  # only the grown site differs
                self.assertEqual(log["reads"], [self.LIMIT + 1])
                self.assertEqual(log["returned"], [self.LIMIT + 1])
                loaded.assert_not_called()
                text.assert_not_called()
                self.assertTrue(all(stream.closed for stream in log["streams"]))

    def test_site_admission_failure_closes_descriptor_and_preserves_private_refusal(self):
        document = json.loads((self.fresh["CA"] / "content" / "site.json").read_text(encoding="utf-8"))
        broken = dict(document, schema_version=2)
        good = json.dumps(document).encode("utf-8")
        cases = {"invalid-utf8": b"\xff\xfe" + good, "truncated": good[:-5], "extra-data": good + b" {}",
                 "schema": json.dumps(broken).encode("utf-8"), "empty": b"", "oversize": good + b" " * self.LIMIT}
        for label, data in cases.items():
            for name in (None, "background"):
                root = self.older(tag=f"-{label}-{name}")
                (root / "content" / "site.json").write_bytes(data)
                log, patch = self.spy_site_read()
                with self.subTest(case=label, name=name), patch:
                    self.assert_site_refused(root, name, self.work)
                    self.assertTrue(all(stream.closed for stream in log["streams"]))
                    self.assertFalse((root / "content" / "pages").exists())
        root = self.older(tag="-readerror")  # a read error is the same refusal, descriptor closed
        log, patch = self.spy_site_read(fail=True)
        with patch:
            self.assert_site_refused(root, "faq", self.work)
        self.assertTrue(log["streams"] and all(stream.closed for stream in log["streams"]))
        for kind in ("symlink", "fifo"):  # the no-follow, regular-file-only open is kept
            root = self.older(tag="-" + kind)
            site = root / "content" / "site.json"
            original = site.read_bytes()
            site.unlink()
            if kind == "symlink":
                (self.outside / "site.json").write_bytes(original)
                site.symlink_to(self.outside / "site.json")
            else:
                os.mkfifo(site)
            with self.subTest(kind=kind):
                self.assert_site_refused(root, "faq", self.work)
        root = self.older(tag="-accepted")  # in-bound valid input is still accepted and its descriptor closed
        log, patch = self.spy_site_read()
        with patch:
            self.assertEqual(add_starter(root, "faq")["written"], "content/faq.json")
        self.assertEqual(log["reads"], [self.LIMIT + 1])
        self.assertTrue(all(stream.closed for stream in log["streams"]))

    def test_no_command_no_public_byte_change(self):
        root = self.older()
        for _ in range(2):
            result = cli("build", "--directory", root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        public = snapshot(root / "public")
        self.assertNotIn("help-map.html", public)
        result = cli("build", "--directory", root)
        self.assertEqual((result.returncode, snapshot(root / "public")), (0, public))
        result = cli("content", "add-starter", "help-map", "--directory", root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], WARNING)
        self.assertEqual(snapshot(root / "public"), public)  # nothing was built
        result = cli("build", "--directory", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Help map the cameras", (root / "public" / "help-map.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
