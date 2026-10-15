"""Synthetic date/applicability boundary matrix; no source-date authority.

Reuses PR96 fixtures. Tests describe the pure context helper's current calendar
contract only. A trusted provider must resolve timezone/calendar interpretation;
these fixtures neither authenticate evidence nor supply real legal policy facts.
"""
import copy
import unittest

from campaign_tool.records import context_revision as cr
from campaign_tool.records.detectors import evaluate
from tests.records import test_context_revision as fixtures
from tests.records import test_detectors as detector_fixtures


class ContextDateBoundaryTests(unittest.TestCase):
    def decide(self, rule, context, bases=None):
        return cr.resolve_applicability(
            rule["rule_id"], [rule], context,
            [fixtures.basis(rule, context)] if bases is None else bases,
        )

    def assert_unresolved(self, decision, reason):
        self.assertEqual(decision["classification"], cr.UNRESOLVED)
        self.assertEqual(decision["applicability"], "unknown")
        self.assertIn(reason, decision["reasons"])

    def test_timestamp_strings_are_not_silently_converted_to_calendar_dates(self):
        # The first two strings denote the same instant on different calendar
        # dates. Neither is an admitted jurisdiction-resolved calendar date.
        values = ("2020-01-01T00:30:00+14:00",
                  "2019-12-31T10:30:00Z", "2020-01-01T00:30:00")
        for role in cr.DATE_ROLES:
            for field in ("start", "end"):
                for value in values:
                    with self.subTest(role=role, field=field, value=value):
                        period = fixtures.period()
                        period[field] = value
                        with self.assertRaisesRegex(ValueError, "calendar_date_required"):
                            fixtures.context({role: [period]})

    def test_duplicate_and_conflicting_conduct_assertions_remain_ambiguous(self):
        rule = fixtures.rule()
        for conflicting in (False, True):
            with self.subTest(conflicting=conflicting):
                first = fixtures.period()
                second = copy.deepcopy(first)
                if conflicting:
                    second["start"] = "2020-01-02"
                context = fixtures.context({"conduct": [first, second]})
                reversed_context = fixtures.context({"conduct": [second, first]})
                self.assertEqual(context, reversed_context)
                self.assertEqual(cr.fingerprint(context),
                                 cr.fingerprint(reversed_context))
                self.assertEqual(len(context["dates"]["conduct"]), 2)
                self.assert_unresolved(self.decide(rule, context),
                                       "event_date_missing_or_ambiguous")

    def test_missing_retention_start_is_not_inferred_from_end_or_receipt(self):
        rule = fixtures.rule()
        context = fixtures.context({
            "retention": [fixtures.period(None, "2020-01-02", "detection_retention")],
            "receipt": [fixtures.period("2020-01-01", event_kind="record_received")],
        })
        basis = fixtures.basis(rule, context, "retention", "detection_retention")
        decision = self.decide(rule, context, [basis])
        self.assert_unresolved(decision, "event_date_not_source_bound")
        self.assertIsNone(decision["event_period"])

    def test_calendar_policy_endpoints_and_leap_day_are_explicit(self):
        # This helper has inclusive calendar endpoints. This is not a conversion
        # rule for the structured detector core's exclusive effective_to input.
        rule = fixtures.rule("2020-02-28", "2020-03-01")
        for when in ("2020-02-27", "2020-02-28", "2020-02-29",
                     "2020-03-01", "2020-03-02"):
            with self.subTest(when=when):
                context = fixtures.context({"conduct": [fixtures.period(when)]})
                decision = self.decide(rule, context)
                if when in ("2020-02-28", "2020-02-29", "2020-03-01"):
                    self.assertEqual(decision["classification"], "LEGAL_REVIEW_REQUIRED")
                    self.assertEqual(decision["event_period"]["start"], when)
                else:
                    self.assert_unresolved(decision,
                                           "historical_version_or_coverage_unresolved")

    def test_detector_policy_end_is_exclusive_without_context_conversion(self):
        # Independently exercise the existing detector contract. No adapter
        # translation from the context helper's inclusive end is asserted.
        for when, admitted in (("2020-02-27", False), ("2020-02-28", True),
                               ("2020-02-29", True), ("2020-03-01", False)):
            with self.subTest(when=when):
                values = detector_fixtures.fixture("retention-over-policy")
                values[0][0]["event_at"] = when + "T12:00:00+00:00"
                values[2]["entries"][0].update(
                    effective_from="2020-02-28", effective_to="2020-03-01")
                result = evaluate("retention-over-policy", *values)
                if admitted:
                    self.assertEqual(result["manifest"]["counts"]["evaluated"], 1)
                    self.assertTrue(result["hits"])
                    self.assertTrue(all(hit["triage_only"] for hit in result["hits"]))
                else:
                    self.assertEqual(result["hits"], [])
                    self.assertEqual(result["manifest"]["counts"]["skipped"], 1)
                    self.assertEqual(result["manifest"]["counts"]["blocked"], 0)
                    self.assertEqual(result["manifest"]["outcomes"][0]["reason"],
                                     "rule_not_effective")

    def test_retention_interval_keeps_both_effective_boundary_edges(self):
        rule = fixtures.rule("2020-02-28", "2020-03-01")
        for start, end, admitted in (
            ("2020-02-28", "2020-03-01", True),
            ("2020-02-27", "2020-03-01", False),
            ("2020-02-28", "2020-03-02", False),
        ):
            with self.subTest(start=start, end=end):
                context = fixtures.context({
                    "retention": [fixtures.period(start, end, "detection_retention")],
                })
                basis = fixtures.basis(rule, context, "retention", "detection_retention")
                decision = self.decide(rule, context, [basis])
                if admitted:
                    self.assertEqual(decision["classification"], "LEGAL_REVIEW_REQUIRED")
                    self.assertEqual(decision["event_period"]["end"], end)
                else:
                    self.assert_unresolved(decision, "event_period_crosses_rule_versions")

    def test_duplicate_or_conflicting_applicability_bases_never_last_win(self):
        rule, context = fixtures.rule(), fixtures.context()
        first = fixtures.basis(rule, context)
        for conflicting in (False, True):
            second = copy.deepcopy(first)
            if conflicting:
                second["scope"] = "does_not_apply"
                second["scope_checks"]["regulated_role"]["status"] = "does_not_apply"
            for bases in ([first, second], [second, first]):
                with self.subTest(conflicting=conflicting, reversed=bases[0] is second):
                    self.assert_unresolved(
                        self.decide(rule, context, bases),
                        "missing_or_ambiguous_applicability_basis",
                    )

    def test_competing_controlling_event_roles_do_not_select_a_policy_version(self):
        old = fixtures.rule("2016-01-01", "2020-12-31")
        new = fixtures.rule("2021-01-01")
        context = fixtures.context({
            "conduct": [fixtures.period("2020-01-01")],
            "receipt": [fixtures.period("2021-01-01", event_kind="record_received")],
        })
        bases = [
            fixtures.basis(old, context, "conduct", "search"),
            fixtures.basis(new, context, "receipt", "record_received"),
        ]
        decision = cr.resolve_applicability(old["rule_id"], [old, new], context, bases)
        self.assert_unresolved(decision, "conflicting_controlling_events")
        self.assertIsNone(decision["rule_revision_sha256"])

    def test_retention_end_correction_revokes_analytical_reuse_only(self):
        rule = fixtures.rule()
        contexts = [fixtures.context({
            "retention": [fixtures.period("2020-01-01", end, "detection_retention")],
        }) for end in ("2020-01-02", "2020-01-03")]
        revisions = [
            fixtures.revision(
                context=context,
                applicability_bases=[fixtures.basis(
                    rule, context, "retention", "detection_retention")],
            ) for context in contexts
        ]
        plan = cr.invalidation_plan(*revisions)
        self.assertEqual(plan["stages"],
                         ["catalog", "detect", "review", "compare", "privacy"])
        self.assertTrue(plan["revoke_approval"])
        self.assertTrue(plan["retain_history"])


if __name__ == "__main__":
    unittest.main()
