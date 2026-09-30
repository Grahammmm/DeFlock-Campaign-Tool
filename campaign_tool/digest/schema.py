"""Digest JSON shape and validation (docs/CONTRACTS.md "Confidence labels").

The digest is private analysis. Every conclusion carries ``confidence`` in
``verified | likely | needs_attorney_review`` and at least one source with
the original's sha256, an exact locator and, for a legal duty, a rule_id.
Validation reuses the JSON-schema subset in :mod:`campaign_tool.law`.
"""
from ..law import validate

CONFIDENCE = ["verified", "likely", "needs_attorney_review"]
HEX64 = "^[0-9a-f]{64}$"

SOURCE = {
    "type": "object",
    "required": ["sha256", "locator"],
    "additionalProperties": False,
    "properties": {
        "sha256": {"type": "string", "pattern": HEX64},
        "locator": {"type": "object"},
        "rule_id": {"type": ["string", "null"]},
    },
}
LOCATED_TEXT = {
    "type": "object",
    "required": ["text"],
    "additionalProperties": False,
    "properties": {"text": {"type": "string", "minLength": 1}, "locator": {"type": ["object", "null"]}},
}
HIT = {
    "type": "object",
    "required": ["detector", "detector_version", "rule_id", "citation", "locator", "excerpt", "kind", "detail"],
    "additionalProperties": False,
    "properties": {
        "detector": {"type": "string"},
        "detector_version": {"type": "string"},
        "rule_id": {"type": ["string", "null"]},
        "citation": {"type": ["string", "null"]},
        "locator": {"type": "object"},
        "excerpt": {"type": "string"},
        "kind": {"type": "string", "enum": ["mention", "absence", "comparison"]},
        "detail": {"type": "object"},
    },
}
DIGEST_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "sha256", "jurisdiction", "law_package_version", "privacy_tier",
                 "redaction_count", "redaction_counts", "model_id", "detector_version", "scope", "actors",
                 "dates", "duties", "statements", "omissions", "counterevidence", "hits", "conclusions",
                 "limitations"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": 1},
        "sha256": {"type": "string", "pattern": HEX64},
        "jurisdiction": {"type": "string", "pattern": "^[a-z]{2}-[a-z]{2}$"},
        "law_package_version": {"type": "string", "minLength": 1},
        "privacy_tier": {"type": "string", "enum": ["redacted_cloud", "strict_local"]},
        "redaction_count": {"type": "integer", "minimum": 0},
        "redaction_counts": {"type": "object"},
        "model_id": {"type": ["string", "null"]},
        "detector_version": {"type": "string"},
        "scope": {"type": "string"},
        "actors": {"type": "array", "items": {"type": "string"}},
        "dates": {"type": "array", "items": {
            "type": "object", "required": ["date"], "additionalProperties": False,
            "properties": {"date": {"type": "string"}, "locator": {"type": ["object", "null"]}, "note": {"type": "string"}}}},
        "duties": {"type": "array", "items": {
            "type": "object", "required": ["rule_id", "locators"], "additionalProperties": False,
            "properties": {"rule_id": {"type": "string"}, "citation": {"type": ["string", "null"]},
                           "locators": {"type": "array", "items": {"type": "object"}}}}},
        "statements": {"type": "array", "items": LOCATED_TEXT},
        "omissions": {"type": "array", "items": LOCATED_TEXT},
        "counterevidence": {"type": "array", "items": LOCATED_TEXT},
        "hits": {"type": "array", "items": HIT},
        "conclusions": {"type": "array", "items": {
            "type": "object", "required": ["text", "confidence", "sources"], "additionalProperties": False,
            "properties": {
                "text": {"type": "string", "minLength": 1},
                "confidence": {"type": "string", "enum": CONFIDENCE},
                "sources": {"type": "array", "minItems": 1, "items": SOURCE},
            }}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
}

# What the model is allowed to return; everything else is filled in deterministically.
MODEL_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["scope", "actors", "dates", "statements", "omissions", "counterevidence", "conclusions"],
    "additionalProperties": False,
    "properties": {
        "scope": {"type": "string"},
        "actors": {"type": "array", "items": {"type": "string"}},
        "dates": DIGEST_SCHEMA["properties"]["dates"],
        "statements": {"type": "array", "items": LOCATED_TEXT},
        "omissions": {"type": "array", "items": LOCATED_TEXT},
        "counterevidence": {"type": "array", "items": LOCATED_TEXT},
        "conclusions": {"type": "array", "items": {
            "type": "object", "required": ["text", "confidence", "sources"], "additionalProperties": False,
            "properties": {
                "text": {"type": "string", "minLength": 1},
                "confidence": {"type": "string", "enum": CONFIDENCE},
                "sources": {"type": "array", "minItems": 1, "items": {
                    "type": "object", "required": ["locator"], "additionalProperties": False,
                    "properties": {"locator": {"type": "object"}, "rule_id": {"type": ["string", "null"]}}}},
            }}},
    },
}

DEFAULT_LIMITATIONS = [
    "Detectors match keywords in extracted text; unextracted, scanned or redacted pages are not covered.",
    "Absence of a term in extracted text is not proof that the record lacks it.",
    "Rule matches cite the law package in force on the stated event date; the package status may be draft.",
    "Nothing in this digest is a finding, a legal conclusion or public wording until independently reviewed.",
]


def empty_digest(sha256, jurisdiction, law_package_version, privacy_tier, detector_version):
    return {
        "schema_version": 1,
        "sha256": sha256,
        "jurisdiction": jurisdiction,
        "law_package_version": law_package_version,
        "privacy_tier": privacy_tier,
        "redaction_count": 0,
        "redaction_counts": {},
        "model_id": None,
        "detector_version": detector_version,
        "scope": "",
        "actors": [],
        "dates": [],
        "duties": [],
        "statements": [],
        "omissions": [],
        "counterevidence": [],
        "hits": [],
        "conclusions": [],
        "limitations": list(DEFAULT_LIMITATIONS),
    }


def validate_digest(document):
    """Raise ValueError when ``document`` is not a well-formed digest."""
    validate(document, DIGEST_SCHEMA)
    for index, conclusion in enumerate(document["conclusions"]):
        for source in conclusion["sources"]:
            if source["sha256"] != document["sha256"]:
                raise ValueError(f"conclusions[{index}]: source sha256 does not match the digested original")
    return document


def validate_model_output(document):
    validate(document, MODEL_OUTPUT_SCHEMA)
    return document
