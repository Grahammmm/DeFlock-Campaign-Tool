"""Synthetic request-tracker import through the public records CLI."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import cli
from campaign_tool.records.ledger import store


class RequestsImportTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.project = Path(root.name).resolve()
        self.tracker = self.project / "tracker.csv"
        self.db = self.project / "processing-v1" / "ledger.sqlite"
        self.addCleanup(os.umask, os.umask(0o077))
        store.initialize(self.db)

    def command(self, *flags):
        stdout = io.StringIO()
        argv = ["records", "requests-import", "--project", str(self.project), "--tracker", str(self.tracker), *flags]
        with patch.object(sys, "argv", argv), redirect_stdout(stdout):
            code = cli.main()
        return code, json.loads(stdout.getvalue())

    def files(self):
        return {p.relative_to(self.project): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}

    def test_preview_apply_and_replay_keep_existing_state(self):
        # Catches write-by-default, duplicate/overwriting inserts, loss of the reference used by matching,
        # and accidental writes outside the two identity tables.
        self.tracker.write_text("\ufeffagency,request_id,submitted,status\n"
                                "Fable Bay Records,FB-123,2030-01-02,waiting\n"
                                "fable bay records,FB-980,,closed\n"
                                "Juniper Agency,JA-777,2030-01-03,closed\n", encoding="utf-8")
        with store.ledger(self.db) as db, db:
            db.execute("INSERT INTO agencies VALUES ('established', 'Fable Bay Records', NULL, 'fable.example')")
            db.execute("INSERT INTO requests (id,agency_id,external_ref,status) "
                       "VALUES ('canonical', 'established', 'FB-980', 'reviewed')")
        before = self.files()
        planned = {"tracker_rows": 3, "agencies_to_add": 1, "requests_to_add": 2}
        self.assertEqual(self.command(), (0, dict(planned, mode="dry-run", agencies_written=0, requests_written=0)))
        self.assertEqual(self.files(), before)
        self.assertEqual(self.command("--apply"), (0, dict(planned, mode="apply", agencies_written=1, requests_written=2)))
        after = self.files()
        self.assertEqual(set(after), set(before))
        name = self.tracker.relative_to(self.project)
        self.assertEqual(after[name], before[name])
        with store.ledger(self.db) as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM agencies ORDER BY id")],
                             [("established", "Fable Bay Records", None, "fable.example"),
                              ("juniper-agency", "Juniper Agency", None, None)])
            self.assertEqual([tuple(row) for row in db.execute(
                "SELECT id,agency_id,external_ref,submitted_at,status FROM requests ORDER BY id")],
                [("FB-123", "established", "FB-123", "2030-01-02", "waiting"),
                 ("JA-777", "juniper-agency", "JA-777", "2030-01-03", "closed"),
                 ("canonical", "established", "FB-980", None, "reviewed")])
            self.assertEqual(db.execute("SELECT count(*) FROM joins").fetchone()[0], 0)
        self.assertEqual(self.command("--apply"), (0, dict(mode="apply", tracker_rows=3, agencies_to_add=0,
                         requests_to_add=0, agencies_written=0, requests_written=0)))
        self.assertEqual(self.files(), after)

    def test_bad_later_row_refuses_entire_file(self):
        # A streaming importer or INSERT OR IGNORE would silently apply the first row of a bad batch.
        for bad_row, reason in ((",JL-888", "missing_agency_or_request_id"),
                                ("Juniper Agency, ", "missing_agency_or_request_id"),
                                (",", "missing_agency_or_request_id"),
                                ("Juniper Agency,fb-123", "duplicate_request_id"),
                                ("Fable-Bay Records,JL-888", "agency_id_conflict")):
            with self.subTest(reason=reason, row=bad_row):
                self.tracker.write_text(f"agency,request_id\nFable Bay Records,FB-123\n{bad_row}\n", encoding="utf-8")
                before = self.files()
                for flags in ((), ("--apply",)):
                    self.assertEqual(self.command(*flags), (1, {"error": f"tracker_row_3:{reason}"}))
                    self.assertEqual(self.files(), before)

    def test_existing_request_id_for_another_agency_is_not_rebound(self):
        # A request PK is not permission to replace a reviewed agency association.
        with store.ledger(self.db) as db, db:
            db.execute("INSERT INTO agencies(id,name) VALUES ('current', 'Current Agency')")
            db.execute("INSERT INTO requests(id,agency_id,external_ref,status) VALUES ('FB-123','current','FB-123','closed')")
        self.tracker.write_text("agency,request_id\nFable Bay Records,FB-123\n", encoding="utf-8")
        before = self.files()
        self.assertEqual(self.command("--apply"), (1, {"error": "tracker_row_2:request_id_conflict"}))
        self.assertEqual(self.files(), before)
