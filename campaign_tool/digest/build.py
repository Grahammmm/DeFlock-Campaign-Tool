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
from .redact import Redactor
from .schema import empty_digest, validate_digest


def redact_units(units, allowlist=(), denylist=()):
    """Redact every unit's text with one shared placeholder table.

    Units are redacted one at a time (never joined), so a pattern can never
    match across a page break, and the locator's own strings (attachment
    member names, sheet titles) are redacted with the same table.
    """
    redactor = Redactor(allowlist=allowlist, denylist=denylist)
    counts = {}
    redacted = []
    for index, unit in enumerate(units):
        result = redactor.redact(unit.get("text") or "")
        for key, value in result.counts.items():
            counts[key] = counts.get(key, 0) + value
        redacted.append({**unit, "text": result.text, "model_locator": model_locator(unit.get("locator") or {}, index)})
    return redacted, counts


LOCATOR_SAFE_KEYS = ("scope", "kind", "type")


def model_locator(locator, index):
    """The locator as the model may see it: numbers and fixed vocabulary only.

    Attachment member names and sheet titles are free text (often a person's
    name), so they never cross the model boundary; ``unit`` lets
    :func:`restore_locators` map the model's echo back to the real locator.
    """
    out = {k: v for k, v in locator.items() if not isinstance(v, str) or k in LOCATOR_SAFE_KEYS}
    out["unit"] = index
    return out


def restore_locators(output, redacted):
    """Replace every ``{..., "unit": i}`` locator the model echoed with unit i's real locator."""
    def real(locator):
        if isinstance(locator, dict) and isinstance(locator.get("unit"), int) and 0 <= locator["unit"] < len(redacted):
            return dict(redacted[locator["unit"]].get("locator") or {})
        return locator
    for key in ("dates", "statements", "omissions", "counterevidence"):
        for item in output.get(key) or []:
            if isinstance(item, dict) and "locator" in item:
                item["locator"] = real(item["locator"])
    for conclusion in output.get("conclusions") or []:
        for source in conclusion.get("sources") or []:
            if isinstance(source, dict) and "locator" in source:
                source["locator"] = real(source["locator"])
    return output


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
        output = restore_locators(narrative(model_config, redacted, hits, rules, opener=opener), redacted)
        # Units carry a model-facing locator; the digest records the real one.
        for unit in redacted:
            unit.pop("model_locator", None)
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
