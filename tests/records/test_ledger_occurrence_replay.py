"""Synthetic canonical-binding replay regressions; no real records."""
import json
import sqlite3
import unittest
from campaign_tool.records.ledger.occurrence_projection import project_occurrences
from tests.records import test_ledger_occurrences as occurrence_fixtures
from contextlib import closing

class OccurrenceReplayBindingTests(unittest.TestCase):
    def fixture(self):
        helper = occurrence_fixtures.OccurrenceProjectionTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        other_sha = helper.add_doc("fictional second original")
        import_id = helper.capture()
        project_occurrences(helper.database, import_id)
        return helper, import_id, other_sha

    def test_changed_canonical_fields_fail_closed(self):
        for field, value in (("source_ref", "{}"), ("kind", "mail"),
                             ("parent_occurrence_id", "self"),
                             ("original_sha256", "other"), ("evidence", "{}")):
            with self.subTest(field=field):
                helper, import_id, other_sha = self.fixture()
                with closing(sqlite3.connect(helper.database)) as con, con:
                    oid = con.execute("SELECT id FROM occurrences").fetchone()[0]
                    if value == "self": value = oid
                    if value == "other": value = other_sha
                    con.execute("UPDATE occurrences SET " + field + "=? WHERE id=?", (value, oid))
                with self.assertRaises(ValueError):
                    project_occurrences(helper.database, import_id)

    def test_replaced_canonical_row_fails_closed(self):
        helper, import_id, _ = self.fixture()
        with closing(sqlite3.connect(helper.database)) as con, con:
            con.row_factory = sqlite3.Row
            values = dict(con.execute("SELECT * FROM occurrences").fetchone())
            values["source_ref"] = json.dumps({"scheme": "fictional incorrect replacement"})
            names = list(values)
            con.execute("INSERT OR REPLACE INTO occurrences(" + ",".join(names) + ") VALUES(" +
                        ",".join("?" for _ in names) + ")", [values[n] for n in names])
        with self.assertRaises(ValueError):
            project_occurrences(helper.database, import_id)
