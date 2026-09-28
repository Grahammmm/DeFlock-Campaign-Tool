"""Choose today's fact and format, and write the Reel script.

Two writers produce the same script shape:
- `template_script`: deterministic, built from the fact's own hooks and beats.
- `llm_script`: optional, asks Claude for a fresh variant constrained to the fact
  and the campaign's messaging guide. Its output must pass `check_script`, or the
  pipeline falls back to the template script.
"""
import datetime as dt
import hashlib
import json
import os
import re
import urllib.request

from .facts import numbers_in

FORMATS = {
    # name: (beat order, frames it suits)
    "stat-drop": (["hook", "fact_beats", "why", "cta"], None),
    "map-reveal": (["hook", "map", "fact_beats", "cta"], None),
    "records-receipt": (["hook", "fact_beats", "why", "cta"], {"retention", "sharing", "audits", "training", "cost", "records"}),
}
RECEIPT_HOOK = "We requested {agency}'s license plate camera records. Here's what came back."


def plan(facts, day, history, formats=None):
    """Pick the fact and format for `day`, avoiding recent repeats. Deterministic for a date."""
    today = day.isoformat()
    recent = [h for h in history if h.get("date", "") >= (day - dt.timedelta(days=21)).isoformat() and h.get("date") != today]
    recent_facts = {h["fact_id"] for h in recent[-14:]}
    recent_formats = [h["format"] for h in recent[-2:]]
    live = [f for f in facts if not f.expires or f.expires >= today]
    pool = [f for f in live if f.id not in recent_facts] or live
    seed = int(hashlib.sha256(today.encode()).hexdigest(), 16)
    # Weighted, date-seeded choice so reruns of the same day pick the same fact.
    total = sum(f.weight for f in pool)
    point = (seed % 10_000) / 10_000 * total
    for fact in sorted(pool, key=lambda f: f.id):
        point -= fact.weight
        if point <= 0:
            break
    names = formats or list(FORMATS)
    usable = [n for n in names if FORMATS[n][1] is None or fact.frame in FORMATS[n][1]]
    if fact.frame == "map" or not fact.agency:
        usable = [n for n in usable if n != "records-receipt"] or usable
    fresh = [n for n in usable if n not in recent_formats] or usable
    return fact, fresh[seed // 7 % len(fresh)]


def template_script(fact, fmt, campaign, day):
    order = FORMATS[fmt][0]
    seed = int(hashlib.sha256((day.isoformat() + fact.id).encode()).hexdigest(), 16)
    hook = fact.hooks[seed % len(fact.hooks)]
    if fmt == "records-receipt" and fact.agency:
        hook = RECEIPT_HOOK.format(agency=fact.agency)
    beats = []
    for step in order:
        if step == "hook":
            beats.append({"type": "hook", "text": hook})
        elif step == "map":
            beats.append({"type": "map", "big": campaign["map"]["count_label"], "small": campaign["map"]["caption"]})
        elif step == "fact_beats":
            beats += [dict(b, source=b.get("source", fact.source_label)) for b in fact.beats]
        elif step == "why":
            beats.append({"type": "line", "text": fact.why})
        elif step == "cta":
            beats.append({"type": "cta", "text": campaign["cta"]["text"], "small": campaign["cta"]["small"]})
    return {
        "date": day.isoformat(), "fact_id": fact.id, "format": fmt, "writer": "template",
        "beats": beats, "caption": caption(fact, hook, campaign),
        "veo_prompt": veo_prompt(fact, campaign), "source_url": fact.source_url,
    }


def caption(fact, hook, campaign):
    c = campaign["caption"]
    first = fact.extra.get("first_line") or c["first_line"].format(agency=fact.agency or campaign["place"])
    lines = [first, "", hook, "",
             fact.claim, "", f"Source: {fact.source_label}. {c['sources_line']}", "", c["cta"], "",
             " ".join(c["hashtags"][:5])]
    return "\n".join(lines)


def veo_prompt(fact, campaign):
    v = campaign["veo"]
    return f"{fact.visual}. {v['style']}"


def check_script(script, fact, campaign):
    """Reject a script that adds numbers, banned terms, or overlong lines."""
    allowed = {n.replace(",", "") for n in fact.numbers} | {n.replace(",", "") for n in campaign.get("always_allowed_numbers", [])}
    problems = []
    texts, cited = [], []
    for beat in script["beats"]:
        texts += [beat.get("text", ""), beat.get("big", ""), beat.get("small", "")]
        cited.append(beat.get("source", ""))
    # The caption quotes the published claim and its source verbatim; check everything else in it.
    texts.append(script["caption"].replace(fact.claim, "").replace(fact.source_label, ""))
    for text in texts + cited:
        extra = numbers_in(text) - allowed
        if extra and text not in cited:
            problems.append(f"numbers not in the fact: {sorted(extra)} in '{text[:60]}'")
        for term in campaign.get("banned_terms", []):
            if re.search(r"\b" + re.escape(term) + r"\b", text, re.I):
                problems.append(f"banned term '{term}' in '{text[:60]}'")
    for beat in script["beats"]:
        if len(beat.get("text", "")) > 140 or len(beat.get("big", "")) > 24:
            problems.append(f"beat too long: {beat}")
    if not any(b["type"] == "cta" for b in script["beats"]):
        problems.append("no call to action")
    if len(script["beats"]) > 7:
        problems.append("too many beats")
    return problems


LLM_INSTRUCTIONS = """You write the on-screen script for one 18-25 second Instagram Reel for a local
campaign about license plate reader cameras. Follow the messaging guide. Use ONLY the fact
provided: every number you write must appear in the fact's `numbers`. Don't add claims,
names of individuals, or legal conclusions that the fact doesn't state. Plain, calm,
factual tone; no insults, no anti-police framing, no conspiracy language.

Return JSON only:
{"hook": "<= 90 characters, first 2 seconds, curiosity or direct address>",
 "beats": [{"type": "stat", "big": "<number or short figure, <= 12 chars>", "small": "<label <= 90 chars>"} |
           {"type": "line", "text": "<= 110 characters"} |
           {"type": "quote", "text": "<words from the fact, <= 120 characters>"}],   (2-3 beats)
 "why": "<= 110 characters: why a resident should care>"}"""


def llm_script(fact, fmt, campaign, day, guide, api_key=None, model=None):
    """Ask Claude for a fresh variant. Returns None if unavailable or invalid."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    body = {
        "model": model or os.environ.get("SOCIAL_CLAUDE_MODEL", "claude-sonnet-5"),
        "max_tokens": 800,
        "system": LLM_INSTRUCTIONS + "\n\nMESSAGING GUIDE:\n" + guide[:12000],
        "messages": [{"role": "user", "content": json.dumps({
            "format": fmt, "fact": {"agency": fact.agency, "claim": fact.claim, "numbers": fact.numbers,
                                    "example_hooks": fact.hooks, "example_beats": fact.beats, "why": fact.why}})}],
    }
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            text = json.loads(resp.read())["content"][0]["text"]
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception as exc:  # network, quota or parse failure: fall back
        print(f"LLM script unavailable ({type(exc).__name__}); using template")
        return None
    script = template_script(fact, fmt, campaign, day)
    beats = [{"type": "hook", "text": data["hook"]}]
    if fmt == "map-reveal":
        beats.append(next(b for b in script["beats"] if b["type"] == "map"))
    for b in data.get("beats", [])[:3]:
        if b.get("type") in {"stat", "line", "quote"}:
            beats.append(dict(b, source=fact.source_label))
    if fmt != "map-reveal":
        beats.append({"type": "line", "text": data.get("why", fact.why)})
    beats.append(next(b for b in script["beats"] if b["type"] == "cta"))
    script.update(beats=beats, writer="claude", caption=caption(fact, data["hook"], campaign))
    problems = check_script(script, fact, campaign)
    if problems:
        print("LLM script rejected: " + "; ".join(problems))
        return None
    return script
