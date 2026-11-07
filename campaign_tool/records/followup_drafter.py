"""Follow-up reply drafts chosen by playbook pattern (REC-PLAY-2). Drafts only: nothing is sent.

Input is one small "case" (a JSON file): the agency, the request, the type of response that
came back, the asked items with their ratings, what was asked before, and which agency policy
section to quote. The module picks the playbook follow-up pattern for the response type,
orders the open asks tier 1 first (audit and search/use logs, then policy, retention, access
and sharing, then contracts), drops anything already produced or already pending (the
prior-asks check), quotes the agency's own policy section from the policy register
(REC-POL-1), marks every playbook citation verified or unverified and keeps the unverified
ones out of the letter body, and runs the banned-phrase lint. A draft that fails the lint or
still has an open placeholder is marked "needs edit".

Everything specific (campaign, agency, jurisdiction, playbook, policy rows) is data. The module
sends, posts and publishes nothing, writes no file, calls no model and reads no credentials.
Every draft is DRAFT -- NOT SENT and needs the owner's signed approval (REC-AUTO-4) first.
"""
import argparse
import json
import re
import sys
from pathlib import Path

from . import agency_policies as _pol
from . import item_tiers as _it
from . import playbook as _pb

SCHEMA = "records-followup-case-v1"
SCHEMA_OUT = "records-followup-draft-v1"
MAX_BYTES = 1024 * 1024
BANNER = "DRAFT \u2014 NOT SENT. Needs the owner's signed approval and a review before anyone sends it."
EDIT = "[EDIT ME: {}]"
NOTES_RULE = "\n---\n"
# response type words (from received items and registers) -> playbook pattern id
RESPONSE_TYPES = {
    "audit_no_documents": "audit_no_documents", "closed_new_request": "closed_new_request",
    "closed": "closed_new_request", "closed_with_open_items": "closed_new_request",
    "extension": "extension", "partial": "partial_production", "partial_production": "partial_production",
    "denial": "blanket_denial", "denied": "blanket_denial", "blanket_denial": "blanket_denial",
    "fee": "fee", "fee_estimate": "fee", "no_records": "no_records", "no_responsive_records": "no_records",
    "contradiction": "records_contradict_policy", "records_contradict_policy": "records_contradict_policy",
    "silence": "silence", "no_response": "silence", "vague": "vague_answer", "vague_answer": "vague_answer",
}
_SLOT = re.compile(r"\[([^\[\]]+)\]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_DONE_PRIOR = frozenset({"produced", "answered"})
_PENDING_PRIOR = frozenset({"pending", "sent", "open"})


def _need(condition, code, detail=""):
    if not condition:
        raise _pb.PlaybookError(code, detail)


def _clean(value, limit=600):
    return _CONTROL.sub("", " ".join(str(value).split()))[:limit]


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", _pb._normalise(text or "").lower()).strip()


def _slot_key(inner):
    return re.sub(r"[^a-z0-9]+", "_", inner.lower()).strip("_")


def _label(row):
    """'Policy 466.5(e)': a section that starts with the policy number is joined to the policy word; otherwise both are kept."""
    match = re.match(r"^\s*(.*?)\s*(\d+)\s*$", row["policy_id"])
    if match and match.group(1) and row["section"].startswith(match.group(2)):
        return match.group(1) + " " + row["section"]
    return row["policy_id"] + " " + row["section"]


def _policy_row(rows, agency_id, want):
    if not want:
        return None
    _need(isinstance(want, dict) and isinstance(want.get("section"), str) and want["section"].strip(), "case_invalid", "policy.section")
    matches = [r for r in _pol.sections(rows, agency_id, want.get("policy_id")) if r["section"] == want["section"].strip()]
    return matches[0] if matches else None


def _is_prior(ask, prior):
    """The prior-asks check: the status of an earlier ask that matches this one, else None."""
    here = _norm(ask["text"])
    for entry in prior:
        same_id = entry.get("ask_id") and entry["ask_id"] == ask.get("ask_id")
        there = _norm(entry.get("text"))
        if same_id or (here and there and (here == there or here in there or there in here)):
            return entry["status"]
    return None


def _validate(case):
    _need(isinstance(case, dict) and case.get("schema") == SCHEMA, "case_invalid", "schema")
    agency = case.get("agency")
    _need(isinstance(agency, dict) and all(isinstance(agency.get(k), str) and agency[k].strip() for k in ("id", "name")),
          "case_invalid", "agency")
    _need(isinstance(case.get("request_id"), str) and case["request_id"].strip(), "case_invalid", "request_id")
    _need(isinstance(case.get("response_type"), str) and case["response_type"].strip(), "case_invalid", "response_type")
    for key in ("request_date", "response_date", "contact", "sender", "subject", "recipient"):
        _need(case.get(key) is None or isinstance(case[key], str), "case_invalid", key)
    asks = case.get("asks")
    _need(isinstance(asks, list) and asks, "case_invalid", "asks")
    for ask in asks:
        _need(isinstance(ask, dict) and isinstance(ask.get("text"), str) and ask["text"].strip(), "case_invalid", "ask.text")
        _need(ask.get("produced") is None or (isinstance(ask["produced"], list) and all(
            isinstance(p, dict) and isinstance(p.get("file"), str) and p["file"].strip() for p in ask["produced"])),
            "case_invalid", "ask.produced")
    prior = case.get("prior_asks", [])
    _need(isinstance(prior, list) and all(isinstance(p, dict) and isinstance(p.get("text") or p.get("ask_id"), str)
                                          and p.get("status") in _DONE_PRIOR | _PENDING_PRIOR for p in prior),
          "case_invalid", "prior_asks")
    slots = case.get("slots", {})
    _need(isinstance(slots, dict) and all(isinstance(v, str) for v in slots.values()), "case_invalid", "slots")


def _tiered(book, case):
    """Asks with tier, state and original number; open ones first by tier; the prior-asks check applied."""
    prior = case.get("prior_asks", [])
    rows = []
    for position, ask in enumerate(case["asks"]):
        tier = _pb.tier_for(book, ask["text"])
        rows.append({"ask_id": ask.get("ask_id"), "number": ask.get("number") or position + 1, "text": _clean(ask["text"]),
                     "tier": tier, "state": _it.state_of(ask.get("rating")), "produced": ask.get("produced") or [], "_pos": position})
    open_asks, dropped = [], []
    for row in rows:
        earlier = _is_prior(row, prior)
        if row["state"] in _it.DONE or earlier in _DONE_PRIOR:
            dropped.append({"number": row["number"], "text": row["text"], "reason": "already produced"})
        elif earlier in _PENDING_PRIOR:
            dropped.append({"number": row["number"], "text": row["text"], "reason": "already asked, still pending"})
        else:
            open_asks.append(row)
    last = len(book["tiers"]) + 1
    open_asks.sort(key=lambda a: (a["tier"] if a["tier"] is not None else last, a["_pos"]))
    return open_asks, dropped, rows


_EDIT_SLOT = re.compile(r"\[EDIT ME: ([^\[\]]+)\]")
_SENTENCE = re.compile(r"(?<=[.?!])\s+")
_GENERIC = frozenset({"alpr", "agency", "policy", "records", "record", "completed", "under", "used", "system", "that",
                      "with", "from", "this", "please", "which", "were", "does", "have", "been", "should"})


def _words(text):
    """Distinctive lower-case word stems of a text (slots removed, plural s stripped)."""
    stems = set()
    for word in re.sub(r"\[[^\[\]]*\]", " ", _norm(text)).split():
        word = word[:-1] if word.endswith("s") and len(word) > 4 else word
        if len(word) >= 4 and word not in _GENERIC:
            stems.add(word)
    return stems


def _drop_dropped_sentences(question, open_asks, dropped_rows):
    """Remove sentences that are only about an ask the prior-asks check dropped, so no dropped record is asked for again."""
    if not dropped_rows:
        return question
    dropped_words = set().union(*(_words(d["text"]) for d in dropped_rows))
    keep = []
    for sentence in _SENTENCE.split(question):
        if _words(sentence) & dropped_words:  # conservative: any subject word of a dropped ask suppresses it, even if an open ask shares the word
            continue
        keep.append(sentence)
    return " ".join(keep)


def _numbers(asks):
    numbers = ["%s" % a["number"] for a in asks]
    return "item " + numbers[0] if len(numbers) == 1 else "items " + ", ".join(numbers[:-1]) + " and " + numbers[-1]


def _fill(text, slots, unfilled):
    def swap(match):
        inner = match.group(1)
        value = slots.get(_slot_key(inner))
        if value and value.strip():
            return _clean(value)
        unfilled.append(inner)
        return EDIT.format(inner)
    return _SLOT.sub(swap, text)


def _citations(book, entry):
    out = []
    for key in entry["cite"]:
        cite = book["citations"][key]
        out.append({"key": key, "ref": cite["ref"], "gives": cite.get("gives", ""), "status": cite["status"],
                    "in_body": cite["status"] == "verified-in-package"})
    return out


def draft(book, case, policy_rows=()):
    """Build one follow-up draft. Returns a dict; ``body`` is the text. Raises PlaybookError on a bad case."""
    _validate(case)
    kind = RESPONSE_TYPES.get(_norm(case["response_type"]).replace(" ", "_"))
    if kind is None:
        raise _pb.PlaybookError("response_type_unknown", _clean(case["response_type"], 60))
    entry = _pb.pattern(book, kind)
    agency, request_id = _clean(case["agency"]["name"], 200), _clean(case["request_id"], 128)
    slots = {_slot_key(k): v for k, v in case.get("slots", {}).items()}
    unfilled = []

    open_asks, dropped, all_asks = _tiered(book, case)
    dropped_rows = [a for a in all_asks if a["number"] in {d["number"] for d in dropped}]
    policy = _policy_row(policy_rows, case["agency"]["id"], case.get("policy"))
    if case.get("policy") and policy is None:
        slots.pop("policy_section", None)
        unfilled.append("policy section not found in the policy register")
    elif policy is not None:
        slots["policy_section"] = _label(policy)

    produced = [(a["number"], p) for a in all_asks for p in a["produced"]]
    lead = [a for a in open_asks if a["tier"] == 1]
    lines = [f"# Draft (followup-{kind}): {agency} / {request_id}", "", BANNER, "",
             f"Target agency: {agency}", f"Belongs to request: {request_id}", f"Pattern: {kind}", "State: pending-approval",
             "To: " + (_clean(case["recipient"], 200) if case.get("recipient") else EDIT.format("recipient and contact; not taken from the record")),
             "Subject: " + _clean(case.get("subject") or f"Request {request_id}: follow-up ({kind.replace('_', ' ')})", 200), "",
             ("Dear " + _clean(case["contact"], 120) + ",") if case.get("contact") else f"Hello {EDIT.format('contact')},", ""]
    when = f" of {_clean(case['response_date'], 40)}" if case.get("response_date") else ""
    lines.append(f"Thank you for your response{when} to request {request_id}"
                 + (f", dated {_clean(case['request_date'], 40)}" if case.get("request_date") else "") + ".")
    if produced:
        files = "; ".join("%s%s" % (_clean(p["file"], 160), (", page %s" % p["page"]) if p.get("page") else "") for _, p in produced)
        lines.append(f"We have the records you produced ({files}); we are not asking for those again.")
    lines.append("")

    if open_asks:
        lines.append("Items still open, audit and search/use logs first:")
        for ask in open_asks:
            lines.append(f"- Item {ask['number']} asked for: \u201c{ask['text']}\u201d (state in our records: {ask['state']})")
        lines.append("")
    if policy is not None:
        lines += [f"Your own policy, {_label(policy)} (page {policy['page']}), says: \u201c{_clean(policy['quote'], 400)}\u201d", ""]
    if case.get("precedent") is True:
        lines += ["Another county agency released an export of this kind.", ""]

    questions = []
    for text in entry["questions"] if open_asks else ():  # nothing open: nothing to ask
        text = _drop_dropped_sentences(text, open_asks, dropped_rows)
        if not text:
            continue
        text = text.replace("the lead items", _numbers(lead) if lead else "the first items listed above")
        questions.append(_fill(text, slots, unfilled))
    if questions:
        lines.append("Please:")
        lines += [f"{n}. {q}" for n, q in enumerate(questions, 1)]
    else:
        lines.append("Nothing further is asked in this letter: every item is already produced or already pending.")
        unfilled.append("no open asks remain; nothing to request, do not send")
    lines.append("")

    cites = _citations(book, entry)
    shown = [c for c in cites if c["in_body"]]
    if shown:
        lines += ["For reference: " + "; ".join(f"{c['ref']} ({c['gives']})" if c["gives"] else c["ref"] for c in shown) + ".", ""]
    lines += list(book["first_request"].get("closing", [])) + ["", "Thank you,", "",
                                                               _clean(case["sender"], 160) if case.get("sender") else EDIT.format("name"), ""]
    letter = "\n".join(lines)
    for inner in _EDIT_SLOT.findall(letter):  # every placeholder left in the letter, not only _fill() ones
        if inner not in unfilled:
            unfilled.append(inner)

    lint = _pb.lint(book, letter)
    status = "needs edit" if lint["hits"] or unfilled else "ready for review"
    notes = ["Notes for the reviewer (remove before sending):", f"  Pattern {kind}: {entry['goal']}"]
    notes += ["  Do not: " + d for d in entry["do_not"]]
    held = [c for c in cites if not c["in_body"]]
    notes += ["  Left out of the body (%s): %s" % (c["status"], c["ref"]) for c in held]
    notes += ["  Dropped by the prior-asks check: item %s (%s)" % (d["number"], d["reason"]) for d in dropped]
    notes += ["  Open placeholder: " + u for u in unfilled]
    notes += ["  Banned-phrase check: %s%s" % (lint["status"], "".join(" [%s]" % h["id"] for h in lint["hits"]))]
    notes += ["  Status: " + status, "Draft only. Not legal advice. Review required; nothing here is a finding that anyone broke a rule."]
    body = letter + NOTES_RULE + "\n".join(notes) + "\n"
    return {"schema": SCHEMA_OUT, "kind": "followup-" + kind, "pattern": kind, "agency_id": case["agency"]["id"],
            "request_id": request_id, "status": status, "review_required": True, "sent": False, "body": body,
            "open_asks": [{"number": a["number"], "tier": a["tier"], "state": a["state"]} for a in open_asks],
            "dropped": dropped, "citations": cites,
            "policy": ({"cite": _pol.cite(policy), "doc_sha256": policy["doc_sha256"], "page": policy["page"]} if policy else None),
            "banned_phrases": lint, "unfilled": unfilled}


def _read_json(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        raise _pb.PlaybookError("unreadable", Path(path).name) from None
    _need(len(raw) <= MAX_BYTES, "too_large", Path(path).name)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _pb.PlaybookError("not_json", Path(path).name) from None


def main():
    parser = argparse.ArgumentParser(prog="records followup-draft", description="Draft a follow-up reply from a case file (never sent).")
    parser.add_argument("case", help="followup case JSON (schema %s)" % SCHEMA)
    parser.add_argument("--playbook", help="playbook JSON; default: the shipped records-alpr-v1")
    parser.add_argument("--policies", help="agency-policies.csv to quote the agency's own policy section from")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args()
    try:
        rows = []
        if args.policies:
            rows, problems = _pol.load(args.policies)
            if problems:
                raise _pb.PlaybookError("policies_invalid", "line %s" % problems[0]["line"])
        result = draft(_pb.load(args.playbook), _read_json(args.case), rows)
    except _pb.PlaybookError as error:
        print("records followup-draft: refused " + error.code + (" (" + error.detail + ")" if error.detail else ""), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else result["body"], end="" if not args.json else "\n")
    return 0 if result["status"] == "ready for review" else 1


if __name__ == "__main__":
    sys.exit(main())
