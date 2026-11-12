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


def append(journal, pin, actor, route, action, target_id, revision_seen, value, choice=None):
    entry = {"schema": SCHEMA, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "actor": actor,
             "route": route, "action": action, "target_id": target_id, "revision_seen": revision_seen, "value": value,
             "source_project_id": pin.source_project_id, "export_run_id": pin.run_id, "export_name": pin.name}
    if choice is not None:
        entry["choice"] = choice
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
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise JournalError("journal_not_regular")
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
    entries = []
    for line in raw[:raw.rfind(b"\n") + 1].split(b"\n")[:-1]:
        try:
            entry = json.loads(line)
            body = {k: v for k, v in entry.items() if k != "entry_id"}
            good = entry.get("schema") == SCHEMA and entry["entry_id"] == hashlib.sha256(_canon(body).encode()).hexdigest()
        except (ValueError, AttributeError, KeyError):
            good = False
        if not good:
            raise JournalError("journal_invalid")
        entries.append(entry)
    return entries


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
        if entry["source_project_id"] != pin.source_project_id or entry["entry_id"] in applied:
            continue
        outcome = outcomes.get(entry["entry_id"]) if isinstance(outcomes, dict) else None
        refusal = outcome.get("refusal") if isinstance(outcome, dict) else None
        text = ("not applied: %s" % refusal) if isinstance(refusal, str) else "pending apply: %s" % entry["action"]
        notes[entry["target_id"]] = text + (" (%s)" % LATER if entry["action"] in ("off", "kill") else "")
    return notes


class JournalSource(snap.SnapshotSource):
    """The snapshot readers with each row's pending journal decision attached as ``pending_apply``."""

    def __init__(self, pin, notes):
        super().__init__(pin)
        self.notes = notes

    def _mark(self, rows):
        return [{**r, "pending_apply": self.notes[r["id"]]} if r.get("id") in self.notes else r for r in rows]

    def queue_rows(self):
        return self._mark(super().queue_rows())

    def packets_pending(self, now=None):
        return self._mark(super().packets_pending(now))

    def public_updates(self):
        doc = super().public_updates()
        return doc if doc["state"] != "ok" else {**doc, "lines": self._mark(doc["lines"]),
                                                 "pending_apply": self.notes.get("switch")}


def _record(handler, action, target, revision, value, choice=None):
    try:
        append(handler.server.decisions_dir, handler.pin, handler.server.actor, handler.path, action, target, revision,
               value, choice)
    except (OSError, JournalError):
        handler._fail(503)
        return False
    return True


def queue(handler, action, ident, value, row, expected, choice):
    """Approve / reject / edit of a queue row, checked against the snapshot row as the writer checks it.
    True: journaled; False: the refusal page was sent."""
    if row is None:
        return handler._fail(404) or False
    if action != "edit" and row["status"] != "pending":
        return handler._fail(409) or False
    if (action == "reject" and len(value) > 2000) or (action == "edit" and len(value.encode()) > aq.MAX_BODY):
        return handler._fail(400) or False
    if action == "edit" and row["revision"] != int(expected):
        return handler._stale(ident) or False
    return _record(handler, action, ident, row["revision"], value, choice)


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
    if not approve and not (0 < len(value.strip()) <= 500 and all(ord(c) >= 32 and ord(c) != 127 for c in value)):
        return handler._fail(400) or False
    return _record(handler, "packet_" + ("approve" if approve else "reject"), ident, revision, value)


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
