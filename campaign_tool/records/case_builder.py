"""Read-only, private follow-up cases from routed residual scope. Never sends."""
import copy
import csv
import hashlib
import io
import re

from .. import law
from . import campaign_config, followup_drafter as drafter, followup_findings, followup_route, followup_state
from .intake.folder import secure_open
from .ledger.store import checked_path
from .outcomes import timeline

ASK_COLUMNS = ["ask_id", "message", "item_no", "item_text", "period", "response", "state", "held_doc_ids"]
PRIOR_NOTE = "prior-asks ledger not staged; check repeated asks by hand (R1, R7)"
VAGUE = "vague_answer"
VAGUE_PHRASE = "the cited exemption or search"
WRITTEN_DENIAL_SUFFIX = "-written-denial"
CITATION_LABEL = "agency's citation; UNVERIFIED by us"


class CaseError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise CaseError(code)


CONTRADICT = "records_contradict_policy"
_SETTING = re.compile(r"\b(retention|sharing)\b", re.IGNORECASE)


def _law_context(project):
    """The campaign-selected law package's written-denial rule, chosen by rule identity; never an agency citation."""
    try:
        name = campaign_config.load(project).law_package
        package = law.load_package(name)
        raw = law.package_path(name).read_bytes()
    except (ValueError, OSError):
        raise CaseError("vague_law_unavailable") from None
    rules = [r for r in package.get("rules", []) if str(r.get("rule_id", "")).endswith(WRITTEN_DENIAL_SUFFIX)]
    _need(len(rules) == 1 and all(isinstance(rules[0].get(k), str) and rules[0][k].strip()
                                  for k in ("rule_id", "citation", "duty", "review")), "vague_law_rule_missing")
    rule = rules[0]
    return {"package": name, "status": package.get("status"), "rule_id": rule["rule_id"], "review": rule["review"],
            "citation": rule["citation"], "duty": rule["duty"],
            "sources": [s["url"] for s in rule.get("sources", []) if isinstance(s, dict) and s.get("url")],
            "package_sha256": hashlib.sha256(raw).hexdigest()}


def _source_item(project, run_id, agency, sha):
    try:
        report = followup_state._read(project / "run-outputs" / run_id / "outcomes" / "received-items.json")
    except FileNotFoundError:
        return None
    found = [i.get("item_id") for i in report.get("items", []) if isinstance(i, dict)
             and i.get("original_sha256") == sha and i.get("agency_id") == agency]
    return found[0] if len(found) == 1 and isinstance(found[0], str) else None


def vague_context(project, run_id, route_row):
    """Resolve a vague_answer route's locators back to the same-run denial log (a route's own claim is never authority).

    Raises CaseError, with nothing written, on any identity, hash, page or quote-digest mismatch.
    """
    from . import denial_log  # deferred: denial_log imports followup_route
    located = route_row.get("vague_answer_evidence")
    _need(isinstance(located, list) and located and all(isinstance(x, dict) for x in located), "vague_evidence_missing")
    report = denial_log.load(project, run_id)
    _need(report.get("state") == "available" and report.get("run_id") == run_id, "vague_evidence_unavailable")
    key = (route_row.get("agency_key"), route_row.get("request_id"))
    by_id = {e["id"]: e for e in report["entries"] if (e.get("agency_key"), e.get("request_id")) == key}
    root = followup_state.received_items._safe_dir(project)
    entries = []
    for locator in located:
        entry = by_id.get(locator.get("denial_id"))
        _need(entry is not None, "vague_evidence_mismatch")
        kind, expected = followup_route._vague_candidate(entry)
        _need(kind == "eligible" and expected == locator, "vague_evidence_mismatch")
        words = entry["agency_words"]
        entries.append({"denial_id": entry["id"], "item_id": _source_item(root, run_id, key[0], words["doc_sha256"]),
                        "quote": words["quote"], "page": words["page"], "doc_sha256": words["doc_sha256"],
                        "agency_citation": {"text": list(entry["agency_citation"]["text"]), "label": CITATION_LABEL}})
    return {"entries": entries, "law_package": _law_context(project)}


def _check_vague(route_row, vague):
    """The route's nontext locators must be exactly the supplied context's entries (page, hash, quote digest)."""
    located = route_row.get("vague_answer_evidence")
    _need(isinstance(vague, dict) and isinstance(located, list) and located and isinstance(vague.get("entries"), list)
          and isinstance(vague.get("law_package"), dict) and all(
              isinstance(vague["law_package"].get(k), str) and vague["law_package"][k].strip()
              for k in ("package", "status", "rule_id", "review", "citation", "duty", "package_sha256")),
          "case_vague_mismatch")
    have = [{"denial_id": e.get("denial_id"), "page": e.get("page"), "doc_sha256": e.get("doc_sha256"),
             "quote_sha256": followup_route.quote_digest(e["quote"]) if isinstance(e.get("quote"), str) else None}
            for e in vague["entries"]]
    key = lambda d: sorted(d.items())  # noqa: E731
    _need(sorted(map(key, have)) == sorted(map(key, located)) and all(
        e.get("agency_citation", {}).get("label") == CITATION_LABEL for e in vague["entries"]), "case_vague_mismatch")


def _contradiction_asks(route_row, agency, request_id, finding):
    """Validate the caller's descriptor against the route, then build one new ask per cited fact (signed order)."""
    keys = ("kind", "agency_key", "request_id", "finding_id", "finding_sha256", "evidence_sha256", "facts", "valid")
    _need(isinstance(finding, dict) and all(k in finding for k in keys) and finding["valid"] is True
          and finding["kind"] == followup_findings.KIND, "case_finding_mismatch")
    _need((finding["agency_key"], finding["request_id"]) == (agency, request_id)
          and route_row.get("finding") == followup_findings.locator(finding), "case_finding_mismatch")
    facts = finding["facts"]
    _need(isinstance(facts, list) and facts and all(
        isinstance(f, dict) and set(f) == {"fact_id", "source_sha256", "page", "quote"} and type(f["page"]) is int
        and f["page"] > 0 and all(isinstance(f[k], str) and f[k].strip() for k in ("fact_id", "source_sha256", "quote"))
        for f in facts) and len({f["fact_id"] for f in facts}) == len(facts)
        and followup_findings.evidence_digest(facts) == finding["evidence_sha256"], "case_finding_evidence_invalid")
    asks = [{"ask_id": "finding:%s:%s" % (finding["finding_id"], f["fact_id"]), "number": n, "rating": "missing",
             "text": "Please produce existing records explaining the discrepancy identified in finding %s concerning "
                     "record %s, page %s, fact %s: %s." % (finding["finding_id"], f["source_sha256"], f["page"],
                                                           f["fact_id"], f["quote"].strip().rstrip("."))}
            for n, f in enumerate(facts, 1)]
    slots = {"n": "; ".join("record %s page %s (fact %s)" % (f["source_sha256"][:12], f["page"], f["fact_id"])
                            for f in facts)}
    words = {w.lower() for f in facts for w in _SETTING.findall(f["quote"])}
    if len(words) == 1:
        slots["retention_or_sharing"] = words.pop()
    return asks, slots, {k: copy.deepcopy(finding[k]) for k in keys[:-1]}


def build(route_row, timeline_request, items, *, asks_rows=None, policy_rows=(), policy_ref=None, finding=None,
          vague=None):
    """Build a review-required case; only route.asks_for becomes draftable asks.

    policy_rows is accepted for the caller's matching drafter invocation; a section
    is never selected implicitly. Missing contact information stays missing.
    A reviewed-contradiction route builds its asks only from the finding's cited facts (FLK-REC-2).
    A vague_answer route needs the caller-resolved `vague` context (vague_context); its locators must match it (FLK-REC-4).
    """
    pattern = route_row.get("pattern")
    extras = route_row.get("extra_patterns", [])
    _need(isinstance(extras, list), "route_pattern_unmapped")
    _need(all(isinstance(p, str) and drafter.RESPONSE_TYPES.get(p) == p
              for p in [pattern, *extras]), "route_pattern_unmapped")
    _need(len(set([pattern, *extras])) == len(extras) + 1, "route_pattern_duplicate")
    _need(route_row.get("handled_by", "engine") == "engine", "route_not_actionable")
    agency, request_id = timeline_request.get("agency_key"), timeline_request.get("request_id")
    _need(all(isinstance(v, str) and v.strip() for v in
              (agency, request_id, timeline_request.get("agency"))), "case_identity_missing")
    for row in (route_row, items):
        _need(all(row.get(k, v) == v for k, v in (("agency_key", agency), ("request_id", request_id))),
              "case_request_mismatch")
    by_number = {}
    for ask in items.get("asks", []):
        number = followup_state._number(ask)
        _need(number not in by_number, "case_duplicate_ask")
        by_number[number] = ask
    slots, reviewed = {}, None
    if pattern == VAGUE:
        _check_vague(route_row, vague)
        slots["phrase"] = VAGUE_PHRASE  # neutral wording: the agency's quote never enters the letter
    else:
        _need(vague is None and "vague_answer_evidence" not in route_row, "case_vague_mismatch")
    if pattern == CONTRADICT:
        _need(not extras and route_row.get("asks_for") == [], "case_finding_mismatch")
        new_asks, slots, reviewed = _contradiction_asks(route_row, agency, request_id, finding)
        numbers, asks = [], new_asks
    else:
        _need(finding is None and "finding" not in route_row, "case_finding_mismatch")
        numbers = route_row.get("asks_for")
        _need(isinstance(numbers, list) and numbers and all(type(n) is int and n > 0 for n in numbers)
              and len(set(numbers)) == len(numbers), "route_asks_invalid")
        _need(set(numbers) <= by_number.keys(), "route_asks_missing")
        asks = [dict({k: copy.deepcopy(v) for k, v in by_number[n].items()
                      if k in ("ask_id", "text", "rating", "produced")}, number=n) for n in numbers]
    case = {"schema": drafter.SCHEMA, "agency": {"id": agency, "name": timeline_request["agency"]},
            "request_id": request_id, "response_type": pattern, "extra_patterns": list(extras),
            "asks": asks, "prior_asks": [], "slots": slots, "review_required": True}
    if reviewed:
        case["finding"] = reviewed
    if pattern == VAGUE:
        case["vague_answer"] = copy.deepcopy(vague)
    case["received_asks"] = [{"number": n, "text": a["text"]} for n, a in by_number.items()
                             if n not in numbers and a.get("rating") == "answered"]
    if asks_rows is None:
        case["slots"]["prior_asks_note"] = PRIOR_NOTE
    else:
        for row in asks_rows:
            if row.get("state") in ("produced", "pending"):
                _need(bool(row.get("item_text")), "prior_ask_text_missing")
                # Ledger ask ids need not share the response-items numbering scheme.
                case["prior_asks"].append({"text": row["item_text"], "status": row["state"]})
    events = [e for e in timeline_request.get("events", [])
              if not e.get("superseded") and timeline._day(e.get("date"))]
    sent = [e["date"] for e in events if e.get("type") == "sent"]
    replies = [e["date"] for e in events if e.get("type") in followup_state._AGENCY]
    if sent:
        case["request_date"] = min(sent)
    if replies:
        case["response_date"] = max(replies)
    if pattern == "closed_new_request":
        case["slots"]["old_request_id"] = request_id
        closed = [e["date"] for e in events if e.get("type") == "closed"]
        if closed:
            case["slots"]["closure_date"] = max(closed)
    if policy_ref is not None:
        case["policy"] = copy.deepcopy(policy_ref)
    drafter._validate(case)
    return case


def _asks(project, agency_key):
    _need(isinstance(agency_key, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", agency_key),
          "case_agency_invalid")
    path = checked_path(str(project / "agency-asks" / (agency_key + ".tsv")), existing=False)
    try:
        with secure_open(path) as stream:
            raw = stream.read(1024 * 1024 + 1)
    except FileNotFoundError:
        return None
    _need(len(raw) <= 1024 * 1024, "case_asks_too_large")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter="\t", strict=True)
    _need(reader.fieldnames == ASK_COLUMNS, "case_asks_columns")
    rows = list(reader)
    _need(all(None not in r and all(v is not None for v in r.values()) for r in rows), "case_asks_shape")
    return rows


def build_for_run(project, run_id, agency_key, request_id, *, analysis_root=None):
    """Read same-run outputs and optional operator-staged ledger, without writes.

    With an explicit analysis_root the reviewed contradiction is reloaded through the real adapter here;
    a route's own claim is never authority.
    """
    findings = None
    if analysis_root is not None:
        findings = followup_findings.load_reviewed_contradictions(project, run_id, analysis_root)
    routes = followup_route.route_run(project, run_id, findings=findings)  # validates run id and same-run schemas
    project = followup_state.received_items._safe_dir(project)
    root = project / "run-outputs" / run_id / "outcomes"
    key = (agency_key, request_id)
    by_route = followup_state._index(routes)
    requests = followup_state._index(followup_state._read(root / "timeline.json")["requests"])
    ratings = followup_state._read(root / "response-items.json")
    _need(ratings.get("schema") == followup_state.response_items.SCHEMA and ratings.get("run_id") == run_id,
          "case_ratings_stale")
    by_items = followup_state._index(ratings["requests"])
    _need(key in by_route and key in requests and key in by_items, "case_request_missing")
    chosen = by_route[key].get("finding")
    descriptor = next((d for d in findings or () if chosen and followup_findings.locator(d) == chosen
                       and (d["agency_key"], d["request_id"]) == key), None) if chosen else None
    vague = vague_context(project, run_id, by_route[key]) if by_route[key].get("pattern") == VAGUE else None
    return build(by_route[key], requests[key], by_items[key], asks_rows=_asks(project, agency_key), finding=descriptor,
                 vague=vague)
