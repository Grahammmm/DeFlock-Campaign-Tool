"""Deterministic, conservative kind candidates from admitted extraction copies.

No extraction, inference, filesystem access or stage acceptance. Callers supply
an independently pinned original identity and exact format-extraction-v1 bytes.
Filename/MIME metadata is caller-supplied (not authenticated by the extraction
receipt); its canonical digest is retained, and it cannot alone establish kind.
Kind is a routing candidate, NEVER a mapping into CURRENT_CORE_UNIT or evidence
of actual usage. F-046's handoff references the exact serialized receipt hash;
this module neither changes that envelope nor consumes canonical stores.

Native folder.py workbook receipts carry a cached-values/objects review issue;
their sheet/header cues remain informational and the result stays other/low.
This classifier does not qualify workbooks or remove that upstream hold.
Opening titles use native pdf_page page1, text_line line1 and the first emitted
main-part docx_paragraph. Headers use csv_header record1 and workbook_row row1.
Unknown unit kinds (including the non-native "text") supply no content cues.
"""
import hashlib
import json
import math
import re

VERSION = "deterministic-document-kind-v1"
SCHEMA = "records-understanding-kind-v1"
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
MAX_UNITS = 5000
MAX_TEXT_BYTES = 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
# Exact normalized titles, not substring/keyword guesses. No precedence rule
# silently resolves conflicting kinds. New aliases require versioned review.
TITLES = {
    "search-log": ("search log", "query log"),
    "sharing-list": ("sharing list", "sharing permissions"),
    "network-search": ("network search", "network search log"),
    "retention-setting": ("retention setting", "retention settings"),
    "audit": ("audit log", "access audit"),
    "request": ("public records request", "records request"),
    "training-roster": ("training roster", "training attendance"),
    "policy": ("policy", "use policy", "alpr policy"),
    "contract": ("contract", "service agreement", "purchase agreement"),
    "correspondence": ("correspondence", "email correspondence"),
    "invoice": ("invoice", "fee invoice"),
    "denial-or-extension": ("records request denial", "records request extension"),
}
KINDS = frozenset(TITLES) | {"other"}
# Native workbook_row text contains dict cells; csv_header contains strings.
# These signatures are recognition rules, not a normalized-unit contract.
HEADERS = {
    "search-log": {"event_id", "searched_at", "purpose"},
    "sharing-list": {"agency_id", "recipient_agency_id", "sharing_enabled"},
    "network-search": {"event_id", "network_id", "searched_at"},
    "retention-setting": {"agency_id", "retention_days"},
    "audit": {"event_id", "audit_action", "actor_id"},
    "request": {"request_id", "received_at", "request_status"},
    "training-roster": {"user_id", "training_completed_at"},
}


class KindError(ValueError):
    """Stable typed refusal; no raw source text in diagnostics."""


def require(condition, code):
    if not condition:
        raise KindError(code)


def canonical_bytes(value):
    """File bytes match the parent contract's canonical JSON + single LF."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _parse_float(token):
    # A finite-looking token such as 1e309 silently decodes to inf; refuse it
    # exactly like explicit NaN/Infinity. Underflow to a finite value is kept.
    value = float(token)
    if not math.isfinite(value):
        raise KindError("nonfinite_json")
    return value


def _parse_int(token):
    try:
        return int(token)
    except ValueError:  # Interpreter digit limit; never echo the token.
        raise KindError("invalid_json") from None


def _reject_lone_surrogates(value):
    """Refuse unpaired surrogates in every decoded string and key.

    Iterative, so traversal is bounded by the already size-capped decoded
    value rather than recursion depth. Paired escapes (astral characters) are
    combined by the JSON decoder and are accepted.
    """
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is str:
            require(_LONE_SURROGATE.search(item) is None, "invalid_unicode_json")
        elif type(item) is dict:
            stack.extend(item)
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    def nonfinite(_):
        raise KindError("nonfinite_json")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite,
                           parse_float=_parse_float, parse_int=_parse_int)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise KindError("invalid_json") from None
    _reject_lone_surrogates(value)
    return value


def _title(value):
    return " ".join(re.sub(r"[-_]+", " ", value.casefold()).split()).strip(" :")


def classify(*, run_id, artifact_sha256, derived_receipt_sha256,
             derived_receipt, filename="", mime=""):
    """Return a source-bound kind receipt, or fail closed with KindError.

    Expected hashes/run_id must come from the admitting producer, not be
    computed from untrusted bytes and then passed back as if independently
    trusted. No original content is opened. This checks receipt identity and
    cue provenance, NOT preservation, OCR qualification or semantic truth.
    """
    require(type(run_id) is str and 0 < len(run_id.strip()) <= 256, "run_id_required")
    for value, code in ((artifact_sha256, "artifact_hash_required"),
                        (derived_receipt_sha256, "derived_receipt_hash_required")):
        require(type(value) is str and _SHA.fullmatch(value) is not None, code)
    require(type(derived_receipt) is bytes and len(derived_receipt) <= MAX_RECEIPT_BYTES,
            "derived_receipt_bound")
    require(sha256(derived_receipt) == derived_receipt_sha256, "derived_receipt_hash_mismatch")
    require(type(filename) is str and len(filename) <= 256 and
            not any(c in filename for c in "/\\\x00\r\n"), "filename_invalid")
    require(type(mime) is str and len(mime) <= 256 and
            not any(ord(c) < 32 for c in mime), "mime_invalid")
    receipt = decode(derived_receipt)
    require(type(receipt) is dict and receipt.get("schema") == "format-extraction-v1",
            "derived_receipt_schema")
    require(receipt.get("original_sha256") == artifact_sha256 and
            receipt.get("original_changed") is False, "artifact_receipt_mismatch")
    require(receipt.get("review_status") == "not_reviewed", "receipt_review_claim")
    require(receipt.get("status") in ("complete", "partial", "blocked"), "upstream_status_invalid")
    units, issues = receipt.get("units"), receipt.get("issues")
    require(type(units) is list and len(units) <= MAX_UNITS and
            type(issues) is list and type(receipt.get("pages")) is list, "receipt_units_shape")
    matches = {}
    def add(kind, channel, rule, source=None):
        row = {"kind": kind, "channel": channel, "rule": rule}
        if source is not None:
            # Retain only the canonical raw-unit hash, never raw cue text/names.
            row["unit_sha256"] = sha256(canonical_bytes(source))
        matches[canonical_bytes(row)] = row
    def title(value, channel, source=None):
        folded = _title(value)
        for kind, labels in TITLES.items():
            if folded in labels:
                add(kind, channel, folded.replace(" ", "-"), source)
    title(filename.rsplit(".", 1)[0] if "." in filename else filename, "filename")
    if mime.split(";", 1)[0].strip().casefold() == "message/rfc822":
        add("correspondence", "mime", "message-rfc822")
    for unit in units:
        require(type(unit) is dict and set(unit) == {"kind", "locator", "text", "data"},
                "unit_shape")
        require(type(unit["kind"]) is str and type(unit["locator"]) is dict and
                type(unit["data"]) is dict and type(unit["text"]) is str, "unit_payload_shape")
        require(len(unit["text"].encode("utf-8")) <= MAX_TEXT_BYTES, "unit_text_bound")
    # DOCX emits only nonempty paragraphs and numbers them before filtering.
    # Pick the first emitted main-document paragraph by locator, not input order;
    # header/footer/comment parts cannot establish the document's opening title.
    first_paragraph = min((u["locator"]["paragraph"] for u in units
                           if u["kind"] == "docx_paragraph"
                           and u["locator"].get("part") == "word/document.xml"
                           and type(u["locator"].get("paragraph")) is int
                           and u["locator"]["paragraph"] > 0), default=None)
    for unit in units:
        locator = unit["locator"]
        opening = (
            (unit["kind"] == "pdf_page" and type(locator.get("page")) is int and locator["page"] == 1)
            or (unit["kind"] == "text_line" and type(locator.get("line")) is int and locator["line"] == 1)
            or (unit["kind"] == "docx_paragraph" and locator.get("part") == "word/document.xml"
                and type(locator.get("paragraph")) is int and locator["paragraph"] == first_paragraph)
        )
        if opening:
            lines = [line.strip() for line in unit["text"].splitlines() if line.strip()]
            if lines:
                title(lines[0], "first_page", unit)
        if unit["kind"] in ("workbook_sheet", "workbook_row"):
            sheet = locator.get("sheet")
            require(type(sheet) is str and 0 < len(sheet) <= 1024, "sheet_locator_invalid")
            title(sheet, "sheet", unit)
        header = None
        if unit["kind"] == "workbook_row" and type(locator.get("row")) is int and locator["row"] == 1:
            cells = decode(unit["text"])
            require(type(cells) is list and all(
                type(c) is dict and set(c) == {"cell", "value", "type"}
                and type(c["cell"]) is str and type(c["type"]) is str
                and type(c["value"]) in (str, int, float, bool, type(None))
                for c in cells), "header_shape")
            # Ordinary data, blanks, formulas and mixed rows are not headers.
            # Do not salvage a partial signature by dropping non-string cells.
            if all(type(c["value"]) is str and c["type"] in ("s", "inlineStr") for c in cells):
                header = [c["value"] for c in cells]
        if unit["kind"] == "csv_header" and type(locator.get("record")) is int and locator["record"] == 1:
            header = decode(unit["text"])
            require(type(header) is list and all(type(c) is str for c in header), "header_shape")
        if header is not None:
            names = {c.strip().casefold() for c in header}
            for kind, fields in HEADERS.items():
                if fields <= names:
                    add(kind, "header", "header-" + kind, unit)
    ordered = [matches[key] for key in sorted(matches)]
    candidates = sorted({row["kind"] for row in ordered})
    kind, confidence, reason = "other", "low", "no_decisive_cues"
    if receipt["status"] != "complete" or issues:
        reason = "upstream_not_clear"
    elif len(candidates) > 1:
        reason = "conflicting_cues"
    elif candidates:
        channels = {row["channel"] for row in ordered}
        if channels & {"first_page", "sheet", "header"}:
            kind = candidates[0]
            confidence = "high" if len(channels) >= 2 else "medium"
            reason = "explicit_cues"
        else:
            reason = "metadata_only"
    return {"schema": SCHEMA, "classifier_version": VERSION, "run_id": run_id,
            "artifact_sha256": artifact_sha256, "derived_receipt_sha256": derived_receipt_sha256,
            "metadata_sha256": sha256(canonical_bytes({"filename": filename, "mime": mime})),
            "kind": kind, "confidence": confidence, "reason": reason,
            "matched_cues": ordered, "upstream_status": receipt["status"],
            "review_required": True, "qualified": False, "activation_allowed": False,
            "publication_ready": False, "stage_promotions": 0,
            "semantic_assessment": "NOT_ASSESSED"}
