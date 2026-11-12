"""flock_facts reconcile merges a fresh unreviewed projection with the review overlay in one transaction; walks D-SEAM 8 (DFGL-07d)."""
import contextlib
import csv
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.flock import test_facts_review_txn as txn
from tests.flock.test_facts import TOOL, snapshot
from tests.flock.test_facts_review import ADDED, DOC, HEAD

_spec = importlib.util.spec_from_file_location("flock_facts_reconcile_under_test", TOOL)
facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(facts)

Q = {1: "quote one", 2: "quote two", 3: "quote three"}


class ReconcileTest(unittest.TestCase):
    crash = txn.ReviewTxnTest.crash  # forks a real `review` that dies at its k-th os.replace/os.unlink

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir, self.n = Path(tmp.name), 0
        self.store, self.sup = self.dir / "active", self.dir / "superseded"
        self.folder = self.store / "agencies" / "alpha"
        self.facts, self.log = self.folder / "FACTS.csv", self.folder / "FACT-REVIEWS.csv"
        self.run_tool("import", self.source(Q), "--store", str(self.store), "--added-by", "ana", "--date", ADDED, "--status", "unreviewed")
        self.ids = {int(r["page"]): r["fact_id"] for r in self.rows()}

    def source(self, quotes):
        self.n += 1
        path = self.dir / ("src-%d.csv" % self.n)
        with open(path, "w", newline="") as out:
            csv.writer(out).writerows([facts.V1] + [["Alpha", "REQ-SECRET-7", "vendor", "value %d" % n, DOC, str(n), q] for n, q in quotes.items()])
        return str(path)

    def stage(self, quotes=Q):
        stage = self.dir / ("stage-%d" % (self.n + 1))
        self.run_tool("import", self.source(quotes), "--store", str(stage), "--added-by", "ana", "--date", ADDED, "--status", "unreviewed")
        return stage

    def run_tool(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = facts.main(list(args))
        return code, out.getvalue().splitlines()

    def check(self, store):
        code, out = self.run_tool("--check", str(store))
        return code, [line for line in out if line]

    def review(self, n, to="checked"):
        self.assertEqual(self.run_tool("review", "--store", str(self.store), "--agency", "Alpha", "--ids", self.ids[n], "--set", to, "--reviewer", "ben")[0], 0)

    def rows(self):
        return [r for _, r in facts.load(self.facts, "alpha")[1]]

    def statuses(self):
        return {int(r["page"]): r["status"] for r in self.rows()}

    def reconcile(self, stage, store=None):
        return self.run_tool("reconcile", "--store", str(store or self.store), "--from", str(stage), "--agency", "Alpha", "--superseded", str(self.sup))

    def ok(self, stage, **counts):
        """A successful reconcile: exit 0, the counts line, no secrets on stdout, --check clean, projection equals the staging file."""
        code, out = self.reconcile(stage)
        want = {"kept": 0, "reset": 0, "new": 0, "removed": 0, "raw_dropped": 0, **counts}
        self.assertEqual((code, out), (0, ["reconcile | alpha | " + " ".join("%s=%d" % (k, want[k]) for k in ("kept", "reset", "new", "removed", "raw_dropped"))]))
        self.assertEqual(self.check(self.store), (0, []))
        self.assertEqual(facts.projection_sha256(self.facts), hashlib.sha256((stage / "agencies" / "alpha" / "FACTS.csv").read_bytes()).hexdigest())
        self.assertEqual([n for n in os.listdir(self.folder) if n.startswith(".")], [])
        return out

    def refused(self, stage, line="REVIEW_STATE_INVALID"):
        before = snapshot(self.dir)
        self.assertEqual(self.reconcile(stage), (1, [line]))
        self.assertEqual(snapshot(self.dir), before)
        self.assertFalse(self.sup.exists())

    def test_unchanged_checked_and_disputed_are_kept_and_the_log_is_byte_identical(self):
        self.review(1)
        self.review(2, "disputed")
        before = self.log.read_bytes()
        self.ok(self.stage(), kept=2)
        self.assertEqual(self.statuses(), {1: "checked", 2: "disputed", 3: "unreviewed"})
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual(len(list(self.sup.iterdir())), 1)

    def test_changed_quote_resets_review_and_keeps_old_rows_as_unbound_history(self):
        self.review(1)
        before, fp = self.log.read_bytes(), facts.evidence_fp(self.rows()[0])
        self.ok(self.stage({**Q, 1: "a changed quote"}), reset=1)
        self.assertEqual(self.statuses()[1], "unreviewed")
        self.assertEqual(self.log.read_bytes(), before)
        self.assertNotEqual(facts.evidence_fp(self.rows()[0]), fp)
        self.assertEqual(facts.overlay_errors(self.facts, self.log), [])

    def test_added_evidence_is_unreviewed(self):
        self.review(1)
        self.ok(self.stage({**Q, 4: "quote four"}), kept=1, new=1)
        self.assertEqual(self.statuses(), {1: "checked", 2: "unreviewed", 3: "unreviewed", 4: "unreviewed"})

    def test_removed_evidence_is_gone_and_its_log_rows_stay(self):
        self.review(2)
        before = self.log.read_bytes()
        self.ok(self.stage({1: Q[1], 3: Q[3]}), removed=1)
        self.assertEqual(sorted(self.statuses()), [1, 3])
        self.assertEqual(self.log.read_bytes(), before)

    def test_evidence_that_reverts_regains_its_own_chain(self):
        self.review(1)
        self.ok(self.stage({**Q, 1: "changed"}), reset=1)
        self.ok(self.stage(), kept=1)
        self.assertEqual(self.statuses()[1], "checked")
        self.assertEqual(len(self.log.read_text().splitlines()), 2)

    def test_reversal_chain_takes_the_last_status_and_keeps_every_row(self):
        self.review(1)
        self.review(1, "disputed")
        self.ok(self.stage(), kept=1)
        self.assertEqual(self.statuses()[1], "disputed")
        self.assertEqual(len(self.log.read_text().splitlines()), 3)

    def test_raw_status_edit_is_not_a_review_and_the_prior_pair_is_kept(self):
        facts.write_atomic(self.facts, [dict(r, status="checked") if r["page"] == "3" else r for r in self.rows()])
        self.assertTrue(facts.overlay_errors(self.facts, self.log))
        edited = self.facts.read_bytes()
        self.ok(self.stage(), raw_dropped=1)
        self.assertEqual(self.statuses()[3], "unreviewed")
        (kept,) = list(self.sup.iterdir())
        self.assertEqual((kept / "FACTS.csv").read_bytes(), edited)
        self.assertFalse((kept / "FACT-REVIEWS.csv").exists())
        self.assertEqual(facts.overlay_errors(self.facts, self.log), [])

    def test_absent_log_leaves_every_row_unreviewed_and_creates_none(self):
        self.ok(self.stage())
        self.assertFalse(self.log.exists())
        self.assertEqual(set(self.statuses().values()), {"unreviewed"})

    def test_no_active_facts_installs_the_projection_and_supersedes_nothing(self):
        other = self.dir / "fresh-active"
        stage = self.stage()
        self.assertEqual(self.reconcile(stage, other)[0], 0)
        self.assertEqual(facts.projection_sha256(other / "agencies" / "alpha" / "FACTS.csv"), facts.projection_sha256(self.facts))
        self.assertEqual(self.check(other), (0, []))
        self.assertEqual(os.listdir(other / "agencies" / "alpha"), ["FACTS.csv"])
        self.assertFalse(self.sup.exists())

    def test_malformed_symlinked_and_inconsistent_v1_logs_refuse_with_nothing_written(self):
        self.review(1)
        stage = self.stage()
        good = self.log.read_bytes()
        for name, make in (("malformed", lambda: self.log.write_text("not,a,review,log\n")),
                           ("empty", lambda: self.log.write_bytes(b"")),
                           ("v1 inconsistent", lambda: self.log.write_text(HEAD + "\n%s,unreviewed,disputed,ben,%s,\n" % (self.ids[1], ADDED))),
                           ("symlink", lambda: (os.unlink(self.log), os.symlink(self.dir / "real-log.csv", self.log)))):
            with self.subTest(name):
                if self.log.is_symlink():
                    os.unlink(self.log)
                    self.log.write_bytes(good)
                if name == "symlink":
                    (self.dir / "real-log.csv").write_bytes(good)
                make()
                self.refused(stage)

    def test_consistent_v1_log_is_upgraded_to_v2_in_the_same_transaction(self):
        facts.write_atomic(self.facts, [dict(r, status="checked") if r["page"] == "1" else r for r in self.rows()])
        self.log.write_text(HEAD + "\n%s,unreviewed,checked,ben,%s,\n" % (self.ids[1], ADDED))
        fp = facts.evidence_fp(self.rows()[0])
        self.ok(self.stage(), kept=1)
        self.assertEqual(self.log.read_text().splitlines(), [HEAD + ",evidence_fp", "%s,unreviewed,checked,ben,%s,,%s" % (self.ids[1], ADDED, fp)])
        self.assertEqual(self.statuses()[1], "checked")

    def test_a_pending_journal_from_a_crashed_review_is_recovered_first(self):
        self.crash(txn.P2, [self.ids[1]])
        self.assertEqual(facts.read_reviewed(self.store, "alpha")[2], ["REVIEW_TXN_PENDING"])
        self.ok(self.stage(), kept=1)
        self.assertEqual(self.statuses()[1], "checked")
        self.assertEqual(facts.read_reviewed(self.store, "alpha")[2], [])

    def test_a_review_landing_after_the_staging_import_is_kept(self):
        stage = self.stage()
        self.review(2)
        self.ok(stage, kept=1)
        self.assertEqual(self.statuses()[2], "checked")

    def test_a_held_lock_fails_busy_and_writes_nothing(self):
        stage, ready, release = self.stage(), os.pipe(), os.pipe()
        pid = os.fork()
        if pid == 0:
            with facts.store_lock(self.store):
                os.write(ready[1], b"x")
                os.read(release[0], 1)
            os._exit(0)
        try:
            os.read(ready[0], 1)
            before = snapshot(self.dir)
            with mock.patch.object(facts, "LOCK_WAIT", 0.3):
                self.assertEqual(self.reconcile(stage), (1, ["locked: fact store busy"]))
            self.assertEqual(snapshot(self.dir), before)
        finally:
            os.write(release[1], b"x")
            os.waitpid(pid, 0)

    def test_unreadable_staging_refuses_as_a_staging_error_with_nothing_written(self):
        stage = self.stage()
        path = stage / "agencies" / "alpha" / "FACTS.csv"
        good = path.read_bytes()
        for name, data in (("invalid utf-8", good + b"\xff\xfe\n"), ("csv error", good + b'"' + b"x" * (csv.field_size_limit() + 1) + b'"\n')):
            with self.subTest(name):
                path.write_bytes(data)
                self.refused(stage, "RECONCILE_STAGING_INVALID")

    def test_a_history_row_that_would_bind_to_a_reviewer_who_added_it_refuses_and_keeps_the_pair(self):
        self.review(1)
        stage = self.stage({**Q, 1: "a changed quote"})
        (fp,) = [facts.evidence_fp(r) for _, r in facts.load(stage / "agencies" / "alpha" / "FACTS.csv", "alpha")[1] if r["page"] == "1"]
        # unbound against the active rows (their evidence differs), so the active pair is still valid; bound by the merge, reviewer == added_by
        self.log.write_text(self.log.read_text() + "%s,unreviewed,checked,ana,%s,,%s\n" % (self.ids[1], ADDED, fp))
        self.assertEqual(self.check(self.store), (0, []))
        self.refused(stage)
        self.assertEqual(self.check(self.store), (0, []))

    def test_a_staging_projection_with_a_status_or_a_link_refuses_and_other_verbs_refuse_its_options(self):
        stage = self.stage()
        path = stage / "agencies" / "alpha" / "FACTS.csv"
        path.write_bytes(path.read_bytes().replace(b",unreviewed", b",checked", 1))
        self.refused(stage, "RECONCILE_STAGING_INVALID")
        self.refused(self.dir / "no-such-stage", "RECONCILE_STAGING_INVALID")
        for verb in ("status", "review"):
            with self.assertRaises(SystemExit):
                self.run_tool(verb, "--store", str(self.store), "--from", str(stage))


if __name__ == "__main__":
    unittest.main()
