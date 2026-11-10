"""RF2315-1: personal-data register and --find refuse over-deep private trees with the fixed incomplete refusal.

Synthetic real trees only. Deep chains of one-letter directories are made and removed with plain level-by-level
calls so neither setup nor cleanup recurses past the interpreter limit.
"""
import os
import re
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool import personal_data  # noqa: E402
from tests.test_personal_data import Base, put, run  # noqa: E402

INCOMPLETE = "Stopped: personal-data inventory incomplete; review private folders locally\n"
OVERDEEP = 1100  # well past the default recursion limit of 1000, far below MAX_ENTRIES
DOCS = Path(__file__).resolve().parents[1] / "docs" / "DATA-HANDLING.md"
CLI_MODES = ((), ("--json",), ("--find", "findme"), ("--find", "findme", "--json"))


def chain(base, depth):
    """base/d/d/.../d (``depth`` levels), made one level at a time; the path stays far below PATH_MAX."""
    here = str(base)
    for _ in range(depth):
        here += "/d"
        os.mkdir(here)
    return Path(here)


def unchain(base, depth, leaf=None):
    if leaf:
        os.unlink(str(base) + "/d" * depth + "/" + leaf)
    for level in range(depth, 0, -1):
        os.rmdir(str(base) + "/d" * level)


def snapshot(root):
    """Every path's kind, mode, mtime and size (atime ignored); os.walk is iterative."""
    seen = {}
    for here, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = os.path.join(here, name)
            st = os.lstat(path)
            seen[os.path.relpath(path, root)] = (stat.S_IFMT(st.st_mode), st.st_mode, st.st_mtime_ns, st.st_size)
    return seen


class ScannerOs:
    """The scanner's view of ``os``: records only the descriptors, parents and scans of personal_data itself."""

    def __init__(self):
        self.live, self.problems, self.opened, self.scanned = set(), [], [], []

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, name, flags, *args, dir_fd=None, **kwargs):
        parent = os.fstat(dir_fd).st_ino if dir_fd is not None else None
        fd = os.open(name, flags, *args, dir_fd=dir_fd, **kwargs)
        if fd in self.live:
            self.problems.append("descriptor reused while open")
        self.live.add(fd)
        self.opened.append((parent, str(name)))
        return fd

    def close(self, fd):
        if fd not in self.live:
            self.problems.append("closed a descriptor the scanner does not own")
        self.live.discard(fd)
        os.close(fd)

    def scandir(self, fd):
        self.scanned.append(os.fstat(fd).st_ino)
        return os.scandir(fd)


class DepthTests(Base):
    def campaign(self, name, top, depth, leaf=None):
        """A campaign folder ``tmp/name`` holding ``top``/d^depth (and a file ``leaf`` in the deepest directory)."""
        root = self.tmp / name
        (root / top).mkdir(parents=True)
        end = chain(root / top, depth)
        self.addCleanup(unchain, root / top, depth, leaf)
        if leaf:
            (end / leaf).write_bytes(b"x")
        return root, end

    def test_overdeep_real_tree_refuses_all_cli_modes(self):
        put(self.root, "kit/assignments.json", {"schema_version": 1, "assignments": {"agency-a": "Findme Person"}})
        (self.root / "private").mkdir()
        chain(self.root / "private", OVERDEEP)
        self.addCleanup(unchain, self.root / "private", OVERDEEP)
        self.assertGreater(OVERDEEP, sys.getrecursionlimit())
        for extra in CLI_MODES:
            with self.subTest(extra=extra):
                self.assertEqual(run("personal-data", "--directory", self.root, *extra), (1, "", INCOMPLETE))

    def test_exact_depth_boundary_in_each_root(self):
        for top in ("kit", "private"):
            for depth in (0, 63, 64):
                with self.subTest(top=top, depth=depth):
                    root, _end = self.campaign(f"ok-{top}-{depth}", top, depth, leaf="u.txt")
                    expected = "/".join([top] + ["d"] * depth + ["u.txt"])
                    report = personal_data.personal_data_register(root)
                    self.assertEqual([row["path"] for row in report["files"] if row["kind"] == "unknown"], [expected])
                    found = personal_data.personal_data_find(root, "findme")
                    self.assertEqual([(row["path"], row["file_position"]) for row in found["files"]
                                      if row["kind"] == "unknown"], [(top + "/<unknown>", 1)])
                    for extra in CLI_MODES:
                        code, out, err = run("personal-data", "--directory", root, *extra)
                        self.assertEqual((code, err), (0, ""))
                        self.assertIn(top, out)
            for depth in (65, 66):  # the first real directory needing descent to depth 65 refuses, even when empty
                with self.subTest(top=top, depth=depth):
                    root, _end = self.campaign(f"over-{top}-{depth}", top, depth)
                    for call in (personal_data.personal_data_register,
                                 lambda path: personal_data.personal_data_find(path, "findme")):
                        with self.assertRaises(personal_data.PersonalDataError) as caught:
                            call(root)
                        self.assertEqual(str(caught.exception), personal_data.INCOMPLETE)
                    for extra in CLI_MODES:
                        self.assertEqual(run("personal-data", "--directory", root, *extra), (1, "", INCOMPLETE))

    def test_refused_child_is_never_opened_or_scanned(self):
        root, end = self.campaign("never-opened", "kit", 65)
        depth64, depth63 = end.parent, end.parent.parent
        inode64, inode63, inode65 = (os.stat(path).st_ino for path in (depth64, depth63, end))
        scanner = ScannerOs()
        with mock.patch.object(personal_data, "os", scanner):
            with self.assertRaises(personal_data.PersonalDataError):
                personal_data.personal_data_register(root)
        self.assertIn((inode63, "d"), scanner.opened)  # descent up to depth 64 really happened
        self.assertIn(inode64, scanner.scanned)
        self.assertNotIn((inode64, "d"), scanner.opened)  # the over-limit child was never opened ...
        self.assertNotIn(inode65, scanner.scanned)  # ... or scanned

    def test_depth_refusal_closes_owned_descriptors_and_never_mutates(self):
        for label, depth in (("success", 64), ("refusal", 65)):
            with self.subTest(label=label):
                root, _end = self.campaign(f"fd-{label}", "private", depth)
                put(root, "kit/assignments.json", {"schema_version": 1, "assignments": {"agency-a": "x"}})
                before = snapshot(root)
                scanner = ScannerOs()
                with mock.patch.object(personal_data, "os", scanner):
                    try:
                        personal_data.personal_data_register(root)
                        outcome = "success"
                    except personal_data.PersonalDataError:
                        outcome = "refusal"
                self.assertEqual(outcome, label)
                self.assertGreater(len(scanner.opened), 60)
                self.assertEqual((scanner.problems, scanner.live), ([], set()))
                self.assertEqual(snapshot(root), before)

    def test_depth_refusal_withholds_names_query_and_partial_matches(self):
        put(self.root, "kit/assignments.json", {"schema_version": 1, "assignments": {"agency-a": "Findme Person"}})
        marker = "zz-SENTINEL-NAME-Q7"  # walked after assignments.json, which has already matched the query
        base = self.root / "kit" / marker
        base.mkdir()
        chain(base, 65)
        self.addCleanup(unchain, base, 65)
        for extra in CLI_MODES:
            with self.subTest(extra=extra):
                code, out, err = run("personal-data", "--directory", self.root, *extra)
                self.assertEqual((code, out, err), (1, "", INCOMPLETE))
                for leak in (marker, str(self.root), "findme", "Findme", "agency-a"):
                    self.assertNotIn(leak, out + err)

    def test_depth_guard_preserves_classification_links_order_and_entry_budget(self):
        # a catalog path occupied by a directory is an unreadable recognized store and is never descended
        catalog_dir = self.root / "kit" / "assignments.json"
        catalog_dir.mkdir(parents=True)
        chain(catalog_dir, 70)
        self.addCleanup(unchain, catalog_dir, 70)
        # an unknown symlink is listed, never followed, whatever its target's depth
        outside = self.tmp / "outside"
        outside.mkdir()
        chain(outside, 70)
        self.addCleanup(unchain, outside, 70)
        (self.root / "kit" / "link").symlink_to(outside)
        (self.root / "kit" / "z.txt").write_bytes(b"x")
        put(self.root, "private/b/a.txt", b"x")
        put(self.root, "private/a.txt", b"x")
        report = personal_data.personal_data_register(self.root)
        rows = {(row["kind"], row["path"]): row for row in report["files"]}
        self.assertEqual(rows[("volunteer_assignments", "kit/assignments.json")]["status"], "unreadable")
        self.assertEqual([row["path"] for row in report["files"] if row["kind"] == "unknown"],
                         ["kit/link", "kit/z.txt", "private/a.txt", "private/b/a.txt"])
        counts = {}
        for row in personal_data.personal_data_find(self.root, "findme")["files"]:
            if row["kind"] == "unknown":
                counts[row["path"]] = counts.get(row["path"], 0) + 1
        self.assertEqual(counts, {"kit/<unknown>": 2, "private/<unknown>": 2})

        # the entry budget is one count across both roots, not reset per root
        small = self.tmp / "small"
        for top in ("kit", "private"):
            for name in ("a.txt", "b.txt", "c.txt"):
                put(small, f"{top}/{name}", b"x")
        with mock.patch.object(personal_data, "MAX_ENTRIES", 6):
            personal_data.personal_data_register(small)
        with mock.patch.object(personal_data, "MAX_ENTRIES", 5):
            with self.assertRaises(personal_data.PersonalDataError):
                personal_data.personal_data_register(small)
            self.assertEqual(run("personal-data", "--directory", small, "--find", "findme"), (1, "", INCOMPLETE))

    def test_accepted_trees_preserve_legacy_bytes_and_documented_depth_contract(self):
        # legacy text/JSON bytes stay pinned by tests.test_personal_data (LEGACY_TEXT / LEGACY_JSON), unchanged
        self.assertEqual((personal_data.MAX_DEPTH, personal_data.MAX_ENTRIES), (64, 10000))
        text = re.sub(r"\s+", " ", DOCS.read_text(encoding="utf-8")).lower()
        for phrase in ("more than 10,000 entries in total", "nested more than 64 levels below `kit/` or `private/`",
                       "register and `--find`", "no partial report", "left untouched", "private local review",
                       "do not delete or move anything"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
