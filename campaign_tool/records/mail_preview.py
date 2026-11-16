"""Read-only placement headers and draft warnings; no mailbox access or approval."""
import hashlib
import re

from . import approval_queue
from .outcomes.reading import esc

from . import draft_residue, draft_templates, mail_drafts


def preview(project, row, drafts_config_path=None):
    """Inspect a queue row using placement's thread and header rules.

    Attachments are evidence metadata only: build_message sends a plain-text letter.
    Missing inputs are display values, not permission to place or send a draft.
    """
    if row.get("kind") in draft_templates.REVIEW_COPY_KINDS:
        return {"to": None, "to_note": "Review copy only", "from": None, "from_note": "Not mail",
                "subject": None, "threaded": False, "attachments": [],
                "residue": draft_residue.codes(row["body"]), "body_header_lines": []}
    imported = row.get("kind") == "correspondence-draft"
    envelope = None
    try:
        if imported:
            envelope, thread, reason = mail_drafts.correspondence_envelope(project, row)
        else:
            thread, reason = mail_drafts._thread_for(project, row)
    except mail_drafts.MailDraftError as error:
        thread, reason = None, error.code
    if imported:
        to_note = ("operator-checked " + envelope["recipient_basis"]["source_type"] if envelope else
                   str(reason) + ": placement will refuse until envelope provenance is resolved")
    elif thread is not None:
        to_note = "from the agency's last message in this thread"
    else:
        to_note = "no thread found (%s)" % reason
        to_note += (": placement will refuse until thread provenance is resolved"
                    if reason.startswith("thread_provenance_")
                    else ": placement will be unthreaded")
    from_addr, from_note = None, "set by the Drafts mailbox config"
    if drafts_config_path is not None:
        try:
            from_addr = mail_drafts.load_config(drafts_config_path)["from_addr"]
        except mail_drafts.MailDraftError as error:
            from_note = error.code
    evidence = row.get("evidence") or {}
    attachments = evidence.get("attachments") if isinstance(evidence, dict) else None
    if not (isinstance(attachments, list) and all(
            isinstance(item, dict) and isinstance(item.get("name"), str)
            and ("sha256" not in item or isinstance(item["sha256"], str))
            for item in attachments)):
        attachments = []
    to = (thread.get("reply_to") or None) if thread is not None else None
    subject = mail_drafts._subject(thread, row)
    if imported:
        to = envelope["to"] if envelope else None
        subject = envelope["subject"] if envelope else None
    return {
        "to": to,
        "to_note": to_note, "from": from_addr, "from_note": from_note,
        "subject": subject,
        "threaded": thread is not None,
        "attachments": [{key: item[key] for key in ("name", "sha256") if key in item}
                        for item in attachments],
        "residue": draft_residue.codes(row["body"]),
        "body_header_lines": [n for n, line in enumerate(row["body"].splitlines()[:15], 1)
                              if re.match(r"^(To|Subject|From):", line)],
    }


REFRESH = "Refresh the export to see recipient and sources"


def evidence_sha256(row):
    return hashlib.sha256(approval_queue._canon(row["evidence"]).encode("utf-8")).hexdigest()


def review_context(project, row):
    """Prepare non-secret display facts on the owning host; never a placement override.

    send_ready means resolved recipient context only, not approval or permission to send.
    """
    prepared = preview(project, row)
    evidence = row["evidence"]
    target = evidence.get("target")
    return {"source_project_id": approval_queue._project_id(project), "queue_id": row["id"],
            "revision": row["revision"], "evidence_sha256": evidence_sha256(row),
            "recipient": prepared["to"], "target": target, "subject": prepared["subject"],
            "citations": evidence.get("citations"),
            "why": evidence.get("why") or " ".join(str(evidence.get(k) or "") for k in
                                                     ("clock_kind", "clock_date")).strip(),
            "limits": evidence.get("limits"),
            "source_attachment_sha256": evidence.get("source_attachment_sha256"),
            "source_section": evidence.get("source_section"),
            "send_ready": bool(prepared["to"]) and row["kind"] not in draft_templates.NON_MAIL_KINDS,
            "reason": "Not mail: review only" if target else prepared["to_note"],
            "preview": prepared}


def context_html(context):
    """Shared index/detail presentation; source strings are always escaped."""
    if context is None:
        return '<section class="review-context"><p role="status">%s</p></section>' % REFRESH
    out = ['<section class="review-context"><h2>Recipient, sources and purpose</h2><dl>']
    target = context.get("target")
    fields = [("Target " + k, v) for k, v in target.items()] if target else [
        ("Recipient", context.get("recipient") or "Unknown"),
        ("Recipient context", "Resolved (not send authorization)" if context.get("send_ready") else "Not send-ready")]
    fields += [("Subject", context.get("subject") or "Not supplied"), ("Context note", context.get("reason")),
               ("Purpose", context.get("why") or "Not supplied")]
    fields += [(key, context[key]) for key in ("source_attachment_sha256", "source_section") if context.get(key)]
    for label, value in fields:
        out.append('<dt>%s</dt><dd>%s</dd>' % (esc(label), esc(value)))
    out.append('</dl><h3>Cited records (review only)</h3>')
    citations = context.get("citations")
    if citations is None:
        out.append('<p>Sources unknown: producer supplied no citation list.</p>')
    elif not citations:
        out.append('<p>No citations supplied by producer.</p>')
    else:
        out.append('<ul>')
        for citation in citations:
            # Preserve typed provenance and all caveats, including missing-original/tracker-only facts.
            out.append('<li>%s</li>' % esc('; '.join('%s: %s' % (k, v if v is not None else 'not supplied')
                                                  for k, v in citation.items()) if isinstance(citation, dict) else str(citation)))
        out.append('</ul>')
    limits = context.get("limits") or []
    for limit in limits if isinstance(limits, list) else [limits]:
        out.append('<p>%s</p>' % esc(limit))
    return "\n".join(out + ['</section>'])
