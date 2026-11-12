"""RF1801-4: the source-bundle planner admits selected copies (regular, non-symlink) before any size skip (synthetic, no network)."""
import contextlib
import errno
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from campaign_tool import cli, source_bundle
from campaign_tool.content import ContentError
from campaign_tool.site import build_site, preview_site
from campaign_tool.source_bundle import CHANGED, NOTE_EMPTY, NOTE_OVERSIZE, bundle_bytes, bundle_selection, plan_bundle, rebuild_from_staged
from tests.test_source_bundle import BASE, Fixture, entry

SENTINEL = "SECRET-OS-TEXT-sentinel"


class PlannerBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-plan-metadata-")
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.big = self.dir / "big.bin"
        self.big.write_bytes(b"B" * 4096)
        self.small = self.dir / "small.pdf"
        self.small.write_bytes(b"PUBLIC-COPY")
        self.sources = [entry(1, BASE + "/docs/a.pdf", "Alpha"), entry(2, BASE + "/docs/b.pdf", "Beta")]

    def run_plan(self, a, b=None, notes=None, max_bytes=None, sources=None):
        assets = [(a, Path("docs/a.pdf"))] + ([(b, Path("docs/b.pdf"))] if b is not None else [])
        return plan_bundle(sources or self.sources, BASE, assets, {}, notes, max_bytes)

    def assert_changed(self, call):
        with self.assertRaises(ContentError) as caught:
            call()
        self.assertEqual(str(caught.exception), CHANGED)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__ or caught.exception.__context__ is None)
        return caught.exception

    def forbid_payload(self):
        """Fail the test if any payload is opened/read or a ZIP is allocated."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch("os.open", side_effect=AssertionError("payload opened")))
        stack.enter_context(mock.patch("os.read", side_effect=AssertionError("payload read")))
        stack.enter_context(mock.patch.object(source_bundle, "read_copy", side_effect=AssertionError("read_copy called")))
        stack.enter_context(mock.patch.object(source_bundle, "bundle_bytes", side_effect=AssertionError("zip allocated")))
        self.addCleanup(stack.close)
        return stack


class PlannerAdmissionTests(PlannerBase):
    def test_missing_copy_is_fixed_changed_before_read(self):
        notes = ["earlier note"]
        for missing in (self.dir / "gone.pdf", self.dir / "no-parent" / "deeper" / "a.pdf"):
            with self.subTest(missing=missing.name), self.forbid_payload():
                error = self.assert_changed(lambda: self.run_plan(missing, notes=notes))
                self.assertNotIn(str(self.dir), str(error))
                self.assertNotIn("No such file", str(error))
        self.assertEqual(notes, ["earlier note"])  # no new note, nothing reinterpreted as oversize

    def test_nonregular_cannot_hide_behind_oversize(self):
        link, dangling, directory, fifo = (self.dir / name for name in ("link", "dangling", "dir", "fifo"))
        os.symlink(self.big, link)
        os.symlink(self.dir / "nowhere", dangling)
        directory.mkdir()
        os.mkfifo(fifo)
        for bad in (link, dangling, directory, fifo):
            notes = []
            with self.subTest(bad=bad.name), self.forbid_payload():
                self.assert_changed(lambda: self.run_plan(bad, notes=notes, max_bytes=1))  # cap=1: the old code skipped as oversize
                self.assertEqual(notes, [])
        notes = []
        with self.forbid_payload():  # an invalid later member cannot hide behind an earlier oversized regular file
            self.assert_changed(lambda: self.run_plan(self.big, link, notes=notes, max_bytes=1))
            self.assert_changed(lambda: self.run_plan(self.big, fifo, notes=notes, max_bytes=1))
        self.assertEqual(notes, [])

    def test_metadata_oserrors_are_value_free(self):
        real_lstat = os.lstat
        target = str(self.small)
        failures = (PermissionError(errno.EACCES, SENTINEL + "-perm", target),
                    OSError(errno.ELOOP, SENTINEL + "-loop", target), OSError(SENTINEL + "-generic " + target))
        for failure in failures:
            notes = ["earlier note"]

            def lstat(path, *a, failure=failure, **k):
                if str(path) == target:
                    raise failure
                return real_lstat(path, *a, **k)
            with self.subTest(failure=type(failure).__name__, errno=failure.errno), mock.patch("os.lstat", lstat), self.forbid_payload():
                error = self.assert_changed(lambda: self.run_plan(self.small, notes=notes))
                self.assertNotIn(SENTINEL, str(error))
                self.assertNotIn(target, str(error))
            self.assertEqual(notes, ["earlier note"])
        # the paired real failure, not only an injected one
        self.small.unlink()
        self.assert_changed(lambda: self.run_plan(self.small))

    def test_genuine_oversize_and_empty_keep_no_read_contract(self):
        notes = ["earlier note"]
        with self.forbid_payload():
            self.assertIsNone(self.run_plan(self.big, self.small, notes=notes, max_bytes=1))
        self.assertEqual(notes, ["earlier note", NOTE_OVERSIZE])  # exactly one new note
        notes = []
        with mock.patch("os.lstat", side_effect=AssertionError("metadata for an empty selection")), self.forbid_payload():
            self.assertIsNone(plan_bundle([entry(9, "https://example.invalid/x.pdf")], BASE, [(self.big, Path("docs/a.pdf"))], {}, notes))
            self.assertIsNone(plan_bundle(self.sources, BASE, [], {}, notes))
        self.assertEqual(notes, [NOTE_EMPTY, NOTE_EMPTY])
        # independent final-size controls: the cap is inclusive
        archive = self.run_plan(self.small)
        self.assertEqual(self.run_plan(self.small, max_bytes=len(archive)), archive)
        notes = []
        self.assertIsNone(self.run_plan(self.small, notes=notes, max_bytes=len(archive) - 1))
        self.assertEqual(notes, [NOTE_OVERSIZE])

    def test_duplicate_rows_keep_manifest_multiplicity(self):
        sources = [entry(1, BASE + "/docs/a.pdf", "Alpha"), entry(2, BASE + "/docs/a.pdf#page=2", "Alpha again")]
        assets = [(self.small, Path("docs/a.pdf"))]
        real_lstat, real_read, target = os.lstat, source_bundle.read_copy, str(self.small)
        lstats, at_read = [], []

        def lstat(path, *a, **k):
            if str(path) == target:
                lstats.append(path)
            return real_lstat(path, *a, **k)

        def read(source, size):
            at_read.append(len(lstats))  # planner lookups made before the reader's own inspection
            return real_read(source, size)
        with mock.patch("os.lstat", lstat), mock.patch.object(source_bundle, "read_copy", read):
            data = plan_bundle(sources, BASE, assets, {})
        self.assertEqual(at_read, [1])  # one planner lookup, one read_copy
        self.assertEqual(len(lstats), 2)  # plus the reader's own independent lstat
        rows = bundle_selection(sources, BASE, {"docs/a.pdf": self.small})
        self.assertEqual(len(rows), 2)
        self.assertEqual(data, bundle_bytes(rows, {"docs/a.pdf": b"PUBLIC-COPY"}))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["files/docs/a.pdf", "manifest.csv"])  # one member
            self.assertEqual(archive.read("manifest.csv").decode("utf-8").count("/docs/a.pdf"), 2)  # two manifest rows

    def test_healthy_bytes_and_later_reader_refusal_unchanged(self):
        empty = self.dir / "empty.pdf"
        empty.write_bytes(b"")
        payloads = {"docs/a.pdf": b"PUBLIC-COPY", "docs/b.pdf": b""}
        for sources in (self.sources, self.sources[::-1]):
            rows = bundle_selection(sources, BASE, payloads)
            self.assertEqual(self.run_plan(self.small, empty, sources=sources), bundle_bytes(rows, payloads))
        real_read = source_bundle.read_copy

        def grow(source, size):  # grows after the planner's admission: the safe reader still refuses
            with open(source, "ab") as handle:
                handle.write(b"+")
            return real_read(source, size)

        def substitute(source, size):
            os.unlink(source)
            os.symlink(self.big, source)
            return real_read(source, size)
        for late in (grow, substitute):
            self.small.write_bytes(b"PUBLIC-COPY")
            with self.subTest(late=late.__name__), mock.patch.object(source_bundle, "read_copy", late):
                self.assert_changed(lambda: self.run_plan(self.small))
            if os.path.lexists(self.small):
                self.small.unlink()


class PipelineTests(Fixture):
    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in self.public.rglob("*") if p.is_file()}

    def residue(self):
        return [p.name for p in self.root.iterdir() if p.name.startswith(".public.")]

    def park(self, victim):
        aside = victim.with_name(victim.name + ".aside")
        victim.rename(aside)
        return aside

    def arm_planner(self, victim):
        """Remove ``victim`` right before the real planner runs (after the earlier content/copy checks)."""
        real, parked = source_bundle.plan_bundle, []

        def forwarding(*args, **kwargs):
            if not parked:
                parked.append(self.park(victim))
            return real(*args, **kwargs)
        return mock.patch.object(source_bundle, "plan_bundle", forwarding), parked

    def test_staged_metadata_failure_preserves_public(self):
        build_site(self.root)
        before = self.snapshot()
        staged_name = []
        real = source_bundle.rebuild_from_staged

        def parking(sources, base_url, relatives, staged_root, *rest, **kw):
            staged = Path(staged_root) / "docs" / "a.pdf"  # after the caller's catalogue reads
            staged_name.append(self.park(staged))
            return real(sources, base_url, relatives, staged_root, *rest, **kw)
        with mock.patch.object(source_bundle, "rebuild_from_staged", parking), self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertEqual(str(caught.exception), CHANGED)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(len(staged_name), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.residue(), [])
        # direct staged metadata failure, with a healthy control
        sources = [entry(1, BASE + "/docs/a.pdf", "Alpha")]
        staged = self.root / "staged-direct"
        (staged / "docs").mkdir(parents=True)
        shutil.copyfile(self.content / "public-assets" / "docs" / "a.pdf", staged / "docs" / "a.pdf")
        expected = plan_bundle(sources, BASE, [(staged / "docs" / "a.pdf", "docs/a.pdf")], {})
        rebuild_from_staged(sources, BASE, ["docs/a.pdf"], staged, {}, expected)
        os.unlink(staged / "docs" / "a.pdf")
        with self.assertRaises(ContentError) as caught:
            rebuild_from_staged(sources, BASE, ["docs/a.pdf"], staged, {}, expected)
        self.assertEqual(str(caught.exception), CHANGED)

    def test_build_failure_before_swap_preserves_public(self):
        build_site(self.root)
        before = self.snapshot()
        victim = self.content / "public-assets" / "docs" / "a.pdf"
        hook, parked = self.arm_planner(victim)
        with hook, self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertEqual(str(caught.exception), CHANGED)
        self.assertEqual(len(parked), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.residue(), [])

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", ["campaign", *args]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_cli_and_preview_failure_is_fixed_and_write_free(self):
        code, out, err = self.run_cli("build", "--directory", str(self.root))  # healthy controls
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("build", "--preview", "--directory", str(self.root))
        self.assertEqual(code, 0, err)
        before = self.snapshot()
        victim = self.content / "public-assets" / "docs" / "a.pdf"
        for arguments in (("build", "--directory", str(self.root)), ("build", "--preview", "--directory", str(self.root))):
            hook, parked = self.arm_planner(victim)
            with self.subTest(arguments=arguments[:2]), hook:
                code, out, err = self.run_cli(*arguments)
            self.assertEqual(len(parked), 1)
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("Stopped: " + CHANGED, err)
            for leaked in (str(self.root), "a.pdf", "No such file", "Errno", "Traceback"):
                self.assertNotIn(leaked, err)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.residue(), [])
            parked[0].rename(victim)  # restore for the next case
        report = preview_site(self.root)  # restored copy: the healthy preview still succeeds
        self.assertEqual(report["changed"], [])


if __name__ == "__main__":
    unittest.main()
