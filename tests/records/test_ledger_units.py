"""Synthetic-only unit capture, bounded recovery and provenance regressions."""

import hashlib
import json
import sqlite3
import unittest
from unittest import mock

from campaign_tool.records.ledger import store, unit_projection as projection
from tests.records.test_ledger import LedgerTests


class UnitProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LedgerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.database = self.fixture.output

    def legacy(self, sql, parameters=()):
        with sqlite3.connect(self.fixture.snapshot / "intake.sqlite") as connection:
            connection.execute(sql, parameters)
        self.fixture.refresh_manifest()

    def capture(self):
        return store.import_legacy(self.fixture.snapshot, self.database)["import_id"]

    def rows(self, table):
        with store.ledger(self.database) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)]

    def add_units(self, count):
        with sqlite3.connect(self.fixture.snapshot / "intake.sqlite") as connection:
            sample = connection.execute("SELECT * FROM units").fetchone()
            connection.executemany(
                "INSERT INTO units VALUES(?,?,?,?,?,?)",
                [(sample[0], n, "text", json.dumps({"line": n + 1}),
                  "synthetic " + str(n), "{}") for n in range(1, count)],
            )
        self.fixture.refresh_manifest()

    def test_exact_text_locator_hash_and_missing_provenance(self):
        text = " synthetic\r\nCafe\u0301\x00tail "
        locator = '{ "line" : 1 }'
        self.legacy("UPDATE units SET text=?,locator=?", (text, locator))
        capture = self.capture()
        result = projection.project_units(self.database, capture)
        unit = self.rows("units")[0]
        evidence = self.rows("unit_projection_rows")[0]
        self.assertEqual(result["projected_source_rows"], 1)
        self.assertEqual(unit["locator"], locator)
        self.assertEqual(evidence["observed_text"], text)
        self.assertEqual(unit["text_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(evidence["observed_text_sha256"], unit["text_sha256"])
        self.assertIsNone(unit["parser"])
        self.assertIsNone(unit["parser_version"])
        self.assertEqual(unit["status"], "captured_unverified")
        self.assertFalse(json.loads(unit["provenance_json"])["original_parser_verified"])
        self.assertEqual(result["unresolved_reasons"], {"missing_parser_provenance": 1})
        self.assertEqual(result["adapter"], projection.ADAPTER_VERSION)

    def test_exact_source_row_binding(self):
        capture = self.capture()
        projection.project_units(self.database, capture)
        evidence = self.rows("unit_projection_rows")[0]
        source = next(row for row in self.rows("legacy_rows") if row["source_table"] == "units")
        for key in ("import_id", "source_table", "source_rowid", "payload_sha256"):
            self.assertEqual(evidence[key], source[key])
        self.assertEqual(evidence["integrity_checked"], 1)

    def test_default_one_batch_and_resume(self):
        self.add_units(7)
        capture = self.capture()
        first = projection.project_units(self.database, capture, batch_size=3)
        self.assertEqual(first["processed_source_rows"], 3)
        self.assertEqual(first["status"], "in_progress")
        second = projection.project_units(self.database, capture, batch_size=2, max_batches=2)
        self.assertEqual(second["processed_source_rows"], 7)
        self.assertEqual(second["status"], "complete")
        self.assertEqual(len(self.rows("units")), 7)
        checkpoints = self.rows("unit_projection_checkpoints")
        self.assertEqual(len(checkpoints), 3)
        self.assertEqual(checkpoints[1]["previous_sha256"], checkpoints[0]["checkpoint_sha256"])

    def test_replay_no_duplicates(self):
        capture = self.capture()
        first = projection.project_units(self.database, capture)
        before = {table: self.rows(table) for table in (
            "units", "unit_projection_rows", "unit_projection_checkpoints", "import_gaps")}
        second = projection.project_units(self.database, capture)
        self.assertEqual(first["projection_id"], second["projection_id"])
        self.assertTrue(second["reused"])
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows)

    def test_interrupted_batch_rolls_back_then_resumes(self):
        self.add_units(4)
        capture = self.capture()
        projection.project_units(self.database, capture, batch_size=1)
        original = projection._project_row
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic interruption")
        with mock.patch.object(projection, "_project_row", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                projection.project_units(self.database, capture, batch_size=2)
        self.assertEqual(len(self.rows("units")), 1)
        self.assertEqual(len(self.rows("unit_projection_rows")), 1)
        self.assertEqual(len(self.rows("unit_projection_checkpoints")), 1)
        result = projection.project_units(self.database, capture, batch_size=3)
        self.assertEqual(result["processed_source_rows"], 4)

    def test_byte_bound_stops_batch(self):
        self.add_units(3)
        capture = self.capture()
        result = projection.project_units(
            self.database, capture, batch_size=3, max_row_bytes=300, max_batch_bytes=300)
        self.assertEqual(result["processed_source_rows"], 1)
        self.assertEqual(result["status"], "in_progress")

    def test_oversized_row_is_explicit_gap_without_text_fetch(self):
        self.legacy("UPDATE units SET text=?", ("x" * 4096,))
        capture = self.capture()
        result = projection.project_units(
            self.database, capture, max_row_bytes=300, max_batch_bytes=300)
        self.assertEqual(result["unresolved_reasons"], {"resource_row_too_large": 1})
        self.assertEqual(self.rows("units"), [])
        evidence = self.rows("unit_projection_rows")[0]
        self.assertIsNone(evidence["observed_text"])
        self.assertEqual(evidence["integrity_checked"], 0)
        self.assertEqual(len(self.rows("legacy_rows")), result["expected_source_rows"] + 3)

    def test_resource_policy_changes_do_not_reuse_skipped_run(self):
        self.legacy("UPDATE units SET text=?", ("x" * 1024,))
        capture = self.capture()
        limited = projection.project_units(
            self.database, capture, max_row_bytes=300, max_batch_bytes=300)
        normal = projection.project_units(self.database, capture)
        self.assertNotEqual(limited["projection_id"], normal["projection_id"])
        self.assertEqual(normal["projected_source_rows"], 1)

    def test_invalid_locator_gap(self):
        self.legacy("UPDATE units SET locator=?", ('{"line":1,"line":2}',))
        result = projection.project_units(self.database, self.capture())
        self.assertEqual(result["unresolved_reasons"], {"exact_locator_missing_or_invalid": 1})
        self.assertEqual(self.rows("units"), [])

    def test_mime_locator_preserved_not_reinterpreted(self):
        locator = '{"mime_part_index":3}'
        self.legacy("UPDATE units SET locator=?", (locator,))
        projection.project_units(self.database, self.capture())
        self.assertEqual(self.rows("units")[0]["locator"], locator)
        self.assertEqual(self.rows("occurrences"), [])

    def test_missing_text_gap_and_empty_text_is_observation(self):
        self.legacy("UPDATE units SET text=NULL")
        result = projection.project_units(self.database, self.capture())
        self.assertEqual(result["unresolved_reasons"], {"observed_text_missing_or_invalid": 1})

    def test_empty_text_has_exact_empty_hash(self):
        self.legacy("UPDATE units SET text=''")
        result = projection.project_units(self.database, self.capture())
        self.assertEqual(result["projected_source_rows"], 1)
        self.assertEqual(self.rows("units")[0]["text_sha256"], hashlib.sha256(b"").hexdigest())

    def test_missing_original_is_gap(self):
        self.legacy("UPDATE units SET sha=?", ("f" * 64,))
        result = projection.project_units(self.database, self.capture())
        self.assertEqual(result["unresolved_reasons"], {"canonical_original_missing": 1})
        self.assertEqual(self.rows("units"), [])

    def test_negative_ordinal_gap(self):
        self.legacy("UPDATE units SET ordinal=-1")
        result = projection.project_units(self.database, self.capture())
        self.assertEqual(result["unresolved_reasons"], {"invalid_legacy_ordinal": 1})

    def test_no_stage_or_acceptance_promotion(self):
        capture = self.capture()
        before = {table: self.rows(table) for table in ("stage_state", "stage_events")}
        before_acceptance = store.counts(self.database)["acceptance"]
        result = projection.project_units(self.database, capture)
        self.assertEqual(result["stage_promotions"], 0)
        self.assertIsNone(result["end_to_end_complete"])
        self.assertFalse(result["publication_ready"])
        for table, rows in before.items():
            self.assertEqual(rows, self.rows(table))
        self.assertEqual(before_acceptance, store.counts(self.database)["acceptance"])

    def test_empty_capture_completes_and_replays(self):
        self.legacy("DELETE FROM units")
        capture = self.capture()
        result = projection.project_units(self.database, capture)
        self.assertEqual(result["processed_source_rows"], 0)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(projection.project_units(self.database, capture)["reused"])

    def test_incomplete_capture_rejected(self):
        capture = store.import_legacy(
            self.fixture.snapshot, self.database, batch_size=1, max_batches=1)["import_id"]
        with self.assertRaises(ValueError):
            projection.project_units(self.database, capture)

    def test_invalid_limits_rejected(self):
        for kwargs in ({"batch_size": True}, {"batch_size": 10001},
                       {"max_batches": 0}, {"max_row_bytes": 0},
                       {"max_row_bytes": 301, "max_batch_bytes": 300}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                projection.project_units(self.database, "a" * 64, **kwargs)
        with self.assertRaises(ValueError):
            projection.project_units(self.database, "not-a-capture")

    def test_corrupt_payload_is_not_projected(self):
        source = {"source_rowid": 1, "payload_sha256": "0" * 64,
                  "payload_json": "{}", "identity_json": "{}"}
        with self.assertRaises(ValueError):
            projection._project_row(None, "run", "capture", source, "adapter")

    def test_wrong_identity_is_not_projected(self):
        payload = '{"sha":"' + "a" * 64 + '","ordinal":0}'
        source = {"source_rowid": 1, "payload_sha256": projection._hash(payload),
                  "payload_json": payload, "identity_json": "{}"}
        with self.assertRaises(ValueError):
            projection._project_row(None, "run", "capture", source, "adapter")

    def test_extension_evidence_is_immutable(self):
        capture = self.capture()
        projection.project_units(self.database, capture)
        with store.ledger(self.database) as connection:
            for table in ("unit_projection_runs", "unit_projection_rows", "unit_projection_checkpoints"):
                with self.subTest(table=table):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute("DELETE FROM " + table)
                    connection.rollback()
                    with self.assertRaises(sqlite3.Error):
                        connection.execute("INSERT OR REPLACE INTO " + table + " SELECT * FROM " + table)
                    connection.rollback()


    def _guarded_capture(self, partial=False):
        if partial:
            self.add_units(2)
        capture = self.capture()
        projection.project_units(self.database, capture, batch_size=1)
        return capture

    def _finish_guarded_capture(self, capture, partial):
        result = projection.project_units(self.database, capture, batch_size=1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["processed_source_rows"], 2 if partial else 1)
        self.assertEqual(result["reused"], not partial)
        self.assertEqual(result["stage_promotions"], 0)
        for unit in self.rows("units"):
            self.assertIsNone(unit["parser"])
            self.assertIsNone(unit["parser_version"])
            self.assertEqual(unit["status"], "captured_unverified")

    def _reject_mutations(self, partial):
        capture = self._guarded_capture(partial)
        before = self.rows("units")
        fields = {
            "text_sha256": "f" * 64, "locator": '{"line":999}',
            "original_sha256": "e" * 64, "parser": "invented",
            "parser_version": "invented", "status": "accepted",
            "provenance_json": "{}", "derived_path": "invented",
            "legacy_ordinal": 999, "unit_type": "invented", "id": "replacement",
        }
        with store.ledger(self.database) as connection:
            for field, value in fields.items():
                with self.subTest(field=field), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE units SET " + field + "=?", (value,))
                connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM units")
            connection.rollback()
        self.assertEqual(self.rows("units"), before)
        self._finish_guarded_capture(capture, partial)

    def test_completed_replay_rejects_canonical_mutations(self):
        self._reject_mutations(False)

    def test_partial_resume_rejects_canonical_mutations(self):
        self._reject_mutations(True)

    def _reject_replace(self, partial):
        capture = self._guarded_capture(partial)
        before = self.rows("units")
        with store.ledger(self.database) as connection:
            connection.execute("PRAGMA recursive_triggers=OFF")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT OR REPLACE INTO units SELECT id,original_sha256,parser,"
                    "parser_version,locator,unit_type,'wrong',derived_path,status,"
                    "legacy_ordinal,provenance_json FROM units")
            connection.rollback()
        self.assertEqual(self.rows("units"), before)
        self._finish_guarded_capture(capture, partial)

    def test_completed_replay_rejects_canonical_replace(self):
        self._reject_replace(False)

    def test_partial_resume_rejects_canonical_replace(self):
        self._reject_replace(True)

    def _reject_source_mutations(self, partial):
        capture = self._guarded_capture(partial)
        with store.ledger(self.database) as connection:
            connection.execute("PRAGMA recursive_triggers=OFF")
            for sql in (
                "UPDATE legacy_rows SET payload_json='{}' WHERE source_table='units'",
                "UPDATE legacy_rows SET identity_json='{}' WHERE source_table='units'",
                "DELETE FROM legacy_rows WHERE source_table='units'",
                "INSERT OR REPLACE INTO legacy_rows SELECT * FROM legacy_rows WHERE source_table='units'",
            ):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql)
                connection.rollback()
        self._finish_guarded_capture(capture, partial)

    def test_completed_replay_rejects_source_mutations(self):
        self._reject_source_mutations(False)

    def test_partial_resume_rejects_source_mutations(self):
        self._reject_source_mutations(True)

    def _reject_removed_guard(self, partial):
        capture = self._guarded_capture(partial)
        with store.ledger(self.database) as connection:
            connection.execute("DROP TRIGGER unit_capture_guard_units_update")
            connection.execute("UPDATE units SET status='accepted'")
            connection.commit()
        before = self.rows("unit_projection_checkpoints")
        with self.assertRaisesRegex(ValueError, "protection missing or changed"):
            projection.project_units(self.database, capture)
        self.assertEqual(self.rows("unit_projection_checkpoints"), before)

    def test_completed_replay_fails_closed_when_guard_removed(self):
        self._reject_removed_guard(False)

    def test_partial_resume_fails_closed_when_guard_removed(self):
        self._reject_removed_guard(True)

    def _reject_unguarded_history(self, partial):
        capture = self._guarded_capture(partial)
        with store.ledger(self.database) as connection:
            for statement in projection._statements(projection.guards.SQL):
                kind, name = statement.split()[1:3]
                connection.execute("DROP " + kind + " " + name.split("(")[0])
            connection.execute(
                "DELETE FROM ledger_meta WHERE key=?", (projection.guards.MARKER,))
            connection.commit()
        before = self.rows("unit_projection_checkpoints")
        with self.assertRaisesRegex(ValueError, "unguarded unit history"):
            projection.project_units(self.database, capture)
        self.assertEqual(self.rows("unit_projection_checkpoints"), before)

    def test_completed_unguarded_history_not_grandfathered(self):
        self._reject_unguarded_history(False)

    def test_partial_unguarded_history_not_grandfathered(self):
        self._reject_unguarded_history(True)

    def test_cross_capture_reuse_remains_valid(self):
        first = self.capture()
        projection.project_units(self.database, first)
        original_unit = self.rows("units")[0]
        self.add_units(2)
        second = self.capture()
        self.assertNotEqual(first, second)
        result = projection.project_units(self.database, second, max_batches=2)
        self.assertEqual(result["projected_source_rows"], 2)
        self.assertEqual(len(self.rows("units")), 2)
        self.assertIn(original_unit, self.rows("units"))
        self.assertEqual(len(self.rows("unit_projection_rows")), 3)

    def test_completed_replay_never_reprojects_prior_payloads(self):
        capture = self._guarded_capture(False)
        with mock.patch.object(projection, "_project_row", side_effect=AssertionError("reprocessed")):
            result = projection.project_units(self.database, capture)
        self.assertTrue(result["reused"])

    def test_binding_guard_marker_cannot_be_replaced(self):
        self._guarded_capture(False)
        with store.ledger(self.database) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT OR REPLACE INTO ledger_meta(key,value) VALUES(?,?)",
                    (projection.guards.MARKER, "invented"))
            connection.rollback()


    def _reject_incoming_unit_collision(self, partial):
        capture = self._guarded_capture(partial)
        original = self.rows("units")[0]
        with store.ledger(self.database) as connection:
            connection.execute("PRAGMA recursive_triggers=OFF")
            columns = list(original)
            alternate = dict(original, id="unbound-synthetic", status="untrusted")
            connection.execute(
                "INSERT INTO units(" + ",".join(columns) + ") VALUES(" +
                ",".join("?" for _ in columns) + ")", [alternate[key] for key in columns])
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE OR REPLACE units SET id=? WHERE id='unbound-synthetic'",
                    (original["id"],))
            connection.rollback()
            self.assertEqual(dict(connection.execute(
                "SELECT * FROM units WHERE id=?", (original["id"],)).fetchone()), original)
            connection.execute("DELETE FROM units WHERE id='unbound-synthetic'")
            connection.commit()
        self._finish_guarded_capture(capture, partial)

    def test_completed_replay_rejects_incoming_update_replace(self):
        self._reject_incoming_unit_collision(False)

    def test_partial_resume_rejects_incoming_update_replace(self):
        self._reject_incoming_unit_collision(True)

    def test_incoming_metadata_marker_collision_rejected(self):
        self._guarded_capture(False)
        with store.ledger(self.database) as connection:
            connection.execute("PRAGMA recursive_triggers=OFF")
            connection.execute("INSERT INTO ledger_meta VALUES('unbound-synthetic','wrong')")
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE OR REPLACE ledger_meta SET key=? WHERE key='unbound-synthetic'",
                    (projection.guards.MARKER,))
            connection.rollback()

    def test_incoming_source_identity_collision_rejected(self):
        first = self.capture()
        self.add_units(2)
        second = self.capture()
        projection.project_units(self.database, first)
        with store.ledger(self.database) as connection:
            connection.execute("PRAGMA recursive_triggers=OFF")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE OR REPLACE legacy_rows SET import_id=? WHERE import_id=? "
                    "AND source_table='units' AND source_rowid=1", (first, second))
            connection.rollback()
        self.assertTrue(projection.project_units(self.database, first)["reused"])

    def _forged_checkpoint(self, partial):
        capture = self._guarded_capture(partial)
        old = self.rows("unit_projection_checkpoints")[-1]
        summary = json.loads(old["summary_json"])
        summary["batches"] += 1
        summary["status"] = "complete"
        summary["processed_source_rows"] = summary["expected_source_rows"]
        summary["projected_source_rows"] = summary["expected_source_rows"]
        summary["unresolved_reasons"] = {"missing_parser_provenance": summary["expected_source_rows"]}
        last = summary["expected_source_rows"]
        values = (old["projection_id"], summary["batches"], last,
                  old["checkpoint_sha256"], projection._hash(store.canonical(
                      [old["projection_id"], summary["batches"], last,
                       old["checkpoint_sha256"], summary])), store.canonical(summary))
        with store.ledger(self.database) as connection:
            # Ordinary SQL cannot append fresh keys.
            with self.assertRaises(sqlite3.Error):
                connection.execute("INSERT INTO unit_projection_checkpoints VALUES(?,?,?,?,?,?)", values)
            connection.rollback()
            # Even the author capability cannot authorize a relationally false checkpoint.
            permit = projection._AppendPermit(connection)
            with permit.allow("checkpoint", values), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO unit_projection_checkpoints VALUES(?,?,?,?,?,?)", values)
            connection.rollback()
        self.assertEqual(len(self.rows("unit_projection_checkpoints")), 1)
        self._finish_guarded_capture(capture, partial)

    def test_completed_replay_rejects_fresh_forged_checkpoint(self):
        self._forged_checkpoint(False)

    def test_partial_resume_rejects_fresh_forged_checkpoint(self):
        self._forged_checkpoint(True)

    def _mismatched_association(self, partial):
        self.add_units(2)
        capture = self.capture()
        projection.project_units(self.database, capture, batch_size=1,
                                 max_batches=1 if partial else 2)
        run = self.rows("unit_projection_runs")[0]["id"]
        first = self.rows("unit_projection_rows")[0]
        source = next(row for row in self.rows("legacy_rows")
                      if row["source_table"] == "units" and row["source_rowid"] == 2)
        # Partial case targets a real unprocessed source; completed case uses a fresh nonexistent key.
        rowid = 2 if partial else 3
        values = (run, capture, rowid, source["payload_sha256"], first["unit_id"],
                  "projected", "missing_parser_provenance", first["observed_text"],
                  first["observed_text_sha256"], first["observed_locator"], 1)
        sql = "INSERT INTO unit_projection_rows VALUES(?,?,'units',?,?,?,?,?,?,?,?,?)"
        with store.ledger(self.database) as connection:
            with self.assertRaises(sqlite3.Error):
                connection.execute(sql, values)
            connection.rollback()
            permit = projection._AppendPermit(connection)
            with permit.allow("row", values), self.assertRaises(sqlite3.IntegrityError):
                connection.execute(sql, values)
            connection.rollback()
            wrong_hash = list(values)
            wrong_hash[3] = "0" * 64
            with permit.allow("row", wrong_hash), self.assertRaises(sqlite3.IntegrityError):
                connection.execute(sql, wrong_hash)
            connection.rollback()
        result = projection.project_units(self.database, capture)
        self.assertEqual(result["processed_source_rows"], 2)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(self.rows("unit_projection_rows")), 2)

    def test_completed_replay_rejects_fresh_mismatched_association(self):
        self._mismatched_association(False)

    def test_partial_resume_rejects_fresh_mismatched_association(self):
        self._mismatched_association(True)

    def test_checkpoint_rejects_false_disposition_counts(self):
        self.add_units(2)
        capture = self.capture()
        projection.project_units(self.database, capture, batch_size=1)
        old = self.rows("unit_projection_checkpoints")[0]
        with store.ledger(self.database) as connection:
            permit = projection._AppendPermit(connection)
            source = connection.execute(
                "SELECT * FROM legacy_rows WHERE import_id=? AND source_table='units' "
                "AND source_rowid=2", (capture,)).fetchone()
            adapter_hash = self.rows("unit_projection_runs")[0]["adapter_sha256"]
            projection._project_row(connection, old["projection_id"], capture,
                                    source, adapter_hash, permit=permit)
            summary = json.loads(old["summary_json"])
            summary.update(batches=2, status="complete", processed_source_rows=2,
                           projected_source_rows=1, blocked_source_rows=1)
            summary["unresolved_reasons"]["missing_parser_provenance"] = 2
            values = (old["projection_id"], 2, 2, old["checkpoint_sha256"],
                      projection._hash(store.canonical(
                          [old["projection_id"], 2, 2, old["checkpoint_sha256"], summary])),
                      store.canonical(summary))
            with permit.allow("checkpoint", values), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO unit_projection_checkpoints VALUES(?,?,?,?,?,?)", values)
            connection.rollback()
        result = projection.project_units(self.database, capture)
        self.assertEqual(result["projected_source_rows"], 2)

    def test_run_bound_source_suffix_cannot_be_changed(self):
        capture = self._guarded_capture(True)
        with store.ledger(self.database) as connection:
            for sql in (
                "UPDATE legacy_rows SET payload_json='{}' WHERE source_table='units' AND source_rowid=2",
                "DELETE FROM legacy_rows WHERE source_table='units' AND source_rowid=2",
                "INSERT OR REPLACE INTO legacy_rows SELECT * FROM legacy_rows "
                "WHERE source_table='units' AND source_rowid=2",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql)
                connection.rollback()
        self._finish_guarded_capture(capture, True)



if __name__ == "__main__":
    unittest.main()
