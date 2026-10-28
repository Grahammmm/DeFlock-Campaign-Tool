"""Finite, synthetic in-memory write-prefix proofs; inert on unittest import."""
import argparse
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import importlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc10-persistence-fault-prefixes/cases.json"
SITES = ("detector_runs", "wp6_manifests", "wp6_input_originals", "detector_hits",
         "wp6_hit_identities", "wp6_run_hits")
SECOND = SITES[2:]
MODES = ("fresh", "existing")
OBSERVATIONS = []
REAL_CONNECT = sqlite3.connect


def spec():
    value = json.loads(FIXTURE.read_text())
    if (value["schema_modes"] != list(MODES) or value["insert_sites"] != list(SITES)
            or value["second_insert_sites"] != list(SECOND) or value["expected_hits"] != 2
            or value["authority"] != "test_only"):
        raise ValueError("frozen_matrix_mismatch")
    return value


class PrefixConnection(sqlite3.Connection):
    """Actual SQLite, with one synthetic execute interruption and optional bad rollback."""
    target = None
    ordinal = 1
    seen = 0
    ineffective_rollback = False

    def arm(self, table, ordinal):
        self.target, self.ordinal, self.seen = table, ordinal, 0
        self.completed, self.attempts = [], []
        self.rollback_calls = 0
        self.at_fault = None

    def execute(self, sql, parameters=()):
        words = sql.split()
        table = words[2].split("(")[0] if len(words) > 2 and words[:2] == ["INSERT", "INTO"] else None
        if self.target and table in SITES:
            self.attempts.append(table)
            if table == self.target:
                self.seen += 1
                if self.seen == self.ordinal:
                    self.at_fault = snapshot(self)
                    raise sqlite3.IntegrityError("synthetic-fc10-prefix:" + table + ":" + str(self.ordinal))
            result = super().execute(sql, parameters)
            self.completed.append(table)
            return result
        return super().execute(sql, parameters)

    def rollback(self):
        self.rollback_calls = getattr(self, "rollback_calls", 0) + 1
        if not self.ineffective_rollback:
            return super().rollback()
        return None


def snapshot(connection):
    schema = list(connection.execute("SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master ORDER BY type,name"))
    tables = [row[1] for row in schema if row[0] == "table"]
    rows = {table: sorted(list(connection.execute('SELECT * FROM "' + table + '"')), key=repr)
            for table in tables}
    return {"schema": schema, "rows": rows,
            "schema_version": connection.execute("PRAGMA schema_version").fetchone()[0],
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0]}


def snapshot_hash(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


@contextmanager
def offline_scope():
    """Scoped before product imports; no process-global hooks on discovery."""
    def unavailable(*args, **kwargs):
        raise RuntimeError("offline test boundary: forbidden side effect")

    def memory_only(database, *args, **kwargs):
        if database != ":memory:":
            raise RuntimeError("offline test boundary: SQLite must be in memory")
        return REAL_CONNECT(database, *args, **kwargs)

    with ExitStack() as stack:
        for owner, name in ((socket, "socket"), (socket, "create_connection"),
                            (socket, "getaddrinfo"), (subprocess, "Popen")):
            stack.enter_context(mock.patch.object(owner, name, unavailable))
        stack.enter_context(mock.patch.object(sqlite3, "connect", memory_only))
        yield


class PrefixProofs(unittest.TestCase):
    def setUp(self):
        self.scope = offline_scope()
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.p = importlib.import_module("campaign_tool.records.detectors.persistence")
        self.base = importlib.import_module("campaign_tool.records.ledger.migrations.v001")
        self.fixture = spec()

    def values(self, seed=False):
        units = []
        for index, label in enumerate(self.fixture["original_labels"]):
            units.append({"original_sha256": self.p.digest(label), "locator": {"row": index + 1},
                          "event_id": ("seed-" if seed else "") + self.fixture["events"][index],
                          "event_at": "2020-01-20T12:00:00+00:00", "record_type": "retention-setting",
                          "fields": {"retention_mode": "days", "retention_days": self.fixture["retention_days"][index]}})
        joins = [{"original_sha256": unit["original_sha256"], "status": "typed", "agency_id": "synthetic-agency"}
                 for unit in units]
        rules = {"version": "synthetic-pack-v1", "entries": [{"detector": self.fixture["detector"],
                 "agency_id": "synthetic-agency", "rule_id": "synthetic-rule", "version": "synthetic-v1",
                 "source_sha256": self.p.digest("synthetic policy"), "effective_from": "2020-01-01",
                 "effective_to": "2021-01-01", "parameters": {"maximum_days": self.fixture["maximum_days"],
                 "unit": "calendar_days"}}]}
        return {"units": units, "joins": joins, "rules": rules, "config": {"as_of": "2020-01-20"}}

    def prepare(self, mode):
        con = sqlite3.connect(":memory:", factory=PrefixConnection)
        self.addCleanup(con.close)
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(self.base.SQL)
        con.execute("INSERT INTO schema_migrations VALUES(?,?)", (self.base.VERSION, hashlib.sha256(self.base.SQL.encode()).hexdigest()))
        con.execute("INSERT INTO ledger_meta VALUES('acceptance','candidate_unreconciled')")
        values = self.values()
        for unit in values["units"]:
            con.execute("INSERT INTO originals VALUES(?,1,'text/plain',NULL,'original','in_scope',NULL,'captured',NULL,'{}')", (unit["original_sha256"],))
        for run_id in ("current", "previous"):
            con.execute("INSERT INTO runs VALUES(?,'detector','2020-01-20T00:00:00Z',NULL,'synthetic-engine-v1',NULL,?,NULL,'running','{}')", (run_id, self.p.digest(values["config"])))
        for unit in values["units"]:
            for stage in ("preserve", "extract", "catalog", "detect", "review", "compare", "privacy"):
                con.execute("INSERT INTO stage_state VALUES(?,?,'pending',NULL,'synthetic','2020-01-20T00:00:00Z','current',NULL)", (unit["original_sha256"], stage))
        con.commit()
        if mode == "existing":
            seed = self.p.persist_evaluation(con, run_id="previous", detector=self.fixture["detector"], **self.values(seed=True))
            self.assertEqual(seed["hit_count"], 2)
            self.assertFalse(con.in_transaction)
        else:
            self.assertFalse(any(row[0].startswith("wp6_") for row in con.execute("SELECT name FROM sqlite_master")))
        return con, values

    def persist(self, con, values):
        return self.p.persist_evaluation(con, run_id="current", detector=self.fixture["detector"], **values)

    def assert_exact_state_and_transaction_closed(self, con, before):
        after = snapshot(con)
        # Both invariants are evaluated, and one named assertion decides the control.
        self.assertTrue(after == before and not con.in_transaction,
                        "exact_state_and_transaction_closed: state_equal=" + str(after == before)
                        + "; transaction_closed=" + str(not con.in_transaction))

    def clean_retry_replay(self, con, values, before):
        con.target = None
        original_inputs = copy.deepcopy(values)
        first = self.persist(con, values)
        self.assertFalse(first["replayed"])
        self.assertEqual(first["hit_count"], 2)
        self.assertEqual(len(set(first["hit_keys"])), 2)
        self.assertFalse(con.in_transaction)
        saved = snapshot(con)
        second = self.persist(con, values)
        self.assertTrue(second["replayed"])
        self.assertEqual({k: v for k, v in first.items() if k != "replayed"},
                         {k: v for k, v in second.items() if k != "replayed"})
        self.assert_exact_state_and_transaction_closed(con, saved)
        self.assertEqual(values, original_inputs)
        for table in before["rows"]:
            if table not in SITES:
                self.assertEqual(saved["rows"][table], before["rows"][table], table)
            else:
                for row in before["rows"][table]:
                    self.assertIn(row, saved["rows"][table], table)
        # Nonempty stages, stage history, acceptance and run status are checked exactly.
        for table in ("runs", "stage_state", "stage_events", "receipts", "ledger_meta"):
            self.assertEqual(saved["rows"][table], before["rows"][table], table)
        for table, expected in (("wp6_input_originals", 2), ("wp6_run_hits", 2), ("wp6_manifests", 1),
                                ("detector_runs", 1), ("detector_hits", 2)):
            self.assertEqual(con.execute("SELECT COUNT(*) FROM " + table + " WHERE run_id='current'").fetchone()[0], expected)
        rows = list(con.execute("SELECT dedupe_key,hit_json,hit_sha256 FROM wp6_run_hits WHERE run_id='current' ORDER BY dedupe_key"))
        expected = self.p.evaluate(self.fixture["detector"], **values)
        self.assertEqual(rows, sorted((hit["dedupe_key"], self.p.canonical(hit), self.p.digest(hit)) for hit in expected["hits"]))
        self.assertEqual(con.execute("SELECT manifest_json FROM wp6_manifests WHERE run_id='current'").fetchone()[0], self.p.canonical(expected["manifest"]))
        for result in (first, second):
            self.assertEqual(result["stage_promotions"], 0)
            self.assertEqual(result["acceptance"], "supplied_evidence_triage_only")
            self.assertIs(result["publication_ready"], False)
        return {"first": first, "replay": second, "saved_state": saved}

    def fault(self, mode, table, ordinal):
        con, values = self.prepare(mode)
        before = snapshot(con)
        con.arm(table, ordinal)
        with self.assertRaisesRegex(self.p.PersistenceError, "^persistence_integrity_failure: synthetic-fc10-prefix:" + table + ":" + str(ordinal) + "$"):
            self.persist(con, values)
        self.assertEqual(con.seen, ordinal)
        self.assertEqual(con.rollback_calls, 1)
        self.assertIsNotNone(con.at_fault)
        # Every target except the first is reached after real, completed writes.
        if table != "detector_runs":
            self.assertGreater(len(con.completed), 0)
            self.assertNotEqual(con.at_fault, before)
        if ordinal == 2:
            self.assertEqual(con.completed.count(table), 1)
            if table in ("detector_hits", "wp6_hit_identities", "wp6_run_hits"):
                self.assertEqual(con.completed.count("wp6_run_hits"), 1)
        self.assert_exact_state_and_transaction_closed(con, before)
        observation = {"test": self.id(), "kind": "prefix", "mode": mode, "table": table, "ordinal": ordinal,
                       "before": before, "at_fault": con.at_fault, "after_failure": snapshot(con),
                       "completed_prefix": list(con.completed), "transaction_closed": not con.in_transaction}
        observation["retry"] = self.clean_retry_replay(con, values, before)
        golden, golden_values = self.prepare(mode)
        golden_before = snapshot(golden)
        observation["unmodified_positive"] = self.clean_retry_replay(golden, golden_values, golden_before)
        self.assertEqual(observation["retry"], observation["unmodified_positive"])
        OBSERVATIONS.append(observation)

    def baseline(self, mode):
        con, values = self.prepare(mode)
        before = snapshot(con)
        OBSERVATIONS.append({"test": self.id(), "kind": "unmodified_baseline", "mode": mode,
                             "before": before, "positive": self.clean_retry_replay(con, values, before)})

    def caller(self, mode):
        con, values = self.prepare(mode)
        con.execute("BEGIN")
        con.execute("INSERT INTO ledger_meta VALUES('caller-owned','uncommitted-synthetic')")
        before = snapshot(con)
        with self.assertRaisesRegex(self.p.PersistenceError, "^caller_transaction_must_be_closed$"):
            self.persist(con, values)
        self.assertTrue(con.in_transaction)
        self.assertEqual(snapshot(con), before)
        con.rollback()
        OBSERVATIONS.append({"test": self.id(), "kind": "caller_transaction_refused", "mode": mode,
                             "caller_state_preserved": True, "before": before})

    def test_ineffective_rollback_control_and_restored_positive(self):
        control = self.fixture["negative_control"]
        self.assertEqual(control, {"schema_mode": "fresh", "table": "detector_hits", "ordinal": 2,
                                  "mutation": "ineffective_rollback", "deciding_assertion": "exact_state_and_transaction_closed"})
        con, values = self.prepare(control["schema_mode"])
        before = snapshot(con)
        con.arm(control["table"], control["ordinal"])
        con.ineffective_rollback = True
        with self.assertRaisesRegex(self.p.PersistenceError, "^persistence_integrity_failure: synthetic-fc10-prefix:detector_hits:2$"):
            self.persist(con, values)
        # All unrelated prerequisites are independently valid before the deciding assertion.
        self.assertEqual(con.rollback_calls, 1)
        self.assertEqual(con.completed, ["detector_runs", "wp6_manifests", "wp6_input_originals",
                                        "wp6_input_originals", "detector_hits", "wp6_hit_identities", "wp6_run_hits"])
        self.assertTrue(con.in_transaction)
        self.assertNotEqual(snapshot(con), before)
        with self.assertRaisesRegex(AssertionError, "exact_state_and_transaction_closed") as killed:
            self.assert_exact_state_and_transaction_closed(con, before)
        OBSERVATIONS.append({"test": self.id(), "kind": "injected_control", "before": before,
                             "after": snapshot(con), "deciding_failure": str(killed.exception),
                             "completed_prefix": list(con.completed), "transaction_open": con.in_transaction})
        # Dispose only the broken in-memory connection. The positive uses a fresh one.
        con.close()
        self.fault("fresh", "detector_hits", 2)
        OBSERVATIONS.append({"test": self.id(), "kind": "restored_positive", "passed": True})


def install_cases():
    for mode in MODES:
        for table in SITES:
            for ordinal in ((1, 2) if table in SECOND else (1,)):
                def case(self, m=mode, t=table, n=ordinal):
                    self.fault(m, t, n)
                setattr(PrefixProofs, "test_prefix_" + mode + "_" + table + "_" + str(ordinal), case)
        for label, method in (("baseline", PrefixProofs.baseline), ("caller", PrefixProofs.caller)):
            def case(self, m=mode, f=method):
                f(self, m)
            setattr(PrefixProofs, "test_" + label + "_" + mode, case)


install_cases()


class RecordingResult(unittest.TextTestResult):
    def startTest(self, test):
        self.executed_ids.append(test.id())
        super().startTest(test)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed_ids = []


def ambient():
    import os
    return (dict(os.environ), tempfile.tempdir, sqlite3.connect, socket.socket,
            subprocess.Popen, tuple(sys.path), Path.cwd())


def script_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--mode", choices=("suite", "baseline", "discovery", "compatibility"), default="suite")
    args = parser.parse_args()
    root = args.temp_root.resolve(strict=True)
    if args.temp_root != root or not args.report.is_absolute() or root not in args.report.parents:
        raise ValueError("isolated_child_paths_required")
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    before = ambient()
    # Sentinel is unrelated to the tested module and is checked on both sides of discovery/execution.
    sentinel = root / "sentinel.txt"
    sentinel.write_text("unrelated-synthetic-sentinel\n")
    def sentinel_identity():
        st = sentinel.stat()
        return (st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns)
    sentinel_stat = sentinel_identity()
    module = "tests.records.test_fc10_persistence_fault_prefixes"
    with offline_scope():
        guarded_before = ambient()
        loader = unittest.TestLoader()
        if args.mode == "discovery":
            suite = loader.discover(str(REPO / "tests/records"), pattern="test_fc10_persistence_fault_prefixes.py", top_level_dir=str(REPO))
        elif args.mode == "compatibility":
            suite = loader.loadTestsFromName("tests.records.test_detector_persistence")
        elif args.mode == "baseline":
            suite = loader.loadTestsFromNames([module + ".PrefixProofs.test_baseline_" + mode for mode in MODES])
        else:
            suite = loader.loadTestsFromName(module)
        if ambient() != guarded_before or sentinel.read_text() != "unrelated-synthetic-sentinel\n" or sentinel_identity() != sentinel_stat:
            raise RuntimeError("discovery_contaminated_ambient_state")
        # offline_scope owns temporary patches; compare against the patched baseline here.
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    if ambient() != before or sentinel.read_text() != "unrelated-synthetic-sentinel\n" or sentinel_identity() != sentinel_stat:
        raise RuntimeError("test_contaminated_ambient_state")
    loaded = sys.modules.get(module)
    report = {"mode": args.mode, "tests_run": result.testsRun, "test_ids": result.executed_ids,
              "failures": [(test.id(), text) for test, text in result.failures],
              "errors": [(test.id(), text) for test, text in result.errors],
              "skips": [(test.id(), text) for test, text in result.skipped],
              "ambient_preserved": True, "observations": loaded.OBSERVATIONS if loaded else [],
              "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest()}
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    expected = 2 if args.mode == "baseline" else (None if args.mode == "compatibility" else 25)
    return int(not result.wasSuccessful() or bool(result.skipped) or (expected is not None and result.testsRun != expected))


if __name__ == "__main__":
    raise SystemExit(script_main())
