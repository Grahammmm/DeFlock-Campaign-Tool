"""Recommended next steps for a run's gap proposals: a social post draft, a site-update diff and a follow-up probe.

Reads one run's ``outcomes/gap-proposals.json`` (REC-AUTO-9) and the campaign's ``content/agencies.json`` and queues
PENDING drafts in the approval queue (REC-AUTO-4). Wording is fixed templates only; no model is called. This code
never sends, posts or edits the live site: it writes nothing but approval-queue rows and never touches content/,
public/ or kit/. Approving a row (in the admin panel) records a decision only.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from .. import content, legal_claims, privacy_patterns, social
from . import approval_queue, draft_templates

SCHEMA = "records-gap-proposals-v1"
KINDS = draft_templates.NEXT_STEP_KINDS
SOCIAL, SITE, PROBE = KINDS
MAX_INPUT = 64 * 1024 * 1024
ELIGIBLE = ("high", "medium")
AGENCY_WIDE = "agency-wide"  # the queue needs a non-empty request id; a null request is stored under this label
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
LABELS = {"sharing": "data sharing", "immigration": "immigration-related access", "stated_date": "promised response dates",
          "request_id": "which request an item answers", "received_at": "dates of receipt"}
ASKS = {
    ("misuse_lead", "sharing"): "the ALPR data-sharing agreements in effect and the audit log entries for searches made by or "
                                "on behalf of agencies outside California during the period covered by the record on page {page}",
    ("misuse_lead", "immigration"): "the audit log entries and any records of access to or sharing of ALPR data for "
                                    "immigration-related purposes during the period covered by the record on page {page}",
    ("promised_not_received", None): "the further response or records the agency stated on page {page} it would provide",
    ("deadline_passed_no_answer", None): "the agency's determination on this request, or the date it will issue one",
    ("exemption_without_citation", None): "the provision or exemption the agency relies on for the denial on page {page}",
    ("fee_review", None): "the basis and an itemization of the fee stated on page {page}",
    ("missing_field", "request_id"): "confirmation of which request the item on page {page} answers",
    ("missing_field", "received_at"): "the date the item on page {page} was received",
}
GAP_ASK = ("the current ALPR usage and privacy policy section governing {label}, and any records showing how it "
           "was applied")


class NextStepsError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _read(path, code):
    """Bounded no-follow regular-file read; None when absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise NextStepsError(code) from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT:
            raise NextStepsError(code)
        return stream.read(MAX_INPUT + 1)


def _json(raw, code):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise NextStepsError(code) from None


def _item_id(kind, agency, request, ids, extra=None):
    material = ["records-next-step-v1", kind, agency, request, sorted(ids)] + ([extra] if extra else [])
    return hashlib.sha256(_canon(material).encode()).hexdigest()


def _label(proposal):
    field = proposal.get("detector_or_field")
    return LABELS.get(field) or (str(field).replace("_", " ") if field else "document comparison points")


def _evidence(group, why):
    return {"wording": "template", "why": why, "proposals": [
        {k: p.get(k) for k in ("proposal_id", "item_id", "page", "quote", "type", "confidence")} for p in group]}


def _social_text(name, group, base_url):
    n = len(group)
    labels = sorted({_label(p) for p in group})
    ids = sorted({p["item_id"][:12] for p in group if p.get("item_id")})
    noun = "question" if n == 1 else "questions"
    tail = f" {base_url}" if base_url else ""
    variants = []
    for with_labels in (True, False):
        for with_ids in (True, False):
            about = f" about {', '.join(labels)}" if with_labels and labels else ""
            rec = f" Source record: {', '.join(ids[:2])}." if with_ids and ids else ""
            variants.append(f"{name}: we received records in response to our public records requests and are reviewing "
                            f"{n} {noun}{about} against the agency's ALPR policy and state law.{rec}{tail}")
    return variants


def _social(name, group, base_url):
    """-> (text|None, diagnostic|None). Longer variants are tried first; a variant that trips the privacy lint is skipped."""
    refused = None
    for text in _social_text(name, group, base_url):
        if social.post_length(text, "x") > 280:
            continue
        found = privacy_patterns.categories(text)
        if found:
            refused = found
            continue
        if legal_claims.legal_claims(text):
            return None, "social_legal_claim_flagged"
        return text, None
    if refused:
        return None, "social_privacy_refused:" + ",".join(refused)
    return None, "social_too_long"


def _site_diff(root, agency_id, day):
    path = Path(root) / "content" / "agencies.json"
    raw = _read(path, "site_update_invalid")
    if raw is None:
        raise NextStepsError("site_file_missing")
    text = raw.decode("utf-8", "replace")
    doc = _json(raw, "site_update_invalid")
    indent = next((len(line) - len(line.lstrip(" ")) for line in text.splitlines()[1:] if line.startswith(" ")), 2) or 2
    entry = next((e for e in doc if isinstance(e, dict) and e.get("agency_id") == agency_id), None) \
        if isinstance(doc, list) else None
    if entry is None:
        raise NextStepsError("agency_not_in_content")
    status = f"Records received {day}; under review"
    profile = entry.get("profile")
    if profile is None:
        profile = entry["profile"] = {}
    if not isinstance(profile, dict):
        raise NextStepsError("site_update_invalid")
    if profile.get("status") == status:
        return None
    before = json.dumps(doc, indent=indent, ensure_ascii=False) + "\n"
    profile["status"] = status
    try:
        content.load_agencies(doc, "content/agencies.json")
    except content.ContentError:
        raise NextStepsError("site_update_invalid") from None
    after = json.dumps(doc, indent=indent, ensure_ascii=False) + "\n"
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="content/agencies.json",
                                        tofile="content/agencies.json (proposed)", n=2))


def _probe(name, request, group):
    asks = []
    for number, p in enumerate(group, 1):
        template = ASKS.get((p["type"], p.get("detector_or_field"))) or ASKS.get((p["type"], None)) or GAP_ASK
        ask = template.format(page=p.get("page"), label=_label(p))
        rule = p.get("rule_ref") if isinstance(p.get("rule_ref"), dict) else {}
        cite = rule.get("label") or rule.get("citation") or ""
        quote = " ".join(str(p.get("quote") or "").split()).replace('"', "'")  # inner double quotes would end the quotation
        asks.append(f'{number}. Please provide {ask}' + (f" ({cite})" if cite else "") +
                    f'. The record states, on page {p.get("page")}: "{quote}"')
    unquoted = "\n".join(re.sub(r'"[^"\n]*"', " ", a) for a in asks)
    if legal_claims.legal_claims(unquoted):
        return None
    scope = "request " + request if request else "our requests to this agency (agency-wide)"
    return (f"{draft_templates.DRAFT_BANNER}\n\nTo: {name}\nRe: follow-up on {scope}\n\n"
            "Thank you for the records provided. To understand them fully, we ask for the following:\n\n"
            + "\n".join(asks) + "\n\n" + draft_templates.FOOTER)


def recommend(proposals_doc, agencies, *, run_id, root=None, received=None, base_url=None):
    """Pure given its inputs (``root`` is only read for the site diff). -> {"drafts": [...], "diagnostics": [...], "skipped_low": n}."""
    names = {a["agency_id"]: a["name"] for a in agencies}
    drafts, diagnostics, skipped_low = [], [], 0
    days = {}
    for item in (received or {}).get("items", []):
        day = str(item.get("received_at") or "")[:10]
        if isinstance(item, dict) and DAY.fullmatch(day) and item.get("agency_id"):
            days[item["agency_id"]] = max(days.get(item["agency_id"], ""), day)

    def diag(code, agency=None):
        diagnostics.append({"code": code.split(":")[0], "agency_id": agency, **({"categories": code.split(":")[1].split(",")}
                                                                                 if ":" in code else {})})
    for entry in proposals_doc.get("agencies", []):
        agency, by_request = entry["agency_id"], []
        for req in entry.get("requests", []):
            keep = [p for p in req.get("proposals", []) if p.get("confidence") in ELIGIBLE]
            skipped_low += len([p for p in req.get("proposals", []) if p.get("confidence") not in ELIGIBLE])
            if keep:
                by_request.append((req.get("request_id"), keep))
        if not by_request:
            continue
        group = [p for _, g in by_request for p in g]
        ids = [p["proposal_id"] for p in group]
        name = names.get(agency)
        if name is None:
            diag("agency_not_in_content", agency)
        else:
            text, problem = _social(name, group, base_url)
            if problem:
                diag(problem, agency)
            else:
                drafts.append({"kind": SOCIAL, "agency_id": agency, "request_id": AGENCY_WIDE, "body": text,
                               "item_id": _item_id(SOCIAL, agency, None, ids),
                               "evidence": _evidence(group, f"{len(group)} proposal(s) of confidence high or medium for "
                                                            "this agency; a short factual post tells readers records arrived "
                                                            "and are under review.")})
            day = days.get(agency)
            if root is None:
                pass
            elif day is None:
                diag("received_date_missing", agency)
            else:
                try:
                    diff = _site_diff(root, agency, day)
                except NextStepsError as error:
                    diag(error.code, agency)
                else:
                    if diff:
                        drafts.append({"kind": SITE, "agency_id": agency, "request_id": AGENCY_WIDE, "body": diff,
                                       "item_id": _item_id(SITE, agency, None, ids, day),
                                       "evidence": _evidence(group, "The agency page should say records were received and "
                                                                    "are under review; the diff changes profile.status only.")})
        for request, keep in by_request:
            body = _probe(name or agency, request, keep)
            if body is None:
                diag("probe_legal_claim_flagged", agency)
                continue
            drafts.append({"kind": PROBE, "agency_id": agency, "request_id": request or AGENCY_WIDE, "body": body,
                           "item_id": _item_id(PROBE, agency, request, [p["proposal_id"] for p in keep]),
                           "evidence": _evidence(keep, "Each ask answers the gap or lead named in its evidence row.")})
    return {"drafts": drafts, "diagnostics": diagnostics, "skipped_low": skipped_low}


def _load(project, run_id, campaign_root):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or run_id in (".", ".."):
        raise NextStepsError("run_id_invalid")
    outcomes = Path(os.path.abspath(project)) / "run-outputs" / run_id / "outcomes"
    raw = _read(outcomes / "gap-proposals.json", "proposals_invalid")
    if raw is None:
        return None, None, None
    doc = _json(raw, "proposals_invalid")
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("agencies"), list):
        raise NextStepsError("proposals_invalid")
    if doc.get("run_id") != run_id:
        raise NextStepsError("proposals_stale")
    rec_raw = _read(outcomes / "received-items.json", "proposals_invalid")
    received = _json(rec_raw, "proposals_invalid") if rec_raw is not None else {}
    try:
        agencies = content.load_campaign_agencies(campaign_root)
    except (content.ContentError, OSError, ValueError):
        raise NextStepsError("content_invalid") from None
    return doc, agencies, received


def queue_run_next_steps(project, run_id, *, campaign_root, base_url=None, dry_run=False):
    try:
        doc, agencies, received = _load(project, run_id, campaign_root)
    except NextStepsError as error:
        return {"queued": 0, "skipped": error.code}
    if doc is None:
        return {"queued": 0, "skipped": "proposals_missing"}
    try:
        result = recommend(doc, agencies, run_id=run_id, root=campaign_root, received=received, base_url=base_url)
    except (KeyError, TypeError, AttributeError):
        return {"queued": 0, "skipped": "proposals_invalid"}
    report = {"queued": 0, "existing": 0, "skipped_low": result["skipped_low"], "diagnostics": result["diagnostics"]}
    if dry_run:
        report["drafts"] = result["drafts"]
        return report
    known = {r["id"] for r in approval_queue.load_queue(project)}
    for draft in result["drafts"]:
        ident = approval_queue.draft_id(draft["item_id"], draft["kind"])
        approval_queue.add_draft(project, source_run_id=run_id, **draft)
        report["existing" if ident in known else "queued"] += 1
        known.add(ident)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records next-steps", description="Queue recommended next-step drafts. Sends and publishes nothing.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--campaign-root", required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--dry-run", action="store_true", help="print the recommendations as JSON and write nothing")
    args = parser.parse_args(argv)
    try:
        report = queue_run_next_steps(args.project, args.run_id, campaign_root=args.campaign_root,
                                      base_url=args.base_url, dry_run=args.dry_run)
    except approval_queue.ApprovalQueueError as error:
        print(error.code, file=sys.stderr)
        return 3
    print(_canon(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
