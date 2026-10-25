"""FA1301: refuse a base ledger whose required v001 objects are missing or altered.

Synthetic, file-based fixtures only. Every ledger is generated below a fresh
temporary directory; no operator record, live path or network is used.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.migrations import v001

REPO = Path(os.path.abspath(__file__)).parents[2]
HELPER = REPO / "tools" / "accept_fa1301.py"
MISMATCH = "ledger base schema mismatch"
DECIDING_MARK = "FA1301-GATE-MISSING: ledger yielded a connection and UPDATE originals succeeded"
SHA = hashlib.sha256(b"fa1301 synthetic original").hexdigest()
RECEIPT = hashlib.sha256(b"fa1301 synthetic receipt").hexdigest()


def statements(sql):
    current, out = "", []
    for character in sql:
        current += character
        if character == ";" and sqlite3.complete_statement(current):
            out.append(current.strip())
            current = ""
    if current.strip():
        raise ValueError("incomplete synthetic SQL")
    return out


def reference_objects():
    """Independent in-memory view of the unchanged v001 definitions."""
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(v001.SQL)
        return [tuple(row) for row in con.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name")]
    finally:
        con.close()


def private_dir(path):
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def seed_rows(con):
    con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                (SHA, 1, "text/plain", None, "original", "in_scope", None, "declared", None, "{}"))
    con.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (RECEIPT, "extract", SHA, "fixture", "synthetic", "pass", "{}", "[]", "synthetic",
                 "synthetic", "[]", "2000-01-01T00:00:00Z", None, "synthetic-receipt.json"))
    con.execute("INSERT INTO legacy_imports VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("import", SHA, SHA, "synthetic", SHA, "captured.sqlite", "captured", "2000-01-01T00:00:00Z",
                 None, "{}", SHA))
    con.execute("INSERT INTO legacy_rows VALUES(?,?,?,?,?,?)", ("import", "docs", 1, "{}", "{}", SHA))
    con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("run", "synthetic", "2000-01-01T00:00:00Z", None, "synthetic", None, None, None, "open", "{}"))
    con.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)",
                (SHA, "extract", "pending", None, "fixture", "2000-01-01T00:00:00Z", "run", None))


def initialized_ledger(directory):
    """Actual store.initialize output plus synthetic evidence rows."""
    path = directory / "ledger.sqlite"
    store.initialize(path)
    con = sqlite3.connect(path)
    try:
        seed_rows(con)
        con.commit()
    finally:
        con.close()
    return path


def premise_ledger(directory, omit=None):
    """Ledger built from the actual v001 SQL with at most one named statement omitted."""
    private_dir(directory)
    kept = statements(v001.SQL)
    if omit is not None:
        prefix = "CREATE TRIGGER " + omit + " "
        dropped = [item for item in kept if item.startswith(prefix)]
        if len(dropped) != 1:
            raise ValueError("premise omission is not exactly one statement")
        kept = [item for item in kept if not item.startswith(prefix)]
    path = directory / "ledger.sqlite"
    con = sqlite3.connect(path)
    try:
        con.executescript("\n".join(kept))
        con.execute("INSERT INTO schema_migrations VALUES(?,?)", (v001.VERSION, store.SCHEMA_HASH))
        con.execute("INSERT INTO ledger_meta VALUES('acceptance','candidate_unreconciled')")
        con.execute("PRAGMA user_version=1")
        con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (SHA, 1, "text/plain", None, "original", "in_scope", None, "declared", None, "{}"))
        con.commit()
    finally:
        con.close()
    path.chmod(0o600)
    return path


def tamper(path, sql):
    con = sqlite3.connect(path)
    try:
        con.executescript(sql)
        con.commit()
    finally:
        con.close()


def snapshot(path):
    """File bytes, sidecars and schema/markers/rows, read without modifying the ledger."""
    directory = sorted(os.listdir(path.parent))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        return {
            "sha256": digest, "directory": directory,
            "journal": con.execute("PRAGMA journal_mode").fetchone()[0],
            "user_version": con.execute("PRAGMA user_version").fetchone()[0],
            "schema": con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name").fetchall(),
            "migrations": con.execute("SELECT * FROM schema_migrations").fetchall(),
            "meta": con.execute("SELECT * FROM ledger_meta ORDER BY key").fetchall(),
            "originals": con.execute("SELECT * FROM originals").fetchall(),
        }
    finally:
        con.close()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fa1301-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.counter = 0

    def fresh(self):
        self.counter += 1
        return self.root / ("case-%03d" % self.counter)

    def template(self):
        """Base fixture: real initialize + rows, then rollback-journal mode for byte comparisons."""
        if not hasattr(self, "_template"):
            path = initialized_ledger(private_dir(self.root / "template"))
            con = sqlite3.connect(path)
            try:
                self.assertEqual(con.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
            finally:
                con.close()
            self._template = path
        return self._template

    def copy(self):
        directory = private_dir(self.fresh())
        path = directory / "ledger.sqlite"
        shutil.copyfile(self.template(), path)
        path.chmod(0o600)
        return path

    def assert_markers_valid(self, path):
        con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], v001.VERSION)
            self.assertEqual(con.execute("SELECT checksum FROM schema_migrations WHERE version=?",
                                         (v001.VERSION,)).fetchone()[0], store.SCHEMA_HASH)
        finally:
            con.close()

    def assert_refused(self, path, message=MISMATCH, readonly=False):
        before = snapshot(path)
        yielded = []
        with self.assertRaises(ValueError) as caught:
            with store.ledger(path, readonly=readonly) as con:
                yielded.append(con)
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(yielded, [], "ledger yielded a connection for an invalid base schema")
        self.assertEqual(snapshot(path), before, "refusal changed the ledger, its sidecars or journal mode")


class Behavior(Fixture):
    # ---- A1: ordinary ledgers and genuine supported extensions stay admitted.
    def test_initialized_ledger_opens_writable_and_readonly(self):
        path = initialized_ledger(private_dir(self.fresh()))
        with store.ledger(path) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(con.execute("PRAGMA recursive_triggers").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT bytes FROM originals").fetchone()[0], 1)
        with store.ledger(path, readonly=True) as con:
            self.assertEqual(con.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM legacy_rows").fetchone()[0], 1)
        self.assertEqual(store.counts(path)["originals"], 1)

    def test_base_guards_still_reject_updates_deletes_and_replacements(self):
        path = initialized_ledger(private_dir(self.fresh()))
        with store.ledger(path) as con:
            for table in v001.IMMUTABLE_KEYS:
                for recursive in ("ON", "OFF"):
                    with self.subTest(table=table, recursive=recursive):
                        con.execute("PRAGMA recursive_triggers=" + recursive)
                        with self.assertRaisesRegex(sqlite3.IntegrityError, "^immutable evidence$"):
                            con.execute("UPDATE " + table + " SET rowid=rowid")
                        with self.assertRaisesRegex(sqlite3.IntegrityError, "^immutable evidence$"):
                            con.execute("DELETE FROM " + table)
                        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable evidence cannot be replaced"):
                            con.execute("INSERT OR REPLACE INTO " + table + " SELECT * FROM " + table + " LIMIT 1")
            con.rollback()
            self.assertEqual(con.execute("SELECT bytes FROM originals").fetchone()[0], 1)

    def test_supported_extensions_and_additive_objects_do_not_mismatch(self):
        from campaign_tool.records.ledger import occurrence_projection, original_resolution
        from campaign_tool.records.ledger import receipt_projection, stages, unit_projection
        path = initialized_ledger(private_dir(self.fresh()))
        base = {row[1] for row in reference_objects()}
        installers = (("original_resolution", original_resolution.ensure_schema),
                      ("occurrence_projection", occurrence_projection._ensure_schema),
                      ("unit_projection", unit_projection._install),
                      ("stages", stages._install))
        for name, install in installers:
            with self.subTest(extension=name), store.ledger(path) as con:
                con.execute("BEGIN IMMEDIATE")
                install(con)
                con.commit()
        with store.ledger(path) as con:
            receipt_projection.install(con)
        tamper(path, "CREATE TABLE fa1301_additive(value TEXT);"
                     "CREATE INDEX fa1301_additive_originals ON originals(role);"
                     "CREATE TRIGGER fa1301_additive_audit AFTER INSERT ON originals BEGIN SELECT 1; END;")
        con = sqlite3.connect(path)
        try:
            extra = con.execute("SELECT name,tbl_name FROM sqlite_master").fetchall()
        finally:
            con.close()
        added = [row for row in extra if row[0] not in base and not row[0].startswith("sqlite_")]
        self.assertTrue(any(name.startswith("original_resolution") for name, _ in added))
        self.assertTrue(any(name.startswith("rp_") for name, _ in added))
        self.assertTrue(any(tbl in base and name not in base for name, tbl in added),
                        "no additive object targets a base table")
        for readonly in (False, True):
            with store.ledger(path, readonly=readonly) as con:
                self.assertEqual(con.execute("SELECT count(*) FROM originals").fetchone()[0], 1)
        with store.ledger(path) as con:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "^immutable evidence$"):
                con.execute("UPDATE originals SET bytes=2")

    # ---- A2: correct markers, missing/altered required objects refuse before yield.
    def test_each_missing_or_altered_trigger_refused_with_valid_markers(self):
        triggers = [row for row in reference_objects() if row[0] == "trigger"]
        self.assertEqual(len(triggers), 6 + 3 * len(v001.IMMUTABLE_KEYS))
        for _, name, table, sql in triggers:
            timing, event = re.search(r"\b(BEFORE|AFTER) (INSERT|UPDATE|DELETE) ON ", sql).groups()
            variants = {"missing": "DROP TRIGGER %s;" % name,
                        "altered": "DROP TRIGGER %s; CREATE TRIGGER %s %s %s ON %s BEGIN SELECT 1; END;"
                                   % (name, name, timing, event, table)}
            for variant, change in variants.items():
                for readonly in (False, True):
                    with self.subTest(trigger=name, variant=variant, readonly=readonly):
                        path = self.copy()
                        tamper(path, change)
                        self.assert_markers_valid(path)
                        self.assert_refused(path, readonly=readonly)

    def test_altered_or_missing_required_tables_and_indexes_refused(self):
        changes = {
            "originals_column": "ALTER TABLE originals ADD COLUMN fa1301_extra TEXT;",
            "receipts_column": "ALTER TABLE receipts ADD COLUMN fa1301_extra TEXT;",
            "legacy_rows_column": "ALTER TABLE legacy_rows ADD COLUMN fa1301_extra TEXT;",
            "missing_table": "DROP TABLE alerts;",
            "missing_index": "DROP INDEX units_original;",
            "altered_index": "DROP INDEX stage_status; CREATE INDEX stage_status ON stage_state(owner);",
            "table_replaced_by_view": "DROP TABLE work_leases; CREATE VIEW work_leases AS SELECT 1 AS item_key;",
        }
        for label, change in changes.items():
            for readonly in (False, True):
                with self.subTest(change=label, readonly=readonly):
                    path = self.copy()
                    tamper(path, change)
                    self.assert_markers_valid(path)
                    self.assert_refused(path, readonly=readonly)

    def test_rollback_journal_ledger_keeps_mode_when_refused_and_switches_when_valid(self):
        path = self.copy()
        tamper(path, "DROP TRIGGER immutable_receipts_update;")
        self.assertEqual(snapshot(path)["journal"], "delete")
        self.assert_refused(path)
        valid = self.copy()
        with store.ledger(valid) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    # ---- A3: repeat, reopen, no repair/leak, existing failure precedence.
    def test_repeated_refusal_never_repairs_or_leaks_connections(self):
        path = self.copy()
        tamper(path, "DROP TRIGGER immutable_originals_update;")
        initial = snapshot(path)
        real_connect = sqlite3.connect
        opened = []

        def tracking(*args, **kwargs):
            con = real_connect(*args, **kwargs)
            opened.append(con)
            return con

        with mock.patch.object(store.sqlite3, "connect", tracking):
            for readonly in (False, True, False):
                self.assert_refused(path, readonly=readonly)
        self.assertTrue(opened)
        for con in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                con.execute("SELECT 1")
        self.assertEqual(snapshot(path), initial)
        self.assertNotIn("immutable_originals_update", {row[1] for row in snapshot(path)["schema"]})
        valid = self.copy()
        for readonly in (False, True):
            with store.ledger(valid, readonly=readonly) as con:
                kept = con
                self.assertEqual(con.execute("SELECT bytes FROM originals").fetchone()[0], 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                kept.execute("SELECT 1")
        self.assert_refused(path)

    def test_version_and_checksum_failures_keep_precedence(self):
        cases = {
            "unsupported ledger schema": "PRAGMA user_version=2;",
            "ledger migration identity mismatch": "UPDATE schema_migrations SET checksum='0';",
            "ledger migration identity mismatch ": "DELETE FROM schema_migrations;",
            "unsupported ledger schema ": "PRAGMA user_version=2; UPDATE schema_migrations SET checksum='0';",
        }
        for label, change in cases.items():
            with self.subTest(change=change):
                path = self.copy()
                tamper(path, "DROP TRIGGER immutable_originals_update; " + change)
                self.assert_refused(path, message=label.strip())
        path = self.copy()
        tamper(path, "PRAGMA user_version=2;")
        self.assert_refused(path, message="unsupported ledger schema")


class GateControl(Fixture):
    """A4 deciding control: helper disables only the comparison in a disposable copy."""

    def test_missing_originals_update_guard_refuses_before_yield(self):
        path = premise_ledger(self.fresh(), omit="immutable_originals_update")
        self.assert_markers_valid(path)
        self.assertNotIn("immutable_originals_update", {row[1] for row in snapshot(path)["schema"]})
        before = snapshot(path)
        changed = None
        try:
            with store.ledger(path) as con:
                con.execute("UPDATE originals SET bytes=2")
                changed = con.execute("SELECT bytes FROM originals").fetchone()[0]
                con.rollback()
        except ValueError as exc:
            self.assertEqual(str(exc), MISMATCH)
        else:
            self.fail("%s (bytes=%s)" % (DECIDING_MARK, changed))
        self.assertEqual(snapshot(path), before)

    def test_valid_guard_control_rejects_update(self):
        path = premise_ledger(self.fresh())
        self.assert_markers_valid(path)
        with store.ledger(path) as con:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "^immutable evidence$"):
                con.execute("UPDATE originals SET bytes=2")
            con.rollback()
            self.assertEqual(con.execute("SELECT bytes FROM originals").fetchone()[0], 1)


class Isolation(Fixture):
    def launch(self, *args, env_extra=None):
        env = {"HOME": str(self.root), "TMPDIR": str(self.root), "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}
        env.update(env_extra or {})
        return subprocess.run([sys.executable, "-B", str(HELPER), *map(str, args)], cwd=str(REPO),
                              capture_output=True, text=True, timeout=300, env=env)

    def test_in_process_modules_are_the_candidate(self):
        import campaign_tool
        for module in (campaign_tool, store):
            self.assertTrue(os.path.abspath(module.__file__).startswith(str(REPO) + os.sep), module.__file__)

    def test_fresh_child_uses_candidate_and_blocks_network_process_and_outside_paths(self):
        import json
        outside = private_dir(self.root / "outside") / "target.txt"
        outside.write_text("synthetic outside file")
        run_dir = private_dir(self.root / "probe-run")
        proc = self.launch("--child", "probe", "--run-dir", run_dir,
                           env_extra={"FA1301_PROBE_OUTSIDE_FILE": str(outside)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads((run_dir / "child-result.json").read_text())
        for path in data["modules"].values():
            self.assertTrue(path.startswith(str(REPO) + os.sep), path)
        self.assertTrue(data["dont_write_bytecode"])
        self.assertTrue(data["tempdir"].startswith(str(run_dir) + os.sep))
        self.assertTrue(data["home"].startswith(str(run_dir) + os.sep))
        self.assertGreater(data["workload_tables"], 20)
        self.assertEqual(data["workload_hits"], [])
        for name, control in data["controls"].items():
            self.assertTrue(control["blocked"], (name, control))
        kinds = {hit["kind"] for hit in data["control_hits"]}
        self.assertTrue({"network", "process", "file-read", "sqlite-path"} <= kinds, kinds)
        self.assertEqual(outside.read_text(), "synthetic outside file")

    def test_linked_output_ancestry_refused_before_writes(self):
        real = private_dir(self.root / "real")
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        for target in (link, link / "nested" / "out"):
            with self.subTest(target=str(target)):
                proc = self.launch("--case", "behavior", "--output-root", target)
                self.assertEqual(proc.returncode, 3, proc.stderr)
                self.assertIn("symlink", proc.stderr)
                self.assertEqual(os.listdir(real), [])
        proc = self.launch("--child", "behavior", "--run-dir", link)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(os.listdir(real), [])

    def test_unknown_case_missing_output_and_repo_output_refused(self):
        target = self.root / "never-created"
        proc = self.launch("--case", "everything", "--output-root", target)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(target.exists())
        proc = self.launch("--case", "behavior")
        self.assertEqual(proc.returncode, 2)
        inside = REPO / "fa1301-refused-output"
        proc = self.launch("--case", "behavior", "--output-root", inside / "out")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertFalse(inside.exists())


if __name__ == "__main__":
    unittest.main()
