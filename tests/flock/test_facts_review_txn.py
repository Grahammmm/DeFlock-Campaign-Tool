"""flock_facts review is one journaled two-file transaction; recover() rolls it forward; read_reviewed() refuses a pending journal (DFGL-07c)."""
import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.flock.test_facts import TOOL, snapshot
from tests.flock.test_facts_review import ADDED, DOC, FactsReviewTest, facts

P0, P1, P2, P3 = 1, 2, 3, 4  # the k-th os.replace / os.unlink call of a review: journal commit, log, FACTS, journal unlink
JOURNAL = ".FACTS-TXN.json"
STAGED = (".FACTS.csv.txn", ".FACT-REVIEWS.csv.txn")


class ReviewTxnTest(unittest.TestCase):
    setUp, run_tool, review, check, log_lines = (getattr(FactsReviewTest, n) for n in ("setUp", "run_tool", "review", "check", "log_lines"))

    def crash(self, k, ids, to="checked"):
        """Fork; the child runs the real `review` and exits 70 instead of its k-th os.replace/os.unlink call."""
        pid = os.fork()
        if pid == 0:
            code = 99
            try:
                calls = [0]
                for name in ("replace", "unlink"):
                    def hook(*a, _real=getattr(os, name), **kw):
                        calls[0] += 1
                        if calls[0] == k:
                            os._exit(70)
                        return _real(*a, **kw)
                    setattr(facts.os, name, hook)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    facts.main(["review", "--store", str(self.store), "--agency", "Alpha", "--ids", ",".join(ids), "--set", to, "--reviewer", "ben"])
            except BaseException:
                code = 98
            os._exit(code)
        self.assertEqual(os.waitpid(pid, 0)[1] >> 8, 70)

    def pending(self):
        return sorted(p.name for p in self.folder.iterdir() if p.name.startswith("."))

    def statuses(self):
        return {r["fact_id"]: r["status"] for _, r in facts.load(self.facts, "alpha")[1]}

    def assert_applied_once(self, fact_id):
        self.assertEqual(self.statuses()[fact_id], "checked")
        self.assertEqual([r["fact_id"] for r in csv.DictReader(self.log.read_text().splitlines())].count(fact_id), 1)
        self.assertEqual((self.pending(), self.check()), ([], (0, [])))

    def test_clean_review_leaves_no_journal_or_staged_file_and_applies_once(self):
        self.assertEqual(self.review([self.a])[0], 0)
        self.assert_applied_once(self.a)

    def test_p0_crash_before_commit_leaves_the_old_pair_and_the_next_review_drops_the_temps(self):
        before = snapshot(self.dir)
        self.crash(P0, [self.a])
        self.assertTrue(any(n in self.pending() for n in STAGED))
        self.assertEqual({k: v for k, v in snapshot(self.dir).items() if not k.rsplit("/", 1)[-1].startswith(".")}, before)
        rows, _, codes = facts.read_reviewed(self.store, "alpha")
        self.assertEqual((len(rows), codes), (3, []))
        self.assertEqual(self.review([self.b], "checked", "cara")[0], 0)
        self.assertEqual((self.statuses()[self.a], self.statuses()[self.b]), ("unreviewed", "checked"))
        self.assertEqual([r["fact_id"] for r in csv.DictReader(self.log.read_text().splitlines())], [self.b])
        self.assertEqual((self.pending(), self.check()), ([], (0, [])))

    def test_p1_p2_p3_crash_is_pending_and_the_next_review_completes_it_exactly_once(self):
        for k in (P1, P2, P3):
            with self.subTest(point=k):
                self.setUp()
                self.crash(k, [self.a])
                self.assertEqual(facts.read_reviewed(self.store, "alpha"), ([], [], ["REVIEW_TXN_PENDING"]))
                self.assertEqual(self.review([self.b], "checked", "cara")[0], 0)
                self.assert_applied_once(self.a)
                self.assert_applied_once(self.b)

    def test_recover_alone_completes_a_crashed_review_and_is_idempotent(self):
        self.crash(P2, [self.a])
        with facts.store_lock(self.store):
            self.assertIsNone(facts.recover(self.store, "alpha"))
            self.assertIsNone(facts.recover(self.store, "alpha"))
        self.assert_applied_once(self.a)
        rows, log_rows, codes = facts.read_reviewed(self.store, "alpha")
        self.assertEqual((len(rows), [r["fact_id"] for r in log_rows], codes), (3, [self.a], []))

    def test_journal_matching_neither_file_is_corrupt_and_nothing_changes(self):
        self.crash(P1, [self.a])
        journal = self.folder / JOURNAL
        doc = json.loads(journal.read_text())
        doc["facts"]["old"] = doc["facts"]["new"] = "0" * 64
        journal.write_text(json.dumps(doc))
        before = snapshot(self.dir)
        code, out, _ = self.review([self.b], "checked", "cara")
        self.assertEqual((code, out), (1, ["REVIEW_TXN_CORRUPT"]))
        self.assertEqual(snapshot(self.dir), before)
        with facts.store_lock(self.store), self.assertRaises(facts.TxnCorrupt):
            facts.recover(self.store, "alpha")
        self.assertEqual(snapshot(self.dir), before)

    def test_read_reviewed_refuses_a_raw_status_edit_and_a_malformed_log(self):
        self.review([self.a])
        self.assertEqual(facts.read_reviewed(self.store, "alpha")[2], [])
        rows = [dict(r, status="checked") if r["fact_id"] == self.b else r for r in self.rows]
        facts.write_atomic(self.facts, rows)
        self.assertEqual(facts.read_reviewed(self.store, "alpha"), ([], [], ["REVIEW_STATE_INVALID"]))
        facts.write_atomic(self.facts, self.rows[:1] + self.rows[1:])
        self.log.write_text("not,a,review,log\n")
        self.assertEqual(facts.read_reviewed(self.store, "alpha"), ([], [], ["REVIEW_STATE_INVALID"]))

    def test_read_reviewed_waits_for_a_held_exclusive_lock_then_raises_locked(self):
        ready, release = os.pipe(), os.pipe()
        pid = os.fork()
        if pid == 0:
            with facts.store_lock(self.store):
                os.write(ready[1], b"x")
                os.read(release[0], 1)
            os._exit(0)
        try:
            os.read(ready[0], 1)
            with mock.patch.object(facts, "LOCK_WAIT", 0.3), self.assertRaises(facts.Locked):
                facts.read_reviewed(self.store, "alpha")
        finally:
            os.write(release[1], b"x")
            os.waitpid(pid, 0)

    def new_row_source(self):
        src = tempfile.NamedTemporaryFile("w", suffix=".csv", newline="", delete=False)  # outside the snapshotted store dir
        self.addCleanup(os.unlink, src.name)
        with src:
            csv.writer(src).writerows([facts.V1, ["Alpha", "REQ-SECRET-7", "vendor", "fresh value", DOC, "9", "fresh quote"]])
        return src.name

    def import_new(self, *extra):
        done = subprocess.run([sys.executable, "-B", str(TOOL), "import", str(self.new_row_source()), "--store", str(self.store),
                               "--added-by", "ana", "--date", ADDED, "--status", "unreviewed", *extra], capture_output=True, text=True)
        return done.returncode, done.stdout.splitlines()

    def test_real_import_after_a_crashed_review_recovers_it_before_reading_the_store(self):
        for k in (P1, P2):
            with self.subTest(point=k):
                self.setUp()
                self.crash(k, [self.a])
                code, out = self.import_new()
                self.assertEqual((code, out[-1]), (0, "imported=1 duplicates=0 errors=0 agencies=1"))
                status = self.statuses()
                self.assertEqual((len(status), status[self.a]), (4, "checked"))
                self.assertEqual(sorted(s for f, s in status.items() if f not in (self.a, self.b, self.c)), ["unreviewed"])
                self.assert_applied_once(self.a)
                self.assertEqual(facts.read_reviewed(self.store, "alpha")[2], [])

    def test_import_with_a_corrupt_journal_refuses_and_dry_run_ignores_the_journal(self):
        self.crash(P2, [self.a])
        before = snapshot(self.dir)
        self.assertEqual(self.import_new("--dry-run")[0], 0)
        self.assertEqual(snapshot(self.dir), before)
        (self.folder / JOURNAL).write_text(json.dumps({"schema": "flock-facts-txn-v1", "agency": "alpha"}))
        before = snapshot(self.dir)
        self.assertEqual(self.import_new(), (1, ["REVIEW_TXN_CORRUPT"]))
        self.assertEqual(snapshot(self.dir), before)

    def test_dry_run_import_succeeds_while_another_process_holds_the_lock(self):
        self.crash(P1, [self.a])
        ready, release = os.pipe(), os.pipe()
        pid = os.fork()
        if pid == 0:
            with facts.store_lock(self.store):
                os.write(ready[1], b"x")
                os.read(release[0], 1)
            os._exit(0)
        try:
            os.read(ready[0], 1)
            before = snapshot(self.dir)
            code, out = self.import_new("--dry-run")
            self.assertEqual((code, out[-1]), (0, "imported=0 would_import=1 duplicates=0 errors=0 agencies=1 dry_run=1"))
            self.assertEqual(snapshot(self.dir), before)
        finally:
            os.write(release[1], b"x")
            os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
