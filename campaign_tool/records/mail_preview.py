"""Read-only placement headers and draft warnings; no mailbox access or approval."""
import re

from . import draft_residue, mail_drafts


def preview(project, row, drafts_config_path=None):
    """Inspect a queue row using placement's thread and header rules.

    Attachments are evidence metadata only: build_message sends a plain-text letter.
    Missing inputs are display values, not permission to place or send a draft.
    """
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
        to_note = ("operator-checked " + envelope["recipient_basis"] if envelope else
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
