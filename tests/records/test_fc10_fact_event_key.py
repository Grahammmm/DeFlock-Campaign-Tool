"""FC10 factual event-key refusal over a fictional, cited, selected-policy fixture.

The fixture is authored here, not a capture of real records. Discovery imports
are inert: no product import, fixture read, path change or tempfile change
happens until a test runs (or this file is run as a script).
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
FIXTURE = REPO / "tests/fixtures/fc10-fact-event-key/cases.json"
# Fixed names avoid fixture reads or any product import at discovery time.
CASE_IDS = ("omitted_default", "valid_ascii_unicode_delimiter_keys", "empty_and_whitespace",
            "null_bool_int_float_list_object", "mixed_valid_invalid_keys", "valid_multiple_events_sorted",
            "no_input_mutation", "original_fact_identity_error_preserved")


def load_api():
    # Script mode derives its imports from this file, never the caller's cwd.
    previous = list(sys.path)
    try:
        sys.path.insert(0, str(REPO))
        return tuple(importlib.import_module("campaign_tool.records.understanding." + n)
                     for n in ("evidence", "policy", "reconcile"))
    finally:
        sys.path[:] = previous


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def source_bundle(evidence, text, locator, fmt):
    subject = hashlib.sha256(text.encode()).hexdigest()
    receipt = {"schema": "format-extraction-v1", "original_sha256": subject, "original_changed": False,
               "status": "complete", "parser": "fictional", "parser_version": "0", "route": {"format": fmt},
               "pages": [{"page_no": locator["page"], "status": "ok"}],
               "units": [{"kind": "text", "locator": locator, "text": text, "data": {}}]}
    return subject, evidence.normalize_receipt(receipt, subject, evidence.sha(receipt))


def build_inputs(evidence, fixture):
    spec = fixture["version"]
    subject, rule_bundle = source_bundle(evidence, spec["text"], spec["unit_locator"], "text")
    rule_cite = evidence.cite_text(rule_bundle["units"][0], spec["text"])
    scope = fixture["scope"]
    version = {k: spec[k] for k in ("version_id", "family_id", "effective_from", "issued_at",
                                    "retrieved_at", "supersession_status")}
    version.update(agency_id=scope["agency_id"], jurisdiction=scope["jurisdiction"],
                   regulated_roles=[scope["regulated_role"]],
                   source={"source_id": spec["source_id"], "url": spec["url"], "locator": spec["locator"],
                           "content_sha256": subject, "verification_status": "verified_exact_source"},
                   requirements=[{"requirement_id": spec["requirement_id"], "text": spec["text"],
                                  "citations": [rule_cite], "test": {"field": spec["field"], "operator": "at_most",
                                                                     "expected": spec["maximum"]}}])
    fspec = fixture["fact_source"]
    _, fact_bundle = source_bundle(evidence, fspec["text"], fspec["locator"], "json")
    fact_cite = evidence.grounded_evidence.pointer_support(fact_bundle["units"][0], fspec["pointer"])
    bundles = (rule_bundle, fact_bundle)
    combined = evidence.bundle([u for b in bundles for u in b["units"]],
                               {k: v for b in bundles for k, v in b["source_context"].items()},
                               [r for b in bundles for r in b["source_receipts"]])
    return {"bundle": combined, "versions": [version], "fact_cite": fact_cite, "rule_cite": rule_cite}


def make_facts(fixture, inputs, variant):
    # Every fact is otherwise valid: real field, numeric value and a current exact citation.
    fspec = fixture["fact_source"]
    base = {"field": fspec["field"], "value": fspec["value"], "citations": [inputs["fact_cite"]]}
    return [copy.deepcopy(dict(base, **spec)) for spec in variant["facts"]]


def compare_kwargs(fixture):
    scope = fixture["scope"]
    return {k: scope[k] for k in ("agency_id", "jurisdiction", "regulated_role", "event_at", "request_number")}


def observe(reconcile, inputs, fixture, facts):
    """One literal observation: the returned result, or the exact raised type and message."""
    before = copy.deepcopy((facts, inputs["versions"], inputs["bundle"]))
    try:
        result = reconcile.compare(facts, inputs["versions"], bundle=inputs["bundle"], **compare_kwargs(fixture))
        outcome = {"outcome": "findings", "result": result}
    except Exception as error:  # recorded, then judged by explicit assertions
        outcome = {"outcome": "raised", "type": type(error).__name__, "message": str(error)}
    outcome["inputs_unchanged"] = (facts, inputs["versions"], inputs["bundle"]) == before
    return outcome


def old_label(obs):
    # Observed behaviour in the vocabulary of the fixture's old_behavior field.
    if obs["outcome"] == "findings":
        return "admitted"
    return obs["message"] if obs["type"] == "ValueError" else obs["type"]


def observe_cases(evidence, reconcile, fixture=None):
    """Observations of every frozen variant against a given reconcile module (baseline or candidate)."""
    fixture = fixture or load_fixture()
    inputs = build_inputs(evidence, fixture)
    return {case["id"]: [observe(reconcile, inputs, fixture, make_facts(fixture, inputs, v)) for v in case["variants"]]
            for case in fixture["cases"]}


class FactEventKeyTests(unittest.TestCase):
    def setUp(self):
        # Pure-API tests: scoped guards restore themselves, even after failure.
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.create_connection", "sqlite3.connect", "subprocess.Popen"):
            guard = mock.patch(target, side_effect=RuntimeError("offline_api_not_allowed"))
            guard.start()
            self.addCleanup(guard.stop)
        self.evidence, self.policy, self.reconcile = load_api()
        self.fixture = load_fixture()
        self.cases = {c["id"]: c for c in self.fixture["cases"]}
        self.assertEqual(tuple(self.cases), CASE_IDS)
        self.inputs = build_inputs(self.evidence, self.fixture)
        kwargs = compare_kwargs(self.fixture)
        self.selected = self.policy.requirements_for(self.inputs["versions"], **{
            k: kwargs[k] for k in ("agency_id", "jurisdiction", "regulated_role", "event_at")})

    def run_variant(self, variant):
        facts = make_facts(self.fixture, self.inputs, variant)
        obs = observe(self.reconcile, self.inputs, self.fixture, facts)
        self.assertTrue(obs["inputs_unchanged"], "input_mutation")
        return facts, obs

    def assert_refusal(self, obs, code):
        # Explicit typed check: an incidental TypeError or an admitted result fails here.
        label = code + "_refusal"
        self.assertEqual(obs["outcome"], "raised", label)
        self.assertEqual((obs["type"], obs["message"]), ("ValueError", code), label)
        self.assertNotIn("result", obs, label)

    def assert_findings(self, obs, variant, facts):
        self.assertEqual(obs["outcome"], "findings", "valid_key_admitted")
        result = obs["result"]
        self.assertEqual(result["status"], "derived_findings_needing_review")
        self.assertEqual(result["gaps"], [])
        self.assertFalse(result["review_acceptance"])
        self.assertEqual(result["legal_conclusions"], 0)
        findings = result["findings"]
        self.assertEqual([f["event_key"] for f in findings], variant["events"], "event_order")
        self.assertEqual(len({f["finding_id"] for f in findings}), len(findings))
        self.assertEqual(self.selected["status"], "selected")
        version = self.selected["selection"]["version"]
        requirement = self.selected["requirements"][0]
        scope = {k: v for k, v in compare_kwargs(self.fixture).items()}
        for finding in findings:
            key = finding["event_key"]
            # Exact supplied string, never stripped, stringified or rewritten.
            self.assertIs(type(key), str)
            group = [f for f in facts if f.get("event_key", "request-level") == key]
            self.assertTrue(group)
            self.assertTrue(all(type(f.get("event_key", "request-level")) is str for f in group))
            self.assertEqual(finding["fact_ids"], sorted(f["fact_id"] for f in group))
            self.assertEqual(finding["citations"], [self.inputs["fact_cite"]] * len(group))
            self.assertEqual(finding["classification"], "no_conflict_observed")
            self.assertEqual(finding["reason"], "only_supplied_fields_no_global_compliance_claim")
            self.assertEqual(finding["requirement_id"], self.fixture["version"]["requirement_id"])
            self.assertEqual(finding["version_id"], self.fixture["version"]["version_id"])
            self.assertEqual(finding["rule_selection"], "selected")
            self.assertEqual(finding["legal_assessment"], "NOT_ASSESSED")
            self.assertFalse(finding["review_acceptance"])
            self.assertEqual(finding["finding_identity_version"], "content-bound-v2")
            expected = self.reconcile.finding_identity(
                snapshot_sha256=self.inputs["bundle"]["snapshot_sha256"],
                scope={"agency_id": scope["agency_id"], "request_number": scope["request_number"],
                       "jurisdiction": scope["jurisdiction"], "regulated_role": scope["regulated_role"],
                       "event_at": scope["event_at"]},
                family_id=self.fixture["version"]["family_id"], rule_selection="selected", version=version,
                requirement=requirement, event_key=key, facts=group)
            self.assertEqual(finding["finding_id"], expected, "finding_identity")

    def check_case(self, case_id):
        observations = []
        for variant in self.cases[case_id]["variants"]:
            facts, obs = self.run_variant(variant)
            if variant["expect"] == "refusal":
                self.assert_refusal(obs, variant["error"])
            else:
                self.assert_findings(obs, variant, facts)
            observations.append(obs)
        return observations

    def test_fixture_pins_and_exact_citations(self):
        self.assertTrue(self.fixture["synthetic_only"])
        self.assertIs(type(self.fixture["fact_source"]["value"]), int)
        rule, fact = self.inputs["rule_cite"], self.inputs["fact_cite"]
        self.assertEqual(rule["quote"], self.fixture["version"]["text"])
        self.assertEqual(rule["original_sha256"], self.inputs["versions"][0]["source"]["content_sha256"])
        self.assertEqual(fact["quote"], "60")
        self.assertEqual(fact["json_pointer"], "/retention_days")
        for cite in (rule, fact):
            checked = self.evidence.validate_citation(cite, self.bundle_value())
            self.assertEqual(checked["structural_support"], "verified_exact_retained_bytes")
            self.assertFalse(checked["review_acceptance"])
        # Every malformed-key variant sits beside a valid current citation and selected policy.
        self.assertEqual(self.selected["status"], "selected")
        self.assertEqual(self.selected["selection"]["version"]["version_id"], self.fixture["version"]["version_id"])
        refusal_variants = [v for c in self.fixture["cases"] for v in c["variants"] if v["expect"] == "refusal"]
        self.assertTrue(all("old_behavior" in v for v in refusal_variants))

    def bundle_value(self):
        return self.inputs["bundle"]

    def test_negative_control_old_predicate_is_killed_and_restored(self):
        control = self.fixture["negative_control"]
        self.assertTrue(callable(getattr(self.reconcile, "valid_fact_event_key", None)), "predicate_present")
        baseline = {cid: self.check_case(cid) for cid in control["restore_cases"]}
        killed = self.cases[control["killed_case"]]
        injected = []
        # Replace only the new predicate, in memory, with the old accept-everything behaviour.
        with mock.patch.object(self.reconcile, "valid_fact_event_key", side_effect=lambda key: True) as old:
            for variant in killed["variants"]:
                _, obs = self.run_variant(variant)
                with self.assertRaisesRegex(AssertionError, control["killed_assertion"]):
                    self.assert_refusal(obs, variant["error"])
                # Containers fail incidentally with TypeError; scalars are silently admitted.
                self.assertEqual(old_label(obs), variant["old_behavior"], "old_behavior_reproduced")
                injected.append({"facts": variant["facts"], "observed": old_label(obs), "observation": obs})
            self.assertEqual(old.call_count, len(killed["variants"]))
            # Unrelated prerequisites remain valid while the predicate is replaced.
            during = {cid: self.check_case(cid) for cid in control["restore_cases"]}
        self.assertEqual(self.reconcile.valid_fact_event_key(["event-A"]), False)
        final = {cid: self.check_case(cid) for cid in (*control["restore_cases"], control["killed_case"])}
        for cid in control["restore_cases"]:
            self.assertEqual(final[cid], baseline[cid])
            self.assertEqual(during[cid], baseline[cid])
        print("FC10_CONTROL " + json.dumps({
            "injected_behavior": control["inject"], "killed_assertion": control["killed_assertion"],
            "pre_injection_candidate": baseline, "injected_observations": injected,
            "injected_prerequisites": during, "final_candidate_output": final}, sort_keys=True))


def _case_test(case_id):
    def test(self):
        self.check_case(case_id)
    test.__name__ = "test_case_" + case_id
    return test


for _case_id in CASE_IDS:
    setattr(FactEventKeyTests, "test_case_" + _case_id, _case_test(_case_id))


def script_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporary-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.temporary_root
    if not root.is_absolute() or any(p.is_symlink() for p in (root, *root.parents)):
        parser.error("absolute nonsymlink temporary root required")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    tempfile.tempdir = str(root)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(FactEventKeyTests))
    print("FC10_COVERAGE " + json.dumps({"case_ids": list(CASE_IDS), "tests_run": result.testsRun,
                                          "skipped": len(result.skipped), "successful": result.wasSuccessful()},
                                         sort_keys=True))
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(script_main())
