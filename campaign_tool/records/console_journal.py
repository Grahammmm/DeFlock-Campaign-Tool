"""Decision journal for the console served from a pinned export (DFGL-16B).

In journal mode (``records panel --export-root DIR --run-id auto --decisions-dir J``) an organizer decision is
validated against the pinned snapshot with the writer's own rules and appended to ``J/decisions.jsonl`` instead of
being written anywhere: not into the export, not into the live project. Nothing here verifies a signature, calls a
writer or touches ``approval_queue`` / ``public_packets``; applying entries to the live project is DFGL-16C.
"""
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime, timezone

from . import approval_queue as aq, console_snapshot as snap, public_packets as pp
from .console_ocr import OcrPagesError, complete_text

SCHEMA, FILE, LOCK, MAX_JOURNAL = "records-console-decision-v1", "decisions.jsonl", "decisions.lock", 64 * 1024 * 1024
LATER = ("takes effect at the next apply (records run); for an immediate stop use the attended command "
         "in docs/RECORDS-ADMIN-PANEL.md")
NOTE = "Journal mode: a decision is recorded here and applied to the live project at the next records run."


class JournalError(Exception):
    pass


def _now():
    return time.time()


def check_dir(journal, export_root):
    """ValueError unless J is absolute, a real 0700 owner-only directory and disjoint from the export root."""
    root = os.path.realpath(export_root)
    try:
        info = os.lstat(journal)
        ok = (os.path.isabs(journal) and os.path.realpath(journal) == journal and stat.S_ISDIR(info.st_mode)
              and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700
              and not (journal == root or journal.startswith(root + os.sep) or root.startswith(journal + os.sep)))
    except (OSError, TypeError, ValueError):
        ok = False
    if not ok:
        raise ValueError("decisions_dir_invalid")


def _canon(entry):
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


FINDING_KIND, FINDING_ACTIONS = "finding_review", ("finding_approve", "finding_reject")  # DF2007-06: applied by analysis-tick, not decisions-apply
_HEX64 = re.compile(r"[0-9a-f]{64}")


def finding_value(entry):
    """-> {analysis_root_sha256, signature|reason} of a well-formed finding_review entry, else ValueError (nothing is trusted)."""
    try:
        value = json.loads(entry["value"])
        want = "signature" if entry["action"] == "finding_approve" else "reason"
        agency, _, finding_id = entry["target_id"].partition(":")
        if (entry["action"] not in FINDING_ACTIONS or not isinstance(value, dict) or set(value) != {"analysis_root_sha256", want}
                or not isinstance(value[want], str) or not value[want].strip() or not _HEX64.fullmatch(str(value["analysis_root_sha256"]))
                or not _HEX64.fullmatch(str(entry["revision_seen"])) or not agency or not finding_id or entry["value"] != _canon(value)):
            raise ValueError("finding_entry_invalid")
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError("finding_entry_invalid") from None
    return value


REDACTION_ACTION = "redaction_signoff"  # DF2007-08: replayed by tools/flock_redact.py signoff-apply, not by decisions-apply
LINE_APPROVAL_ACTION = "public_update_line_approval"  # DF2007-10: applied by decisions-apply through public_updates.approve_line


def append(journal, pin, actor, route, action, target_id, revision_seen, value, choice=None, kind=None):
    entry = {"schema": SCHEMA, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "actor": actor,
             "route": route, "action": action, "target_id": target_id, "revision_seen": revision_seen, "value": value,
             "source_project_id": pin.source_project_id, "export_run_id": pin.run_id, "export_name": pin.name}
    if choice is not None:
        entry["choice"] = choice
    if kind is not None:  # only a newly supplied kind adds a field: every older entry and hash stays byte-compatible
        entry["kind"] = kind
    entry["entry_id"] = hashlib.sha256(_canon(entry).encode()).hexdigest()
    data = (_canon(entry) + "\n").encode()
    flags = os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open(journal, os.O_RDONLY | os.O_DIRECTORY | flags)
    try:
        lock = os.open(LOCK, os.O_RDWR | os.O_CREAT | flags, 0o600, dir_fd=directory)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fd = os.open(FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | flags, 0o600, dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_size + len(data) > MAX_JOURNAL:
                    raise JournalError("journal_unusable")
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(lock)
    finally:
        os.close(directory)
    return entry["entry_id"]


def read(journal):
    """Every entry, oldest first; an unfinished tail (a writer mid-append) is ignored, anything else bad raises."""
    directory = os.open(journal, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        raw = snap._read_file(directory, FILE, MAX_JOURNAL) or b""
    finally:
        os.close(directory)
    return parse(raw)


def parse(raw):
    """The entries of journal bytes (also used by DFGL-16C's apply, which reads them from a stream)."""
    entries = []
    for line in raw[:raw.rfind(b"\n") + 1].split(b"\n")[:-1]:
        try:
            entry = json.loads(line)
            body = {k: v for k, v in entry.items() if k != "entry_id"}
            good = entry.get("schema") == SCHEMA and entry["entry_id"] == hashlib.sha256(_canon(body).encode()).hexdigest()
            if good and "kind" in entry:  # the one known kind, with its own strict envelope; no permissive default
                good = entry["kind"] == FINDING_KIND and bool(finding_value(entry))
        except (ValueError, AttributeError, KeyError):
            good = False
        if not good:
            raise JournalError("journal_invalid")
        entries.append(entry)
    return entries


def pending_edit(server, ident, *, pin=None):
    """Inspect every same-project edit, not the lossy latest-action overlay.

    Only an applied receipt or a final refusal in a fresh live-derived export resolves
    an entry. Missing outcomes (including deferred applies) remain waiting.
    """
    if server.decisions_dir is None:
        return False
    pin = pin or server.pinner.pin()
    return ident in pending_edit_ids(server.decisions_dir, pin)


def pending_edit_ids(journal, pin):
    """One bounded history read for all rows in a pinned page."""
    directory = os.open(pin.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        manifest = aq._strict_load(snap._read_file(directory, "EXPORT.json", snap.MAX_MANIFEST) or b"", "journal_invalid")
    finally:
        os.close(directory)
    applied, outcomes = manifest.get("applied_journal_ids") or [], manifest.get("apply_outcomes") or {}
    pending = set()
    for entry in read(journal):
        if (entry["source_project_id"] != pin.source_project_id or
                entry["action"] != "edit" or entry.get("kind") or entry["entry_id"] in applied):
            continue
        outcome = outcomes.get(entry["entry_id"], {})
        if not outcome.get("refusal") or outcome.get("outcome") == "DEFERRED":
            pending.add(entry["target_id"])
    return pending


def overlay(journal, pin):
    """{target id (or "switch"): text} for this project's entries the pinned export has not applied; the latest wins."""
    directory = os.open(pin.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        manifest = aq._strict_load(snap._read_file(directory, "EXPORT.json", snap.MAX_MANIFEST) or b"", "journal_invalid")
    finally:
        os.close(directory)
    applied, outcomes = manifest.get("applied_journal_ids") or [], manifest.get("apply_outcomes") or {}
    notes = {}
    for entry in read(journal):
        if entry["source_project_id"] != pin.source_project_id or entry["entry_id"] in applied or entry.get("kind") or entry["action"] == REDACTION_ACTION:
            continue  # a finding_review entry belongs to the analysis root and a redaction_signoff to the Flock store (each page shows its own pending state)
        outcome = outcomes.get(entry["entry_id"]) if isinstance(outcomes, dict) else None
        refusal = outcome.get("refusal") if isinstance(outcome, dict) else None
        text = ("not applied: %s" % refusal) if isinstance(refusal, str) else "pending apply: %s" % entry["action"]
        if entry["action"] == LINE_APPROVAL_ACTION:
            text = ("not applied: %s" % refusal) if isinstance(refusal, str) else "recorded; pending apply (not approved until applied and verified)"
        notes[entry["target_id"]] = text + (" (%s)" % LATER if entry["action"] in ("off", "kill") else "")
    return notes


class JournalSource(snap.SnapshotSource):
    """The snapshot readers with each row's pending journal decision attached as ``pending_apply``."""

    def __init__(self, pin, notes):
        super().__init__(pin)
        self.notes = notes
        self.edit_pending = set()

    def _mark(self, rows):
        return [{**r, "pending_apply": self.notes[r["id"]]} if r.get("id") in self.notes else r for r in rows]

    def queue_rows(self):
        return [{**r, "edit_pending": r["id"] in self.edit_pending} for r in self._mark(super().queue_rows())]

    def packets_pending(self, now=None):
        return self._mark(super().packets_pending(now))

    def ocr_pages(self):
        doc = super().ocr_pages()
        return {**doc, "rows": self._mark(doc["rows"])}

    def public_updates(self):
        doc = super().public_updates()
        return doc if doc["state"] != "ok" else {**doc, "lines": self._mark(doc["lines"]),
                                                 "pending_apply": self.notes.get("switch")}


def _record(handler, action, target, revision, value, choice=None, kind=None):
    try:
        entry_id = append(handler.server.decisions_dir, handler.pin, handler.actor or handler.server.actor, handler.path, action,
                          target, revision, value, choice, kind)
    except (OSError, JournalError):
        handler._fail(503)
        return False
    if kind is not None:  # read the exact appended entry back before anything claims it was recorded
        try:
            if not any(e["entry_id"] == entry_id for e in read(handler.server.decisions_dir)):
                raise JournalError("journal_unusable")
        except (OSError, JournalError):
            handler._fail(503)
            return False
    return True


def finding(handler, action, target, digest, value):
    """One finding_review decision (already checked against the live analysis view by console_findings): journal only."""
    return _record(handler, action, target, digest, value, kind=FINDING_KIND)


def redaction_signoff(handler, target, digest, value):
    """One pasted sign-off signature for a Flock redaction proposal (already checked against the store by console_redactions): journal only."""
    return _record(handler, REDACTION_ACTION, target, digest, value)


def queue(handler, action, ident, value, row, expected, choice):
    """Approve / reject / edit of a queue row, checked against the snapshot row as the writer checks it.
    True: journaled; False: the refusal page was sent."""
    if row is None:
        return handler._fail(404) or False
    if action != "edit" and (row["status"] != "pending" or row.get("approval_unverified")):
        return handler._fail(409) or False
    if (action == "reject" and len(value) > 2000) or (action == "edit" and len(value.encode()) > aq.MAX_BODY):
        return handler._fail(400) or False
    if action == "edit" and row["revision"] != int(expected):
        return handler._stale(ident) or False
    if action == "approve":
        try:
            if pending_edit(handler.server, ident, pin=handler.pin):
                return handler._approve_refused("approve_edit_pending") or False
        except (OSError, ValueError, KeyError, TypeError, JournalError, aq.ApprovalQueueError):
            return handler._fail(503, "journal_unavailable") or False
    return _record(handler, action, ident, row["revision"], value, choice)


def relink(handler, message_sha256, supersedes):
    """"This tie is wrong": journal the queue-only ``message_link_relink`` action. The message must be tied in the pinned
    export (its metadata present) and the page's fingerprint must be the exported current line. Applying it only queues the
    replacement decision; it never appends a link. True: journaled; False: the refusal page was sent."""
    try:
        links = handler.options["source"].message_links()
    except (aq.ApprovalQueueError, OSError, ValueError, KeyError, TypeError):
        return handler._fail(503) or False  # an export without link metadata: refresh it
    current = links.get(message_sha256)
    if not isinstance(current, dict) or current.get("line_sha256") != supersedes:
        return handler._fail(409) or False
    return _record(handler, "message_link_relink", message_sha256, None, supersedes)


def packet(handler, field, ident, revision, value):
    """Packet approve / reject against the exported pending packets (revision, status and expiry as the writer)."""
    try:
        row = next((r for r in handler.options["source"].packets_pending() if r["id"] == ident), None)
        expired = row is not None and _now() > datetime.strptime(
            row["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (KeyError, TypeError, ValueError, OSError, pp.PacketError):
        return handler._fail(503) or False
    approve = field == "signature"
    if approve and not value:
        return handler._fail(403) or False
    if row is None or row["revision"] != revision or (approve and (row["status"] != "proposed" or expired)):
        return handler._fail(409) or False
    if not approve and not (0 < len(value.strip()) <= 500 and len(value) <= 500 and all(ord(c) >= 32 and ord(c) != 127 for c in value)):
        return handler._fail(400) or False
    return _record(handler, "packet_" + ("approve" if approve else "reject"), ident, revision, value)


def ocr(handler, action, ident, derivative, confidence):
    """Confirm / reject ONE exported OCR page: it must be waiting in the pinned export (and not already journaled), and the
    page the person saw (derivative hash and confidence) must still be this export's. The journal value pins both, so the
    apply step refuses a page that was re-read since. True: journaled; False: the refusal page was sent."""
    try:
        row = next((r for r in handler.options["source"].ocr_pages()["rows"] if r["id"] == ident), None)
    except (OcrPagesError, OSError, ValueError, KeyError, TypeError):
        return handler._fail(503) or False
    if row is None:
        return handler._fail(404) or False
    if row["state"] != "pending" or str(row.get("pending_apply") or "").startswith("pending apply"):
        return handler._fail(409) or False
    if derivative != (row["derivative_sha256"] or "") or confidence != json.dumps(row["confidence"]):
        return handler._fail(409) or False
    if action == "ocr_confirm" and not complete_text(row):  # the whole text was not presented: only a reject is taken
        return handler._fail(409) or False
    value = _canon({"confidence": row["confidence"], "derivative_sha256": row["derivative_sha256"]})
    return _record(handler, action, ident, None, value)


def line_approval(handler, ident, revision, signature):
    """One pasted signature for one status line (CON-LINE-APPROVE-1): the line must be open in the pinned export at exactly
    this revision and not already waiting here. Nothing is verified or posted. True: journaled; False: the refusal was sent."""
    try:
        doc = handler.options["source"].public_updates()
    except OSError:
        doc = {"state": "unavailable"}
    if doc["state"] != "ok":
        return handler._fail(503) or False
    row = next((r for r in doc["lines"] if r["id"] == ident), None)
    if (row is None or row["state"] not in ("proposed", "holding") or row.get("revision") != revision
            or ident not in (doc.get("line_payloads") or {}) or "pending apply" in str(row.get("pending_apply") or "")):
        return handler._fail(409) or False
    return _record(handler, LINE_APPROVAL_ACTION, ident, revision, signature)


def oversight(handler, action, ident):
    """Undo / veto (the line must exist and be open in the snapshot), OFF and kill."""
    if action == "retract":
        if not (isinstance(ident, str) and re.fullmatch(r"[0-9a-f]{16}", ident)):
            return handler._fail(400) or False
        try:
            doc = handler.options["source"].public_updates()
        except OSError:
            doc = {"state": "unavailable"}
        if doc["state"] != "ok":
            return handler._fail(503) or False
        row = next((r for r in doc["lines"] if r["id"] == ident), None)
        if row is None or row["state"] not in ("proposed", "holding", "posted"):
            return handler._fail(409) or False
    return _record(handler, action, ident if action == "retract" else "switch", None, "")
