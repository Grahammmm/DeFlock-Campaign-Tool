"""FC-FINDING-IDENTITY: reconciliation finding IDs bind evidence and policy.

Every fixture is invented for these tests. The agency, request numbers, policy
text and hashes do not describe any real record, person or source; hashes are
derived from labelled synthetic bytes. Passing is machine validity only and
remains REVIEW REQUIRED, never a semantic or legal acceptance.
"""
import copy
import hashlib
import unittest

from campaign_tool.records import grounded_evidence
from campaign_tool.records.understanding import evidence, reconcile

AGENCY = "example-agency"
REQUEST = "R-0001"
IDENTITY_VERSION = "content-bound-v2"
REQ_TEXT = "audit log entries for 30 days"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def page_one(days=30):
    return ("Public records request R-0001\n"
            "Agency: Example Agency\n"
            "Date: 2030-01-15\n"
            "Policy 100.1 requires retention of audit log entries for 30 days.\n"
            "Observed retention noted as {} days.".format(days))


def receipt(label=b"synthetic original document for finding identity tests", system="synthetic"):
    original = digest(label)
    return original, {
        "schema": "format-extraction-v1",
        "original_sha256": original,
        "original_changed": False,
        "status": "complete",
        "parser": "synthetic",
        "parser_version": "0",
        "route": {"format": "pdf"},
        "pages": [{"page_no": 1, "status": "ok"}, {"page_no": 2, "status": "ok"}],
        "units": [
            {"kind": "text", "locator": {"page": 1}, "text": page_one(), "data": {}},
            {"kind": "text", "locator": {"page": 2},
             "text": '{"retention_days": 30, "backup_days": 45, "system": "' + system + '"}', "data": {}},
        ],
    }


def make_bundle(**kwargs):
    original, native = receipt(**kwargs)
    raw = evidence.canonical(native)
    return evidence.normalize_receipt(native, original, digest(raw))


def unit(bundle, page):
    return next(u for u in bundle["units"] if u["locator"] == {"page": page})


def versions_for(bundle, *, version_id="v1", expected=30, field="retention_days", operator="at_most",
                 content_sha256=None, locator="section 100.1"):
    rule_cite = evidence.cite_text(unit(bundle, 1), REQ_TEXT)
    test = {"operator": operator, "expected": expected}
    if field is not None:
        test["field"] = field
    return [{
        "version_id": version_id,
        "family_id": "retention",
        "jurisdiction": "example-state",
        "agency_id": "*",
        "regulated_roles": ["operator"],
        "effective_from": "2029-01-01",
        "issued_at": "2028-12-01",
        "retrieved_at": "2031-01-01",
        "supersession_status": "not_superseded_as_of_retrieval",
        "source": {
            "source_id": "synthetic-policy",
            "url": "https://example.invalid/policy",
            "locator": locator,
            "verification_status": "verified",
            "content_sha256": content_sha256 or rule_cite["original_sha256"],
        },
        "requirements": [{"requirement_id": "r1", "text": REQ_TEXT, "citations": [rule_cite], "test": test}],
    }]


def scope(**overrides):
    value = {"agency_id": AGENCY, "jurisdiction": "example-state", "regulated_role": "operator",
             "event_at": "2030-01-15", "request_number": REQUEST}
    value.update(overrides)
    return value


def pointer(bundle, path="/retention_days"):
    return grounded_evidence.pointer_support(unit(bundle, 2), path)


def text_cite(bundle, quote="Observed retention noted as 30 days"):
    return evidence.cite_text(unit(bundle, 1), quote)


def fact(bundle, fact_id="f1", value=30, cites=None, **extra):
    row = {"fact_id": fact_id, "field": "retention_days", "value": value,
           "citations": cites if cites is not None else [pointer(bundle)]}
    row.update(extra)
    return row


def run(facts, versions, bundle, **scope_overrides):
    return reconcile.compare(facts, versions, bundle=bundle, **scope(**scope_overrides))


def only(result):
    findings = result["findings"]
    if len(findings) != 1:
        raise AssertionError("expected exactly one finding, got {}".format(len(findings)))
    return findings[0]


def scenarios():
    """Named synthetic scenarios shared with the acceptance runner's baseline differential.

    Each entry maps a name to (facts, versions, bundle, scope kwargs) and the
    classification the pinned baseline produces for its single finding.
    """
    b = make_bundle()
    other = make_bundle(label=b"second synthetic original for finding identity tests", system="synthetic-b")
    v = versions_for(b)
    two = [fact(b, "f1"), fact(b, "f2")]
    multi = [pointer(b), text_cite(b)]
    return {
        "baseline_value_30": ([fact(b)], v, b, scope(), "no_conflict_observed"),
        "value_45": ([fact(b, value=45)], v, b, scope(), "potential_conflict"),
        "two_facts": (two, v, b, scope(), "no_conflict_observed"),
        "two_facts_reversed": (list(reversed(two)), v, b, scope(), "no_conflict_observed"),
        "two_citations": ([fact(b, cites=multi)], v, b, scope(), "no_conflict_observed"),
        "two_citations_reversed": ([fact(b, cites=list(reversed(multi)))], v, b, scope(), "no_conflict_observed"),
        "other_snapshot": ([fact(other, cites=[pointer(other)])], versions_for(other), other, scope(),
                           "no_conflict_observed"),
        "other_source_support": ([fact(b, cites=[text_cite(b)])], v, b, scope(), "no_conflict_observed"),
        "policy_version_id": ([fact(b)], versions_for(b, version_id="v1-amended"), b, scope(),
                              "no_conflict_observed"),
        "policy_test_expected": ([fact(b)], versions_for(b, expected=60), b, scope(), "no_conflict_observed"),
        "policy_source_locator": ([fact(b)], versions_for(b, locator="section 100.1(a)"), b, scope(),
                                  "no_conflict_observed"),
        "event_key_e1": ([fact(b, event_key="e1")], v, b, scope(), "no_conflict_observed"),
        "event_key_e2": ([fact(b, event_key="e2")], v, b, scope(), "no_conflict_observed"),
        "scope_event_at": ([fact(b)], v, b, scope(event_at="2030-06-30"), "no_conflict_observed"),
        "scope_request_none": ([fact(b, request_number=None)], v, b, scope(request_number=None),
                               "no_conflict_observed"),
        "counterevidence": ([fact(b, counterevidence=True)], v, b, scope(), "contradictory_evidence"),
        "exception": ([fact(b, exception_applies=True)], v, b, scope(), "exception_applies"),
        "missing_na": ([fact(b, value="n/a")], v, b, scope(), "missing_evidence"),
        "missing_no_facts": ([], v, b, scope(), "missing_evidence"),
        "not_in_force": ([fact(b)], v, b, scope(event_at="2028-06-01"), "applicability_unresolved"),
        "not_in_force_45": ([fact(b, value=45)], v, b, scope(event_at="2028-06-01"), "applicability_unresolved"),
        "rule_unbound": ([fact(b)], versions_for(b, content_sha256=digest(b"unrelated synthetic policy bytes")),
                         b, scope(), "applicability_unresolved"),
        "field_unmapped": ([fact(b)], versions_for(b, field=None), b, scope(), "not_assessed"),
    }


def run_scenario(name, compare=None):
    facts, versions, bundle, kwargs, _expected = scenarios()[name]
    return (compare or reconcile.compare)(facts, versions, bundle=bundle, **kwargs)


def finding_id(name):
    return only(run_scenario(name))["finding_id"]


class IdentityContractTests(unittest.TestCase):
    """Versioned canonical preimage and its stability under equivalent input."""

    def test_repeated_identical_run_is_stable_and_versioned(self):
        first = only(run_scenario("baseline_value_30"))
        second = only(run_scenario("baseline_value_30"))
        self.assertEqual(first["finding_id"], second["finding_id"])
        self.assertTrue(evidence.is_hash(first["finding_id"]))
        self.assertEqual(first["finding_identity_version"], IDENTITY_VERSION)
        self.assertEqual(first, second)

    def test_old_fact_id_only_identity_is_not_reused_as_v2(self):
        found = only(run_scenario("baseline_value_30"))
        old = evidence.sha([AGENCY, REQUEST, "retention", "r1", "request-level", ["f1"], "selected"])
        self.assertNotEqual(found["finding_id"], old)

    def test_fact_order_permutation_keeps_identity_and_rendering(self):
        forward = only(run_scenario("two_facts"))
        backward = only(run_scenario("two_facts_reversed"))
        self.assertEqual(forward["finding_id"], backward["finding_id"])
        self.assertEqual(forward["fact_ids"], ["f1", "f2"])
        self.assertEqual(forward, backward)

    def test_citation_order_permutation_keeps_identity_and_rendering(self):
        forward = only(run_scenario("two_citations"))
        backward = only(run_scenario("two_citations_reversed"))
        self.assertEqual(forward["finding_id"], backward["finding_id"])
        self.assertEqual(forward["citations"], backward["citations"])
        self.assertEqual(forward, backward)
        keys = [evidence.canonical(c) for c in forward["citations"]]
        self.assertEqual(keys, sorted(keys))

    def test_inputs_are_not_mutated(self):
        facts, versions, bundle, kwargs, _ = scenarios()["two_citations_reversed"]
        facts = facts + [fact(bundle, "f0")]
        before = copy.deepcopy((facts, versions, bundle))
        reconcile.compare(facts, versions, bundle=bundle, **kwargs)
        self.assertEqual((facts, versions, bundle), before)


class EvidenceBindingTests(unittest.TestCase):
    """Changing facts, support or flags must change the identity."""

    def test_same_fact_id_different_cited_value_changes_identity(self):
        self.assertNotEqual(finding_id("baseline_value_30"), finding_id("value_45"))

    def test_changed_snapshot_changes_identity(self):
        self.assertNotEqual(finding_id("baseline_value_30"), finding_id("other_snapshot"))

    def test_changed_source_support_changes_identity(self):
        self.assertNotEqual(finding_id("baseline_value_30"), finding_id("other_source_support"))

    def test_counterevidence_flag_changes_identity(self):
        self.assertNotEqual(finding_id("baseline_value_30"), finding_id("counterevidence"))

    def test_exception_flag_changes_identity(self):
        self.assertNotEqual(finding_id("baseline_value_30"), finding_id("exception"))

    def test_missing_evidence_has_distinct_bound_identity(self):
        na = only(run_scenario("missing_na"))
        empty = only(run_scenario("missing_no_facts"))
        self.assertEqual(na["finding_identity_version"], IDENTITY_VERSION)
        self.assertEqual(empty["finding_identity_version"], IDENTITY_VERSION)
        ids = {finding_id("baseline_value_30"), na["finding_id"], empty["finding_id"]}
        self.assertEqual(len(ids), 3)
        self.assertEqual(only(run_scenario("missing_no_facts"))["finding_id"], empty["finding_id"])


class PolicyAndScopeBindingTests(unittest.TestCase):
    """Governing version, requirement test, source and scope are bound."""

    def test_policy_version_id_changes_identity(self):
        self.assertNotEqual(finding_id("baseline_value_30"), finding_id("policy_version_id"))

    def test_policy_test_change_changes_identity(self):
        self.assertNotEqual(finding_id("baseline_value_30"), finding_id("policy_test_expected"))

    def test_policy_source_pointer_changes_identity(self):
        self.assertNotEqual(finding_id("baseline_value_30"), finding_id("policy_source_locator"))

    def test_event_key_changes_identity(self):
        self.assertNotEqual(finding_id("event_key_e1"), finding_id("event_key_e2"))

    def test_scope_changes_identity(self):
        base = finding_id("baseline_value_30")
        self.assertNotEqual(base, finding_id("scope_event_at"))
        self.assertNotEqual(base, finding_id("scope_request_none"))

    def test_invalid_applicability_identity_still_binds_evidence(self):
        unresolved = only(run_scenario("not_in_force"))
        self.assertEqual(unresolved["finding_identity_version"], IDENTITY_VERSION)
        self.assertNotEqual(unresolved["finding_id"], finding_id("baseline_value_30"))
        self.assertNotEqual(unresolved["finding_id"], finding_id("not_in_force_45"))
        self.assertNotEqual(finding_id("rule_unbound"), finding_id("baseline_value_30"))


def governed_versions(bundle, *, exception_cites=None, term_cites=None):
    """Selected version carrying cited policy exceptions and incorporated terms."""
    versions = versions_for(bundle)
    first = evidence.cite_text(unit(bundle, 1), "Policy 100.1")
    second = evidence.cite_text(unit(bundle, 1), "Agency: Example Agency")
    versions[0]["exceptions"] = [
        {"exception_id": "x1", "text": "synthetic exception text",
         "citations": exception_cites if exception_cites is not None else [first, second]},
        {"exception_id": "x2", "text": "second synthetic exception", "citations": [second]},
    ]
    versions[0]["incorporated_terms"] = [
        {"term_id": "t1", "text": "synthetic incorporated term",
         "citations": term_cites if term_cites is not None else [second, first]},
    ]
    return versions, first, second


class GoverningCitationOrderTests(unittest.TestCase):
    """Citation order inside the selected version's exceptions and terms is not identity."""

    def setUp(self):
        self.bundle = make_bundle()
        self.facts = [fact(self.bundle)]

    def finding(self, versions):
        return only(run(self.facts, versions, self.bundle))

    def test_exception_citation_permutation_keeps_identity_and_rendering(self):
        versions, first, second = governed_versions(self.bundle)
        forward = self.finding(versions)
        reordered, _, _ = governed_versions(self.bundle, exception_cites=[second, first])
        backward = self.finding(reordered)
        self.assertEqual(forward["classification"], "no_conflict_observed")
        self.assertEqual(forward["finding_id"], backward["finding_id"])
        self.assertEqual(forward, backward)

    def test_incorporated_term_citation_permutation_keeps_identity_and_rendering(self):
        versions, first, second = governed_versions(self.bundle)
        forward = self.finding(versions)
        reordered, _, _ = governed_versions(self.bundle, term_cites=[first, second])
        backward = self.finding(reordered)
        self.assertEqual(forward["classification"], "no_conflict_observed")
        self.assertEqual(forward["finding_id"], backward["finding_id"])
        self.assertEqual(forward, backward)

    def test_governing_inputs_are_not_mutated(self):
        versions, first, second = governed_versions(self.bundle, exception_cites=None, term_cites=None)
        versions[0]["exceptions"][0]["citations"] = [second, first]
        before = copy.deepcopy((self.facts, versions, self.bundle))
        self.finding(versions)
        self.assertEqual((self.facts, versions, self.bundle), before)
        self.assertEqual(versions[0]["exceptions"][0]["citations"], [second, first])

    def test_changed_exception_citation_content_changes_identity(self):
        versions, first, _second = governed_versions(self.bundle)
        other = evidence.cite_text(unit(self.bundle, 1), "Public records request R-0001")
        changed, _, _ = governed_versions(self.bundle, exception_cites=[first, other])
        base = self.finding(versions)
        moved = self.finding(changed)
        self.assertNotEqual(base["finding_id"], moved["finding_id"])
        self.assertEqual(base["classification"], moved["classification"])

    def test_changed_incorporated_term_citation_content_changes_identity(self):
        versions, first, _second = governed_versions(self.bundle)
        changed, _, _ = governed_versions(self.bundle, term_cites=[first])
        self.assertNotEqual(self.finding(versions)["finding_id"], self.finding(changed)["finding_id"])

    def test_exception_item_order_remains_bound(self):
        versions, _first, _second = governed_versions(self.bundle)
        swapped = copy.deepcopy(versions)
        swapped[0]["exceptions"].reverse()
        self.assertNotEqual(self.finding(versions)["finding_id"], self.finding(swapped)["finding_id"])


class ClassificationPreservedTests(unittest.TestCase):
    """The patch changes identity/order only, never factual or legal decisions."""

    def test_every_scenario_keeps_baseline_classification(self):
        for name, (_f, _v, _b, _s, expected) in scenarios().items():
            with self.subTest(scenario=name):
                result = run_scenario(name)
                found = only(result)
                self.assertEqual(found["classification"], expected)
                self.assertEqual(found["legal_assessment"], "NOT_ASSESSED")
                self.assertFalse(found["review_acceptance"])
                self.assertTrue(found["derived"])
                self.assertEqual(result["legal_conclusions"], 0)
                self.assertFalse(result["review_acceptance"])

    def test_identity_uses_one_helper(self):
        self.assertTrue(callable(getattr(reconcile, "finding_identity", None)))
        self.assertEqual(getattr(reconcile, "FINDING_IDENTITY_VERSION", None), IDENTITY_VERSION)


if __name__ == "__main__":
    unittest.main()
