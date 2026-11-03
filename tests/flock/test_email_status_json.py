"""RF1230-22: `flock_email.py status STORE --json` / `flock_approve.py status STORE --json` print the same runs, stages,
counts and exit code as the text form, as one flock-email-status-v1 document; text output and refusals are unchanged."""
import contextlib, hashlib, importlib.util, io, json, os, subprocess, sys, unittest
from pathlib import Path

import tests.flock.test_email_status as base  # module import, so its tests are not collected twice
from tests.flock.approval_field import trusted, write_field

ROOT, SLUG, OTHER, SENT = base.ROOT, base.SLUG, base.OTHER, base.SENT
NOTE = "signatures not checked here (flock_approve.py status STORE checks them)"
CANARIES = base.LEAKS + ("records@city.example", "owner@example.invalid", "BEGIN SSH SIGNATURE", "Pat", "Sent by", "sha256")
KEYS = ["counts", "note", "refused_links", "review_required", "runs", "schema", "signatures_checked"]


def counts(runs=0, followup=0, drafted=0, approved=0, sent=0, malformed=0, inconsistent=0, ua=0, us=0):
    return {"runs": runs, "followup": followup, "drafted": drafted, "approved": approved, "sent": sent, "malformed": malformed,
            "inconsistent": inconsistent, "unverified_approval": ua, "unverified_sent": us}


def approve(*argv):
    """flock_approve.py main in-process with the test approver trusted (the test trust-file hook; never a real key)."""
    spec = importlib.util.spec_from_file_location("_status_json_flock_approve", ROOT / "tools" / "flock_approve.py")
    tool = spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), trusted():
        code = tool.main([str(a) for a in argv])
    return subprocess.CompletedProcess(argv, code, out.getvalue(), err.getvalue())


def tamper(rundir):
    """Edit the draft after G signed it and rebind the field's hash, keeping the old signature: well-formed, unverifiable."""
    draft, field = rundir / "EMAIL-DRAFT.md", rundir / "EMAIL-APPROVAL.json"
    draft.write_text(draft.read_text(encoding="utf-8").replace("CPRA", "CPRA (edited)"), encoding="utf-8")
    body = draft.read_bytes().decode("utf-8").removesuffix("\n").rpartition("\n")[0] + "\n"
    rec = json.loads(field.read_text(encoding="utf-8"))
    field.write_text(json.dumps({**rec, "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}), encoding="utf-8")


class EmailStatusJsonTests(unittest.TestCase):
    setUp, make = base.StatusTests.setUp, base.StatusTests.make

    def doc(self, done, checked, code=0):
        """The parsed document, after checking its exit code, exact bytes, keys, constants and canaries."""
        self.assertEqual((done.returncode, done.stderr), (code, ""))
        for leak in CANARIES:
            self.assertNotIn(leak, done.stdout + done.stderr)
        got = json.loads(done.stdout)
        self.assertEqual(done.stdout, json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        self.assertEqual((sorted(got), got["schema"], got["review_required"], got["signatures_checked"]),
                         (KEYS, "flock-email-status-v1", True, checked))
        return got

    def both(self, store=None):
        """(text, json) results of flock_email status STORE."""
        return base.run("status", store or self.store), base.run("status", store or self.store, "--json")

    def test_ladder_runs_in_line_order_counts_equal_text_totals_note_set(self):
        self.make("2026-10-01", email=False)
        self.make("2026-10-02")
        self.make("2026-10-03", OTHER, approve=True)
        before = base.snapshot(self.tmp)
        text, done = self.both()
        self.assertEqual(text.returncode, 0)
        self.assertEqual(text.stdout.splitlines(), [  # golden: byte-identical to origin/main
            f"email | {SLUG} | 2026-10-01 | follow-up drafted", f"email | {SLUG} | 2026-10-02 | email drafted",
            f"email | {OTHER} | 2026-10-03 | approval unverified", base.totals(3, 1, 1, ua=1)])
        got = self.doc(done, False)
        self.assertEqual(got["runs"], [{"agency": SLUG, "run": "2026-10-01", "stage": "follow-up drafted"},
                                       {"agency": SLUG, "run": "2026-10-02", "stage": "email drafted"},
                                       {"agency": OTHER, "run": "2026-10-03", "stage": "approval unverified"}])
        self.assertEqual([f"email | {r['agency']} | {r['run']} | {r['stage']}" for r in got["runs"]], text.stdout.splitlines()[:-1])
        self.assertEqual((got["counts"], got["refused_links"], got["note"]), (counts(3, 1, 1, ua=1), [], NOTE))
        tail = text.stdout.splitlines()[-1].split(" | ")[1].split()
        self.assertEqual(got["counts"], {k: int(v) for k, v in (kv.split("=") for kv in tail)})  # the text totals, every label
        self.assertEqual(base.snapshot(self.tmp), before)  # reads only

    def test_no_unverified_approval_means_empty_note_and_empty_store(self):
        self.make("2026-10-02")
        got = self.doc(self.both()[1], False)
        self.assertEqual((got["note"], got["counts"]), ("", counts(1, drafted=1)))
        empty = self.tmp / "empty"
        (empty / "agencies" / "no-runs-yet").mkdir(parents=True)
        done = base.run("status", empty, "--json")
        self.assertEqual((self.doc(done, False)["runs"], self.doc(done, False)["counts"]), ([], counts()))

    def test_inconsistent_and_malformed_exit_1_in_both_forms(self):
        self.make("2026-10-02", sent="Sent by: Pat 2026-10-31")
        bad = self.tmp / "bad"
        path = self.make("2026-10-02", store=bad) / "EMAIL-DRAFT.md"
        path.write_text(path.read_text(encoding="utf-8").replace(SENT, ""), encoding="utf-8")
        for store, want, tally in ((self.store, "inconsistent: sent without approval", counts(1, inconsistent=1)),
                                   (bad, "malformed", counts(1, malformed=1))):
            with self.subTest(stage=want):
                text, done = self.both(store)
                self.assertEqual(text.returncode, 1)
                self.assertEqual(text.stdout.splitlines()[0], f"email | {SLUG} | 2026-10-02 | {want}")
                got = self.doc(done, False, 1)
                self.assertEqual((got["runs"], got["counts"], got["note"]), ([{"agency": SLUG, "run": "2026-10-02", "stage": want}], tally, ""))

    def test_linked_agency_refused_and_linked_runs_folder_gives_no_runs(self):
        self.make("2026-10-02")
        far = self.make("2026-10-05", "elsewhere", store=self.tmp / "other")
        os.symlink(far.parents[2], self.store / "agencies" / "sb-linked")  # sorts between the two real agencies
        (self.store / "agencies" / "zz-linked-runs").mkdir()
        os.symlink(far.parent, self.store / "agencies" / "zz-linked-runs" / "runs")
        self.make("2026-10-03", OTHER)
        text, done = self.both()
        self.assertEqual(text.returncode, 0)
        self.assertEqual(text.stdout.splitlines(), [  # golden: byte-identical to origin/main, refused line in folder order
            f"email | {SLUG} | 2026-10-02 | email drafted", "email | sb-linked | link refused",
            f"email | {OTHER} | 2026-10-03 | email drafted", base.totals(2, drafted=2)])
        got = self.doc(done, False)
        self.assertEqual(got["refused_links"], ["sb-linked"])
        self.assertEqual([(r["agency"], r["run"]) for r in got["runs"]], [(SLUG, "2026-10-02"), (OTHER, "2026-10-03")])
        self.assertNotIn("elsewhere", done.stdout)
        self.assertNotIn("2026-10-05", done.stdout)

    def test_flock_approve_json_checks_signatures(self):
        self.make("2026-10-01", email=False)
        self.make("2026-10-02", approve=True)
        self.make("2026-10-03", OTHER, approve=True, sent="Sent by: Pat 2026-10-31")
        text, done = approve("status", self.store), approve("status", self.store, "--json")
        self.assertEqual(text.returncode, 0)
        self.assertEqual(text.stdout.splitlines(), [  # golden: byte-identical to origin/main
            f"email | {SLUG} | 2026-10-01 | follow-up drafted", f"email | {SLUG} | 2026-10-02 | approval recorded",
            f"email | {OTHER} | 2026-10-03 | sent recorded", base.totals(3, 1, approved=1, sent=1, checked=True)])
        got = self.doc(done, True)
        self.assertEqual([r["stage"] for r in got["runs"]], ["follow-up drafted", "approval recorded", "sent recorded"])
        self.assertEqual((got["counts"], got["note"]), (counts(3, 1, approved=1, sent=1), ""))
        plain = self.doc(base.run("status", self.store, "--json"), False)  # the same store without a verifier
        self.assertEqual([r["stage"] for r in plain["runs"]], ["follow-up drafted", "approval unverified", "sent unverified"])

    def test_flock_approve_json_tampered_or_forged_is_unverified_exit_1(self):
        for name, spoil in {"tampered-draft": tamper, "forged-signature": lambda r: write_field(r, forged=True)}.items():
            with self.subTest(case=name):
                store = self.tmp / name
                spoil(self.make("2026-10-02", store=store, approve=True))
                text, done = approve("status", store), approve("status", store, "--json")
                self.assertEqual(text.returncode, 1)
                self.assertEqual(text.stdout.splitlines(), [f"email | {SLUG} | 2026-10-02 | approval unverified",
                                                            base.totals(1, ua=1, checked=True)])
                got = self.doc(done, True, 1)
                self.assertEqual((got["runs"][0]["stage"], got["counts"], got["note"]), ("approval unverified", counts(1, ua=1), ""))

    def test_check_refusals_and_usage_unchanged_with_empty_stdout(self):
        for tool in ("flock_email.py", "flock_approve.py"):
            for store, err in ((ROOT, "check: STORE is inside the repository\n"), (self.tmp, "check: no agencies folder\n"),
                               (self.tmp / "absent", "check: no agencies folder\n")):
                for extra in ([], ["--json"]):
                    with self.subTest(tool=tool, store=store.name, extra=extra):
                        done = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / tool), "status", str(store), *extra],
                                              capture_output=True, text=True)
                        self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", err))
            for argv in (["status", self.store, "--jsn"], ["status", self.store, "--json", "--json"], ["status", "--json", self.store],
                         ["status", self.store, "--JSON"], ["status", self.store, "json"]):
                with self.subTest(tool=tool, argv=argv):
                    done = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / tool), *map(str, argv)], capture_output=True, text=True)
                    self.assertEqual((done.returncode, done.stdout), (2, ""))
                    self.assertIn("status STORE [--json]", done.stderr)


if __name__ == "__main__":
    unittest.main()
