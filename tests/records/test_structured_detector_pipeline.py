"""Synthetic contract regressions, no extraction, services or real records."""
import copy
import json
import sqlite3
import unittest

from campaign_tool.records.detectors.core import DETECTORS, canonical, digest
from campaign_tool.records.detectors.pipeline import (
    ContractError, NORMALIZED_SCHEMA, canonical_joins, evaluate_structured,
)
from tests.records.test_detectors import fixture, h, negative


def context(detector, *, zero=False):
    values = fixture(detector)
    if zero:
        negative(detector, values)
    units, joins, rules, config = values
    sources, mapped = [], []
    for index, unit in enumerate(units):
        source = {"unit_id": "unit-" + str(index), "original_sha256": unit["original_sha256"],
                  "locator": copy.deepcopy(unit["locator"]), "artifact_sha256": digest(unit)}
        sources.append(source)
        mapped.append({"record": unit, "source": {k: source[k] for k in ("unit_id", "artifact_sha256")}})
    sources.append({"unit_id": "policy-unit", "original_sha256": rules["entries"][0]["source_sha256"],
                    "locator": {"page": 1}, "artifact_sha256": h("synthetic policy extracted text")})
    policy = sources[-1]
    mapped.append({"record": {"original_sha256": policy["original_sha256"], "locator": policy["locator"],
                              "record_type": "policy", "event_id": "policy-unit", "fields": {}},
                   "source": {k: policy[k] for k in ("unit_id", "artifact_sha256")}})
    # Synthetic supplied review state, using current gate field names. These
    # fixtures establish no real policy's validity or applicability.
    rules["entries"][0].update(version_verified=True, applicability="yes")
    subject = units[0]["original_sha256"]
    normalized = {"schema": NORMALIZED_SCHEMA, "version": "synthetic-mapper-v1", "complete": True,
                  "source_inventory_sha256": digest(sources),
                  "units": mapped}
    return {"subject_sha256": subject, "normalized": normalized, "source_bindings": sources,
            "rules": rules, "config": config}, joins


def ledger(joins, sources):
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE joins(id TEXT, original_sha256 TEXT, status TEXT, agency_id TEXT,
                           request_id TEXT, evidence TEXT);
        CREATE TABLE agencies(id TEXT);
        CREATE TABLE requests(id TEXT, agency_id TEXT);
        INSERT INTO agencies VALUES('synthetic-agency');
    """)
    for index, join in enumerate(joins):
        source = next(s for s in sources if s["original_sha256"] == join["original_sha256"])
        proof = {"source_sha256": source["original_sha256"], "locator": canonical(source["locator"])}
        con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?)", (str(index), join["original_sha256"],
                    join["status"], join["agency_id"], None, json.dumps(proof)))
    return con


class StructuredPipelineTests(unittest.TestCase):
    def prepare(self, detector="retention-over-policy", *, zero=False):
        args, joins = context(detector, zero=zero)
        con = ledger(joins, args["source_bindings"])
        self.addCleanup(con.close)
        args["joins"] = canonical_joins(con, args["source_bindings"])
        return args, con

    def entry(self, args, name="retention-over-policy"):
        return evaluate_structured(**args)["structured"][name]

    def test_all_seven_positive_and_zero_hit_evaluations(self):
        for detector in DETECTORS:
            for zero in (False, True):
                with self.subTest(detector=detector, zero=zero):
                    args, _ = self.prepare(detector, zero=zero)
                    output = evaluate_structured(**args)
                    self.assertEqual(set(output["structured"]), set(DETECTORS))
                    entry = output["structured"][detector]
                    self.assertEqual(entry["status"], "evaluated")
                    self.assertEqual(bool(entry["hits"]), not zero)
                    self.assertEqual(entry["manifest"]["counts"]["evaluated"], 1)
                    self.assertFalse(output["publication_ready"])
                    self.assertEqual(output["stage_promotions"], 0)
                    self.assertTrue(all(output["revisions"].values()))

    def test_missing_normalization_is_blocked_for_all_seven(self):
        output = evaluate_structured(subject_sha256=h("synthetic subject"))
        self.assertEqual({r["status"] for r in output["structured"].values()}, {"blocked"})
        self.assertEqual({r["reason"] for r in output["structured"].values()}, {"normalization_missing"})

    def test_inapplicable_requires_complete_known_type_inventory(self):
        args, _ = self.prepare()
        for row in args["normalized"]["units"]:
            row["record"]["record_type"] = "policy"
        entry = self.entry(args)
        self.assertEqual(entry["status"], "inapplicable")
        self.assertEqual(entry["manifest"]["counts"]["skipped"], 2)
        args["normalized"]["complete"] = False
        self.assertEqual(self.entry(args)["status"], "blocked")

    def test_unknown_mappings_and_omitted_units_block(self):
        for mutation in ("unknown", "omitted", "empty", "schema"):
            args, _ = self.prepare()
            if mutation == "unknown":
                args["normalized"]["units"][0]["record"]["record_type"] = "unknown"
            elif mutation == "omitted":
                extra = copy.deepcopy(args["source_bindings"][0])
                extra["unit_id"] = "omitted-unit"
                args["source_bindings"].append(extra)
                args["normalized"]["source_inventory_sha256"] = digest(args["source_bindings"])
            elif mutation == "empty":
                args["normalized"]["units"] = []
            else:
                args["normalized"]["schema"] = "guessed-columns-v0"
            self.assertEqual(self.entry(args)["status"], "blocked", mutation)

    def test_source_hash_locator_and_inventory_drift_block(self):
        for key in ("hash", "locator", "inventory", "unit_id"):
            args, _ = self.prepare()
            row = args["normalized"]["units"][0]
            if key == "hash":
                row["source"]["artifact_sha256"] = h("changed")
            elif key == "locator":
                row["record"]["locator"] = {"row": 999}
            elif key == "inventory":
                args["normalized"]["source_inventory_sha256"] = h("old inventory")
            else:
                row["source"]["unit_id"] = "unbound"
            self.assertEqual(self.entry(args)["status"], "blocked", key)

    def test_bare_typed_join_and_stale_snapshot_block(self):
        args, _ = self.prepare()
        args["joins"] = [{"status": "typed", "agency_id": "synthetic-agency"}]
        self.assertEqual(self.entry(args)["reason"], "canonical_join_snapshot_missing")
        args, _ = self.prepare()
        args["source_bindings"][-1]["artifact_sha256"] = h("changed policy text")
        args["normalized"]["units"][-1]["source"]["artifact_sha256"] = h("changed policy text")
        args["normalized"]["source_inventory_sha256"] = digest(args["source_bindings"])
        self.assertEqual(self.entry(args)["reason"], "canonical_join_sources_changed")

    def test_missing_hinted_and_ambiguous_canonical_joins_block(self):
        for mutation in ("missing", "hinted", "ambiguous"):
            args, con = self.prepare()
            if mutation == "missing":
                con.execute("DELETE FROM joins")
            elif mutation == "hinted":
                con.execute("UPDATE joins SET status='hinted'")
            else:
                con.execute("INSERT INTO agencies VALUES('other-synthetic')")
                con.execute("INSERT INTO joins SELECT 'other',original_sha256,status,'other-synthetic',request_id,evidence FROM joins")
            args["joins"] = canonical_joins(con, args["source_bindings"])
            self.assertEqual(self.entry(args)["status"], "blocked", mutation)

    def test_join_support_and_entity_relations_are_checked(self):
        for mutation, reason in (("support", "typed_join_support_missing"),
                                 ("agency", "typed_agency_missing"),
                                 ("request", "typed_request_agency_mismatch")):
            args, con = self.prepare()
            if mutation == "support":
                con.execute("UPDATE joins SET evidence=?", (json.dumps({"source_sha256": h("wrong"), "locator": {"row": 1}}),))
            elif mutation == "agency":
                con.execute("DELETE FROM agencies")
            else:
                con.execute("UPDATE joins SET request_id='missing-request'")
            with self.assertRaisesRegex(ContractError, reason):
                canonical_joins(con, args["source_bindings"])

    def test_training_requires_separate_bound_and_joined_support(self):
        args, con = self.prepare("search-before-training")
        con.execute("DELETE FROM joins WHERE original_sha256 != ?", (args["subject_sha256"],))
        args["joins"] = canonical_joins(con, args["source_bindings"])
        self.assertEqual(self.entry(args, "search-before-training")["status"], "blocked")

    def test_omitted_training_counterevidence_blocks(self):
        args, con = self.prepare("search-before-training")
        earlier = copy.deepcopy(args["source_bindings"][1])
        earlier.update(unit_id="earlier-training", locator={"row": 10})
        args["source_bindings"].append(earlier)
        args["normalized"]["source_inventory_sha256"] = digest(args["source_bindings"])
        args["joins"] = canonical_joins(con, args["source_bindings"])
        self.assertEqual(self.entry(args, "search-before-training")["reason"], "normalization_inventory_incomplete")
        row = copy.deepcopy(args["normalized"]["units"][1])
        row["record"].update(event_id="earlier-training", locator={"row": 10})
        row["record"]["fields"]["training_completed_at"] = "2020-01-19T12:00:00+00:00"
        row["source"]["unit_id"] = "earlier-training"
        args["normalized"]["units"].append(row)
        entry = self.entry(args, "search-before-training")
        self.assertEqual(entry["status"], "evaluated")
        self.assertEqual(entry["hit_count"], 0)

    def test_malformed_join_evidence_is_bounded(self):
        for value in ('[' * 1500 + ']' * 1500, 'x' * 20000, 'null', '[]'):
            args, con = self.prepare()
            con.execute("UPDATE joins SET evidence=?", (value,))
            with self.assertRaises(ContractError):
                canonical_joins(con, args["source_bindings"])

    def test_all_join_revision_rows_have_byte_bounds(self):
        for status in ("hinted", "blocked", "typed"):
            args, con = self.prepare()
            con.execute("UPDATE joins SET status=?, evidence=?", (status, "x" * 20000))
            with self.assertRaisesRegex(ContractError, "typed_join_evidence_bound"):
                canonical_joins(con, args["source_bindings"])
        args, con = self.prepare()
        con.execute("ALTER TABLE agencies ADD COLUMN name TEXT")
        con.execute("UPDATE agencies SET name=?", ("x" * 33000,))
        with self.assertRaisesRegex(ContractError, "join_revision_byte_bound"):
            canonical_joins(con, args["source_bindings"])

    def test_unverified_rule_versions_do_not_evaluate(self):
        for key, value in (("version_verified", False), ("applicability", "unknown")):
            args, _ = self.prepare()
            args["rules"]["entries"][0][key] = value
            self.assertEqual(self.entry(args)["reason"], "rule_version_or_applicability_unresolved")

    def test_auxiliary_search_events_cannot_pollute_subject(self):
        args, _ = self.prepare("search-before-training")
        args["normalized"]["units"][1]["record"]["record_type"] = "search-log"
        self.assertEqual(self.entry(args)["reason"], "unrelated_support_record")

    def test_missing_redacted_rows_do_not_create_subset_rates(self):
        args, _ = self.prepare("purpose-quality")
        extra = copy.deepcopy(args["normalized"]["units"][0])
        extra["record"]["event_id"] = "redacted-event"
        extra["record"]["fields"]["purpose"] = {"state": "redacted"}
        args["normalized"]["units"].append(extra)
        entry = self.entry(args, "purpose-quality")
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(entry["hits"], [])
        self.assertEqual(entry["suppressed_hit_count"], 2)
        self.assertEqual(entry["manifest"]["counts"]["blocked"], 1)

    def test_current_rule_intervals_and_unbound_rules(self):
        args, _ = self.prepare()
        args["rules"]["entries"][0].update(effective_from="2021-01-01", effective_to="2022-01-01")
        self.assertEqual(self.entry(args)["status"], "inapplicable")
        args["rules"]["entries"] = []
        self.assertEqual(self.entry(args)["status"], "blocked")
        args, _ = self.prepare()
        args["rules"]["entries"].append(copy.deepcopy(args["rules"]["entries"][0]))
        self.assertEqual(self.entry(args)["status"], "blocked")
        args, _ = self.prepare()
        args["rules"]["entries"][0]["source_sha256"] = h("unbound policy")
        self.assertEqual(self.entry(args)["reason"], "rule_source_unbound")

    def test_duplicates_determinism_revisions_and_deep_copy(self):
        args, _ = self.prepare()
        before = evaluate_structured(**args)
        args["normalized"]["units"].append(copy.deepcopy(args["normalized"]["units"][0]))
        after = evaluate_structured(**args)
        self.assertNotEqual(before["result_sha256"], after["result_sha256"])
        self.assertEqual(before["structured"]["retention-over-policy"]["hits"], after["structured"]["retention-over-policy"]["hits"])
        self.assertEqual(after, evaluate_structured(**args))
        after["structured"]["retention-over-policy"]["hits"][0]["evidence"][0]["locator"]["row"] = 999
        self.assertEqual(args["normalized"]["units"][0]["record"]["locator"], {"row": 1})

    def test_malformed_oversize_and_nonfinite_inputs_fail_closed(self):
        for mutation in ("oversize", "nan", "malformed", "deep"):
            args, _ = self.prepare()
            if mutation == "oversize":
                args["normalized"]["units"] *= 5001
            elif mutation == "nan":
                args["config"]["bad"] = float("nan")
            elif mutation == "malformed":
                args["normalized"]["units"] = [None]
            else:
                nested = {}
                args["config"]["deep"] = nested
                for _ in range(50):
                    nested["child"] = {}
                    nested = nested["child"]
            self.assertEqual(self.entry(args)["status"], "blocked", mutation)


if __name__ == "__main__":
    unittest.main()
