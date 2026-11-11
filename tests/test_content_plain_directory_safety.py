"""RF1315-2: `content check-plain` reads only through descriptors for its admitted content/ and pages/ folders.

Synthetic data only. Run from the repository root: python3 -B -m unittest tests.test_content_plain_directory_safety -v
"""
import contextlib
import errno
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, content_plain, plain_language  # noqa: E402

OUTSIDE_WARNING = "QZX"
LOCAL_WARNING = "ALPR"


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-content-plain-dir-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.root = self.base / "campaign"
        self.content = self.root / "content"
        write(self.root / "campaign.json", {"schema_version": 1, "name": "Synthetic", "county": "Synthetic", "state": "CA"})
        write(self.content / "pages" / "a.json", {"title": "Local", "body_md": f"The {LOCAL_WARNING}."})
        write(self.content / "faq.json", {"entries": [{"question": "Q", "answer_md": f"The {LOCAL_WARNING}."}]})
        write(self.content / "glossary.json", {"terms": [{"term": "T", "definition_md": "Plain."}]})
        # An outside tree with the same names whose abbreviation would show up under a local label.
        self.outside = self.base / "outside"
        write(self.outside / "pages" / "a.json", {"title": "Out", "body_md": f"The {OUTSIDE_WARNING}."})
        write(self.outside / "faq.json", {"entries": [{"question": "Q", "answer_md": f"The {OUTSIDE_WARNING}."}]})
        write(self.outside / "glossary.json", {"terms": [{"term": "T", "definition_md": f"The {OUTSIDE_WARNING}."}]})
        self.outside_inodes = {p.stat().st_ino for p in self.outside.rglob("*")} | {self.outside.stat().st_ino}
        self.expected = content_plain.check_content(self.root)
        self.assertEqual({row["abbreviation"] for row in self.expected["warnings"]}, {LOCAL_WARNING})

    def swap(self, path, how, other=None):
        """Rename ``path`` aside and put a replacement of kind ``how`` in its place (test-owned actor)."""
        path.rename(path.with_name("held-" + path.name))
        if how == "symlink":
            path.symlink_to(other or self.outside)
        elif how == "other-dir":
            path.mkdir()
            for child in (other or self.outside).iterdir():
                if child.is_file():
                    (path / child.name).write_bytes(child.read_bytes())
        elif how == "file":
            path.write_bytes(b"{}")
        elif how == "fifo":
            os.mkfifo(path)

    def restore(self, path, how):
        if how == "other-dir":
            for child in path.iterdir():
                child.unlink()
            path.rmdir()
        else:
            path.unlink()
        path.with_name("held-" + path.name).rename(path)

    @contextlib.contextmanager
    def hook(self, function_name, trigger, action):
        """Run ``action`` once, immediately before the real ``os.<function_name>`` call that ``trigger`` accepts."""
        real = getattr(os, function_name)
        fired = []

        def wrapper(*args, **kwargs):
            if not fired and trigger(*args, **kwargs):
                fired.append(True)
                action()
            return real(*args, **kwargs)

        with mock.patch.object(content_plain.os, function_name, side_effect=wrapper):
            yield fired

    @contextlib.contextmanager
    def watching_outside_reads(self):
        real_read, hits = os.read, []

        def spy(fd, count):
            if os.fstat(fd).st_ino in self.outside_inodes:
                hits.append(fd)
            return real_read(fd, count)

        with mock.patch.object(content_plain.os, "read", side_effect=spy):
            yield hits
        self.assertEqual(hits, [], "an outside file was read")

    def run_cli(self, as_json):
        argv = ["campaign_tool", "content", "check-plain", "--directory", str(self.root)] + (["--json"] if as_json else [])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
                code = 0
            except SystemExit as stopped:
                code = stopped.code
        return code, out.getvalue(), err.getvalue()


class AdmissionSwapTests(Case):
    def refused(self, label, **kwargs):
        with self.assertRaises(content_plain.ContentError) as caught:
            content_plain.check_content(self.root)
        self.assertEqual(str(caught.exception), f"{label}: could not be read")
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_pages_swap_before_directory_open_never_reads_external_tree(self):
        pages = self.content / "pages"
        for how in ("symlink", "other-dir", "file", "fifo"):
            with self.subTest(how=how), self.watching_outside_reads(), \
                    mock.patch.object(content_plain.os, "listdir", wraps=os.listdir) as listing, \
                    self.hook("open", lambda path, flags, *a, **k: path == "pages" and flags & os.O_DIRECTORY,
                              lambda: self.swap(pages, how)) as fired:
                self.refused("content/pages")
                self.assertTrue(fired)
                self.assertEqual([c for c in listing.call_args_list if isinstance(c.args[0], int)], [])
                self.restore(pages, how)
                self.assertEqual(content_plain.check_content(self.root), self.expected)

    def test_content_swap_before_directory_open_refuses_all_surfaces_in_both_cli_modes(self):
        for how in ("symlink", "other-dir", "file", "fifo"):
            with self.subTest(how=how):
                (self.outside / "pages" / "b.json").write_text('{"title": "t", "body_md": "b"}', encoding="utf-8")
                with self.watching_outside_reads(), \
                        self.hook("open", lambda path, flags, *a, **k: path == "content" and flags & os.O_DIRECTORY,
                                  lambda: self.swap(self.content, how)):
                    self.refused("content")
                self.restore(self.content, how)
                for as_json in (False, True):
                    with self.hook("open", lambda path, flags, *a, **k: path == "content" and flags & os.O_DIRECTORY,
                                   lambda: self.swap(self.content, how)), \
                            mock.patch.object(plain_language, "check_fields") as checking:
                        code, out, err = self.run_cli(as_json)
                    self.restore(self.content, how)
                    self.assertEqual((code, out, err), (1, "", "Stopped: content: could not be read\n"))
                    checking.assert_not_called()
                    self.assertNotIn(str(self.base), err)
                (self.outside / "pages" / "b.json").unlink()

    def test_static_links_keep_their_distinct_diagnostic(self):
        self.swap(self.content / "pages", "symlink")
        with self.assertRaises(content_plain.ContentError) as caught:
            content_plain.check_content(self.root)
        self.assertEqual(str(caught.exception), "content/pages: must be a real folder, not a symlink or file")

    def test_admission_errors_are_fixed_private_and_narrow(self):
        real_open = os.open
        for label, name in (("content", "content"), ("content/pages", "pages")):
            for number in (errno.EACCES, errno.ENOENT, errno.ENOTDIR, errno.EIO):
                error = OSError(number, "PRIVATE-LINE\nPRIVATE", str(self.base / "SECRET"))

                def failing(path, flags, *args, name=name, error=error, **kwargs):
                    if path == name and flags & os.O_DIRECTORY:
                        raise error
                    return real_open(path, flags, *args, **kwargs)

                with self.subTest(label=label, number=number), \
                        mock.patch.object(content_plain.os, "open", side_effect=failing):
                    self.refused(label)
        with mock.patch.object(content_plain.os, "open", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                content_plain.check_content(self.root)

    def test_missing_root_content_and_pages_stay_optional(self):
        empty = {"advisory": True, "warnings": []}
        self.assertEqual(content_plain.check_content(self.base / "nothing-here"), empty)
        bare = self.base / "bare"
        bare.mkdir()
        self.assertEqual(content_plain.check_content(bare), empty)
        (bare / "content").mkdir()
        self.assertEqual(content_plain.check_content(bare), empty)

    def test_observed_leaf_disappearance_is_unreadable_not_skipped(self):
        real_stat = os.stat
        faq = self.content / "faq.json"

        def stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == "faq.json" and faq.exists():
                faq.unlink()
            return info

        with mock.patch.object(content_plain.os, "stat", side_effect=stat):
            with self.assertRaises(content_plain.ContentError) as caught:
                content_plain.check_content(self.root)
        self.assertEqual(str(caught.exception), "content/faq.json: could not be read")


class AdmittedDescriptorTests(Case):
    def test_pages_descriptor_survives_swap_before_listdir_and_before_leaf_open(self):
        pages = self.content / "pages"
        triggers = {
            "listdir": ("listdir", lambda *a, **k: True),
            "leaf-open": ("open", lambda path, flags, *a, **k: path == "a.json" and not flags & os.O_DIRECTORY),
        }
        for when, (function, trigger) in triggers.items():
            for how in ("symlink", "other-dir"):
                with self.subTest(when=when, how=how), self.watching_outside_reads(), \
                        self.hook(function, trigger, lambda: self.swap(pages, how)) as fired:
                    self.assertEqual(content_plain.check_content(self.root), self.expected)
                    self.assertTrue(fired)
                    self.restore(pages, how)

    def test_content_descriptor_keeps_faq_glossary_and_aliases_local(self):
        write(self.content / "glossary.json", {"terms": [{"term": "T", "definition_md": "Plain.", "aliases": [LOCAL_WARNING]}]})
        expected = content_plain.check_content(self.root)
        self.assertEqual(expected["warnings"], [])
        (self.outside / "glossary.json").write_text(json.dumps({"terms": [{"definition_md": "Plain."}]}), encoding="utf-8")
        triggers = (lambda path, flags, *a, **k: path == "a.json" and not flags & os.O_DIRECTORY,
                    lambda path, flags, *a, **k: path == "faq.json" and not flags & os.O_DIRECTORY,
                    lambda path, flags, *a, **k: path == "glossary.json" and not flags & os.O_DIRECTORY)
        for index, trigger in enumerate(triggers):
            for how in ("symlink", "other-dir"):
                with self.subTest(index=index, how=how), self.watching_outside_reads(), \
                        self.hook("open", trigger, lambda: self.swap(self.content, how)) as fired:
                    self.assertEqual(content_plain.check_content(self.root), expected)
                    self.assertTrue(fired)
                    self.restore(self.content, how)

    def test_missing_local_optional_leaf_is_not_rediscovered_outside(self):
        (self.content / "glossary.json").unlink()
        expected = content_plain.check_content(self.root)
        for how in ("symlink", "other-dir"):
            with self.subTest(how=how), self.watching_outside_reads(), \
                    self.hook("open", lambda path, flags, *a, **k: path == "faq.json" and not flags & os.O_DIRECTORY,
                              lambda: self.swap(self.content, how)):
                self.assertEqual(content_plain.check_content(self.root), expected)
                self.restore(self.content, how)

    def test_actual_cli_post_admission_swap_keeps_exact_report_and_recovers(self):
        code, out, err = self.run_cli(False)
        healthy_text = (code, out, err)
        code, out, err = self.run_cli(True)
        healthy_json = (code, out, err)
        self.assertEqual(healthy_json[0], 0)
        self.assertEqual(json.loads(healthy_json[1]), self.expected)
        before = {p: p.read_bytes() for p in self.outside.rglob("*.json")}
        for as_json, healthy in ((False, healthy_text), (True, healthy_json)):
            with self.watching_outside_reads(), \
                    self.hook("listdir", lambda *a, **k: True, lambda: self.swap(self.content / "pages", "symlink")):
                self.assertEqual(self.run_cli(as_json), healthy)
            self.restore(self.content / "pages", "symlink")
            self.assertEqual(self.run_cli(as_json), healthy)
        self.assertEqual({p: p.read_bytes() for p in self.outside.rglob("*.json")}, before)


@unittest.skipUnless(os.path.isdir("/proc/self/fd"), "needs /proc/self/fd")
class DescriptorLifecycleTests(Case):
    def open_fds(self):
        return sorted(os.listdir("/proc/self/fd"))

    def test_every_acquisition_is_closed_and_opens_are_read_only(self):
        real_open, flags_seen = os.open, []

        def spy(path, flags, *args, **kwargs):
            flags_seen.append(flags)
            return real_open(path, flags, *args, **kwargs)

        sentinel = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, sentinel)
        forbidden = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_EXCL
        cases = {"success": lambda: None,
                 "bad-json": lambda: (self.content / "faq.json").write_bytes(b"{"),
                 "too-large": lambda: (self.content / "glossary.json").write_bytes(b" " * (content_plain.MAX_JSON_BYTES + 1)),
                 "schema": lambda: write(self.content / "pages" / "a.json", {"title": 1}),
                 "deep": lambda: (self.content / "faq.json").write_bytes(b"[" * 20000 + b"]" * 20000),
                 "no-pages": lambda: (self.content / "pages" / "a.json").unlink() or (self.content / "pages").rmdir()}
        for name, prepare in cases.items():
            with self.subTest(case=name):
                prepare()
                before = self.open_fds()
                flags_seen.clear()
                with mock.patch.object(content_plain.os, "open", side_effect=spy):
                    try:
                        content_plain.check_content(self.root)
                    except content_plain.ContentError:
                        pass
                self.assertEqual(self.open_fds(), before)
                self.assertTrue(flags_seen)
                self.assertTrue(all(flag & forbidden == 0 for flag in flags_seen))
                self.assertIn(sentinel, [int(n) for n in self.open_fds()])
                setUp_tree = self.base / "reset"
                del setUp_tree
                self.setUp()

    def test_engine_failure_still_closes_everything(self):
        before = self.open_fds()
        with mock.patch.object(plain_language, "check_fields", side_effect=RuntimeError("engine")):
            with self.assertRaises(RuntimeError):
                content_plain.check_content(self.root)
        self.assertEqual(self.open_fds(), before)


if __name__ == "__main__":
    unittest.main()
