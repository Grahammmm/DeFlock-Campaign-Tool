"""Exact-key resolution on synthetic v001 ledgers only; no live records."""
from contextlib import closing
import copy
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from campaign_tool.records.ledger.migrations import v001
from campaign_tool.records import native_extract
from campaign_tool.records.run import Pipeline, root_lock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("resolve_alerts", ROOT / "tools/resolve_alerts.py")
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="t4-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.database = self.root / "ledger.sqlite"
        self.keys = ["extract:fixture-a:extraction_check_refused", "extract:fixture-b:extraction_error", "extract:fixture-c:extraction_error"]
        with closing(sqlite3.connect(self.database)) as con, con:
            con.executescript(v001.SQL)
            con.execute("PRAGMA user_version=1")
            con.execute("INSERT INTO schema_migrations VALUES(?,?)", (v001.VERSION, hashlib.sha256(v001.SQL.encode()).hexdigest()))
            con.execute("INSERT INTO ledger_meta VALUES('fixture','not-live')")
            rows = [(str(i), key, "first", "last", 3, "owner", "open") for i, key in enumerate(self.keys)]
            rows += [("review", "review:key", "first", "last", 2, "reviewer", "needs_review"),
                     ("closed", "closed:key", "first", "last", 4, "owner", "resolved_with_evidence")]
            con.executemany("INSERT INTO alerts VALUES(?,?,?,?,?,?,?)", rows)
        self.database.chmod(0o600)
        self.key_file = self.root / "keys.json"
        self.contract_file = self.root / "contract.json"
        self.flag = self.root / "quiescent.flag"
        self.contract = {"schema_version": 1, "target_state": "resolved", "confirmed_by": "SYNTHETIC TEST CUSTODIAN ONLY",
                         "confirmed_at": "2000-01-01 00:00 UTC (FIXTURE)", "confirmation_reference": "fixture:not-a-real-decision",
                         "retry_semantics": copy.deepcopy(TOOL.RETRY_CONTRACT)}
        self.put(self.key_file, self.keys)
        self.put(self.contract_file, self.contract)
        self.flag.write_bytes(b"T4_QUIESCENT\n")
        self.flag.chmod(0o600)
        self.before = self.query("SELECT * FROM alerts ORDER BY id")
        self.before_counts = self.counts()

    def put(self, path, data):
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def query(self, sql, values=()):
        with closing(sqlite3.connect(self.database)) as con:
            return con.execute(sql, values).fetchall()

    def counts(self):
        with closing(sqlite3.connect(self.database)) as con:
            return TOOL.table_counts(con)

    def options(self):
        return dict(root=self.root, keys_file=self.key_file, expected_count=3,
                    keys_sha256=digest(self.key_file), contract_file=self.contract_file,
                    contract_sha256=digest(self.contract_file), quiescence_flag=self.flag)

    def argv(self, **overrides):
        options = self.options()
        options.update(overrides)
        return [part for name, value in options.items() for part in ("--" + name.replace("_", "-"), str(value))]

    def call(self, **overrides):
        options = self.options()
        options.update(overrides)
        return TOOL.resolve(**options)

    def unchanged(self):
        self.assertEqual(self.query("SELECT * FROM alerts ORDER BY id"), self.before)
        self.assertEqual(self.counts(), self.before_counts)

    def refuses(self, code, **overrides):
        with self.assertRaisesRegex(TOOL.Refusal, "^" + code + "$"):
            self.call(**overrides)
        self.unchanged()

    def before_open_refuses(self, code, **overrides):
        with mock.patch.object(TOOL.sqlite3, "connect", side_effect=AssertionError("SQLite opened")) as connect:
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                self.assertEqual(TOOL.main(self.argv(**overrides)), 2)
            self.assertEqual(json.loads(output.getvalue()), {"refused": code})
            connect.assert_not_called()
        self.assertFalse(Path(str(self.database) + "-wal").exists())
        self.unchanged()

    def test_positive_preserves_metadata_counts_and_integrity(self):
        result = self.call()
        expected = [row[:-1] + ("resolved",) if row[1] in self.keys else row for row in self.before]
        self.assertEqual(self.query("SELECT * FROM alerts ORDER BY id"), expected)
        self.assertEqual(self.counts(), self.before_counts)
        self.assertEqual(self.query("PRAGMA integrity_check"), [("ok",)])
        total = sum(self.before_counts.values())
        self.assertEqual(result, dict(changed=3, alerts_before=5, alerts_after=5,
                                     ledger_rows_before=total, ledger_rows_after=total, integrity_ok=True))

    def test_missing_or_non_open_key_rolls_back_bytes(self):
        for key in ("absent", "review:key", "closed:key", self.keys[-1].upper()):
            with self.subTest(key=key):
                self.put(self.key_file, self.keys[:2] + [key])
                before = self.database.read_bytes()
                output = io.StringIO()
                with mock.patch("sys.stdout", output):
                    self.assertEqual(TOOL.main(self.argv()), 2)
                self.assertEqual(json.loads(output.getvalue()), {"refused": "exact_open_key_rowcount"})
                self.assertEqual(self.database.read_bytes(), before)
                self.unchanged()

    def test_real_engine_release_and_repeat(self):
        pipeline = object.__new__(Pipeline)
        pipeline.root = SimpleNamespace(ledger=self.database)
        self.assertIsNotNone(native_extract.held_outcome(pipeline, "fixture-a"))
        self.call()
        self.assertIsNone(native_extract.held_outcome(pipeline, "fixture-a"))
        pipeline._alert(self.keys[0], owner="ignored-new-owner")
        row = self.query("SELECT first_seen,count,owner,state FROM alerts WHERE key=?", (self.keys[0],))[0]
        self.assertEqual(row, ("first", 4, "owner", "open"))
        self.assertIsNotNone(native_extract.held_outcome(pipeline, "fixture-a"))

    def test_evidence_target_and_vocabulary_refusals(self):
        original = copy.deepcopy(self.contract)
        changes = [("target_state", state, "unconfirmed_target") for state in ("needs_review", "open", "unknown", ["resolved"])]
        changes += [("confirmed_by", " ", "custodian_confirmation_required"),
                    ("confirmed_at", None, "custodian_confirmation_required"),
                    ("confirmation_reference", "", "custodian_confirmation_required"),
                    ("retry_semantics", {}, "retry_contract_mismatch")]
        retry = copy.deepcopy(TOOL.RETRY_CONTRACT)
        retry["non_open_releases"] = 1
        changes.append(("retry_semantics", retry, "retry_contract_mismatch"))
        for field, value, reason in changes:
            with self.subTest(field=field, value=value):
                self.contract = dict(original, **{field: value})
                self.put(self.contract_file, self.contract)
                self.before_open_refuses(reason)
        self.contract = dict(original, target_state="resolved_with_evidence")
        self.put(self.contract_file, self.contract)
        self.call()
        self.assertEqual(self.query("SELECT count(*) FROM alerts WHERE state='resolved_with_evidence'"), [(4,)])

    def test_root_lock_is_shared_with_engine(self):
        before = self.database.read_bytes()
        with root_lock(self.root):
            self.before_open_refuses("root_locked")
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.call()["changed"], 3)

    def test_tool_holds_lock_through_commit(self):
        original = TOOL.require_integrity
        calls = []
        def check(con):
            original(con)
            with self.assertRaisesRegex(RuntimeError, "root_locked"):
                with root_lock(self.root):
                    self.fail("engine acquired resolution lock")
            calls.append(1)
        with mock.patch.object(TOOL, "require_integrity", side_effect=check):
            self.call()
        self.assertEqual(len(calls), 2)
        with root_lock(self.root):
            pass

    def test_missing_and_wrong_flag_before_sqlite(self):
        self.before_open_refuses("input_or_database_error", quiescence_flag=self.root / "absent")
        self.flag.write_bytes(b"not-quiescent\n")
        self.before_open_refuses("quiescence_attestation_required")

    def test_input_modes_hashes_links_and_json(self):
        for path in (self.key_file, self.contract_file, self.flag, self.database):
            with self.subTest(path=path.name):
                path.chmod(0o644)
                self.before_open_refuses("owner_0600_required")
                path.chmod(0o600)
        self.before_open_refuses("frozen_hash_mismatch", keys_sha256="0" * 64)
        self.before_open_refuses("frozen_hash_mismatch", contract_sha256="0" * 64)
        link = self.root / "link"
        link.symlink_to(self.key_file)
        self.before_open_refuses("symlink_path", keys_file=link)
        link.unlink()
        os.link(self.key_file, link)
        self.before_open_refuses("regular_single_link_required")
        link.unlink()
        directory = self.root / "linked-directory"
        directory.symlink_to(self.root, target_is_directory=True)
        self.before_open_refuses("symlink_path", keys_file=directory / "keys.json")
        for keys, reason in ((self.keys[:2], "key_count_or_duplicate"), ([self.keys[0]] * 3, "key_count_or_duplicate"),
                             (["", "b", "c"], "key_list_required"), (["a\n", "b", "c"], "key_list_required"),
                             (["a", "b", None], "key_list_required")):
            self.put(self.key_file, keys)
            self.before_open_refuses(reason)
        self.key_file.write_text('{"key":1,"key":2}')
        self.before_open_refuses("duplicate_json_field")
        self.key_file.write_text("not-json")
        self.before_open_refuses("invalid_json")
        self.before_open_refuses("positive_expected_count_required", expected_count=0)

    def test_sidecar_symlink_precedes_open(self):
        sidecar = Path(str(self.database) + "-wal")
        sidecar.symlink_to(self.root / "absent")
        try:
            with mock.patch.object(TOOL.sqlite3, "connect") as connect:
                with self.assertRaisesRegex(TOOL.Refusal, "symlink_path"):
                    self.call()
                connect.assert_not_called()
        finally:
            sidecar.unlink()
        self.unchanged()

    def test_integrity_and_trigger_failures_rollback(self):
        original = TOOL.require_integrity
        calls = []
        def fail_second(con):
            original(con)
            calls.append(1)
            if len(calls) == 2:
                raise TOOL.Refusal("integrity_check_failed")
        with mock.patch.object(TOOL, "require_integrity", side_effect=fail_second):
            self.refuses("integrity_check_failed")
        self.assertEqual(len(calls), 2)
        with closing(sqlite3.connect(self.database)) as con, con:
            con.execute("CREATE TRIGGER bad AFTER UPDATE ON alerts BEGIN UPDATE ledger_meta SET value='mutated'; END")
        self.refuses("unexpected_changes")
        self.assertEqual(self.query("SELECT value FROM ledger_meta WHERE key='fixture'"), [("not-live",)])

    def test_row_counts_and_changed_input_rollback(self):
        original = TOOL.table_counts
        calls = []
        def changed_count(con):
            result = original(con)
            calls.append(1)
            if len(calls) == 2:
                result["alerts"] += 1
            return result
        with mock.patch.object(TOOL, "table_counts", side_effect=changed_count):
            self.refuses("ledger_row_count_changed")
        original_integrity = TOOL.require_integrity
        def change_flag(con):
            original_integrity(con)
            self.flag.write_text("changed\n")
        with mock.patch.object(TOOL, "require_integrity", side_effect=change_flag):
            self.refuses("frozen_hash_mismatch")

    def test_ledger_path_replacement_rolls_back_original(self):
        original = TOOL.require_integrity
        calls = []
        moved = self.root / "moved.sqlite"
        before = self.database.read_bytes()
        def replace_path(con):
            original(con)
            calls.append(1)
            if len(calls) == 2:
                self.database.rename(moved)
                self.database.write_bytes(before)
                self.database.chmod(0o600)
        with mock.patch.object(TOOL, "require_integrity", side_effect=replace_path):
            with self.assertRaisesRegex(TOOL.Refusal, "ledger_changed"):
                self.call()
        self.assertEqual(moved.read_bytes(), before)
        self.unchanged()

    def test_bounds_schema_parent_and_lock_inputs(self):
        self.root.chmod(0o755)
        self.before_open_refuses("ledger_parent_0700_required")
        self.root.chmod(0o700)
        self.key_file.write_bytes(b" " * (TOOL.MAX_INPUT_BYTES + 1))
        self.before_open_refuses("input_changed_or_oversized")
        self.put(self.key_file, self.keys)
        self.contract_file.write_text('{"schema_version":1,"schema_version":1}')
        self.before_open_refuses("duplicate_json_field")
        self.put(self.contract_file, dict(self.contract, schema_version=True))
        self.before_open_refuses("contract_shape")
        self.put(self.contract_file, self.contract)
        lock = self.root / "run.lock"
        lock.symlink_to(self.key_file)
        self.before_open_refuses("input_or_database_error")
        lock.unlink()
        os.link(self.key_file, lock)
        self.before_open_refuses("regular_single_link_required")
        lock.unlink()
        with closing(sqlite3.connect(self.database)) as con, con:
            con.execute("ALTER TABLE alerts ADD COLUMN extra TEXT")
        with self.assertRaisesRegex(TOOL.Refusal, "alerts_schema_mismatch"):
            self.call()
        self.assertEqual(self.query("SELECT state FROM alerts WHERE state='open'"), [("open",)] * 3)

    def test_exact_not_like_or_sql_and_cli_output(self):
        special = "extract:fixture_%' OR 1=1 --:extraction_error"
        with closing(sqlite3.connect(self.database)) as con, con:
            con.execute("UPDATE alerts SET key=? WHERE key=?", (special, self.keys[-1]))
        self.put(self.key_file, self.keys[:2] + [special])
        argv = [sys.executable, "-B", str(ROOT / "tools/resolve_alerts.py"), *self.argv()]
        result = subprocess.run(argv, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads(result.stdout)), {"changed", "alerts_before", "alerts_after", "ledger_rows_before", "ledger_rows_after", "integrity_ok"})
        self.assertEqual(json.loads(result.stdout)["changed"], 3)
        self.assertEqual(result.stderr, "")
        replay = subprocess.run(argv, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(replay.returncode, 2)
        self.assertEqual(json.loads(replay.stdout), {"refused": "exact_open_key_rowcount"})
        self.assertEqual(replay.stderr, "")
        self.assertEqual(len(replay.stdout.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
