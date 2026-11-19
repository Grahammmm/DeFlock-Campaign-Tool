"""Read-only history of journaled decisions and receipts from the request's pinned export."""
import os
import re
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import approval_queue as aq, console_export, console_journal, console_layout, console_snapshot
from .console_layout import esc

PATHS = ("/decisions",)
NAV = ("Decisions", 40)

PACIFIC = ZoneInfo("America/Los_Angeles")
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
WAITING = "Waiting for the next records run"
UNAVAILABLE = "Outcomes unavailable"
REFUSALS = {
    "DEFERRED": "Deferred. The records run did not apply this decision yet.",
    "STALE_REVISION": "Refused. The item changed after this decision was recorded.",
    "SIGNATURE_REFUSED": "Refused. The records run could not verify the approval signature.",
    "INVALID_TRANSITION": "Refused. The item's state did not allow this decision.",
}
GENERIC_REFUSAL = re.compile(r"REFUSED [a-z_]{1,64}")  # console_apply.outcome's generic refusal envelope
ACTIONS = {
    "approve": ("Approve draft", "Draft", "/review"),
    "reject": ("Reject draft", "Draft", "/review"),
    "edit": ("Edit draft", "Draft", "/review"),
    "packet_approve": ("Approve packet", "Packet", "/packets"),
    "packet_reject": ("Reject packet", "Packet", "/packets"),
    "finding_approve": ("Approve finding", "Finding review", "/findings/review"),
    "finding_reject": ("Reject finding", "Finding review", "/findings/review"),
    "off": ("Turn off public updates", "Public-update switch", "/public-updates"),
    "kill": ("Stop public updates immediately", "Public-update switch", "/public-updates"),
    "retract": ("Retract public update", "Public update", "/public-updates"),
    console_journal.LINE_APPROVAL_ACTION: ("Approve public update", "Public update", "/public-updates"),
    "message_link_relink": ("Relink message", "Message link", "/board"),
    "ocr_confirm": ("Confirm OCR page", "OCR page", "/ocr-review"),
    "ocr_reject": ("Reject OCR page", "OCR page", "/ocr-review"),
    console_journal.REDACTION_ACTION: ("Sign off on redaction", "Redaction", "/redactions"),
}


def _timestamp(value):
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        raise ValueError("invalid decision timestamp")
    return datetime.fromisoformat(value)


def _entry_id(value):
    return isinstance(value, str) and console_export.HEX.fullmatch(value) is not None


def _read_outcomes(pin):
    """None if the pinned manifest or its outcome collection is unavailable, not an empty collection."""
    try:
        directory = os.open(pin.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            raw = console_snapshot._read_file(directory, console_export.MANIFEST, console_snapshot.MAX_MANIFEST)
        finally:
            os.close(directory)
        manifest = aq._strict_load(raw or b"", "invalid decision outcomes")
        if not isinstance(manifest, dict) or manifest.get("schema") != console_export.SCHEMA:
            return None
        if (manifest.get("run_id"), manifest.get("source_project_id"), manifest.get("exported_at")) != (
                pin.run_id, pin.source_project_id, pin.exported_at):
            return None
        applied, outcomes = manifest.get("applied_journal_ids"), manifest.get("apply_outcomes")
        if (isinstance(applied, list) and all(_entry_id(ident) for ident in applied)
                and isinstance(outcomes, dict) and all(_entry_id(ident) for ident in outcomes)):
            ids = set(applied)
            if len(ids) == len(applied):
                return ids, outcomes
    except (OSError, aq.ApprovalQueueError, console_snapshot.SnapshotError):
        pass
    return None


def _status(at, cutoff, ident, evidence):
    # A newer entry is waiting even if an older export claims to know its outcome.
    if cutoff is not None and at > cutoff:
        return WAITING
    if evidence is None or cutoff is None:
        return UNAVAILABLE
    applied, outcomes = evidence
    if ident in applied:
        return "Applied" if outcomes.get(ident) == {"outcome": "APPLIED"} else UNAVAILABLE
    if ident not in outcomes:
        return WAITING
    receipt = outcomes[ident]
    if not isinstance(receipt, dict):
        return UNAVAILABLE
    code = receipt.get("outcome")
    if not isinstance(code, str) or receipt != {"outcome": code, "refusal": code}:
        return UNAVAILABLE
    if code in REFUSALS:
        return REFUSALS[code]
    if GENERIC_REFUSAL.fullmatch(code):
        return "Refused. The records run did not apply this decision."
    return UNAVAILABLE


def _item(entry):
    action = entry.get("action")
    label, kind, link = ACTIONS.get(action if isinstance(action, str) else "", ("Unknown action", "Unknown item", None))
    if link == "/review":
        target = entry.get("target_id")
        link = link + "?" + urlencode({"id": target}) if _entry_id(target) else None
    return label, '<a href="%s">%s</a>' % (esc(link), esc(kind)) if link else esc(kind)


def serve(handler):
    title = NAV[0]

    def send(status, content):
        handler._send(status, console_layout.shell(title, "<h1>Decisions</h1>" + content, active=title))
        return True

    directory, pin = handler.server.decisions_dir, handler.pin
    if directory is None:
        return send(200, "<p>Decisions: not configured on this console</p>")
    if pin is None:
        return send(503, "<p>Outcomes unavailable. No export is available for this request.</p>")
    try:
        records = []
        for entry in console_journal.read(directory):
            if entry["source_project_id"] == pin.source_project_id:
                at = _timestamp(entry.get("at"))
                records.append((at, entry["entry_id"], at.astimezone(PACIFIC), entry))
        records.sort(key=lambda record: record[:2], reverse=True)
    except (OSError, ValueError, KeyError, OverflowError, console_journal.JournalError, console_snapshot.SnapshotError):
        return send(503, "<p>Decision journal unavailable. The recorded decisions could not be read.</p>")
    try:
        cutoff = _timestamp(pin.exported_at)
    except ValueError:
        cutoff = None
    evidence = _read_outcomes(pin) if cutoff is not None else None
    body = ["<p>Outcomes unavailable. This export does not have readable decision results.</p>"] if evidence is None else []
    if not records:
        return send(200, "".join(body) + "<p>No decisions recorded for this project.</p>")
    body.append('<div class="scroll"><table><thead><tr><th scope="col">Recorded (Pacific)</th>'
                '<th scope="col">Action</th><th scope="col">Item</th><th scope="col">Outcome</th>'
                '</tr></thead><tbody>')
    for at, ident, local, entry in records:
        action, item = _item(entry)
        body.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(local.strftime("%Y-%m-%d %H:%M:%S %Z")), esc(action), item, esc(_status(at, cutoff, ident, evidence))))
    body.append("</tbody></table></div>")
    return send(200, "".join(body))
