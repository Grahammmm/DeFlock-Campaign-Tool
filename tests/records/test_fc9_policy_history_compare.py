"""Offline fictional historical policy composition; discovery imports are inert.

The fixture is authored here, not a capture of real records. Every expectation
is frozen separately from the version handoff. There are no product changes.
"""
import argparse
import copy
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc9-policy-history-compare/cases.json"


def load_api():
    # Script mode derives its imports from this file, never the caller's cwd.
    # Ordinary discovery does not modify sys.path or ambient tempfile settings.
    previous = list(sys.path)
    try:
        sys.path.insert(0, str(REPO))
        return tuple(importlib.import_module("campaign_tool.records.understanding." + n)
                     for n in ("evidence", "policy", "reconcile"))
    finally:
        sys.path[:] = previous


def selected_handoff(real_requirements, versions, **scope):
    """Test-local seam between the real selector and real comparison API."""
    return real_requirements(versions, **scope)


class HistoricalComparisonTests(unittest.TestCase):
    def setUp(self):
        # Pure-API tests: scoped guards restore themselves, even after failure.
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.create_connection", "sqlite3.connect", "subprocess.Popen"):
            guard = mock.patch(target, side_effect=RuntimeError("offline_api_not_allowed"))
            guard.start()
            self.addCleanup(guard.stop)
        self.evidence, self.policy, self.reconcile = load_api()
        self.real_requirements = self.policy.requirements_for
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.cases = {c["id"]: c for c in self.fixture["cases"]}
        self.assertEqual(len(self.cases), 10)
        self.bundle, self.versions, self.facts, self.rule_cites = self.make_inputs()

    def make_inputs(self):
        evidence = self.evidence
        bundles = []
        version_rows = []
        rules = {}
        for spec in self.fixture["versions"]:
            # The content pin binds exactly the corresponding fictional source.
            subject = hashlib.sha256(spec["text"].encode()).hexdigest()
            receipt = {"schema": "format-extraction-v1", "original_sha256": subject,
                       "original_changed": False, "status": "complete", "parser": "fictional",
                       "parser_version": "0", "route": {"format": "text"},
                       "pages": [{"page_no": spec["unit_locator"]["page"], "status": "ok"}],
                       "units": [{"kind": "text", "locator": spec["unit_locator"],
                                  "text": spec["text"], "data": {}}]}
            bundle = evidence.normalize_receipt(receipt, subject, evidence.sha(receipt))
            bundles.append(bundle)
            cite = evidence.cite_text(bundle["units"][0], spec["text"])
            rules[spec["version_id"]] = cite
            row = {k: spec[k] for k in ("version_id", "effective_from", "issued_at", "retrieved_at")}
            for key in ("effective_to", "supersession_status"):
                if key in spec:
                    row[key] = spec[key]
            row.update(family_id="fictional-retention", agency_id=self.fixture["scope"]["agency_id"],
                       jurisdiction=self.fixture["scope"]["jurisdiction"], regulated_roles=["operator"],
                       source={"source_id": spec["source_id"], "url": spec["url"],
                               "locator": spec["locator"], "content_sha256": subject,
                               "verification_status": "verified_exact_source"},
                       requirements=[{"requirement_id": "fictional-maximum-retention", "text": spec["text"],
                                      "citations": [cite], "test": {"field": "retention_days",
                                      "operator": "at_most", "expected": spec["maximum"]}}])
            version_rows.append(row)
        spec = self.fixture["fact"]
        subject = hashlib.sha256(spec["text"].encode()).hexdigest()
        receipt = {"schema": "format-extraction-v1", "original_sha256": subject,
                   "original_changed": False, "status": "complete", "parser": "fictional",
                   "parser_version": "0", "route": {"format": "json"},
                   "pages": [{"page_no": 1, "status": "ok"}],
                   "units": [{"kind": "text", "locator": spec["locator"], "text": spec["text"], "data": {}}]}
        fact_bundle = evidence.normalize_receipt(receipt, subject, evidence.sha(receipt))
        bundles.append(fact_bundle)
        fact_cite = evidence.grounded_evidence.pointer_support(fact_bundle["units"][0], "/retention_days")
        fact = {k: spec[k] for k in ("fact_id", "event_key", "field", "value")}
        fact["citations"] = [fact_cite]
        combined = evidence.bundle([u for b in bundles for u in b["units"]],
                                   {k: v for b in bundles for k, v in b["source_context"].items()},
                                   [r for b in bundles for r in b["source_receipts"]])
        return combined, version_rows, [fact], rules

    def observe(self, case):
        versions = copy.deepcopy(self.versions)
        if "old_effective_to" in case:
            versions[0]["effective_to"] = case["old_effective_to"]
        scope = {k: v for k, v in self.fixture["scope"].items() if k != "request_number"}
        scope["event_at"] = case["event_at"]
        selected = self.real_requirements(versions, **scope)
        with mock.patch.object(self.policy, "requirements_for", side_effect=lambda rows, **kw:
                               selected_handoff(self.real_requirements, rows, **kw)):
            result = self.reconcile.compare(self.facts, versions, bundle=self.bundle,
                                           request_number=self.fixture["scope"]["request_number"], **scope)
        return selected, result

    def check_case(self, case_id):
        case = self.cases[case_id]
        before = copy.deepcopy((self.bundle, self.versions, self.facts))
        selected, result = self.observe(case)
        self.assertEqual((self.bundle, self.versions, self.facts), before, "input_mutation")
        self.assertEqual(selected["status"], case["status"])
        self.assertTrue(selected["selection"]["validation"]["valid"])
        self.assertEqual(selected["selection"]["validation"]["issues"], [])
        self.assertFalse(selected["legal_conclusion"])
        self.assertFalse(result["review_acceptance"])
        self.assertEqual(result["legal_conclusions"], 0)
        self.assertEqual(result["status"], "derived_findings_needing_review")
        self.assertEqual(result["gaps"], [])
        findings = result["findings"]
        if case["status"] == "selected":
            vid = case["version_id"]
            version = next(v for v in self.versions if v["version_id"] == vid)
            self.assertEqual(selected["selection"]["version"]["version_id"], vid)
            self.assertEqual(selected["selection"]["candidates"], [vid])
            self.assertEqual(selected["selection"]["source_pointer"], version["source"])
            self.assertEqual(selected["gaps"], [])
            self.assertEqual(len(selected["requirements"]), 1)
            requirement = selected["requirements"][0]
            self.assertEqual(requirement["text"], case["text"])
            self.assertEqual(requirement["version_id"], vid)
            self.assertEqual(requirement["source_pointer"], version["source"])
            self.assertEqual(requirement["citations"], [self.rule_cites[vid]])
            self.assertEqual(len(findings), 1)
        else:
            self.assertIsNone(selected["selection"]["version"])
            self.assertEqual(selected["requirements"], [])
            self.assertEqual([g["code"] for g in selected["gaps"]], [case["gap_code"]])
            self.assertEqual(len(findings), 2)
            self.assertEqual({f["version_id"] for f in findings}, {v["version_id"] for v in self.versions})
        for finding in findings:
            vid = finding["version_id"]
            version = next(v for v in self.versions if v["version_id"] == vid)
            expected_text = case.get("text", version["requirements"][0]["text"])
            self.assertEqual(finding["requirement_text"], expected_text, "requirement_text_mismatch")
            self.assertEqual(finding["classification"], case["classification"], "classification_mismatch")
            self.assertEqual(finding["rule_source"], version["source"])
            self.assertEqual(finding["rule_selection"], case["status"])
            self.assertEqual(finding["citations"], self.facts[0]["citations"])
            self.assertEqual(finding["fact_ids"], ["fictional-retention-fact"])
            self.assertEqual(finding["requirement_id"], "fictional-maximum-retention")
            self.assertEqual(finding["event_key"], "fictional-same-event")
            self.assertRegex(finding["finding_id"], r"^[a-f0-9]{64}$")
            self.assertEqual(finding["finding_identity_version"], "content-bound-v2")
            self.assertEqual(finding["legal_assessment"], "NOT_ASSESSED")
            self.assertFalse(finding["review_acceptance"])
            self.assertTrue(finding["derived"])
            if case["status"] != "selected":
                self.assertEqual(finding["reason"], case["status"])
            else:
                self.assertEqual(finding["version_id"], case["version_id"])
        return result

    def test_source_pins_and_exact_citations(self):
        self.assertTrue(self.fixture["synthetic_only"])
        self.assertEqual(self.fixture["fact"]["value"], 60)
        self.assertIs(type(self.fixture["fact"]["value"]), int)
        self.assertEqual(len({v["source"]["source_id"] for v in self.versions}), 2)
        self.assertEqual(len({v["source"]["locator"] for v in self.versions}), 2)
        self.assertEqual([v["requirements"][0]["test"] for v in self.versions], [
            {"field": "retention_days", "operator": "at_most", "expected": 30},
            {"field": "retention_days", "operator": "at_most", "expected": 90}])
        for spec, version in zip(self.fixture["versions"], self.versions):
            cite = self.rule_cites[version["version_id"]]
            self.assertEqual(cite["original_sha256"], hashlib.sha256(spec["text"].encode()).hexdigest())
            self.assertEqual(cite["original_sha256"], version["source"]["content_sha256"])
            self.assertEqual(cite["locator"], spec["unit_locator"])
            self.assertEqual(cite["quote"], spec["text"])
            self.assertEqual(cite["span"], {"start_byte": 0, "end_byte": len(spec["text"].encode())})
            self.assertIsNone(cite["json_pointer"])
        fact = self.facts[0]["citations"][0]
        self.assertEqual(fact["quote"], "60")
        self.assertEqual(fact["locator"], {"page": 1})
        self.assertEqual(fact["json_pointer"], "/retention_days")
        self.assertEqual(fact["span"], {"start_byte": 18, "end_byte": 20})
        for cite in [fact, *self.rule_cites.values()]:
            checked = self.evidence.validate_citation(cite, self.bundle)
            self.assertEqual(checked["structural_support"], "verified_exact_retained_bytes")
            self.assertEqual(checked["semantic_assessment"], "NOT_ASSESSED")
            self.assertFalse(checked["review_acceptance"])

    def test_finding_identity_changes_and_repeats_stably(self):
        results = [self.check_case(k) for k in ("day_before", "exact_boundary", "day_after", "retrieval_boundary")]
        ids = [r["findings"][0]["finding_id"] for r in results]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(ids[0], self.check_case("day_before")["findings"][0]["finding_id"])
        self.assertEqual(ids[1], self.check_case("exact_boundary")["findings"][0]["finding_id"])

    def test_synthetic_old_handoff_is_killed_and_restored(self):
        before = self.check_case("day_before")
        boundary = self.check_case("exact_boundary")
        case = self.cases["exact_boundary"]
        baseline_selected, _ = self.observe(case)

        def retain_old(real, versions, **scope):
            return real(versions, **dict(scope, event_at="2030-06-30"))

        with mock.patch(__name__ + ".selected_handoff", side_effect=retain_old):
            selected, mutated = self.observe(case)
            # Independent real selection is still new; only the handoff is old.
            self.assertEqual(selected, baseline_selected)
            finding = mutated["findings"][0]
            self.assertEqual(finding["version_id"], "fictional-old")
            self.assertEqual(finding["requirement_text"], "Fictional retention must be at most 30 days.")
            self.assertEqual(finding["classification"], "potential_conflict")
            self.assertNotEqual(finding["finding_id"], boundary["findings"][0]["finding_id"])
            self.assertEqual(finding["citations"], boundary["findings"][0]["citations"])
            with self.assertRaisesRegex(AssertionError, "requirement_text_mismatch"):
                self.assertEqual(finding["requirement_text"], case["text"], "requirement_text_mismatch")
            with self.assertRaisesRegex(AssertionError, "classification_mismatch"):
                self.assertEqual(finding["classification"], case["classification"], "classification_mismatch")
        final_old = self.check_case("day_before")
        final_new = self.check_case("exact_boundary")
        self.assertEqual(final_old, before)
        self.assertEqual(final_new, boundary)
        print("FC9_CONTROL " + json.dumps({"injected_behavior": "test_local_handoff_keeps_old_version",
              "baseline_observations": {"old": before, "new": boundary},
              "injected_output": mutated, "killed_assertions": self.fixture["negative_control"]["expected_failed_assertions"],
              "final_candidate_output": {"old": final_old, "new": final_new}}, sort_keys=True))


def _case_test(case_id):
    def test(self):
        self.check_case(case_id)
    test.__name__ = "test_case_" + case_id
    return test


# Fixed names avoid fixture reads or any product import at discovery time.
CASE_IDS = ("old_start", "day_before", "exact_boundary", "day_after", "retrieval_boundary",
            "retrieval_next_day", "before_start", "one_day_gap", "unknown_event", "malformed_event")
for _case_id in CASE_IDS:
    setattr(HistoricalComparisonTests, "test_case_" + _case_id, _case_test(_case_id))


def script_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporary-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.temporary_root
    if not root.is_absolute() or any(p.is_symlink() for p in (root, *root.parents)):
        parser.error("absolute nonsymlink temporary root required")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    tempfile.tempdir = str(root)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(HistoricalComparisonTests))
    print("FC9_COVERAGE " + json.dumps({"case_ids": list(CASE_IDS), "tests_run": result.testsRun,
          "skipped": len(result.skipped), "successful": result.wasSuccessful()}, sort_keys=True))
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(script_main())
