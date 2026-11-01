"""FA1404 synthetic regression: private snapshot history refuses INSERT OR REPLACE.

Every database here is generated inside a fixture-owned temporary root by the
real ledger_catalog / ledger_catalog_acceptance APIs. No committed database,
private record or host path is used.
"""
import hashlib
import re
import sqlite3
import unittest
from unittest import mock

from tests.records import test_ledger_catalog as fixtures
from contextlib import closing
store, catalog_links, H1, H2 = fixtures.store, fixtures.catalog_links, fixtures.H1, fixtures.H2
from campaign_tool.records import ledger_catalog as catalog
from campaign_tool.records import ledger_catalog_acceptance as acceptance

RESERVED_TRIGGER = "immutable_events_insert_reserved"
ADDED_TRIGGERS = ("immutable_acceptances_insert_collision", "immutable_events_insert_collision",
                  RESERVED_TRIGGER)
ADDED_TABLES = {"immutable_acceptances_insert_collision": "snapshot_acceptances",
                "immutable_events_insert_collision": "snapshot_events",
                RESERVED_TRIGGER: "snapshot_events"}
BASELINE_TRIGGERS = ("immutable_acceptances_update", "immutable_acceptances_delete",
                     "immutable_events_update", "immutable_events_delete")
ACCEPTANCE_REFUSAL = "immutable snapshot acceptance insert collision"
HISTORY_REFUSAL = "immutable snapshot history insert collision"
RESERVED_REFUSAL = "immutable snapshot history reserved sequence"
BASELINE_HISTORY_REFUSAL = "immutable snapshot history"
PRESERVATION_MARKER = "FA1404-PRESERVATION history changed by colliding replacement"
# SQLite hands a BEFORE INSERT trigger NEW.sequence=-1 both for an explicit -1
# and for a not-yet-assigned AUTOINCREMENT value, so -1 is the reserved slot.
RESERVED_SEQUENCE = -1
RECEIPT_KEYS = {"schema_version", "snapshot_id", "run_id", "catalog_sha256", "manifest_sha256",
                "accepted_at", "previous_snapshot_id", "source_check", "catalog_snapshot_accepted",
                "stage_promotions", "document_review_approval", "publication_ready"}
FRESH_ID = hashlib.sha256(b"fa1404 synthetic noncolliding snapshot").hexdigest()


def without_added_guards(sql, names=ADDED_TRIGGERS):
    """Gate-control SQL: the candidate text minus ONLY the named added triggers.

    Raises ValueError unless exactly the named added statements are removed and
    every other trigger (the four baseline update/delete guards and any added
    trigger not named) remains byte-identical.
    """
    if not names or not set(names) <= set(ADDED_TRIGGERS):
        raise ValueError("fa1404_control_variant_names:" + ",".join(names))
    pattern = re.compile(r"CREATE TRIGGER IF NOT EXISTS (" + "|".join(names) + r")\b.*?\bEND;\n", re.S)
    removed = [m.group(1) for m in pattern.finditer(sql)]
    if sorted(removed) != sorted(names):
        raise ValueError("fa1404_control_variant_mismatch:" + ",".join(removed))
    variant = pattern.sub("", sql)
    kept = BASELINE_TRIGGERS + tuple(n for n in ADDED_TRIGGERS if n not in names
                                     and re.search(r"CREATE TRIGGER IF NOT EXISTS " + n + r"\b", sql))
    for name in kept:
        original = re.search(r"CREATE TRIGGER IF NOT EXISTS " + name + r"\b.*?\bEND;", sql, re.S)
        if original is None or original.group(0) not in variant:
            raise ValueError("fa1404_control_variant_baseline_changed:" + name)
    return variant


@unittest.skipUnless(store is not None and catalog_links is not None, "WP1/PR19 dependency overlays required")
class FA1404NoReplaceTests(unittest.TestCase):
    setUp = fixtures.CanonicalCatalogTests.setUp
    join = fixtures.CanonicalCatalogTests.join

    # -- synthetic acceptance fixture through the real APIs -------------------
    def prepare(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET status='completed',ended_at='2026-01-03T00:00:00+00:00',"
                        "config_sha256=? WHERE run_id='synthetic'", (H2,))
        result = catalog.build(self.db, self.root, run_id="synthetic")
        catalog.export(result, self.output)
        payloads = catalog.artifact_payloads(result)
        return result, {"run_id": "synthetic",
                        "expected_catalog_sha256": catalog.digest(payloads["catalog.json"]),
                        "expected_manifest_sha256": catalog.digest(payloads["artifact-hashes.json"])}

    def accept(self, result, args):
        return acceptance.accept_snapshot(self.db, self.root, self.output, result["snapshot_id"], **args)

    @property
    def history_db(self):
        return self.output / "accepted-snapshots.sqlite"

    def two_snapshots(self):
        old, old_args = self.prepare()
        first = self.accept(old, old_args)
        self.join("fa1404-second")
        new, new_args = self.prepare()
        new_args["expected_current_snapshot_id"] = old["snapshot_id"]
        second = self.accept(new, new_args)
        self.assertFalse(first["reused"])
        self.assertFalse(second["reused"])
        return old["snapshot_id"], new["snapshot_id"]

    def history(self):
        with closing(sqlite3.connect(self.history_db.as_uri() + "?mode=ro", uri=True)) as con, con:
            accepted = [tuple(bytes(v) if isinstance(v, (bytes, memoryview)) else v for v in row)
                        for row in con.execute("SELECT * FROM snapshot_acceptances ORDER BY snapshot_id")]
            events = list(con.execute("SELECT sequence, previous_snapshot_id, snapshot_id "
                                      "FROM snapshot_events ORDER BY sequence"))
            pointer = list(con.execute("SELECT singleton, snapshot_id FROM accepted_pointer"))
        con.close()
        return {"acceptances": accepted, "events": events, "pointer": pointer}

    def triggers(self):
        with closing(sqlite3.connect(self.history_db.as_uri() + "?mode=ro", uri=True)) as con, con:
            rows = dict(con.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='trigger'"))
        con.close()
        return rows

    def attempt(self, statement, params, recursive):
        """One attacker-style connection: default foreign_keys, commit on success."""
        con = sqlite3.connect(self.history_db)
        try:
            con.execute("PRAGMA recursive_triggers=" + ("ON" if recursive else "OFF"))
            self.assertEqual(con.execute("PRAGMA recursive_triggers").fetchone()[0], int(recursive))
            try:
                con.execute(statement, params)
            except sqlite3.Error as error:
                con.rollback()
                return error
            con.commit()
            return None
        finally:
            con.close()

    def replacement_attempts(self, old_id, new_id, recursive):
        acceptance_row = (old_id, "replaced", H1, H1, H1, b"{}")
        attempts = {}
        for verb in ("INSERT OR REPLACE", "REPLACE"):
            attempts[verb + " snapshot_acceptances"] = self.attempt(
                verb + " INTO snapshot_acceptances VALUES(?,?,?,?,?,?)", acceptance_row, recursive)
            attempts[verb + " snapshot_events"] = self.attempt(
                verb + " INTO snapshot_events(sequence,previous_snapshot_id,snapshot_id) VALUES(1,NULL,?)",
                (new_id,), recursive)
        return attempts

    def check_replacement_refused(self, recursive):
        old_id, new_id = self.two_snapshots()
        before, before_bytes = self.history(), self.history_db.read_bytes()
        accepted_before = acceptance.read_accepted(self.output)
        attempts = self.replacement_attempts(old_id, new_id, recursive)
        after = self.history()
        # Preservation is the deciding assertion (A1/A4): history bytes and chain unchanged.
        self.assertTrue(after == before and self.history_db.read_bytes() == before_bytes,
                        PRESERVATION_MARKER + " recursive_triggers=" + str(recursive))
        self.assertEqual([e[1:] for e in after["events"]], [(None, old_id), (old_id, new_id)])
        self.assertEqual(acceptance.read_accepted(self.output), accepted_before)
        for label, error in attempts.items():
            with self.subTest(attempt=label):
                self.assertIsInstance(error, sqlite3.IntegrityError)
                expected = ACCEPTANCE_REFUSAL if "acceptances" in label else HISTORY_REFUSAL
                self.assertEqual(str(error), expected)

    # -- A1 -------------------------------------------------------------------
    def test_a1_replace_refused_recursive_triggers_off(self):
        self.check_replacement_refused(False)

    def test_a1_replace_refused_recursive_triggers_on(self):
        self.check_replacement_refused(True)

    def test_a1_legacy_database_gains_guards_on_next_acceptance(self):
        legacy = without_added_guards(acceptance.SQL)
        con = sqlite3.connect(self.history_db)
        con.executescript(legacy)
        con.close()
        self.history_db.chmod(0o600)
        self.assertFalse(set(ADDED_TRIGGERS) & set(self.triggers()))
        old_id, new_id = self.two_snapshots()
        installed = self.triggers()
        self.assertEqual({name: installed.get(name) for name in ADDED_TRIGGERS}, ADDED_TABLES)
        self.assertEqual(set(BASELINE_TRIGGERS) | set(ADDED_TRIGGERS), set(installed))
        before = self.history()
        attempts = self.replacement_attempts(old_id, new_id, False)
        self.assertEqual(self.history(), before, PRESERVATION_MARKER)
        self.assertTrue(all(isinstance(e, sqlite3.IntegrityError) for e in attempts.values()))

    # -- A2 -------------------------------------------------------------------
    def test_a2_first_cas_and_exact_replay_one_event_each(self):
        old, old_args = self.prepare()
        self.assertFalse(self.accept(old, old_args)["reused"])
        self.assertTrue(self.accept(old, old_args)["reused"])
        self.join("fa1404-cas")
        new, new_args = self.prepare()
        with self.assertRaisesRegex(catalog.CatalogError, "accepted_pointer_changed"):
            self.accept(new, new_args)
        new_args["expected_current_snapshot_id"] = old["snapshot_id"]
        first = self.accept(new, new_args)
        replay = self.accept(new, new_args)
        self.assertFalse(first["reused"])
        self.assertTrue(replay["reused"])
        self.assertEqual(first["receipt"], replay["receipt"])
        old_args["expected_current_snapshot_id"] = new["snapshot_id"]
        with self.assertRaisesRegex(catalog.CatalogError, "superseded_snapshot_cannot_reactivate"):
            self.accept(old, old_args)
        state = self.history()
        self.assertEqual([e[1:] for e in state["events"]],
                         [(None, old["snapshot_id"]), (old["snapshot_id"], new["snapshot_id"])])
        self.assertEqual(len(state["acceptances"]), 2)
        self.assertEqual(state["pointer"], [(1, new["snapshot_id"])])

    def test_a2_response_loss_and_directory_sync_recovery(self):
        old, old_args = self.prepare()
        with mock.patch.object(acceptance, "_after_commit", side_effect=RuntimeError("synthetic response loss")):
            with self.assertRaises(RuntimeError):
                self.accept(old, old_args)
        self.assertTrue(self.accept(old, old_args)["reused"])
        self.join("fa1404-sync")
        new, new_args = self.prepare()
        new_args["expected_current_snapshot_id"] = old["snapshot_id"]
        with mock.patch.object(acceptance, "_sync_root", side_effect=OSError("synthetic directory sync")):
            with self.assertRaises(OSError):
                self.accept(new, new_args)
        with mock.patch.object(acceptance, "_sync_root", wraps=acceptance._sync_root) as sync:
            self.assertTrue(self.accept(new, new_args)["reused"])
            sync.assert_called_once_with(self.output)
        self.assertEqual([e[1:] for e in self.history()["events"]],
                         [(None, old["snapshot_id"]), (old["snapshot_id"], new["snapshot_id"])])

    def test_a2_noncolliding_inserts_and_pointer_cas_continue(self):
        old_id, new_id = self.two_snapshots()
        for recursive in (False, True):
            fresh = FRESH_ID[:-1] + ("a" if recursive else "b")
            with self.subTest(recursive_triggers=recursive):
                self.assertIsNone(self.attempt("INSERT INTO snapshot_acceptances VALUES(?,?,?,?,?,?)",
                                               (fresh, "synthetic", H1, H1, H1, b"{}"), recursive))
                self.assertIsNone(self.attempt(
                    "INSERT INTO snapshot_events(previous_snapshot_id,snapshot_id) VALUES(?,?)",
                    (new_id, fresh), recursive))
                self.assertIsNone(self.attempt(
                    "INSERT OR REPLACE INTO accepted_pointer VALUES(1,?)", (fresh,), recursive))
                self.assertIsNone(self.attempt(
                    "INSERT INTO accepted_pointer VALUES(1,?) ON CONFLICT(singleton) DO UPDATE "
                    "SET snapshot_id=excluded.snapshot_id", (new_id,), recursive))
        state = self.history()
        self.assertEqual([e[0] for e in state["events"]], [1, 2, 3, 4])
        self.assertEqual(state["events"][:2], [(1, None, old_id), (2, old_id, new_id)])
        self.assertEqual(len(state["acceptances"]), 4)
        self.assertEqual(state["pointer"], [(1, new_id)])
        self.assertIsNone(self.attempt(
            "INSERT INTO snapshot_events(sequence,previous_snapshot_id,snapshot_id) VALUES(9,?,?)",
            (old_id, new_id), False))

    # -- R1-MF1: the reserved sequence -1 slot --------------------------------
    def seed_reserved_row(self, variant_names):
        """History created under SQL lacking `variant_names`, holding an explicit -1 event."""
        sql = without_added_guards(acceptance.SQL, variant_names) if variant_names else acceptance.SQL
        with mock.patch.object(acceptance, "SQL", sql):
            old_id, new_id = self.two_snapshots()
            seeded = self.attempt("INSERT INTO snapshot_events(sequence,previous_snapshot_id,snapshot_id) "
                                  "VALUES(?,NULL,?)", (RESERVED_SEQUENCE, old_id), False)
        self.assertIsNone(seeded)
        self.assertEqual(self.history()["events"][0], (RESERVED_SEQUENCE, None, old_id))
        return old_id, new_id

    def check_reserved_row(self, recursive):
        present = tuple(n for n in ADDED_TRIGGERS
                        if re.search(r"CREATE TRIGGER IF NOT EXISTS " + n + r"\b", acceptance.SQL))
        old_id, new_id = self.seed_reserved_row(present)
        acceptance._connect(self.output).close()  # real module install path adds the guards
        self.assertEqual(set(self.triggers()), set(BASELINE_TRIGGERS) | set(present))
        before, before_bytes = self.history(), self.history_db.read_bytes()
        attempts = {verb: self.attempt(verb + " INTO snapshot_events(sequence,previous_snapshot_id,snapshot_id) "
                                       "VALUES(?,?,?)", (RESERVED_SEQUENCE, old_id, new_id), recursive)
                    for verb in ("INSERT OR REPLACE", "REPLACE", "INSERT")}
        # Deciding assertion: the colliding explicit -1 replacement leaves history intact.
        self.assertTrue(self.history() == before and self.history_db.read_bytes() == before_bytes,
                        PRESERVATION_MARKER + " reserved sequence recursive_triggers=" + str(recursive))
        replace_refusal = BASELINE_HISTORY_REFUSAL if recursive else RESERVED_REFUSAL
        for verb, error in attempts.items():
            with self.subTest(attempt=verb):
                self.assertIsInstance(error, sqlite3.IntegrityError)
                if verb != "INSERT":
                    self.assertEqual(str(error), replace_refusal)
        # Positive: a noncolliding automatic insert still gets the next sequence.
        self.assertIsNone(self.attempt("INSERT INTO snapshot_events(previous_snapshot_id,snapshot_id) "
                                       "VALUES(?,?)", (new_id, old_id), recursive))
        self.assertEqual(self.history()["events"], before["events"] + [(3, new_id, old_id)])
        self.assertEqual(self.history()["acceptances"], before["acceptances"])

    def test_a2_reserved_row_auto_insert_continues_recursive_triggers_off(self):
        self.check_reserved_row(False)

    def test_a2_reserved_row_auto_insert_continues_recursive_triggers_on(self):
        self.check_reserved_row(True)

    def test_a1_explicit_reserved_sequence_refused(self):
        old_id, new_id = self.two_snapshots()
        before, before_bytes = self.history(), self.history_db.read_bytes()
        for recursive in (False, True):
            for verb in ("INSERT", "INSERT OR REPLACE"):
                with self.subTest(recursive_triggers=recursive, verb=verb):
                    error = self.attempt(verb + " INTO snapshot_events(sequence,previous_snapshot_id,snapshot_id) "
                                         "VALUES(?,?,?)", (RESERVED_SEQUENCE, old_id, new_id), recursive)
                    self.assertIsInstance(error, sqlite3.IntegrityError)
                    self.assertEqual(str(error), RESERVED_REFUSAL)
        self.assertEqual(self.history(), before)
        self.assertEqual(self.history_db.read_bytes(), before_bytes)

    # -- A3 -------------------------------------------------------------------
    def test_a3_no_new_publication_or_semantic_authority(self):
        old, old_args = self.prepare()
        receipts = [self.accept(old, old_args)]
        self.join("fa1404-authority")
        new, new_args = self.prepare()
        new_args["expected_current_snapshot_id"] = old["snapshot_id"]
        receipts.append(self.accept(new, new_args))
        receipts.append(acceptance.read_accepted(self.output))
        for item in receipts:
            receipt = item["receipt"]
            self.assertEqual(set(receipt), RECEIPT_KEYS)
            self.assertIs(receipt["stage_promotions"], 0)
            self.assertIs(receipt["document_review_approval"], False)
            self.assertIs(receipt["publication_ready"], False)
            self.assertIs(receipt["catalog_snapshot_accepted"], True)
        with closing(sqlite3.connect(self.db)) as con, con:
            self.assertEqual(con.execute("SELECT count(*) FROM stage_state WHERE status='pending'").fetchone()[0], 14)
        con.close()

    # -- A4 support: the gate-control variant is exact and reproduces the gap --
    def test_a4_control_variant_removes_only_added_guards(self):
        variant = without_added_guards(acceptance.SQL)
        self.assertEqual(len(acceptance.SQL) - len(variant),
                         sum(len(m.group(0)) for m in re.finditer(
                             r"CREATE TRIGGER IF NOT EXISTS (?:" + "|".join(ADDED_TRIGGERS) + r")\b.*?\bEND;\n",
                             acceptance.SQL, re.S)))
        for name in ADDED_TRIGGERS:
            self.assertNotIn(name, variant)
        for name in BASELINE_TRIGGERS:
            self.assertIn(name, variant)
        with mock.patch.object(acceptance, "SQL", variant):
            old_id, new_id = self.two_snapshots()
            self.assertFalse(set(ADDED_TRIGGERS) & set(self.triggers()))
            replaced = self.attempt("INSERT OR REPLACE INTO snapshot_acceptances VALUES(?,?,?,?,?,?)",
                                    (old_id, "replaced", H1, H1, H1, b"{}"), False)
            moved = self.attempt("INSERT OR REPLACE INTO snapshot_events(sequence,previous_snapshot_id,"
                                 "snapshot_id) VALUES(1,NULL,?)", (new_id,), False)
        self.assertIsNone(replaced)
        self.assertIsNone(moved)
        state = self.history()
        self.assertIn((1, None, new_id), state["events"])
        self.assertIn("replaced", [row[1] for row in state["acceptances"] if row[0] == old_id])

    def test_a4_reserved_guard_is_load_bearing(self):
        """Without only the reserved guard, the sequence<>-1 exemption reopens -1 replacement."""
        variant = without_added_guards(acceptance.SQL, (RESERVED_TRIGGER,))
        self.assertIn("immutable_events_insert_collision", variant)
        self.assertNotIn(RESERVED_TRIGGER, variant)
        old_id, new_id = self.seed_reserved_row((RESERVED_TRIGGER,))
        self.assertFalse({RESERVED_TRIGGER} & set(self.triggers()))
        replaced = self.attempt("INSERT OR REPLACE INTO snapshot_events(sequence,previous_snapshot_id,"
                                "snapshot_id) VALUES(?,?,?)", (RESERVED_SEQUENCE, old_id, new_id), False)
        self.assertIsNone(replaced)
        self.assertIn((RESERVED_SEQUENCE, old_id, new_id), self.history()["events"])


if __name__ == "__main__":
    unittest.main()
