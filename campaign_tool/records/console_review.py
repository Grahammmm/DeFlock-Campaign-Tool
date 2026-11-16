"""One draft's editor; decisions reuse the existing signed approval POSTs."""
import urllib.parse

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import approval_queue, console_followups, console_journal, console_layout, console_snapshot, draft_templates, mail_drafts, mail_preview, mail_send
from .outcomes.reading import esc
from . import denial_log, denial_reply_check, followup_clock

READ_ONLY = "Read-only export: decisions are unavailable here."

RESIDUE = {"edit_placeholder": "[EDIT ME] placeholder left", "draft_heading": "Draft heading left",
           "draft_banner": "Draft banner left", "reviewer_notes": "Notes for the reviewer left",
           "before_sending": "BEFORE SENDING instructions left", "draft_footer": "Template footer left"}


def approval_label(row, switches):
    if row["kind"] in draft_templates.REVIEW_COPY_KINDS:
        return "Approve copy (draft-copy decision only; nothing is posted, sent or published)"
    if row["kind"] in draft_templates.NEXT_STEP_KINDS:
        return "Approve (records the decision only; nothing is posted or sent)"
    if switches["can_send"]:
        return "Approve and send"
    reason = switches["reason"]
    return "Approve - sending is off (%s). This marks it Approved, not sent." % mail_send.REASONS.get(reason, reason)


def approval_status(project, row, switches, source=None, delivery_block=None):
    if row["kind"] in draft_templates.REVIEW_COPY_KINDS:
        return esc("Approved copy; draft-copy decision only; nothing is posted, sent or published.")
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
    if delivery_block:
        return esc(delivery_block)
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


def export_note(pin):
    """Age of this exact snapshot, not the historical run's as-of date."""
    now = datetime.now(timezone.utc)
    try:
        stamp = datetime.fromisoformat(pin.exported_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("missing timezone")
        seconds = (now - stamp).total_seconds()
        if seconds < 0:
            age = "clock skew: export timestamp is in the future; refresh needed"
        else:
            minutes = int(seconds // 60)
            age = "%d days, %d hours, %d minutes old" % (minutes // 1440, minutes // 60 % 24, minutes % 60)
    except (ValueError, TypeError, AttributeError, OverflowError):
        age = "age unknown; refresh needed"
    return "<p>Snapshot export %s — %s. Status is as of export; not proof of current trust or delivery.</p>" % (
        esc(pin.exported_at), esc(age))


def recovery_note(marker):
    if marker == "unavailable":
        text = "Approval verification unavailable; this is not a final signature refusal."
    elif marker == "untrusted":
        text = ("Approval recorded but not verified under the live project's trust at this read/export; "
                "the signing key may no longer be trusted.")
    else:
        text = "Approval recorded but unverified; decisions are withheld."
    return "<p role=\"status\">%s Restoring the same trust may restore the old decision without an edit. " \
           "To deliberately revise: Edit, save, wait for a fresh export, review, then Approve with a current key. " \
           "A second Approve or Reject cannot replace the recorded approval.</p>" % esc(text)


def delivery_context(source, row):
    """All revisions of this identity; unavailable history must not mean unsent."""
    results = [r for r in source.send_results() if r["queue_id"] == row["id"]]
    placements = [r for r in source.mail_drafts() if r["queue_id"] == row["id"]]
    for entry in results + placements:
        if type(entry.get("revision")) is not int or not isinstance(entry.get("state"), str):
            raise ValueError("delivery_context_invalid")
    if any(r.get("state") == "sent" for r in results):
        return False, "Already delivered: revision cannot recall the old delivery. No resend is offered here."
    if any(r.get("state") not in ("sent", "failed", "refused") for r in results):
        return False, "Delivery unresolved; reconcile in the outbox before any retry or revision."
    if any(r["state"] not in ("placed", "found_existing", "superseded", "failed", "refused") for r in placements):
        return False, "Delivery context unavailable; reconcile before revision."
    if any(r.get("state") in ("placed", "found_existing") for r in placements):
        return True, "An older mailbox copy remains; revision does not rewrite or recall it."
    return True, "Available delivery history records no send for this draft."


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


def machine_reading(project, row, run_id, source, export):
    """The selected run's machine reading for this draft's request (FLK-REC-8); a failure is contained here as 'unknown'."""
    selected = export.run_id if export is not None else run_id  # a pinned export's own run, never the draft's older run
    try:
        reading = (source or console_snapshot.LiveSource(project)).followup_reading(selected) if selected else None
    except Exception:
        reading = None
    return console_followups.render(reading, row["agency_id"], row["request_id"], draft_run_id=row.get("source_run_id"))


def pending_note(row):
    return esc(" - %s" % row["pending_apply"]) if row.get("pending_apply") else ""


def candidate_url(ident, choice):
    return "/review?id=%s&choice=%s" % (urllib.parse.quote(ident), urllib.parse.quote(choice, safe=""))


def render_link(row, token, payload, refusal=None, *, read_only=False, choice=None, withheld=False):
    """A message_link row, two steps. Step 1 lists the candidates (links, no signable text). Step 2 (``choice`` selected,
    ``payload`` the exact text for that one candidate) shows the bound ``choice:`` line and the payload, and an approve form
    carrying that choice. A selection whose exported payload is missing (``withheld``) offers rejection only."""
    from . import admin_panel as ap

    pending = row["status"] == "pending"
    replaces = row["evidence"].get("replaces") if row["evidence"].get("supersedes") else None  # a "This tie is wrong" replacement
    title = "Replace the tie for this message" if isinstance(replaces, dict) else "Tie this message to a request"
    out = ["<h1>%s</h1>" % esc(title), "<p role=\"status\">%s</p>" % esc(
        "Pending" if pending else row["status"].capitalize() + (": " + str(row.get("reason") or "") if row.get("reason") else ""))
           + pending_note(row),
           "<pre>%s</pre>" % esc(row["body"])]
    if isinstance(replaces, dict):
        out.append("<p>Currently tied to request <strong>%s</strong> (agency %s). Approving another request replaces that tie from "
                   "the next run on; the earlier line and the evidence are kept, and nothing is sent.</p>" % (
                       esc(replaces.get("request_id")), esc(replaces.get("agency_id"))))
    message, field_name, typed = refusal or (None, None, None)
    if message:
        out.append('<p role="alert" class="diag">%s. Nothing was saved or decided.</p>' % esc(message.rstrip(".")))
    candidates = row["evidence"].get("candidates", []) if pending else []
    if pending:
        out.append("<h2>Approve</h2>")
        if payload is None:
            if withheld:
                out.append('<p role="alert" class="diag">%s</p>' % esc(
                    "This export has no signing text for that request. Refresh the export before approving a tie."))
            out.append("<p>Choose the request this message answers. The text to sign is shown for one request at a time.</p><ul>")
            out.extend('<li><a class="request-choice" href="%s">%s (agency %s)</a></li>' % (
                esc(candidate_url(row["id"], c["request_id"])), esc(c["request_id"]), esc(c["agency_id"])) for c in candidates)
            out.append("</ul>")
        else:
            out.append('<p>Selected request: <strong id="selected-choice">%s</strong> '
                       '(<a href="%s">choose another</a>)</p>' % (
                           esc(approval_queue.message_link_choice_line(row, choice)), esc("/review?id=" + urllib.parse.quote(row["id"]))))
            out.append('<pre id="approval-payload">%s</pre>' % esc(payload.decode("utf-8")))
            if read_only:
                out.append("<p>%s</p>" % READ_ONLY)
            else:
                out.append("<p>Sign the text above, which names this one request, with your approval key and paste the signature. "
                           "The signature ties this message to that request only, from the next run on. Nothing is sent or published.</p>")
                out.append(ap._form("/approvals/approve", token, row["id"], "",
                                    '<input type="hidden" name="choice" value="%s">' % esc(choice) +
                                    '<label for="signature">Detached SSH signature</label>'
                                    '<textarea id="signature" name="signature" required>%s</textarea>' % esc(
                                        typed if field_name == "signature" else ""),
                                    "Approve: tie to the chosen request", back="review"))
        if not read_only:
            out.append(ap._form("/approvals/reject", token, row["id"], "",
                                '<label for="reason">Why is it not a response?</label>'
                                '<input id="reason" name="reason" required maxlength="2000" value="%s">' % esc(
                                    typed if field_name == "reason" else ""), "Not a response", back="review"))
        elif payload is None:
            out.append("<p>%s</p>" % READ_ONLY)
    out.append('<p><a href="/">Back</a></p>')
    return console_layout.shell("Tie this message to a request", "\n".join(out), active="Approvals")


def render(project, row, history, preview, token, payload, send_config=None, *, run_id=None, refusal=None, source=None,
           export=None, journal=False, context=None, pending_rows=()):
    from . import admin_panel as ap

    title = "Review: %s for %s / %s" % (row["kind"].replace("-", " "), row["agency_id"], row["request_id"])
    pending = row["status"] == "pending"
    unverified = row.get("approval_unverified")
    decisions = pending and not unverified
    switches = mail_send.switch_state(project, send_config)
    source = source or console_snapshot.LiveSource(project)
    unsent, delivery = delivery_context(source, row) if row["status"] == "approved" or unverified else (True, "")
    status = approval_status(project, row, switches, source, None if unsent else delivery) if row["status"] == "approved" else esc(
        "Pending" if pending else "Rejected: " + str(row.get("reason") or ""))
    if not unsent and row["status"] != "approved":
        status = esc(delivery)
    if unverified:
        status = esc("Approval recorded but unverified (not an ordinary new pending draft)")
    message, field_name, typed = refusal or (None, None, None)
    live = export is None or journal  # a read-only export (no journal) renders no form
    out = ["<h1>%s</h1><p role=\"status\">%s%s</p>" % (esc(title), status, pending_note(row))]
    if export is not None:
        out.append(export_note(export))
    if unverified:
        out.append(recovery_note(unverified))
    if row["kind"] in draft_templates.REVIEW_COPY_KINDS + draft_templates.NEXT_STEP_KINDS:
        out.append("<p>Revision changes the copy decision only; it does not undo any prior mail or site use.</p>")
    if delivery:
        out.append("<p>%s</p>" % esc(delivery))
    revise = row["status"] == "approved" and unsent
    editable = live and unsent and (pending or revise)
    if row.get("edit_pending"):
        out.append('<p role="status">Waiting for edit to apply. Wait for a fresh live-derived export; '
                   'live approval has not been cleared by this hosted page.</p>')
    if revise or unverified:
        out.append("<p>Saving appends an edit, retains the original and approval event, clears approval for the new "
                   "revision, and requires a fresh review and signature. Save, wait for a fresh export, then approve again.</p>")
    if export is not None and row["status"] == "approved":
        proof = row.get("approval_evidence") or {}
        out.append("<p>Approved by %s: signature verified on the live project (%s) at export %s (signed revision %s, "
                   "signature sha256 %s).</p>" % (esc(row.get("approved_by")), esc(export.source_project_id),
                                                  esc(export.exported_at), esc(proof.get("signed_revision")),
                                                  esc(str(proof.get("signature_sha256"))[:12])))
    if message:
        out.append('<p role="alert" class="diag">%s. Nothing was saved or decided.</p>' % esc(message.rstrip(".")))
    out.append(mail_preview.context_html(context))
    out.append(navigation(row, pending_rows))
    out.append("<dl>")
    copy = row["kind"] in draft_templates.REVIEW_COPY_KINDS
    for label, value in [] if copy or (export is not None and context is None) else (("To", preview["to"] or "Unknown"), ("To note", preview["to_note"]),
                         ("From", preview["from"] or "Unknown"), ("From note", preview["from_note"]),
                         ("Subject", preview["subject"]),
                         ("Reply in the agency's thread", "yes" if preview["threaded"] else "no"),
                         ("Attachments", ", ".join(a["name"] for a in preview["attachments"]) or "None")):
        out.append("<dt>%s</dt><dd>%s</dd>" % (esc(label), esc(value)))
    if copy:
        evidence = row["evidence"]
        for key, value in evidence.get("target", {}).items():
            out.append("<dt>Target %s</dt><dd>%s</dd>" % (esc(key), esc(value)))
        for key, value in evidence.get("correction", {}).items():
            out.append("<dt>Correction %s</dt><dd>%s</dd>" % (esc(key), esc(value)))
        out.append("<dt>Source attachment sha256</dt><dd>%s</dd><dt>Source section</dt><dd>%s</dd>" % (
            esc(evidence.get("source_attachment_sha256")), esc(evidence.get("source_section"))))
        out.append("</dl><h2>Source caveats (review only)</h2><ul>")
        for citation in evidence.get("citations", []):
            out.append("<li>%s</li>" % esc("; ".join("%s: %s" % (k, v if v is not None else "not supplied")
                                                            for k, v in citation.items())))
        out.append("</ul><p>Copy approval cannot create a public packet or automatic next-site-update. "
                   "No mail placement or send is available.</p>")
    else:
        out.append("</dl>")
    out.append(refusal_panel(project, row, run_id or row["source_run_id"]))
    out.append(machine_reading(project, row, run_id, source, export))
    warnings = [RESIDUE[code] for code in preview["residue"]]
    if not copy and preview["body_header_lines"]:
        warnings.append("Lines %s: the body has its own To/Subject/From lines; the real headers are shown above" %
                        ", ".join(map(str, preview["body_header_lines"])))
    if warnings:
        out.append("<h2>Warnings</h2><ul>" + "".join("<li>%s</li>" % esc(w) for w in warnings) + "</ul>")
    # The initial newline prevents HTML's textarea newline stripping from losing a body newline.
    field = '<label for="body">Draft text</label><textarea id="body" name="body" rows="28"%s>\n%s</textarea>' % (
        " required" if editable else " readonly", esc(typed if field_name == "body" else row["body"]))
    if editable:
        # Keep the editor form open through the signing controls. Both buttons submit
        # this very textarea and revision; Save remains a separate action.
        out.append('<form method="post" action="/approvals/edit">'
                   '<input type="hidden" name="csrf_token" value="%s">'
                   '<input type="hidden" name="id" value="%s">'
                   '<input type="hidden" name="return" value="review">%s%s'
                   '<button type="submit">%s</button>' %
                   (esc(token), esc(row["id"]), ap.revision_field(row), field,
                    "Revise; clears approval" if revise else "Save edit"))
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
    if editable and not decisions:
        out.append("</form>")
    if decisions and not live:
        out.append('<h2>Approve</h2><pre id="approval-payload">%s</pre><p>%s</p>' % (esc(payload.decode("utf-8")), READ_ONLY))
    elif decisions and row.get("edit_pending"):
        out.append('<h2>Approve</h2><p role="status">Waiting for edit to apply. '
                   'Wait for the refreshed export, then review and approve.</p></form>')
    elif decisions:
        out.append('<h2>Approve</h2><pre id="approval-payload">%s</pre>' % esc(payload.decode("utf-8")))
        if journal:
            out.append("<p>%s</p>" % esc(console_journal.NOTE))
        out.append("<p>Sign this text with your approval key "
                   "(<code>ssh-keygen -Y sign -n deflock-approval</code>) and paste the signature. "
                   "The console holds no key and never signs. Approval does not send or publish. "
                   + ("This is a draft-copy decision only.</p>" if copy else
                      "For mail, continue to the separate send check after approval.</p>"))
        out.append('<label for="signature">Detached SSH signature</label>'
                   '<textarea id="signature" name="signature">%s</textarea>'
                   '<button type="submit" formaction="/approvals/approve">%s</button></form>' %
                   (esc(typed if field_name == "signature" else ""), esc(approval_label(row, switches))))
        out.append(ap._form("/approvals/reject", token, row["id"], "",
                            '<label for="reason">Rejection reason</label>'
                            '<input id="reason" name="reason" required maxlength="2000" value="%s">' % esc(
                                typed if field_name == "reason" else ""),
                            "Reject with reason", back="review"))
    if decisions and live and row.get("edit_pending"):
        out.append(ap._form("/approvals/reject", token, row["id"], "",
                            '<label>Rejection reason<input name="reason" required maxlength="2000"></label>',
                            "Reject with reason", back="review"))
    out.append('<p><a href="/">Back</a></p>')
    return console_layout.shell(title, "\n".join(out), active="Approvals")


def navigation(row, rows):
    ids = sorted({r["id"] for r in rows if r["status"] == "pending" and r["id"] != row["id"]})
    if not ids:
        return '<nav aria-label="Draft navigation"><a href="/">Queue index</a> · No other pending drafts.</nav>'
    ident = next((i for i in ids if i > row["id"]), ids[0])
    url = esc("/review?id=" + urllib.parse.quote(ident, safe=""))
    leave = '<a href="%s">Leave pending</a> · ' % url if row["status"] == "pending" else ""
    return '<nav aria-label="Draft navigation">%s<a href="%s">Next</a> · <a href="/">Queue index</a></nav>' % (leave, url)


def serve(handler):
    from . import admin_panel as ap

    path, _, query = handler.path.partition("?")
    if path != "/review":
        return False
    args = urllib.parse.parse_qs(query, keep_blank_values=True)
    ids, choices = args.get("id", []), args.get("choice", [])
    if not set(args) <= {"id", "choice"} or len(ids) != 1 or len(choices) > 1 or not ap.DRAFT_ID.fullmatch(ids[0]):
        handler._fail(400)
        return True
    return _show(handler, ids[0], 200, None, choices[0] if choices else None)


def refuse(handler, ident, message, typed, choice=None):
    """A refused review form: the review page again (400) with the reason; typed = (field, text) is echoed escaped."""
    if typed is None:
        return _show(handler, ident, 400, (message, None, None), choice)
    return _show(handler, ident, 400, (message,) + typed, choice)


def _show(handler, ident, status, refusal, choice=None):
    options = handler.options
    project, source, pin = options["project"], options["source"], handler.pin
    journal = handler.server.decisions_dir is not None
    try:
        rows = {r["id"]: r for r in source.queue_rows()}
        if ident not in rows:
            handler._fail(404)
            return True
        row = rows[ident]
        ids = [ident]
        if row["kind"] == draft_templates.MESSAGE_LINK:
            payload, withheld = None, False
            if choice is not None and row["status"] == "pending":
                try:
                    approval_queue.message_link_choice_line(row, choice)  # exactly one candidate, else refused
                except approval_queue.ApprovalQueueError:
                    if status == 200:  # a hand-made link to a missing, duplicate or unknown candidate
                        handler._fail(400)
                        return True
                    choice = None  # a refused form re-renders the choice list
                if choice is not None:
                    try:
                        payload = source.queue_payload(ids[0], choice)
                    except approval_queue.ApprovalQueueError as error:
                        if error.code not in ("link_payloads_missing", "message_link_choice"):
                            raise
                        withheld = True  # an older export: no signable text for this candidate
            handler._send(status, render_link(row, handler.csrf_token(), payload, refusal,
                                              read_only=pin is not None and not journal, choice=choice, withheld=withheld))
            return True
        if choice is not None and status == 200:  # only a tie is selected by a candidate
            handler._fail(400)
            return True
        history = source.queue_history(ids[0])
        context = source.review_context(ident)
        if source.snapshot:
            preview = context["preview"] if context is not None else {
                "to": None, "to_note": mail_preview.REFRESH, "from": None, "from_note": "Not exported",
                "subject": None, "threaded": False, "attachments": [], "residue": [], "body_header_lines": []}
        else:
            preview = (mail_preview.preview(project, row, handler.server.mail_drafts_config)
                       if handler.server.mail_drafts_config is not None else context["preview"])
        payload = source.queue_payload(ids[0]) if row["status"] == "pending" and not row.get("approval_unverified") else None
        page = render(project, row, history, preview, handler.csrf_token(), payload,
                      options.get("send_config"), run_id=options["run_id"], refusal=refusal, source=source, export=pin,
                      journal=journal, context=context, pending_rows=rows.values())
    except (approval_queue.ApprovalQueueError, mail_send.MailSendError, mail_drafts.MailDraftError,
            OSError, ValueError, KeyError, TypeError):
        handler._fail(503)
        return True
    handler._send(status, page)
    return True
