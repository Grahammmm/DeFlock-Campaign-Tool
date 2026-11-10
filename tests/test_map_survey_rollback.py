"""RF0725-4: ``map survey-sheet`` failure rollback removes only the HTML member this invocation created.

Synthetic fictional campaign, real files, fixed day, no network. Faults are injected with real filesystem operations
immediately before the second install (or at a real syscall seam); rollback itself is never mocked."""
import contextlib
import errno
import io
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from campaign_tool import cli, map_survey  # noqa: E402
from tests.test_map_survey import CAMERAS, EXAMPLE, run, square  # noqa: E402

DAY = date(2026, 11, 10)
HTML, CSV = "cedar-2026-11-10.html", "cedar-2026-11-10.csv"
COLLISION = f"kit/survey/{CSV}: exists; nothing was overwritten"


class RollbackTests(unittest.TestCase):
    def setUp(self):
        blocked = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network is blocked"))
        blocked.start()
        self.addCleanup(blocked.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-survey-rollback-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        map_dir = self.root / "content" / "map"
        (map_dir / "city-boundaries.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
            square("Cedar", 0.5, 1.0), square("Pine", 1.0, 1.5), square("West", -1.0, -0.5)]}), encoding="utf-8")
        (map_dir / "cameras.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": CAMERAS}),
                                                 encoding="utf-8")
        config = json.loads((map_dir / "map-config.json").read_text(encoding="utf-8"))
        (map_dir / "map-config.json").write_text(json.dumps(dict(config, initial_point_count=len(CAMERAS))),
                                                 encoding="utf-8")
        self.folder = self.root / "kit" / "survey"
        self.park = Path(temporary.name) / "parked-original.html"

    # --- helpers
    def survey(self):
        return map_survey.survey_sheet(self.root, "Cedar", today=DAY)

    @contextlib.contextmanager
    def before_second_install(self, action):
        """Run ``action(folder)`` after the HTML member is installed and before the CSV install starts."""
        real, calls = map_survey._install_new, []

        def wrapper(directory, name, data, owned=None):
            calls.append(name)
            if len(calls) == 2:
                action(directory)
            return real(directory, name, data) if owned is None else real(directory, name, data, owned)

        with mock.patch.object(map_survey, "_install_new", wrapper):
            yield

    def park_original_and_collide(self, replace=None):
        def action(folder):
            os.rename(folder / HTML, self.park)  # the original owned inode now lives outside the folder
            if replace is not None:
                replace(folder / HTML)
            (folder / CSV).write_bytes(b"SYNTHETIC CSV COLLISION")
        return action

    def expect_collision(self):
        with self.assertRaises(map_survey.SurveyError) as caught:
            self.survey()
        self.assertEqual(str(caught.exception), COLLISION)
        self.assertEqual((self.folder / CSV).read_bytes(), b"SYNTHETIC CSV COLLISION")

    def names(self):
        return sorted(p.name for p in self.folder.iterdir())

    def tree(self):
        kit = self.root / "kit"
        return {str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() and not p.is_symlink() else None)
                for p in sorted(self.root.rglob("*")) if p != kit and kit not in p.parents}

    # --- ownership
    def test_second_install_collision_preserves_replaced_regular_html(self):
        def replacement(path):
            path.write_bytes(b"FOREIGN REPLACEMENT")
            os.chmod(path, 0o640)
            os.utime(path, (1_000_000_000, 1_000_000_000))
        original_bytes = []
        action = self.park_original_and_collide(replacement)

        def recording(folder):
            original_bytes.append((folder / HTML).read_bytes())
            action(folder)

        with self.before_second_install(recording):
            self.expect_collision()
        replaced = (self.folder / HTML).stat()
        self.assertEqual((self.folder / HTML).read_bytes(), b"FOREIGN REPLACEMENT")
        self.assertEqual(stat.S_IMODE(replaced.st_mode), 0o640)
        self.assertEqual(int(replaced.st_mtime), 1_000_000_000)
        self.assertEqual(self.park.read_bytes(), original_bytes[0])
        self.assertEqual(self.names(), [CSV, HTML])  # no staging leftovers, no success report

    def test_rollback_preserves_replacement_symlink_directory_and_foreign_hardlink(self):
        sentinel = self.root.parent / "sentinel.txt"
        sentinel.write_bytes(b"SENTINEL")

        def link_live(path):
            os.symlink(sentinel, path)

        def link_dangling(path):
            os.symlink(self.root.parent / "no-such-target", path)

        def empty_dir(path):
            path.mkdir()

        def full_dir(path):
            path.mkdir()
            (path / "inner.txt").write_bytes(b"INNER")

        def hard_link(path):
            os.link(sentinel, path)  # a second name for a *different* inode

        for label, make in (("live link", link_live), ("dangling link", link_dangling), ("empty dir", empty_dir),
                            ("full dir", full_dir), ("hard link", hard_link)):
            with self.subTest(label), tempfile.TemporaryDirectory() as scratch:
                shutil.rmtree(self.root / "kit", ignore_errors=True)
                self.park = Path(scratch) / "parked.html"
                with self.before_second_install(self.park_original_and_collide(make)):
                    self.expect_collision()
                entry = self.folder / HTML
                self.assertTrue(os.path.lexists(entry), label)
                if label == "live link":
                    self.assertTrue(entry.is_symlink())
                    self.assertEqual(os.readlink(entry), str(sentinel))
                elif label == "dangling link":
                    self.assertTrue(entry.is_symlink() and not entry.exists())
                elif "dir" in label:
                    self.assertTrue(entry.is_dir() and not entry.is_symlink())
                    self.assertEqual(sorted(p.name for p in entry.iterdir()), ["inner.txt"] * (label == "full dir"))
                else:
                    self.assertEqual(entry.read_bytes(), b"SENTINEL")
                    self.assertEqual(os.stat(entry).st_ino, os.stat(sentinel).st_ino)
                self.assertEqual(sentinel.read_bytes(), b"SENTINEL")
                self.assertTrue(self.park.is_file())

    def test_ordinary_hard_link_to_the_same_owned_inode_is_still_owned(self):
        """Identity decides, not link count or name: an extra name for the owned inode does not stop cleanup."""
        extra = self.root.parent / "extra-name.html"

        def action(folder):
            os.link(folder / HTML, extra)
            (folder / CSV).write_bytes(b"SYNTHETIC CSV COLLISION")

        with self.before_second_install(action):
            self.expect_collision()
        self.assertEqual(self.names(), [CSV])
        self.assertTrue(extra.is_file())

    def test_missing_installed_html_does_not_mask_csv_collision(self):
        with self.before_second_install(self.park_original_and_collide()):
            self.expect_collision()  # SurveyError, not FileNotFoundError
        self.assertEqual(self.names(), [CSV])
        self.assertTrue(self.park.is_file())

    # --- ordinary rollback and failure seams
    def test_ordinary_second_member_failure_cleans_owned_html(self):
        def collide(folder):
            (folder / CSV).write_bytes(b"SYNTHETIC CSV COLLISION")

        with self.before_second_install(collide):
            self.expect_collision()
        self.assertEqual(self.names(), [CSV])  # owned HTML gone, collision untouched, no staging files
        (self.folder / CSV).unlink()  # only the fixture-created collision
        report = self.survey()
        self.assertEqual(report["written"], [f"kit/survey/{HTML}", f"kit/survey/{CSV}"])
        self.assertEqual(self.names(), [CSV, HTML])

    def test_each_second_member_failure_seam_removes_only_owned_files(self):
        def flaky(real, trigger):
            count = []

            def call(*args, **kwargs):
                count.append(1)
                if len(count) == 2:
                    raise OSError(errno.EIO, "injected")
                return real(*args, **kwargs)
            return call

        class FailingWrite:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self.handle.__exit__(*exc)

            def write(self, data):
                self.handle.write(data[: len(data) // 2])  # partial write, then failure
                raise OSError(errno.ENOSPC, "injected")

        real_fdopen, seen = os.fdopen, []

        def fdopen(fd, mode, **kwargs):
            seen.append(1)
            handle = real_fdopen(fd, mode, **kwargs)
            return FailingWrite(handle) if len(seen) == 2 else handle

        seams = {"fsync": lambda: mock.patch.object(map_survey.os, "fsync", flaky(os.fsync, None)),
                 "link": lambda: mock.patch.object(map_survey.os, "link", flaky(os.link, None)),
                 "write": lambda: mock.patch.object(map_survey.os, "fdopen", fdopen)}
        for label, patcher in seams.items():
            with self.subTest(label):
                shutil.rmtree(self.root / "kit", ignore_errors=True)
                seen.clear()
                with patcher(), self.assertRaises(OSError) as caught:
                    self.survey()
                self.assertEqual(caught.exception.errno in (errno.EIO, errno.ENOSPC), True)
                self.assertEqual(self.names(), [])

    def test_installed_member_is_registered_before_post_link_failure(self):
        real_unlink = os.unlink
        injected = []

        def failing_temp_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(f".{CSV}.") and not injected:
                injected.append(path)
                if foreign:
                    os.replace(self.root.parent / "foreign.csv", self.folder / CSV)
                raise OSError(errno.EIO, "injected staging cleanup failure")
            return real_unlink(path, *args, **kwargs)

        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                shutil.rmtree(self.root / "kit", ignore_errors=True)
                injected.clear()
                (self.root.parent / "foreign.csv").write_bytes(b"FOREIGN CSV")
                with mock.patch.object(map_survey.os, "unlink", failing_temp_cleanup), \
                        self.assertRaises(OSError) as caught:
                    self.survey()
                self.assertEqual(caught.exception.errno, errno.EIO)
                leftovers = [n for n in self.names() if n.endswith(".tmp")]
                self.assertEqual(len(leftovers), 1)  # the staging file whose cleanup was injected to fail
                kept = [n for n in self.names() if not n.endswith(".tmp")]
                self.assertEqual(kept, [CSV] if foreign else [])  # linked member tracked; foreign replacement kept
                if foreign:
                    self.assertEqual((self.folder / CSV).read_bytes(), b"FOREIGN CSV")

    def test_retained_descriptors_close_once_on_success_and_every_failure(self):
        def traced(run_survey):
            opened, closed = [], []
            real_mkstemp, real_close = tempfile.mkstemp, os.close

            def mkstemp(*args, **kwargs):
                fd, name = real_mkstemp(*args, **kwargs)
                opened.append(fd)
                return fd, name

            def close(fd):
                closed.append(fd)
                return real_close(fd)

            with mock.patch.object(map_survey.tempfile, "mkstemp", mkstemp), \
                    mock.patch.object(map_survey.os, "close", close):
                run_survey(opened)
            self.assertEqual(len(opened), len(set(opened)))
            for fd in opened:
                self.assertEqual(closed.count(fd), 1, (fd, opened, closed))
                with self.assertRaises(OSError):
                    os.fstat(fd)
            return opened

        # rollback compares the retained descriptor while it is still open (real fstat), then it is closed once
        compared = []
        real_rollback = map_survey._rollback

        def checking_rollback(owned):
            for _, fd in owned:
                compared.append(os.fstat(fd).st_ino)
            return real_rollback(owned)

        def failing(opened):
            with mock.patch.object(map_survey, "_rollback", checking_rollback), \
                    self.before_second_install(lambda folder: (folder / CSV).write_bytes(b"X")):
                with self.assertRaises(map_survey.SurveyError):
                    self.survey()

        self.assertEqual(len(traced(failing)), 2)
        self.assertEqual(len(compared), 1)  # the HTML member was open and statable at comparison time
        self.assertEqual(self.names(), [CSV])

        shutil.rmtree(self.root / "kit")
        self.assertEqual(len(traced(lambda opened: self.survey())), 2)  # success path
        shutil.rmtree(self.root / "kit")

        def fail_before_stream(opened):
            with mock.patch.object(map_survey.os, "fchmod", side_effect=OSError(errno.EPERM, "injected")), \
                    self.assertRaises(OSError):
                self.survey()

        self.assertEqual(len(traced(fail_before_stream)), 1)
        self.assertEqual(self.names(), [])

    def test_identity_inspection_or_unlink_failure_preserves_original_error(self):
        real_unlink, real_rollback = os.unlink, map_survey._rollback
        html_path = lambda: self.folder / HTML  # noqa: E731

        def temp_cleanup_then_fail(path, *args, **kwargs):  # post-link failure on the second member
            if Path(path).name.startswith(f".{CSV}."):
                real_unlink(path, *args, **kwargs)
                raise OSError(errno.EIO, "original failure")
            return real_unlink(path, *args, **kwargs)

        def failing_html_unlink(path, *args, **kwargs):
            if Path(path) == html_path():
                raise OSError(errno.EACCES, "injected unlink failure")
            return temp_cleanup_then_fail(path, *args, **kwargs)

        cases = {
            "fstat": (lambda: mock.patch.object(map_survey.os, "fstat", side_effect=OSError(errno.EIO, "x")),
                      temp_cleanup_then_fail, [CSV, HTML]),
            "lstat": (lambda: mock.patch.object(map_survey.os, "lstat", side_effect=OSError(errno.EIO, "x")),
                      temp_cleanup_then_fail, [CSV, HTML]),
            "unlink": (lambda: contextlib.nullcontext(), failing_html_unlink, [HTML]),  # CSV still cleaned
        }
        for label, (arm, unlink, expected) in cases.items():
            with self.subTest(label):
                shutil.rmtree(self.root / "kit", ignore_errors=True)

                def armed(owned, arm=arm):
                    with arm():
                        return real_rollback(owned)

                with mock.patch.object(map_survey.os, "unlink", unlink), \
                        mock.patch.object(map_survey, "_rollback", armed), \
                        self.assertRaises(OSError) as caught:
                    self.survey()
                self.assertEqual(caught.exception.errno, errno.EIO)
                self.assertEqual(str(caught.exception), "[Errno 5] original failure")
                self.assertEqual(self.names(), sorted(expected))

    # --- real CLI
    def cli_main(self, *args):
        fixed = mock.Mock(wraps=date)
        fixed.today.return_value = DAY
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(map_survey, "date", fixed), \
                mock.patch("sys.argv", ["campaign_tool", "map", "survey-sheet", "--city", "Cedar",
                                        "--directory", str(self.root), *args]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
                code = 0
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_real_cli_failure_and_success_keep_existing_contract(self):
        with self.before_second_install(self.park_original_and_collide(lambda p: p.write_bytes(b"FOREIGN"))):
            code, out, err = self.cli_main()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(COLLISION, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual((self.folder / HTML).read_bytes(), b"FOREIGN")
        shutil.rmtree(self.root / "kit")

        first = run("map", "survey-sheet", "--city", "Cedar", "--directory", str(self.root))
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(first.stdout)
        self.assertEqual(list(report), ["written", "city", "date", "points", "note"])
        self.assertEqual(report["city"], "Cedar")
        self.assertEqual(report["points"], 2)
        self.assertEqual(report["note"], map_survey.NOTE)
        html_path, csv_path = (self.root / rel for rel in report["written"])
        rows, _, _ = map_survey.survey_rows(self.root, "Cedar", today=date.fromisoformat(report["date"]))
        self.assertEqual(csv_path.read_bytes(), map_survey.render_csv(rows))
        self.assertTrue(html_path.read_bytes().startswith(b"<!doctype html>"))
        for path in (self.root / "kit", self.folder):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (html_path, csv_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = (html_path.read_bytes(), csv_path.read_bytes())
        again = run("map", "survey-sheet", "--city", "Cedar", "--directory", str(self.root))
        self.assertEqual(again.returncode, 1)
        self.assertIn("exists; nothing was overwritten", again.stderr)
        self.assertEqual((html_path.read_bytes(), csv_path.read_bytes()), before)

    def test_refusal_never_changes_campaign_inputs_or_public_tree(self):
        public = self.root / "public"
        public.mkdir()
        (public / "sentinel.txt").write_bytes(b"PUBLIC SENTINEL")
        with self.before_second_install(self.park_original_and_collide(lambda p: p.write_bytes(b"FOREIGN"))):
            before = self.tree()  # after the injector's own writes would be too late: snapshot what it never touches
            self.assertTrue(before)
            self.expect_collision()
        after = self.tree()
        self.assertEqual(after, before)
        self.assertEqual((public / "sentinel.txt").read_bytes(), b"PUBLIC SENTINEL")
        self.assertEqual(sorted(p.name for p in public.iterdir()), ["sentinel.txt"])


if __name__ == "__main__":
    unittest.main()
