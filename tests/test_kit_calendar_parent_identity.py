"""The private calendar's staging, commit and cleanup stay anchored to the admitted export directory (synthetic data)."""
import errno
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from campaign_tool import kit_calendar, kit_track  # noqa: E402
from tests import test_kit_calendar as base  # noqa: E402

NOW = base.NOW
CHANGED = "kit-calendar output parent changed; inspect the original export directory"
SENTINEL = b"unrelated replacement sentinel"
KINDS = ("public", "content", "other", "dir")


def snapshot(folder):
    return {str(p.relative_to(folder)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            for p in sorted(Path(folder).rglob("*")) if p.is_file()}


class State:
    stage = None
    swapped = False
    original = None


class ParentIdentityTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing fixture (a real init/kit campaign) rather than inheriting its discoverable tests.
        self.fx = base.KitCalendarTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.seed()
        self.tmp, self.root = self.fx.tmp, self.fx.root
        (self.root / "public").mkdir(exist_ok=True)
        (self.tmp / "other-tree").mkdir()
        self.destinations = {"public": self.root / "public", "content": self.root / "content",
                             "other": self.tmp / "other-tree"}
        self.expected = self.expected_bytes()

    def expected_bytes(self):
        events = (kit_calendar._request_events(self.root, None)[0]
                  + kit_calendar._meeting_events(self.root, kit_track._day("2026-11-01")))
        return kit_calendar.render(events, NOW).encode("utf-8")

    def export(self, name):
        folder = self.tmp / name
        folder.mkdir()
        (folder / "keep.txt").write_bytes(b"original unrelated file")
        return folder

    def publish(self, out):
        return kit_calendar.calendar(self.root, out, "2026-11-01", now=NOW)

    def swap(self, folder, kind, state):
        """Move the admitted directory aside and rebind its name to ``kind``'s destination (a deliberate fixture act)."""
        if state.swapped:
            return
        state.swapped = True
        original = folder.with_name(folder.name + "-original")
        folder.rename(original)
        if kind == "dir":
            folder.mkdir()
        else:
            folder.symlink_to(self.destinations[kind])
        if state.stage:
            (folder / state.stage).write_bytes(SENTINEL)
            os.chmod(folder / state.stage, 0o640)
        state.original = original

    def driven(self, folder, kind, seam):
        """Run the real export with one directory substitution at ``seam``; return (state, outcome exception)."""
        state = State()
        real_open, real_fsync, real_link, real_unlink = os.open, os.fsync, os.link, os.unlink

        def opener(path, flags, *args, **kwargs):
            if flags & os.O_EXCL:
                state.stage = path
            return real_open(path, flags, *args, **kwargs)

        def fsync(fd):
            real_fsync(fd)
            if seam in ("fsync", "error"):
                self.swap(folder, kind, state)
            if seam == "error":
                raise OSError(errno.EIO, "synthetic fsync failure")

        def link(source, destination, **kwargs):
            if seam == "link":
                self.swap(folder, kind, state)
            return real_link(source, destination, **kwargs)

        def unlink(path, **kwargs):
            if seam in ("unlink", "error"):
                self.swap(folder, kind, state)
            return real_unlink(path, **kwargs)

        def meeting(root, day, _real=kit_calendar._meeting_events):
            if seam == "admitted":
                self.swap(folder, kind, state)
            return _real(root, day)

        patches = [mock.patch.object(os, "open", opener), mock.patch.object(os, "fsync", fsync),
                   mock.patch.object(os, "link", link), mock.patch.object(os, "unlink", unlink),
                   mock.patch.object(kit_calendar, "_meeting_events", meeting)]
        error = None
        for patch in patches:
            patch.start()
        try:
            self.publish(folder / "dates.ics")
        except BaseException as exc:  # noqa: BLE001 - the test inspects the outcome
            error = exc
        finally:
            for patch in patches:
                patch.stop()
        return state, error

    def destination_before(self, kind, folder):
        return None if kind == "dir" else snapshot(self.destinations[kind])

    def check_replacement(self, kind, folder, state, before):
        """The replacement tree holds only what the actor put there, byte- and mode-identical."""
        if state.stage:
            self.assertEqual((folder / state.stage).read_bytes(), SENTINEL)
            self.assertEqual(stat.S_IMODE((folder / state.stage).stat().st_mode), 0o640)
        if kind == "dir":
            names = sorted(p.name for p in folder.iterdir())
            self.assertEqual(names, [state.stage] if state.stage else [])
        else:
            expected = dict(before)
            if state.stage:
                expected[state.stage] = (SENTINEL, 0o640)
            self.assertEqual(snapshot(self.destinations[kind]), expected)
        self.assertFalse((folder / "dates.ics").exists())

    # --- the three phases ---------------------------------------------------------------------------------------

    def test_parent_substitution_after_admission_never_writes_public_or_content(self):
        for kind in KINDS:
            for seam in ("admitted", "fsync"):
                with self.subTest(kind=kind, seam=seam):
                    folder = self.export(f"exports-{kind}-{seam}")
                    before = self.destination_before(kind, folder)
                    inputs = snapshot(self.root / "kit")
                    state, error = self.driven(folder, kind, seam)
                    self.assertIsInstance(error, ValueError)
                    self.assertEqual(str(error), CHANGED)
                    self.check_replacement(kind, folder, state, before)
                    # Only the admitted directory was touched: no final file, this call's stage removed, rest intact.
                    self.assertEqual(sorted(p.name for p in state.original.iterdir()), ["keep.txt"])
                    self.assertEqual(snapshot(self.root / "kit"), inputs)

    def test_cli_and_subprocess_refuse_a_substituted_parent_with_no_success_output(self):
        folder = self.export("exports-cli")
        state = State()
        real = kit_calendar._meeting_events

        def meeting(root, day):
            self.swap(folder, "public", state)
            return real(root, day)
        public_before = snapshot(self.root / "public")
        with mock.patch.object(kit_calendar, "_meeting_events", meeting):
            code, out, err = base.run("kit-calendar", "--directory", self.root, "--out", folder / "dates.ics",
                                      "--as-of", "2026-11-01")
        self.assertEqual((code, out), (3, ""))
        self.assertIn(CHANGED, err)
        self.assertEqual(snapshot(self.root / "public"), public_before)
        # The same injection in a real subprocess (a scratch driver; no production hook).
        child_folder = self.export("exports-child")
        driver = self.tmp / "driver.py"
        driver.write_text(DRIVER, encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(driver), str(REPO_ROOT), str(self.root),
                                 str(child_folder), str(self.root / "public")],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual((result.returncode, result.stdout), (3, ""), result.stderr)
        self.assertIn(CHANGED, result.stderr)
        self.assertEqual(snapshot(self.root / "public"), public_before)
        self.assertEqual(sorted(p.name for p in child_folder.with_name("exports-child-original").iterdir()),
                         ["keep.txt"])

    def test_swap_during_parent_acquisition_refuses_before_stage(self):
        real_open = os.open
        for kind in KINDS:
            with self.subTest(kind=kind):
                folder = self.export(f"exports-acq-{kind}")
                before = self.destination_before(kind, folder)
                state, opened = State(), []

                def opener(path, flags, *args, folder=folder, kind=kind, state=state, opened=opened, **kwargs):
                    if flags & os.O_DIRECTORY and str(path) == str(folder):
                        self.swap(folder, kind, state)
                    fd = real_open(path, flags, *args, **kwargs)
                    opened.append(fd)
                    return fd
                with mock.patch.object(os, "open", opener):
                    with self.assertRaises(ValueError) as caught:
                        self.publish(folder / "dates.ics")
                self.assertEqual(str(caught.exception), CHANGED)
                self.check_replacement(kind, folder, state, before)
                self.assertEqual(sorted(p.name for p in state.original.iterdir()), ["keep.txt"])
                for fd in opened:
                    with self.assertRaises(OSError):
                        os.fstat(fd)

    def test_swap_of_an_intermediate_ancestor_is_not_admitted(self):
        area = self.tmp / "area"
        (area / "exports").mkdir(parents=True)
        (self.tmp / "decoy" / "exports").mkdir(parents=True)
        (self.tmp / "decoy" / "exports" / "keep.txt").write_bytes(SENTINEL)
        real_open, state = os.open, State()

        def opener(path, flags, *args, **kwargs):
            if flags & os.O_DIRECTORY and str(path) == str(area / "exports") and not state.swapped:
                state.swapped = True
                area.rename(self.tmp / "area-original")
                area.symlink_to(self.tmp / "decoy")
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(os, "open", opener):
            with self.assertRaisesRegex(ValueError, CHANGED):
                self.publish(area / "exports" / "dates.ics")
        self.assertEqual(sorted(p.name for p in (self.tmp / "decoy" / "exports").iterdir()), ["keep.txt"])
        self.assertEqual(list((self.tmp / "area-original" / "exports").iterdir()), [])

    def test_a_safe_unchanged_ancestor_alias_still_works(self):
        (self.tmp / "area" / "exports").mkdir(parents=True)
        (self.tmp / "alias").symlink_to(self.tmp / "area")
        out = self.tmp / "alias" / "exports" / "dates.ics"
        report = self.publish(out)
        self.assertEqual(report, {"written": str(out), "events": 2})
        self.assertEqual((self.tmp / "area" / "exports" / "dates.ics").read_bytes(), self.expected)

    def test_precommit_swap_cannot_link_replacement_sentinel(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                folder = self.export(f"exports-pre-{kind}")
                before = self.destination_before(kind, folder)
                state, error = self.driven(folder, kind, "fsync")
                self.assertEqual(str(error), CHANGED)
                self.assertNotIn("dates.ics", [p.name for p in state.original.iterdir()])
                self.assertEqual(sorted(p.name for p in state.original.iterdir()), ["keep.txt"])
                self.assertEqual((state.original / "keep.txt").read_bytes(), b"original unrelated file")
                self.check_replacement(kind, folder, state, before)

    def test_swap_entering_the_link_commits_only_the_original_calendar(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                folder = self.export(f"exports-link-{kind}")
                before = self.destination_before(kind, folder)
                state, error = self.driven(folder, kind, "link")
                self.assertEqual(str(error), CHANGED)
                final = state.original / "dates.ics"
                self.assertEqual(final.read_bytes(), self.expected)  # the complete original calendar, never the sentinel
                self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o600)
                self.assertEqual(sorted(p.name for p in state.original.iterdir()), ["dates.ics", "keep.txt"])
                if kind == "dir":
                    self.assertEqual(sorted(p.name for p in folder.iterdir()), [state.stage])
                else:
                    expected = dict(before)
                    expected[state.stage] = (SENTINEL, 0o640)
                    self.assertEqual(snapshot(self.destinations[kind]), expected)
                self.assertEqual((folder / state.stage).read_bytes(), SENTINEL)

    def test_cleanup_swap_never_removes_replacement_entry(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                folder = self.export(f"exports-clean-{kind}")
                before = self.destination_before(kind, folder)
                state, error = self.driven(folder, kind, "unlink")
                self.assertEqual(str(error), CHANGED)
                final = state.original / "dates.ics"  # committed: kept complete, not rolled back
                self.assertEqual(final.read_bytes(), self.expected)
                self.assertEqual(sorted(p.name for p in state.original.iterdir()), ["dates.ics", "keep.txt"])
                self.assertEqual((folder / state.stage).read_bytes(), SENTINEL)
                if kind != "dir":
                    expected = dict(before)
                    expected[state.stage] = (SENTINEL, 0o640)
                    self.assertEqual(snapshot(self.destinations[kind]), expected)

    def test_precommit_error_cleanup_cannot_be_redirected(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                folder = self.export(f"exports-err-{kind}")
                before = self.destination_before(kind, folder)
                state, error = self.driven(folder, kind, "error")
                self.assertIsInstance(error, OSError)  # the primary failure, not masked by the substitution
                self.assertIn("synthetic fsync failure", str(error))
                self.assertEqual(sorted(p.name for p in state.original.iterdir()), ["keep.txt"])
                self.check_replacement(kind, folder, state, before)

    # --- descriptor lifetime -----------------------------------------------------------------------------------

    def test_parent_and_stage_descriptors_close_on_every_failure(self):
        real_open, real_fstat = os.open, os.fstat
        steps = ("open", "fstat", "wrap", "chmod", "write", "flush", "fsync", "close", "link", "unlink")
        for step in steps:
            folder = self.export(f"exports-fd-{step}")
            watch = real_open(folder / "keep.txt", os.O_RDONLY)
            seen, opened = [], []

            def opener(path, flags, *args, step=step, opened=opened, **kwargs):
                if step == "open" and flags & os.O_DIRECTORY:
                    raise OSError(errno.EIO, "synthetic open failure")
                fd = real_open(path, flags, *args, **kwargs)
                opened.append(fd)
                return fd

            def fstat(fd, step=step):
                info = real_fstat(fd)
                if step == "fstat" and stat.S_ISDIR(info.st_mode):
                    raise OSError(errno.EIO, "synthetic fstat failure")
                return info
            patches = [mock.patch.object(os, "open", opener), mock.patch.object(os, "fstat", fstat)]
            if step in ("wrap", "chmod", "write", "flush", "fsync", "close"):
                patches.append(mock.patch.object(os, "fdopen", base.faulty_fdopen(step, seen)))
            if step in ("chmod", "fsync"):
                patches.append(mock.patch.object(os, "fchmod" if step == "chmod" else "fsync",
                                                 side_effect=OSError(f"synthetic {step} failure")))
            if step == "link":
                patches.append(mock.patch.object(os, "link", side_effect=OSError(errno.EIO, "synthetic link failure")))
            if step == "unlink":
                patches.append(mock.patch.object(os, "unlink", side_effect=OSError(errno.EIO, "synthetic unlink failure")))
            try:
                with self.subTest(step=step):
                    for patch in patches:
                        patch.start()
                    try:
                        with self.assertRaisesRegex(OSError, f"synthetic {step} failure"):
                            self.publish(folder / "dates.ics")
                    finally:
                        for patch in reversed(patches):
                            patch.stop()
                    for fd in opened + seen:
                        with self.assertRaises(OSError, msg=f"{step}: descriptor {fd} leaked"):
                            os.fstat(fd)
                    self.assertEqual(os.fstat(watch).st_size, len(b"original unrelated file"))
                    names = sorted(p.name for p in folder.iterdir())
                    if step == "unlink":  # a failed post-commit removal is raised; the complete target stays
                        self.assertEqual([n for n in names if not n.startswith(".kit-calendar.")],
                                         ["dates.ics", "keep.txt"])
                        self.assertEqual((folder / "dates.ics").read_bytes(), self.expected)
                    else:
                        self.assertEqual(names, ["keep.txt"])
            finally:
                os.close(watch)

    def test_missing_directory_relative_support_refuses_before_writing(self):
        folder = self.export("exports-unsupported")
        with mock.patch.object(kit_calendar, "_DIRFD", False), mock.patch.object(os, "open") as opener:
            with self.assertRaisesRegex(ValueError, "directory-relative file operations"):
                self.publish(folder / "dates.ics")
        opener.assert_not_called()
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["keep.txt"])

    # --- compatibility -----------------------------------------------------------------------------------------

    def test_existing_target_races_stay_exclusive_with_dirfds(self):
        folder = self.export("exports-race")
        real_link = os.link
        out = folder / "dates.ics"

        def link(source, destination, **dirfds):
            out.write_bytes(b"sentinel")
            return real_link(source, destination, **dirfds)
        with mock.patch.object(os, "link", side_effect=link):
            with self.assertRaisesRegex(ValueError, "exists; nothing was overwritten"):
                self.publish(out)
        self.assertEqual(out.read_bytes(), b"sentinel")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["dates.ics", "keep.txt"])

    def test_healthy_fixed_clock_bytes_and_static_refusals_survive(self):
        folder = self.export("exports-healthy")
        out = folder / "dates.ics"
        report = self.publish(out)
        self.assertEqual(report, {"written": str(out), "events": 2})
        self.assertEqual(out.read_bytes(), self.expected)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["dates.ics", "keep.txt"])
        with self.assertRaisesRegex(ValueError, "exists; nothing was overwritten"):
            self.publish(out)
        for refused, words in ((self.root / "public" / "dates.ics", "public/"), (self.tmp / "dates.txt", ".ics"),
                               (self.tmp / "missing" / "dates.ics", "existing directory")):
            with self.assertRaisesRegex(ValueError, words):
                self.publish(refused)


DRIVER = '''
import os, sys
from datetime import datetime, timezone
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from campaign_tool import cli, kit_calendar
root, folder, public = sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
real = kit_calendar._meeting_events


def meeting(r, day):
    folder.rename(folder.with_name(folder.name + "-original"))
    folder.symlink_to(public)
    return real(r, day)


kit_calendar._meeting_events = meeting
sys.argv = ["campaign_tool", "kit-calendar", "--directory", root, "--out", str(folder / "dates.ics"),
            "--as-of", "2026-11-01"]
cli.main()
'''


if __name__ == "__main__":
    unittest.main()
