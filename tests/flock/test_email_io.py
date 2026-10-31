import errno, importlib.util, io, os, shutil, subprocess, sys, tempfile, unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL, FIXTURES = ROOT / "tools" / "flock_email.py", ROOT / "tests" / "fixtures" / "flock"
SLUG, DATE = "sample-city-police-department", "2026-10-31"
EXISTS, INREPO = "draft: EMAIL-DRAFT already exists\n", "draft: RUNDIR is inside the repository\n"


def tree(base):
    rundir = Path(base) / "agencies" / SLUG / "runs" / DATE
    rundir.mkdir(parents=True)
    shutil.copy(FIXTURES / "followup-draft-sample.md", rundir / "FOLLOWUP-DRAFT.md")
    shutil.copy(FIXTURES / "contact-sample.csv", rundir.parents[1] / "CONTACT.csv")
    return rundir, Path(base) / "drafts"


def cli(rundir, drafts):
    cmd = [sys.executable, "-B", str(TOOL), "draft", str(rundir), "--drafts", str(drafts)]
    return subprocess.run(cmd, capture_output=True, text=True)


class Full:  # a draft file whose write fails as a full disk would
    def __init__(self, fh): self.fh = fh
    def __enter__(self): return self
    def __exit__(self, *exc): self.fh.close()
    def write(self, _text): raise OSError(errno.ENOSPC, "No space left on device")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rundir, self.drafts = tree(self.tmp)
        self.first, self.second = self.rundir / "EMAIL-DRAFT.md", self.drafts / f"{SLUG}-{DATE}-EMAIL-DRAFT.md"

    def refused(self, done, err):
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", err))
        self.assertEqual(sorted(self.tmp.rglob("*.tmp")), [])

    def in_process(self, **patches):
        spec = importlib.util.spec_from_file_location("flock_email_under_test", TOOL)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        err = io.StringIO()
        with mock.patch.multiple(mod.os, **patches), redirect_stderr(err):
            rc = mod.draft(str(self.rundir), str(self.drafts))
        return subprocess.CompletedProcess([], rc, "", err.getvalue())


class EmailDraftIoTests(Base):
    def test_dangling_symlink_at_either_target_is_refused_and_kept(self):
        os.symlink(self.tmp / "missing-1", self.first)
        self.refused(cli(self.rundir, self.drafts), EXISTS)
        self.assertEqual((os.readlink(self.first), self.drafts.exists()), (str(self.tmp / "missing-1"), False))
        os.unlink(self.first)
        self.drafts.mkdir()
        os.symlink(self.tmp / "missing-2", self.second)
        self.refused(cli(self.rundir, self.drafts), EXISTS)
        self.assertEqual(os.readlink(self.second), str(self.tmp / "missing-2"))
        self.assertFalse(os.path.lexists(self.first))

    def test_race_on_second_link_removes_only_our_first_copy(self):
        real, calls = os.link, []
        def link(src, dst):
            calls.append(dst)
            if len(calls) == 2:  # another writer wins the second name
                Path(dst).write_text("theirs\n", encoding="utf-8")
                raise FileExistsError(errno.EEXIST, "File exists")
            real(src, dst)
        self.refused(self.in_process(link=link), EXISTS)
        self.assertFalse(os.path.lexists(self.first))
        self.assertEqual(self.second.read_text(encoding="utf-8"), "theirs\n")

    def test_io_error_on_second_write_or_link_rolls_back(self):
        real_fdopen, real_link = os.fdopen, os.link
        def full_second(fd, *a, **k):
            fh = real_fdopen(fd, *a, **k)
            return Full(fh) if os.path.lexists(self.first) else fh
        def bad_second_link(src, dst):
            if os.path.lexists(self.first): raise PermissionError(errno.EACCES, "Permission denied")
            real_link(src, dst)
        for patch, err in (({"fdopen": full_second}, "draft: io OSError\n"),
                           ({"link": bad_second_link}, "draft: io PermissionError\n")):
            with self.subTest(err=err):
                self.refused(self.in_process(**patch), err)
                self.assertEqual([os.path.lexists(self.first), os.path.lexists(self.second)], [False, False])

    def test_existing_real_file_at_either_target_is_refused_unchanged(self):
        for which in ("first", "second"):
            with self.subTest(which=which):
                self.setUp()
                target, other = (self.first, self.second) if which == "first" else (self.second, self.first)
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(b"keep\n")
                self.refused(cli(self.rundir, self.drafts), EXISTS)
                self.assertEqual((target.read_bytes(), os.path.lexists(other)), (b"keep\n", False))

    def test_happy_path_then_second_run_refused(self):
        done = cli(self.rundir, self.drafts)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "written=2 to_domain=city.example\n", ""))
        body = self.first.read_bytes()
        self.assertEqual(self.second.read_bytes(), body)
        self.assertEqual(sorted(self.tmp.rglob("*.tmp")), [])
        self.refused(cli(self.rundir, self.drafts), EXISTS)
        self.assertEqual((self.first.read_bytes(), self.second.read_bytes()), (body, body))


class RundirInRepoTests(Base):
    def setUp(self):
        super().setUp()
        self.inrepo = Path(tempfile.mkdtemp(dir=ROOT, prefix=".rf0935-5-"))

    def tearDown(self):
        shutil.rmtree(self.inrepo, True)

    def test_rundir_inside_repo_refused_directly_or_via_symlink(self):
        rundir, _ = tree(self.inrepo)
        os.symlink(rundir, self.tmp / "outside-link")
        for given in (rundir, self.tmp / "outside-link"):
            with self.subTest(given=str(given)):
                self.refused(cli(given, self.drafts), INREPO)
                self.assertEqual([os.path.lexists(rundir / "EMAIL-DRAFT.md"), self.drafts.exists()], [False, False])

    def test_drafts_inside_repo_keeps_its_message(self):
        self.refused(cli(self.rundir, self.inrepo / "drafts"), "draft: --drafts is inside the repository\n")
        self.assertEqual([os.path.lexists(self.first), (self.inrepo / "drafts").exists()], [False, False])
