"""Synthetic context/version regressions; no files, models, network or corpus."""
import copy
import unittest

from campaign_tool.records import context_revision as cr


ORIGINAL = "a" * 64
EVIDENCE = [{"source_sha256": ORIGINAL, "locator": "page 2, row 3", "occurrence_id": "occurrence-1"}]


def rule(start="2016-01-01", end=None, **changes):
    return {"rule_id": "xx-synthetic", "effective_from": start, "effective_to": end,
            "duty": "Synthetic duty text", "actor": "synthetic operator", "exceptions": [],
            "sources": [{"url": "https://authority.example.invalid/synthetic", "sha256": "b" * 64}],
            "review": "verified", **changes}


def period(start="2020-01-01", end=None, event_kind="search"):
    return {"start": start, "end": end, "event_kind": event_kind, "evidence": copy.deepcopy(EVIDENCE)}


def context(dates=None, **evidence):
    return cr.event_context({"occurrence_ids": ["occurrence-1"], "request_ids": ["request-1"],
                             "agency_ids": ["agency-1"], **evidence},
                            {"conduct": [period()]} if dates is None else dates)


def basis(r, c, role="conduct", event_kind="search"):
    return {"rule_revision_sha256": cr.rule_revision(r), "context_sha256": cr.fingerprint(c),
            "event_role": role, "event_kind": event_kind, "version_verified": True,
            "version_evidence": copy.deepcopy(EVIDENCE), "scope": "applies",
            "scope_checks": {key: {"status": "applies", "evidence": copy.deepcopy(EVIDENCE)}
                             for key in ("jurisdiction", "regulated_role", "policy_adoption")},
            "rationale": "Synthetic sourced applicability assertion; requires independent review."}


def revision(**changes):
    r, c = rule(), context()
    values = {"subject_sha256": ORIGINAL, "extraction": {"receipt_sha256": "c" * 64,
              "unit_sha256s": ["d" * 64], "coverage": {"covered": 1, "total": 1}},
              "context": c, "rules": [r], "versions": {key: key + "-v1" for key in cr.VERSION_KEYS},
              "applicability_bases": [basis(r, c)], "rule_context": {"status": "reviewed"}}
    values.update(changes)
    return cr.build_revision(**values)


class RevisionTests(unittest.TestCase):
    def test_identical_inputs_reuse_all_stages(self):
        a, b = revision(), revision()
        self.assertEqual(a, b)
        self.assertEqual(cr.invalidation_plan(a, b)["stages"], [])
        self.assertFalse(cr.invalidation_plan(a, b)["revoke_approval"])

    def test_same_id_changed_rule_contents_reopens_detect_review_and_downstream(self):
        for field, value in (("duty", "Amended duty"), ("exceptions", ["New exception"]),
                             ("effective_from", "2017-01-01"), ("sources", []), ("review", "draft")):
            with self.subTest(field=field):
                new_rule = rule(**{field: value})
                plan = cr.invalidation_plan(revision(), revision(rules=[new_rule]))
                self.assertEqual(plan["stages"], ["detect", "review", "compare", "privacy"])
                self.assertTrue(plan["revoke_approval"])
                self.assertTrue(plan["retain_history"])

    def test_same_original_different_occurrence_request_agency_reuses_extraction_only(self):
        for key in ("occurrence_ids", "request_ids", "agency_ids"):
            with self.subTest(key=key):
                other = context(**{key: ["different-context"]})
                plan = cr.invalidation_plan(revision(), revision(context=other))
                self.assertEqual(plan["stages"], ["catalog", "detect", "review", "compare", "privacy"])

    def test_corrected_extraction_receipt_units_or_coverage_invalidates_all_analysis(self):
        for extraction in ({"receipt_sha256": "e" * 64},
                           {"receipt_sha256": "c" * 64, "unit_sha256s": ["f" * 64]},
                           {"receipt_sha256": "c" * 64, "coverage": {"covered": 1, "total": 2}}):
            self.assertEqual(cr.invalidation_plan(revision(), revision(extraction=extraction))["stages"], list(cr.STAGES))

    def test_date_kind_source_and_value_changes_invalidate_catalog_and_analysis(self):
        for edit in ({"start": "2019-01-01"}, {"event_kind": "implementation"},
                     {"evidence": [{"source_sha256": "f" * 64, "locator": "row 4"}]}):
            p = period()
            p.update(edit)
            new = context({"conduct": [p]})
            self.assertEqual(cr.invalidation_plan(revision(), revision(context=new))["stages"],
                             ["catalog", "detect", "review", "compare", "privacy"])

    def test_each_analytical_version_invalidates_its_consumers(self):
        starts = {"extraction": "extract", "catalog": "catalog", "context": "catalog", "detector": "detect",
                  "applicability": "compare", "privacy": "privacy"}
        for key in cr.VERSION_KEYS:
            with self.subTest(key=key):
                versions = {k: k + "-v1" for k in cr.VERSION_KEYS}
                versions[key] = "changed"
                expected = list(cr.STAGES[cr.STAGES.index(starts.get(key, "review")):])
                self.assertEqual(cr.invalidation_plan(revision(), revision(versions=versions))["stages"], expected)

    def test_applicability_correction_targets_compare_privacy(self):
        r, c = rule(), context()
        b = basis(r, c)
        b["scope"] = "does_not_apply"
        b["scope_checks"]["regulated_role"]["status"] = "does_not_apply"
        self.assertEqual(cr.invalidation_plan(revision(), revision(applicability_bases=[b]))["stages"],
                         ["compare", "privacy"])

    def test_package_status_and_rule_order_are_dependencies(self):
        self.assertEqual(cr.invalidation_plan(revision(), revision(rule_context={"status": "draft"}))["stages"][0], "detect")
        a, b = rule(), rule(rule_id="xx-second")
        self.assertNotEqual(revision(rules=[a, b]), revision(rules=[b, a]))

    def test_legacy_or_corrupt_revision_is_not_reusable(self):
        bad = revision()
        bad["stage_hashes"]["review"] = "0" * 64
        for previous in (None, {}, bad):
            self.assertEqual(cr.invalidation_plan(previous, revision())["stages"], list(cr.STAGES))
        with self.assertRaises(ValueError):
            cr.invalidation_plan(revision(), bad)

    def test_cannot_compare_distinct_originals_or_omit_versions(self):
        with self.assertRaisesRegex(ValueError, "subject_mismatch"):
            cr.invalidation_plan(revision(), revision(subject_sha256="f" * 64))
        with self.assertRaisesRegex(ValueError, "versions_required"):
            revision(versions={"model": "alias"})

    def test_rehashed_but_inconsistent_stored_manifest_is_not_reusable(self):
        for mutation in (lambda m: m["dependencies"].update(rules="f" * 64),
                         lambda m: m.update(dependencies={"subject_sha256": ORIGINAL}),
                         lambda m: m.update(stage_hashes=list(cr.STAGES))):
            bad = revision()
            mutation(bad)
            bad["revision_sha256"] = cr.fingerprint({k: v for k, v in bad.items() if k != "revision_sha256"})
            self.assertEqual(cr.invalidation_plan(bad, revision())["stages"], list(cr.STAGES))
            with self.assertRaisesRegex(ValueError, "current_revision_invalid"):
                cr.invalidation_plan(revision(), bad)

    def test_snapshot_does_not_alias_input_and_canonical_keys_are_strict(self):
        source = {"occurrence_ids": ["one"]}
        c = cr.event_context(source)
        source["occurrence_ids"].append("two")
        self.assertEqual(c["evidence_context"]["occurrence_ids"], ["one"])
        for value in ({1: "ambiguous"}, {"number": float("nan")}, {"tuple": (1, 2)}):
            with self.assertRaises(ValueError):
                cr.fingerprint(value)


class ApplicabilityTests(unittest.TestCase):
    def resolve(self, r=None, c=None, b=None):
        r, c = rule() if r is None else r, context() if c is None else c
        return cr.resolve_applicability(r["rule_id"], [r], c, [basis(r, c) if b is None else b])

    def test_known_conduct_is_still_legal_review_not_compliance(self):
        decision = self.resolve()
        self.assertEqual(decision["classification"], "LEGAL_REVIEW_REQUIRED")
        self.assertEqual(decision["event_period"]["start"], "2020-01-01")

    def test_malformed_context_or_rules_cannot_drop_binding_fields(self):
        c = context()
        for bad in (None, {**c, "schema": "wrong"}, {**c, "unsupported_field": "lost"}):
            with self.assertRaises(ValueError):
                self.resolve(c=bad if bad is not None else {"schema": None})
        with self.assertRaises(ValueError):
            cr.resolve_applicability("xx-synthetic", [None], c)

    def test_receipt_or_production_does_not_supply_missing_conduct(self):
        c = context({"receipt": [period("2026-01-01", event_kind="record_received")]})
        self.assertEqual(self.resolve(c=c)["classification"], cr.UNRESOLVED)
        self.assertEqual(self.resolve(c=context({}))["classification"], cr.UNRESOLVED)

    def test_request_receipt_is_explicit_and_not_record_receipt(self):
        r = rule()
        c = context({"receipt": [period("2020-01-01", event_kind="request_received")]})
        b = basis(r, c, "receipt", "request_received")
        self.assertEqual(self.resolve(r, c, b)["applicability"], "applies")
        b["event_kind"] = "record_received"
        self.assertEqual(self.resolve(r, c, b)["classification"], cr.UNRESOLVED)

    def test_dates_and_scope_require_bound_source_evidence(self):
        r, c = rule(), context()
        for field in ("version_verified", "version_evidence", "scope_checks", "rationale"):
            b = basis(r, c)
            b[field] = None
            self.assertEqual(self.resolve(r, c, b)["classification"], cr.UNRESOLVED)
        p = period()
        p["evidence"] = []
        self.assertEqual(self.resolve(c=context({"conduct": [p]}))["classification"], cr.UNRESOLVED)

    def test_missing_unknown_and_empty_duties_never_inapplicable(self):
        c = context()
        for duties in ([], ["xx-unknown"]):
            out = cr.compare_applicability(duties, [rule()], c)
            self.assertEqual(out["status"], "blocked")
            self.assertEqual(out["classification"], cr.UNRESOLVED)

    def test_same_id_historical_rules_select_by_sourced_event(self):
        old, new = rule("2016-01-01", "2020-12-31"), rule("2021-01-01", duty="New duty")
        for when, expected in (("2020-12-31", old), ("2021-01-01", new)):
            c = context({"conduct": [period(when)]})
            decision = cr.resolve_applicability(old["rule_id"], [old, new], c, [basis(old, c), basis(new, c)])
            self.assertEqual(decision["rule_revision_sha256"], cr.rule_revision(expected))

    def test_historical_gap_or_overlap_remains_unresolved(self):
        c = context()
        for rules in ([rule("2021-01-01")], [rule(), rule("2019-01-01", duty="Other duty")]):
            decision = cr.resolve_applicability("xx-synthetic", rules, c, [basis(r, c) for r in rules])
            self.assertEqual(decision["classification"], cr.UNRESOLVED)

    def test_retention_requires_complete_single_version_interval(self):
        r = rule("2016-01-01", "2020-12-31")
        for start, end in (("2020-01-01", None), ("2020-01-01", "2021-01-01")):
            c = context({"retention": [period(start, end, "detection_retention")]})
            b = basis(r, c, "retention", "detection_retention")
            self.assertEqual(self.resolve(r, c, b)["classification"], cr.UNRESOLVED)

    def test_ambiguous_dates_and_stale_rule_or_context_basis_are_unresolved(self):
        r, c = rule(), context()
        old = basis(r, c)
        ambiguous = context({"conduct": [period(), period("2019-01-01")]})
        for changed in (ambiguous, context(request_ids=["new-request"])):
            self.assertEqual(self.resolve(r, changed, old)["classification"], cr.UNRESOLVED)
        self.assertEqual(self.resolve(rule(duty="Changed duty"), c, old)["classification"], cr.UNRESOLVED)
        self.assertEqual(self.resolve(r, ambiguous)["classification"], cr.UNRESOLVED)

    def test_scope_exclusion_needs_all_explicit_checks(self):
        r, c = rule(), context()
        b = basis(r, c)
        b["scope"] = "does_not_apply"
        self.assertEqual(self.resolve(r, c, b)["classification"], cr.UNRESOLVED)
        b["scope_checks"]["jurisdiction"]["status"] = "does_not_apply"
        self.assertEqual(self.resolve(r, c, b)["classification"], "NOT_APPLICABLE")
        b["scope_checks"]["policy_adoption"]["status"] = "unknown"
        self.assertEqual(self.resolve(r, c, b)["classification"], cr.UNRESOLVED)

    def test_invalid_date_and_missing_effective_boundary_fail_closed(self):
        for p in (period("2020-02-30"), period("2020-02-01", "2020-01-01")):
            with self.assertRaises(ValueError):
                context({"conduct": [p]})
        r = rule()
        del r["effective_to"]
        self.assertEqual(self.resolve(r)["classification"], cr.UNRESOLVED)

    def test_independent_date_roles_do_not_overwrite_each_other(self):
        c = context({"conduct": [period("2018-01-01")],
                     "receipt": [period("2026-01-01", event_kind="record_received")],
                     "disclosure": [period("2020-01-01", event_kind="withholding_decision")],
                     "retention": [period("2018-01-01", "2019-01-01", "detection_retention")]})
        self.assertEqual(self.resolve(c=c)["event_period"]["start"], "2018-01-01")
        self.assertEqual(set(c["dates"]), set(cr.DATE_ROLES))


class LedgerPlanTests(unittest.TestCase):
    def test_rule_change_reopens_both_detect_and_review_and_retains_history(self):
        # Use the ledger's explicitly synthetic authority, never production promotion.
        from tests.records.test_ledger_stages import StageTests
        fixture = StageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.advance("privacy")
        history = fixture.rows("receipts")
        content = fixture.rows("stage_content")
        originals = fixture.rows("originals")
        before = revision(subject_sha256=fixture.subject)
        after = revision(subject_sha256=fixture.subject, rules=[rule(duty="Amended synthetic duty")])
        plan = cr.invalidation_plan(before, after)
        for name in plan["stages"]:
            fixture.runner.invalidate(fixture.subject, name, reason=plan["reason"])
        states = {r["stage"]: r["status"] for r in fixture.rows("stage_state")
                  if r["original_sha256"] == fixture.subject}
        self.assertEqual(states, {"preserve": "done", "extract": "done", "catalog": "done",
                                  "detect": "pending", "review": "pending", "compare": "pending", "privacy": "pending"})
        self.assertEqual(fixture.rows("receipts"), history)
        self.assertEqual(fixture.rows("stage_content"), content)
        self.assertEqual(fixture.rows("originals"), originals)


if __name__ == "__main__":
    unittest.main()
