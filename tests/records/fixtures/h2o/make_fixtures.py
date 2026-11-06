"""Regenerates the four recorded synthetic H2O responses next to this file (no record text)."""
import json
from pathlib import Path

DOC = ["acknowledgement", "extension", "denial", "fee_notice", "records", "no_records", "clarification", "other"]
WHOLE = ["complete", "partial_withheld", "fully_withheld", "not_applicable"]


def spread(names, top, p):
    rest = (1 - p) / (len(names) - 1)
    return {n: (p if n == top else rest) for n in names}


def choice(names, top, p):
    return {"type": "choice", "choice": top, "probabilities": spread(names, top, p)}


def score(level, p):
    probs = {str(i): (1 - p) / 3 for i in range(4)}
    probs[str(level)] = p
    return {"type": "score", "score": float(level), "probabilities": probs}


def answers(doc, p, deadline, p_deadline, whole, privacy, level, follow, p_follow):
    return {"answers": {
        "doc_type": choice(DOC, doc, p),
        "deadline_stated": choice(["yes", "no"], deadline, p_deadline),
        "completeness": choice(WHOLE, whole, p),
        "privacy_present": {"type": "noul", "noul": privacy},
        "urgency": score(level, p),
        "followup_needed": choice(["yes", "no"], follow, p_follow),
    }}


FIXTURES = {
    "confident.json": answers("extension", 0.96, "yes", 0.95, "not_applicable", 0.05, 2, "yes", 0.95),
    "floored.json": answers("extension", 0.96, "yes", 0.801, "not_applicable", 0.801, 2, "yes", 0.801),
    "lowconfidence.json": answers("extension", 0.62, "yes", 0.70, "partial_withheld", 0.199, 1, "no", 0.60),
    "malformed.json": {"answers": {"doc_type": {"type": "choice", "choice": "extension"}}},
}

if __name__ == "__main__":
    for name, doc in FIXTURES.items():
        (Path(__file__).parent / name).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
