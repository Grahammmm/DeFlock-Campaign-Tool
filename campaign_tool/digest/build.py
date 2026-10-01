"""Compose a digest: redact, detect, optionally narrate, validate.

Without a model configuration the digest is detector-only: ``duties`` are
the rules the detectors matched, ``statements`` are the detector excerpts
and every conclusion is ``needs_attorney_review``. With a model, the
narrative fields come from the validated model output and each conclusion's
sources are bound to the digested sha256. Either way the result passes
:func:`campaign_tool.digest.schema.validate_digest`.
"""
from datetime import date

from ..law import rules_in_force
from .detectors import DETECTOR_VERSION, run_detectors
from .model import ModelError, narrative
from .redact import redact
from .schema import empty_digest, validate_digest


def redact_units(units, allowlist=(), denylist=()):
    """Redact every unit's text with one shared placeholder table."""
    joined = "\n\x1e\n".join(unit.get("text") or "" for unit in units)
    result = redact(joined, allowlist=allowlist, denylist=denylist)
    pieces = result.text.split("\n\x1e\n")
    if len(pieces) != len(units):  # a placeholder can never contain the separator, but be explicit
        raise ValueError("redaction changed the unit count")
    redacted = [{**unit, "text": piece} for unit, piece in zip(units, pieces)]
    return redacted, result.counts


def _duties_from_hits(hits):
    duties = {}
    for hit in hits:
        if not hit["rule_id"]:
            continue
        entry = duties.setdefault(hit["rule_id"], {"rule_id": hit["rule_id"], "citation": hit["citation"], "locators": []})
        if hit["locator"] not in entry["locators"]:
            entry["locators"].append(hit["locator"])
    return list(duties.values())


def _detector_only(digest, hits):
    digest["scope"] = "Detector-only digest: keyword and citation matches with exact locators; no narrative."
    for hit in hits:
        if hit["kind"] == "absence":
            digest["omissions"].append({"text": f"{hit['detector']}: {hit['detail'].get('element') or hit['detail'].get('note', '')}".strip(": "), "locator": hit["locator"]})
        elif hit["excerpt"]:
            digest["statements"].append({"text": hit["excerpt"], "locator": hit["locator"]})
    for duty in digest["duties"]:
        digest["conclusions"].append({
            "text": f"Extracted text mentions subject matter of {duty['citation'] or duty['rule_id']}; whether the record satisfies the duty needs review.",
            "confidence": "needs_attorney_review",
            "sources": [{"sha256": digest["sha256"], "locator": loc, "rule_id": duty["rule_id"]} for loc in duty["locators"][:5]],
        })
    if not digest["conclusions"]:
        digest["conclusions"].append({
            "text": "No detector matched a law-package rule; the record needs manual review before any conclusion.",
            "confidence": "needs_attorney_review",
            "sources": [{"sha256": digest["sha256"], "locator": {"scope": "document"}, "rule_id": None}],
        })


def build_digest(sha256, units, package, jurisdiction, law_package_version, privacy_tier,
                 model_config=None, allowlist=(), denylist=(), event_date=None, opener=None):
    redacted, counts = redact_units(units, allowlist=allowlist, denylist=denylist)
    hits = run_detectors(redacted, package)
    digest = empty_digest(sha256, jurisdiction, law_package_version, privacy_tier, DETECTOR_VERSION)
    digest["redaction_counts"] = counts
    digest["redaction_count"] = sum(counts.values())
    digest["hits"] = hits
    digest["duties"] = _duties_from_hits(hits)
    if model_config is None:
        _detector_only(digest, hits)
        digest["limitations"].append("No model configured: narrative fields are detector output only.")
    else:
        rules = rules_in_force(package, event_date or date.today())
        output = narrative(model_config, redacted, hits, rules, opener=opener)
        digest["model_id"] = model_config.model_id
        for key in ("scope", "actors", "dates", "statements", "omissions", "counterevidence"):
            digest[key] = output[key]
        known_rules = {rule["rule_id"] for rule in package["rules"]}
        for conclusion in output["conclusions"]:
            sources = []
            for source in conclusion["sources"]:
                rule_id = source.get("rule_id")
                if rule_id is not None and rule_id not in known_rules:
                    raise ModelError("model cited unknown rule_id " + repr(rule_id))
                sources.append({"sha256": sha256, "locator": source["locator"], "rule_id": rule_id})
            confidence = conclusion["confidence"]
            if any(s["rule_id"] for s in sources):
                confidence = "needs_attorney_review"  # legal conclusions are never auto-verified
            digest["conclusions"].append({"text": conclusion["text"], "confidence": confidence, "sources": sources})
        digest["limitations"].append(f"Narrative written by model {model_config.model_id} from redacted text; verify every locator against the original.")
    return validate_digest(digest)
