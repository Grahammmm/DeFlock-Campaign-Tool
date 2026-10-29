"""FC7-DETECTOR-RUN-CLOCK: impossible runs.started_at values never reach detector evidence.

Synthetic, in-memory only. The schema, synthetic run and detector inputs are the
source-shaped ones from tests.records.test_detector_persistence.PersistenceTests;
only runs.started_at is varied. A fixed trusted clock is installed for every case.
Machine-valid output here is REVIEW REQUIRED, never evidence acceptance.
"""
from datetime import datetime, timezone
import sqlite3
import unittest
from unittest import mock

from campaign_tool.records.detectors import persistence
from campaign_tool.records.detectors.persistence import PersistenceError
from tests.records import test_detector_persistence as base

if not base.LEDGER_AVAILABLE:  # fail closed: this contract may not be skipped
    raise ImportError("FC7 run-clock tests require the WP1 canonical ledger schema")

TRUSTED_NOW = datetime(2020, 1, 21, tzinfo=timezone.utc)
INVALID = "run_started_at_invalid"
EVIDENCE_TABLES = ("detector_runs", "detector_hits", "wp6_manifests", "wp6_hit_identities",
                   "wp6_input_originals", "wp6_run_hits")

VALID_TIMES = (
    "2020-01-20T00:00:00Z",
    "2020-01-20T00:00:00+00:00",
    "2020-01-20T00:00:00.5Z",
    "2020-01-20T00:00:00.123456+00:00",
    "2020-01-20T00:00:00.123456789Z",
    "2020-01-20T05:30:00.123+05:30",
    "2020-01-19T19:00:00-05:00",
    "2020-01-20T00:00:00-00:00",
    "2020-01-20T23:59:59+23:59",
    "2020-01-19T00:00:00-23:59",
    "2016-02-29T00:00:00Z",
)

IMPOSSIBLE_OFFSETS = (
    "2020-01-20T00:00:00+00:60",
    "2020-01-20T00:00:00+00:99",
    "2020-01-20T00:00:00-00:99",
    "2020-01-20T00:00:00.5+00:99",
    "2020-01-20T00:00:00+23:60",
    "2020-01-20T00:00:00+24:00",
    "2020-01-20T00:00:00-24:00",
    "2020-01-20T00:00:00+99:00",
    "2020-01-20T00:00:00+99:99",
)

NAIVE_OR_MALFORMED = (
    "2020-01-20T00:00:00",
    "2020-01-20T00:00:00.123456",
    "2020-01-20",
    "",
    " ",
    "2020-01-20 00:00:00Z",
    "20200120T000000Z",
    "2020-01-20T00:00:00+0000",
    "2020-01-20T00:00:00+00",
    "2020-01-20T00:00:00+00:00:00",
    "2020-01-20T00:00:00,5Z",
    "2020-01-20T00:00:00.Z",
    "2020-01-20T00:00:00z",
    "2020-01-20t00:00:00Z",
    "2020-01-20T00:00Z",
    "2020-01-20T00:00:00Z\n",
    " 2020-01-20T00:00:00Z",
    "2020-01-20T00:00:00ZZ",
    "2020-W04-1T00:00:00Z",
    "\uff12020-01-20T00:00:00Z",
    "2020-01-20T00:00:00+\u0660\u0660:\u0660\u0660",
    "20200120",
)

CALENDAR_ERRORS = (
    "2020-02-30T00:00:00Z",
    "2021-02-29T00:00:00Z",
    "2020-04-31T00:00:00Z",
    "2020-13-01T00:00:00Z",
    "2020-00-10T00:00:00Z",
    "2020-01-00T00:00:00Z",
    "2020-01-32T00:00:00Z",
    "2020-01-20T24:00:00Z",
    "2020-01-20T00:60:00Z",
    "2020-01-20T00:00:60Z",
    "0000-01-20T00:00:00Z",
)


class RunClockTests(unittest.TestCase):
    setUp_base = base.PersistenceTests.setUp
    add_run = base.PersistenceTests.add_run
    persist = base.PersistenceTests.persist
    count = base.PersistenceTests.count

    def setUp(self):
        self.setUp_base()
        clock = mock.patch.object(persistence, "_trusted_now", return_value=TRUSTED_NOW)
        clock.start()
        self.addCleanup(clock.stop)

    # -- helpers -----------------------------------------------------------
    def set_started_at(self, value, run_id="run-1"):
        self.connection.execute("UPDATE runs SET started_at=? WHERE run_id=?", (value, run_id))
        self.connection.commit()
        stored = self.connection.execute("SELECT started_at FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
        self.assertEqual(stored, value)  # only runs.started_at changed, stored verbatim

    def snapshot(self):
        tables = [row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        schema = list(self.connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
        rows = {table: sorted(self.connection.execute(f'SELECT * FROM "{table}"'), key=repr)
                for table in tables}
        return {"schema": schema, "rows": rows}

    def assert_rejected(self, run_id="run-1"):
        before = self.snapshot()
        with self.assertRaises(PersistenceError) as caught:
            self.persist(run_id)
        self.assertEqual(str(caught.exception), INVALID)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.snapshot(), before)
        return before

    def assert_no_evidence(self):
        names = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master")}
        for table in EVIDENCE_TABLES:
            if table in names:
                self.assertEqual(self.count(table), 0, table)

    # -- frozen case IDs ---------------------------------------------------
    def test_valid_z_offset_and_fractional_times(self):
        """valid_z_offset_and_fractional_times"""
        keys = set()
        for index, value in enumerate(VALID_TIMES):
            with self.subTest(started_at=value):
                run_id = f"run-valid-{index}"
                self.add_run(run_id)
                self.set_started_at(value, run_id)
                receipt = self.persist(run_id)
                self.assertFalse(receipt["replayed"])
                self.assertEqual(receipt["run_id"], run_id)
                self.assertFalse(receipt["publication_ready"])
                self.assertEqual(receipt["stage_promotions"], 0)
                self.assertEqual(receipt["hit_count"], 1)
                keys.update(receipt["hit_keys"])
                saved = self.connection.execute(
                    "SELECT started_at FROM detector_runs WHERE run_id=?", (run_id,)).fetchone()
                self.assertEqual(saved, (value,))
                self.assertTrue(self.persist(run_id)["replayed"])
        self.assertEqual(self.count("detector_runs"), len(VALID_TIMES))
        self.assertEqual(self.count("wp6_manifests"), len(VALID_TIMES))
        self.assertEqual(self.count("wp6_run_hits"), len(VALID_TIMES))
        self.assertEqual(self.count("detector_hits"), len(keys))

    def test_offset_minute_60_99_and_hour_24(self):
        """offset_minute_60_99_and_hour_24"""
        for value in IMPOSSIBLE_OFFSETS:
            with self.subTest(started_at=value):
                self.set_started_at(value)
                self.assert_rejected()
                self.assert_no_evidence()
        # The unrelated bindings stay valid: the same run persists once the clock field is real.
        self.set_started_at("2020-01-20T00:00:00+00:59")
        receipt = self.persist()
        self.assertFalse(receipt["replayed"])
        self.assertEqual(self.count("detector_runs"), 1)

    def test_naive_null_nonstring_and_calendar_errors(self):
        """naive_null_nonstring_and_calendar_errors"""
        for value in NAIVE_OR_MALFORMED + CALENDAR_ERRORS:
            with self.subTest(started_at=value):
                self.set_started_at(value)
                self.assert_rejected()
        for value in (b"2020-01-20T00:00:00Z", bytes(4)):
            with self.subTest(started_at=value):
                self.set_started_at(value)  # BLOB keeps its storage class: a non-string run clock
                self.assert_rejected()
        # SQLite TEXT affinity stores numbers as text; the stored text is still not a run clock.
        self.connection.execute("UPDATE runs SET started_at=?", (20200120,))
        self.connection.commit()
        self.assert_rejected()
        # The canonical schema refuses NULL; a NULL read at the boundary is still refused.
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE runs SET started_at=NULL")
        self.connection.rollback()
        self.set_started_at("2020-01-20T00:00:00Z")
        real_one = persistence.one
        for value in (None, 20200120, 1.5, ["2020-01-20T00:00:00Z"]):
            with self.subTest(read_value=value):
                def odd_clock(connection, sql, values=(), value=value):
                    row = real_one(connection, sql, values)
                    if sql.startswith("SELECT * FROM runs ") and row is not None:
                        row["started_at"] = value
                    return row
                with mock.patch.object(persistence, "one", side_effect=odd_clock):
                    self.assert_rejected()
        self.assert_no_evidence()
        self.assertFalse(self.persist()["replayed"])

    def test_no_migration_or_evidence_write_on_invalid(self):
        """no_migration_or_evidence_write_on_invalid"""
        self.set_started_at("2020-01-20T00:00:00+00:99")
        with mock.patch.object(persistence.migration, "apply",
                               side_effect=AssertionError("migration reached")) as apply:
            before = self.assert_rejected()
        apply.assert_not_called()
        self.assertFalse(any(name.startswith("wp6_") for _, name, _, _ in before["schema"]))
        self.assert_no_evidence()
        # With the extension already installed, an invalid run clock still writes nothing.
        self.connection.execute("BEGIN")
        persistence.migration.apply(self.connection)
        self.connection.commit()
        for value in ("2020-01-20T00:00:00+24:00", "2020-02-30T00:00:00Z", "2020-01-20T00:00:00"):
            with self.subTest(started_at=value):
                self.set_started_at(value)
                self.assert_rejected()
                self.assert_no_evidence()
        runs = list(self.connection.execute("SELECT * FROM runs"))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][8], "running")

    def test_valid_replay_idempotence(self):
        """valid_replay_idempotence"""
        value = "2020-01-20T05:30:00.250+05:30"
        self.set_started_at(value)
        first = self.persist()
        middle = self.snapshot()
        second = self.persist()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual({k: v for k, v in first.items() if k != "replayed"},
                         {k: v for k, v in second.items() if k != "replayed"})
        self.assertEqual(self.snapshot(), middle)
        # Old-run rewrite is not offered: corrupting the saved run's clock blocks replay without writes.
        self.set_started_at("2020-01-20T05:30:00.250+05:99")
        self.assert_rejected()
        self.set_started_at(value)
        self.assertTrue(self.persist()["replayed"])
        self.assertEqual(self.snapshot(), middle)

    def test_future_run_rule_and_publication_flag(self):
        """future_run_rule_and_publication_flag"""
        self.set_started_at("2020-01-21T00:00:00.000001Z")
        with self.assertRaises(PersistenceError) as caught:
            self.persist()
        self.assertEqual(str(caught.exception), "run_started_in_future")
        self.set_started_at("2020-01-21T01:00:01+01:00")
        with self.assertRaises(PersistenceError) as caught:
            self.persist()
        self.assertEqual(str(caught.exception), "run_started_in_future")
        # Invalid fields are refused as invalid, not mistaken for a future or past instant.
        for value in ("2099-01-01T00:00:00+00:99", "2020-01-20T00:00:00+24:00", "2099-02-30T00:00:00Z"):
            with self.subTest(started_at=value):
                self.set_started_at(value)
                self.assert_rejected()
        self.assertEqual(self.count("detector_runs"), 0)
        self.set_started_at("2020-01-21T01:00:00+01:00")  # exactly the trusted instant
        receipt = self.persist()
        self.assertFalse(receipt["replayed"])
        self.assertFalse(receipt["publication_ready"])
        self.assertEqual(receipt["stage_promotions"], 0)
        self.assertEqual(receipt["acceptance"], "supplied_evidence_triage_only")
        self.assertIs(self.persist()["publication_ready"], False)


if __name__ == "__main__":
    unittest.main()
