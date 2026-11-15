"""Received messages that tie to no request, and the organizer-approved links that tie them (DFGL-04).

`write_run_unmatched` lists every received item whose request is unknown in
run-outputs/RUN/outcomes/unmatched-messages.json (metadata only: message hash, received date,
sender domain, attachment count, up to 3 candidate requests from the ledger register) and queues one
`message_link` decision per message. An approved choice is appended to project/message-links.jsonl,
which `received_items` reads on the next run. Nothing is sent, matched by a model or published.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from email.utils import parseaddr
from pathlib import Path

SCHEMA = "records-unmatched-v1"
LINK_SCHEMA = "records-message-link-v1"
LINKS_NAME = "message-links.jsonl"
MAX_CANDIDATES = 3
MAX_FILE = 8 * 1024 * 1024
HEX64 = re.compile(r"[a-f0-9]{64}")
ID = re.compile(r"[^\x00-\x1f\x7f]{1,200}")


def _need(condition, code):
    if not condition:
        raise ValueError(code)


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _regular(path):
    """lstat a private data file: None when absent; ValueError for a symlink or anything not a lone regular file."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_FILE, "message_links_file")
    return info


def _link_rows(raw):
    """-> ({message_sha256: current row}, invalid_line_count) from the raw file bytes. The latest valid line wins; a bad
    line is skipped. A row's ``line_sha256`` is the SHA-256 of its line exactly as stored (no terminating LF, nothing
    else removed or re-serialized); ``supersedes`` is the fingerprint it names, else None. Shared by read_links,
    current_link_rows and append_link so all three pick the same row."""
    rows, invalid = {}, 0
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for line in lines:
        try:
            row = json.loads(line)
            ok = (isinstance(row, dict) and row.get("schema") == LINK_SCHEMA and HEX64.fullmatch(str(row.get("message_sha256")))
                  and all(isinstance(row.get(k), str) and ID.fullmatch(row[k]) for k in ("agency_id", "request_id"))
                  and (row.get("supersedes") is None or HEX64.fullmatch(str(row.get("supersedes")))))
        except (ValueError, UnicodeError, RecursionError):
            ok = False
        if not ok:
            invalid += 1
            continue
        rows[row["message_sha256"]] = {"agency_id": row["agency_id"], "request_id": row["request_id"],
                                       "line_sha256": hashlib.sha256(line).hexdigest(), "supersedes": row.get("supersedes")}
    return rows, invalid


def current_link_rows(project):
    """-> ({message_sha256: {agency_id, request_id, line_sha256, supersedes}}, invalid_line_count). Read-only; a missing
    file is an empty state and creates nothing. Same row per message as read_links."""
    path = Path(os.path.abspath(project)) / LINKS_NAME
    if _regular(path) is None:
        return {}, 0
    return _link_rows(path.read_bytes())


def link_fingerprints(project):
    """{message_sha256: {agency_id, request_id, line_sha256}} - what the "This tie is wrong" control needs, nothing more."""
    rows = current_link_rows(project)[0]
    return {sha: {k: row[k] for k in ("agency_id", "request_id", "line_sha256")} for sha, row in sorted(rows.items())}


def read_links(project):
    """-> ({message_sha256: (agency_id, request_id)}, invalid_line_count). Latest line wins; bad lines are skipped."""
    rows, invalid = current_link_rows(project)
    return {sha: (row["agency_id"], row["request_id"]) for sha, row in rows.items()}, invalid


def check_relink(current, supersedes, target):
    """The state of a replacement of the line ``supersedes`` by ``target`` (agency_id, request_id) over the message's
    ``current`` row (None when untied): "replay" when the current line already is exactly this replacement, "ok" when
    it may be appended. message_link_stale when the current line is not the replaced one (changed, missing or another
    lineage); message_link_invalid for a malformed fingerprint or a replacement that names the same request."""
    _need(isinstance(supersedes, str) and HEX64.fullmatch(supersedes), "message_link_invalid")
    if current is not None and (current["agency_id"], current["request_id"]) == tuple(target) and current["supersedes"] == supersedes:
        return "replay"
    _need(current is not None and current["line_sha256"] == supersedes, "message_link_stale")
    _need((current["agency_id"], current["request_id"]) != tuple(target), "message_link_invalid")
    return "ok"


def require_current(project, message_sha256, supersedes, target):
    """Read-only precheck of a replacement before its approval is recorded (the writer rechecks atomically)."""
    return check_relink(current_link_rows(project)[0].get(message_sha256), supersedes, target)


def _read_fd(fd):
    chunks, size = [], 0
    while size <= MAX_FILE:
        chunk = os.pread(fd, MAX_FILE + 1 - size, size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    _need(size <= MAX_FILE, "message_links_file")
    return b"".join(chunks)


def append_link(project, message_sha256, agency_id, request_id, actor, *, supersedes=None):
    """Append one link line (0600). The same choice is a no-op; a different existing choice is refused unless the line
    is a replacement naming (``supersedes``) the SHA-256 of the exact current line it replaces. The check, the read and
    the append happen under one exclusive lock on the file; no earlier line is ever rewritten."""
    _need(HEX64.fullmatch(message_sha256 or "") and ID.fullmatch(agency_id or "") and ID.fullmatch(request_id or ""),
          "message_link_invalid")
    _need(supersedes is None or (isinstance(supersedes, str) and HEX64.fullmatch(supersedes)), "message_link_invalid")
    path = Path(os.path.abspath(project)) / LINKS_NAME
    try:  # a replacement never creates the file: with no current line there is nothing to replace
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | (0 if supersedes else os.O_CREAT), 0o600)
    except FileNotFoundError:
        raise ValueError("message_link_stale") from None
    try:
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "message_links_file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = _read_fd(fd)
        current = _link_rows(raw)[0].get(message_sha256)
        if supersedes is None:
            _need(current is None or (current["agency_id"], current["request_id"]) == (agency_id, request_id),
                  "message_link_conflict")
            if current is not None:
                return False
        elif check_relink(current, supersedes, (agency_id, request_id)) == "replay":
            return False
        line = {"schema": LINK_SCHEMA, "message_sha256": message_sha256, "request_id": request_id,
                "agency_id": agency_id, "actor": str(actor)[:80], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if supersedes is not None:
            line["supersedes"] = supersedes
        data = (_canon(line) + "\n").encode()
        _need(len(raw) + len(data) <= MAX_FILE, "message_links_file")
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def choose(row, choice):
    """-> (agency_id, request_id) when `choice` is one of the row's candidate request ids, else ValueError."""
    candidates = (row.get("evidence") or {}).get("candidates")
    _need(isinstance(candidates, list) and isinstance(choice, str), "message_link_choice")
    match = [c for c in candidates if isinstance(c, dict) and c.get("request_id") == choice]
    _need(len(match) == 1 and HEX64.fullmatch(str(row.get("item_id"))), "message_link_choice")
    return match[0]["agency_id"], match[0]["request_id"]


def _sender_domain(db, sha):
    rows = db.execute("SELECT headers FROM mail_messages WHERE eml_sha256=? ORDER BY id", (sha,)).fetchall()
    if not rows:
        rows = db.execute("SELECT m.headers FROM occurrences o JOIN occurrences p ON o.parent_occurrence_id=p.id "
                          "JOIN mail_messages m ON m.id=p.id WHERE o.original_sha256=? ORDER BY m.id", (sha,)).fetchall()
    for row in rows[:1]:
        try:
            fields = json.loads(row[0]).get("fields", [])
            values = [v for k, v in fields if str(k).lower() == "from" and isinstance(v, str)]
            address = parseaddr(values[0])[1] if len(values) == 1 else ""
        except (ValueError, TypeError, AttributeError):
            return None
        domain = address.rpartition("@")[2].strip().lower()
        return domain if re.fullmatch(r"[a-z0-9.-]{1,253}", domain) else None
    return None


def _candidates(db, agency_id, domain):
    base = ("SELECT r.id,r.agency_id FROM requests r %s ORDER BY (r.determination_at IS NOT NULL OR r.production_at IS NOT NULL), "
            "r.submitted_at DESC, r.id LIMIT %d")
    if agency_id:
        rows = db.execute(base % ("WHERE r.agency_id=?", MAX_CANDIDATES), (agency_id,)).fetchall()
    elif domain:
        rows = db.execute(base % ("JOIN agencies a ON a.id=r.agency_id WHERE a.portal_host IS NOT NULL AND a.portal_host!='' "
                                  "AND (a.portal_host=? OR ? LIKE '%.'||a.portal_host)", MAX_CANDIDATES), (domain, domain)).fetchall()
    else:
        rows = []
    return [{"request_id": r[0], "agency_id": r[1]} for r in rows]


def collect(db, received, *, run_id):
    """Pure read of the ledger + this run's received items -> records-unmatched-v1 report."""
    entries = []
    for item in received["items"]:
        if item.get("request_id") is not None:
            continue
        sha = item["original_sha256"]
        attachments = db.execute(
            "SELECT COUNT(*) FROM occurrences WHERE kind='attachment' AND parent_occurrence_id IN "
            "(SELECT id FROM occurrences WHERE original_sha256=?)", (sha,)).fetchone()[0]
        domain = _sender_domain(db, sha)
        entries.append({"message_sha256": sha, "received_date": (item.get("received_at") or "")[:10] or None,
                        "sender_domain": domain, "attachment_count": attachments,
                        "candidates": _candidates(db, item.get("agency_id"), domain),
                        "agency_id": item.get("agency_id")})
    entries.sort(key=lambda e: e["message_sha256"])
    return {"schema": SCHEMA, "run_id": run_id, "review_required": True, "messages": entries}


def _draft(entry):
    options = entry["candidates"]
    lines = ["Which request is this message a response to?",
             "Message %s received %s from %s with %d attachment(s). It could not be tied to a request."
             % (entry["message_sha256"][:12], entry["received_date"] or "on an unknown date",
                entry["sender_domain"] or "an unknown sender", entry["attachment_count"])]
    lines += ["%d. request %s (agency %s)" % (n, c["request_id"], c["agency_id"]) for n, c in enumerate(options, 1)]
    if not options:
        lines.append("No candidate request was found; you can only mark it as not a response.")
    return {"item_id": entry["message_sha256"], "agency_id": entry["agency_id"] or "unmatched", "request_id": "unmatched",
            "body": "\n".join(lines),
            "evidence": {"source": "unmatched", "why": "Message not tied to any request", "candidates": options,
                         "limits": ["Your choice becomes a link used from the next run on; nothing is sent."]}}


def _relink_draft(current, supersedes, entry):
    options = [c for c in entry["candidates"] if (c["agency_id"], c["request_id"]) != (current["agency_id"], current["request_id"])]
    lines = ["Is this message tied to the wrong request?",
             "Message %s is currently tied to request %s (agency %s). Approving a different request replaces that tie from the "
             "next run on; the earlier link line and the message's evidence are kept." % (
                 entry["message_sha256"][:12], current["request_id"], current["agency_id"])]
    lines += ["%d. request %s (agency %s)" % (n, c["request_id"], c["agency_id"]) for n, c in enumerate(options, 1)]
    if not options:
        lines.append("No other candidate request was found; you can only leave the tie as it is.")
    return {"item_id": entry["message_sha256"], "agency_id": current["agency_id"], "request_id": "unmatched",
            "body": "\n".join(lines),
            "evidence": {"source": "unmatched", "why": "Message tie marked wrong", "supersedes": supersedes,
                         "replaces": {"agency_id": current["agency_id"], "request_id": current["request_id"]},
                         "candidates": options,
                         "limits": ["Your choice replaces the current tie from the next run on; nothing is sent.",
                                    "Approving does not mean the earlier request never received this message."]}}


def queue_relink(project, run_id, message_sha256, supersedes):
    """Queue (or return) the one ``message_link`` replacement decision for a message whose tie was marked wrong.

    ``supersedes`` is the fingerprint of the message's current link line as the person saw it; a message whose current line
    is not that one is refused with message_link_stale. Candidates come from the run's ledger with the current request left
    out. -> (decision id, created). Nothing is appended to the links file."""
    from ..approval_queue import draft_id, load_queue, queue_message_relink
    from ..draft_templates import MESSAGE_LINK
    from ..run_stages import LedgerBusy
    from ..run_stages import snapshot_ro
    _need(isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is not None, "unmatched_run_id")
    _need(HEX64.fullmatch(message_sha256 or "") and HEX64.fullmatch(supersedes or ""), "message_link_invalid")
    ident = draft_id(message_sha256, MESSAGE_LINK, supersedes=supersedes)
    existing = next((r for r in load_queue(project) if r["id"] == ident), None)
    if existing is not None:  # a repeated "wrong" never reopens a decision
        return ident, False
    current = current_link_rows(project)[0].get(message_sha256)
    _need(current is not None and current["line_sha256"] == supersedes, "message_link_stale")
    try:
        with snapshot_ro(Path(os.path.abspath(project)) / "processing-v1" / "ledger.sqlite") as db:
            domain = _sender_domain(db, message_sha256)
            options = _candidates(db, current["agency_id"], domain)
    except LedgerBusy:
        raise ValueError("message_link_ledger_busy") from None
    except sqlite3.Error:
        raise ValueError("message_link_source_missing") from None
    entry = {"message_sha256": message_sha256, "candidates": options}
    return ident, queue_message_relink(project, run_id, _relink_draft(current, supersedes, entry))[1]


def write_run_unmatched(project, run_id):
    """Outputs-stage hook, called after write_run_received_items. Writes unmatched-messages.json (0600) and queues
    one `message_link` decision per untied message. Returns the report; raises on any failure (caller warns)."""
    from ..approval_queue import queue_message_links
    from ..run_stages import snapshot_ro
    _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is not None, "unmatched_run_id")
    project = Path(os.path.abspath(project))
    outcomes = project / "run-outputs" / run_id / "outcomes"
    for part in (project / "run-outputs", project / "run-outputs" / run_id, outcomes):
        _need(part.is_dir() and not part.is_symlink(), "unmatched_unsafe_path")
    source = outcomes / "received-items.json"
    _need(_regular(source) is not None, "unmatched_received_items")
    received = json.loads(source.read_bytes())
    _need(isinstance(received, dict) and received.get("run_id") == run_id and isinstance(received.get("items"), list),
          "unmatched_received_items")
    with snapshot_ro(project / "processing-v1" / "ledger.sqlite") as db:
        report = collect(db, received, run_id=run_id)
    queue_message_links(project, run_id, [_draft(e) for e in report["messages"]])
    for entry in report["messages"]:
        del entry["agency_id"]
    target, temporary = outcomes / "unmatched-messages.json", outcomes / ".unmatched-messages.json.tmp"
    _need(not target.is_symlink(), "unmatched_unsafe_path")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write((_canon(report) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    return report
