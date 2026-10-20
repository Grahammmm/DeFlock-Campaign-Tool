"""T12s: the T11i/T12 harness is strict (T13 must-fix 2). Synthetic fixtures only.

Three parts:

* ``EvaluateTests``: ``scripts/run_t11i_test_harness.evaluate`` on a hand-built
  receipt that looks like the restored TEST baseline (36 adopted, 115 coded
  gaps), plus adversarial receipts. Three failed runs, missing adoption counts,
  no advancement, PARSER_MISSING, a dropped document, an unexpected warning and
  a missing or undetected fault must each fail. So must a fault run that shows
  any stage result outside its explicit scenario (``FAULT_SCENARIOS``), even
  when the fault itself was detected.
* ``FaultInjectorTests``: ``scripts/t12_fault_inject.py`` refuses anything that
  is not a marked sandbox. Its portal 404 goes through the engine's own intake
  and fetch state machine, with no network, and the real portal stage adapter
  then reports it.
* ``RealAdapterHarnessTests``: a synthetic records project goes through the
  real orchestrator and the real ``run_stages`` adapters. The stage plan's
  on_fail policy comes from the committed TEST template, and nothing runs on
  the stub backend. It does three bounded runs, then the duplicate receipt,
  portal 404 and corrupt PDF faults. The resulting receipt is checked with the
  same ``evaluate`` and has to pass. The docker-down and telegram-down faults
  sit at the wrapper level (``run-records.sh``), so they are covered by
  ``tests/test_records_runtime.py`` and the live harness, not here. These
  tests need the native parsers (pypdf/openpyxl). Without them they skip with
  UNSUPPORTED_RUNTIME, and the records-run workflow fails on any skip.

No Docker, network, mailbox, TEST copy or live data is used. A passing receipt
is TEST evidence only and is review-required (``target_mode``).
"""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from campaign_tool.records import mail_adoption, native_extract, run_orchestrator, run_report
from campaign_tool.records.intake import eml_export, mail_delta
from campaign_tool.records.portal.store import Queue
from campaign_tool.records.run import Pipeline
from tests.records.test_native_extract import scanned_pdf
from tests.runner.helpers import tiny_pdf

REPO = Path(__file__).resolve().parents[2]
SHIM = REPO / "tests" / "records" / "t12_stage_shim.py"
TEMPLATE = REPO / "deploy" / "records-runtime" / "targets" / "test-container.json.in"
HAVE_PARSERS = not native_extract.missing_parsers()


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = load_script("run_t11i_test_harness")
injector = load_script("t12_fault_inject")
from tests.records import t12_stage_shim as shim  # noqa: E402

WRAPPER_FAULTS = ("duplicate_receipt", "portal_404", "corrupt_pdf")  # no wrapper in-process


# --------------------------------------------------------------------------------------------
# Hand-built receipts (TEST-baseline-shaped numbers, no record content)
# --------------------------------------------------------------------------------------------

def stage(status="ok", code=None, counts=(0, 0, 0, 0), outcomes=None):
    entry = {"status": status, "error_code": code,
             "counts": dict(zip(("input", "output", "failed", "skipped"), counts))}
    if outcomes is not None:
        entry["outcomes"] = outcomes
    return entry


def run_summary(index, *, adopted, coded=115, before=3, extracted=0, awaiting=4):
    receipts = before + 36 + coded
    already = before if index == 1 else before + 36
    extract_outcomes = {"AWAITING_OCR": awaiting, **({"EXTRACTED": extracted} if extracted else {})}
    stages = {
        "sync": stage(counts=(receipts, receipts, 0, 0)),
        "delta": stage(counts=(receipts, adopted, 0, already + coded)),
        "adoption": stage("warn", "ADOPTION_CODED_GAPS", (receipts, adopted, 0, already + coded)),
        "portal": stage(counts=(2, 2, 0, 0)),
        "extract": stage(counts=(extracted + awaiting, extracted, 0, awaiting), outcomes=extract_outcomes),
        "digest_gates": stage(counts=(10, 0, 0, 10)),
        "outputs": stage(counts=(1, 1, 0, 0)),
        "notify": stage("fail", "NOTIFY_CONNECTION_REFUSED", (1, 0, 1, 0)),
    }
    return {"run": index, "exit": 0, "report_present": True, "report_valid": True, "overall_status": "fail",
            "notify_failed": True, "stages": stages,
            "new_data": {"documents": adopted, "pages": extracted, "extracted_units": extracted,
                         "portal_releases": 0, "request_status_changes": 0},
            "review_required": 0,
            "needs_g": [{"code": "ADOPTION_CODED_GAPS", "stage": "adoption", "severity": "info", "count": coded},
                        {"code": "AWAITING_OCR", "stage": "extract", "severity": "info", "count": awaiting}],
            "ledger_after": {"mail_messages": before + 36, "originals": 160, "runs": 4 + index,
                             "quick_check": "ok", "foreign_key_violations": 0, "portal_quick_check": "ok",
                             "portal_items": 2, "portal_refusals": 0}}


def good_receipt():
    runs = [run_summary(1, adopted=36, extracted=80), run_summary(2, adopted=0), run_summary(3, adopted=0)]
    dup = copy.deepcopy(runs[2])
    dup.update(run=None, injected=True, restored=True, exit=12)
    dup["stages"]["delta"] = stage("warn", "DELTA_BLOCKING_CONFLICTS", (154, 0, 0, 154))
    dup["stages"]["adoption"] = stage("fail", "ADOPTION_BLOCKED", (154, 0, 0, 154))
    dup["needs_g"].append({"code": "ADOPTION_RECEIPT_GAPS", "stage": "adoption", "severity": "blocking",
                           "count": 1})
    portal = copy.deepcopy(runs[2])
    portal["stages"]["portal"] = stage("warn", "PORTAL_HTTP_404", (2, 2, 1, 0))
    portal["needs_g"].append({"code": "PORTAL_HTTP_404", "stage": "portal", "severity": "action", "count": 1})
    portal["injection"] = {"fault": "portal_404", "status": "injected", "http_status": 404}
    corrupt = copy.deepcopy(portal)
    corrupt["stages"]["extract"] = stage("warn", "EXTRACT_DOCUMENT_FAILURES", (6, 1, 1, 4),
                                         {"AWAITING_OCR": 4, "CORRUPT_DOCUMENT": 1, "EXTRACTED": 1})
    corrupt["needs_g"].append({"code": "EXTRACT_CORRUPT_DOCUMENT", "stage": "extract", "severity": "action",
                               "count": 1})
    corrupt["injection"] = {"fault": "corrupt_pdf", "status": "injected", "documents": 2, "messages": 1}
    corrupt["ledger_after"]["mail_messages"] += 1  # the injector ingests one message via the intake
    for fault in (dup, portal, corrupt):
        fault.pop("run")
    test_counts = {"mail_messages": 39, "originals": 160}
    return {"receipt_version": 2, "target_mode": harness.TARGET_MODE, "live_acceptance": False,
            "expect": {"adopted": 36, "coded_gaps": 115, "require_native_parsers": True, "runs": 3,
                       "allowed_warnings": [list(pair) for pair in harness.DEFAULT_ALLOWED_WARNINGS],
                       "required_faults": list(harness.REQUIRED_FAULTS)},
            "sandbox_before": {"mail_messages": 3, "originals": 4},
            "runs": runs,
            "faults": {"duplicate_receipt": dup, "portal_404": portal, "corrupt_pdf": corrupt,
                       "docker_down": {"exit": 75, "ledger_unchanged": True, "report_present": False}},
            "test_before": test_counts, "test_after": dict(test_counts),
            "live_tree_unchanged": True, "test_tree_unchanged": True}


class EvaluateTests(unittest.TestCase):
    def failures(self, receipt, **kw):
        return harness.evaluate(receipt, **kw)[1]

    def assertFailsWith(self, receipt, *names):
        failures = self.failures(receipt)
        self.assertTrue(failures, "an adversarial receipt must not pass")
        for name in names:
            self.assertIn(name, failures)

    def test_baseline_shaped_receipt_passes_every_check(self):
        checks, failures = harness.evaluate(good_receipt())
        self.assertEqual(failures, [])
        self.assertTrue(all(value is True for value in checks.values()))
        for fault in harness.REQUIRED_FAULTS:
            self.assertIn("fault_" + fault, checks)

    def test_three_failed_runs_with_the_same_exit_cannot_pass(self):
        receipt = good_receipt()
        for run in receipt["runs"]:
            run["exit"] = 14  # extract failed on every run: "consistent", but failed
            run["stages"]["extract"] = stage("fail", "EXTRACT_ALL_ATTEMPTS_FAILED", (5, 0, 5, 0),
                                             {"EXTRACT_ERROR": 5})
            run["stages"]["digest_gates"] = stage("skipped", "UPSTREAM_FAILED")
            run["stages"]["outputs"] = stage("skipped", "UPSTREAM_FAILED")
        self.assertFailsWith(receipt, "three_successful_runs", "extract_advanced", "fault_telegram_down")

    def test_a_failed_stage_fails_even_with_exit_zero(self):
        receipt = good_receipt()
        receipt["runs"][1]["stages"]["portal"] = stage("fail", "PORTAL_INTAKE_FAILED", (0, 0, 1, 0))
        self.assertFailsWith(receipt, "three_successful_runs")

    def test_a_skipped_work_stage_fails(self):
        receipt = good_receipt()
        receipt["runs"][0]["stages"]["adoption"] = stage("skipped", "ADOPTION_NOT_CONFIGURED")
        self.assertFailsWith(receipt, "three_successful_runs")

    def test_missing_or_invalid_report_fails(self):
        for key, value in (("report_present", False), ("report_valid", False)):
            with self.subTest(key):
                receipt = good_receipt()
                receipt["runs"][2][key] = value
                self.assertFailsWith(receipt, "three_successful_runs")

    def test_fewer_than_three_runs_or_a_mismatched_run_count_fails(self):
        receipt = good_receipt()
        receipt["runs"] = receipt["runs"][:2]
        receipt["expect"]["runs"] = 2
        self.assertFailsWith(receipt, "three_successful_runs", "expectations_explicit")
        receipt = good_receipt()
        receipt["expect"]["runs"] = 4
        self.assertFailsWith(receipt, "three_successful_runs")

    def test_missing_adoption_counts_are_not_defaulted_to_zero(self):
        for index in (0, 1, 2):
            with self.subTest(run=index + 1):
                receipt = good_receipt()
                del receipt["runs"][index]["stages"]["adoption"]["counts"]["output"]
                failures = self.failures(receipt)
                self.assertIn("adoption_counts_present", failures)
                self.assertIn("adoption_matches_expected" if index == 0 else "rerun_adopts_zero", failures)
        receipt = good_receipt()
        del receipt["runs"][1]["stages"]["adoption"]
        self.assertFailsWith(receipt, "adoption_counts_present", "rerun_adopts_zero")

    def test_no_successful_adoption_fails(self):
        receipt = good_receipt()
        first = receipt["runs"][0]
        first["stages"]["adoption"]["counts"]["output"] = 0
        first["stages"]["adoption"]["counts"]["skipped"] += 36
        self.assertFailsWith(receipt, "adoption_matches_expected", "adoption_conserved")
        receipt = good_receipt()
        receipt["expect"]["adopted"] = 0
        self.assertFailsWith(receipt, "expectations_explicit")

    def test_coded_gaps_must_match_the_explicit_expectation(self):
        receipt = good_receipt()
        receipt["expect"]["coded_gaps"] = 114
        self.assertFailsWith(receipt, "coded_gaps_match_expected", "adoption_conserved")
        receipt = good_receipt()
        receipt["runs"][2]["needs_g"] = [item for item in receipt["runs"][2]["needs_g"]
                                         if item["code"] != "ADOPTION_CODED_GAPS"]
        self.assertFailsWith(receipt, "coded_gaps_match_expected")

    def test_conservation_breaks_fail(self):
        receipt = good_receipt()
        receipt["runs"][0]["ledger_after"]["mail_messages"] = 38  # one adopted message missing
        self.assertFailsWith(receipt, "adoption_conserved")
        receipt = good_receipt()
        receipt["runs"][0]["stages"]["adoption"]["counts"]["failed"] = 1
        self.assertFailsWith(receipt, "adoption_conserved")
        receipt = good_receipt()
        receipt["sandbox_before"]["mail_messages"] = 4
        self.assertFailsWith(receipt, "adoption_conserved")

    def test_rerun_that_adopts_or_changes_the_ledger_fails(self):
        receipt = good_receipt()
        receipt["runs"][2]["stages"]["adoption"]["counts"]["output"] = 1
        self.assertFailsWith(receipt, "rerun_adopts_zero")
        receipt = good_receipt()
        receipt["runs"][1]["ledger_after"]["originals"] += 1
        self.assertFailsWith(receipt, "ledger_stable_after_first_run")

    def test_integrity_must_be_ok_on_every_run(self):
        for key, value in (("quick_check", "*** in database main ***"), ("foreign_key_violations", 2),
                           ("portal_quick_check", "corrupt")):
            with self.subTest(key):
                receipt = good_receipt()
                receipt["runs"][1]["ledger_after"][key] = value
                self.assertFailsWith(receipt, "integrity_ok")

    def test_no_extraction_is_missing_advancement(self):
        receipt = good_receipt()
        first = receipt["runs"][0]
        first["stages"]["extract"] = stage(counts=(4, 0, 0, 4), outcomes={"AWAITING_OCR": 4})
        first["new_data"]["extracted_units"] = 0
        self.assertFailsWith(receipt, "extract_advanced")

    def test_dropped_awaiting_ocr_document_fails(self):
        receipt = good_receipt()
        receipt["runs"][1]["stages"]["extract"]["outcomes"]["AWAITING_OCR"] = 3  # input is 4
        self.assertFailsWith(receipt, "extract_outcomes_account_for_every_document")
        receipt = good_receipt()
        receipt["runs"][2]["needs_g"][1]["count"] = 3  # needs-G item disagrees with the outcome
        self.assertFailsWith(receipt, "extract_outcomes_account_for_every_document")

    def test_parser_missing_is_a_failure_not_ocr_accounting(self):
        receipt = good_receipt()
        run = receipt["runs"][0]
        run["stages"]["extract"] = stage("warn", "EXTRACT_NATIVE_PARSER_MISSING", (84, 40, 0, 44),
                                         {"AWAITING_OCR": 4, "EXTRACTED": 40, "PARSER_MISSING": 40})
        run["needs_g"].append({"code": "EXTRACT_NATIVE_PARSER_MISSING", "stage": "extract",
                               "severity": "action", "count": 40})
        self.assertFailsWith(receipt, "native_parsers_present", "three_successful_runs")
        # Allowing the warning does not help while parsers are required ...
        receipt["expect"]["allowed_warnings"].append(["extract", "EXTRACT_NATIVE_PARSER_MISSING"])
        self.assertFailsWith(receipt, "native_parsers_present", "expectations_explicit")
        # ... only an explicit --no-require-native-parsers run accepts it.
        receipt["expect"]["require_native_parsers"] = False
        self.assertEqual(self.failures(receipt), [])

    def test_parser_missing_in_a_fault_run_also_fails(self):
        receipt = good_receipt()
        receipt["faults"]["corrupt_pdf"]["stages"]["extract"]["outcomes"]["PARSER_MISSING"] = 1
        self.assertFailsWith(receipt, "native_parsers_present")

    def test_unexpected_warning_fails_unless_explicitly_expected(self):
        receipt = good_receipt()
        receipt["runs"][1]["stages"]["portal"] = stage("warn", "PORTAL_HOST_REFUSED", (3, 2, 0, 1))
        self.assertFailsWith(receipt, "three_successful_runs")
        receipt["expect"]["allowed_warnings"].append(["portal", "PORTAL_HOST_REFUSED"])
        self.assertEqual(self.failures(receipt), [])

    def test_missing_fault_cannot_pass(self):
        for fault in ("duplicate_receipt", "portal_404", "corrupt_pdf", "docker_down"):
            with self.subTest(fault):
                receipt = good_receipt()
                del receipt["faults"][fault]
                self.assertFailsWith(receipt, "fault_" + fault)

    def test_unexercised_or_undetected_fault_cannot_pass(self):
        cases = {
            "portal_404 refused": ("portal_404", lambda f: f["injection"].update(status="refused")),
            "portal_404 not detected": ("portal_404", lambda f: f["stages"].update(portal=stage(counts=(2, 2, 0, 0)))),
            "portal_404 stops the run": ("portal_404", lambda f: f["stages"].update(
                extract=stage("skipped", "UPSTREAM_FAILED"), digest_gates=stage("skipped", "UPSTREAM_FAILED"))),
            "corrupt_pdf refused": ("corrupt_pdf", lambda f: f["injection"].update(status="refused")),
            "corrupt_pdf not coded": ("corrupt_pdf", lambda f: f["stages"]["extract"]["outcomes"].pop(
                "CORRUPT_DOCUMENT")),
            "corrupt_pdf stops the run": ("corrupt_pdf", lambda f: f["stages"].update(
                outputs=stage("skipped", "UPSTREAM_FAILED"))),
            "duplicate not injected": ("duplicate_receipt", lambda f: f.update(injected=False)),
            "duplicate not restored": ("duplicate_receipt", lambda f: f.update(restored=False)),
            "duplicate adopted": ("duplicate_receipt", lambda f: f["stages"]["adoption"]["counts"].update(output=1)),
            "duplicate counts missing": ("duplicate_receipt", lambda f: f["stages"]["adoption"].pop("counts")),
            "duplicate not blocked": ("duplicate_receipt", lambda f: f.update(exit=0)),
            "duplicate gap not coded": ("duplicate_receipt", lambda f: f.update(needs_g=f["needs_g"][:2])),
            "duplicate stops the run": ("duplicate_receipt", lambda f: f["stages"].update(
                portal=stage("skipped", "UPSTREAM_FAILED"))),
            "duplicate wrote": ("duplicate_receipt", lambda f: f["ledger_after"].update(mail_messages=40)),
            "docker_down wrote": ("docker_down", lambda f: f.update(ledger_unchanged=False)),
            "docker_down exit 0": ("docker_down", lambda f: f.update(exit=0)),
        }
        for label, (fault, mutate) in cases.items():
            with self.subTest(label):
                receipt = good_receipt()
                mutate(receipt["faults"][fault])
                self.assertFailsWith(receipt, "fault_" + fault)

    def test_fault_scenarios_reject_unrelated_warnings_and_failures(self):
        """A detected fault never excuses an unrelated stage result in the same run (T12s review)."""
        unrelated = {
            "sync warn": ("sync", stage("warn", "SYNC_STATUS_UNREADABLE", (4, 4, 0, 0))),
            "delta fail": ("delta", stage("fail", "DELTA_SCAN_FAILED", (4, 0, 1, 0))),
            "portal refused": ("portal", stage("warn", "PORTAL_HOST_REFUSED", (3, 2, 0, 1))),
            "gates skipped": ("digest_gates", stage("skipped", "UPSTREAM_FAILED")),
            "outputs warn": ("outputs", stage("warn", "OUTPUTS_PARTIAL", (1, 0, 0, 1))),
        }
        for fault, scenario in harness.FAULT_SCENARIOS.items():
            for label, (name, result) in unrelated.items():
                if name in scenario["stages"]:
                    continue  # that stage's expected result is covered below
                with self.subTest(fault=fault, unrelated=label):
                    receipt = good_receipt()
                    receipt["faults"][fault]["stages"][name] = copy.deepcopy(result)
                    self.assertFailsWith(receipt, f"fault_{fault}_only_expected_results")
            with self.subTest(fault=fault, unrelated="extract failure"):
                if "extract" not in scenario["stages"]:
                    receipt = good_receipt()
                    receipt["faults"][fault]["stages"]["extract"] = stage(
                        "warn", "EXTRACT_DOCUMENT_FAILURES", (5, 0, 1, 4), {"AWAITING_OCR": 4, "EXTRACT_ERROR": 1})
                    self.assertFailsWith(receipt, f"fault_{fault}_only_expected_results")
            with self.subTest(fault=fault, unrelated="exit"):
                receipt = good_receipt()
                receipt["faults"][fault]["exit"] = 13
                self.assertFailsWith(receipt, f"fault_{fault}_only_expected_results")
            with self.subTest(fault=fault, unrelated="invalid report"):
                receipt = good_receipt()
                receipt["faults"][fault]["report_valid"] = False
                self.assertFailsWith(receipt, f"fault_{fault}_only_expected_results", "run_reports_valid")

    def test_each_scenario_requires_exactly_its_expected_stage_results(self):
        for fault, scenario in harness.FAULT_SCENARIOS.items():
            for name, (status, code) in scenario["stages"].items():
                for label, result in (("ok", stage(counts=(1, 1, 0, 0))),
                                      ("other code", stage(status, "SOMETHING_ELSE", (1, 0, 1, 0))),
                                      ("other status", stage("fail" if status == "warn" else "warn", code,
                                                             (1, 0, 1, 0)))):
                    with self.subTest(fault=fault, stage=name, result=label):
                        receipt = good_receipt()
                        if name == "extract":  # keep the outcome accounting valid
                            result["outcomes"] = {"AWAITING_OCR": 4}
                            result["counts"]["input"] = 4
                        receipt["faults"][fault]["stages"][name] = result
                        self.assertFailsWith(receipt, f"fault_{fault}_only_expected_results")

    def test_baseline_expected_gaps_stay_allowed_in_fault_runs(self):
        receipt = good_receipt()
        for fault in ("duplicate_receipt", "portal_404"):
            receipt["faults"][fault]["stages"]["extract"] = stage(
                "warn", "EXTRACT_WORKBOOK_POLICY_REQUIRED", (5, 0, 0, 5),
                {"AWAITING_OCR": 4, "WORKBOOK_POLICY_REQUIRED": 1})
        self.assertEqual(self.failures(receipt), [])

    def test_persistent_prior_faults_are_explicit(self):
        # The portal item blocked by portal_404 is still blocked in the corrupt_pdf run ...
        self.assertEqual(harness.FAULT_SCENARIOS["corrupt_pdf"]["stages"]["portal"], ("warn", "PORTAL_HTTP_404"))
        receipt = good_receipt()
        receipt["faults"]["corrupt_pdf"]["stages"]["portal"] = stage(counts=(2, 2, 0, 0))
        self.assertFailsWith(receipt, "fault_corrupt_pdf_only_expected_results")
        # ... but a corrupt document showing up in the portal_404 run is not expected there.
        receipt = good_receipt()
        receipt["faults"]["portal_404"]["stages"]["extract"] = copy.deepcopy(
            receipt["faults"]["corrupt_pdf"]["stages"]["extract"])
        receipt["faults"]["portal_404"]["needs_g"] = copy.deepcopy(receipt["faults"]["corrupt_pdf"]["needs_g"])
        self.assertFailsWith(receipt, "fault_portal_404_only_expected_results")
        # And the duplicate's delta conflict must not linger into a later scenario.
        receipt = good_receipt()
        receipt["faults"]["portal_404"]["stages"]["delta"] = stage("warn", "DELTA_BLOCKING_CONFLICTS",
                                                                   (154, 0, 0, 154))
        self.assertFailsWith(receipt, "fault_portal_404_only_expected_results")

    def test_fault_runs_keep_accounting_integrity_and_idempotency(self):
        cases = {
            "dropped document": ("corrupt_pdf", lambda f: f["stages"]["extract"]["outcomes"].update(AWAITING_OCR=3),
                                 "extract_outcomes_account_for_every_document"),
            "extract counts missing": ("portal_404", lambda f: f["stages"]["extract"].pop("counts"),
                                       "extract_outcomes_account_for_every_document"),
            "integrity": ("duplicate_receipt", lambda f: f["ledger_after"].update(foreign_key_violations=1),
                          "integrity_ok"),
            "portal ledger missing": ("portal_404", lambda f: f.pop("ledger_after"), "integrity_ok"),
            "portal run adopts": ("portal_404", lambda f: f["stages"]["adoption"]["counts"].update(output=1),
                                  "fault_portal_404"),
            "corrupt run ledger grows": ("corrupt_pdf", lambda f: f["ledger_after"].update(mail_messages=41),
                                         "fault_corrupt_pdf"),
            "corrupt injection count missing": ("corrupt_pdf", lambda f: f["injection"].pop("messages"),
                                                "fault_corrupt_pdf"),
        }
        for label, (fault, mutate, check) in cases.items():
            with self.subTest(label):
                receipt = good_receipt()
                mutate(receipt["faults"][fault])
                self.assertFailsWith(receipt, check)

    def test_telegram_down_must_fail_notify_on_every_run(self):
        receipt = good_receipt()
        receipt["runs"][1]["notify_failed"] = False
        self.assertFailsWith(receipt, "fault_telegram_down")

    def test_unknown_required_fault_cannot_pass(self):
        failures = self.failures(good_receipt(), required_faults=harness.REQUIRED_FAULTS + ("disk_full",))
        self.assertEqual(failures, ["fault_disk_full"])

    def test_receipt_is_test_evidence_never_live_acceptance(self):
        for key, value in (("target_mode", "LIVE_ACCEPTANCE"), ("live_acceptance", True)):
            with self.subTest(key):
                receipt = good_receipt()
                receipt[key] = value
                self.assertFailsWith(receipt, "target_mode_is_test_evidence")

    def test_isolation_checks(self):
        for key, value in (("live_tree_unchanged", False), ("test_tree_unchanged", False),
                           ("test_after", {"mail_messages": 40, "originals": 160})):
            with self.subTest(key):
                receipt = good_receipt()
                receipt[key] = value
                self.assertTrue(set(self.failures(receipt)) & {"live_tree_unchanged", "test_tree_unchanged",
                                                               "test_untouched"})

    def test_empty_receipt_fails_everything(self):
        checks, failures = harness.evaluate({})
        self.assertEqual(set(failures), set(checks))


def with_failures(run, failures):
    """Give ``run``'s extract stage these failure outcomes (on top of its other outcomes)."""
    extract = run["stages"]["extract"]
    outcomes = {code: count for code, count in extract["outcomes"].items()
                if code not in harness.EXTRACT_FAILURE_NEEDS}
    outcomes.update(failures)
    total = sum(failures.values())
    counts = extract["counts"]
    run["stages"]["extract"] = stage("warn" if total else "ok", "EXTRACT_DOCUMENT_FAILURES" if total else None,
                                     (sum(outcomes.values()), counts["output"], total,
                                      sum(outcomes.values()) - counts["output"] - total), outcomes)
    run["needs_g"] = [item for item in run["needs_g"] if item["stage"] != "extract"
                      or item["code"] not in harness.EXTRACT_FAILURE_NEEDS.values()]
    run["needs_g"] += [{"code": harness.EXTRACT_FAILURE_NEEDS[code], "stage": "extract", "severity": "action",
                        "count": count} for code, count in failures.items()]


def declared_receipt(corrupt=20):
    """good_receipt() with ``corrupt`` declared CORRUPT_DOCUMENT gaps (G D4-A, TEST shape)."""
    receipt = good_receipt()
    declared = {"CORRUPT_DOCUMENT": corrupt}
    receipt["expect"]["extract_failures"] = dict(declared)
    for run in receipt["runs"] + [receipt["faults"]["duplicate_receipt"], receipt["faults"]["portal_404"]]:
        with_failures(run, declared)
    with_failures(receipt["faults"]["corrupt_pdf"], {"CORRUPT_DOCUMENT": corrupt + 1})
    receipt["extract_failure_manifest"] = {
        "status": "ok", "by_code": dict(declared),
        "documents": [{"original_sha256": f"{index:064x}", "code": "CORRUPT_DOCUMENT", "form": None,
                       "mail": {"uid": index, "date_utc": "2026-09-30", "sender_domain_class": "records_portal"}}
                      for index in range(corrupt)]}
    return receipt


class DeclaredExtractFailureTests(unittest.TestCase):
    """G D4-A: declared extraction gaps are exact on every run, never a blanket allowance."""

    def failures(self, receipt):
        return harness.evaluate(receipt)[1]

    def test_the_declared_test_shape_passes(self):
        checks, failures = harness.evaluate(declared_receipt())
        self.assertEqual(failures, [])
        self.assertIs(checks["extract_failures_itemized"], True)

    def test_undeclared_extract_failures_fail_every_run_kind(self):
        receipt = declared_receipt()
        del receipt["expect"]["extract_failures"]
        failures = self.failures(receipt)
        for name in ("three_successful_runs", "fault_duplicate_receipt_only_expected_results",
                     "fault_portal_404_only_expected_results", "fault_corrupt_pdf",
                     "fault_corrupt_pdf_only_expected_results"):
            self.assertIn(name, failures)

    def test_counts_must_be_exact_on_every_baseline_run(self):
        for index in (0, 1, 2):
            for label, failures in (("one fewer", {"CORRUPT_DOCUMENT": 19}), ("one more", {"CORRUPT_DOCUMENT": 21}),
                                    ("other code", {"CORRUPT_DOCUMENT": 20, "EXTRACT_ERROR": 1}),
                                    ("none", {})):
                with self.subTest(run=index + 1, case=label):
                    receipt = declared_receipt()
                    with_failures(receipt["runs"][index], failures)
                    self.assertIn("three_successful_runs", self.failures(receipt))

    def test_failed_count_and_needs_items_must_agree(self):
        receipt = declared_receipt()
        receipt["runs"][1]["stages"]["extract"]["counts"]["failed"] = 21
        self.assertIn("three_successful_runs", self.failures(receipt))
        receipt = declared_receipt()
        for item in receipt["runs"][2]["needs_g"]:
            if item["code"] == "EXTRACT_CORRUPT_DOCUMENT":
                item["count"] = 19
        self.assertIn("three_successful_runs", self.failures(receipt))

    def test_declared_gaps_persist_exactly_in_the_fault_runs(self):
        for fault in ("duplicate_receipt", "portal_404"):
            with self.subTest(fault):
                receipt = declared_receipt()
                with_failures(receipt["faults"][fault], {"CORRUPT_DOCUMENT": 21})
                self.assertIn(f"fault_{fault}_only_expected_results", self.failures(receipt))

    def test_corrupt_pdf_must_add_exactly_one_corrupt_document(self):
        for count in (20, 22):
            with self.subTest(count):
                receipt = declared_receipt()
                with_failures(receipt["faults"]["corrupt_pdf"], {"CORRUPT_DOCUMENT": count})
                failures = self.failures(receipt)
                self.assertIn("fault_corrupt_pdf", failures)
                self.assertIn("fault_corrupt_pdf_only_expected_results", failures)
        # Without declared gaps the corrupt_pdf run must show exactly one, too.
        receipt = good_receipt()
        with_failures(receipt["faults"]["corrupt_pdf"], {"CORRUPT_DOCUMENT": 2})
        self.assertIn("fault_corrupt_pdf", self.failures(receipt))

    def test_a_blanket_allowance_is_not_an_expectation(self):
        receipt = declared_receipt()
        del receipt["expect"]["extract_failures"]
        receipt["expect"]["allowed_warnings"].append(list(harness.EXTRACT_FAILURES_WARNING))
        self.assertIn("expectations_explicit", self.failures(receipt))
        receipt = declared_receipt()
        receipt["expect"]["allowed_warnings"].append(list(harness.EXTRACT_FAILURES_WARNING))
        self.assertIn("expectations_explicit", self.failures(receipt))

    def test_invalid_declarations_fail(self):
        for value in ({"CORRUPT": 20}, {"CORRUPT_DOCUMENT": 0}, {"CORRUPT_DOCUMENT": "20"},
                      {"AWAITING_OCR": 2}, {"PARSER_MISSING": 1}, ["CORRUPT_DOCUMENT", 20]):
            with self.subTest(value=value):
                receipt = declared_receipt()
                receipt["expect"]["extract_failures"] = value
                self.assertIn("expectations_explicit", self.failures(receipt))

    def test_the_declared_gaps_must_be_itemized(self):
        cases = {
            "missing": lambda m: m.clear(),
            "refused": lambda m: m.update(status="refused"),
            "one short": lambda m: m["documents"].pop(),
            "by_code differs": lambda m: m.update(by_code={"CORRUPT_DOCUMENT": 19}),
            "duplicate document": lambda m: m["documents"].__setitem__(1, dict(m["documents"][0])),
            "not a hash": lambda m: m["documents"][0].update(original_sha256="x"),
            "other code": lambda m: m["documents"][0].update(code="EXTRACT_ERROR"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                receipt = declared_receipt()
                mutate(receipt["extract_failure_manifest"])
                self.assertIn("extract_failures_itemized", self.failures(receipt))

    # T13 review (Sol #2): the full map of extract-failure needs-G items is compared.

    def stray_item(self, code="EXTRACT_ERROR", stage="extract", count=1):
        return {"code": code, "stage": stage, "severity": "action", "count": count}

    def test_an_undeclared_needs_item_fails_the_baseline_runs(self):
        for index in (0, 1, 2):
            for code in sorted(set(harness.EXTRACT_FAILURE_NEEDS.values()) - {"EXTRACT_CORRUPT_DOCUMENT"}):
                with self.subTest(run=index + 1, code=code):
                    receipt = declared_receipt()
                    receipt["runs"][index]["needs_g"].append(self.stray_item(code))
                    self.assertIn("three_successful_runs", self.failures(receipt))

    def test_an_undeclared_needs_item_fails_every_fault_scenario(self):
        for fault in harness.FAULT_SCENARIOS:
            with self.subTest(fault):
                receipt = declared_receipt()
                receipt["faults"][fault]["needs_g"].append(self.stray_item())
                failures = self.failures(receipt)
                self.assertIn(f"fault_{fault}_only_expected_results", failures)
                if fault == "corrupt_pdf":
                    self.assertIn("fault_corrupt_pdf", failures)

    def test_an_undeclared_needs_item_fails_without_any_declaration(self):
        receipt = good_receipt()
        self.assertEqual(self.failures(receipt), [])
        for target in ("baseline", *harness.FAULT_SCENARIOS):
            with self.subTest(target):
                receipt = good_receipt()
                run = receipt["runs"][0] if target == "baseline" else receipt["faults"][target]
                run["needs_g"].append(self.stray_item())
                name = ("three_successful_runs" if target == "baseline"
                        else f"fault_{target}_only_expected_results")
                self.assertIn(name, self.failures(receipt))

    def test_corrupt_pdf_needs_items_are_exactly_declared_plus_one(self):
        for label, extra in (("one more", 1), ("one fewer", -1)):
            with self.subTest(label):
                receipt = declared_receipt()
                for item in receipt["faults"]["corrupt_pdf"]["needs_g"]:
                    if item["code"] == "EXTRACT_CORRUPT_DOCUMENT":
                        item["count"] += extra
                failures = self.failures(receipt)
                self.assertIn("fault_corrupt_pdf", failures)
                self.assertIn("fault_corrupt_pdf_only_expected_results", failures)

    def test_a_failure_needs_item_on_another_stage_or_malformed_fails(self):
        for label, item in {"other stage": self.stray_item("EXTRACT_CORRUPT_DOCUMENT", stage="outputs", count=20),
                            "negative": self.stray_item("EXTRACT_ERROR", count=-1),
                            "string count": {**self.stray_item("EXTRACT_ERROR"), "count": "1"}}.items():
            with self.subTest(label):
                receipt = declared_receipt()
                receipt["runs"][1]["needs_g"].append(item)
                self.assertIn("three_successful_runs", self.failures(receipt))

    def test_the_manifest_carries_the_reacquisition_metadata(self):
        for label, mutate in {"mail removed": lambda d: d.pop("mail"),
                              "mail null": lambda d: d.update(mail=None),
                              "form removed": lambda d: d.pop("form"),
                              "class removed": lambda d: d["mail"].pop("sender_domain_class"),
                              "date removed": lambda d: d["mail"].pop("date_utc"),
                              "uid removed": lambda d: d["mail"].pop("uid")}.items():
            with self.subTest(label):
                receipt = declared_receipt()
                mutate(receipt["extract_failure_manifest"]["documents"][3])
                self.assertIn("extract_failures_itemized", self.failures(receipt))

    def test_codes_match_the_engine(self):
        from campaign_tool.records import run_stages
        self.assertEqual(set(harness.EXTRACT_FAILURE_NEEDS), native_extract.FAILED)
        needs = {outcome: code for outcome, code, _ in run_stages.EXTRACT_NEEDS}
        self.assertEqual(harness.EXTRACT_FAILURE_NEEDS, {code: needs[code] for code in harness.EXTRACT_FAILURE_NEEDS})


class HarnessCliTests(unittest.TestCase):
    BASE = ["--engine", "/nonexistent/engine", "--release-commit", "0" * 40, "--scratch", "/nonexistent/s",
            "--receipt", "/nonexistent/r.json", "--container", "c", "--container-work", "/w"]

    def main(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = harness.main([*self.BASE, *extra])
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue()

    def test_expectations_are_required_arguments(self):
        self.assertEqual(self.main()[0], 2)
        self.assertEqual(self.main("--expect-adopted", "36")[0], 2)
        self.assertEqual(self.main("--expect-coded-gaps", "115")[0], 2)

    def test_zero_adoption_expectation_is_refused(self):
        self.assertEqual(self.main("--expect-adopted", "0", "--expect-coded-gaps", "115")[0], 2)

    def test_fewer_than_three_runs_refused(self):
        code, out = self.main("--expect-adopted", "36", "--expect-coded-gaps", "115", "--runs", "2")
        self.assertEqual((code, json.loads(out)["code"]), (2, "runs_below_three"))

    def test_parser_missing_cannot_be_an_expected_gap_while_parsers_are_required(self):
        code, out = self.main("--expect-adopted", "36", "--expect-coded-gaps", "115",
                              "--allow-warn", "extract:EXTRACT_NATIVE_PARSER_MISSING")
        self.assertEqual((code, json.loads(out)["code"]), (2, "parser_missing_cannot_be_expected"))

    def test_bad_allow_warn_refused(self):
        self.assertEqual(self.main("--expect-adopted", "36", "--expect-coded-gaps", "115",
                                   "--allow-warn", "notify:NOTIFY_FAILED")[0], 2)

    def test_reaches_setup_only_with_valid_expectations(self):
        code, out = self.main("--expect-adopted", "36", "--expect-coded-gaps", "115")
        self.assertEqual((code, json.loads(out)["code"]), (2, "engine_tree_missing"))
        code, out = self.main("--expect-adopted", "36", "--expect-coded-gaps", "115",
                              "--expect-extract-failures", "CORRUPT_DOCUMENT=20")
        self.assertEqual((code, json.loads(out)["code"]), (2, "engine_tree_missing"))

    def test_bad_extract_failure_declarations_refused(self):
        for value in ("CORRUPT_DOCUMENT", "CORRUPT_DOCUMENT=0", "CORRUPT_DOCUMENT=-1", "CORRUPT_DOCUMENT=x",
                      "AWAITING_OCR=2", "PARSER_MISSING=1", "=3"):
            with self.subTest(value):
                self.assertEqual(self.main("--expect-adopted", "36", "--expect-coded-gaps", "115",
                                           "--expect-extract-failures", value)[0], 2)
        code, out = self.main("--expect-adopted", "36", "--expect-coded-gaps", "115",
                              "--expect-extract-failures", "CORRUPT_DOCUMENT=20",
                              "--expect-extract-failures", "CORRUPT_DOCUMENT=21")
        self.assertEqual((code, json.loads(out)["code"]), (2, "extract_failure_declared_twice"))

    def test_extract_failures_cannot_be_a_blanket_allowance(self):
        code, out = self.main("--expect-adopted", "36", "--expect-coded-gaps", "115",
                              "--allow-warn", "extract:EXTRACT_DOCUMENT_FAILURES")
        self.assertEqual((code, json.loads(out)["code"]), (2, "extract_failures_need_exact_counts"))


# --------------------------------------------------------------------------------------------
# Synthetic records project (real engine adapters)
# --------------------------------------------------------------------------------------------

def blob(mail_root, data):
    sha = hashlib.sha256(data).hexdigest()
    rel = Path("objects") / sha[:2] / sha
    eml_export._ensure_private(mail_root / rel, data)
    return {"path": str(rel), "sha256": sha, "bytes": len(data)}


def write_receipt(mail_root, uid, *, body, attachments=(), rfc822=False):
    """A legacy-shape export receipt (same shape as the adoption regression fixtures)."""
    msg = EmailMessage()
    msg["From"] = "records@t12.example.invalid"
    msg["To"] = "organizer@t12.example.invalid"
    msg["Subject"] = f"T12 synthetic message {uid}"
    msg["Message-ID"] = f"<t12-{uid}@t12.example.invalid>"
    msg.set_content(body)
    for filename, payload in attachments:
        msg.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)
    if rfc822:
        inner = EmailMessage()
        inner["Subject"] = "forwarded"
        inner.set_content("synthetic forwarded message")
        msg.add_attachment(inner)
    for index, part in enumerate(msg.walk()):
        if part.is_multipart():
            part.set_boundary(f"t12-{uid}-boundary-{index}")
    raw = msg.as_bytes(policy=SMTP)
    attached = []
    for index, part in enumerate(msg.walk()):
        if part.get_filename():
            item = blob(mail_root, part.get_payload(decode=True))
            item.update(part=index, filename=mail_delta.safe_filename(part.get_filename()),
                        original_filename=part.get_filename(), content_type=part.get_content_type())
            attached.append(item)
    receipt = {"complete": True, "account_id": "t12-synthetic-account", "folder": "T12",
               "uidvalidity": 900, "uid": uid, "bytes": len(raw), "internaldate": "2026-01-02T03:04:05Z",
               "original_eml": blob(mail_root, raw), "attachments": attached}
    key = hashlib.sha256(json.dumps([receipt[k] for k in ("account_id", "folder", "uidvalidity", "uid")])
                         .encode()).hexdigest()
    eml_export._ensure_private(mail_root / "receipts" / f"{key}.json",
                               (json.dumps(receipt, sort_keys=True) + "\n").encode())


def write_private(path, data):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def make_project(base):
    """mail-history (4 receipts), an initialized processing-v1, portal-queue-v1, config, marker."""
    project = base / ".t11i-sandbox-synthetic" / "records-project"
    project.mkdir(parents=True, mode=0o700)
    mail = project / "mail-history"
    (mail / "receipts").mkdir(parents=True, mode=0o700)
    write_receipt(mail, 31, body="Synthetic production letter with the policy attached.\n",
                  attachments=[("policy.pdf", tiny_pdf())])
    write_receipt(mail, 32, body=f"Your records are ready: https://{shim.PORTAL_HOST}/documents/t12-42\n")
    write_receipt(mail, 33, body="Synthetic scanned page attached.\n", attachments=[("scan.pdf", scanned_pdf())])
    write_receipt(mail, 34, body="Synthetic forward.\n", rfc822=True)  # coded gap: unsupported_rfc822_part
    write_private(mail / "SYNC-STATUS.json", b'{"status": "ok"}\n')
    Pipeline(project / "processing-v1")
    Queue(str(project / "portal-queue-v1")).close()
    write_private(project / ".t4-adoption-config-v1.json", json.dumps(
        {"schema": mail_adoption.CONFIG_SCHEMA, "source": "mail-history", "adoption": "enabled",
         "disable_marker_sha256": None}).encode())
    write_private(project / injector.SANDBOX_MARKER, b'{"sandbox_marker_version":1}\n')
    return project


def stage_plan(project):
    """The committed TEST plan's names, timeouts and on_fail policy; argv = real adapter, locally."""
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    extra = {"adoption": ["--config", str(project / ".t4-adoption-config-v1.json")],
             "portal": ["--approval-file", "/synthetic/approval.json"],
             "extract": ["--max-documents", "200", "--document-timeout", "120"]}
    stages = []
    for entry in template["stages"]:
        name = entry["name"]
        argv = [sys.executable, "-B", str(SHIM), name, "--project", str(project),
                "--timeout", str(entry["timeout_seconds"] - 10), "--run-id", "{run_id}", *extra.get(name, ())]
        stages.append({"name": name, "argv": argv, "timeout_seconds": entry["timeout_seconds"],
                       "on_fail": entry["on_fail"]})
    return run_orchestrator.validate_target_config(
        {"target_config_version": 1, "target": "TEST", "mode": "test", "stages": stages})


def run_injector(*argv):
    out = io.StringIO()
    with mock.patch("campaign_tool.records.portal.policy.load_approval", shim.synthetic_approval), \
            contextlib.redirect_stdout(out):
        code = injector.main(list(argv))
    return code, json.loads(out.getvalue())


class FaultInjectorTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        temp = tempfile.TemporaryDirectory(prefix="t12s-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.project = make_project(self.base)

    def test_refuses_a_project_without_the_sandbox_marker(self):
        marker = self.project / injector.SANDBOX_MARKER
        marker.unlink()
        for fault in ("portal-404", "corrupt-pdf"):
            code, line = run_injector(fault, "--project", str(self.project), "--approval-file", "/a.json")
            self.assertEqual((code, line["status"], line["code"]), (1, "refused", "sandbox_marker_missing"))
        write_private(marker, b"{}\n")
        marker.chmod(0o644)
        code, line = run_injector("corrupt-pdf", "--project", str(self.project))
        self.assertEqual((code, line["code"]), (1, "sandbox_marker_unsafe"))
        self.assertEqual(sorted(p.name for p in self.project.iterdir() if p.name.startswith(".t12-inbox")), [])

    def test_portal_404_needs_the_allowlist_argument(self):
        code, line = run_injector("portal-404", "--project", str(self.project))
        self.assertEqual((code, line["code"]), (1, "allowlist_missing"))

    def test_portal_404_blocks_one_item_without_network_and_the_real_stage_reports_it(self):
        with mock.patch("socket.socket", side_effect=AssertionError("no network")):
            code, line = run_injector("portal-404", "--project", str(self.project), "--approval-file", "/a.json")
        self.assertEqual(code, 0, line)
        self.assertEqual((line["status"], line["http_status"], line["item_state"], line["item_reason"]),
                         ("injected", 404, "blocked", "http_failure"))
        self.assertEqual((line["enrolled"], line["attempted"], line["transport_calls"]), (1, 1, 1))
        self.assertNotIn(shim.PORTAL_HOST, json.dumps(line))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stage_code = shim.main(["portal", "--project", str(self.project), "--timeout", "60",
                                    "--approval-file", "/a.json"])
        parsed = run_orchestrator.parse_stage_result(out.getvalue().encode(),
                                                     run_orchestrator.load_stage_result_schema())
        self.assertEqual(stage_code, 0)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "PORTAL_HTTP_404"))
        needs = {item["code"]: item["count"] for item in parsed["needs_g"]}
        self.assertEqual(needs["PORTAL_HTTP_404"], 1)
        self.assertNotIn("PORTAL_FETCH_BLOCKED", needs)
        self.assertEqual(parsed["counts"]["failed"], 1)
        # A second injection does not attempt the blocked item again.
        code, line = run_injector("portal-404", "--project", str(self.project), "--approval-file", "/a.json")
        self.assertEqual((code, line["attempted"], line["transport_calls"]), (0, 0, 0))

    def test_blocked_item_without_404_is_reported_as_fetch_blocked(self):
        run_injector("portal-404", "--project", str(self.project), "--approval-file", "/a.json")
        con = sqlite3.connect(self.project / "portal-queue-v1" / "portal.sqlite")
        with con:
            con.execute("UPDATE portal_attempts SET evidence_json='[]'")
        con.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            shim.main(["portal", "--project", str(self.project), "--timeout", "60", "--approval-file", "/a.json"])
        parsed = json.loads(out.getvalue())
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "PORTAL_FETCH_BLOCKED"))
        self.assertIn({"code": "PORTAL_FETCH_BLOCKED", "severity": "action", "count": 1}, parsed["needs_g"])


@unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
class RealAdapterHarnessTests(unittest.TestCase):
    """Three bounded runs and three faults through the real orchestrator + run_stages adapters."""

    group_inheriting = False  # see RealAdapterHarnessUnderDefaultAclTests

    @classmethod
    def setUpClass(cls):
        os.umask(0o077)
        cls.temp = tempfile.TemporaryDirectory(prefix="t12s-e2e-")
        base = Path(cls.temp.name)
        if cls.group_inheriting:
            cls.inheritance = inherit_group_bits(base)
        cls.project = make_project(base)
        cls.work = base / "work"
        cls.work.mkdir(mode=0o700)
        cls.config = stage_plan(cls.project)
        cls.env = dict(os.environ, PYTHONPATH=str(REPO))
        cls.counter = 0
        cls.receipt = cls.build_receipt()
        cls.checks, cls.failed = harness.evaluate(cls.receipt, required_faults=WRAPPER_FAULTS, isolation=False)

    @classmethod
    def tearDownClass(cls):
        os.umask(0o077)
        cls.temp.cleanup()

    @classmethod
    def run_once(cls):
        cls.counter += 1
        report_path = cls.work / f"report-{cls.counter}.json"
        run_id = f"run_20261019T12{cls.counter:04d}Z_0123abcd"
        code = run_orchestrator.execute(cls.config, run_id, "test", run_orchestrator.utc_now(),
                                        time.time() + 1800, report_path, cls.work, env=cls.env)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return {**harness.summarize(report, code), "ledger_after": harness.ledger_counts_local(cls.project)}

    @classmethod
    def build_receipt(cls):
        expect = {"adopted": 3, "coded_gaps": 1, "require_native_parsers": True, "runs": 3,
                  "allowed_warnings": [list(pair) for pair in harness.DEFAULT_ALLOWED_WARNINGS],
                  "required_faults": list(WRAPPER_FAULTS)}
        receipt = {"receipt_version": harness.RECEIPT_VERSION, "target_mode": harness.TARGET_MODE,
                   "live_acceptance": False, "expect": expect,
                   "sandbox_before": harness.ledger_counts_local(cls.project), "runs": [], "faults": {}}
        for index in range(3):
            receipt["runs"].append({"run": index + 1, **cls.run_once()})
        receipts = cls.project / "mail-history" / "receipts"
        first = sorted(receipts.glob("*.json"))[0]
        duplicate = receipts / f"zz-duplicate-{first.name}"
        shutil.copy2(first, duplicate)
        receipt["faults"]["duplicate_receipt"] = {"injected": True, **cls.run_once()}
        duplicate.rename(cls.project / f".t12-removed-{duplicate.name}")  # as the harness does
        receipt["faults"]["duplicate_receipt"]["restored"] = True
        _, injection = run_injector("portal-404", "--project", str(cls.project), "--approval-file", "/a.json")
        receipt["faults"]["portal_404"] = {"injection": injection, **cls.run_once()}
        _, injection = run_injector("corrupt-pdf", "--project", str(cls.project))
        receipt["faults"]["corrupt_pdf"] = {"injection": injection, **cls.run_once()}
        # Not part of the receipt: one more real run with an unreadable SYNC-STATUS.json, the
        # unrelated real-adapter problem the T12s review used to show a fault run passing.
        status = cls.project / "mail-history" / "SYNC-STATUS.json"
        original = status.read_bytes()
        write_private(status, b"{not json\n")
        cls.unreadable_sync_run = cls.run_once()
        write_private(status, original)
        return receipt

    def test_real_adapter_receipt_passes_the_strict_evaluation(self):
        self.assertEqual(self.failed, [], json.dumps(self.receipt, indent=1, sort_keys=True)[:6000])

    def test_three_runs_succeed_with_explicit_gaps_and_idempotent_adoption(self):
        runs = self.receipt["runs"]
        self.assertEqual([run["exit"] for run in runs], [0, 0, 0])
        self.assertTrue(all(run["report_valid"] for run in runs))
        adoption = [run["stages"]["adoption"] for run in runs]
        self.assertEqual([a["counts"]["output"] for a in adoption], [3, 0, 0])
        self.assertEqual({(a["status"], a["error_code"]) for a in adoption}, {("warn", "ADOPTION_CODED_GAPS")})
        self.assertEqual(adoption[0]["counts"], {"input": 4, "output": 3, "failed": 0, "skipped": 1})
        self.assertEqual([run["ledger_after"]["mail_messages"] for run in runs], [3, 3, 3])
        self.assertEqual(self.receipt["sandbox_before"]["mail_messages"], 0)
        self.assertTrue(all(run["ledger_after"]["quick_check"] == "ok" for run in runs))

    def test_first_run_extracts_native_text_and_counts_awaiting_ocr(self):
        first, second = self.receipt["runs"][:2]
        extract = first["stages"]["extract"]
        # 3 adopted messages + the born-digital PDF are extracted; the scan waits for OCR.
        self.assertEqual(extract["outcomes"], {"EXTRACTED": 4, "AWAITING_OCR": 1})
        self.assertEqual((extract["status"], extract["error_code"]), ("ok", None))
        self.assertGreater(first["new_data"]["extracted_units"], 0)
        self.assertEqual(second["stages"]["extract"]["outcomes"], {"AWAITING_OCR": 1})

    def test_duplicate_receipt_is_fail_closed_and_the_run_continues(self):
        run = self.receipt["faults"]["duplicate_receipt"]
        adoption = run["stages"]["adoption"]
        self.assertEqual((adoption["status"], adoption["error_code"]), ("fail", "ADOPTION_BLOCKED"))
        self.assertEqual(adoption["counts"]["output"], 0)
        self.assertIn({"code": "ADOPTION_RECEIPT_GAPS", "stage": "adoption", "severity": "blocking", "count": 1},
                      run["needs_g"])
        self.assertEqual(run["exit"], 12)  # the first failed work stage is adoption
        self.assertEqual(run["ledger_after"]["mail_messages"], 3)
        on_fail = {stage["name"]: stage["on_fail"] for stage in self.config["stages"]}
        self.assertEqual(on_fail["adoption"], "continue")
        for name in ("portal", "extract", "digest_gates", "outputs"):
            self.assertIn(run["stages"][name]["status"], ("ok", "warn"), name)
        # Once the duplicate is moved out, the next run adopts cleanly again (exit 0).
        self.assertEqual(self.receipt["faults"]["portal_404"]["stages"]["adoption"]["status"], "warn")

    def test_portal_404_is_detected_by_the_real_portal_stage_and_the_run_continues(self):
        run = self.receipt["faults"]["portal_404"]
        self.assertEqual(run["injection"]["status"], "injected")
        self.assertEqual(run["exit"], 0)
        self.assertEqual((run["stages"]["portal"]["status"], run["stages"]["portal"]["error_code"]),
                         ("warn", "PORTAL_HTTP_404"))
        on_fail = {stage["name"]: stage["on_fail"] for stage in self.config["stages"]}
        self.assertEqual(on_fail["portal"], "continue")
        for name in ("extract", "digest_gates", "outputs"):
            self.assertIn(run["stages"][name]["status"], ("ok", "warn"), name)

    def test_corrupt_pdf_is_coded_by_the_real_extract_stage_and_the_run_continues(self):
        run = self.receipt["faults"]["corrupt_pdf"]
        self.assertEqual(run["injection"]["status"], "injected")
        extract = run["stages"]["extract"]
        self.assertEqual(extract["outcomes"].get("CORRUPT_DOCUMENT"), 1)
        self.assertEqual((extract["status"], extract["error_code"]), ("warn", "EXTRACT_DOCUMENT_FAILURES"))
        self.assertEqual(run["exit"], 0)
        on_fail = {stage["name"]: stage["on_fail"] for stage in self.config["stages"]}
        self.assertEqual(on_fail["extract"], "stop")  # a coded document is not a stage failure
        for name in ("digest_gates", "outputs"):
            self.assertIn(run["stages"][name]["status"], ("ok", "warn"), name)

    def test_an_unrelated_real_adapter_warning_fails_a_fault_scenario(self):
        """Sol's T12s reproduction: a real run with an unreadable SYNC-STATUS.json, after the
        injected faults, must not pass as the portal_404 or corrupt_pdf scenario."""
        run = self.unreadable_sync_run
        self.assertEqual((run["exit"], run["report_valid"]), (0, True))
        self.assertEqual((run["stages"]["sync"]["status"], run["stages"]["sync"]["error_code"]),
                         ("warn", "SYNC_STATUS_UNREADABLE"))
        # The earlier faults persist in this run: portal 404 and the corrupt document.
        self.assertEqual(run["stages"]["portal"]["error_code"], "PORTAL_HTTP_404")
        self.assertEqual(run["stages"]["extract"]["error_code"], "EXTRACT_DOCUMENT_FAILURES")
        for fault in ("portal_404", "corrupt_pdf"):
            with self.subTest(fault):
                receipt = copy.deepcopy(self.receipt)
                receipt["faults"][fault] = {"injection": receipt["faults"][fault]["injection"],
                                            **copy.deepcopy(run)}
                failures = harness.evaluate(receipt, required_faults=WRAPPER_FAULTS, isolation=False)[1]
                self.assertIn(f"fault_{fault}_only_expected_results", failures)
        # The only thing wrong with it as the corrupt_pdf run is the sync warning.
        receipt = copy.deepcopy(self.receipt)
        receipt["faults"]["corrupt_pdf"] = {"injection": receipt["faults"]["corrupt_pdf"]["injection"],
                                            **copy.deepcopy(run)}
        self.assertEqual(harness.evaluate(receipt, required_faults=WRAPPER_FAULTS, isolation=False)[1],
                         ["fault_corrupt_pdf_only_expected_results"])
        receipt["faults"]["corrupt_pdf"]["stages"]["sync"] = copy.deepcopy(
            self.receipt["faults"]["corrupt_pdf"]["stages"]["sync"])
        self.assertEqual(harness.evaluate(receipt, required_faults=WRAPPER_FAULTS, isolation=False)[1], [])

    def test_real_fault_runs_match_their_scenarios_exactly(self):
        for fault, scenario in harness.FAULT_SCENARIOS.items():
            with self.subTest(fault):
                run = self.receipt["faults"][fault]
                for name, expected in scenario["stages"].items():
                    self.assertEqual((run["stages"][name]["status"], run["stages"][name]["error_code"]),
                                     expected, name)
                self.assertIs(self.checks[f"fault_{fault}_only_expected_results"], True)

    def test_the_same_runs_fail_when_the_expectations_are_wrong(self):
        for key, value, check in (("adopted", 4, "adoption_matches_expected"),
                                  ("coded_gaps", 0, "coded_gaps_match_expected")):
            with self.subTest(key):
                receipt = copy.deepcopy(self.receipt)
                receipt["expect"][key] = value
                failures = harness.evaluate(receipt, required_faults=WRAPPER_FAULTS, isolation=False)[1]
                self.assertIn(check, failures)

    def test_receipt_holds_numbers_and_codes_only(self):
        text = json.dumps(self.receipt)
        for needle in (self.temp.name, shim.PORTAL_HOST, "t12.example.invalid", "policy.pdf"):
            self.assertNotIn(needle, text)
        self.assertEqual(self.receipt["target_mode"], "TEST_SANDBOX_EVIDENCE")
        self.assertIs(self.receipt["live_acceptance"], False)

    def test_report_validation_is_real(self):
        report = json.loads(sorted(self.work.glob("report-*.json"))[0].read_text())
        run_report.validate_run_report(report)
        self.assertTrue(harness.report_is_valid(report))
        report["overall_status"] = "ok" if report["overall_status"] != "ok" else "fail"
        self.assertFalse(harness.report_is_valid(report))
        self.assertFalse(harness.report_is_valid({}))


def inherit_group_bits(base):
    """Make new files under ``base`` keep the group bits they are created with, umask or not.

    That is the rootless Flock host-work (T13 receipt attempt 1, cause 4): its default ACL
    (``default:user:<container coder>:rwx``, ``default:mask::rwx``) makes the kernel ignore
    the process umask, so ``open(..., "w")`` gives 0660 even under ``umask 077``. Uses a
    real default ACL when ``setfacl`` works here; otherwise umask 007, which gives new
    files the same mode bits. Returns which one is in force.
    """
    setfacl = shutil.which("setfacl")
    if setfacl:
        proc = subprocess.run([setfacl, "-d", "-m", f"u:{os.geteuid()}:rwx,g::---,m::rwx,o::---", str(base)],
                              capture_output=True)
        if proc.returncode == 0:
            probe = base / ".acl-probe"
            with open(probe, "w"):  # requested 0666; the umask (077 here) must not apply
                pass
            inherited = os.stat(probe).st_mode & 0o070 == 0o060
            probe.unlink()
            if inherited:
                return "default_acl"
            subprocess.run([setfacl, "-k", str(base)], capture_output=True)
    os.umask(0o007)
    return "umask_007"


class RealAdapterHarnessUnderDefaultAclTests(RealAdapterHarnessTests):
    """The same real-adapter runs where new files inherit group bits (``inherit_group_bits``).

    The owner-only checks (sandbox marker, extraction artifacts) stay strict. The engine has
    to create its private files 0600 and its private directories 0700 explicitly instead of
    relying on the umask, or the receipt fails as it did on the swapped runtime.
    """
    group_inheriting = True

    def test_group_bits_really_are_inherited(self):
        self.assertIn(self.inheritance, ("default_acl", "umask_007"))
        probe = self.project.parent / ".mode-probe"
        with open(probe, "w"):
            pass
        self.addCleanup(probe.unlink)
        self.assertEqual(os.stat(probe).st_mode & 0o070, 0o060)

    def test_extraction_artifacts_are_owner_only(self):
        extract = self.project / "processing-v1" / "extract"
        artifacts = [p for p in extract.rglob("*") if p.is_file() and not p.is_symlink()]
        self.assertTrue(any(p.name == "digest.json" for p in artifacts))
        loose = sorted(str(p.relative_to(extract)) for p in artifacts if p.stat().st_mode & 0o077)
        self.assertEqual(loose, [])


if __name__ == "__main__":
    unittest.main()
