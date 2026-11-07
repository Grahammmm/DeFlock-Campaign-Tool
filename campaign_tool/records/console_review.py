"""One draft's editor; decisions reuse the existing signed approval POSTs."""
import urllib.parse

from . import approval_queue, console_layout, mail_preview
from .outcomes.reading import esc

RESIDUE = {"edit_placeholder": "[EDIT ME] placeholder left", "draft_heading": "Draft heading left",
           "draft_banner": "Draft banner left", "reviewer_notes": "Notes for the reviewer left",
           "before_sending": "BEFORE SENDING instructions left", "draft_footer": "Template footer left"}


def render(project, row, history, preview, token, payload):
    from . import admin_panel as ap

    title = "Review: %s for %s / %s" % (row["kind"].replace("-", " "), row["agency_id"], row["request_id"])
    pending = row["status"] == "pending"
    status = {"pending": "Pending", "approved": "Approved (not sent)",
              "rejected": "Rejected: " + str(row.get("reason") or "")}[row["status"]]
    out = ["<h1>%s</h1><p role=\"status\">%s</p>" % (esc(title), esc(status)), "<dl>"]
    for label, value in (("To", preview["to"] or "Unknown"), ("To note", preview["to_note"]),
                         ("From", preview["from"] or "Unknown"), ("From note", preview["from_note"]),
                         ("Subject", preview["subject"]),
                         ("Reply in the agency's thread", "yes" if preview["threaded"] else "no"),
                         ("Attachments", ", ".join(a["name"] for a in preview["attachments"]) or "None")):
        out.append("<dt>%s</dt><dd>%s</dd>" % (esc(label), esc(value)))
    out.append("</dl>")
    warnings = [RESIDUE[code] for code in preview["residue"]]
    if preview["body_header_lines"]:
        warnings.append("Lines %s: the body has its own To/Subject/From lines; the real headers are shown above" %
                        ", ".join(map(str, preview["body_header_lines"])))
    if warnings:
        out.append("<h2>Warnings</h2><ul>" + "".join("<li>%s</li>" % esc(w) for w in warnings) + "</ul>")
    # The initial newline prevents HTML's textarea newline stripping from losing a body newline.
    field = '<label for="body">Draft text</label><textarea id="body" name="body" rows="28"%s>\n%s</textarea>' % (
        " required" if pending else " readonly", esc(row["body"]))
    if pending:
        out.append(ap._form("/approvals/edit", token, row["id"], "", field, "Save edit", back="review"))
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
    if pending:
        out.append('<h2>Approve</h2><pre id="approval-payload">%s</pre>' % esc(payload.decode("utf-8")))
        out.append("<p>Sign this text with your approval key "
                   "(<code>ssh-keygen -Y sign -n deflock-approval</code>) and paste the signature. "
                   "The console holds no key and never signs. Approval does not send or publish.</p>")
        out.append(ap._form("/approvals/approve", token, row["id"], "",
                            '<label for="signature">Detached SSH signature</label>'
                            '<textarea id="signature" name="signature" required></textarea>', "Approve", back="review"))
        out.append(ap._form("/approvals/reject", token, row["id"], "",
                            '<label for="reason">Rejection reason</label>'
                            '<input id="reason" name="reason" required maxlength="2000">',
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
    project = handler.server.options["project"]
    try:
        rows = {r["id"]: r for r in approval_queue.load_queue(project)}
        if ids[0] not in rows:
            handler._fail(404)
            return True
        row = rows[ids[0]]
        history = approval_queue.history(project, ids[0])
        preview = mail_preview.preview(project, row, handler.server.mail_drafts_config)
        payload = approval_queue.approval_payload(project, ids[0]) if row["status"] == "pending" else None
        page = render(project, row, history, preview, handler.server.csrf_token, payload)
    except (approval_queue.ApprovalQueueError, OSError, ValueError, KeyError, TypeError):
        handler._fail(503)
        return True
    handler._send(200, page)
    return True
