"""Synthetic regressions: unresolved policy dates never become a selected governing version.

Fictional versions only; no network, model, SQLite or send path is touched.
"""
from copy import deepcopy
import unittest

from campaign_tool.records import grounded_evidence
from campaign_tool.records.understanding import cli, correspondence, evidence, policy, reconcile
from tests.records.test_understanding import AGENCY, make_bundle, policy_versions

SCOPE = dict(agency_id=AGENCY, jurisdiction="example-state", regulated_role="operator",
             event_at="2030-01-15")
TEXT = "audit log entries for 30 days"


def requirement(bundle, rid="fictional-retention"):
    return {"requirement_id": rid, "text": TEXT,
            "citations": [evidence.cite_text(bundle["units"][0], TEXT)],
            "test": {"field": "retention_days", "operator": "at_most", "expected": 30}}


def version(bundle, vid="v1", **over):
    row = policy_versions(requirements=[requirement(bundle)])[0]
    row["version_id"] = vid
    row.update(over)
    return row


def ids(selection):
    return [g.get("version_ids") for g in selection["gaps"] if g["code"] == "applicability_date_unknown"]


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.bundle = make_bundle()

    def select(self, versions, **over):
        return policy.select_version(versions, **{**SCOPE, **over})

    def assert_hold(self, sel, candidates, unresolved):
        self.assertEqual(sel["status"], "effective_date_unknown")
        self.assertIsNone(sel["version"])
        self.assertNotIn("source_pointer", sel)
        self.assertEqual(sel["candidates"], candidates)
        self.assertEqual(ids(sel), [unresolved])

    def test_open_version_requires_retrieval_attestation(self):
        b = self.bundle
        missing = version(b)
        missing.pop("retrieved_at")
        for row in (missing, version(b, retrieved_at=None)):
            sel = self.select([row], event_at="9999-12-31")
            self.assert_hold(sel, ["v1"], ["v1"])
            self.assertEqual(self.select([row])["status"], "effective_date_unknown")
        for status in (None, "unknown"):
            row = version(b, supersession_status=status)
            self.assert_hold(self.select([row]), ["v1"], ["v1"])
        # Exactly on retrieval remains eligible; the next day is not extrapolated.
        self.assertEqual(self.select([version(b, retrieved_at="2030-01-15")])["status"], "selected")
        self.assert_hold(self.select([version(b, retrieved_at="2030-01-14")]), ["v1"], ["v1"])
        self.assertEqual(self.select([version(b)], event_at="2030-01-15")["status"], "selected")
        # Malformed retrieval stays an invalid collection; a real retrieval date repairs the hold.
        bad = self.select([version(b, retrieved_at="2030-13-45")])
        self.assertEqual(bad["status"], "invalid_versions")
        self.assertIsNone(bad["version"])
        repaired = deepcopy(missing)
        repaired["retrieved_at"] = "2031-01-01"
        self.assertEqual(self.select([repaired])["status"], "selected")

    def test_undated_competitor_blocks_dated_selection(self):
        b = self.bundle
        known = version(b, "v1")
        undated = version(b, "v2-undated", effective_from=None)
        for order in ([known, undated], [undated, known]):
            self.assert_hold(self.select(order), ["v1", "v2-undated"], ["v2-undated"])
        # Wildcard and specific agency rows intersect; both orders hold.
        specific = version(b, "v3-specific", effective_from=None, agency_id=AGENCY)
        for order in ([known, specific], [specific, known]):
            self.assert_hold(self.select(order), ["v1", "v3-specific"], ["v3-specific"])
        # Removing or fully bounding the contender restores selection.
        self.assertEqual(self.select([known])["status"], "selected")
        bounded = version(b, "v2-undated", effective_from="2028-06-01", effective_to="2028-07-01")
        self.assertEqual(self.select([known, bounded])["version"]["version_id"], "v1")
        # Genuine overlap between dated versions is still rejected by validation.
        both = [version(b, "a", effective_from="2029-01-01"),
                version(b, "b", effective_from="2029-02-01", effective_to="2031-01-01")]
        self.assertEqual(self.select(both)["status"], "invalid_versions")
        self.assertEqual(self.select(both + [undated])["status"], "invalid_versions")

    def test_proven_outside_does_not_block(self):
        b = self.bundle
        known = version(b, "v1")
        before_event = version(b, "old", effective_from=None, effective_to="2029-01-01")
        on_event_end = version(b, "ended", effective_from=None, effective_to="2030-01-15")
        superseded = version(b, "sup", effective_from=None, superseded_at="2030-01-15")
        future = version(b, "future", effective_from="2030-06-01", supersession_status=None)
        known_to_june = version(b, "v1", effective_to="2030-06-01")
        for other in (before_event, on_event_end, superseded, future):
            sel = self.select([other, known_to_june])
            self.assertEqual(sel["status"], "selected", other["version_id"])
            self.assertEqual(sel["version"]["version_id"], "v1")
        wrong = [version(b, "agency", effective_from=None, agency_id="other-agency"),
                 version(b, "juris", effective_from=None, jurisdiction="other-state"),
                 version(b, "role", effective_from=None, regulated_roles=["auditor"])]
        for other in wrong:
            self.assertEqual(self.select([known, other])["status"], "selected", other["version_id"])
        # Unknown start with a known end after the event is still unresolved.
        later_end = version(b, "later", effective_from=None, effective_to="2030-02-01")
        self.assert_hold(self.select([known, later_end]), ["later", "v1"], ["later"])
        # Closed intervals need no retrieval date and keep half-open endpoints.
        closed = version(b, "c", effective_from="2030-01-15", effective_to="2030-01-16")
        closed.pop("retrieved_at")
        closed["supersession_status"] = None
        self.assertEqual(self.select([closed])["status"], "selected")  # exact start
        self.assertEqual(self.select([closed], event_at="2030-01-14")["status"], "not_in_force")
        self.assertEqual(self.select([closed], event_at="2030-01-16")["status"], "not_in_force")
        sup = version(b, "s", effective_from="2030-01-01", superseded_at="2030-01-16")
        self.assertEqual(self.select([sup], event_at="2030-01-15")["status"], "selected")
        self.assertEqual(self.select([sup], event_at="2030-01-16")["status"], "not_in_force")
        # Everything definitely outside keeps not_in_force.
        sel = self.select([before_event, future])
        self.assertEqual(sel["status"], "not_in_force")
        self.assertEqual(sel["candidates"], ["old", "future"])

    def hold_inputs(self):
        b = self.bundle
        facts = [{"fact_id": "fictional-fact", "field": "retention_days", "value": 30,
                  "citations": [grounded_evidence.pointer_support(b["units"][1], "/retention_days")]}]
        other = version(b, "o1", family_id="other-family", effective_to="2031-01-01")
        other["requirements"] = [requirement(b, "other-retention")]
        known = version(b, "v1")
        undated = version(b, "v2-undated", effective_from=None)
        return facts, known, undated, other

    def test_temporal_hold_reaches_real_consumers(self):
        b = self.bundle
        facts, known, undated, other = self.hold_inputs()
        # Fully dated positive case.
        done = reconcile.compare(facts, [known], bundle=b, **SCOPE)
        self.assertEqual([f["classification"] for f in done["findings"]], ["no_conflict_observed"])
        versions = [known, undated, other]
        req = policy.requirements_for(versions[:2], **SCOPE)
        self.assertEqual(req["status"], "effective_date_unknown")
        self.assertEqual((req["requirements"], req["exceptions"], req["incorporated_terms"]), ([], [], []))
        self.assertFalse(req["legal_conclusion"])
        result = reconcile.compare(facts, versions, bundle=b, **SCOPE)
        by_family = {f["family_id"]: f for f in result["findings"]}
        held = by_family["retention"]
        self.assertEqual(held["classification"], "applicability_unresolved")
        self.assertEqual(held["rule_selection"], "effective_date_unknown")
        self.assertEqual(held["legal_assessment"], "NOT_ASSESSED")
        self.assertFalse(held["review_acceptance"])
        self.assertEqual(held["fact_ids"], ["fictional-fact"])
        self.assertTrue(held["citations"] and held["rule_source"])
        self.assertEqual(by_family["other-family"]["classification"], "no_conflict_observed")
        self.assertEqual(len(result["findings"]), 3)  # v1, v2 rows of the held family + other family
        self.assertEqual({f["classification"] for f in result["findings"] if f["family_id"] == "retention"},
                         {"applicability_unresolved"})
        # Open version without retrieval attestation is held in the same way.
        no_retrieval = version(b, "v1")
        no_retrieval.pop("retrieved_at")
        found = reconcile.compare(facts, [no_retrieval], bundle=b, **{**SCOPE, "event_at": "9999-12-31"})
        self.assertEqual([f["classification"] for f in found["findings"]], ["applicability_unresolved"])

    def rule_and_tracker(self, versions):
        b = self.bundle
        promise = "SYN-81: We will provide the fictional audit records."
        receipt = {"schema": "format-extraction-v1", "original_sha256": evidence.sha(b"FICTIONAL RF0814 ORIGINAL"),
                   "original_changed": False, "pages": [], "status": "extracted",
                   "units": [{"kind": "text", "locator": {"part": ["synthetic", 1]}, "text": promise,
                              "data": {"headers": []}}]}
        bundle = evidence.normalize_receipt(receipt, receipt["original_sha256"], evidence.sha(receipt))
        citation = evidence.cite_text(bundle["units"][0], promise)
        event = {"agency_id": "fictional-office", "request_number": "SYN-81", "event_type": "promise",
                 "occurred_at": "2030-01-14T09:00:00Z", "due_at": "2030-02-01T09:00:00Z",
                 "due_kind": "promised_delivery", "citations": [citation]}
        rule = {"agency_id": "fictional-office", "request_number": "SYN-81", "jurisdiction": "example-state",
                "regulated_role": "operator", "event_at": "2030-01-15", "version_id": "v1",
                "requirement_id": "fictional-retention", "basis": "Fictional deadline basis.",
                "determination_due_at": "2030-01-20T09:00:00Z", "delivery_due_at": "2030-02-10T09:00:00Z",
                "citations": [deepcopy(citation)], "policy_versions": versions}
        return event, rule, bundle

    def test_policy_deadline_and_cli_respect_temporal_hold(self):
        b = self.bundle
        known = version(b, "v1")
        undated = version(b, "v2-undated", effective_from=None)
        for versions, expected in (([known], {"determination", "delivery"}), ([known, undated], set())):
            event, rule, bundle = self.rule_and_tracker(versions)
            out = correspondence.track([event], as_of="2030-01-16T12:00:00Z", deadline_rules=[rule], bundle=bundle)
            actions = {a["deadline_kind"] for a in out["due_actions"]
                       if a.get("source") == "explicit_validated_policy_version"}
            self.assertEqual(actions, expected)
            reasons = sorted(h["reason"] for h in out["holds"] if "deadline" in h["reason"])
            if expected:
                self.assertEqual(reasons, [])
            else:
                self.assertEqual(reasons, ["delivery_deadline_policy_version_unresolved",
                                           "determination_deadline_policy_version_unresolved"])
        for order in ([known, undated], [undated, known]):
            out = cli.execute("policy", {"versions": order, "scope": dict(SCOPE)})
            self.assertTrue(out["validation"]["valid"])
            self.assertEqual(out["requirements"]["status"], "effective_date_unknown")
            self.assertIsNone(out["requirements"]["selection"]["version"])
            self.assertEqual(out["requirements"]["requirements"], [])

    def test_temporal_selection_is_repeatable_and_nonmutating(self):
        b = self.bundle
        facts, known, undated, other = self.hold_inputs()
        versions = [known, undated, other]
        before = deepcopy((versions, facts, b))
        first = reconcile.compare(facts, versions, bundle=b, **SCOPE)
        again = reconcile.compare(facts, versions, bundle=b, **SCOPE)
        flipped = reconcile.compare(facts, list(reversed(versions)), bundle=b, **SCOPE)
        self.assertEqual(first, again)
        self.assertEqual(sorted(f["finding_id"] for f in first["findings"]),
                         sorted(f["finding_id"] for f in flipped["findings"]))
        self.assertEqual(before, (versions, facts, b))
        a = policy.select_version([known, undated], **SCOPE)
        z = policy.select_version([undated, known], **SCOPE)
        self.assertEqual((a["candidates"], a["gaps"]), (z["candidates"], z["gaps"]))
        # No dates are fabricated onto the inputs.
        self.assertIsNone(undated["effective_from"])
        # A selected version with no transcribed requirements keeps its existing result.
        empty = version(b)
        empty["requirements"] = []
        res = policy.requirements_for([empty], **SCOPE)
        self.assertEqual(res["status"], "selected")
        self.assertEqual([g["code"] for g in res["gaps"]], ["requirements_not_transcribed"])


if __name__ == "__main__":
    unittest.main()
