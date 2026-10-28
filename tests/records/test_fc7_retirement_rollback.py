"""FC7-RETIREMENT-ROLLBACK: finish() closes its own transaction on every failure.

Synthetic, candidate-local SQLite only. The tables reuse the actual column
shapes of ledger_meta, stage_state, receipts and stage_validation_authority
(foreign keys and immutability triggers are not needed for this cleanup
contract). Faults are injected through a ``sqlite3.Connection`` subclass.
Transaction state and the persisted marker are checked from a second
connection. Nothing here performs production retirement or publication.
"""
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import review_retirement

SCHEMA = """
CREATE TABLE ledger_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE receipts(
 sha256 TEXT PRIMARY KEY, stage TEXT NOT NULL, subject_sha256 TEXT NOT NULL,
 reviewer_id TEXT NOT NULL, role TEXT NOT NULL, verdict TEXT NOT NULL,
 coverage TEXT NOT NULL, locators TEXT NOT NULL, rationale TEXT NOT NULL,
 model_or_tool TEXT NOT NULL, input_hashes TEXT NOT NULL, created_at_tz TEXT NOT NULL,
 supersedes TEXT, path TEXT NOT NULL,
 UNIQUE(sha256,stage,subject_sha256));
CREATE TABLE stage_state(
 original_sha256 TEXT NOT NULL,
 stage TEXT NOT NULL CHECK(stage IN ('preserve','extract','catalog','detect','review','compare','privacy')),
 status TEXT NOT NULL CHECK(status IN ('pending','in_progress','done','blocked','inapplicable')),
 receipt_sha256 TEXT, owner TEXT NOT NULL, updated_at TEXT NOT NULL,
 run_id TEXT NOT NULL, reason TEXT,
 PRIMARY KEY(original_sha256,stage));
CREATE TABLE stage_validation_authority(
 receipt_sha256 TEXT PRIMARY KEY NOT NULL, claim_id TEXT NOT NULL,
 run_id TEXT NOT NULL, owner TEXT NOT NULL,
 profile_sha256 TEXT NOT NULL, test_only INTEGER NOT NULL CHECK(test_only IN (0,1)),
 validator_id TEXT NOT NULL);
"""
HISTORY = ("receipts", "stage_state", "stage_validation_authority")
INVALID = "review_authority_retirement_state_invalid"
SELECT_ELIGIBLE = "SELECT 1 FROM stage_state"
WRITE_MARKER = "INSERT INTO ledger_meta"


def digest(label):
    return hashlib.sha256(("fc7-synthetic:" + label).encode()).hexdigest()


class InjectedFault(sqlite3.OperationalError):
    pass


class FaultConnection(sqlite3.Connection):
    """Synthetic connection: optional faults before/after a matching statement or at commit."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.faults = []  # (sql fragment, "before"|"after", exception)
        self.commit_fault = None
        self.rollbacks = 0
        self.statements = []

    def execute(self, sql, parameters=()):
        self.statements.append(sql)
        for fragment, when, error in self.faults:
            if fragment in sql and when == "before":
                raise error
        cursor = super().execute(sql, parameters)
        for fragment, when, error in self.faults:
            if fragment in sql and when == "after":
                raise error
        return cursor

    def commit(self):
        if self.commit_fault is not None:
            raise self.commit_fault
        super().commit()

    def rollback(self):
        self.rollbacks += 1
        super().rollback()


class RetirementRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fc7-retirement-rollback-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / "synthetic-ledger.sqlite"
        con = sqlite3.connect(str(self.path))
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        con.commit()
        con.close()
        self.con = self.open(FaultConnection)

    def open(self, factory=sqlite3.Connection, timeout=5):
        con = sqlite3.connect(str(self.path), timeout=timeout, factory=factory)
        self.addCleanup(con.close)
        return con

    # ------------------------------------------------------------ synthetic state
    def seed(self, marker=review_retirement.PENDING, reviewer="independent:synthetic", test_only=0,
             status="done", label="a"):
        con = self.open()
        if marker is not None:
            con.execute("INSERT INTO ledger_meta(key,value) VALUES(?,?)", (review_retirement.KEY, marker))
        original, receipt = digest("original-" + label), digest("receipt-" + label)
        con.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt, "review", original, reviewer, "reviewer", "pass", "[]", "[]", "synthetic",
                     "synthetic-tool", "[]", "2026-01-01T00:00:00+00:00", None, "synthetic/receipt.json"))
        con.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)",
                    (original, "review", status, receipt, "synthetic-owner", "2026-01-01T00:00:00+00:00",
                     "synthetic-run", None))
        con.execute("INSERT INTO stage_validation_authority VALUES(?,?,?,?,?,?,?)",
                    (receipt, "synthetic-claim-" + label, "synthetic-run", "synthetic-owner",
                     digest("profile-" + label), test_only, "synthetic-validator"))
        con.commit()
        con.close()

    def marker(self):
        con = self.open()
        row = con.execute("SELECT value FROM ledger_meta WHERE key=?", (review_retirement.KEY,)).fetchone()
        con.close()
        return None if row is None else row[0]

    def history(self):
        con = self.open()
        rows = {table: con.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall() for table in HISTORY}
        con.close()
        return rows

    def write_lock_free(self):
        """True when a fresh connection can take the write lock at once (no open writer)."""
        con = self.open(timeout=0)
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            con.close()
            if "locked" in str(error):
                return False
            raise
        con.rollback()
        con.close()
        return True

    def assert_closed(self, con=None):
        con = con or self.con
        self.assertFalse(con.in_transaction, "finish() left its transaction open")
        self.assertTrue(self.write_lock_free(), "write lock still held after finish() failed")

    def assert_failed_closed(self, error_type, message=None, expected_marker=review_retirement.PENDING):
        before = self.history()
        with self.assertRaises(error_type) as caught:
            review_retirement.finish(self.con)
        if message is not None:
            self.assertEqual(str(caught.exception), message)
        self.assert_closed()
        self.assertEqual(self.marker(), expected_marker)
        self.assertEqual(self.history(), before)
        return caught.exception

    # ------------------------------------------------------------ frozen cases
    def test_complete_empty_and_eligible_false(self):
        # No marker and nothing eligible: True, and no marker is created.
        self.seed(marker=None)
        self.assertIs(review_retirement.finish(self.con), True)
        self.assert_closed()
        self.assertIsNone(self.marker())
        # Valid empty completion with a pending marker: True, COMPLETE committed, connection idle.
        con = self.open()
        con.execute("INSERT INTO ledger_meta(key,value) VALUES(?,?)", (review_retirement.KEY, review_retirement.PENDING))
        con.commit()
        con.close()
        before = self.history()
        self.assertIs(review_retirement.finish(self.con), True)
        self.assert_closed()
        self.assertEqual(self.marker(), review_retirement.COMPLETE)
        self.assertEqual(self.history(), before)
        # Idempotent on an already-complete marker.
        self.assertIs(review_retirement.finish(self.con), True)
        self.assertEqual(self.marker(), review_retirement.COMPLETE)
        self.assert_closed()
        # Eligible work (challenge reviewer, or test-only validation) returns False and keeps the hold.
        for label, reviewer, test_only in (("challenge", "challenge:synthetic", 0),
                                           ("test-only", "independent:synthetic", 1)):
            with self.subTest(eligible=label):
                self.seed(marker=None, reviewer=reviewer, test_only=test_only, label=label)
                con = self.open()
                con.execute("UPDATE ledger_meta SET value=? WHERE key=?",
                            (review_retirement.PENDING, review_retirement.KEY))
                con.commit()
                con.close()
                before = self.history()
                self.assertIs(review_retirement.finish(self.con), False)
                self.assert_closed()
                self.assertEqual(self.marker(), review_retirement.PENDING)
                self.assertEqual(self.history(), before)
                con = self.open()
                con.execute("DELETE FROM stage_state WHERE original_sha256=?", (digest("original-" + label),))
                con.commit()
                con.close()

    def test_invalid_marker_kept_and_transaction_closed(self):
        for value in ("invalid", review_retirement.PENDING.replace("pending", "Pending"),
                      '{"state": "pending", "schema": "' + review_retirement.KEY + '"}'):
            with self.subTest(value=value):
                con = self.open()
                con.execute("INSERT INTO ledger_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE "
                            "SET value=excluded.value", (review_retirement.KEY, value))
                con.commit()
                con.close()
                error = self.assert_failed_closed(review_retirement.RetirementError, INVALID, expected_marker=value)
                self.assertIsInstance(error, ValueError)
                self.assertEqual(self.con.rollbacks, 1)
                self.con.rollbacks = 0

    def test_selection_and_write_failure(self):
        self.seed()
        # Eligible-queue selection fails before any write.
        self.con.faults = [(SELECT_ELIGIBLE, "before", InjectedFault("synthetic selection failure"))]
        self.assert_failed_closed(InjectedFault, "synthetic selection failure")
        # State write is refused before it runs.
        self.con.faults = [(WRITE_MARKER, "before", InjectedFault("synthetic write refused"))]
        self.assert_failed_closed(InjectedFault, "synthetic write refused")
        # State write runs (tentative COMPLETE) and then fails: the tentative value must not survive.
        self.con.faults = [(WRITE_MARKER, "after", InjectedFault("synthetic write failure after write"))]
        self.assert_failed_closed(InjectedFault, "synthetic write failure after write")
        row = self.con.execute("SELECT value FROM ledger_meta WHERE key=?", (review_retirement.KEY,)).fetchone()
        self.assertEqual(row[0], review_retirement.PENDING)
        self.assertEqual(self.con.rollbacks, 3)

    def test_commit_failure_and_keyboard_interrupt(self):
        self.seed()
        # Commit fails after the tentative COMPLETE write: never True, tentative state discarded.
        self.con.commit_fault = InjectedFault("synthetic commit failure")
        self.assert_failed_closed(InjectedFault, "synthetic commit failure")
        self.con.commit_fault = None
        # Interruption during selection and during the state write re-raises KeyboardInterrupt.
        for fragment, when in ((SELECT_ELIGIBLE, "before"), (WRITE_MARKER, "after")):
            with self.subTest(interrupt=fragment, when=when):
                self.con.faults = [(fragment, when, KeyboardInterrupt())]
                self.assert_failed_closed(KeyboardInterrupt)
        # Interruption at commit.
        self.con.faults = []
        self.con.commit_fault = KeyboardInterrupt()
        self.assert_failed_closed(KeyboardInterrupt)
        self.assertEqual(self.con.rollbacks, 4)

    def test_caller_transaction_begin_failure_preserved(self):
        self.seed()
        # A caller already owns a transaction on this connection with a tentative write.
        self.con.execute("INSERT INTO ledger_meta(key,value) VALUES('fc7-caller-key','caller-tentative')")
        self.assertTrue(self.con.in_transaction)
        with self.assertRaises(sqlite3.OperationalError) as caught:
            review_retirement.finish(self.con)
        self.assertIn("within a transaction", str(caught.exception))
        self.assertEqual(self.con.rollbacks, 0)
        self.assertTrue(self.con.in_transaction, "caller-owned transaction was ended by finish()")
        self.assertEqual(self.con.execute("SELECT value FROM ledger_meta WHERE key='fc7-caller-key'").fetchone()[0],
                         "caller-tentative")
        self.con.commit()
        self.assertEqual(self.open().execute("SELECT value FROM ledger_meta WHERE key='fc7-caller-key'").fetchone()[0],
                         "caller-tentative")
        self.assertEqual(self.marker(), review_retirement.PENDING)
        # BEGIN refused because another writer holds the lock: nothing is rolled back, the other writer survives.
        other = self.open()
        other.execute("BEGIN IMMEDIATE")
        other.execute("UPDATE ledger_meta SET value='other-tentative' WHERE key='fc7-caller-key'")
        busy = self.open(FaultConnection, timeout=0)
        with self.assertRaises(sqlite3.OperationalError) as caught:
            review_retirement.finish(busy)
        self.assertIn("locked", str(caught.exception))
        self.assertEqual(busy.rollbacks, 0)
        self.assertFalse(busy.in_transaction)
        self.assertTrue(other.in_transaction)
        other.commit()
        self.assertEqual(self.open().execute("SELECT value FROM ledger_meta WHERE key='fc7-caller-key'").fetchone()[0],
                         "other-tentative")
        self.assertEqual(self.marker(), review_retirement.PENDING)

    def test_fresh_connection_sees_prior_marker(self):
        self.seed()
        before = self.history()
        self.con.faults = [(WRITE_MARKER, "after", InjectedFault("synthetic write failure after write"))]
        with self.assertRaises(InjectedFault):
            review_retirement.finish(self.con)
        # The failing connection stays open; a fresh connection reads the committed marker and can write.
        fresh = self.open(timeout=0)
        self.assertEqual(fresh.execute("SELECT value FROM ledger_meta WHERE key=?",
                                       (review_retirement.KEY,)).fetchone()[0], review_retirement.PENDING)
        fresh.execute("BEGIN IMMEDIATE")
        fresh.rollback()
        self.assertEqual(self.history(), before)
        self.assertFalse(self.con.in_transaction)

    def test_retry_with_valid_synthetic_state(self):
        self.seed(marker="invalid")
        with self.assertRaises(review_retirement.RetirementError):
            review_retirement.finish(self.con)
        # Synthetic state becomes valid (test-only re-seed, not a production repair); the same connection retries.
        con = self.open()
        con.execute("UPDATE ledger_meta SET value=? WHERE key=?", (review_retirement.PENDING, review_retirement.KEY))
        con.commit()
        con.close()
        self.assertIs(review_retirement.finish(self.con), True)
        self.assert_closed()
        self.assertEqual(self.marker(), review_retirement.COMPLETE)
        # A transient write failure followed by a clean retry on the same connection also completes.
        con = self.open()
        con.execute("UPDATE ledger_meta SET value=? WHERE key=?", (review_retirement.PENDING, review_retirement.KEY))
        con.commit()
        con.close()
        self.con.faults = [(WRITE_MARKER, "before", InjectedFault("synthetic transient write failure"))]
        with self.assertRaises(InjectedFault):
            review_retirement.finish(self.con)
        self.con.faults = []
        self.assertIs(review_retirement.finish(self.con), True)
        self.assert_closed()
        self.assertEqual(self.marker(), review_retirement.COMPLETE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
