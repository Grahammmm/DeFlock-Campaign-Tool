"""flock_email check STORE: one readiness line per agency CONTACT.csv; draft refuses a RUNDIR outside the store layout (RF1130-1)."""
import contextlib, importlib.util, io, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL, FIXTURES = ROOT / "tools" / "flock_email.py", ROOT / "tests" / "fixtures" / "flock"
SLUG = "sample-city-police-department"
HEADER = "agency,role,email,source_url,source_sha256,checked_at,added_by"
TAIL = " mismatch=0 | draft only; G approval required before any send"
LAYOUT = "draft: RUNDIR must be <store>/agencies/<slug>/runs/<date>\n"


def row(agency, email="records@city.example"):
    return f"{agency},records,{email},https://www.city.example/records,{'c' * 64},2026-10-30,sample"


def run(*argv):
    return subprocess.run([sys.executable, "-B", str(TOOL), *map(str, argv)], capture_output=True, text=True)


def snapshot(base):
    return sorted((str(p.relative_to(base)), p.lstat().st_mtime_ns) for p in Path(base).rglob("*"))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        (self.store / "agencies").mkdir(parents=True)

    def agency(self, slug, text=None):
        folder = self.store / "agencies" / slug
        folder.mkdir()
        if text is None:
            shutil.copy(FIXTURES / "contact-sample.csv", folder / "CONTACT.csv")
        elif text is not False:
            (folder / "CONTACT.csv").write_text(text, encoding="utf-8")
        return folder

    def test_golden_sample_agency_is_ready_with_domain_only(self):
        self.agency(SLUG)
        done = run("check", self.store)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout, f"contact | {SLUG} | ready | domain=city.example\n"
                                      f"contact | agencies=1 ready=1 not_ready=0{TAIL}\n")
        self.assertNotIn("records@", done.stdout)

    def test_mixed_store_sorted_by_slug_exit_1(self):
        self.agency("c-ready", f"{HEADER}\n{row('C Ready')}\n")
        self.agency("a-missing", False)
        self.agency("b-two", f"{HEADER}\n{row('B Two')}\n{row('B Two', 'other@city.example')}\n")
        (self.store / "agencies" / "notes.txt").write_text("not an agency", encoding="utf-8")
        done = run("check", self.store)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines(), [
            "contact | a-missing | not ready | contact: CONTACT.csv missing or unreadable",
            "contact | b-two | not ready | contact: more than one records row",
            "contact | c-ready | ready | domain=city.example",
            f"contact | agencies=3 ready=1 not_ready=2{TAIL}"])

    def test_bad_header_bad_email_and_agency_mismatch_name_the_row_without_the_address(self):
        cases = {"bad-header": (f"agency,role,email\n{row('Bad Header')}\n", "row 1: header does not match CONTACT.csv v1"),
                 "bad-email": (f"{HEADER}\n{row('Bad Email', 'secret.person@nowhere')}\n", "row 2: email is invalid"),
                 "wrong-slug": (f"{HEADER}\n{row('Some Other Agency', 'hidden.local@city.example')}\n", "row 2: agency is invalid")}
        for slug, (text, _) in cases.items():
            self.agency(slug, text)
        done = run("check", self.store)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines()[:3], [f"contact | {s} | not ready | {want}" for s, (_, want) in sorted(cases.items())])
        for leaked in ("secret.person", "nowhere", "hidden.local", "Some Other Agency"):
            self.assertNotIn(leaked, done.stdout + done.stderr)

    def test_symlinked_agency_folder_is_refused_and_not_followed(self):
        target = self.tmp / "elsewhere" / SLUG
        target.mkdir(parents=True)
        shutil.copy(FIXTURES / "contact-sample.csv", target / "CONTACT.csv")
        os.symlink(target, self.store / "agencies" / SLUG)
        os.chmod(target, 0)
        self.addCleanup(os.chmod, target, 0o700)  # mode 0: following the link would read "missing or unreadable"
        done = run("check", self.store)
        self.assertEqual((done.returncode, done.stderr), (1, ""))
        self.assertEqual(done.stdout, f"contact | {SLUG} | not ready | link refused\n"
                                      f"contact | agencies=1 ready=0 not_ready=1{TAIL}\n")

    def test_empty_agencies_gives_zero_totals_and_missing_agencies_is_usage(self):
        done = run("check", self.store)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, f"contact | agencies=0 ready=0 not_ready=0{TAIL}\n", ""))
        bare, linked = self.tmp / "bare", self.tmp / "linked"
        bare.mkdir()
        linked.mkdir()
        os.symlink(self.store / "agencies", linked / "agencies")
        for store in (bare, linked, self.tmp / "absent"):
            with self.subTest(store=store.name):
                done = run("check", store)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", "check: no agencies folder\n"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads mode-0 folders")
    def test_unreadable_agencies_folder_is_usage_without_traceback(self):
        os.chmod(self.store / "agencies", 0)
        self.addCleanup(os.chmod, self.store / "agencies", 0o700)
        done = run("check", self.store)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", "check: agencies folder unreadable\n"))

    def test_store_inside_repository_refused_before_reading(self):
        spec = importlib.util.spec_from_file_location("_email_rf1130", TOOL)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        self.agency(SLUG)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tool, "REPO", self.tmp), mock.patch.object(tool, "contact", side_effect=AssertionError("read")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["check", str(self.store)])
        self.assertEqual((code, out.getvalue(), err.getvalue()), (2, "", "check: STORE is inside the repository\n"))

    def test_check_writes_nothing(self):
        self.agency(SLUG)
        self.agency("a-missing", False)
        before = snapshot(self.store)
        self.assertEqual(run("check", self.store).returncode, 1)
        self.assertEqual(snapshot(self.store), before)

    def test_help_lists_check_and_wrong_arity_is_usage(self):
        self.assertIn("check STORE", run("--help").stdout)
        for argv in (["check"], ["check", self.store, "extra"]):
            with self.subTest(argv=argv):
                done = run(*argv)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertIn("check STORE", done.stderr)


class DraftLayoutTests(unittest.TestCase):
    def test_rundir_outside_store_layout_refused_without_traceback_or_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            agency = tmp / "store" / "agencies" / SLUG
            agency.mkdir(parents=True)
            shutil.copy(FIXTURES / "contact-sample.csv", agency / "CONTACT.csv")  # deep_wrong would reach it without the rule
            shallow, other = tmp / "x", agency / "other" / "2026-10-31"
            deep_wrong = tmp / "store" / "elsewhere" / SLUG / "runs" / "2026-10-31"
            for rundir in (shallow, other, deep_wrong):
                rundir.mkdir(parents=True)
                shutil.copy(FIXTURES / "followup-draft-sample.md", rundir / "FOLLOWUP-DRAFT.md")
            drafts = tmp / "drafts"
            for rundir in (shallow, other, deep_wrong, Path("/")):
                with self.subTest(rundir=str(rundir)):
                    before = sorted(p.name for p in rundir.iterdir()) if rundir != Path("/") else None
                    done = run("draft", rundir, "--drafts", drafts)
                    self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", LAYOUT))
                    self.assertFalse(drafts.exists())
                    if before is not None:
                        self.assertEqual(sorted(p.name for p in rundir.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
