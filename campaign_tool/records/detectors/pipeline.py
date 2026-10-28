"""Source-bound structured detector contract; no inference, IO or acceptance.

The host must verify extraction bytes before supplying source_bindings. Mapping
decisions come from an explicit normalizer, never text hints or worksheet labels.
canonical_joins reads the canonical ledger and checks source support and entity
relations. None of these structural checks is independent factual/legal review.
"""
from dataclasses import dataclass
import json

from .core import DETECTORS, TYPES, MAX_INPUT_BYTES, canonical, digest, evaluate, reference, sha
from .persistence import _preflight

VERSION = "structured-detector-adapter-v1"
NORMALIZED_SCHEMA = "structured-normalization-v1"
KNOWN_TYPES = set().union(*TYPES.values()) | {
    "training-roster", "policy", "contract", "correspondence", "invoice",
    "denial-or-extension", "other",
}


class ContractError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise ContractError(reason)


def _sources(bindings):
    require(type(bindings) is list, "source_bindings_required")
    _preflight(bindings, [], {}, {})
    require(len(canonical(bindings).encode()) <= MAX_INPUT_BYTES, "source_bindings_byte_bound")
    indexed = {}
    for row in bindings:
        require(set(row) == {"unit_id", "original_sha256", "artifact_sha256", "locator"},
                "source_binding_shape")
        identity = row["unit_id"]
        require(type(identity) is str and 0 < len(identity.strip()) <= 256,
                "source_unit_identity_invalid")
        require(identity not in indexed, "duplicate_source_unit_identity")
        require(sha(row["artifact_sha256"]), "source_artifact_hash_missing")
        reference(row)
        indexed[identity] = row
    return indexed


@dataclass(frozen=True)
class CanonicalJoins:
    """Snapshot returned by canonical_joins, not a record-controlled JSON flag."""
    source_bindings_sha256: str
    rows_json: str
    revision_sha256: str


def canonical_joins(connection, source_bindings):
    """Read-only canonical join admission, against caller-verified source bytes.

    Capture fresh under the host's ledger read snapshot on each evaluation. A
    caller must not cache this across join, agency or request revisions.
    """
    sources = _sources(source_bindings)
    supported = {(row["original_sha256"], canonical(row["locator"])) for row in sources.values()}
    records, revisions, revision_bytes, join_count = [], [], 0, 0

    def track(row):
        nonlocal revision_bytes
        _preflight([row], [], {}, {})
        size = len(canonical(row).encode())
        revision_bytes += size
        require(size <= 32768 and revision_bytes <= MAX_INPUT_BYTES, "join_revision_byte_bound")
        revisions.append(row)

    for subject in sorted({row["original_sha256"] for row in sources.values()}):
        cursor = connection.execute("SELECT * FROM joins WHERE original_sha256=? ORDER BY id", (subject,))
        columns = [column[0] for column in cursor.description]
        for values in cursor:
            join_count += 1
            require(join_count <= 10000, "join_row_bound")
            row = dict(zip(columns, values))
            require(type(row["evidence"]) is str and len(row["evidence"].encode()) <= 16384,
                    "typed_join_evidence_bound")
            track(row)
            if row["status"] != "typed":
                continue
            try:
                proof = json.loads(row["evidence"])
                _preflight([], [], {}, {"proof": proof})
                locator = proof.get("locator")
                if isinstance(locator, str):
                    locator = json.loads(locator)
                require((proof.get("source_sha256"), canonical(locator)) in supported,
                        "typed_join_support_missing")
                agency = connection.execute("SELECT * FROM agencies WHERE id=?", (row["agency_id"],)).fetchone()
                require(agency is not None, "typed_agency_missing")
                track({"agency": list(agency)})
                if row["request_id"] is not None:
                    request = connection.execute("SELECT agency_id FROM requests WHERE id=?",
                                                 (row["request_id"],)).fetchone()
                    require(request is not None and request[0] == row["agency_id"],
                            "typed_request_agency_mismatch")
                    track({"request_id": row["request_id"], "agency_id": request[0]})
            except (TypeError, ValueError, AttributeError, RecursionError) as error:
                if isinstance(error, ContractError):
                    raise
                raise ContractError("typed_join_evidence_invalid") from error
            records.append({"original_sha256": subject, "status": "typed", "agency_id": row["agency_id"]})
    return CanonicalJoins(digest(source_bindings), canonical(records), digest(revisions))


def _entry(status, reason, result=None):
    manifest = result["manifest"] if result else None
    # Never present rates/count signals computed from a partially qualified
    # population. The manifest retains per-event blockers and computed counts.
    hits = result["hits"] if result and status == "evaluated" else []
    return {"status": status, "reason": reason, "hits": hits, "hit_count": len(hits),
            "manifest": manifest, "suppressed_hit_count": len(result["hits"]) - len(hits) if result else 0,
            "publication_ready": False}


def evaluate_structured(*, subject_sha256, normalized=None, source_bindings=None,
                        joins=None, rules=None, config=None):
    """Evaluate all seven checks with explicit provenance and three statuses.

    normalized.units contains {record: CURRENT_CORE_UNIT, source: {unit_id,
    artifact_sha256}}. Complete means the normalizer classified every supplied
    extraction unit, including support; it does not certify extraction completeness.
    The host must supply the authoritative extraction inventory, including all
    support sources. Unknown mappings use record_type='unknown' and stay blocked.

    Rules retain the existing WP6 interface and exclusive effective_to boundary.
    Their source hashes must resolve in verified source_bindings. Supplied rule
    applicability never means that every applicable legal duty was enumerated.
    """
    require(sha(subject_sha256), "subject_hash_invalid")
    revisions = {"normalization_sha256": None, "source_bindings_sha256": None,
                 "joins_sha256": None, "rules_sha256": None, "config_sha256": None}
    try:
        require(type(normalized) is dict, "normalization_missing")
        require(type(rules) is dict and type(config) is dict, "rules_or_config_missing")
        # Bounds apply to the entire envelope, before hashing or copying it.
        _preflight(normalized.get("units"), [], rules, {"normalization": normalized, "config": config})
        sources = _sources(source_bindings)
        require(len(canonical([normalized, source_bindings, rules, config]).encode()) <= MAX_INPUT_BYTES,
                "structured_context_byte_bound")
        revisions.update(normalization_sha256=digest(normalized), source_bindings_sha256=digest(source_bindings),
                         rules_sha256=digest(rules), config_sha256=digest(config))
        require(normalized.get("schema") == NORMALIZED_SCHEMA, "normalization_schema_unsupported")
        require(type(normalized.get("version")) is str and bool(normalized["version"].strip()),
                "normalizer_version_missing")
        require(normalized.get("complete") is True, "normalization_incomplete")
        primary = [row for row in source_bindings if row["original_sha256"] == subject_sha256]
        require(primary, "primary_source_inventory_missing")
        require(normalized.get("source_inventory_sha256") == digest(source_bindings), "source_inventory_mismatch")
        units, mapped, first_records = [], set(), {}
        for item in normalized["units"]:
            require(set(item) == {"record", "source"}, "normalized_unit_shape")
            record, binding = item["record"], item["source"]
            require(type(record) is dict and type(binding) is dict and
                    set(binding) == {"unit_id", "artifact_sha256"}, "normalized_source_shape")
            source = sources.get(binding["unit_id"])
            require(source is not None and binding["artifact_sha256"] == source["artifact_sha256"],
                    "normalized_source_missing_or_changed")
            require(reference(record) == reference(source), "normalized_locator_mismatch")
            kind = record.get("record_type")
            require(kind in KNOWN_TYPES, "record_mapping_unknown")
            require(record["original_sha256"] == subject_sha256 or
                    kind not in set().union(*TYPES.values()),
                    "unrelated_support_record")
            # One extracted source unit has one canonical normalized record. An
            # exactly equal repeated mapping keeps core duplicate accounting; a
            # different record for the same unit is a reinterpretation, not data.
            record_text = canonical(record)
            require(first_records.setdefault(source["unit_id"], record_text) == record_text,
                    "normalized_source_record_conflict")
            mapped.add(source["unit_id"])
            units.append(record)  # Do not put mapping metadata into core event identity.
        require(set(sources) == mapped, "normalization_inventory_incomplete")
        require(type(rules.get("version")) is str and bool(rules["version"].strip()), "rules_version_missing")
        require(isinstance(joins, CanonicalJoins), "canonical_join_snapshot_missing")
        require(joins.source_bindings_sha256 == digest(source_bindings), "canonical_join_sources_changed")
        revisions["joins_sha256"] = joins.revision_sha256
        join_rows = json.loads(joins.rows_json)
        # Reuse the current rules, without translating a law package or making
        # up a default policy, holiday table, conduct date or agency identity.
        bound_rules = {source["original_sha256"] for source in sources.values()}
        missing_rules = {entry.get("detector") for entry in rules.get("entries", [])
                         if entry.get("source_sha256") not in bound_rules}
        unresolved_rules = {entry.get("detector") for entry in rules.get("entries", [])
                            if entry.get("version_verified") is not True or entry.get("applicability") != "yes"}
        structured = {}
        for name in DETECTORS:
            if name in missing_rules:
                structured[name] = _entry("blocked", "rule_source_unbound")
                continue
            if name in unresolved_rules:
                structured[name] = _entry("blocked", "rule_version_or_applicability_unresolved")
                continue
            result = evaluate(name, units, join_rows, rules, config)
            counts = result["manifest"]["counts"]
            if counts["blocked"]:
                status, reason = "blocked", "required_context_unresolved"
            elif counts["evaluated"]:
                status, reason = "evaluated", "supplied_context_evaluated"
            else:
                status, reason = "inapplicable", ("no_matching_type_in_complete_normalization" if not counts["eligible"]
                                                  else "outside_supplied_rule_intervals")
            structured[name] = _entry(status, reason, result)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        # Stable reasons only; never copy malformed source values into errors.
        reason = str(error) if isinstance(error, ContractError) else "invalid_structured_context"
        structured = {name: _entry("blocked", reason) for name in DETECTORS}
    output = {"schema": VERSION, "subject_sha256": subject_sha256, "revisions": revisions,
              "structured": structured, "acceptance": "supplied_evidence_triage_only",
              "publication_ready": False, "stage_promotions": 0}
    output["result_sha256"] = digest(output)
    # The adapter never returns aliases into trusted snapshots or caller data.
    return json.loads(canonical(output))
