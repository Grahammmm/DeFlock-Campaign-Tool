"""Private gap and possible-misuse PROPOSALS per agency and request.

Built from the SAME run's received-items artifact (REC-AUTO-3), the current page
text and the campaign's law package. Everything is a question for a human
reviewer: a gap is a documentary comparison and a lead is a reason to ask, never
a legal finding. Rules are fixed and decide every confidence. An optional local
yes/no scorer (the H2O model) adds a recorded score and probabilities, and can
only MOVE a proposal to the review list; with no scorer or no answer a fixed
deterministic score is used and logged. Nothing here is sent, drafted or
published, and no module outside campaign_tool/records/ may import it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from datetime import date, datetime, timezone

from ...digest import detectors
from ...kit_sharing_suggest import NOTICE
from ...legal_claims import legal_claims
from ..understanding.evidence import read_pinned, sha
from . import received_items as ri
from .comparison import citation

SCHEMA = "records-gap-proposals-v1"
INPUT = "received-items.json"
OUTPUT = "gap-proposals.json"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_QUOTE = 600
CONFIDENCE = ("low", "medium", "high")
DETERMINISTIC_SCORE = {"high": 0.8, "medium": 0.5, "low": 0.25}
ANSWER_KINDS = frozenset({"response", "extension", "denial", "fee", "records"})
FINAL_KINDS = frozenset({"records", "denial"})
EXEMPT_CITE = re.compile(r"(?i)(?:\b79[2-3]\d\.\d{2,3}\b|\b6254(?:\.\d+)?\b|\b6255\b|\bexempt\w*\b[^.]{0,80}\b(?:code|section|§)\b"
                         r"|\b(?:code|section|§)\s*[^.]{0,40}\b(?:exempt|7927|7928|7929|7930)\w*)")
FEE_BASIS = re.compile(r"(?i)\b(staff time|labor|hourly|per hour|search(?:ing)? time|review time|redact\w*|"
                       r"administrative (?:cost|fee)s?)\b")
SECTION = {"sharing": "1798.90.55(b)", "immigration": "7284.6"}
DISCLAIMER = ri.DISCLAIMER
LIMITS = ("Fixed rules decide every confidence; a model score only moves an item to the review list.",
          "A lead is a reason to ask, not evidence of misuse.",
          "A gap is a documentary comparison, not a legal finding.",
          "Nothing here is published or sent.")
# Fixed question templates: no agency name, no accusation (a test passes each through legal_claims).
QUESTIONS = {
    ("policy_gap", None): "Does the material on page {page} meet the comparison point cited for it, and what should be asked?",
    ("misuse_lead", "sharing"): "Does the sharing described on page {page} fall within the sharing limits in the agency's ALPR policy?",
    ("misuse_lead", "immigration"): "Does the immigration-related text on page {page} describe any data access or sharing, and is it within the policy?",
    ("promised_not_received", None): "The agency stated a date on page {page}; has the further response or the records arrived since?",
    ("deadline_passed_no_answer", None): "The recorded determination date has passed with no answer recorded; was a response received that is not yet in the inventory?",
    ("exemption_without_citation", None): "Does the denial on page {page} identify the exemption or provision it relies on?",
    ("fee_review", None): "Is the fee basis on page {page} limited to the direct cost of duplication under the cited section?",
    ("missing_field", "request_id"): "Which request does the item on page {page} answer?",
    ("missing_field", "received_at"): "When was the item on page {page} received?",
}


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _lower(level):
    return CONFIDENCE[max(0, CONFIDENCE.index(level) - 1)]


def _proposal_id(agency, request, kind, key, item, page, quote):
    return hashlib.sha256(_canon(["records-gap-proposal-v1", agency, request, kind, key, item, page, quote]).encode()).hexdigest()


def _lines(units):
    for unit in units:
        for line in unit["text"].splitlines():
            if line.strip():
                yield unit["page"], line.strip()


def _day(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _when(value):
    return ri._utc(value)


def propose(received, page_text, package, *, run_id, requests=(), now=None, scorer=None):
    """Pure. received: parsed received-items.json; page_text: {original_sha256: [{page, text}]};
    requests: ledger request rows (dicts); now: aware datetime; scorer: yes/no callable or None."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    status = (package or {}).get("status") or "draft"
    pool, diagnostics, unlinked = [], [], 0
    items = [i for i in received.get("items", []) if isinstance(i, dict)]
    by_agency = {}
    for item in items:
        if item.get("agency_id") is None:
            unlinked += 1
        else:
            by_agency.setdefault(item["agency_id"], []).append(item)
    not_proposed = 0

    def add(item, kind, key, page, quote, rule_ref, policy, confidence, basis, reason, *, agency=None, request=None,
            evidence="document", unreviewed=False):
        agency = agency or item["agency_id"]
        request = request if request is not None or item is None else item.get("request_id")
        flags = []
        if policy is None:
            flags.append("policy_section_unavailable")
        if unreviewed:
            confidence = _lower(confidence)
            flags.append("text_unreviewed")
        if not quote or (evidence == "document" and not isinstance(page, int)):
            diagnostics.append({"type": kind, "item_id": item and item["item_id"], "reason": "quote_or_page_missing"})
            return
        quote = quote[:MAX_QUOTE]
        source = item["item_id"] if item else f"request:{request}"
        pool.append({"proposal_id": _proposal_id(agency, request, kind, key, source, page, quote), "agency_id": agency,
                     "request_id": request, "type": kind, "detector_or_field": key, "confidence": confidence,
                     "confidence_basis": basis, "item_id": item["item_id"] if item else None, "source_id": source,
                     "evidence_kind": evidence, "page": page, "quote": quote, "rule_ref": rule_ref,
                     "policy_section": policy, "flags": flags, "reason": reason,
                     "question": QUESTIONS[(kind, key if (kind, key) in QUESTIONS else None)].format(page=page),
                     "review_required": True, "legal_conclusion": False, "law_package_status": status,
                     "disclaimer": DISCLAIMER})

    for agency, agency_items in sorted(by_agency.items()):
        sharing_policy = next((f["policy_ref"] for i in agency_items for f in i.get("findings", [])
                               if f.get("comparison_ref", {}).get("field") == "sharing_limits" and f.get("policy_ref")), None)
        for item in sorted(agency_items, key=lambda i: i["item_id"]):
            unreviewed = bool(item.get("needs_visual_review_unresolved"))
            units = page_text.get(item.get("original_sha256"), [])
            for finding in item.get("findings", []):
                if finding.get("rating") != "gap":
                    not_proposed += 1
                    continue
                policy = finding.get("policy_ref")
                add(item, "policy_gap", finding.get("comparison_ref", {}).get("field"), finding.get("page"), finding.get("quote"),
                    finding.get("rule_ref"), policy, "high" if policy else "medium",
                    ["comparison_gap_rating", "policy_ref" if policy else "statutory_comparison"],
                    finding.get("comparison_reason") or "The comparison rated this point as a gap.", unreviewed=unreviewed)
            kind = item.get("kind")
            if kind == "records":
                seen, audit_pages = set(), {p for p, l in _lines(units) if detectors.AUDIT.search(l)}
                for hit in detectors.detect_sharing(({"locator": {"page": u["page"]}, "text": u["text"]} for u in units), package):
                    name = "immigration" if hit["detector"] == "immigration_enforcement_mention" else "sharing"
                    page = hit["locator"].get("page")
                    if (name, page) in seen:
                        continue
                    seen.add((name, page))
                    rule = citation(SECTION[name], law="GOV" if name == "immigration" else "CIV")
                    medium = page in audit_pages
                    add(item, "misuse_lead", name, page, hit["excerpt"], rule, sharing_policy, "medium" if medium else "low",
                        ["sharing_or_immigration_keyword"] + (["audit_log_on_same_page"] if medium else []),
                        NOTICE + ": the text names a party or term the policy limits; this only asks whether the policy was followed.",
                        unreviewed=unreviewed)
            if kind == "denial":
                text = "\n".join(u["text"] for u in units)
                if text and not EXEMPT_CITE.search(text):
                    basis = item.get("kind_basis") or {}
                    add(item, "exemption_without_citation", None, basis.get("page"), basis.get("quote"),
                        citation("7922.540", law="GOV"), None, "medium", ["denial_without_provision_pattern"],
                        "No Public Records Act section or named exemption was found in the extracted text of this denial.",
                        unreviewed=unreviewed)
            if kind == "fee":
                for page, line in _lines(units):
                    if FEE_BASIS.search(line):
                        add(item, "fee_review", None, page, line, citation("7922.530", law="GOV"), None, "low",
                            ["fee_basis_keyword"], "The fee text mentions a basis other than direct duplication cost.",
                            unreviewed=unreviewed)
                        break
            basis = item.get("kind_basis") or {}
            missing = [f for f in ("request_id", "received_at") if item.get(f) is None]
            for field in missing if kind in ANSWER_KINDS and basis.get("quote") else ():
                add(item, "missing_field", field, basis.get("page"), basis.get("quote"), citation("7922.535", law="GOV"), None,
                    "low", ["missing_" + field], f"The item has no {field.replace('_', ' ')} recorded.", unreviewed=unreviewed)
            stated = next((d for d in item.get("deadlines", []) if d.get("date")), None)
            due = _day(stated and stated["date"])
            if stated and due and due < today:
                def later(o):
                    a, b = _when(o.get("received_at")), _when(item.get("received_at"))
                    return a is not None and b is not None and a > b
                answered = any(o["item_id"] != item["item_id"] and o.get("kind") in FINAL_KINDS
                               and o.get("request_id") == item.get("request_id") and later(o) for o in agency_items)
                if not answered:
                    inferred = bool(stated.get("year_inferred"))
                    add(item, "promised_not_received", "stated_date", stated["page"], stated["quote"],
                        citation("7922.535", law="GOV"), None, "low" if inferred else "medium",
                        ["stated_date_passed", "no_later_records_or_denial"] + (["year_inferred"] if inferred else []),
                        f"The agency stated {stated['date_text']}, which has passed, and no later records or denial is in the inventory.",
                        unreviewed=unreviewed)
    answered_requests = {(i["agency_id"], i["request_id"]) for i in items if i.get("kind") in ANSWER_KINDS and i.get("request_id")}
    for req in requests:
        due = _day((req.get("determination_due_at") or "")[:10])
        key = (req.get("agency_id"), req.get("id"))
        if due and due < today and not req.get("determination_at") and key not in answered_requests:
            add(None, "deadline_passed_no_answer", "determination_due_at", None,
                f"determination_due_at={req['determination_due_at']}; no determination recorded",
                citation("7922.535", law="GOV"), None, "medium", ["ledger_due_date_passed", "no_answer_item"],
                "The recorded determination date has passed with no determination or answer in the inventory.",
                agency=req["agency_id"], request=req["id"], evidence="ledger_field")
    pool = list({p["proposal_id"]: p for p in pool}.values())
    log = []
    for proposal in pool:
        verdict = _score(scorer, proposal)
        proposal["score"] = {k: verdict[k] for k in ("source", "answer", "p_yes")}
        log.append({"proposal_id": proposal["proposal_id"], **verdict})
        reasons = ([f"confidence_{proposal['confidence']}"] if proposal["confidence"] == "low" else []) + proposal["flags"]
        if verdict["source"] == "model" and verdict["answer"] == "no":
            reasons.append("model_does_not_support")
        proposal["review_bucket"] = "review_list" if reasons else "proposal"
        proposal["review_reasons"] = sorted(set(reasons))
    grouped = {}
    for proposal in pool:
        grouped.setdefault(proposal["agency_id"], {}).setdefault(proposal["request_id"], []).append(proposal)
    agencies = [{"agency_id": a, "requests": [
        {"request_id": r, "scope": "request" if r else "agency_wide", "proposals": sorted(p, key=lambda x: x["proposal_id"])}
        for r, p in sorted(reqs.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))]}
        for a, reqs in sorted(grouped.items())]
    counts = {"policy_gap": 0, "misuse_lead": 0, "by_confidence": {"high": 0, "medium": 0, "low": 0}, "not_proposed": not_proposed}
    for proposal in pool:
        counts[proposal["type"]] = counts.get(proposal["type"], 0) + 1
        counts["by_confidence"][proposal["confidence"]] += 1
    return {"schema": SCHEMA, "run_id": run_id, "publishable": False, "review_required": True, "legal_conclusion": False,
            "law_package_status": status, "disclaimer": DISCLAIMER,
            "received_items_sha256": hashlib.sha256((_canon(received) + "\n").encode()).hexdigest(),
            "agencies": agencies, "counts": counts, "unlinked_items": unlinked,
            "review_list": sorted(p["proposal_id"] for p in pool if p["review_bucket"] == "review_list"),
            "model_log": sorted(log, key=lambda e: e["proposal_id"]),
            "diagnostics": diagnostics, "limits": list(LIMITS)}


def _score(scorer, proposal):
    """Model yes/no with probabilities when it answers; otherwise the fixed deterministic score (logged either way)."""
    fallback = {"source": "deterministic", "answer": None, "p_yes": DETERMINISTIC_SCORE[proposal["confidence"]],
                "question": proposal["question"], "logprobs": []}
    if scorer is None:
        return fallback
    try:
        got = scorer(proposal["question"], proposal["quote"])
        p_yes = float(got["p_yes"])
        if got.get("answer") not in ("yes", "no") or not 0.0 <= p_yes <= 1.0:
            return fallback
        return {"source": "model", "answer": got["answer"], "p_yes": p_yes, "question": proposal["question"],
                "logprobs": [[str(t), float(lp)] for t, lp in got.get("logprobs", [])][:8]}
    except Exception:  # noqa: BLE001 - a model fault is never fatal: the deterministic score stands
        return fallback


def h2o_scorer(url, *, timeout=20):
    """Yes/no scorer over a LOOPBACK OpenAI-compatible endpoint (the local H2O server). Refuses any other host."""
    import math
    import urllib.parse
    import urllib.request
    host = urllib.parse.urlparse(url).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("gap_proposals_scorer_not_loopback")

    def ask(question, quote):
        body = {"messages": [{"role": "user", "content": f"Excerpt: {quote}\nQuestion: {question}\nAnswer with one word, yes or no."}],
                "max_tokens": 1, "temperature": 0, "logprobs": True, "top_logprobs": 5}
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback checked above
            top = json.load(response)["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        mass = {"yes": 0.0, "no": 0.0}
        for entry in top:
            word = entry["token"].strip().lower()
            if word in mass:
                mass[word] += math.exp(entry["logprob"])
        total = mass["yes"] + mass["no"]
        if total <= 0:
            return {"answer": None, "p_yes": 0.0}
        return {"answer": "yes" if mass["yes"] >= mass["no"] else "no", "p_yes": mass["yes"] / total,
                "logprobs": [(e["token"], e["logprob"]) for e in top]}
    return ask


def _safe_dir(path):
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        ri._need(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode), "gap_proposals_unsafe_path")
    return path


def _read_input(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    ri._need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_INPUT_BYTES, "gap_proposals_input_file")
    raw = path.read_bytes()
    try:
        received = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("gap_proposals_input_json") from None
    ri._need(type(received) is dict and received.get("schema") == ri.SCHEMA, "gap_proposals_input_schema")
    return received


def write_run_gap_proposals(project, run_id, *, jurisdiction="us-ca", scorer=None, now=None):
    """Outputs-stage hook, called after write_run_received_items. Returns the report.

    scorer defaults to the loopback H2O endpoint named by DEFLOCK_H2O_URL when set, else none."""
    from ...law import load_package
    from ..run_stages import LedgerBusy, snapshot_ro
    try:
        ri._need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is not None, "gap_proposals_run_id")
        project = _safe_dir(project)
        run_dir = _safe_dir(project / "run-outputs" / run_id)
        outcomes = _safe_dir(run_dir / "outcomes")
        received = _read_input(outcomes / INPUT)
        if received is None:
            return {"agencies": [], "skipped": "inputs_missing"}
        ri._need(received.get("run_id") == run_id, "gap_proposals_input_stale")
        processing = _safe_dir(project / "processing-v1")
        page_text, requests = {}, []
        with snapshot_ro(processing / "ledger.sqlite") as db:
            db.row_factory = sqlite3.Row
            for item in received.get("items", []):
                if item.get("extraction_status") == "current" and ri.is_hash(item.get("original_sha256")):
                    page_text[item["original_sha256"]] = ri._current_units(db, processing, item["original_sha256"])[0]
            if "requests" in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                requests = [dict(r) for r in db.execute("SELECT id,agency_id,determination_due_at,determination_at FROM requests ORDER BY id")]
        if scorer is None and os.environ.get("DEFLOCK_H2O_URL"):
            scorer = h2o_scorer(os.environ["DEFLOCK_H2O_URL"])
        report = propose(received, page_text, load_package(jurisdiction), run_id=run_id, requests=requests, now=now, scorer=scorer)
        encoded = (_canon(report) + "\n").encode()
        ri._need(len(encoded) <= ri.MAX_OUTPUT_BYTES, "gap_proposals_output_bound")
        outcomes.mkdir(mode=0o700, exist_ok=True)
        os.chmod(outcomes, 0o700)
        target = outcomes / OUTPUT
        if os.path.lexists(target):
            info = target.lstat()
            if not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= ri.MAX_OUTPUT_BYTES
                    and target.read_bytes() == encoded):
                raise OSError("gap_proposals_run_already_exists")
            return report
        temporary = outcomes / ".gap-proposals.json.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return report
    except (ValueError, sqlite3.Error, LedgerBusy) as error:
        raise OSError("GAP_PROPOSALS_UNAVAILABLE") from error
