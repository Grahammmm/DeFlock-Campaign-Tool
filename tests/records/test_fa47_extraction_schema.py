"""FA47: ensure_schema verifies the adapter's own schema objects, not only its marker.

Every store here is newly generated synthetic data built on the actual WP1 store
module. No canonical ledger, live record or copied production database is opened.
Corruption is applied only to these generated stores; detection is not recovery.
"""
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import extraction_ledger as enrollment
from campaign_tool.records.extraction_ledger import ExtractionBindingError, SCHEMA, sha
from campaign_tool.records.ledger import store
from tests.records import test_extraction_ledger as enrollment_tests

MISMATCH = "^adapter_schema_mismatch$"
# Independent frozen inventory of the adapter-owned objects declared by SCHEMA.
TABLES = ("extraction_adapter_schema", "extraction_adapter_imports",
          "extraction_adapter_pages", "extraction_adapter_current")
TRIGGERS = {
    "extraction_adapter_imports_update": ("extraction_adapter_imports", "UPDATE"),
    "extraction_adapter_imports_delete": ("extraction_adapter_imports", "DELETE"),
    "extraction_adapter_pages_update": ("extraction_adapter_pages", "UPDATE"),
    "extraction_adapter_pages_delete": ("extraction_adapter_pages", "DELETE"),
    "extraction_import_no_replace": ("extraction_adapter_imports", "INSERT"),
    "extraction_page_no_replace": ("extraction_adapter_pages", "INSERT"),
}
# Constraint/column alterations of one table each; the original marker is kept.
TABLE_VARIANTS = {
    "imports_unique_removed": (",\n UNIQUE(original_sha256,receipt_sha256))", ")"),
    "pages_primary_key_narrowed": ("PRIMARY KEY(import_id,page_no)", "PRIMARY KEY(import_id)"),
    "current_reference_removed": ("import_id TEXT NOT NULL REFERENCES extraction_adapter_imports(id));",
                                  "import_id TEXT NOT NULL);"),
    "schema_checksum_nullable": ("checksum TEXT NOT NULL);", "checksum TEXT);"),
}


def ensure(con):
    # Resolved at call time so an isolated control can substitute the function.
    return enrollment.ensure_schema(con)


def snapshot(path):
    """Complete read-only image of a generated store: every object and every row."""
    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as con:
        objects = sorted(tuple(r) for r in con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master"))
        rows = {}
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            rows[name] = sorted(map(repr, con.execute('SELECT * FROM "%s"' % name.replace('"', '""'))))
    return objects, rows


def reference_objects():
    with closing(sqlite3.connect(":memory:")) as con:
        con.executescript(SCHEMA)
        return {r[1]: tuple(r) for r in con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master")}


class GeneratedStore(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = self.root / "ledger" / "canonical.sqlite"
        store.initialize(self.database)

    def ensure_store(self):
        with store.ledger(self.database) as con:
            ensure(con)

    def raw(self, script):
        with closing(sqlite3.connect(self.database)) as con:
            con.executescript(script)
            con.commit()

    def adapter_objects(self):
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as con:
            return {r[1]: tuple(r) for r in con.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name LIKE 'extraction_adapter_%'")}

    def assert_refused_unchanged(self):
        before = snapshot(self.database)
        with store.ledger(self.database) as con:
            changes = con.total_changes
            with self.assertRaisesRegex(ExtractionBindingError, MISMATCH):
                ensure(con)
            self.assertEqual(con.total_changes, changes)
            self.assertFalse(con.in_transaction)
        self.assertEqual(snapshot(self.database), before)

    def assert_complete_schema(self):
        objects = self.adapter_objects()
        reference = reference_objects()
        for table in TABLES:
            self.assertEqual(objects[table][0], "table")
            self.assertEqual(objects[table], reference[table])
        triggers = {name: row for name, row in objects.items() if row[0] == "trigger"}
        self.assertEqual(set(triggers), set(TRIGGERS))
        for name, (table, event) in TRIGGERS.items():
            self.assertEqual(triggers[name][2], table)
            self.assertIn("BEFORE " + event + " ON " + table, triggers[name][3])
            self.assertIn("RAISE(ABORT", triggers[name][3])
            self.assertEqual(triggers[name], reference[name])
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as con:
            self.assertEqual([tuple(r) for r in con.execute("SELECT version,checksum FROM extraction_adapter_schema")],
                             [(1, sha(SCHEMA.encode()))])


class EnrollmentStore(unittest.TestCase):
    """Uses the existing synthetic enrollment fixture with the actual WP1 store."""

    def setUp(self):
        enrollment_tests.ExtractionLedgerTests.setUpClass()
        self.fixture = enrollment_tests.ExtractionLedgerTests("test_no_blanket_stage_validator")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.database = self.fixture.database

    def raw(self, script):
        with closing(sqlite3.connect(self.database)) as con:
            con.executescript(script)
            con.commit()


class A1FreshAndReuse(GeneratedStore):
    def test_fresh_store_creates_marker_and_all_required_objects(self):
        self.ensure_store()
        self.assert_complete_schema()

    def test_reopen_and_repeated_ensure_change_nothing(self):
        self.ensure_store()
        first = snapshot(self.database)
        for _ in range(3):
            with store.ledger(self.database) as con:
                changes = con.total_changes
                ensure(con)
                ensure(con)
                self.assertEqual(con.total_changes, changes)
                self.assertFalse(con.in_transaction)
        self.assertEqual(snapshot(self.database), first)
        self.assert_complete_schema()

    def test_unrelated_wp1_objects_are_ignored_and_preserved(self):
        self.ensure_store()
        self.raw("CREATE TABLE fa47_unrelated(x TEXT); INSERT INTO fa47_unrelated VALUES('kept');"
                 "CREATE INDEX fa47_unrelated_runs ON runs(status);"
                 "CREATE VIEW fa47_unrelated_view AS SELECT x FROM fa47_unrelated;"
                 "CREATE TRIGGER fa47_unrelated_trigger AFTER INSERT ON fa47_unrelated BEGIN SELECT 1; END;")
        before = snapshot(self.database)
        self.ensure_store()
        self.assertEqual(snapshot(self.database), before)


class A1ExistingRows(EnrollmentStore):
    def test_valid_existing_adapter_rows_preserved_exactly(self):
        self.fixture.enroll(self.fixture.bundle(pdf=True))
        self.raw("CREATE TABLE fa47_unrelated(x TEXT); INSERT INTO fa47_unrelated VALUES('kept');")
        before = snapshot(self.database)
        self.assertEqual(self.fixture.count("extraction_adapter_imports"), 1)
        self.assertEqual(self.fixture.count("extraction_adapter_pages"), 2)
        with store.ledger(self.database) as con:
            changes = con.total_changes
            ensure(con)
            self.assertEqual(con.total_changes, changes)
        self.assertEqual(snapshot(self.database), before)


class A2MarkerMatchesButObjectsDiffer(GeneratedStore):
    def test_missing_required_trigger_refused(self):
        for name in TRIGGERS:
            with self.subTest(trigger=name):
                self.setUp()
                self.ensure_store()
                self.raw("DROP TRIGGER %s;" % name)
                self.assertNotIn(name, self.adapter_objects())
                self.assert_refused_unchanged()

    def test_permissive_same_named_trigger_refused(self):
        for name, (table, event) in TRIGGERS.items():
            with self.subTest(trigger=name):
                self.setUp()
                self.ensure_store()
                self.raw("DROP TRIGGER %s; CREATE TRIGGER %s BEFORE %s ON %s BEGIN SELECT 1; END;"
                         % (name, name, event, table))
                self.assertEqual(self.adapter_objects()[name][2], table)
                self.assert_refused_unchanged()

    def test_altered_table_constraint_with_original_marker_refused(self):
        for label, (old, new) in TABLE_VARIANTS.items():
            with self.subTest(variant=label):
                self.setUp()
                self.assertEqual(SCHEMA.count(old), 1)
                variant = SCHEMA.replace(old, new)
                self.raw("BEGIN;\n" + variant + "INSERT INTO extraction_adapter_schema VALUES(1,'%s');\nCOMMIT;"
                         % sha(SCHEMA.encode()))
                self.assert_refused_unchanged()

    def test_added_column_and_missing_table_refused(self):
        for script in ("ALTER TABLE extraction_adapter_pages ADD COLUMN note TEXT;",
                       "DROP TABLE extraction_adapter_current;"):
            with self.subTest(script=script):
                self.setUp()
                self.ensure_store()
                self.raw(script)
                self.assert_refused_unchanged()

    def test_existing_marker_row_failures_stay_refused(self):
        digest = sha(SCHEMA.encode())
        for script in ("DELETE FROM extraction_adapter_schema;",
                       "INSERT INTO extraction_adapter_schema VALUES(2,'%s');" % digest,
                       "UPDATE extraction_adapter_schema SET checksum='wrong';",
                       "UPDATE extraction_adapter_schema SET version=2;"):
            with self.subTest(script=script):
                self.setUp()
                self.ensure_store()
                self.raw(script)
                self.assert_refused_unchanged()


class A3BoundaryAndNoRepair(GeneratedStore):
    def caller_work_survives(self, corrupted):
        self.ensure_store()
        if corrupted:
            self.raw("DROP TRIGGER extraction_adapter_pages_delete;")
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO ledger_meta VALUES('fa47_caller_work','pending')")
            self.assertTrue(con.in_transaction)
            if corrupted:
                with self.assertRaisesRegex(ExtractionBindingError, MISMATCH):
                    ensure(con)
            else:
                ensure(con)
            self.assertTrue(con.in_transaction)
            self.assertEqual(con.execute("SELECT value FROM ledger_meta WHERE key='fa47_caller_work'").fetchone()[0],
                             "pending")
            with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as other:
                self.assertIsNone(other.execute("SELECT 1 FROM ledger_meta WHERE key='fa47_caller_work'").fetchone())
            con.rollback()
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as other:
            self.assertIsNone(other.execute("SELECT 1 FROM ledger_meta WHERE key='fa47_caller_work'").fetchone())

    def test_intact_check_neither_commits_nor_discards_caller_work(self):
        self.caller_work_survives(False)

    def test_refused_check_neither_commits_nor_discards_caller_work(self):
        self.caller_work_survives(True)

    def test_detection_is_deterministic_on_reopen_and_never_repairs(self):
        self.ensure_store()
        self.raw("DROP TRIGGER extraction_page_no_replace;")
        for _ in range(3):
            self.assert_refused_unchanged()
            self.assertNotIn("extraction_page_no_replace", self.adapter_objects())


class A3ImmutableHistoryStillEnforced(EnrollmentStore):
    def test_update_delete_and_replace_still_fail_on_intact_store(self):
        self.fixture.enroll(self.fixture.bundle(pdf=True))
        with store.ledger(self.database) as con:
            ensure(con)
        attempts = ("UPDATE extraction_adapter_imports SET created_at='changed'",
                    "DELETE FROM extraction_adapter_imports",
                    "UPDATE extraction_adapter_pages SET page_json='{}'",
                    "DELETE FROM extraction_adapter_pages",
                    "INSERT OR REPLACE INTO extraction_adapter_pages SELECT * FROM extraction_adapter_pages LIMIT 1",
                    "INSERT INTO extraction_adapter_imports SELECT * FROM extraction_adapter_imports LIMIT 1")
        before = snapshot(self.database)
        for statement in attempts:
            with self.subTest(statement=statement):
                with closing(sqlite3.connect(self.database)) as con:
                    with self.assertRaises(sqlite3.IntegrityError):
                        con.execute(statement)
                    con.rollback()
        self.assertEqual(snapshot(self.database), before)


class A5EnrollmentBoundary(EnrollmentStore):
    def test_enrollment_rejects_corrupted_adapter_before_writes(self):
        first = self.fixture.enroll(self.fixture.bundle(pdf=True))
        self.raw("DROP TRIGGER extraction_page_no_replace;")
        before = snapshot(self.database)
        counts = {t: self.fixture.count(t) for t in ("runs", "units", "page_state", "extraction_adapter_imports",
                                                     "extraction_adapter_pages", "extraction_adapter_current")}
        with self.assertRaisesRegex(ExtractionBindingError, MISMATCH):
            self.fixture.enroll(self.fixture.bundle(pdf=True, text="second", parser_version="2"))
        self.assertEqual({t: self.fixture.count(t) for t in counts}, counts)
        self.assertEqual(snapshot(self.database), before)
        self.assertEqual(counts["extraction_adapter_imports"], 1)
        self.assertTrue(first["import_id"])

    def test_enrollment_positive_reuse_on_intact_adapter(self):
        receipt = self.fixture.bundle(pdf=True)
        first = self.fixture.enroll(receipt)
        replay = self.fixture.enroll(receipt)
        self.assertFalse(first["reused"])
        self.assertTrue(replay["reused"])
        self.assertEqual(first["import_id"], replay["import_id"])
        second = self.fixture.enroll(self.fixture.bundle(pdf=True, text="second", parser_version="2"))
        self.assertNotEqual(second["import_id"], first["import_id"])
        self.assertEqual(self.fixture.count("extraction_adapter_imports"), 2)


if __name__ == "__main__":
    unittest.main()
