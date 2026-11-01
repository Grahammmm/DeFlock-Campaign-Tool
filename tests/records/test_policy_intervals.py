"""Synthetic opt-in calendar adapter contracts; no policy authority asserted."""
from contextlib import closing
import copy
import unittest

from campaign_tool.records.detectors.core import digest
from campaign_tool.records.detectors.pipeline import ContractError, canonical_joins
from campaign_tool.records.detectors.policy_intervals import SCHEMA, prepare_rules, evaluate_with_calendar_policy
from tests.records.test_structured_detector_pipeline import context, ledger


def fixture():
    args, joins = context("retention-over-policy")
    sources, normalized = args["source_bindings"], args["normalized"]
    rule = args.pop("rules")
    def evidence(s):
        return [{k: s[k] for k in ("unit_id", "artifact_sha256", "locator")}]
    unit = normalized["units"][0]
    event = unit["record"]["event_at"]
    envelope = {"schema": SCHEMA, "provider_revision": "synthetic-v1",
                "source_inventory_sha256": digest(sources), "normalization_sha256": digest(normalized),
                "rules": rule, "intervals": [{"rule_entry_sha256": digest(rule["entries"][0]),
                    "end_semantics": "inclusive_calendar", "jurisdiction": "synthetic",
                    "evidence": evidence(sources[-1])}],
                "event_calendar_receipts": [{"unit_id": unit["source"]["unit_id"],
                    "normalized_unit_sha256": digest(unit), "event_at": event,
                    "calendar_date": event[:10], "jurisdiction": "synthetic", "timezone": "UTC",
                    "resolver_revision": "synthetic-v1", "evidence": evidence(sources[0])}]}
    return envelope, args, joins


class PolicyIntervalsTests(unittest.TestCase):
    def prepare(self, env, args):
        return prepare_rules(envelope=env, source_bindings=args["source_bindings"], normalized=args["normalized"])

    def dates(self, env, start, end):
        env["rules"]["entries"][0].update(effective_from=start, effective_to=end)
        env["intervals"][0]["rule_entry_sha256"] = digest(env["rules"]["entries"][0])

    def test_rollovers_null_and_single_day(self):
        for start, end, expected in [("2024-02-28", "2024-02-29", "2024-03-01"),
                                     ("2023-12-31", "2023-12-31", "2024-01-01"),
                                     ("2023-01-01", None, None)]:
            env, args, _ = fixture()
            self.dates(env, start, end)
            self.assertEqual(self.prepare(env, args)["entries"][0]["effective_to"], expected)

    def test_exclusive_preserved(self):
        env, args, _ = fixture()
        self.dates(env, "2024-01-01", "9999-12-31")
        env["intervals"][0]["end_semantics"] = "exclusive_calendar"
        self.assertEqual(self.prepare(env, args)["entries"], env["rules"]["entries"])

    def test_bad_endpoints(self):
        for start, end in [("2024-01-01", "9999-12-31"), ("2024-02-02", "2024-02-01"),
                           ("2024-01-01T00:00:00Z", None), ("2024-01-01", "2024-02-30"),
                           ("2024-01-01", "20240102")]:
            env, args, _ = fixture()
            self.dates(env, start, end)
            with self.assertRaises(ContractError):
                self.prepare(env, args)
        env, args, _ = fixture()
        del env["rules"]["entries"][0]["effective_to"]
        env["intervals"][0]["rule_entry_sha256"] = digest(env["rules"]["entries"][0])
        with self.assertRaisesRegex(ContractError, "policy_end_missing"):
            self.prepare(env, args)

    def test_descriptor_and_provenance_drift(self):
        for mutation in ("missing", "duplicate", "semantics", "source", "normalization", "identity",
                         "evidence", "receipt", "timezone", "jurisdiction", "calendar"):
            with self.subTest(mutation=mutation):
                env, args, _ = fixture()
                if mutation == "missing": env["intervals"] = []
                elif mutation == "duplicate": env["intervals"] *= 2
                elif mutation == "semantics": env["intervals"][0].pop("end_semantics")
                elif mutation == "source": env["source_inventory_sha256"] = "0" * 64
                elif mutation == "normalization": env["normalization_sha256"] = "0" * 64
                elif mutation == "identity": env["rules"]["entries"][0]["version_verified"] = False
                elif mutation == "evidence": env["intervals"][0]["evidence"][0]["artifact_sha256"] = "0" * 64
                elif mutation == "receipt": env["event_calendar_receipts"][0]["event_at"] += "x"
                elif mutation == "timezone": env["event_calendar_receipts"][0]["timezone"] = ""
                elif mutation == "jurisdiction": env["intervals"][0]["jurisdiction"] = ""
                else: env["event_calendar_receipts"][0]["calendar_date"] = "2000-01-01"
                with self.assertRaises(ContractError): self.prepare(env, args)

    def test_identity_immutability_and_opt_in_final_day(self):
        env, args, joins = fixture()
        event_day = args["normalized"]["units"][0]["record"]["event_at"][:10]
        self.dates(env, event_day, event_day)
        before = copy.deepcopy([env, args])
        converted = self.prepare(env, args)
        self.assertEqual(converted, self.prepare(env, args))
        self.assertEqual([env, args], before)
        env2 = copy.deepcopy(env)
        env2["provider_revision"] = "synthetic-v2"
        self.assertNotEqual(digest(converted), digest(self.prepare(env2, args)))
        converted["calendar_policy_provenance"]["original_envelope"]["rules"]["entries"].clear()
        self.assertEqual([env, args], before)
        with closing(ledger(joins, args["source_bindings"])) as con:
            args["joins"] = canonical_joins(con, args["source_bindings"])
            result = evaluate_with_calendar_policy(envelope=env, **args)
        entry = result["structured"]["retention-over-policy"]
        self.assertEqual(entry["status"], "evaluated")
        self.assertTrue(entry["hits"])
        self.assertFalse(result["publication_ready"])
        self.assertEqual(result["stage_promotions"], 0)
        from datetime import date, timedelta
        previous = (date.fromisoformat(event_day) - timedelta(days=1)).isoformat()
        self.dates(env, previous, previous)
        result = evaluate_with_calendar_policy(envelope=env, **args)
        self.assertEqual(result["structured"]["retention-over-policy"]["status"], "inapplicable")
        self.assertEqual(result["structured"]["retention-over-policy"]["hits"], [])
