"""Explicit host-only calendar policy boundary. No IO or legal verification.

The host verifies source bytes, policy assertions and calendar receipts before
constructing this envelope. A hash binds identity, never truth or applicability.
Nothing in the runtime imports this adapter. Endpoints are calendar dates; event
receipts must agree with the existing detector's event date (no timestamp rewrite).
"""
from datetime import date, timedelta
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import MAX_INPUT_BYTES, TYPES, canonical, digest, moment, sha
from .persistence import _preflight
from .pipeline import ContractError, _sources, evaluate_structured, require

SCHEMA = "structured-policy-intervals-v1"
VERSION = "calendar-policy-adapter-v1"


def _day(value):
    require(type(value) is str, "policy_date_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ContractError("policy_date_invalid") from error
    require(parsed.isoformat() == value, "policy_date_not_canonical")
    return parsed


def _text(value):
    return type(value) is str and 0 < len(value.strip()) <= 256


def prepare_rules(*, envelope, source_bindings, normalized):
    """Return detached exclusive rules or raise ContractError, failing closed.

    Supply one descriptor per exact original entry and one calendar receipt per
    detector input unit. Evidence resolves against the same verified inventory.
    The complete original envelope is retained in hashed rules provenance.
    """
    try:
        _preflight([], [], {}, {"envelope": envelope, "normalized": normalized,
                                  "sources": source_bindings})
        require(len(canonical([envelope, source_bindings, normalized]).encode()) <= MAX_INPUT_BYTES,
                "policy_context_byte_bound")
        require(type(envelope) is dict and envelope.get("schema") == SCHEMA, "policy_schema_invalid")
        require(_text(envelope.get("provider_revision")), "policy_provider_revision_missing")
        sources = _sources(source_bindings)
        require(envelope.get("source_inventory_sha256") == digest(source_bindings) ==
                normalized.get("source_inventory_sha256"), "policy_source_inventory_mismatch")
        require(envelope.get("normalization_sha256") == digest(normalized), "policy_normalization_mismatch")
        rules = envelope["rules"]
        require(type(rules) is dict and type(rules.get("entries")) is list and
                len(rules["entries"]) <= 256, "policy_rules_invalid")
        descriptors = envelope.get("intervals")
        require(type(descriptors) is list and len(descriptors) == len(rules["entries"]),
                "policy_descriptor_count")

        def evidence(rows, original=None):
            require(type(rows) is list and bool(rows), "policy_evidence_missing")
            for row in rows:
                require(type(row) is dict and set(row) == {"unit_id", "artifact_sha256", "locator"},
                        "policy_evidence_shape")
                source = sources.get(row["unit_id"])
                require(source is not None and source["artifact_sha256"] == row["artifact_sha256"] and
                        source["locator"] == row["locator"] and
                        (original is None or source["original_sha256"] == original), "policy_evidence_mismatch")

        converted = json.loads(canonical(rules))
        seen, jurisdictions = set(), set()
        for entry, output in zip(rules["entries"], converted["entries"]):
            identity = digest(entry)
            require(identity not in seen, "policy_duplicate_entry")
            seen.add(identity)
            matches = [d for d in descriptors if d.get("rule_entry_sha256") == identity]
            require(len(matches) == 1, "policy_descriptor_identity")
            descriptor = matches[0]
            require(_text(descriptor.get("jurisdiction")), "policy_jurisdiction_missing")
            jurisdictions.add(descriptor["jurisdiction"])
            require(sha(entry.get("source_sha256")), "policy_source_hash_missing")
            evidence(descriptor.get("evidence"), entry["source_sha256"])
            semantics = descriptor.get("end_semantics")
            require(semantics in ("inclusive_calendar", "exclusive_calendar"), "policy_end_semantics_invalid")
            start = _day(entry["effective_from"])
            require("effective_to" in entry, "policy_end_missing")
            if entry["effective_to"] is not None:
                end = _day(entry["effective_to"])
                require(end >= start if semantics == "inclusive_calendar" else end > start,
                        "policy_interval_reversed_or_empty")
                if semantics == "inclusive_calendar":
                    require(end != date.max, "inclusive_end_overflow")
                    output["effective_to"] = (end + timedelta(days=1)).isoformat()
        receipts = envelope.get("event_calendar_receipts")
        require(type(receipts) is list, "policy_calendar_receipts_missing")
        units = [u for u in normalized["units"] if u["record"].get("record_type") in
                 set().union(*TYPES.values())]
        require(len(receipts) == len(units), "policy_calendar_receipt_count")
        seen_units = set()
        for unit in units:
            unit_id = unit["source"]["unit_id"]
            require(unit_id not in seen_units, "policy_calendar_unit_duplicate")
            seen_units.add(unit_id)
            matches = [r for r in receipts if r.get("unit_id") == unit_id]
            require(len(matches) == 1, "policy_calendar_receipt_identity")
            receipt, record = matches[0], unit["record"]
            require(receipt.get("normalized_unit_sha256") == digest(unit) and
                    receipt.get("event_at") == record.get("event_at"), "policy_calendar_receipt_mismatch")
            require(receipt.get("jurisdiction") in jurisdictions and
                    _text(receipt.get("resolver_revision")) and _text(receipt.get("timezone")),
                    "policy_calendar_context_missing")
            event = moment(record["event_at"])
            local = event.astimezone(ZoneInfo(receipt["timezone"]))
            require(_day(receipt.get("calendar_date")) == event.date() == local.date(),
                    "policy_calendar_date_mismatch")
            evidence(receipt.get("evidence"), record["original_sha256"])
        converted["calendar_policy_provenance"] = {
            "adapter_revision": VERSION, "adapter_revision_sha256": digest(VERSION),
            "original_envelope_sha256": digest(envelope), "original_envelope": json.loads(canonical(envelope))}
        require(len(canonical(converted).encode()) <= MAX_INPUT_BYTES, "policy_output_byte_bound")
        return converted
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, ZoneInfoNotFoundError) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError("policy_context_invalid") from error


def evaluate_with_calendar_policy(*, envelope, source_bindings, normalized, **context):
    """Explicit opt-in; invalid policy context raises before detector evaluation."""
    rules = prepare_rules(envelope=envelope, source_bindings=source_bindings, normalized=normalized)
    return evaluate_structured(source_bindings=source_bindings, normalized=normalized, rules=rules, **context)
