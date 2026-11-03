"""flock_email check STORE --json: contact readiness as one flock-email-contact-v1 document; text and exit unchanged (RF1230-23)."""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_email.py"
HEADER = "agency,role,email,source_url,source_sha256,checked_at,added_by"
LOCAL, PATH, QUERY, ADDED, SHA = "zz-local-part", "zz-src-path", "zz-src-query", "zz-added-by", "d" * 64
NOTE = "draft only; G approval required before any send"
AGES = ("--max-age-days", "30", "--today", "2026-11-02")


def row(agency, checked="2026-10-30", domain="example.org", page="www.example.org"):
    return f"{agency},records,{LOCAL}@{domain},https://{page}/{PATH}?{QUERY}=1,{SHA},{checked},{ADDED}"


def put(store, slug, text):
    folder = store / "agencies" / slug
    folder.mkdir(parents=True)
    (folder / "CONTACT.csv").write_text(text, encoding="utf-8")


def build_mixed(store):
    """One ready same-site contact, one ready portal-domain contact (warning), one bad header."""
    put(store, "a-same", f"{HEADER}\n{row('A Same')}\n")
    put(store, "b-portal", f"{HEADER}\n{row('B Portal', domain='portal.example.net')}\n")
    put(store, "c-bad", f"agency,email\n{row('C Bad')}\n")


def build_ages(store):
    put(store, "a-fresh", f"{HEADER}\n{row('A Fresh', '2026-10-20')}\n")
    put(store, "b-stale", f"{HEADER}\n{row('B Stale', '2026-09-01')}\n")
    put(store, "c-future", f"{HEADER}\n{row('C Future', '2026-11-05')}\n")


# Captured from origin/main tools/flock_email.py (before RF1230-23) on these exact fixtures.
GOLDEN_MIXED = ("contact | a-same | ready | domain=example.org\n"
                "contact | b-portal | ready | domain=portal.example.net | warn: domain differs from source host www.example.org\n"
                "contact | c-bad | not ready | row 1: header does not match CONTACT.csv v1\n"
                f"contact | agencies=3 ready=2 not_ready=1 mismatch=1 | {NOTE}\n")
GOLDEN_MIXED_AGES = GOLDEN_MIXED.replace("not_ready=1 mismatch", "not_ready=1 stale=0 mismatch")
GOLDEN_AGES = ("contact | a-fresh | ready | domain=example.org\n"
               "contact | b-stale | stale | checked 2026-09-01, 62 days old\n"
               "contact | c-future | not ready | contact: checked_at is in the future\n"
               f"contact | agencies=3 ready=1 not_ready=1 stale=1 mismatch=0 | {NOTE}\n")


def run(*argv):
    return subprocess.run([sys.executable, "-B", str(TOOL), *map(str, argv)], capture_output=True, text=True)


def entry(agency, state, domain=None, checked=None, age=None, error=None, warn=None):
    return {"agency": agency, "state": state, "domain": domain, "checked_at": checked, "age_days": age, "error": error,
            "warn_source_host": warn}


class CheckJsonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        (self.store / "agencies").mkdir(parents=True)

    def doc(self, *extra, code):
        done = run("check", self.store, *extra)
        self.assertEqual((done.returncode, done.stderr), (code, ""))
        for canary in (LOCAL, PATH, QUERY, ADDED, SHA, "@"):
            self.assertNotIn(canary, done.stdout)
        got = json.loads(done.stdout)
        self.assertEqual(done.stdout, json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        return got

    def test_mixed_store_rows_totals_and_exit_1_match_text(self):
        build_mixed(self.store)
        got = self.doc("--json", code=1)
        self.assertEqual(got, {
            "schema": "flock-email-contact-v1", "review_required": True, "max_age_days": None, "today": None, "note": NOTE,
            "agencies": [entry("a-same", "ready", "example.org"),
                         entry("b-portal", "ready", "portal.example.net", warn="www.example.org"),
                         entry("c-bad", "not_ready", error="row 1: header does not match CONTACT.csv v1")],
            "totals": {"agencies": 3, "ready": 2, "not_ready": 1, "stale": 0, "mismatch": 1}})
        self.assertEqual(run("check", self.store).stdout.splitlines()[-1],
                         f"contact | agencies=3 ready=2 not_ready=1 mismatch=1 | {NOTE}")

    def test_stale_has_date_and_age_counted_stale_and_future_is_not_ready(self):
        build_ages(self.store)
        got = self.doc(*AGES, "--json", code=1)
        self.assertEqual((got["max_age_days"], got["today"]), (30, "2026-11-02"))
        self.assertEqual(got["agencies"], [
            entry("a-fresh", "ready", "example.org"),
            entry("b-stale", "stale", checked="2026-09-01", age=62, error="contact: checked_at 2026-09-01 is 62 days old (max 30)"),
            entry("c-future", "not_ready", error="contact: checked_at is in the future")])
        self.assertEqual(got["totals"], {"agencies": 3, "ready": 1, "not_ready": 1, "stale": 1, "mismatch": 0})

    def test_without_max_age_an_old_contact_is_ready_and_has_no_age(self):
        put(self.store, "a-old", f"{HEADER}\n{row('A Old', '2020-01-01')}\n")
        got = self.doc("--json", code=0)
        self.assertEqual(got["agencies"], [entry("a-old", "ready", "example.org")])
        self.assertEqual((got["today"], got["totals"]["stale"]), (None, 0))

    def test_all_ready_exit_0_and_json_position_does_not_matter(self):
        put(self.store, "a-one", f"{HEADER}\n{row('A One')}\n")
        put(self.store, "b-two", f"{HEADER}\n{row('B Two', domain='records.example.org')}\n")  # a subdomain is the same site
        first = run("check", self.store, "--json", *AGES)
        for argv in (("check", self.store, *AGES, "--json"), ("check", self.store, *AGES[:2], "--json", *AGES[2:])):
            with self.subTest(argv=argv[2:]):
                self.assertEqual((run(*argv).returncode, run(*argv).stdout), (0, first.stdout))
        got = json.loads(first.stdout)
        self.assertEqual((first.returncode, got["totals"]), (0, {"agencies": 2, "ready": 2, "not_ready": 0, "stale": 0, "mismatch": 0}))

    def test_symlinked_agency_folder_is_link_refused_and_never_read(self):
        put(self.store, "a-same", f"{HEADER}\n{row('A Same')}\n")
        target = self.tmp / "elsewhere" / "b-link"
        target.mkdir(parents=True)
        (target / "CONTACT.csv").write_text(f"{HEADER}\n{row('B Link')}\n", encoding="utf-8")
        (target / "CONTACT.csv").chmod(0)
        self.addCleanup((target / "CONTACT.csv").chmod, 0o600)
        os.symlink(target, self.store / "agencies" / "b-link")
        got = self.doc("--json", code=1)
        self.assertEqual(got["agencies"][1], entry("b-link", "link_refused", error="link refused"))
        self.assertEqual(got["totals"], {"agencies": 2, "ready": 1, "not_ready": 1, "stale": 0, "mismatch": 0})
        self.assertEqual(run("check", self.store).stdout.splitlines()[1], "contact | b-link | not ready | link refused")

    def test_store_refusals_keep_stderr_and_exit_2_with_empty_stdout(self):
        bare, linked = self.tmp / "bare", self.tmp / "linked"
        bare.mkdir()
        linked.mkdir()
        os.symlink(self.store / "agencies", linked / "agencies")
        cases = [(ROOT, "check: STORE is inside the repository\n"), (bare, "check: no agencies folder\n"),
                 (linked, "check: no agencies folder\n")]
        for store, err in cases:
            for extra in ((), ("--json",), (*AGES, "--json")):
                with self.subTest(store=store.name, extra=extra):
                    done = run("check", store, *extra)
                    self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", err))

    def test_bad_json_or_age_options_are_usage_exit_2(self):
        build_mixed(self.store)
        run_dir = self.store / "agencies" / "a-same" / "runs" / "2026-11-02"
        run_dir.mkdir(parents=True)
        cases = [("check", self.store, "--json", "--json"), ("check", self.store, "--json", "--max-age-days"),
                 ("check", self.store, "--max-age-days", "0", "--json"), ("check", self.store, "--max-age-days", "30", "--today", "--json"),
                 ("check", self.store, "--json", "extra"),
                 ("lint", run_dir, "--drafts", self.tmp / "d", "--json"), ("draft", run_dir, "--drafts", self.tmp / "d", "--json")]
        for argv in cases:
            with self.subTest(argv=argv):
                done = run(*argv)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertTrue(done.stderr.startswith("usage: flock_email.py"))
        self.assertFalse((self.tmp / "d").exists())

    def test_text_output_is_byte_identical_to_main(self):
        build_mixed(self.store)
        for extra, golden in (((), GOLDEN_MIXED), (AGES, GOLDEN_MIXED_AGES)):
            with self.subTest(extra=extra):
                done = run("check", self.store, *extra)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (1, golden, ""))
        other = self.tmp / "other"
        build_ages(other)
        done = run("check", other, *AGES)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, GOLDEN_AGES, ""))

    def test_usage_names_the_flag_on_the_check_line(self):
        self.assertIn("flock_email.py check STORE [--json]", run("--help").stdout)


if __name__ == "__main__":
    unittest.main()
