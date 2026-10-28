"""Deterministic, resumable accounting for semantic processing of text views.

This module performs no extraction, model calls, review, or stage promotion. Its
public JSON-compatible API is ``build_manifest``, ``render_chunk``,
``initial_checkpoint``, ``validate_checkpoint``, ``record_attempt``,
``pending_chunks`` and ``coverage_summary``. Persist returned checkpoints
atomically in the caller's existing private ledger; an interrupted call before
persistence can safely replay the same receipt.

The inventory MUST enumerate all authoritative source units, without a text
selection limit, including hidden, empty, and unreadable units. Its completeness
and ``source_manifest_sha256`` must be established by the extraction adapter,
not a model. Missing metadata is represented by unknown view lengths, never by
dropping the unit. A duplicate identity is an error. ``inventory_complete`` is
false unless the caller explicitly establishes that inventory's completeness.
For the records ledger, use the accepted extract content's import ID and the
validated current extraction manifest's ordered ``unit_ids``. The units table
can retain historical imports: selecting every row for an original is not an
authoritative inventory. Bind this accepted extraction snapshot's manifest hash.

``selected_text`` maps opaque SHA-256 unit IDs to already-redacted text. Optional
inventory ``view_chars``/``view_sha256`` describe these redacted views, NOT raw
extraction text. All ranges are half-open Python string character offsets into
those views. Redaction can change length; these are never original-file offsets.
``context_sha256`` binds redaction, model, prompt and acceptance-policy versions.

Only ``render_chunk`` output should enter the model's record-text field. Its
ASCII JSON payload (including escaping and metadata) fits ``max_chunk_chars``;
callers must budget other prompt fields separately and must not truncate this
payload. Returned visibility describes emission, not model understanding.
``record_attempt`` accepts ranges only after caller-side response/review
validation; extraction, dispatch and a model's claimed counts cannot accept
ranges. Receipts are externally verified evidence references, not authenticated
by this module. Self-hashes detect accidental drift, not hostile re-signing.

Even acceptance of every range establishes only recorded processing coverage.
It never establishes substantive understanding, factual/legal review, or
publication clearance. Ledger-compatible receipt scope is always ``selected``.
"""

from copy import deepcopy
import hashlib
import json
import re


VERSION = "semantic-coverage-v1"
CHECKPOINT_VERSION = "semantic-progress-v1"
PAYLOAD_VERSION = "semantic-text-v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha(value, name):
    _require(isinstance(value, str) and _SHA.fullmatch(value), name)
    return value


def _integer(value, minimum, name):
    _require(type(value) is int and value >= minimum, name)
    return value


def _signed(value, field):
    result = deepcopy(value)
    result[field] = _hash(_canonical(value))
    return result


def _verify_hash(value, field):
    _require(isinstance(value, dict), "invalid_" + field)
    _sha(value.get(field), field)
    body = {k: v for k, v in value.items() if k != field}
    _require(value[field] == _hash(_canonical(body)), field + "_mismatch")


def _range(unit_id, start, end):
    return {"unit_id": unit_id, "start": start, "end": end}


def _payload(ranges, texts, indexes):
    return _canonical({"schema": PAYLOAD_VERSION, "ranges": [
        {"unit": indexes[r["unit_id"]], "start": r["start"], "end": r["end"],
         "text": texts[r["unit_id"]][r["start"]:r["end"]]} for r in ranges]})


def _normalize_inventory(inventory, selected_text):
    _require(isinstance(selected_text, dict), "selected_text_mapping")
    units, seen = [], set()
    for row in inventory:
        _require(isinstance(row, dict), "inventory_unit_object")
        identity = _sha(row.get("unit_id"), "inventory_unit_id")
        _require(identity not in seen, "duplicate_inventory_unit")
        seen.add(identity)
        locator = row.get("locator")
        _require((isinstance(locator, (dict, str)) and bool(locator)) or
                 (locator is None and isinstance(row.get("blocked_reason"), str) and row["blocked_reason"].strip()),
                 "inventory_locator")
        # Copy only documented metadata; original text is never retained here.
        unit = {"unit_id": identity, "locator": deepcopy(locator),
                "unit_type": row.get("unit_type"), "hidden": row.get("hidden", False),
                "blocked_reason": row.get("blocked_reason"),
                "view_chars": row.get("view_chars"), "view_sha256": row.get("view_sha256"),
                "selected": identity in selected_text}
        _require(type(unit["hidden"]) is bool, "inventory_hidden")
        for name in ("unit_type", "blocked_reason"):
            _require(unit[name] is None or (isinstance(unit[name], str) and unit[name].strip()),
                     "inventory_" + name)
        if unit["view_chars"] is not None or unit["view_sha256"] is not None:
            _integer(unit["view_chars"], 0, "inventory_view_chars")
            _sha(unit["view_sha256"], "inventory_view_sha256")
        if unit["selected"]:
            text = selected_text[identity]
            _require(isinstance(text, str), "selected_text_string")
            if unit["view_chars"] is not None:
                _require((unit["view_chars"], unit["view_sha256"]) == (len(text), _hash(text)),
                         "selected_text_view_mismatch")
            unit.update(view_chars=len(text), view_sha256=_hash(text))
        units.append(unit)
    _require(set(selected_text).issubset(seen), "selected_unit_outside_inventory")
    return units


def build_manifest(source_sha256, inventory, selected_text, *, source_manifest_sha256,
                   extraction_version, context_sha256, inventory_complete=False,
                   max_chunk_chars=12000, max_chunk_ranges=128):
    """Build from ALL unit metadata and a separately bounded redacted selection.

    Inventory entries require ``unit_id`` (SHA-256) and ``locator`` (nonempty
    dict/string, or None with an explicit ``blocked_reason`` for a missing or
    malformed canonical row); optional fields are ``unit_type``, ``hidden``,
    ``blocked_reason``, ``view_chars`` and ``view_sha256``. Preserve source order
    for deterministic chunk order. Unknown view metadata is allowed, including
    for unreadable units. Supplying text establishes only its view hash/length.
    No source count or character count is derived from model assertions.
    """
    _sha(source_sha256, "source_sha256")
    _sha(source_manifest_sha256, "source_manifest_sha256")
    _sha(context_sha256, "context_sha256")
    _require(isinstance(extraction_version, str) and extraction_version.strip(), "extraction_version")
    _require(type(inventory_complete) is bool, "inventory_complete")
    _integer(max_chunk_chars, 256, "max_chunk_chars")
    _integer(max_chunk_ranges, 1, "max_chunk_ranges")
    units = _normalize_inventory(inventory, selected_text)
    indexes = {unit["unit_id"]: index for index, unit in enumerate(units)}
    chunks, ranges = [], []

    def flush():
        if not ranges:
            return
        payload = _payload(ranges, selected_text, indexes)
        body = {"ordinal": len(chunks), "ranges": deepcopy(ranges),
                "payload_chars": len(payload), "payload_sha256": _hash(payload)}
        chunks.append(_signed(body, "chunk_id"))
        ranges.clear()

    for unit in units:
        if not unit["selected"]:
            continue
        identity, size = unit["unit_id"], unit["view_chars"]
        start = 0
        while start < size:
            if len(ranges) >= max_chunk_ranges:
                flush()
            # One character costs at least one serialized character. Binary
            # search also accounts for escaped Unicode, controls, and offsets.
            low, high = start + 1, min(size, start + max_chunk_chars)
            best = None
            while low <= high:
                end = (low + high) // 2
                candidate = _range(identity, start, end)
                if len(_payload([*ranges, candidate], selected_text, indexes)) <= max_chunk_chars:
                    best, low = candidate, end + 1
                else:
                    high = end - 1
            if best is None:
                _require(bool(ranges), "chunk_budget_cannot_fit_character")
                flush()
                continue
            ranges.append(best)
            start = best["end"]
            if start < size:
                flush()
    flush()
    body = {"schema": VERSION, "source_sha256": source_sha256,
            "source_manifest_sha256": source_manifest_sha256,
            "extraction_version": extraction_version, "context_sha256": context_sha256,
            "inventory_complete": inventory_complete,
            "inventory_sha256": _hash(_canonical(units)), "units": units,
            "max_chunk_chars": max_chunk_chars, "max_chunk_ranges": max_chunk_ranges,
            "chunks": chunks}
    return _signed(body, "manifest_sha256")


def _normalize_ranges(ranges, units, *, reject_overlap=True):
    _require(isinstance(ranges, list), "ranges_list")
    unique = set()
    for row in ranges:
        _require(isinstance(row, dict) and set(row) == {"unit_id", "start", "end"}, "range_shape")
        identity = row["unit_id"]
        _require(isinstance(identity, str) and identity in units, "range_unit_outside_inventory")
        start = _integer(row["start"], 0, "range_start")
        end = _integer(row["end"], 1, "range_end")
        size = units[identity]["view_chars"]
        _require(size is not None and start < end <= size, "range_outside_view")
        unique.add((identity, start, end))
    merged = []
    for identity, start, end in sorted(unique):
        if merged and merged[-1]["unit_id"] == identity and start <= merged[-1]["end"]:
            _require(not reject_overlap or start == merged[-1]["end"], "overlapping_ranges")
            merged[-1]["end"] = max(merged[-1]["end"], end)
        else:
            merged.append(_range(identity, start, end))
    return merged


def _subtract(ranges, covered):
    """Return range differences, retaining half-open offset semantics."""
    by_unit = {}
    for row in covered:
        by_unit.setdefault(row["unit_id"], []).append(row)
    missing = []
    for row in ranges:
        start, end = row["start"], row["end"]
        for found in by_unit.get(row["unit_id"], []):
            if found["end"] <= start or found["start"] >= end:
                continue
            if found["start"] > start:
                missing.append(_range(row["unit_id"], start, found["start"]))
            start = max(start, found["end"])
            if start >= end:
                break
        if start < end:
            missing.append(_range(row["unit_id"], start, end))
    return missing


def _checked_manifest(manifest):
    _verify_hash(manifest, "manifest_sha256")
    _require(manifest.get("schema") == VERSION, "manifest_version")
    for name in ("source_sha256", "source_manifest_sha256", "context_sha256"):
        _sha(manifest.get(name), name)
    _require(type(manifest.get("inventory_complete")) is bool, "inventory_complete")
    _require(isinstance(manifest.get("extraction_version"), str) and
             manifest["extraction_version"].strip(), "extraction_version")
    _integer(manifest.get("max_chunk_chars"), 256, "max_chunk_chars")
    _integer(manifest.get("max_chunk_ranges"), 1, "max_chunk_ranges")
    _require(isinstance(manifest.get("units"), list), "inventory_units")
    units = {}
    for row in manifest["units"]:
        # Reuse the inventory contract without requiring retained text bytes.
        normalized = _normalize_inventory([row], {})[0]
        _require(type(row.get("selected")) is bool, "unit_selected")
        normalized["selected"] = row["selected"]
        _require(row == normalized, "inventory_metadata")
        _require(row["unit_id"] not in units, "duplicate_inventory_unit")
        _require(not row["selected"] or row["view_chars"] is not None, "selected_view_unknown")
        units[row["unit_id"]] = row
    _require(manifest.get("inventory_sha256") == _hash(_canonical(manifest["units"])),
             "inventory_sha256_mismatch")
    _require(isinstance(manifest.get("chunks"), list), "manifest_chunks")
    planned = []
    for ordinal, chunk in enumerate(manifest["chunks"]):
        _verify_hash(chunk, "chunk_id")
        _require(chunk.get("ordinal") == ordinal and type(chunk["ordinal"]) is int, "chunk_ordinal")
        _require(isinstance(chunk.get("ranges"), list) and
                 1 <= len(chunk["ranges"]) <= manifest["max_chunk_ranges"], "chunk_ranges")
        canonical = _normalize_ranges(chunk["ranges"], units)
        _require(sum(r["end"] - r["start"] for r in canonical) ==
                 sum(r["end"] - r["start"] for r in chunk["ranges"]), "duplicate_chunk_range")
        _integer(chunk.get("payload_chars"), 1, "chunk_payload_chars")
        _require(chunk["payload_chars"] <= manifest["max_chunk_chars"], "chunk_payload_budget")
        _sha(chunk.get("payload_sha256"), "chunk_payload_sha256")
        planned.extend(chunk["ranges"])
    normalized = _normalize_ranges(planned, units)
    _require(sum(r["end"] - r["start"] for r in planned) ==
             sum(r["end"] - r["start"] for r in normalized), "duplicate_planned_range")
    expected = [_range(u["unit_id"], 0, u["view_chars"]) for u in units.values()
                if u["selected"] and u["view_chars"]]
    _require(normalized == sorted(expected, key=lambda r: r["unit_id"]), "planned_view_coverage")
    return units, {chunk["chunk_id"]: chunk for chunk in manifest["chunks"]}


def _known_chunk(chunk_id, chunks, reason):
    """Refuse a non-string or unplanned chunk identity with ``reason``.

    The type check precedes hash-map membership, so unhashable restored or
    caller values are typed refusals rather than ``TypeError``. IDs are never
    coerced or mapped to an ordinal.
    """
    _require(isinstance(chunk_id, str) and chunk_id in chunks, reason)
    return chunk_id


def render_chunk(manifest, chunk_id, selected_text):
    """Return exact ``payload``, ``payload_sha256`` and ``visible_ranges``.

    Payloads expose only integer ``unit`` references (stable inventory indexes),
    character offsets and redacted text. ``unit_references`` and
    ``visible_ranges`` are local-only maps; never send these source identities
    to a model. Restore model references with the existing privacy owner API.
    Only views needed for this chunk must be supplied. Full view hashes are
    checked before slicing, so offsets cannot silently address changed text.
    The result is an emission receipt, never evidence of semantic acceptance.
    """
    units, chunks = _checked_manifest(manifest)
    _known_chunk(chunk_id, chunks, "unknown_chunk")
    _require(isinstance(selected_text, dict), "selected_text_mapping")
    chunk = chunks[chunk_id]
    for row in chunk["ranges"]:
        unit = units[row["unit_id"]]
        text = selected_text.get(row["unit_id"])
        _require(isinstance(text, str) and len(text) == unit["view_chars"] and
                 _hash(text) == unit["view_sha256"], "selected_text_view_mismatch")
    indexes = {unit["unit_id"]: index for index, unit in enumerate(manifest["units"])}
    payload = _payload(chunk["ranges"], selected_text, indexes)
    _require(len(payload) == chunk["payload_chars"] and _hash(payload) == chunk["payload_sha256"],
             "chunk_payload_mismatch")
    return {"payload": payload, "payload_sha256": chunk["payload_sha256"],
            "visible_ranges": deepcopy(chunk["ranges"]),
            "unit_references": [{"unit": indexes[identity], "unit_id": identity}
                                for identity in dict.fromkeys(r["unit_id"] for r in chunk["ranges"])]}


def _binding(manifest):
    return {name: manifest[name] for name in
            ("source_sha256", "source_manifest_sha256", "extraction_version", "context_sha256", "manifest_sha256")}


def initial_checkpoint(manifest):
    """Start with no accepted or observed ranges; extraction accepts nothing."""
    _checked_manifest(manifest)
    return _signed(dict(schema=CHECKPOINT_VERSION, **_binding(manifest), attempts=[]), "checkpoint_sha256")


def _attempt(chunk, units, *, visible_ranges, accepted_ranges, receipt_sha256, completed):
    _sha(receipt_sha256, "receipt_sha256")
    _require(type(completed) is bool, "attempt_completed")
    visible = _normalize_ranges(visible_ranges, units)
    accepted = _normalize_ranges(accepted_ranges, units)
    planned = _normalize_ranges(chunk["ranges"], units)
    _require(not _subtract(visible, planned), "visible_outside_chunk")
    _require(not _subtract(accepted, visible), "accepted_outside_visible")
    _require(completed or not accepted, "interrupted_attempt_cannot_accept")
    return {"chunk_id": chunk["chunk_id"], "visible_ranges": visible,
            "accepted_ranges": accepted, "receipt_sha256": receipt_sha256, "completed": completed}


def validate_checkpoint(manifest, checkpoint):
    """Validate JSON restored after interruption and return a defensive copy.

    Changing source, extraction snapshot/version, selection, chunk budget or
    processing context invalidates progress. Accepted ranges must be backed by
    a completed attempt and fit that attempt's actual visible range union.
    """
    units, chunks = _checked_manifest(manifest)
    _verify_hash(checkpoint, "checkpoint_sha256")
    _require(checkpoint.get("schema") == CHECKPOINT_VERSION, "checkpoint_version")
    _require(all(checkpoint.get(k) == v for k, v in _binding(manifest).items()), "checkpoint_binding")
    _require(set(checkpoint) == {"schema", "attempts", "checkpoint_sha256", *_binding(manifest)},
             "checkpoint_shape")
    _require(isinstance(checkpoint.get("attempts"), list), "checkpoint_attempts")
    receipts = set()
    for attempt in checkpoint["attempts"]:
        _require(isinstance(attempt, dict), "checkpoint_chunk")
        _known_chunk(attempt.get("chunk_id"), chunks, "checkpoint_chunk")
        _require(set(attempt) == {"chunk_id", "visible_ranges", "accepted_ranges", "receipt_sha256", "completed"},
                 "checkpoint_attempt_shape")
        normalized = _attempt(chunks[attempt["chunk_id"]], units,
                              **{k: v for k, v in attempt.items() if k != "chunk_id"})
        _require(attempt == normalized, "checkpoint_attempt_ranges")
        _require(attempt["receipt_sha256"] not in receipts, "duplicate_checkpoint_receipt")
        receipts.add(attempt["receipt_sha256"])
    return deepcopy(checkpoint)


def record_attempt(manifest, checkpoint, chunk_id, *, visible_ranges, accepted_ranges,
                   receipt_sha256, completed=True):
    """Append verified progress; exact receipt retries are idempotent.

    Exact duplicate ranges are deduplicated; unequal overlapping declarations
    within an attempt are rejected. Repeated attempts may overlap earlier work,
    but summaries count their union once. ``completed=False`` records observed
    emission after interruption and must not contain any accepted ranges.
    Empty/omitted accepted ranges remain queued even after a completed response.
    """
    result = validate_checkpoint(manifest, checkpoint)
    units, chunks = _checked_manifest(manifest)
    _known_chunk(chunk_id, chunks, "unknown_chunk")
    attempt = _attempt(chunks[chunk_id], units, visible_ranges=visible_ranges,
                       accepted_ranges=accepted_ranges, receipt_sha256=receipt_sha256, completed=completed)
    existing = next((a for a in result["attempts"] if a["receipt_sha256"] == receipt_sha256), None)
    if existing is not None:
        _require(existing == attempt, "receipt_replay_conflict")
        return result
    result["attempts"].append(attempt)
    del result["checkpoint_sha256"]
    return _signed(result, "checkpoint_sha256")


def _progress(manifest, checkpoint):
    checkpoint = validate_checkpoint(manifest, checkpoint)
    units = {u["unit_id"]: u for u in manifest["units"]}
    visible = _normalize_ranges([r for a in checkpoint["attempts"] for r in a["visible_ranges"]],
                                units, reject_overlap=False)
    accepted = _normalize_ranges([r for a in checkpoint["attempts"] for r in a["accepted_ranges"]],
                                 units, reject_overlap=False)
    return visible, accepted


def pending_chunks(manifest, checkpoint):
    """Return ordered chunk IDs with any unaccepted range, including interruptions."""
    _, accepted = _progress(manifest, checkpoint)
    return [c["chunk_id"] for c in manifest["chunks"] if _subtract(c["ranges"], accepted)]


def coverage_summary(manifest, checkpoint):
    """Return distinct inventory, visibility and accepted-coverage counts.

    Unit counts require that unit's entire nonempty view. Unknown, empty,
    blocked and unselected units remain in the denominator and produce gaps.
    ``all_units_accepted`` is a bookkeeping result, NOT understanding/review.
    """
    visible, accepted = _progress(manifest, checkpoint)
    visible_units, accepted_units, gaps = 0, 0, []
    for unit in manifest["units"]:
        identity, size = unit["unit_id"], unit["view_chars"]
        reasons = []
        whole = [_range(identity, 0, size)] if size else []
        unseen, unaccepted = _subtract(whole, visible), _subtract(whole, accepted)
        if whole and not unseen:
            visible_units += 1
        if whole and not unaccepted and not unit["blocked_reason"]:
            accepted_units += 1
        if unit["blocked_reason"]:
            reasons.append("source_unit_blocked")
        if not unit["selected"]:
            reasons.append("unit_not_selected")
        if size is None:
            reasons.append("unknown_model_text")
        elif size == 0:
            reasons.append("empty_model_text")
        if unseen:
            reasons.append("text_not_visible")
        if unaccepted:
            reasons.append("text_not_accepted")
        if reasons:
            gaps.append({"unit_id": identity, "reasons": reasons,
                         "blocked_reason": unit["blocked_reason"],
                         "unseen_ranges": unseen, "unaccepted_ranges": unaccepted})
    total = len(manifest["units"])
    all_accepted = bool(total and manifest["inventory_complete"] and accepted_units == total and not gaps)
    return {"schema": VERSION, **_binding(manifest),
            "inventory_complete": manifest["inventory_complete"], "inventory_total": total,
            "selected_units": sum(u["selected"] for u in manifest["units"]),
            "known_view_chars": sum(u["view_chars"] or 0 for u in manifest["units"]),
            "unknown_view_units": sum(u["view_chars"] is None for u in manifest["units"]),
            "model_visible_units": visible_units, "accepted_units": accepted_units,
            "model_visible_chars": sum(r["end"] - r["start"] for r in visible),
            "accepted_chars": sum(r["end"] - r["start"] for r in accepted),
            "all_units_accepted": all_accepted, "status": "accepted_scope_complete" if all_accepted else "partial",
            "semantic_understanding_established": False,
            "receipt_coverage": {"denominator": {"kind": "items", "total": total},
                                 "covered": accepted_units, "scope": "selected"},
            "gaps": gaps,
            "inventory_gaps": [] if manifest["inventory_complete"] else ["authoritative_inventory_incomplete"]}
