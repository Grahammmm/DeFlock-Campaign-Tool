"""flock_email status STORE: each agency run's email stage, read back from the drafts; sent without approval fails (RF1140-9)."""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

from tests.flock.approval_field import write_field

ROOT = Path(__file__).resolve().parents[2]
TOOL, FIXTURES = ROOT / "tools" / "flock_email.py", ROOT / "tests" / "fixtures" / "flock"
SLUG, OTHER = "sample-city-police-department", "second-town"
SENT = "Sent by: ____ (operator, by hand, after G's reply)"
TAIL = " | records only what the files say"
LEAKS = ("@", "city.example", "Subject", "CPRA request", "Alex", "ref X", "To:")


def run(*argv):
    return subprocess.run([sys.executable, "-B", str(TOOL), *map(str, argv)], capture_output=True, text=True)


def totals(runs=0, followup=0, drafted=0, approved=0, sent=0, malformed=0, inconsistent=0):
    return (f"email | runs={runs} followup={followup} drafted={drafted} approved={approved} sent={sent} "
            f"malformed={malformed} inconsistent={inconsistent}{TAIL}")


def snapshot(base):
    return sorted((str(p.relative_to(base)), p.lstat().st_mtime_ns) for p in Path(base).rglob("*"))


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        (self.store / "agencies").mkdir(parents=True)

    def make(self, date, slug=SLUG, email=True, approve=False, sent=SENT, store=None, prose=""):
        """A run folder with the sample follow-up and, when email is set, an EMAIL-DRAFT.md written by `draft` itself.
        approve writes the EMAIL-APPROVAL.json state field; prose adds a sentence that must grant nothing."""
        agency = (store or self.store) / "agencies" / slug
        rundir = agency / "runs" / date
        rundir.mkdir(parents=True)
        shutil.copy(FIXTURES / "followup-draft-sample.md", rundir / "FOLLOWUP-DRAFT.md")
        if not (agency / "CONTACT.csv").exists():
            name = "Sample City Police Department" if slug == SLUG else slug.replace("-", " ").title()
            (agency / "CONTACT.csv").write_text("agency,role,email,source_url,source_sha256,checked_at,added_by\n"
                                                f"{name},records,records@city.example,https://www.city.example/r,{'c' * 64},"
                                                "2026-10-30,sample\n", encoding="utf-8")
        if email:
            done = run("draft", rundir, "--drafts", self.tmp / f"drafts-{(store or self.store).name}")
            self.assertEqual(done.returncode, 0, done.stderr)
            path = rundir / "EMAIL-DRAFT.md"
            path.write_text(path.read_text(encoding="utf-8").replace(SENT, prose + sent), encoding="utf-8")
            if approve:
                write_field(rundir)
        return rundir

    def status(self, store=None):
        done = run("status", store or self.store)
        for leaked in LEAKS:
            self.assertNotIn(leaked, done.stdout + done.stderr)
        return done

    def test_golden_ladder_one_line_per_run_and_writes_nothing(self):
        self.make("2026-10-01", email=False)
        self.make("2026-10-02")
        self.make("2026-10-03", OTHER, approve=True)
        self.make("2026-10-04", OTHER, approve=True, sent="Sent by: Alex 2026-10-31")
        before = snapshot(self.tmp)
        done = self.status()
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout.splitlines(), [
            f"email | {SLUG} | 2026-10-01 | follow-up drafted", f"email | {SLUG} | 2026-10-02 | email drafted",
            f"email | {OTHER} | 2026-10-03 | approval recorded", f"email | {OTHER} | 2026-10-04 | sent recorded",
            totals(4, 1, 1, 1, 1)])
        self.assertEqual(snapshot(self.tmp), before)

    def test_sent_without_approval_is_inconsistent_exit_1(self):
        self.make("2026-10-02", sent="Sent by: Alex 2026-10-31")
        done = self.status()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines(), [f"email | {SLUG} | 2026-10-02 | inconsistent: sent without approval",
                                                    totals(1, inconsistent=1)])

    def test_filled_detection_edges(self):
        ok = "G APPROVED: G 2026-10-31 ok\n"  # a sentence; it must never count as approval
        cases = {"2026-10-01": (False, SENT, "", "email drafted"),
                 "2026-10-02": (False, "Sent by:   ", "", "email drafted"),
                 "2026-10-03": (True, "Sent by: ____ later", "", "approval recorded"),
                 "2026-10-04": (True, "Sent by: Alex 2026-10-31", "", "sent recorded"),
                 "2026-10-05": (False, "Sent by: Alex 2026-10-31", "", "inconsistent: sent without approval"),
                 "2026-10-06": (False, SENT, ok, "email drafted"),
                 "2026-10-07": (False, "Sent by: Alex 2026-10-31", ok, "inconsistent: sent without approval")}
        for date, (approve, sent, prose, _) in cases.items():
            self.make(date, approve=approve, sent=sent, prose=prose)
        done = self.status()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines(), [f"email | {SLUG} | {d} | {c[-1]}" for d, c in cases.items()]
                         + [totals(7, drafted=3, approved=1, sent=1, inconsistent=2)])

    def test_malformed_drafts_each_exit_1(self):
        cases = {"no-head": lambda t: t.split("\n", 1)[1], "no-sent": lambda t: t.replace(SENT, ""),
                 "sent-twice": lambda t: t + "Sent by: Alex\n"}
        for name, edit in cases.items():
            with self.subTest(case=name):
                store = self.tmp / name
                path = self.make("2026-10-02", store=store) / "EMAIL-DRAFT.md"
                path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
                done = self.status(store)
                self.assertEqual((done.returncode, done.stdout.splitlines()),
                                 (1, [f"email | {SLUG} | 2026-10-02 | malformed", totals(1, malformed=1)]))
        def field(path, **change):
            rec = json.loads(path.with_name("EMAIL-APPROVAL.json").read_text(encoding="utf-8"))
            path.with_name("EMAIL-APPROVAL.json").write_text(json.dumps({**rec, **change}), encoding="utf-8")
        spoil = {"edited-after-approval": lambda p: p.write_text(p.read_text(encoding="utf-8").replace("CPRA", "CPRA (edit)"), encoding="utf-8"),
                 "field-not-json": lambda p: p.with_name("EMAIL-APPROVAL.json").write_text("{", encoding="utf-8"),
                 "state-is-prose": lambda p: field(p, state="G approved this"),
                 "other-run": lambda p: field(p, subject=f"{SLUG}/2026-10-01")}
        for name, edit in spoil.items():
            with self.subTest(case=name):
                store = self.tmp / name
                edit(self.make("2026-10-02", store=store, approve=True) / "EMAIL-DRAFT.md")
                done = self.status(store)
                self.assertEqual((done.returncode, done.stdout.splitlines()),
                                 (1, [f"email | {SLUG} | 2026-10-02 | malformed", totals(1, malformed=1)]))
        for name, write in {"not-utf8": lambda p: p.write_bytes(b"\xff\xfe"), "a-folder": lambda p: p.mkdir()}.items():
            with self.subTest(case=name):
                store = self.tmp / name
                write(self.make("2026-10-02", store=store, email=False) / "EMAIL-DRAFT.md")
                self.assertEqual(self.status(store).stdout.splitlines()[0], f"email | {SLUG} | 2026-10-02 | malformed")

    def test_links_refused_and_never_followed(self):
        target = self.make("2026-10-09", "elsewhere", store=self.tmp / "other") / "EMAIL-DRAFT.md"
        os.chmod(target, 0)  # a read through the link would report "malformed"
        self.addCleanup(os.chmod, target, 0o600)
        os.symlink(target, self.make("2026-10-01", email=False) / "EMAIL-DRAFT.md")
        follow = self.make("2026-10-02", email=False) / "FOLLOWUP-DRAFT.md"
        follow.unlink()
        os.symlink(FIXTURES / "followup-draft-sample.md", follow)
        os.symlink(target.parent, self.store / "agencies" / SLUG / "runs" / "2026-10-03")
        os.symlink(target.parents[2], self.store / "agencies" / "linked-agency")
        done = self.status()
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout.splitlines(), ["email | linked-agency | link refused"]
                         + [f"email | {SLUG} | 2026-10-0{n} | refused: link" for n in (1, 2, 3)] + [totals(3)])

    def test_non_date_folders_files_and_empty_runs_get_no_line(self):
        runs = self.make("2026-10-02").parent
        shutil.copytree(runs / "2026-10-02", runs / "notes")
        (runs / "2026-10-29").write_text("a file, not a run\n", encoding="utf-8")
        (runs / "2026-10-30").mkdir()
        (runs / "2026-10-30" / "README.md").write_text("no draft here\n", encoding="utf-8")
        (self.store / "agencies" / "runs-is-a-file").mkdir()
        (self.store / "agencies" / "runs-is-a-file" / "runs").write_text("x\n", encoding="utf-8")
        done = self.status()
        self.assertEqual((done.returncode, done.stdout.splitlines()), (0, [f"email | {SLUG} | 2026-10-02 | email drafted",
                                                                          totals(1, drafted=1)]))

    def test_empty_store_zero_totals_and_missing_store_is_the_check_error(self):
        (self.store / "agencies" / "no-runs-yet").mkdir()
        done = self.status()
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, totals() + "\n", ""))
        for store in (self.tmp / "absent", self.tmp):
            with self.subTest(store=store.name):
                done = self.status(store)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", "check: no agencies folder\n"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads mode-0 folders")
    def test_unreadable_runs_folder_fails_closed(self):
        runs = self.make("2026-10-02").parent
        os.chmod(runs, 0)
        self.addCleanup(os.chmod, runs, 0o700)
        done = self.status()
        self.assertEqual((done.returncode, done.stderr), (1, ""))
        self.assertEqual(done.stdout.splitlines(), [f"email | {SLUG} | runs | malformed", totals(1, malformed=1)])

    def test_help_lists_status_and_wrong_arity_is_usage(self):
        self.assertIn("status STORE", run("--help").stdout)
        for argv in (["status"], ["status", self.store, "extra"]):
            with self.subTest(argv=argv):
                done = run(*argv)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertIn("status STORE", done.stderr)


if __name__ == "__main__":
    unittest.main()
