"""Synthetic-only register contract and real outputs-stage integration.

No services, mailboxes, CAS bytes, network, real tracker rows or canonical writes.
Local run residue stays under .git; no retained evidence is deleted by tests.
"""
import contextlib
import copy
import csv
from datetime import date, timedelta
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import run_stages
from campaign_tool.records.ledger.migrations import v001
from campaign_tool.records.outcomes import register

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "outcome_register" / "tracker.csv"
TODAY = "2030-01-12"


def request(rid="r1", agency="a1", ref="FB-01", **values):
    return {"id": rid, "agency_id": agency, "external_ref": ref,
            "submitted_at": "2030-01-01", "determination_due_at": None,
            "determination_at": None, "production_at": None, "status": "open", **values}


def data():
    result = {table: [] for table in register.correspondence.TABLES}
    result["agencies"] = [{"id": "a1", "name": "Fable Bay Police"},
                          {"id": "a2", "name": "Juniper County Sheriff"}]
    result["requests"] = [request(status="partial"), request("r2", ref="FB-02"), request("r3", "a2", status="complete")]
    return result


def original(sha, **values):
    return {"sha256": sha, "bytes": 1, "mime_detected": "application/pdf",
            "preservation_status": "preserved", "role": "record", "scope": "in_scope", **values}


def occurrence(oid, sha, parent=None, **values):
    return {"id": oid, "original_sha256": sha, "kind": "attachment", "source_ref": "synthetic",
            "parent_occurrence_id": parent, "acquired_at": "2030-01-05T10:00:00-08:00",
            "evidence": "{}", **values}


def join(sha, **values):
    return {"id": "j1", "original_sha256": sha, "agency_id": "a1", "request_id": "r1",
            "join_type": "request", "status": "typed", "blocked_reason": None,
            "evidence": json.dumps({"source_sha256": sha, "locator": "message/request reference"}), **values}


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.gaps = register.read_tracker(FIXTURE)
        self.data = data()

    def report(self, **kwargs):
        return register.build_register(self.data, self.rows, as_of=TODAY, **kwargs)

    def test_all_requests_agency_scoped_dedup_and_tracker_only(self):
        report = self.report()
        self.assertEqual(report["request_count"], 4)
        rows = {(r["agency"], r["request_id"]): r for r in report["requests"]}
        self.assertEqual(len(rows), 4)
        first = rows[("Fable Bay Police", "FB-01")]
        self.assertEqual(first["asked"], "ALPR retention policy")
        self.assertEqual(first["missing"], "Sharing list")
        self.assertEqual(first["reported_arrived"], "One policy PDF")
        self.assertEqual(first["production_at"], "2030-01-05")
        self.assertEqual(first["next_action"], "follow up")
        complete = rows[("Juniper County Sheriff", "FB-01")]
        self.assertEqual(complete["next_action"], "nothing")
        self.assertFalse(complete["deadline"]["running"])
        self.assertEqual(complete["fulfillment"], "not_assessed")
        self.assertEqual(len(first["sources"]), 2)
        self.assertEqual(first["sources"][0], {"table": "requests", "id": "r1"})
        self.assertEqual(first["sources"][1]["row"], 2)
        self.assertEqual(self.gaps, [])

    def test_calendar_ten_plus_fourteen_and_date_boundaries(self):
        row = {"submitted_at": "2030-01-01"}
        for day, state in (("2030-01-10", "running"), ("2030-01-11", "due_today"),
                           ("2030-01-12", "past_due_reminder")):
            with self.subTest(day=day):
                issues = []
                clock = register.deadline(row, register._day(day), issues)
                self.assertEqual(clock["due"], "2030-01-11")
                self.assertEqual(clock["state"], state)
                self.assertTrue(clock["running"])
                self.assertEqual(issues, [])
        clock = register.deadline({**row, "extension": "Yes"}, register._day(TODAY), [])
        self.assertEqual((clock["due"], clock["base_due"], clock["extended_due"]),
                         ("2030-01-25", "2030-01-11", "2030-01-25"))
        self.assertEqual(clock["state"], "running")
        self.assertEqual(self.report()["requests"][1]["next_action"], "wait")

    def test_weekend_last_days_roll_to_monday_for_both_periods(self):
        # Literal dates are independent expectations: Jan 12/26 are Saturdays,
        # Jan 13/27 are Sundays; Jan 14/28 are Mondays.
        for start in ("2030-01-02", "2030-01-03"):
            for extension, due in (("No", "2030-01-14"), ("Yes", "2030-01-28")):
                for offset, state, action in ((-1, "running", "wait"),
                                              (0, "due_today", "follow up"),
                                              (1, "past_due_reminder", "follow up")):
                    with self.subTest(start=start, extension=extension, offset=offset):
                        day = date.fromisoformat(due) + timedelta(days=offset)
                        source = data()
                        source["requests"] = [request(submitted_at=start)]
                        tracker = [{"agency": "a1", "request_id": "FB-01", "asked": "Audit logs",
                                    "extension": extension, "source": {"file": "synthetic.csv", "row": 2}}]
                        row = register.build_register(source, tracker, as_of=day.isoformat())["requests"][0]
                        self.assertEqual(row["deadline"]["base_due"], "2030-01-14")
                        self.assertEqual(row["deadline"]["extended_due"], "2030-01-28")
                        self.assertEqual(row["deadline"]["due"], due)
                        self.assertEqual(row["deadline"]["state"], state)
                        self.assertEqual(row["next_action"], action)
                        self.assertEqual(row["issues"], [])

    def test_receipt_not_send_proxy_and_no_production_deadline(self):
        issues = []
        clock = register.deadline({"received_at": "2030-01-03", "submitted_at": "2030-01-01",
                                   "production_at": "2030-01-05"}, register._day(TODAY), issues)
        self.assertEqual(clock["due"], "2030-01-14")
        self.assertEqual(clock["basis"], "recorded_receipt")
        self.assertTrue(clock["running"], "production alone never proves determination")
        determined = register.deadline({"submitted_at": "2030-01-01", "determination_at": "2030-01-10"},
                                       register._day(TODAY), [])
        self.assertEqual(determined["state"], "determined")
        self.assertFalse(determined["running"])

    def test_bad_unknown_future_dates_and_extensions_clarify(self):
        for field, value, issue in (("submitted_at", "bad", "invalid_date:submitted_at"),
                                    ("submitted_at", "2030-02-01", "request_date_in_future"),
                                    ("determination_at", "2030-02-01", "determination_date_inconsistent"),
                                    ("extension", "maybe", "extension_unresolved"),
                                    ("determination_due_at", "2030-01-30", "recorded_due_differs_from_computed")):
            with self.subTest(field=field):
                rows = copy.deepcopy(self.rows)
                # Mutate authoritative ledger fields when present; otherwise the tracker field.
                source = copy.deepcopy(self.data)
                if field in source["requests"][0]:
                    source["requests"][0][field] = value
                    rows[0].pop(field, None)
                else:
                    rows[0][field] = value
                result = register.build_register(source, rows, as_of=TODAY)["requests"][0]
                self.assertIn(issue, result["issues"])
                self.assertEqual(result["next_action"], "clarify")
        self.assertIsNone(register._day("2030-01-01garbage"))
        self.assertEqual(register._day("2030-01-02T01:00:00Z").isoformat(), "2030-01-01")
        self.assertEqual(register._day("1/2/2030").isoformat(), "2030-01-02")

    def test_unknown_scope_is_not_invented_and_conflicts_visible(self):
        no_tracker = register.build_register(self.data, as_of=TODAY)
        self.assertEqual(no_tracker["request_count"], 3)
        for row in no_tracker["requests"]:
            self.assertEqual(row["next_action"], "clarify")
            self.assertIn("Unknown", row["asked"])
            self.assertIn("Unknown", row["withheld"])
            self.assertIn("Unknown", row["missing"])
        self.data["requests"][0]["submitted_at"] = "2029-12-01"
        row = self.report()["requests"][0]
        self.assertEqual(row["deadline"]["base_due"], "2029-12-11")
        self.assertIn("source_conflict:submitted_at", row["issues"])
        self.assertEqual(row["next_action"], "clarify")

    def test_mail_auto_reply_and_attachment_delivery_not_fulfillment(self):
        parent, child = "a" * 64, "b" * 64
        self.data["originals"] = [original(parent, mime_detected="message/rfc822"), original(child)]
        self.data["occurrences"] = [occurrence("mail-1", parent, kind="mail"), occurrence("attachment-1", child, "mail-1")]
        self.data["joins"] = [join(parent)]
        self.data["mail_messages"] = [{"id": "m1", "eml_sha256": parent, "received_at": "2030-01-05T10:00:00-08:00",
                                      "headers": json.dumps({"schema": "verified-eml-headers-v1", "source_original_sha256": parent,
                                                             "parser_defects": [], "fields": [["Subject", "Automatic acknowledgement — request received"]]})}]
        row = self.report()["requests"][0]
        self.assertEqual({d["occurrence_id"] for d in row["arrived"]}, {"mail-1", "attachment-1"})
        mail = next(d for d in row["arrived"] if d["kind"] == "mail")["mail"][0]
        self.assertIn("Automatic", mail["subject"])
        self.assertEqual(row["deadline"]["state"], "past_due_reminder")
        self.assertEqual(row["fulfillment"], "not_assessed")
        self.assertEqual(row["next_action"], "follow up")
        self.assertEqual(self.report()["requests"][1]["arrived"], [])

    def test_hinted_conflicting_excluded_and_unassigned_delivery_not_used(self):
        sha = "a" * 64
        self.data["originals"] = [original(sha)]
        self.data["occurrences"] = [occurrence("o1", sha)]
        for change in ({"status": "hinted"}, {"agency_id": "a2"}, {"evidence": "null"}):
            with self.subTest(change=change):
                self.data["joins"] = [join(sha, **change)]
                result = self.report()
                self.assertEqual(result["requests"][0]["arrived"], [])
                self.assertEqual(result["unassigned_occurrences"], 1)
                self.assertTrue(result["binding_gaps"])
        self.data["joins"] = [join(sha)]
        self.data["originals"][0]["scope"] = "out_of_scope"
        self.assertEqual(self.report()["requests"][0]["arrived"], [])
        self.data["originals"][0]["scope"] = "in_scope"
        self.data["joins"].append(join(sha, id="j2", request_id="r2"))
        self.assertEqual(self.report()["requests"][0]["arrived"], [])

    def test_tracker_duplicate_conflicts_missing_identity_and_empty(self):
        rows = self.rows + [{**self.rows[0], "asked": "Contradictory scope"}]
        report = register.build_register(self.data, rows, as_of=TODAY)
        self.assertEqual(report["request_count"], 4)
        self.assertIn("source_conflict:asked", report["requests"][0]["issues"])
        bad = {"agency": "", "request_id": "", "source": {"file": "fixture.csv", "row": 99}}
        report = register.build_register(data(), [bad], as_of=TODAY)
        self.assertEqual(report["request_count"], 4)
        self.assertIn("tracker_identity_unresolved", report["requests"][-1]["issues"])
        empty = {table: [] for table in register.correspondence.TABLES}
        self.assertEqual(register.build_register(empty, as_of=TODAY)["request_count"], 0)

    def test_markdown_escapes_untrusted_text_and_keeps_sources(self):
        self.rows[0]["asked"] = "<script>alert(1)</script>|fake\nrow"
        text = register.markdown(self.report())
        self.assertNotIn("<script>", text)
        self.assertIn("&#124;fake<br>row", text)
        self.assertIn("ledger requests:r1", text)
        self.assertIn("tracker.csv:row 2", text)
        self.assertIn("Unknown", text)
        self.assertIn("not a production deadline", text)

    def test_explicit_clarification_status_and_unknown_clock(self):
        self.data["requests"][1]["status"] = "needs clarification"
        self.rows[1]["status"] = "needs clarification"
        row = self.report()["requests"][1]
        self.assertEqual(row["deadline"]["state"], "running")
        self.assertEqual(row["next_action"], "clarify")
        unknown = register.deadline({}, register._day(TODAY), [])
        self.assertEqual(unknown["state"], "unknown")
        self.assertIsNone(unknown["running"])

    def test_negative_control_detects_broken_deadline_calculation(self):
        # Isolated synthetic mutation of the deciding date arithmetic, not the data.
        with mock.patch.object(register, "timedelta", return_value=timedelta(days=1)):
            wrong = register.deadline({"submitted_at": "2030-01-01"}, register._day(TODAY), [])
            with self.assertRaises(AssertionError):
                self.assertEqual(wrong["base_due"], "2030-01-11")
        good = register.deadline({"submitted_at": "2030-01-01"}, register._day(TODAY), [])
        self.assertEqual(good["base_due"], "2030-01-11")


class RegisterIntegrationTests(unittest.TestCase):
    def setUp(self):
        # A normal clone has a .git directory. CI worktrees may use their admitted TMPDIR.
        root = REPO / ".git" / "outcome-register-evidence" / "tests" if (REPO / ".git").is_dir() else Path(tempfile.gettempdir())
        root.mkdir(parents=True, exist_ok=True)
        self.project = Path(tempfile.mkdtemp(prefix="register-", dir=root))
        self.processing = self.project / "processing-v1"
        self.processing.mkdir()
        self.ledger = self.processing / "ledger.sqlite"
        with sqlite3.connect(self.ledger) as con:
            con.executescript(v001.SQL)
            con.execute("INSERT INTO agencies(id,name) VALUES('a1','Fable Bay Police')")
            con.execute("INSERT INTO requests(id,agency_id,external_ref,submitted_at,status) VALUES('r1','a1','FB-01','2030-01-01','open')")
        self.tracker = self.project / "MASTER-REQUEST-TRACKER.csv"
        self.tracker.write_bytes(FIXTURE.read_bytes())
        portal = self.project / "portal-queue-v1"
        portal.mkdir()
        with sqlite3.connect(portal / "portal.sqlite") as con:
            con.execute("CREATE TABLE portal_items(id TEXT)")

    def test_actual_outputs_stage_writes_both_files_and_preserves_counts(self):
        before = self.ledger.read_bytes()
        entries = set(self.processing.iterdir())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_stages.main(["outputs", "--project", str(self.project), "--run-id", "synthetic-run"])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["counts"], {"input": 1, "output": 1, "failed": 0, "skipped": 0})
        self.assertNotIn("Fable", out.getvalue())
        directory = self.project / "run-outputs" / "synthetic-run"
        report = json.loads((directory / "CURRENT-REQUEST-REGISTER.json").read_text())
        self.assertEqual(report["request_count"], 4)
        self.assertEqual((directory / "CURRENT-REQUEST-REGISTER.md").read_text(), register.markdown(report))
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        snapshot = json.loads((self.project / "run-outputs" / "synthetic-run.json").read_text())
        self.assertEqual(snapshot["mail_messages"], 0)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(set(self.processing.iterdir()), entries)

    def test_bad_tracker_warns_without_suppressing_existing_snapshot(self):
        for index, text in enumerate((
                "Agency,Request ID,Status,Request Status\nSecret Agency,S-01,open,open\n",
                "Agency,Request ID\nSecret Agency,S-01,extra\n",
                "Agency,Request ID\nSecret Agency\n",
                "Agency,Agency,Request ID\nSecret Agency,A,S-01\n",
                "Agency,Request ID\n\"unclosed",
                "Agency,Request ID\nSecret Agency,\xff\n")):
            with self.subTest(index=index):
                self.tracker.write_bytes(text.encode("latin-1"))
                self.assert_register_warning(f"bad-tracker-{index}")

    def assert_register_warning(self, run_id):
        before = self.ledger.read_bytes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_stages.main(["outputs", "--project", str(self.project), "--run-id", run_id])
        line = json.loads(out.getvalue())
        snapshot_path = self.project / "run-outputs" / f"{run_id}.json"
        self.assertTrue(snapshot_path.is_file(), "register errors must not suppress the counts snapshot")
        self.assertEqual(json.loads(snapshot_path.read_text()), {
            "snapshot_version": 1, "run_id": run_id, "stage_state": {},
            "mail_messages": 0, "originals": 0, "portal_items": 0, "portal_refusals": 0})
        self.assertEqual(stat.S_IMODE(snapshot_path.stat().st_mode), 0o600)
        self.assertEqual(code, 0)
        self.assertEqual(line["status"], "warn")
        self.assertEqual(line["error_code"], "OUTPUTS_REGISTER_UNAVAILABLE")
        self.assertEqual(line["counts"], {"input": 1, "output": 1, "failed": 0, "skipped": 0})
        self.assertNotIn("Secret", out.getvalue())
        self.assertNotIn(str(self.project), out.getvalue())
        self.assertFalse((self.project / "run-outputs" / run_id).exists())
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_register_read_write_and_unexpected_errors_are_isolated(self):
        for index, error in enumerate((PermissionError("Secret source path"),
                                       ValueError("Secret malformed data"),
                                       RuntimeError("Secret unexpected failure"))):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(register, "read_tracker", side_effect=error):
                    self.assert_register_warning(f"read-error-{index}")
        with mock.patch.object(register.os, "rename", side_effect=OSError("Secret output path")):
            self.assert_register_warning("write-error")

    def test_register_timeout_preserves_snapshot_but_keeps_timeout_failure(self):
        out = io.StringIO()
        with mock.patch.object(register, "write_for_run", side_effect=run_stages.StageTimeout):
            with contextlib.redirect_stdout(out):
                code = run_stages.main(["outputs", "--project", str(self.project), "--run-id", "timeout"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error_code"], "STAGE_CONTAINER_TIMEOUT")
        self.assertTrue((self.project / "run-outputs" / "timeout.json").is_file())

    def test_snapshot_write_failure_keeps_original_code_and_skips_register(self):
        out = io.StringIO()
        with mock.patch.object(run_stages.os, "replace", side_effect=OSError("Secret snapshot failure")):
            with mock.patch.object(register, "write_for_run") as write_register:
                with contextlib.redirect_stdout(out):
                    code = run_stages.main(["outputs", "--project", str(self.project), "--run-id", "snapshot-failure"])
                write_register.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error_code"], "OUTPUTS_WRITE_FAILED")
        self.assertNotIn("Secret", out.getvalue())

    def test_missing_tracker_and_count_only_legacy_schema_are_explicit(self):
        missing, gaps = register.read_tracker(self.project / "absent.csv")
        self.assertEqual((missing, gaps), ([], ["tracker_missing"]))
        other = self.project / "legacy.sqlite"
        with sqlite3.connect(other) as con:
            con.execute("CREATE TABLE mail_messages(id TEXT)")
        metadata, gaps = register.read_ledger(other)
        self.assertIn("ledger_table_missing:requests", gaps)
        self.assertIn("ledger_columns_missing:mail_messages", gaps)
        report = register.build_register(metadata, self.rows() , as_of=TODAY, gaps=gaps)
        self.assertEqual(report["request_count"], 4)

    def rows(self):
        return register.read_tracker(self.tracker)[0]

    def test_bad_csv_and_busy_database_preserve_previous_output(self):
        register.write_for_run(self.project, "good", as_of=TODAY)
        target = self.project / "run-outputs" / "good" / "CURRENT-REQUEST-REGISTER.json"
        before = target.read_bytes()
        self.assertEqual(register.write_for_run(self.project, "good", as_of=TODAY)["request_count"], 4)
        with self.assertRaisesRegex(OSError, "already_exists"):
            register.write_for_run(self.project, "good", as_of="2030-01-13")
        self.tracker.write_text("Agency,Agency,Request ID\nA,B,C\n")
        with self.assertRaises(OSError):
            register.write_for_run(self.project, "bad", as_of=TODAY)
        self.assertFalse((self.project / "run-outputs" / "bad").exists())
        self.assertEqual(target.read_bytes(), before)
        self.tracker.write_bytes(FIXTURE.read_bytes())
        wal = Path(str(self.ledger) + "-wal")
        wal.write_bytes(b"synthetic busy WAL")
        with self.assertRaises(OSError):
            register.write_for_run(self.project, "busy", as_of=TODAY)
        self.assertFalse((self.project / "run-outputs" / "busy").exists())
        self.assertEqual(wal.read_bytes(), b"synthetic busy WAL")
        self.assertEqual(target.read_bytes(), before)

    def test_csv_malformed_alias_conflicts_unknown_columns_and_missing_values(self):
        for text in ("Agency,Request ID\nA,B,extra\n", "Agency,Request ID\nA\n",
                     "Agency,Request ID,Request Number\nA,B,C\n", "", "Agency,Request ID\n\"unclosed"):
            with self.subTest(text=text):
                self.tracker.write_text(text)
                with self.assertRaises((ValueError, csv.Error)):
                    register.read_tracker(self.tracker)
        self.tracker.write_text("Unrecognized\nretained row\n")
        rows, gaps = register.read_tracker(self.tracker)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(gaps), {"tracker_column_missing:agency", "tracker_column_missing:request_id"})
        self.tracker.write_text("Agency,Request ID\n")
        self.assertEqual(register.read_tracker(self.tracker), ([], []))

    def test_source_and_output_symlink_ancestors_and_invalid_run_ids_refused(self):
        for run_id in ("../escape", "", ".hidden", "a/b"):
            with self.subTest(run_id=run_id), self.assertRaises(OSError):
                register.write_for_run(self.project, run_id, as_of=TODAY)
        alias = self.project.parent / (self.project.name + "-alias")
        alias.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(OSError):
            register.write_for_run(alias, "unsafe", as_of=TODAY)
        destination = self.project / "run-outputs"
        destination.symlink_to(self.processing, target_is_directory=True)
        with self.assertRaises(OSError):
            register.write_for_run(self.project, "unsafe", as_of=TODAY)
        self.assertFalse((self.processing / "unsafe").exists())

    def test_tracker_nonregular_symlink_and_limits(self):
        alias = self.project / "tracker-alias.csv"
        alias.symlink_to(self.tracker)
        with self.assertRaisesRegex(ValueError, "symlink"):
            register.read_tracker(alias)
        fifo = self.project / "tracker-fifo.csv"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "tracker_not_regular"):
            register.read_tracker(fifo)
        with mock.patch.object(register, "MAX_ROWS", 4):
            self.assertEqual(len(register.read_tracker(self.tracker)[0]), 4)
        with mock.patch.object(register, "MAX_ROWS", 3):
            with self.assertRaisesRegex(ValueError, "tracker_row_limit"):
                register.read_tracker(self.tracker)
        size = self.tracker.stat().st_size
        with mock.patch.object(register, "MAX_CSV_BYTES", size):
            self.assertEqual(len(register.read_tracker(self.tracker)[0]), 4)
        with mock.patch.object(register, "MAX_CSV_BYTES", size - 1):
            with self.assertRaisesRegex(ValueError, "tracker_byte_limit"):
                register.read_tracker(self.tracker)
        with mock.patch.object(register, "MAX_ROWS", 0):
            with self.assertRaisesRegex(ValueError, "ledger_row_limit:agencies"):
                register.read_ledger(self.ledger)

    def test_second_file_write_failure_never_publishes_partial_pair(self):
        real_open = os.open

        def failing_open(path, flags, *args, **kwargs):
            if flags & os.O_WRONLY and Path(path).name == "CURRENT-REQUEST-REGISTER.md":
                raise OSError("synthetic second-file failure")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(register.os, "open", side_effect=failing_open):
            with self.assertRaises(OSError):
                register.write_for_run(self.project, "second-file-failure", as_of=TODAY)
        self.assertFalse((self.project / "run-outputs" / "second-file-failure").exists())

    def test_write_failure_never_publishes_partial_pair(self):
        with mock.patch.object(register.os, "rename", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                register.write_for_run(self.project, "failure", as_of=TODAY)
        self.assertFalse((self.project / "run-outputs" / "failure").exists())
        report = register.write_for_run(self.project, "recovered", as_of=TODAY)
        self.assertEqual(report["request_count"], 4)


if __name__ == "__main__":
    unittest.main()
