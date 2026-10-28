"""Synthetic detector persistence against the actual WP1 base schema."""
import copy
import json
import sqlite3
import unittest
from unittest import mock

from campaign_tool.records.detectors.core import canonical, digest
from campaign_tool.records.detectors.persistence import PersistenceError, persist_evaluation
from campaign_tool.records.detectors.migrations import persistence_v001 as migration
import importlib.util
LEDGER_AVAILABLE = importlib.util.find_spec("campaign_tool.records.ledger") is not None
if LEDGER_AVAILABLE:
    from campaign_tool.records.ledger.migrations.v001 import SQL as BASE_SQL
from tests.records.test_detectors import fixture


@unittest.skipUnless(LEDGER_AVAILABLE, "WP1 canonical ledger dependency not installed; integration workflow is required")
class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(BASE_SQL)
        self.connection.execute("INSERT INTO ledger_meta VALUES('acceptance','candidate_unreconciled')")
        self.values = fixture("retention-over-policy")
        for subject in {unit["original_sha256"] for unit in self.values[0]}:
            self.connection.execute("INSERT INTO originals VALUES(?,1,'text/plain',NULL,'original','in_scope',NULL,'captured',NULL,'{}')", (subject,))
        self.add_run("run-1")
        subject = self.values[0][0]["original_sha256"]
        for stage in ("preserve", "extract", "catalog", "detect", "review", "compare", "privacy"):
            self.connection.execute("INSERT INTO stage_state VALUES(?,?,'pending',NULL,'synthetic','2020-01-20T00:00:00Z','run-1',NULL)", (subject, stage))
        self.connection.commit()

    def add_run(self, run_id, values=None):
        values = values or self.values
        self.connection.execute("INSERT INTO runs VALUES(?,'detector','2020-01-20T00:00:00Z',NULL,'synthetic-engine-v1',NULL,?,NULL,'running','{}')", (run_id, digest(values[3])))
        self.connection.commit()

    def persist(self, run_id="run-1", values=None):
        units, joins, rules, config = values or self.values
        return persist_evaluation(self.connection, run_id=run_id, detector="retention-over-policy",
                                  units=units, joins=joins, rules=rules, config=config)

    def count(self, table):
        return self.connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_exact_manifest_hashes_and_canonical_link(self):
        receipt = self.persist()
        row = self.connection.execute("SELECT manifest_json,units_sha256,joins_sha256,rules_sha256,config_sha256 FROM wp6_manifests").fetchone()
        manifest = json.loads(row[0])
        self.assertEqual(row[1:], tuple(digest(value) for value in self.values))
        self.assertEqual(manifest["manifest_sha256"], receipt["manifest_sha256"])
        self.assertEqual(manifest["hit_keys"], receipt["hit_keys"])
        self.assertEqual(self.count("detector_runs"), 1)
        self.assertEqual(self.count("detector_hits"), 1)
        self.assertEqual(self.count("wp6_run_hits"), 1)

    def test_exact_replay_is_idempotent(self):
        first, second = self.persist(), self.persist()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual({k:v for k,v in first.items() if k != "replayed"},
                         {k:v for k,v in second.items() if k != "replayed"})
        self.assertEqual(self.count("detector_hits"), 1)
        self.assertEqual(self.count("wp6_run_hits"), 1)

    def test_cross_run_reuses_hit_but_retains_new_print_locations(self):
        first = self.persist()
        values = copy.deepcopy(self.values)
        extra = copy.deepcopy(values[0][0])
        extra["locator"] = {"row": 2}
        values[0].append(extra)
        self.add_run("run-2", values)
        second = self.persist("run-2", values)
        self.assertEqual(first["hit_keys"], second["hit_keys"])
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])
        self.assertEqual(self.count("detector_hits"), 1)
        self.assertEqual(self.count("wp6_run_hits"), 2)
        full = json.loads(self.connection.execute("SELECT hit_json FROM wp6_run_hits WHERE run_id='run-2'").fetchone()[0])
        self.assertEqual(len(full["evidence"]), 2)
        self.assertTrue(self.persist("run-2", values)["replayed"])
        self.assertTrue(self.persist()["replayed"])

    def test_changed_observation_new_run_creates_new_hit(self):
        first = self.persist()
        values = copy.deepcopy(self.values)
        values[0][0]["fields"]["retention_days"] = 40
        self.add_run("run-2", values)
        second = self.persist("run-2", values)
        self.assertNotEqual(first["hit_keys"], second["hit_keys"])
        self.assertEqual(self.count("detector_hits"), 2)

    def test_changed_input_same_run_refused(self):
        self.persist()
        values = copy.deepcopy(self.values)
        values[0][0]["fields"]["retention_days"] = 40
        with self.assertRaisesRegex(PersistenceError, "run_manifest_replay_mismatch"):
            self.persist(values=values)
        self.assertEqual(self.count("detector_hits"), 1)

    def test_no_hits_still_gets_manifest(self):
        self.values[0][0]["fields"]["retention_days"] = 30
        result = self.persist()
        self.assertEqual(result["hit_count"], 0)
        self.assertEqual(self.count("wp6_manifests"), 1)
        self.assertEqual(self.count("detector_hits"), 0)
        self.assertTrue(self.persist()["replayed"])

    def test_blocked_input_is_persisted_not_accepted(self):
        self.values[0][0]["locator"] = {"row": None}
        result = self.persist()
        manifest = json.loads(self.connection.execute("SELECT manifest_json FROM wp6_manifests").fetchone()[0])
        self.assertEqual(manifest["counts"]["blocked"], 1)
        self.assertEqual(result["hit_count"], 0)
        self.assertEqual(result["stage_promotions"], 0)
        self.assertFalse(result["publication_ready"])

    def test_unknown_original_fails_closed(self):
        self.values[0][0]["original_sha256"] = digest("unknown synthetic original")
        with self.assertRaisesRegex(PersistenceError, "canonical_original_missing"):
            self.persist()
        self.assertEqual(self.count("detector_runs"), 0)

    def test_run_missing_or_wrong_config_refused(self):
        with self.assertRaisesRegex(PersistenceError, "canonical_run_missing"):
            self.persist("missing")
        self.values[3]["extra"] = "different"
        with self.assertRaisesRegex(PersistenceError, "run_config_mismatch"):
            self.persist()

    def test_inactive_run_cannot_start_but_completed_run_can_replay(self):
        self.persist()
        self.connection.execute("UPDATE runs SET status='completed',ended_at='2020-01-20T01:00:00Z'")
        self.connection.commit()
        self.assertTrue(self.persist()["replayed"])
        self.add_run("run-2")
        self.connection.execute("UPDATE runs SET status='completed' WHERE run_id='run-2'")
        self.connection.commit()
        with self.assertRaisesRegex(PersistenceError, "new_evaluation_requires_active_run"):
            self.persist("run-2")

    def test_run_provenance_mutation_refused_on_replay(self):
        self.persist()
        self.connection.execute("UPDATE runs SET engine_version='different-engine'")
        self.connection.commit()
        with self.assertRaisesRegex(PersistenceError, "run_manifest_replay_mismatch"):
            self.persist()

    def test_canonical_hit_update_and_replace_detected(self):
        self.persist()
        original = self.connection.execute("SELECT * FROM detector_hits").fetchone()
        self.connection.execute("UPDATE detector_hits SET severity=1")
        self.connection.commit()
        with self.assertRaisesRegex(PersistenceError, "canonical_hit_changed"):
            self.persist()
        changed = list(original)
        changed[4] = 2
        self.connection.execute("PRAGMA recursive_triggers=OFF")
        self.connection.execute("INSERT OR REPLACE INTO detector_hits VALUES(?,?,?,?,?,?,?,?)", changed)
        self.connection.commit()
        with self.assertRaisesRegex(PersistenceError, "canonical_hit_changed"):
            self.persist()

    def test_canonical_detector_counts_mutation_detected(self):
        self.persist()
        self.connection.execute("UPDATE detector_runs SET evaluated=999")
        self.connection.commit()
        with self.assertRaisesRegex(PersistenceError, "canonical_detector_run_changed"):
            self.persist()

    def test_extension_update_delete_replace_guarded_recursive_off(self):
        self.persist()
        self.connection.execute("PRAGMA recursive_triggers=OFF")
        for table in migration.TABLES:
            for sql in (f"UPDATE {table} SET rowid=rowid", f"DELETE FROM {table}",
                        f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
                with self.subTest(table=table, sql=sql):
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.connection.execute(sql)
                    self.connection.rollback()
        self.assertTrue(self.persist()["replayed"])

    def test_link_failure_rolls_back_and_retry_succeeds(self):
        self.connection.execute("BEGIN")
        migration.apply(self.connection)
        self.connection.commit()
        self.connection.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON wp6_run_hits BEGIN SELECT RAISE(ABORT,'synthetic interruption'); END")
        with self.assertRaisesRegex(PersistenceError, "synthetic interruption"):
            self.persist()
        for table in ("detector_runs", "detector_hits", "wp6_manifests", "wp6_hit_identities", "wp6_input_originals", "wp6_run_hits"):
            self.assertEqual(self.count(table), 0)
        self.connection.execute("DROP TRIGGER synthetic_failure")
        self.assertFalse(self.persist()["replayed"])

    def test_no_stage_events_receipts_or_acceptance_changes(self):
        before = {table:list(self.connection.execute("SELECT * FROM " + table))
                  for table in ("stage_state", "stage_events", "receipts", "ledger_meta", "runs")}
        self.persist()
        self.persist()
        after = {table:list(self.connection.execute("SELECT * FROM " + table)) for table in before}
        self.assertEqual(before, after)
        self.assertEqual(self.count("stage_state"), 7)

    def test_unmanaged_canonical_run_not_adopted(self):
        self.connection.execute("INSERT INTO detector_runs VALUES('run-1','unknown','unknown','unknown','unknown',0,0,0,0,'unknown')")
        self.connection.commit()
        with self.assertRaisesRegex(PersistenceError, "unmanaged_detector_run_refused"):
            self.persist()

    def test_partial_extension_not_adopted(self):
        self.connection.execute("CREATE TABLE wp6_manifests(run_id TEXT)")
        with self.assertRaisesRegex(PersistenceError, "partial_detector_extension_refused"):
            self.persist()

    def test_foreign_keys_reject_fresh_unbound_link(self):
        self.persist()
        key = self.connection.execute("SELECT dedupe_key FROM detector_hits").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("INSERT INTO wp6_run_hits VALUES('missing-run',?,'{}','invalid')", (key,))
        self.connection.rollback()

    def test_adapter_owns_transaction_without_committing_callers_work(self):
        self.connection.execute("BEGIN")
        with self.assertRaisesRegex(PersistenceError, "caller_transaction_must_be_closed"):
            self.persist()
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()

    def test_oversize_input_has_no_database_writes(self):
        self.values[0].extend(self.values[0] * 5000)
        with self.assertRaises(PersistenceError):
            self.persist()
        self.assertEqual(self.count("detector_runs"), 0)

    def test_same_named_noop_trigger_is_not_adopted(self):
        self.persist()
        self.connection.execute("DROP TRIGGER wp6_run_hits_no_update")
        self.connection.execute("CREATE TRIGGER wp6_run_hits_no_update BEFORE UPDATE ON wp6_run_hits BEGIN SELECT 1; END")
        with self.assertRaisesRegex(PersistenceError, "detector_extension_definition_mismatch"):
            self.persist()
        self.assertEqual(self.count("detector_runs"), 1)
        self.assertEqual(self.count("wp6_run_hits"), 1)

    def test_changed_table_definition_is_not_adopted(self):
        self.persist()
        self.connection.execute("ALTER TABLE wp6_manifests ADD COLUMN unexpected TEXT")
        with self.assertRaisesRegex(PersistenceError, "detector_extension_definition_mismatch"):
            self.persist()
        self.assertEqual(self.count("detector_runs"), 1)

    def test_same_named_guard_on_wrong_table_is_not_adopted(self):
        self.persist()
        self.connection.execute("DROP TRIGGER wp6_run_hits_no_delete")
        self.connection.execute("CREATE TRIGGER wp6_run_hits_no_delete BEFORE DELETE ON wp6_input_originals BEGIN SELECT RAISE(ABORT,'immutable detector evidence'); END")
        with self.assertRaisesRegex(PersistenceError, "detector_extension_definition_mismatch"):
            self.persist()

    def test_future_started_run_rejected_by_trusted_clock(self):
        from datetime import datetime, timezone
        from campaign_tool.records.detectors import persistence
        self.connection.execute("UPDATE runs SET started_at=?", ("2020-01-20T00:00:01Z",))
        self.connection.commit()
        now = datetime(2020, 1, 20, tzinfo=timezone.utc)
        with mock.patch.object(persistence, "_trusted_now", return_value=now):
            with self.assertRaisesRegex(PersistenceError, "run_started_in_future"):
                self.persist()
        self.assertEqual(self.count("detector_runs"), 0)

    def test_trusted_clock_accepts_exact_start_and_equivalent_offset(self):
        from datetime import datetime, timezone
        from campaign_tool.records.detectors import persistence
        self.connection.execute("UPDATE runs SET started_at=?", ("2020-01-20T01:00:00+01:00",))
        self.connection.commit()
        with mock.patch.object(persistence, "_trusted_now", return_value=datetime(2020, 1, 20, tzinfo=timezone.utc)):
            self.assertFalse(self.persist()["replayed"])
            self.assertTrue(self.persist()["replayed"])

    def test_config_as_of_cannot_authorize_future_start(self):
        from datetime import datetime, timezone
        from campaign_tool.records.detectors import persistence
        self.values[3]["as_of"] = "2099-01-01"
        self.connection.execute("UPDATE runs SET started_at=?,config_sha256=?", ("2020-01-21T00:00:00Z", digest(self.values[3])))
        self.connection.commit()
        with mock.patch.object(persistence, "_trusted_now", return_value=datetime(2020, 1, 20, tzinfo=timezone.utc)):
            with self.assertRaisesRegex(PersistenceError, "run_started_in_future"):
                self.persist()
        self.assertEqual(self.count("detector_runs"), 0)

    def test_future_start_offset_is_compared_as_an_instant(self):
        from datetime import datetime, timezone
        from campaign_tool.records.detectors import persistence
        self.connection.execute("UPDATE runs SET started_at=?", ("2020-01-20T01:00:01+01:00",))
        self.connection.commit()
        with mock.patch.object(persistence, "_trusted_now", return_value=datetime(2020, 1, 20, tzinfo=timezone.utc)):
            with self.assertRaisesRegex(PersistenceError, "run_started_in_future"):
                self.persist()

    def test_row_bounds_precede_serialization(self):
        from campaign_tool.records.detectors import persistence
        cases = [(self.values[0] * 5001, self.values[1], self.values[2], self.values[3]),
                 (self.values[0], self.values[1] * 10001, self.values[2], self.values[3]),
                 (self.values[0], self.values[1], dict(self.values[2], entries=self.values[2]["entries"] * 257), self.values[3])]
        for values in cases:
            with self.subTest(rows=[len(values[0]), len(values[1])]):
                with mock.patch.object(persistence, "canonical", side_effect=AssertionError("must reject before serialization")):
                    with self.assertRaisesRegex(PersistenceError, "row_bound"):
                        self.persist(values=values)
        self.assertEqual(self.count("detector_runs"), 0)

    def test_invalid_shapes_precede_serialization(self):
        from campaign_tool.records.detectors import persistence
        units, joins, rules, config = self.values
        cases = [({}, joins, rules, config), (units, {}, rules, config),
                 (units, joins, [], config), (units, joins, rules, []),
                 ([None], joins, rules, config), (units, [None], rules, config),
                 (units, joins, dict(rules, entries=[None]), config),
                 (units, joins, dict(rules, entries={}), config),
                 (units, joins, rules, {"not_json": object()})]
        for values in cases:
            with mock.patch.object(persistence, "canonical", side_effect=AssertionError("must reject before serialization")):
                with self.assertRaises(PersistenceError):
                    self.persist(values=values)
        self.assertEqual(self.count("detector_runs"), 0)

    def test_nested_shape_bounds_precede_serialization(self):
        from campaign_tool.records.detectors import persistence
        deep = None
        for _ in range(persistence.MAX_STRUCTURE_DEPTH + 1):
            deep = [deep]
        cases = [{"deep": deep}, {"wide": [0] * (persistence.MAX_STRUCTURE_NODES + 1)},
                 {"huge": "x" * (persistence.MAX_INPUT_BYTES + 1)}, {"nonfinite": float("inf")}]
        for config in cases:
            with mock.patch.object(persistence, "canonical", side_effect=AssertionError("must reject before serialization")):
                with self.assertRaises(PersistenceError):
                    self.persist(values=(*self.values[:3], config))
        self.assertEqual(self.count("detector_runs"), 0)

    def test_cyclic_shape_rejected_before_serialization(self):
        from campaign_tool.records.detectors import persistence
        config = {}
        config["cycle"] = config
        with mock.patch.object(persistence, "canonical", side_effect=AssertionError("must reject before serialization")):
            with self.assertRaisesRegex(PersistenceError, "input_structure_bound"):
                self.persist(values=(*self.values[:3], config))

    def test_input_objects_unchanged(self):
        before = canonical(self.values)
        self.persist()
        self.assertEqual(canonical(self.values), before)


if __name__ == "__main__":
    unittest.main()
