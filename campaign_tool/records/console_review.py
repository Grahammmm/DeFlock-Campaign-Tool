"""One draft's editor; decisions reuse the existing signed approval POSTs."""
import urllib.parse

from datetime import datetime
from zoneinfo import ZoneInfo

from . import approval_queue, console_layout, console_snapshot, draft_templates, mail_drafts, mail_preview, mail_send
from .outcomes.reading import esc
from . import denial_log, denial_reply_check, followup_clock

READ_ONLY = "Read-only export: decisions are unavailable here."

RESIDUE = {"edit_placeholder": "[EDIT ME] placeholder left", "draft_heading": "Draft heading left",
           "draft_banner": "Draft banner left", "reviewer_notes": "Notes for the reviewer left",
           "before_sending": "BEFORE SENDING instructions left", "draft_footer": "Template footer left"}


def approval_label(row, switches):
    if row["kind"] in draft_templates.NEXT_STEP_KINDS:
        return "Approve (records the decision only; nothing is posted or sent)"
    if switches["can_send"]:
        return "Approve and send"
    reason = switches["reason"]
    return "Approve - sending is off (%s). This marks it Approved, not sent." % mail_send.REASONS.get(reason, reason)


def approval_status(project, row, switches, source=None):
    source = source or console_snapshot.LiveSource(project)
    if row["kind"] in draft_templates.NEXT_STEP_KINDS:
        return esc("Approved; records the decision only; nothing is posted or sent.")
    result = next((r for r in reversed(source.send_results())
                   if (r["queue_id"], r.get("revision")) == (row["id"], row["revision"])), {})
    if result.get("state") == "sent":
        when = datetime.fromisoformat(result["at"].replace("Z", "+00:00")).astimezone(ZoneInfo(mail_send.TZ))
        return esc("Approved and sent on %s. Message id %s" % (
            when.strftime("%Y-%m-%d %H:%M %Z"), str(result.get("message_id") or "")[:12]))
    if result.get("state") in ("attempt", "unknown"):
        return esc("Delivery unresolved; reconcile in the outbox before any retry")
    placement = next((r for r in reversed(source.mail_drafts())
                      if (r["queue_id"], r["revision"]) == (row["id"], row["revision"])), {})
    if placement.get("state") not in ("placed", "found_existing"):
        return esc("Approved, not sent. Not in the Drafts mailbox yet: %s." % (
            placement.get("code") or placement.get("state") or "not_placed"))
    if switches["can_send"]:
        return ('Approved. Next: <a href="/send?id=%s">open the send check</a> '
                '(it re-checks the mailbox copy and switches and asks for your outbox signature).') % esc(row["id"])
    reason = switches["reason"]
    return esc("Approved, not sent. Sending is off: %s." % mail_send.REASONS.get(reason, reason))


def refusal_panel(project, row, run_id):
    if row["kind"] not in ("appeal-denial", "followup-playbook"):
        return ""
    log = denial_log.load(project, run_id)
    if log["state"] != "available":
        return '<h2>Refusal being answered</h2><p>Refusal context unavailable; review the source by hand.</p>'
    try:
        register = followup_clock.read_register(project, run_id)
    except (OSError, ValueError, KeyError, TypeError):
        register = None
    requests = (register or {}).get("requests", [])
    ids = {row["request_id"]}
    for request in requests:
        if request["agency_key"] == row["agency_id"] and row["request_id"] in (request["request_id"], request.get("ledger_request_id")):
            ids.add(request["request_id"])
    entries = [r for r in log["entries"] if r["agency_key"] == row["agency_id"] and
               (r["request_id"] in ids or r.get("queue_id") == row["id"])]
    if not entries:
        return ""
    out = ['<section><h2>Refusal being answered</h2>', '<p>%s</p>' % esc(denial_log.LABEL)]
    for entry in entries:
        words = entry.get("agency_words") or {}
        quote = words.get("quote", "no quote extracted")
        out.append('<h3>%s (%s) · Item %s: %s · %s</h3>' % tuple(esc(str(v)) for v in (
            entry["date"] or "unknown", entry["date_basis"], entry["item_no"] if entry["item_no"] is not None else "whole request",
            entry["item_text"], entry["kind"].replace("_", " "))))
        out.append('<p>%s — page %s; doc %s</p>' % (esc(quote), esc(str(words.get("page", "unknown"))),
                                                        esc(words.get("doc_sha256", "unknown")[:12])))
        out.append('<p>Their citation: %s (UNVERIFIED by us)</p>' % esc(
            "; ".join(entry["agency_citation"]["text"]) or "none extracted"))
    names = {r["agency"] for r in requests if r["agency_key"] != row["agency_id"]}
    warnings = denial_reply_check.check(row["body"], subject_agency=row["agency_id"], other_agency_names=sorted(names))
    out.append('<h3>Advisory reply warnings</h3><p>Heuristics only; not legal review. Lawyer read recommended.</p><ul>')
    out.extend('<li>Line %s: %s</li>' % (w["line"], esc(w["message"])) for w in warnings)
    out.append('</ul><h3>By-hand checklist (not machine-checked)</h3><ul>')
    for question in ("Their letter names the people responsible for the denial (names and titles)?",
                     "Every withheld item is asked again or deliberately dropped?",
                     "Held documents listed with 'please do not resend'?"):
        out.append('<li>%s</li>' % esc(question))
    return "\n".join(out + ['</ul></section>'])


def render_link(row, token, payload, refusal=None, *, read_only=False):
    """A message_link row: the plain question, one choice per candidate, then the usual signed approve / reject."""
    from . import admin_panel as ap

    pending = row["status"] == "pending"
    out = ["<h1>Tie this message to a request</h1>", "<p role=\"status\">%s</p>" % esc(
        "Pending" if pending else row["status"].capitalize() + (": " + str(row.get("reason") or "") if row.get("reason") else "")),
           "<pre>%s</pre>" % esc(row["body"])]
    message, field_name, typed = refusal or (None, None, None)
    if message:
        out.append('<p role="alert" class="diag">%s. Nothing was saved or decided.</p>' % esc(message.rstrip(".")))
    if pending and read_only:
        out.append('<h2>Approve</h2><pre id="approval-payload">%s</pre><p>%s</p>' % (esc(payload.decode("utf-8")), READ_ONLY))
    elif pending:
        options = "".join('<label><input type="radio" name="choice" value="%s" required> %s (agency %s)</label><br>' % (
            esc(c["request_id"]), esc(c["request_id"]), esc(c["agency_id"])) for c in row["evidence"].get("candidates", []))
        out.append('<h2>Approve</h2><pre id="approval-payload">%s</pre>' % esc(payload.decode("utf-8")))
        out.append("<p>Choose the request, sign the text above with your approval key and paste the signature. "
                   "Approval ties this message to that request from the next run on. Nothing is sent or published.</p>")
        if options:
            out.append(ap._form("/approvals/approve", token, row["id"], "", options +
                                '<label for="signature">Detached SSH signature</label>'
                                '<textarea id="signature" name="signature" required>%s</textarea>' % esc(
                                    typed if field_name == "signature" else ""),
                                "Approve: tie to the chosen request", back="review"))
        out.append(ap._form("/approvals/reject", token, row["id"], "",
                            '<label for="reason">Why is it not a response?</label>'
                            '<input id="reason" name="reason" required maxlength="2000" value="%s">' % esc(
                                typed if field_name == "reason" else ""), "Not a response", back="review"))
    out.append('<p><a href="/">Back</a></p>')
    return console_layout.shell("Tie this message to a request", "\n".join(out), active="Approvals")


def render(project, row, history, preview, token, payload, send_config=None, *, run_id=None, refusal=None, source=None,
           export=None):
    from . import admin_panel as ap

    title = "Review: %s for %s / %s" % (row["kind"].replace("-", " "), row["agency_id"], row["request_id"])
    pending = row["status"] == "pending"
    switches = mail_send.switch_state(project, send_config)
    status = approval_status(project, row, switches, source) if row["status"] == "approved" else esc(
        "Pending" if pending else "Rejected: " + str(row.get("reason") or ""))
    message, field_name, typed = refusal or (None, None, None)
    out = ["<h1>%s</h1><p role=\"status\">%s</p>" % (esc(title), status)]
    if export is not None and row["status"] == "approved":
        out.append("<p>Approved by %s: signature verified on the live project (%s) at export %s.</p>" % (
            esc(row.get("approved_by")), esc(export.source_project_id), esc(export.exported_at)))
    if message:
        out.append('<p role="alert" class="diag">%s. Nothing was saved or decided.</p>' % esc(message.rstrip(".")))
    out.append("<dl>")
    for label, value in (("To", preview["to"] or "Unknown"), ("To note", preview["to_note"]),
                         ("From", preview["from"] or "Unknown"), ("From note", preview["from_note"]),
                         ("Subject", preview["subject"]),
                         ("Reply in the agency's thread", "yes" if preview["threaded"] else "no"),
                         ("Attachments", ", ".join(a["name"] for a in preview["attachments"]) or "None")):
        out.append("<dt>%s</dt><dd>%s</dd>" % (esc(label), esc(value)))
    out.append("</dl>")
    out.append(refusal_panel(project, row, run_id or row["source_run_id"]))
    warnings = [RESIDUE[code] for code in preview["residue"]]
    if preview["body_header_lines"]:
        warnings.append("Lines %s: the body has its own To/Subject/From lines; the real headers are shown above" %
                        ", ".join(map(str, preview["body_header_lines"])))
    if warnings:
        out.append("<h2>Warnings</h2><ul>" + "".join("<li>%s</li>" % esc(w) for w in warnings) + "</ul>")
    # The initial newline prevents HTML's textarea newline stripping from losing a body newline.
    field = '<label for="body">Draft text</label><textarea id="body" name="body" rows="28"%s>\n%s</textarea>' % (
        " required" if pending and export is None else " readonly", esc(typed if field_name == "body" else row["body"]))
    if pending and export is None:
        out.append(ap._form("/approvals/edit", token, row["id"], "", ap.revision_field(row) + field, "Save edit",
                            back="review"))
    else:
        out.append(field)
    edits = history["edits"]
    out.append("<h2>Edit history</h2><p>Original sha256 %s, current sha256 %s, %d edits</p>" % (
        esc(history["original"]["sha256"][:12]), esc(history["current"]["sha256"][:12]), len(edits)))
    if edits:
        last = edits[-1]
        out.append("<p>Last by %s at %s</p><ul>" % (esc(last["actor"]), esc(last["at"])))
        for edit in edits:
            out.append("<li>Revision %s, %s, %s, sha256 %s</li>" % tuple(
                esc(edit[k]) for k in ("revision", "at", "actor", "sha256")))
        out.append("</ul><pre>%s</pre>" % esc("\n".join(history["diff"])))
        if history["diff_truncated"]:
            out.append("<p>Diff truncated after 400 lines.</p>")
    else:
        out.append("<p>No edits</p>")
    evidence = row.get("evidence") or {}
    why = evidence.get("why") or " ".join(str(evidence.get(k) or "") for k in ("clock_kind", "clock_date")).strip()
    out.append("<h2>Why this draft</h2><p>%s</p>" % esc(why or "Draft awaiting decision"))
    limits = evidence.get("limits") or []
    for limit in limits if isinstance(limits, list) else [limits]:
        out.append("<p>%s</p>" % esc(limit))
    if pending and export is not None:
        out.append('<h2>Approve</h2><pre id="approval-payload">%s</pre><p>%s</p>' % (esc(payload.decode("utf-8")), READ_ONLY))
    elif pending:
        out.append('<h2>Approve</h2><pre id="approval-payload">%s</pre>' % esc(payload.decode("utf-8")))
        out.append("<p>Sign this text with your approval key "
                   "(<code>ssh-keygen -Y sign -n deflock-approval</code>) and paste the signature. "
                   "The console holds no key and never signs. Approval does not send or publish. "
                   "For mail, continue to the separate send check after approval.</p>")
        out.append(ap._form("/approvals/approve", token, row["id"], "",
                            '<label for="signature">Detached SSH signature</label>'
                            '<textarea id="signature" name="signature" required>%s</textarea>' % esc(
                                typed if field_name == "signature" else ""),
                            approval_label(row, switches), back="review"))
        out.append(ap._form("/approvals/reject", token, row["id"], "",
                            '<label for="reason">Rejection reason</label>'
                            '<input id="reason" name="reason" required maxlength="2000" value="%s">' % esc(
                                typed if field_name == "reason" else ""),
                            "Reject with reason", back="review"))
    out.append('<p><a href="/">Back</a></p>')
    return console_layout.shell(title, "\n".join(out), active="Approvals")


def serve(handler):
    from . import admin_panel as ap

    path, _, query = handler.path.partition("?")
    if path != "/review":
        return False
    args = urllib.parse.parse_qs(query, keep_blank_values=True)
    ids = args.get("id", [])
    if set(args) != {"id"} or len(ids) != 1 or not ap.DRAFT_ID.fullmatch(ids[0]):
        handler._fail(400)
        return True
    return _show(handler, ids[0], 200, None)


def refuse(handler, ident, message, typed):
    """A refused review form: the review page again (400) with the reason; typed = (field, text) is echoed escaped."""
    if typed is None:
        return _show(handler, ident, 400, (message, None, None))
    return _show(handler, ident, 400, (message,) + typed)


def _show(handler, ident, status, refusal):
    options = handler.options
    project, source, pin = options["project"], options["source"], handler.pin
    try:
        rows = {r["id"]: r for r in source.queue_rows()}
        if ident not in rows:
            handler._fail(404)
            return True
        row = rows[ident]
        ids = [ident]
        if row["kind"] == draft_templates.MESSAGE_LINK:
            payload = source.queue_payload(ids[0]) if row["status"] == "pending" else None
            handler._send(status, render_link(row, handler.csrf_token(), payload, refusal, read_only=pin is not None))
            return True
        history = source.queue_history(ids[0])
        preview = mail_preview.preview(project, row, handler.server.mail_drafts_config)
        payload = source.queue_payload(ids[0]) if row["status"] == "pending" else None
        page = render(project, row, history, preview, handler.csrf_token(), payload,
                      options.get("send_config"), run_id=options["run_id"], refusal=refusal, source=source, export=pin)
    except (approval_queue.ApprovalQueueError, mail_send.MailSendError, mail_drafts.MailDraftError,
            OSError, ValueError, KeyError, TypeError):
        handler._fail(503)
        return True
    handler._send(status, page)
    return True
