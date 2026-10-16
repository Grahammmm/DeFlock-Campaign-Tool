"""Host-only preparation contract; no installed semantic provider or activation.

Call only with a fresh resolve_sources result and a host-authenticated provider.
Python types and receipt hashes are bindings, NOT credentials or review. Nothing
here discovers, installs or authenticates providers, reads records, evaluates a
detector, or writes a stage. Untrusted JSON must never select the provider.
"""
from dataclasses import dataclass
import json
from typing import Protocol

from .core import MAX_INPUT_BYTES, TYPES, canonical, digest, moment, reference, sha
from .persistence import _preflight
from .pipeline import KNOWN_TYPES, NORMALIZED_SCHEMA, CanonicalJoins, _sources
from .sources import SourceSnapshot

VERSION = "structured-preparation-v1"
TARGET_TYPES = set().union(*TYPES.values())
DESCRIPTORS = {"normalization", "support", "dates", "policy", "provider"}


@dataclass(frozen=True)
class ProviderInputs:
    """Private host result. No JSON flag can establish issuer authentication."""
    normalized: dict
    rules: dict
    config: dict
    descriptors: dict


class PreparationProvider(Protocol):
    """Installation/authentication and semantic verification belong to the host.

    provide must source-verify mapping, dependency closure, controlling dates and
    historical policy decisions before issuing descriptors. No implementation is
    installed by this module. A model may propose, but cannot grant authority.
    """
    def provide(self, snapshot: SourceSnapshot) -> ProviderInputs: ...


@dataclass(frozen=True)
class PreparedInputs:
    inputs_json: str
    descriptors_json: str
    source_revision_sha256: str
    joins: CanonicalJoins
    revision_sha256: str

    @property
    def kwargs(self):
        """Independent private copies for a future evaluate_structured caller."""
        return {**json.loads(self.inputs_json), "joins": self.joins}


@dataclass(frozen=True)
class Preparation:
    status: str
    reason: str
    prepared: PreparedInputs | None = None


class PreparationError(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise PreparationError(reason)


def _bounded(units, joins, rules, context):
    # Reuse core's stricter 5,000-unit/8-MiB and native JSON structure bounds.
    # Convert only after preflight, including escaped JSON's serialized size.
    _preflight(units, joins, rules, context)
    require(len(canonical([units, joins, rules, context]).encode()) <= MAX_INPUT_BYTES,
            "preparation_byte_bound")


def _descriptor(value, keys, revision):
    require(type(value) is dict and set(value) == keys | {
        "revision", "code_sha256", "receipt_sha256", "source_revision_sha256"},
        "descriptor_shape")
    require(type(value["revision"]) is str and 0 < len(value["revision"].strip()) <= 256,
            "descriptor_revision_missing")
    require(all(sha(value[k]) for k in ("code_sha256", "receipt_sha256", "source_revision_sha256")),
            "descriptor_hash_missing")
    require(value["source_revision_sha256"] == revision, "descriptor_source_changed")


def _prepare(snapshot, supplied, bindings, source_units, originals, join_rows):
    require(type(supplied) is ProviderInputs, "provider_result_type")
    normalized, rules, config, descriptors = (supplied.normalized, supplied.rules,
                                             supplied.config, supplied.descriptors)
    require(type(normalized) is dict and type(descriptors) is dict, "provider_input_shape")
    _bounded(normalized.get("units"), join_rows, rules,
             {"normalized": normalized, "config": config, "descriptors": descriptors,
              "bindings": bindings, "source_units": source_units, "originals": originals})
    require(type(config) is dict, "config_required")
    require(set(descriptors) == DESCRIPTORS, "required_descriptors_missing")
    require(normalized.get("schema") == NORMALIZED_SCHEMA and
            type(normalized.get("version")) is str and bool(normalized["version"].strip()),
            "normalization_version_missing")
    require(normalized.get("complete") is True, "normalization_incomplete")
    require(normalized.get("source_inventory_sha256") == digest(bindings), "normalization_inventory_changed")
    sources = _sources(bindings)
    mapped, dated = set(), {}
    for item in normalized["units"]:
        require(type(item) is dict and set(item) == {"record", "source"}, "normalized_unit_shape")
        record, binding = item["record"], item["source"]
        require(type(record) is dict and type(binding) is dict and
                set(binding) == {"unit_id", "artifact_sha256"}, "normalized_source_shape")
        identity = binding["unit_id"]
        require(type(identity) is str and identity in sources, "normalized_source_missing")
        source = sources[identity]
        require(binding["artifact_sha256"] == source["artifact_sha256"] and
                reference(record) == reference(source), "normalized_source_changed")
        kind = record.get("record_type")
        require(kind in KNOWN_TYPES, "normalization_mapping_unresolved")
        require(record["original_sha256"] == snapshot.subject_sha256 or kind not in TARGET_TYPES,
                "unrelated_support_record")
        mapped.add(identity)
        if kind in TARGET_TYPES:
            require(type(record.get("event_id")) is str and 0 < len(record["event_id"].strip()) <= 256,
                    "event_identity_missing")
            moment(record.get("event_at"))
            dated.setdefault(identity, []).append({"event_id": record["event_id"], "event_at": record["event_at"]})
    require(mapped == set(sources), "normalization_inventory_incomplete")
    require({row["unit_id"] for row in source_units} == set(sources), "source_unit_inventory_changed")
    require(type(rules.get("version")) is str and bool(rules["version"].strip()), "rules_version_missing")
    for rule in rules.get("entries", []):
        require(rule.get("source_sha256") in {s["original_sha256"] for s in bindings}, "policy_source_unbound")
        require(rule.get("version_verified") is True and rule.get("applicability") == "yes",
                "policy_decision_unresolved")

    revision = snapshot.revision_sha256
    for name, keys in {
        "normalization": {"payload_sha256"},
        "support": {"originals_sha256", "bindings_sha256", "required_original_sha256", "unresolved"},
        "dates": {"normalization_sha256", "events_sha256", "decision_sha256", "evidence_unit_ids"},
        "policy": {"payload_sha256", "decision_sha256", "evidence_unit_ids", "interval_end"},
        "provider": {"model"},
    }.items():
        _descriptor(descriptors[name], keys, revision)
    require(descriptors["normalization"]["payload_sha256"] == digest(normalized), "normalization_descriptor_changed")
    support = descriptors["support"]
    identities = [row["original_sha256"] for row in originals]
    require(type(support["required_original_sha256"]) is list and
            sorted(support["required_original_sha256"]) == sorted(identities) and
            support["unresolved"] == [], "support_closure_unresolved")
    require(support["originals_sha256"] == digest(originals) and support["bindings_sha256"] == digest(bindings),
            "support_descriptor_changed")
    dates, policy = descriptors["dates"], descriptors["policy"]
    require(dates["normalization_sha256"] == digest(normalized) and dates["events_sha256"] == digest(dated),
            "date_descriptor_changed")
    require(policy["payload_sha256"] == digest(rules) and policy["interval_end"] == "exclusive",
            "policy_descriptor_changed")
    for descriptor in (dates, policy):
        evidence = descriptor["evidence_unit_ids"]
        require(sha(descriptor["decision_sha256"]) and type(evidence) is list and evidence and
                all(type(identity) is str and identity in sources for identity in evidence),
                "decision_evidence_unbound")
    model = descriptors["provider"]["model"]
    require(type(model) is dict and (model == {"mode": "disabled"} or
            (set(model) == {"mode", "revision_sha256", "prompt_sha256"} and model["mode"] == "pinned" and
             sha(model["revision_sha256"]) and sha(model["prompt_sha256"]))), "model_revision_unresolved")
    inputs = {"subject_sha256": snapshot.subject_sha256, "normalized": normalized,
              "source_bindings": bindings, "rules": rules, "config": config}
    envelope = {"schema": VERSION, "inputs": inputs, "descriptors": descriptors,
                "source_revision_sha256": revision, "joins_revision_sha256": snapshot.joins.revision_sha256,
                "join_rows": join_rows}
    require(len(canonical(envelope).encode()) <= MAX_INPUT_BYTES, "prepared_envelope_byte_bound")
    return PreparedInputs(canonical(inputs), canonical(descriptors), revision, snapshot.joins, digest(envelope))


def prepare(snapshot, *, provider=None):
    """Fully bound private preparation or a stable blocker; never acceptance.

    Receipts/decision digests must resolve and be authenticated by the host
    provider before return. This boundary only validates their shape/bindings.
    SourceSnapshot must come directly from fresh resolution, never record JSON.
    """
    try:
        require(type(snapshot) is SourceSnapshot, "source_snapshot_required")
        require(sha(snapshot.subject_sha256) and sha(snapshot.revision_sha256), "source_revision_missing")
        require(type(snapshot.joins) is CanonicalJoins and sha(snapshot.joins.revision_sha256), "canonical_joins_required")
        raw = [snapshot.bindings_json, snapshot.units_json, snapshot.originals_json, snapshot.joins.rows_json]
        require(all(type(value) is str for value in raw) and
                sum(len(value) for value in raw) <= MAX_INPUT_BYTES and
                sum(len(value.encode()) for value in raw) <= MAX_INPUT_BYTES, "source_snapshot_byte_bound")
        bindings, units, originals, join_rows = [json.loads(value) for value in raw]
        _bounded(bindings, join_rows, {}, {"units": units, "originals": originals})
        require(type(units) is list and type(originals) is list and originals, "source_inventory_missing")
        _preflight(units, join_rows, {}, {})
        sources = _sources(bindings)
        unit_ids = [row["unit_id"] for row in units]
        require(len(unit_ids) == len(set(unit_ids)) and set(unit_ids) == set(sources),
                "source_unit_inventory_changed")
        identities = [row["original_sha256"] for row in originals]
        require(len(identities) == len(set(identities)) and snapshot.subject_sha256 in identities and
                set(identities) == {s["original_sha256"] for s in sources.values()}, "source_original_inventory_changed")
        require(snapshot.joins.source_bindings_sha256 == digest(bindings), "canonical_join_sources_changed")
        require(provider is not None, "trusted_provider_unavailable")
        require(not isinstance(provider, (dict, str, bytes)) and callable(getattr(provider, "provide", None)),
                "host_provider_required")
        try:
            supplied = provider.provide(snapshot)
        except Exception:
            raise PreparationError("provider_unavailable") from None
        prepared = _prepare(snapshot, supplied, bindings, units, originals, join_rows)
        return Preparation("prepared", "host_supplied_inputs_bound", prepared)
    except PreparationError as error:
        return Preparation("blocked", str(error))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        # Never echo record values, paths or provider exception text.
        return Preparation("blocked", "invalid_preparation_input")
