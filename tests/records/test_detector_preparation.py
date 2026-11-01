"""Host-contract tests only; synthetic providers are not installed authority."""
from contextlib import closing
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from campaign_tool.records.detectors import preparation as p
from campaign_tool.records.detectors.core import DETECTORS, MAX_INPUT_BYTES, canonical, digest
from campaign_tool.records.detectors.pipeline import canonical_joins, evaluate_structured
from campaign_tool.records.detectors.sources import SourceSnapshot
from tests.records.test_structured_detector_pipeline import context, ledger


class SyntheticProvider:
    def __init__(self, inputs):
        self.inputs = inputs

    def provide(self, snapshot):
        return self.inputs


def fixture(detector="purpose-quality", zero=False):
    args, joins = context(detector, zero=zero)
    with closing(ledger(joins, args["source_bindings"])) as con:
        bound = canonical_joins(con, args["source_bindings"])
    bindings = args["source_bindings"]
    originals = [{"original_sha256": identity} for identity in sorted({s["original_sha256"] for s in bindings})]
    units = [{"unit_id": s["unit_id"], "payload": {"text": "synthetic extraction"}} for s in bindings]
    revision = digest("synthetic source revision")
    snapshot = SourceSnapshot(args["subject_sha256"], canonical(bindings), canonical(units),
                              canonical(originals), bound, revision)
    common = {"revision": "synthetic-v1", "code_sha256": digest("synthetic provider code"),
              "receipt_sha256": digest("synthetic decision receipt"), "source_revision_sha256": revision}
    normalized = args["normalized"]
    events = {}
    for item in normalized["units"]:
        record = item["record"]
        if record["record_type"] in p.TARGET_TYPES:
            events.setdefault(item["source"]["unit_id"], []).append(
                {k: record[k] for k in ("event_id", "event_at")})
    evidence = [s["unit_id"] for s in bindings]
    descriptors = {
        "normalization": {**common, "payload_sha256": digest(normalized)},
        "support": {**common, "originals_sha256": digest(originals), "bindings_sha256": digest(bindings),
                    "required_original_sha256": [r["original_sha256"] for r in originals], "unresolved": []},
        "dates": {**common, "normalization_sha256": digest(normalized), "events_sha256": digest(events),
                  "decision_sha256": digest("synthetic source date decisions"), "evidence_unit_ids": evidence},
        "policy": {**common, "payload_sha256": digest(args["rules"]),
                   "decision_sha256": digest("synthetic historical applicability decisions"),
                   "evidence_unit_ids": evidence, "interval_end": "exclusive"},
        "provider": {**common, "model": {"mode": "disabled"}},
    }
    return snapshot, p.ProviderInputs(normalized, args["rules"], args["config"], descriptors)


class PreparationTests(unittest.TestCase):
    def blocked(self, snapshot, inputs, reason=None):
        result = p.prepare(snapshot, provider=SyntheticProvider(inputs))
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.prepared)
        if reason:
            self.assertEqual(result.reason, reason)
        return result

    def test_missing_provider_and_record_controlled_provider_block(self):
        snapshot, inputs = fixture()
        self.assertEqual(p.prepare(snapshot).reason, "trusted_provider_unavailable")
        for value in ({"trusted": True, "provide": inputs}, "provider", inputs):
            self.assertEqual(p.prepare(snapshot, provider=value).reason, "host_provider_required")
        self.assertEqual(p.prepare({"snapshot": snapshot}).reason, "source_snapshot_required")

    def test_all_seven_prepared_bundles_preserve_positive_and_zero_hit_contracts(self):
        for detector in DETECTORS:
            for zero in (False, True):
                with self.subTest(detector=detector, zero=zero):
                    snapshot, inputs = fixture(detector, zero)
                    # prepare must not evaluate detectors or perform source IO.
                    result = p.prepare(snapshot, provider=SyntheticProvider(inputs))
                    self.assertEqual(result.status, "prepared", result.reason)
                    evaluation = evaluate_structured(**result.prepared.kwargs)
                    entry = evaluation["structured"][detector]
                    self.assertEqual(entry["status"], "evaluated")
                    self.assertEqual(bool(entry["hits"]), not zero)
                    self.assertFalse(evaluation["publication_ready"])
                    self.assertEqual(evaluation["stage_promotions"], 0)

    def test_each_missing_descriptor_or_receipt_blocks(self):
        for name in p.DESCRIPTORS:
            with self.subTest(name=name):
                snapshot, inputs = fixture()
                del inputs.descriptors[name]
                self.blocked(snapshot, inputs, "required_descriptors_missing")
                snapshot, inputs = fixture()
                inputs.descriptors[name]["receipt_sha256"] = None
                self.blocked(snapshot, inputs, "descriptor_hash_missing")
                snapshot, inputs = fixture()
                inputs.descriptors[name]["source_revision_sha256"] = digest("changed")
                self.blocked(snapshot, inputs, "descriptor_source_changed")

    def test_flags_cannot_replace_descriptors(self):
        snapshot, inputs = fixture()
        for name in p.DESCRIPTORS:
            inputs.descriptors[name] = {"trusted": True, "verified": True}
        self.blocked(snapshot, inputs, "descriptor_shape")

    def test_unknown_or_incomplete_mapping_blocks(self):
        for change in ("unknown", "omitted", "incomplete", "source", "locator"):
            with self.subTest(change=change):
                snapshot, inputs = fixture()
                if change == "unknown":
                    inputs.normalized["units"][0]["record"]["record_type"] = "unknown"
                elif change == "omitted":
                    inputs.normalized["units"].pop()
                elif change == "incomplete":
                    inputs.normalized["complete"] = False
                elif change == "source":
                    inputs.normalized["units"][0]["source"]["artifact_sha256"] = digest("changed")
                else:
                    inputs.normalized["units"][0]["record"]["locator"] = {"row": 999}
                self.blocked(snapshot, inputs)

    def test_support_gap_or_inventory_change_blocks(self):
        for key, value in (("unresolved", ["missing-training"]), ("required_original_sha256", []),
                           ("originals_sha256", digest("changed")), ("bindings_sha256", digest("changed"))):
            snapshot, inputs = fixture()
            inputs.descriptors["support"][key] = value
            self.blocked(snapshot, inputs)

    def test_date_policy_and_evidence_mismatch_block(self):
        mutations = [("dates", "events_sha256", digest("changed")),
                     ("dates", "normalization_sha256", digest("changed")),
                     ("dates", "evidence_unit_ids", ["unknown-unit"]),
                     ("policy", "evidence_unit_ids", []),
                     ("policy", "payload_sha256", digest("changed")),
                     ("policy", "interval_end", "inclusive")]
        for name, key, value in mutations:
            with self.subTest(name=name, key=key):
                snapshot, inputs = fixture()
                inputs.descriptors[name][key] = value
                self.blocked(snapshot, inputs)

    def test_unverified_policy_and_undated_event_block(self):
        snapshot, inputs = fixture()
        inputs.rules["entries"][0]["version_verified"] = False
        self.blocked(snapshot, inputs, "policy_decision_unresolved")
        snapshot, inputs = fixture()
        inputs.normalized["units"][0]["record"].pop("event_at")
        self.blocked(snapshot, inputs, "invalid_preparation_input")

    def test_model_alias_blocks_and_pinned_revision_changes_identity(self):
        snapshot, inputs = fixture()
        inputs.descriptors["provider"]["model"] = {"mode": "alias", "name": "local"}
        self.blocked(snapshot, inputs, "model_revision_unresolved")
        inputs.descriptors["provider"]["model"] = {"mode": "pinned", "revision_sha256": digest("model-1"),
                                                   "prompt_sha256": digest("prompt-1")}
        first = p.prepare(snapshot, provider=SyntheticProvider(inputs))
        self.assertEqual(first.status, "prepared")
        inputs.descriptors["provider"]["model"]["revision_sha256"] = digest("model-2")
        second = p.prepare(snapshot, provider=SyntheticProvider(inputs))
        self.assertNotEqual(first.prepared.revision_sha256, second.prepared.revision_sha256)

    def test_provider_errors_are_sanitized_and_wrong_result_type_blocks(self):
        class Broken:
            def provide(self, snapshot):
                raise RuntimeError("private path and record contents")
        snapshot, inputs = fixture()
        self.assertEqual(p.prepare(snapshot, provider=Broken()).reason, "provider_unavailable")
        self.blocked(snapshot, {"trusted": True}, "provider_result_type")

    def test_provider_lookup_runtime_error_is_sanitized(self):
        class BrokenLookup:
            @property
            def provide(self):
                raise RuntimeError("synthetic-private-marker")
        snapshot, _ = fixture()
        result = p.prepare(snapshot, provider=BrokenLookup())
        self.assertEqual(result, p.Preparation("blocked", "provider_unavailable"))
        self.assertNotIn("synthetic-private-marker", repr(result))

    def test_provider_lookup_preparation_error_is_sanitized(self):
        class BrokenLookup:
            @property
            def provide(self):
                raise p.PreparationError("synthetic-private-marker")
        snapshot, _ = fixture()
        result = p.prepare(snapshot, provider=BrokenLookup())
        self.assertEqual(result, p.Preparation("blocked", "provider_unavailable"))
        self.assertNotIn("synthetic-private-marker", repr(result))

    def test_core_row_and_byte_bounds_apply_before_provider_or_hashing(self):
        snapshot, inputs = fixture()
        over = replace(snapshot, bindings_json=canonical([{}] * 5001))
        with patch.object(SyntheticProvider, "provide", side_effect=AssertionError("must not call")):
            self.assertEqual(p.prepare(over, provider=SyntheticProvider(inputs)).status, "blocked")
        inputs.normalized["units"] = [{}] * 5001
        self.blocked(snapshot, inputs)
        snapshot, inputs = fixture()
        inputs.config["oversize"] = "x" * (MAX_INPUT_BYTES + 1)
        self.blocked(snapshot, inputs)
        snapshot, inputs = fixture()
        inputs.config["escaped"] = "\x00" * (MAX_INPUT_BYTES // 6)
        self.blocked(snapshot, inputs, "preparation_byte_bound")

    def test_malformed_json_and_deep_structure_block(self):
        snapshot, inputs = fixture()
        self.assertEqual(p.prepare(replace(snapshot, units_json="[")).status, "blocked")
        deep = {}
        inputs.config["deep"] = deep
        for _ in range(34):
            deep["next"] = {}
            deep = deep["next"]
        self.blocked(snapshot, inputs)

    def test_source_join_and_inventory_changes_block(self):
        snapshot, inputs = fixture()
        changed = replace(snapshot, joins=replace(snapshot.joins, source_bindings_sha256=digest("changed")))
        self.blocked(changed, inputs, "canonical_join_sources_changed")
        self.blocked(replace(snapshot, units_json="[]"), inputs, "source_unit_inventory_changed")
        self.blocked(replace(snapshot, originals_json="[]"), inputs, "source_inventory_missing")

    def test_unicode_source_byte_bound_blocks_before_provider(self):
        snapshot, inputs = fixture()
        over = replace(snapshot, units_json='["' + "\u2603" * (MAX_INPUT_BYTES // 3) + '"]')
        with patch.object(SyntheticProvider, "provide", side_effect=AssertionError("must not call")):
            self.assertEqual(p.prepare(over, provider=SyntheticProvider(inputs)).reason,
                             "source_snapshot_byte_bound")

    def test_duplicate_source_units_block(self):
        snapshot, inputs = fixture()
        units = snapshot.units
        units.append(units[0])
        self.blocked(replace(snapshot, units_json=canonical(units)), inputs, "source_unit_inventory_changed")

    def test_missing_rules_still_block_evaluation_after_structural_preparation(self):
        snapshot, inputs = fixture()
        inputs.rules["entries"] = []
        inputs.descriptors["policy"]["payload_sha256"] = digest(inputs.rules)
        result = p.prepare(snapshot, provider=SyntheticProvider(inputs))
        self.assertEqual(result.status, "prepared")
        entry = evaluate_structured(**result.prepared.kwargs)["structured"]["purpose-quality"]
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(entry["hits"], [])

    def test_provider_receipt_code_and_model_prompt_changes_bind_identity(self):
        snapshot, inputs = fixture()
        inputs.descriptors["provider"]["model"] = {"mode": "pinned", "revision_sha256": digest("model"),
                                                   "prompt_sha256": digest("prompt")}
        first = p.prepare(snapshot, provider=SyntheticProvider(inputs))
        for name in p.DESCRIPTORS:
            for key in ("code_sha256", "receipt_sha256"):
                modified = copy.deepcopy(inputs)
                modified.descriptors[name][key] = digest("changed")
                second = p.prepare(snapshot, provider=SyntheticProvider(modified))
                self.assertEqual(second.status, "prepared")
                self.assertNotEqual(first.prepared.revision_sha256, second.prepared.revision_sha256)
        inputs.descriptors["provider"]["model"]["prompt_sha256"] = digest("new prompt")
        second = p.prepare(snapshot, provider=SyntheticProvider(inputs))
        self.assertNotEqual(first.prepared.revision_sha256, second.prepared.revision_sha256)

    def test_deterministic_independent_copies_and_all_descriptor_revisions_bound(self):
        snapshot, inputs = fixture()
        first = p.prepare(snapshot, provider=SyntheticProvider(inputs))
        again = p.prepare(snapshot, provider=SyntheticProvider(inputs))
        self.assertEqual(first, again)
        returned = first.prepared.kwargs
        returned["normalized"]["units"].clear()
        self.assertTrue(first.prepared.kwargs["normalized"]["units"])
        for name in p.DESCRIPTORS:
            modified = copy.deepcopy(inputs)
            modified.descriptors[name]["revision"] = "synthetic-v2"
            second = p.prepare(snapshot, provider=SyntheticProvider(modified))
            self.assertEqual(second.status, "prepared")
            self.assertNotEqual(first.prepared.revision_sha256, second.prepared.revision_sha256)
        inputs.config["as_of"] = "2020-01-21"
        second = p.prepare(snapshot, provider=SyntheticProvider(inputs))
        self.assertNotEqual(first.prepared.revision_sha256, second.prepared.revision_sha256)


if __name__ == "__main__":
    unittest.main()
