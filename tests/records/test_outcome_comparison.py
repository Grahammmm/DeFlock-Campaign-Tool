"""Synthetic-only comparison and actual outputs-stage acceptance."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from campaign_tool.records import run_stages
from campaign_tool.records.ledger.migrations import v001
from campaign_tool.records.outcomes import comparison as c
from contextlib import closing

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures/outcome_comparison/fact-sheets.json"


def fixture():
    return json.loads(FIXTURE.read_text())["agencies"]


def report(agencies=None, as_of="2026-04-30"):
    return c.compare(fixture() if agencies is None else agencies, run_id="synthetic-run", as_of=as_of)


def field(agency, key):
    return next(r for r in report([agency])["rows"] if r["field"] == key)


def request_row(request, as_of="2026-04-30"):
    a = fixture()[0]
    a["requests"] = [request]
    return next(r for r in report([a], as_of)["rows"] if r["field"] == "cpra_response_deadline")


class DocumentaryComparison(unittest.TestCase):
    def test_fixture_expected_statuses_and_every_required_field(self):
        rows = report()["rows"]
        harbor = {r["field"]: r for r in rows if r["agency_id"] == "synthetic-harbor-pd"}
        self.assertEqual(set(harbor), set(c.CRITERIA) | {"cpra_response_deadline"})
        self.assertTrue(all(r["status"] == "meets" for r in harbor.values()))
        mesa = {r["field"]: r for r in rows if r["agency_id"] == "synthetic-mesa-pd"}
        self.assertEqual(mesa["usage_policy_published"]["status"], "gap")
        self.assertTrue(all(r["status"] == "unknown" for k, r in mesa.items() if k != "usage_policy_published"))
        self.assertEqual(mesa["vendor_name"]["reason"], "unmapped_fact_sheet_field")
        for r in rows:
            self.assertIn(r["status"], {"meets", "gap", "unknown"})
            self.assertTrue(r["needs_attorney_review"])
            self.assertIs(r["legal_conclusion"], False)
            self.assertTrue(r["citation"]["label"])
            self.assertTrue(r["citation"]["url"].startswith("https://leginfo.legislature.ca.gov/"))
        self.assertIn("not a legal conclusion", report()["disclaimer"])

    def test_each_documentary_predicate_false_missing_and_wrong_type(self):
        for key, (_, _, predicates) in c.CRITERIA.items():
            for predicate in predicates:
                for value, expected in ((False, "gap"), (None, "unknown"), ("false", "unknown"), (0, "unknown")):
                    with self.subTest(key=key, predicate=predicate, value=value):
                        a = fixture()[0]
                        a["facts"][key]["value"][predicate] = value
                        self.assertEqual(field(a, key)["status"], expected)

    def test_missing_redacted_uncited_conflicting_and_unqualified(self):
        for key in c.CRITERIA:
            for mutation in ("missing", "redacted", "uncited", "scope", "conflict", "wrong_fact", "agency_role"):
                with self.subTest(key=key, mutation=mutation):
                    a = fixture()[0]
                    f = a["facts"][key]
                    if mutation == "missing":
                        del a["facts"][key]
                    elif mutation == "redacted":
                        f["state"] = "redacted"
                    elif mutation == "uncited":
                        f["citations"] = []
                    elif mutation == "scope":
                        f["applicability_confirmed"] = False
                    elif mutation == "conflict":
                        f["counterevidence"] = ["unresolved contrary record"]
                    elif mutation == "wrong_fact":
                        a["facts"][key] = "policy should exist"
                    else:
                        a["regulated_roles"] = []
                    self.assertEqual(field(a, key)["status"], "unknown")
        a = fixture()[0]
        a["jurisdiction"] = "us-nv"
        self.assertTrue(all(r["status"] == "unknown" for r in report([a])["rows"]))

    def test_retention_is_layer_specific_and_not_a_universal_cap(self):
        for days in (0, 30, 365, 1825, 9000):
            a = fixture()[0]
            a["facts"]["retention"]["value"]["retention_days"] = days
            self.assertEqual(field(a, "retention")["status"], "meets")
        a["facts"]["retention"]["value"].pop("data_layer")
        self.assertEqual(field(a, "retention")["status"], "unknown")
        self.assertIn("no universal numerical", field(a, "retention")["expected"])

    def test_website_condition_and_native_log_redactions(self):
        a = fixture()[0]
        f = a["facts"]["usage_policy_published"]
        f["value"].pop("conspicuous_online")
        self.assertEqual(field(a, "usage_policy_published")["status"], "unknown")
        f["value"]["has_website"] = False
        self.assertEqual(field(a, "usage_policy_published")["status"], "meets")
        a["facts"]["access_logs"]["state"] = "redacted"
        self.assertEqual(field(a, "access_logs")["status"], "unknown")
        a["regulated_roles"] = ["end_user", "public_agency"]
        self.assertEqual(field(a, "public_comment")["status"], "unknown")
        self.assertEqual(field(a, "access_logs")["status"], "unknown")
        self.assertEqual(field(a, "audit_process")["status"], "meets")

    def test_policy_practice_retention_boundaries_and_unknown_scope(self):
        for days, expected in ((364, "gap"), (365, "meets"), (366, "meets")):
            a = fixture()[0]
            a["facts"]["retention_practice"]["value"]["observed_days"] = days
            row = field(a, "retention_practice")
            self.assertEqual(row["status"], expected)
            self.assertIn("policy_should", row["reason"])
        for days, expected in ((29, "meets"), (30, "meets"), (31, "gap")):
            a = fixture()[0]
            value = a["facts"]["retention_practice"]["value"]
            value.update(min_days=None, max_days=30, observed_days=days)
            self.assertEqual(field(a, "retention_practice")["status"], expected)
        for changes in (dict(policy_data_layer="cloud captures"), dict(native_coverage_complete=False),
                        dict(exceptions_resolved=False), dict(policy_modality="unknown"),
                        dict(policy_version=None), dict(observed_days=True),
                        dict(min_days=None, max_days=None), dict(min_days=100, max_days=30)):
            a = fixture()[0]
            a["facts"]["retention_practice"]["value"].update(changes)
            self.assertEqual(field(a, "retention_practice")["status"], "unknown")

    def test_actual_sharing_and_audits_not_configuration_or_missing_production(self):
        a = fixture()[0]
        a["facts"]["sharing_practice"]["value"]["event_type"] = "configured_relationship"
        self.assertEqual(field(a, "sharing_practice")["status"], "unknown")
        for key in ("audit_practice", "sharing_practice"):
            a = fixture()[0]
            a["facts"][key]["value"]["native_coverage_complete"] = False
            self.assertEqual(field(a, key)["status"], "unknown")
            a["facts"][key]["value"]["native_coverage_complete"] = True
            self.assertEqual(field(a, key)["status"], "meets")

    def test_inputs_unchanged_determinism_and_empty(self):
        a = fixture()
        before = copy.deepcopy(a)
        self.assertEqual(report(a), report(list(reversed(a))))
        self.assertEqual(a, before)
        self.assertEqual(report([])["rows"], [])
        self.assertEqual(report([])["agency_count"], 0)

    def test_citation_gate_negative_control(self):
        a = fixture()[0]
        a["facts"]["retention"]["citations"] = []
        def acceptance():
            self.assertEqual(field(a, "retention")["status"], "unknown")
        acceptance()
        with patch.object(c, "_qualified", return_value=True):
            with self.assertRaises(AssertionError):
                acceptance()
        acceptance()


class CPRAComparison(unittest.TestCase):
    def request(self, **changes):
        request = fixture()[0]["requests"][0]
        request.update(changes)
        return request

    def test_ten_day_boundary_and_production_is_not_determination(self):
        for day, expected in (("2026-04-15", "meets"), ("2026-04-16", "meets"), ("2026-04-17", "gap")):
            row = request_row(self.request(determination_on=day))
            self.assertEqual(row["status"], expected)
            self.assertEqual(row["due_on"], "2026-04-16")
        for as_of, expected in (("2026-04-15", "unknown"), ("2026-04-16", "unknown"), ("2026-04-17", "gap")):
            row = request_row(self.request(determination_state="pending", determination_on=None,
                                          production_on="2026-04-10"), as_of)
            self.assertEqual(row["status"], expected)

    def test_weekend_holiday_and_qualified_extension(self):
        row = request_row(self.request(received_on="2026-04-09", determination_on="2026-04-21",
                                      holidays=["2026-04-20"]))
        self.assertEqual(row["due_on"], "2026-04-21")
        self.assertEqual(row["status"], "meets")
        row = request_row(self.request(extension_claimed=True, extension_days=14,
                                      extension_notice_qualified=True, extension_notice_on="2026-04-16",
                                      determination_on="2026-04-30"))
        self.assertEqual(row["base_due_on"], "2026-04-16")
        self.assertEqual(row["due_on"], "2026-04-30")
        self.assertEqual(row["status"], "meets")
        row = request_row(self.request(extension_claimed=True, extension_days=14,
                                      extension_notice_qualified=True, extension_notice_on="2026-04-16",
                                      determination_state="pending", determination_on=None), "2026-05-01")
        self.assertEqual(row["status"], "gap")

    def test_unknown_receipt_calendar_extension_and_inconsistent_dates(self):
        cases = [dict(receipt_confirmed=False), dict(calendar_confirmed=False),
                 dict(received_on="not a date"), dict(received_on="2026-05-01"),
                 dict(determination_on="2026-05-01"), dict(determination_on="2026-04-01"),
                 dict(extension_claimed=None), dict(calendar_end="2026-04-15"),
                 dict(holidays=None), dict(determination_state="acknowledged"),
                 dict(counterevidence=["conflicting receipt date"]), dict(citations=[]),
                 dict(extension_claimed=True, extension_days=15, extension_notice_qualified=True,
                      extension_notice_on="2026-04-16"),
                 dict(extension_claimed=True, extension_days=14, extension_notice_qualified=False,
                      extension_notice_on="2026-04-16"),
                 dict(extension_claimed=True, extension_days=14, extension_notice_qualified=True,
                      extension_notice_on="2026-04-17")]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(request_row(self.request(**case))["status"], "unknown")


class OutputsStage(unittest.TestCase):
    def test_original_counts_snapshot_acceptance_on_legacy_minimal_schema(self):
        # Exercise the ORIGINAL acceptance body/assertions without its default
        # external temp root or cleanup: all synthetic evidence stays in build/.
        from tests.test_records_run_stages import CountStageTests
        root = REPO / "build/outcome-comparison"
        root.mkdir(parents=True, exist_ok=True)
        project = Path(tempfile.mkdtemp(prefix="legacy-counts-", dir=root))
        case = CountStageTests("test_outputs_snapshot_counts_only")
        setattr(case, "temp", SimpleNamespace(name=str(project)))
        case.project = project
        case.mail = project / "mail-history"
        case.processing = project / "processing-v1"
        case.portal = project / "portal-queue-v1"
        for directory in (case.mail, case.processing, case.portal):
            directory.mkdir()
        case.ledger = case.processing / "ledger.sqlite"
        case.test_outputs_snapshot_counts_only()
        result = json.loads((project / "run-outputs/run-1/comparison.json").read_text())
        self.assertEqual(result["inventory_gaps"], ["agencies", "requests"])
        self.assertFalse(result["inventory_complete"])
        self.assertEqual(result["agency_count"], 0)
        self.assertIn("agencies, requests", (project / "run-outputs/run-1/comparison.md").read_text())

    def project(self, supplied=True):
        root = REPO / "build/outcome-comparison"
        root.mkdir(parents=True, exist_ok=True)
        project = Path(tempfile.mkdtemp(prefix="synthetic-", dir=root))
        processing = project / "processing-v1"
        processing.mkdir()
        with closing(sqlite3.connect(processing / "ledger.sqlite")) as db, db:
            db.executescript(v001.SQL)
            db.executemany("INSERT INTO agencies(id,name,jurisdiction) VALUES(?,?,?)", [
                ("synthetic-harbor-pd", "Invented Harbor Police", "us-ca"),
                ("synthetic-ledger-only", "Invented Ledger Agency", "us-ca")])
            db.execute("INSERT INTO requests(id,agency_id,status,submitted_at,determination_due_at,determination_at) VALUES(?,?,?,?,?,?)",
                       ("synthetic-ledger-request", "synthetic-ledger-only", "acknowledged", "2026-04-06", "2026-04-16", "2026-04-10"))
        portal = project / "portal-queue-v1"
        portal.mkdir()
        with closing(sqlite3.connect(portal / "portal.sqlite")) as db, db:
            db.execute("CREATE TABLE portal_items(id TEXT)")
        if supplied:
            (project / c.INPUT_NAME).write_bytes(FIXTURE.read_bytes())
        return project

    def invoke(self, project, run="synthetic-run"):
        args = run_stages.parser().parse_args(["outputs", "--project", str(project), "--run-id", run])
        return run_stages.stage_outputs(args)

    def fingerprint(self, project):
        return {str(p.relative_to(project)): (p.stat().st_ino, hashlib.sha256(p.read_bytes()).hexdigest())
                for subdir in ("processing-v1", "portal-queue-v1") for p in (project / subdir).iterdir()}

    def test_real_stage_writes_both_reports_without_canonical_changes(self):
        project = self.project()
        before = self.fingerprint(project)
        self.assertEqual(self.invoke(project), (run_stages.result("ok", {"input": 1, "output": 1}), 0))
        output = project / "run-outputs/synthetic-run"
        result = json.loads((output / "comparison.json").read_text())
        self.assertEqual(result["schema"], c.SCHEMA)
        self.assertEqual(result["agency_count"], 3)
        rows = [r for r in result["rows"] if r["agency_id"] == "synthetic-ledger-only"]
        self.assertEqual({r["request_id"] for r in rows if "request_id" in r}, {"synthetic-ledger-request"})
        self.assertTrue(all(r["status"] == "unknown" for r in rows))
        text = (output / "comparison.md").read_text()
        self.assertIn("REVIEW REQUIRED", text)
        self.assertIn("needs_attorney_review", text)
        self.assertIn("synthetic-policy-204", text)
        snapshot = json.loads((project / "run-outputs/synthetic-run.json").read_text())
        self.assertEqual(set(snapshot), {"snapshot_version", "run_id", "stage_state", "mail_messages", "originals", "portal_items", "portal_refusals"})
        self.assertEqual(self.fingerprint(project), before)
        frozen = {p.name: p.read_bytes() for p in output.iterdir()}
        self.assertEqual(self.invoke(project)[1], 0)
        self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, frozen)
        self.assertEqual(self.fingerprint(project), before)
        self.assertEqual(self.invoke(project, "synthetic-next-run")[1], 0)
        self.assertTrue((project / "run-outputs/synthetic-next-run/comparison.md").is_file())

    def test_absent_fact_sheets_emit_unknown_not_empty_success(self):
        project = self.project(False)
        self.assertEqual(self.invoke(project)[1], 0)
        result = json.loads((project / "run-outputs/synthetic-run/comparison.json").read_text())
        self.assertEqual(result["agency_count"], 2)
        self.assertEqual(result["input_status"], "fact_sheets_missing")
        self.assertTrue(result["rows"])
        self.assertTrue(all(r["status"] == "unknown" for r in result["rows"]))

    def test_malformed_input_and_duplicate_id_fail_without_comparison_outputs(self):
        for value in ("{", '{"schema":"wrong","agencies":[]}',
                      '{"schema":"records-agency-fact-sheets-v1","agencies":[],"agencies":[]}',
                      json.dumps({"schema": c.INPUT_SCHEMA, "agencies": fixture() + [fixture()[0]]}),
                      json.dumps({"schema": c.INPUT_SCHEMA, "agencies": [{"agency_id": "x", "facts": []}]})):
            with self.subTest(value=value[:30]):
                project = self.project()
                before = self.fingerprint(project)
                (project / c.INPUT_NAME).write_text(value)
                result, code = self.invoke(project)
                self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))
                self.assertFalse((project / "run-outputs/synthetic-run/comparison.json").exists())
                self.assertEqual(self.fingerprint(project), before)

    def test_supplied_request_cannot_override_canonical_agency(self):
        project = self.project()
        supplied = json.loads((project / c.INPUT_NAME).read_text())
        supplied["agencies"][0]["requests"][0]["request_id"] = "synthetic-ledger-request"
        (project / c.INPUT_NAME).write_text(json.dumps(supplied))
        result, code = self.invoke(project)
        self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))
        self.assertFalse((project / "run-outputs/synthetic-run/comparison.json").exists())

    def test_input_size_and_unsafe_run_name_fail_closed(self):
        project = self.project()
        with patch.object(c, "MAX_BYTES", 1):
            result, code = self.invoke(project)
        self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))
        result, code = self.invoke(project, "..")
        self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))
        self.assertFalse((project / "comparison.json").exists())

    def test_symlink_source_output_ancestor_and_leaf_never_followed(self):
        for location in ("input", "ancestor", "leaf"):
            project = self.project(False)
            victim = project / "untouched.txt"
            victim.write_text("do not read or modify")
            if location == "input":
                (project / c.INPUT_NAME).symlink_to(victim)
            else:
                output = project / "run-outputs/synthetic-run"
                output.parent.mkdir()
                if location == "ancestor":
                    output.symlink_to(project / "processing-v1", target_is_directory=True)
                else:
                    output.mkdir()
                    (output / "comparison.json").symlink_to(victim)
            with patch.object(Path, "open", autospec=True, side_effect=Path.open) as opened:
                result, code = self.invoke(project)
                self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))
                self.assertFalse(any(call.args[0] == victim for call in opened.call_args_list))
            self.assertEqual(victim.read_text(), "do not read or modify")

    def test_busy_ledger_guard_failure_and_write_failure(self):
        project = self.project()
        (project / "processing-v1/ledger.sqlite-wal").write_bytes(b"synthetic busy")
        result, code = self.invoke(project)
        self.assertEqual((code, result["error_code"]), (0, "OUTPUTS_LEDGER_BUSY"))
        self.assertFalse((project / "run-outputs").exists())
        project = self.project()
        with patch.object(c, "_load", side_effect=OSError("synthetic read failure")):
            result, code = self.invoke(project)
        self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))
        with patch.object(c.os, "replace", side_effect=OSError("synthetic write failure")):
            result, code = self.invoke(project)
        self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))

    def test_markdown_treats_source_strings_as_inert_text(self):
        result = report()
        result["input_status"] = "fact_sheets_supplied"
        result["rows"][0]["agency_id"] = "<script>alert(1)</script>|[click](https://invalid.test)\nnext"
        text = c.markdown(result)
        self.assertNotIn("<script>", text)
        self.assertNotIn("[click]", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&#124;", text)

    def test_integration_negative_control_detects_missing_hook(self):
        project = self.project()
        with patch.object(c, "write_run_comparison", return_value=None):
            self.assertEqual(self.invoke(project)[1], 0)
            with self.assertRaises(FileNotFoundError):
                (project / "run-outputs/synthetic-run/comparison.json").read_text()
        self.assertEqual(self.invoke(project)[1], 0)
        self.assertEqual(json.loads((project / "run-outputs/synthetic-run/comparison.json").read_text())["schema"], c.SCHEMA)


if __name__ == "__main__":
    unittest.main()
