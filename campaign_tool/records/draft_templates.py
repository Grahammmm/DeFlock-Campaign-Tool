"""Pure renderer: received-item records -> cited reply drafts (never sent).

Selection is deterministic: extension -> reply-extension, denial -> appeal-denial, fee -> fee-reply,
response/records with at least one cited ``gap`` finding -> followup-missing-records. Everything
else yields no draft. The source text is inert data: quotes are placed inside a delimited block
and never interpreted. No model call, no legal rating, no deadline arithmetic: dates are quoted
exactly as the item supplied them.

The targeted follow-up wording adapts templates/follow-up.md (request the existing referenced
record, or say whether it was withheld, not located or outside the search); it is kept here as
Python so an installed wheel needs no source-tree template.
"""
import json
import re

from .. import kit_track

KINDS = ("reply-extension", "appeal-denial", "fee-reply", "followup-missing-records",
         "reminder-no-response", "reminder-promised-date", "formal-notice-no-response")
FOLLOWUP_KINDS = KINDS[4:]
DRAFT_BANNER = "DRAFT — NOT SENT. Review before sending it yourself."
FOOTER = ("---\nDraft only. Not legal advice. Review required; the law package is draft and every reference above "
          "is the supplied reference, not a conclusion that the agency violated anything.\n")
EDIT = "[EDIT ME: {}]"
MAX_BODY_BYTES = 64 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_SHA = re.compile(r"[a-f0-9]{64}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SOURCE_KIND = {"extension": "reply-extension", "denial": "appeal-denial", "fee": "fee-reply"}


class DraftSourceError(ValueError):
    """The received item cannot be drafted from (content-free message)."""


def _citation(page, quote):
    return type(page) is int and page >= 1 and isinstance(quote, str) and bool(quote.strip())


def _need(condition, message):
    if not condition:
        raise DraftSourceError(message)


def _text(value):
    return isinstance(value, str) and not _CONTROL.search(value)


def linked(item):
    """True when the item names both its agency and its request (never guessed)."""
    return all(isinstance(item.get(k), str) and item[k] for k in ("agency_id", "request_id"))


def validate_item(item):
    """Raise DraftSourceError for a malformed item; return normally for a drafting-eligible shape."""
    _need(isinstance(item, dict) and isinstance(item.get("item_id"), str) and _SHA.fullmatch(item["item_id"]),
          "item_id")
    kind = item.get("kind")
    _need(kind in ("response", "extension", "denial", "fee", "records", "other"), "kind")
    basis = item.get("kind_basis")
    _need(isinstance(basis, dict), "kind_basis")
    if kind == "other":
        _need(basis.get("page") is None and basis.get("quote") is None, "kind_basis")
    else:
        _need(_citation(basis.get("page"), basis.get("quote")), "kind_basis")
    for key in ("agency_id", "request_id"):
        value = item.get(key)
        _need(value is None or (isinstance(value, str) and _ID.fullmatch(value)), key)
    received = item.get("received_at")
    _need(received is None or _text(received), "received_at")
    findings = item.get("findings")
    _need(isinstance(findings, list), "findings")
    for f in findings:
        _need(isinstance(f, dict) and isinstance(f.get("finding_id"), str) and _SHA.fullmatch(f["finding_id"]), "finding")
        _need(f.get("rating") in ("meets", "gap", "unknown"), "finding_rating")
        _need(_citation(f.get("page"), f.get("quote")), "finding_citation")
        _need(isinstance(f.get("rule_ref"), dict) and (f.get("policy_ref") is None or isinstance(f.get("policy_ref"), dict)),
              "finding_ref")
    deadlines = item.get("deadlines", [])
    _need(isinstance(deadlines, list), "deadlines")
    for d in deadlines:
        _need(isinstance(d, dict) and _citation(d.get("page"), d.get("quote")) and _text(d.get("date_text", "")),
              "deadline")


def select(item):
    """The one draft kind this item calls for, or None."""
    kind = item["kind"]
    if kind in _SOURCE_KIND:
        return _SOURCE_KIND[kind]
    if kind in ("response", "records") and any(f["rating"] == "gap" for f in item["findings"]):
        return "followup-missing-records"
    return None


def _block(quote):
    # Quote exactly (Unicode and newlines kept) inside a visibly delimited, indented block.
    return "\n".join("    | " + line for line in quote.split("\n"))


def _ref(ref):
    if not isinstance(ref, dict):
        return None
    for key in ("citation", "rule_id", "id", "title", "criterion"):
        value = ref.get(key)
        if isinstance(value, str) and value and not _CONTROL.search(value):
            return value
    return json.dumps(ref, sort_keys=True, ensure_ascii=False)[:300]


def _deadline_lines(item):
    lines = []
    for number, d in enumerate(item.get("deadlines", []), 1):
        date = d.get("date")
        lines.append(f"  {number}. page {d['page']}: \"{d['quote']}\""
                     + (f" (date as the item recorded it: {date})" if date else " (date not resolved; check the page)"))
    return lines


def _header(item, kind, why):
    deadlines = _deadline_lines(item)
    deadline = ("quoted from the agency's own words (no legal calculation):\n" + "\n".join(deadlines)
                if deadlines else "none stated in the received item; " + EDIT.format("check the original"))
    received = item.get("received_at") or EDIT.format("received date not recorded")
    return (f"# Draft ({kind}): {item['agency_id']} / {item['request_id']}\n\n{DRAFT_BANNER}\n\n"
            f"Target agency: {item['agency_id']}\nBelongs to request: {item['request_id']}\n"
            f"Received item: {item['item_id']} (received {received})\nState: pending-approval\n"
            f"Why: {why}\nDeadline: {deadline}\nTo: {EDIT.format('recipient and contact; not taken from the record')}\n\n")


def _quote_basis(item):
    b = item["kind_basis"]
    return f"The agency wrote (page {b['page']}):\n\n{_block(b['quote'])}\n\n"


def _references(findings):
    out = []
    for number, f in enumerate(findings, 1):
        refs = [r for r in (_ref(f["rule_ref"]), _ref(f.get("policy_ref"))) if r]
        out.append(f"  {number}. page {f['page']}, finding {f['finding_id'][:12]}: \"{f['quote']}\""
                   + (f" [supplied references: {'; '.join(refs)}]" if refs else ""))
    return out


def render(item):
    """Return (kind, body, evidence) for an eligible item, or None. Raises DraftSourceError if malformed."""
    validate_item(item)
    kind = select(item)
    if kind is None or not linked(item):
        return None
    findings = item["findings"]
    gaps = [f for f in findings if f["rating"] == "gap"]
    if kind == "reply-extension":
        why = "the agency recorded an extension notice; ask for the expected date and any written basis."
        text = (_header(item, kind, why) + _quote_basis(item)
                + "Thank you for your notice. We are not agreeing or objecting to anything by writing now; please tell us:\n"
                "1. The date by which you expect to send your response, or the date a first rolling production will be sent.\n"
                "2. Whether there is a written statement of the basis for the extension, and if so a copy of it.\n"
                "3. Whether any records are ready to release now.\n\n"
                f"Thank you,\n\n{EDIT.format('name')}\n\n")
    elif kind == "fee-reply":
        why = "the agency recorded a fee; ask for an itemized estimate and its basis. No payment is authorized."
        text = (_header(item, kind, why) + _quote_basis(item)
                + "Thank you for your notice about fees. Nothing here authorizes any charge or payment. Please send:\n"
                "1. An itemized estimate of the cost, with the legal basis for each charge you rely on.\n"
                "2. Confirmation that the records can be provided as electronic copies.\n"
                "3. Whether any waiver or reduction is available.\n\n"
                f"Thank you,\n\n{EDIT.format('name')}\n\n")
    elif kind == "appeal-denial":
        why = "the agency recorded a denial; this is a draft request for reconsideration, not a filed appeal."
        quoted = _block(item["kind_basis"]["quote"])
        body = kit_track.DENIAL_TEMPLATE.format(
            name=item["agency_id"], subject=f" ({item['request_id']})", first="",
            sent_on=EDIT.format("date the request was sent"), channel=EDIT.format("channel"),
            denied_on=item.get("received_at") or EDIT.format("received date not recorded"), status="draft",
            stated=f"The agency wrote, as quoted from page {item['kind_basis']['page']}:\n{quoted}\n\n",
            first_ask=kit_track.ASK_REST, citations="")
        refs = _references(findings)
        text = (_header(item, kind, why) + body
                + ("\nReferences supplied with the item (review required):\n" + "\n".join(refs) + "\n" if refs else "")
                + "\nThis is a request that the agency reconsider; confirm by hand whether the jurisdiction has a formal "
                  "appeal route before relying on it.\n\n")
    else:
        why = ("a cited documentary discrepancy needs review; it is not proof that anything was withheld.")
        lines = _references(gaps)
        text = (_header(item, kind, why) + _quote_basis(item)
                + "Thank you for the response concerning this request. Items numbered below are discrepancies in the "
                  "response as supplied, quoted with their pages. Please provide the existing record for each, or "
                  "clarify whether it was withheld, not located, or outside the search performed:\n"
                + "\n".join(lines) + "\n\n"
                  "If a record was withheld, please name the legal basis for each record or category.\n\n"
                  f"Thank you,\n\n{EDIT.format('name')}\n\n")
    text += FOOTER
    _need(len(text.encode("utf-8")) <= MAX_BODY_BYTES, "body_bound")
    evidence = {"item_id": item["item_id"], "kind_basis": dict(item["kind_basis"]),
                "findings": [{k: f.get(k) for k in ("finding_id", "rating", "rule_ref", "policy_ref", "page", "quote",
                                                    "comparison_ref")} for f in (gaps if kind == "followup-missing-records"
                                                                                  else findings)],
                "deadlines": [{k: d.get(k) for k in ("date_text", "date", "page", "quote")}
                              for d in item.get("deadlines", [])]}
    return kind, text, evidence


# ---- no-response follow-ups (REC-AUTO-7): facts from the request register only -----------------

_FOLLOWUP_FOOTER = ("---\nDraft only. Not legal advice. Review required. Dates come from the request register, "
                    "which marks them provisional; check receipt dates and holidays by hand.\n")
_BASIS_WORDS = {"recorded_receipt": "counted from the recorded receipt date",
                "submission_proxy": "counted from the submission date, because no receipt date is recorded"}
_LADDER = ("Notes for the reviewer (remove before sending):\n"
           "  Ladder: 1 reminder, 2 formal notice (only after this reminder is approved and the wait has passed), "
           "3 supervisor or records-office head, 4 any appeal or legal route the law package lists for this "
           "jurisdiction; check each by hand.\n")


def _clean(value, limit=500):
    text = " ".join(str(value).split())
    return _CONTROL.sub("", text)[:limit]


def render_followup(kind, *, agency, request_id, asked, request_date, clock_kind, clock_date, deadline, limits,
                    as_of):
    """Return the body of one follow-up draft. ``deadline`` and ``limits`` are the register's own values."""
    _need(kind in FOLLOWUP_KINDS and clock_kind in ("determination", "promised"), "followup_kind")
    _need(all(isinstance(v, str) and v for v in (agency, request_id, clock_date, as_of)), "followup_fields")
    agency, request_id, asked = _clean(agency, 200), _clean(request_id, 128), _clean(asked)
    sent = request_date or EDIT.format("request date not recorded")
    if clock_kind == "determination":
        basis = _BASIS_WORDS.get(deadline.get("basis"), "basis not recorded")
        extension = " An extension is recorded for this request." if deadline.get("extension_recorded") else ""
        clock = (f"Clock: the determination reminder date in our register was {clock_date}, {basis}.{extension}\n"
                 + "\n".join(_clean(line, 600) for line in limits[1:3]) + "\n")
        ask = ("Please send your written determination on this request, or tell us the date by which you expect "
               "to send it.")
    else:
        clock = f"Clock: the date the agency gave: {clock_date}.\n"
        ask = ("Please send the records you described, or tell us a new date by which you expect to send them.")
    recorded = "written determination" if clock_kind == "determination" else "production"
    if kind == "formal-notice-no-response":
        step = (f"This is a formal written notice following our earlier reminder sent on "
                f"{EDIT.format('date you sent the reminder')}. Please reply in writing to this request.")
        title = "Formal notice: no " + recorded + " recorded"
    else:
        step = (f"This is a courtesy reminder. Our register shows no {recorded} recorded as of " + as_of + ".")
        title = "Reminder: no " + recorded + " recorded"
    text = (f"# Draft ({kind}): {agency} / {request_id}\n\n{DRAFT_BANNER}\n\n"
            f"Target agency: {agency}\nBelongs to request: {request_id}\nState: pending-approval\n"
            f"To: {EDIT.format('recipient and contact; not taken from the record')}\n"
            f"Subject: {title} for public records request {request_id}\n\n"
            f"Hello,\n\nI am writing about public records request {request_id}, dated {sent} in our records. "
            f"The request asked for: {asked}\n\n{clock}\n{step}\n\n{ask}\n\n"
            f"Thank you,\n\n{EDIT.format('name')}\n\n{_LADDER}\n{_FOLLOWUP_FOOTER}")
    _need(len(text.encode("utf-8")) <= MAX_BODY_BYTES, "body_bound")
    return text
