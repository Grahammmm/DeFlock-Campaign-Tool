"""Load and validate a campaign's social fact library (facts.json).

A fact is a claim the campaign has already published, with its public source.
The on-screen lines are written from the fact's own wording; `numbers` lists
every figure a script built on this fact may show.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

NUMBER = re.compile(r"\d[\d,.]*")
FRAMES = {"retention", "sharing", "audits", "training", "cost", "map", "win", "national", "law", "records", "meeting"}


@dataclass
class Fact:
    id: str
    agency: str
    claim: str
    source_url: str
    source_label: str
    frame: str
    numbers: list
    hooks: list
    beats: list
    why: str
    visual: str
    weight: float = 1.0
    expires: str = ""
    extra: dict = field(default_factory=dict)


def numbers_in(text):
    """Every figure in a text, normalised (commas removed, trailing dots stripped)."""
    return {m.group(0).replace(",", "").rstrip(".") for m in NUMBER.finditer(text or "")}


def load_facts(path):
    data = json.loads(Path(path).read_text())
    facts, errors = [], []
    seen = set()
    for raw in data.get("facts", []):
        fid = raw.get("id", "?")
        try:
            fact = Fact(id=raw["id"], agency=raw.get("agency", ""), claim=raw["claim"],
                        source_url=raw["source_url"], source_label=raw["source_label"],
                        frame=raw["frame"], numbers=[str(n) for n in raw.get("numbers", [])],
                        hooks=raw["hooks"], beats=raw["beats"], why=raw["why"], visual=raw["visual"],
                        weight=float(raw.get("weight", 1.0)), expires=raw.get("expires", ""),
                        extra={k: v for k, v in raw.items() if k not in Fact.__dataclass_fields__})
        except KeyError as missing:
            errors.append(f"{fid}: missing field {missing}")
            continue
        errors += validate_fact(fact)
        if fact.id in seen:
            errors.append(f"{fact.id}: duplicate id")
        seen.add(fact.id)
        facts.append(fact)
    if errors:
        raise ValueError("Invalid facts file:\n  " + "\n  ".join(errors))
    return facts


def validate_fact(fact):
    errors = []
    if fact.frame not in FRAMES:
        errors.append(f"{fact.id}: frame '{fact.frame}' not in {sorted(FRAMES)}")
    if not fact.source_url.startswith("https://"):
        errors.append(f"{fact.id}: source_url must be an https link")
    if not fact.hooks or not fact.beats:
        errors.append(f"{fact.id}: needs at least one hook and one beat")
    allowed = {n.replace(",", "") for n in fact.numbers}
    claim_numbers = numbers_in(fact.claim)
    for n in allowed - claim_numbers:
        # A listed number must be traceable to the published claim.
        errors.append(f"{fact.id}: number {n} is not in the claim text")
    texts = list(fact.hooks) + [fact.why] + [b.get("text", "") + " " + b.get("big", "") + " " + b.get("small", "") for b in fact.beats]
    for text in texts:
        extra = numbers_in(text) - allowed
        if extra:
            errors.append(f"{fact.id}: '{text[:50]}' uses numbers {sorted(extra)} not listed in numbers")
    for beat in fact.beats:
        if beat.get("type") not in {"stat", "line", "quote", "map"}:
            errors.append(f"{fact.id}: beat type {beat.get('type')} unknown")
    return errors
