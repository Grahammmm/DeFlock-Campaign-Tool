"""draft_followup: propose a follow-up letter; never send.

Inputs (cron supplies ``request_id`` and ``due``; the workspace or an
organizer adds the rest when known):

    {"request_id", "due", "request": {"subject", "agency_id", "agency_name", "sent_at", "scope_id",
     "scope_version", "channel", "to", "extension_claimed_until"},
     "last_correspondence": {"received_at", "classification", "summary", "provider_message_id"},
     "jurisdiction", "promised_date", "evidence_gap", "missing_item", "document"}

The template is ``templates/follow-up.md``. The statutory deadline is
``campaign_tool.law.deadline`` from ``sent_at`` (extension when the agency
claimed one). Per the template, no proposal is made when a fresh response
arrived after the due date or a promised date has not elapsed; the job
still finishes ``done`` with ``skipped`` set. Otherwise the draft is
proposed as a ``send_followup`` external action for organizer approval.
"""
import os
from datetime import date
from pathlib import Path

from campaign_tool import law
from ..loop import Result
from . import idempotency_key

TEMPLATE = Path(law.REPO_ROOT) / "templates" / "follow-up.md"


def _d(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def render(inputs, today, package=None):
    request = inputs.get("request") or {}
    last = inputs.get("last_correspondence") or {}
    sent = _d(request.get("sent_at"))
    extension = bool(request.get("extension_claimed_until"))
    statutory = law.deadline(package, sent, extension=extension).isoformat() if package and sent else None
    due = inputs.get("due") or statutory or "not supplied"
    fields = {
        "[ID and original date]": f"{inputs.get('request_id', 'unknown')} sent {request.get('sent_at') or 'date not supplied'}",
        "[time and source]": f"{last.get('received_at') or 'none on file'} ({last.get('classification') or 'no classification'})",
        "[date or not supplied]": inputs.get("promised_date") or "not supplied",
        "[exact scope]": request.get("scope_id") or "scope not supplied",
        "[file/page/sheet/field]": inputs.get("evidence_gap") or "not specified",
        "[missing exhibit, native field definition, narrow export,\nredaction basis, or rolling production date]": inputs.get("requested_step") or "a determination or the responsive records",
        "[request]": f"request {inputs.get('request_id', 'unknown')}" + (f" ({request['subject']})" if request.get("subject") else ""),
        "[document]": inputs.get("document") or "correspondence",
        "[precise missing item]": inputs.get("missing_item") or "records not yet produced",
        "[Add a reviewed local\nlegal basis only where applicable.]": (
            f"The determination period under {package['records_law']['citation']} ran through {statutory}; "
            f"the reviewed package status is {package['status']}." if package and statutory else ""),
    }
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in fields.items():
        text = text.replace(key, str(value))
    text = text.replace("[ID and original date]", "").strip()
    subject = "Follow-up: " + (request.get("subject") or f"public records request {inputs.get('request_id', '')}").strip()
    return {"subject": subject[:200], "body_md": text, "statutory_deadline": statutory, "due": due,
            "extension_claimed": extension}


def run(ctx):
    inputs = ctx.job.get("inputs") or {}
    request_id = inputs.get("request_id")
    if not request_id:
        return Result("failed", error="draft_followup needs request_id")
    today = _d(inputs.get("today")) or date.today()
    request = inputs.get("request") or {}
    last = inputs.get("last_correspondence") or {}
    due = _d(inputs.get("due"))
    received = _d(last.get("received_at"))
    if received and due and received >= due:
        return Result("done", {"request_id": request_id, "skipped": "fresh response after the due date; follow-up redundant"})
    promised = _d(inputs.get("promised_date")) or _d(request.get("extension_claimed_until"))
    if promised and promised >= today:
        return Result("done", {"request_id": request_id, "skipped": f"promised date {promised.isoformat()} has not elapsed"})
    package = None
    jurisdiction = inputs.get("jurisdiction") or os.environ.get("CAMPAIGN_JURISDICTION")
    if jurisdiction:
        try:
            package = law.load_package(jurisdiction, base=ctx.settings.jurisdictions_dir)
        except ValueError as exc:
            ctx.log("followup.no_package", job_id=ctx.job_id, error=str(exc)[:120])
    draft = render(inputs, today, package)
    proposal = {
        "request_id": request_id,
        "agency_id": request.get("agency_id"),
        "channel": request.get("channel") or "email",
        "to": request.get("to"),
        "in_reply_to": last.get("provider_message_id"),
        "subject": draft["subject"],
        "body_md": draft["body_md"],
        "due": inputs.get("due"),
        "statutory_deadline": draft["statutory_deadline"],
        "basis": "templates/follow-up.md; " + (f"{package['records_law']['citation']} ({package['status']})" if package else "no law package loaded"),
        "note": "Drafted by the runner; nothing was sent. Approve, edit or reject on the Approvals screen.",
    }
    key = idempotency_key("send_followup", request_id, inputs.get("due") or today.isoformat())
    action = ctx.workspace.propose("send_followup", request_id, proposal, key, ctx.job_id)
    ctx.log("followup.proposed", job_id=ctx.job_id, request_id=request_id, action_id=action.get("action_id"), created=action.get("created"))
    return Result("done", {"request_id": request_id, "action_id": action.get("action_id"), "created": action.get("created"),
                           "statutory_deadline": draft["statutory_deadline"]},
                  {"action_id": action.get("action_id")})
