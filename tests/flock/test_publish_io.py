"""RF1005-4: export refuses a mis-shaped RUNDIR and a symlinked PUBLISH.json; I/O errors print `io: <ExcName>`, exit 1."""
import contextlib, importlib.util, io, json, shutil, subprocess, sys, unittest
from pathlib import Path
from unittest import mock
from tests.flock import test_publish as base

spec = importlib.util.spec_from_file_location("_publish_io", base.TOOLS / "flock_publish.py")
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)
LAYOUT = (1, "export refused: RUNDIR must be <store>/agencies/<slug>/runs/<date>\n")
EXISTS = (1, "export refused: PUBLISH.json already exists\n")


def cli(rundir):  # subprocess, stdout and stderr both returned so a traceback cannot hide
    done = subprocess.run([sys.executable, "-B", str(base.TOOLS / "flock_publish.py"), "export", str(rundir), "--site-slug", "sample"],
                          capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


class PublishIoTest(base.FlockPublishTest):  # setUp builds store/agencies/<AGENCY>/runs/2026-10-30 (F4 finished)
    def inproc(self, rundir=None):  # in process; main() is where io errors are named
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = publish.main(["export", str(rundir or self.run_dir), "--site-slug", "sample"])
        return code, out.getvalue()

    def files(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*"))

    def test_happy_path_unchanged(self):
        self.assertEqual(cli(self.run_dir), (0, "exported=4 refused=0\n", ""))
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)

    def test_rundir_layout_refused_before_reading(self):
        shallow = self.tmp / "x"
        shutil.copytree(self.run_dir, shallow)
        shapes = [shallow, self.store / "agencies" / base.AGENCY / "other" / "2026-10-30",
                  self.store / "notagencies" / base.AGENCY / "runs" / "2026-10-30"]
        for rundir in shapes[1:]:
            shutil.copytree(self.run_dir, rundir)
        before = self.files()
        for rundir in shapes:
            with self.subTest(rundir=rundir.relative_to(self.tmp)):
                self.assertEqual(cli(rundir), LAYOUT + ("",))
        self.assertEqual(self.files(), before)
        self.assertEqual([p for p in self.tmp.rglob("PUBLISH.json")], [])
        with mock.patch.object(publish.verify_tool, "family", side_effect=AssertionError("read before layout check")):
            self.assertEqual(self.inproc(Path("/rf1005-4-missing")), LAYOUT)  # < 4 parents: no IndexError, nothing read
            self.assertEqual(self.inproc(shallow), LAYOUT)

    def test_symlinked_publish_json_refused_and_kept(self):
        outside = self.tmp / "outside.json"
        outside.write_bytes(b'{"keep": true}\n')
        for target in (self.tmp / "missing.json", outside):
            with self.subTest(target=target.name):
                self.out.symlink_to(target)
                self.assertEqual(cli(self.run_dir), EXISTS + ("",))
                self.assertTrue(self.out.is_symlink())
                self.assertEqual(self.out.readlink(), target)
                self.out.unlink()
        self.assertFalse((self.tmp / "missing.json").exists())
        self.assertEqual(outside.read_bytes(), b'{"keep": true}\n')
        self.assertEqual(sorted(p.name for p in self.run_dir.glob("*PUBLISH*")), [])

    def test_io_errors_named_without_writing(self):
        before = sorted(p.name for p in self.run_dir.iterdir())
        load_law = mock.patch.object(publish.compare.law_policy, "load_law", side_effect=PermissionError(13, "denied", str(self.store)))
        replace = mock.patch.object(publish.verify_tool.os, "replace", side_effect=PermissionError(13, "denied", str(self.out)))
        for name, patcher in (("law", load_law), ("write", replace)):
            with self.subTest(name), patcher:
                code, out = self.inproc()
                self.assertEqual((code, out), (1, "io: PermissionError\n"))
                self.assertNotIn(str(self.tmp), out)
                self.assertEqual(sorted(p.name for p in self.run_dir.iterdir()), before)  # no PUBLISH.json, no temp file

    def test_corrupt_run_json_after_family_passed(self):
        real = publish.verify_tool.load

        def load_then_corrupt(run):  # family and F3's check have read RUN.json; export's own read sees it corrupt
            result = real(run)
            (run / "RUN.json").write_text("{", encoding="utf-8")
            return result
        with mock.patch.object(publish.verify_tool, "load", side_effect=load_then_corrupt):
            self.assertEqual(self.inproc(), (1, "io: JSONDecodeError\n"))
        self.assertFalse(self.out.exists())

    def test_family_refusal_still_3_and_before_existing_target(self):
        (self.run_dir / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "anthropic"}))
        self.out.symlink_to(self.tmp / "missing.json")
        self.assertEqual(cli(self.run_dir), (3, "refused: verifier family equals author family\n", ""))
        self.assertTrue(self.out.is_symlink())
        shallow = self.tmp / "x"
        shutil.copytree(self.run_dir, shallow, symlinks=True)
        self.assertEqual(cli(shallow), LAYOUT + ("",))  # layout first, then family, then the existing target


for _name in [n for n in dir(base.FlockPublishTest) if n.startswith("test_")]:
    setattr(PublishIoTest, _name, None)  # base tests run once, in tests/flock/test_publish.py

if __name__ == "__main__":
    unittest.main()
