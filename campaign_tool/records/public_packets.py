"""Update packets for documents, summaries and findings (REC-PUB-2). Nothing here publishes, sends or deploys anything.

A packet is a proposed public item (kinds ``document``, ``summary``, ``finding``, ``site_update``) kept in the append-only
private journal ``<project>/public-packets.jsonl``. G approves each packet with an SSH signature over its exact text
(``campaign_tool.approval``, the REC-AUTO-4 verifier, bound to the revision). Only then, and only while the switch file
``public-packets.json`` says ``enabled`` (it ships ``false``) and ``public-packets.KILL`` is absent, may ``write_change`` write
``<project>/site-changes/<id>-r<rev>.json``: an input for a reviewed campaign-site PR. The production deploy stays the
existing manual workflow. Agencies, campaign and jurisdiction are data. ``propose-document`` (DOC-PUBLISH-1) is the only
pipeline path that proposes a ``document`` packet: a received original plus its verified, applied redaction record.

Refused at proposal and again at write: privacy-pattern matches, legal-violation wording, unverified citations, documents
without a redaction record, findings that are not approved for public, a packet older than ``EXPIRY_SECONDS``, or a revision
other than the one that was signed. Refusals carry a stable code and never the text.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone

from .. import approval, legal_claims, privacy_patterns
from . import campaign_config

SCHEMA = "records-public-packets-v1"
CHANGE_SCHEMA = "records-site-change-v1"
SCOPE = "public-packet-v1"
SWITCH_SCOPE = "public-packets-switch-v1"
JOURNAL, SWITCH, KILL, LOCK, OUT = ("public-packets.jsonl", "public-packets.json", "public-packets.KILL",
                                    "public-packets.lock", "site-changes")
KINDS = ("document", "summary", "finding", "site_update")
EXPIRY_SECONDS = 14 * 86400
MAX_JOURNAL = 16 * 1024 * 1024
MAX_TEXT, MAX_TITLE = 4000, 160
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
SHA = re.compile(r"[0-9a-f]{64}")
PACKET_ID = re.compile(r"[0-9a-f]{16}")
ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@-]{0,63}")
REDACTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")
PUBLIC_STATE = "approved for public"


class PacketError(Exception):
    """Content-free refusal; ``code`` is stable; ``detail`` (optional) is a stable reason or category list, never text."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


def _need(condition, code):
    if not condition:
        raise PacketError(code)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def project_id(project):
    return hashlib.sha256(os.path.realpath(project).encode()).hexdigest()[:16]


def _read(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise PacketError("file_invalid") from None
    with os.fdopen(fd, "rb") as stream:
        _need(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "file_invalid")
        data = stream.read(limit + 1)
    _need(len(data) <= limit, "file_too_large")
    return data


def _json(raw, code):
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise PacketError(code) from None


def _replace(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


class _Locked:
    def __init__(self, project, create=False, read_only=False):
        self.read_only = read_only
        if create:
            os.makedirs(project, mode=0o700, exist_ok=True)
        _need(os.path.isdir(project) and not os.path.islink(project), "project_invalid")
        self.project = os.path.abspath(project)

    def __enter__(self):
        self.fd = os.open(os.path.join(self.project, LOCK),
                          (os.O_RDONLY if self.read_only else os.O_RDWR | os.O_CREAT) | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    os.close(self.fd)
                    raise PacketError("journal_busy") from None
                time.sleep(0.05)

    def __exit__(self, *exc):
        os.close(self.fd)

    def path(self, name):
        return os.path.join(self.project, name)

    def events(self):
        raw = _read(self.path(JOURNAL), MAX_JOURNAL) or b""
        _need(raw == b"" or raw.endswith(b"\n"), "journal_invalid")
        out = [_json(line, "journal_invalid") for line in raw.split(b"\n")[:-1]]
        _need(all(isinstance(e, dict) and e.get("schema") == SCHEMA for e in out), "journal_invalid")
        return out

    def append(self, kind, now, **fields):
        line = (_canon({"schema": SCHEMA, "seq": len(self.events()), "at": _iso(now), "ts": int(now), "event": kind,
                        **fields}) + "\n").encode()
        fd = os.open(self.path(JOURNAL), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)


def _actor(actor):
    _need(isinstance(actor, str) and ACTOR.fullmatch(actor) is not None, "actor_invalid")
    return actor


# ---- packet content ----------------------------------------------------------------------------

def _lint(*texts):
    for text in texts:
        _need(not privacy_patterns.categories(text), "privacy_lint")
        _need(not legal_claims.legal_claims(text), "legal_claim_wording")


def clean(packet):
    """Validate a proposed packet and return its canonical form. Raises PacketError (code only)."""
    _need(isinstance(packet, dict), "packet_invalid")
    kind = packet.get("kind")
    _need(kind in KINDS, "kind_invalid")
    agency, request = packet.get("agency_key"), packet.get("request_id") or ""
    _need(isinstance(agency, str) and KEY.fullmatch(agency) is not None, "agency_invalid")
    _need(isinstance(request, str) and (request == "" or KEY.fullmatch(request) is not None), "request_invalid")
    title, text = packet.get("title"), packet.get("text")
    _need(isinstance(title, str) and 0 < len(title) <= MAX_TITLE and "\n" not in title, "title_invalid")
    _need(isinstance(text, str) and 0 < len(text) <= MAX_TEXT, "text_invalid")
    source = packet.get("source")
    _need(isinstance(source, str) and KEY.fullmatch(source) is not None, "source_invalid")
    docs = []
    for doc in packet.get("documents") or []:
        _need(isinstance(doc, dict) and isinstance(doc.get("sha256"), str) and SHA.fullmatch(doc["sha256"]), "document_invalid")
        redactions = doc.get("redaction_ids") or []
        _need(isinstance(redactions, list) and all(isinstance(r, str) and REDACTION_ID.fullmatch(r) for r in redactions),
              "document_invalid")
        received = doc.get("date_received")
        _need(isinstance(received, str) and DAY.fullmatch(received) is not None, "document_invalid")
        docs.append({"sha256": doc["sha256"], "date_received": received, "source_request": doc.get("source_request") or request,
                     "redaction_ids": sorted(set(redactions)), "use_restriction": doc.get("use_restriction") is True})
        src = docs[-1]["source_request"]
        _need(isinstance(src, str) and (src == "" or KEY.fullmatch(src) is not None), "document_invalid")
        _need(kind != "document" or docs[-1]["redaction_ids"], "redaction_missing")
    cites = []
    for cite in packet.get("citations") or []:
        _need(isinstance(cite, dict) and isinstance(cite.get("ref"), str) and 0 < len(cite["ref"]) <= 200, "citation_invalid")
        _need(cite.get("verified") is True, "unverified_citation")
        cites.append({"ref": cite["ref"], "verified": True})
    if kind in ("document", "summary", "finding"):
        _need(docs, "document_required")
    if kind == "finding":
        _need(packet.get("finding_state") == PUBLIC_STATE, "finding_not_public")
        _need(cites, "citation_required")
    _lint(title, text, *(c["ref"] for c in cites))
    out = {"kind": kind, "agency_key": agency, "request_id": request, "source": source, "title": title, "text": text,
           "documents": sorted(docs, key=lambda d: d["sha256"]), "citations": cites}
    if kind == "finding":
        out["finding_state"] = PUBLIC_STATE
    return out


def packet_id(packet):
    return hashlib.sha256(_canon([packet["kind"], packet["agency_key"], packet["request_id"], packet["source"]]).encode()
                          ).hexdigest()[:16]


def fold(events):
    """-> {id: state row}. A new revision supersedes the old one: its approval no longer counts."""
    rows = {}
    for e in events:
        kind, ident = e["event"], e.get("id")
        if kind == "packet":
            rows[ident] = {"id": ident, "revision": e["revision"], "packet": e["packet"], "status": "proposed",
                           "proposed_ts": e["ts"], "digest": e["digest"]}
        elif ident in rows and e.get("revision") == rows[ident]["revision"]:
            row = rows[ident]
            if kind == "approved" and row["status"] == "proposed":
                row.update(status="approved", signed_by=e["principal"], signature=e["signature"], approved_ts=e["ts"])
            elif kind in ("rejected", "cancelled") and row["status"] in ("proposed", "approved"):
                row["status"] = {"rejected": "rejected", "cancelled": "cancelled"}[kind]
            elif kind == "written" and row["status"] == "approved":
                row.update(status="written", file=e["file"], file_sha256=e["file_sha256"])
    return rows


def _expired(row, now):
    return now - row["proposed_ts"] > EXPIRY_SECONDS


def _view(row, now):
    status = "expired" if row["status"] in ("proposed", "approved") and _expired(row, now) else row["status"]
    return {k: row[k] for k in ("id", "revision") if k in row} | {
        "status": status, "kind": row["packet"]["kind"], "agency_key": row["packet"]["agency_key"],
        "title": row["packet"]["title"]} | ({"signed_by": row["signed_by"]} if "signed_by" in row else {}) | (
        {"file": row["file"]} if "file" in row else {})


def review_text(project, row):
    p = row["packet"]
    return "\n".join([f"project: {project_id(project)}", f"id: {row['id']}", f"kind: {p['kind']}", f"agency: {p['agency_key']}",
                      f"request: {p['request_id']}", f"title: {p['title']}", "text:", p["text"],
                      "documents:", _canon(p["documents"]), "citations:", _canon(p["citations"])])


def _payload(project, row):
    return approval.payload(SCOPE, row["id"], review_text(project, row), round_=row["revision"])


# ---- switch ------------------------------------------------------------------------------------

def _switch_state(lock, events):
    if os.path.lexists(lock.path(KILL)):
        return "KILLED"
    raw = _read(lock.path(SWITCH), 4096)
    if raw is None or _json(raw, "switch_invalid").get("enabled") is not True:
        return "OFF"
    last = next((e for e in reversed(events) if e["event"] == "switch"), None)
    if last is None or last.get("state") != "on":
        return "OFF"
    try:
        who = approval.verify(approval.payload(SWITCH_SCOPE, project_id(lock.project), _switch_content(lock.project, last["round"]),
                                               round_=last["round"]), last["signature"])
    except (approval.ApprovalError, KeyError, TypeError):
        return "OFF"
    return "ON" if who == last.get("principal") else "OFF"


def _switch_content(project, round_):
    return (f"project: {project_id(project)}\naction: allow approved update packets to be written as site-content "
            f"change files (no deploy)\nround: {round_}")


def _rounds(events):
    return sum(1 for e in events if e["event"] == "switch")


def switch_payload(project):
    with _Locked(project) as lock:
        rnd = _rounds(lock.events())
    return approval.payload(SWITCH_SCOPE, project_id(project), _switch_content(project, rnd), round_=rnd)


def switch_state(project, *, read_only=False):
    """Read switch state; read_only never creates a synchronization file."""
    try:
        with _Locked(project, read_only=read_only) as lock:
            return _switch_state(lock, lock.events())
    except FileNotFoundError:
        if not read_only:
            raise
        # No lock and no switch evidence is the untouched, shipped-OFF state.
        if any(os.path.lexists(os.path.join(project, name)) for name in (JOURNAL, SWITCH, KILL)):
            raise PacketError("switch_unavailable") from None
        return "OFF"


def init(project):
    """Ship OFF: writes ``{"enabled": false}`` (never overwrites an existing switch file)."""
    with _Locked(project, create=True) as lock:
        if not os.path.lexists(lock.path(SWITCH)):
            _replace(lock.path(SWITCH), b'{"enabled": false}\n')
    return "OFF"


def enable(project, signature, now=None):
    now = time.time() if now is None else now
    _need(isinstance(signature, str) and signature, "approval_required")
    with _Locked(project) as lock:
        _need(not os.path.lexists(lock.path(KILL)), "killed")
        rnd = _rounds(lock.events())
        try:
            who = approval.verify(approval.payload(SWITCH_SCOPE, project_id(project), _switch_content(project, rnd), round_=rnd),
                                  signature)
        except approval.ApprovalError:
            raise PacketError("approval_required") from None
        lock.append("switch", now, state="on", principal=who, round=rnd, signature=signature)
        _replace(lock.path(SWITCH), b'{"enabled": true}\n')
    return "ON"


def turn_off(project, actor, now=None):
    now = time.time() if now is None else now
    with _Locked(project, create=True) as lock:
        lock.append("switch", now, state="off", principal=_actor(actor), round=_rounds(lock.events()))
        _replace(lock.path(SWITCH), b'{"enabled": false}\n')
    return "OFF"


# ---- packet life cycle -------------------------------------------------------------------------

def propose(project, packet, now=None, *, only_new=False):
    """Add a packet. Identical content is a duplicate (no event); changed content is a new revision that voids any approval.
    only_new (propose-document): an existing packet under this id that is not rejected or cancelled is left alone (exists)."""
    now = time.time() if now is None else now
    packet = clean(packet)
    ident = packet_id(packet)
    digest = hashlib.sha256(_canon(packet).encode()).hexdigest()
    with _Locked(project, create=True) as lock:
        row = fold(lock.events()).get(ident)
        if row is not None and only_new and row["status"] not in ("rejected", "cancelled"):
            return {"id": ident, "revision": row["revision"], "result": "exists"}
        reopen = row is not None and (row["status"] == "cancelled" or (only_new and row["status"] == "rejected"))
        if row is not None and row["digest"] == digest and not reopen:
            return {"id": ident, "revision": row["revision"], "result": "duplicate"}
        revision = 0 if row is None else row["revision"] + 1
        lock.append("packet", now, id=ident, revision=revision, packet=packet, digest=digest)
    return {"id": ident, "revision": revision, "result": "proposed"}


def from_queue(project, row, source_run_id=None, now=None):
    """Turn an APPROVED Next Steps site-update row from the approval queue into a ``site_update`` packet."""
    _need(isinstance(row, dict) and row.get("kind") == "next-site-update" and row.get("status") == "approved", "queue_row_invalid")
    return propose(project, {"kind": "site_update", "agency_key": row.get("agency_id"), "request_id": "" if row.get(
        "request_id") == "agency-wide" else row.get("request_id"), "source": f"queue-{row.get('id')}",
        "title": f"Site update for {row.get('agency_id')}", "text": row.get("body")}, now=now)


# ---- a received record as a document packet (DOC-PUBLISH-1) -----------------------------------

RUN = re.compile(r"run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}")  # run ids sort by time


def _file_sha256(path):
    """SHA-256 of a regular file's current bytes, or None (missing, unreadable, link or special file)."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
            return digest.hexdigest()
    except (OSError, TypeError, ValueError):
        return None


def _check_original(project, sha256):
    """Admitted, preserved (current bytes still hash to sha256) and extracted, from a read-only live-ledger snapshot."""
    import sqlite3
    from .ledger import store
    from .ledger_catalog import scope_excluded
    from .outcomes.received_items import ADMITTED_ROLES
    from .run_stages import LedgerBusy, snapshot_ro
    try:
        with snapshot_ro(os.path.join(project, "processing-v1", "ledger.sqlite")) as db:
            db.row_factory = sqlite3.Row
            try:
                original = store.effective_original(db, sha256)
            except ValueError:
                original = None
            stages = {r["stage"]: r["status"] for r in db.execute(
                "SELECT stage,status FROM stage_state WHERE original_sha256=?", (sha256,))}
    except (sqlite3.Error, LedgerBusy, OSError):
        raise PacketError("LEDGER_UNAVAILABLE") from None
    _need(original is not None and not scope_excluded(original) and original["role"] in ADMITTED_ROLES, "ORIGINAL_UNKNOWN")
    _need(stages.get("preserve") == "done" and _file_sha256(original["storage_path"]) == sha256, "NOT_PRESERVED")
    _need(stages.get("extract") == "done", "NOT_EXTRACTED")


def _received_item(project, sha256):
    """The newest run's received-items entry for sha256, or None (an older run is never consulted)."""
    base = os.path.join(project, "run-outputs")
    runs = sorted((n for n in os.listdir(base) if RUN.fullmatch(n)), reverse=True) if os.path.isdir(base) else ()
    if not runs:
        return None
    raw = _read(os.path.join(base, runs[0], "outcomes", "received-items.json"), 64 << 20)  # the newest run only
    if raw is None:
        return None  # its inventory is missing: fail closed (no older run's metadata is used)
    body = _json(raw, "DATE_UNAVAILABLE")
    items = body.get("items") if isinstance(body, dict) else None
    return next((i for i in items if isinstance(i, dict) and i.get("original_sha256") == sha256),
                None) if isinstance(items, list) else None


def propose_document(project, sha256, record_id, *, store=None, title=None, use_restriction=False, now=None):
    """A received record -> one pending ``document`` packet, only with a verified, applied redaction record. Proposes only:
    the record bytes are never copied; the packet names the original and the record id (which pins the derivative)."""
    from . import redaction_record
    _need(isinstance(sha256, str) and SHA.fullmatch(sha256) is not None, "ORIGINAL_UNKNOWN")
    _check_original(project, sha256)
    try:
        item = _received_item(project, sha256)
    except (PacketError, OSError):
        item = None
    _need(item is None or item.get("extraction_status") == "current", "NOT_EXTRACTED")
    day = item["received_at"][:10] if item is not None and isinstance(item.get("received_at"), str) else ""
    try:
        _need(DAY.fullmatch(day) is not None, "DATE_UNAVAILABLE")
        datetime.strptime(day, "%Y-%m-%d")  # a real calendar day
    except ValueError:
        raise PacketError("DATE_UNAVAILABLE") from None
    agency, request = item.get("agency_id"), item.get("request_id") or ""
    _need(isinstance(agency, str) and KEY.fullmatch(agency) is not None, "AGENCY_UNATTRIBUTED")
    try:
        _, data = redaction_record.verified_derivative(os.path.abspath(store or project), agency, sha256, record_id)
        found = privacy_patterns.categories(data.decode("utf-8"))
    except redaction_record.RedactionError as error:
        raise PacketError(error.code, error.reason) from None
    except UnicodeError:
        raise PacketError("REDACTION_UNVERIFIED", "derivative_not_text") from None
    if found:  # the published text still matches a pattern: the record did not cover it
        raise PacketError("PRIVACY_LINT", "derivative: " + ", ".join(found))
    packet = {"kind": "document", "agency_key": agency, "request_id": request, "source": "received-" + sha256,
              "title": title or f"Record received from {agency} on {day}",
              "text": (f"A record received from {agency} on {day}, published as received with personal details removed "
                       f"under redaction record {record_id[:12]}."),
              "documents": [{"sha256": sha256, "date_received": day, "source_request": request,
                             "redaction_ids": [record_id], "use_restriction": use_restriction is True}], "citations": []}
    try:
        got = propose(project, packet, now=now, only_new=True)
    except PacketError as error:
        raise PacketError("PRIVACY_LINT", "title or text") if error.code == "privacy_lint" else error from None
    return dict(got, result="PACKET_EXISTS" if got["result"] in ("exists", "duplicate") else got["result"])


def _get(rows, ident, revision=None):
    _need(isinstance(ident, str) and PACKET_ID.fullmatch(ident) is not None and ident in rows, "packet_unknown")
    _need(revision is None or revision == rows[ident]["revision"], "packet_stale")
    return rows[ident]


def _reading(project, read_only, use, absent):
    """Run use(lock). read_only opens the lock O_RDONLY (never creates it); with no lock file, absent() answers
    only when no journal, switch or kill file exists either, else the state is unreadable (same rule as switch_state)."""
    try:
        with _Locked(project, read_only=read_only) as lock:
            return use(lock)
    except FileNotFoundError:
        if not read_only:
            raise
        if any(os.path.lexists(os.path.join(project, name)) for name in (JOURNAL, SWITCH, KILL)):
            raise PacketError("packets_unavailable") from None
        return absent()


def _no_packet():
    raise PacketError("packet_unknown")


def payload(project, ident, *, read_only=False):
    return _reading(project, read_only, lambda lock: _payload(project, _get(fold(lock.events()), ident)), _no_packet)


def approve(project, ident, signature, now=None, *, revision=None):
    now = time.time() if now is None else now
    _need(isinstance(signature, str) and signature, "approval_required")
    with _Locked(project) as lock:
        row = _get(fold(lock.events()), ident, revision)
        _need(row["status"] == "proposed", "packet_not_open")
        _need(not _expired(row, now), "packet_expired")
        _lint(row["packet"]["title"], row["packet"]["text"])
        try:
            who = approval.verify(_payload(project, row), signature)
        except approval.ApprovalError:
            raise PacketError("approval_required") from None
        lock.append("approved", now, id=ident, revision=row["revision"], principal=who, signature=signature)
    return {"id": ident, "signed_by": who}


def decide(project, ident, how, actor, now=None, *, revision=None, reason=None):
    """how: ``reject`` (before or after approval) or ``cancel`` (the proposer withdraws). Neither works once written."""
    now = time.time() if now is None else now
    _need(how in ("rejected", "cancelled"), "decision_invalid")
    _need(reason is None or (isinstance(reason, str) and 0 < len(reason.strip()) <= 500
                            and len(reason) <= 500 and all(ord(c) >= 32 and ord(c) != 127 for c in reason)), "reason_invalid")
    with _Locked(project) as lock:
        row = _get(fold(lock.events()), ident, revision)
        _need(row["status"] in ("proposed", "approved"), "packet_not_open")
        lock.append(how, now, id=ident, revision=row["revision"], actor=_actor(actor),
                    **({"reason": reason} if reason is not None else {}))
    return {"id": ident, "result": how}


def write_change(project, ident, now=None):
    """Write the site-content change file for an approved packet. Re-verifies everything; writes nothing else."""
    now = time.time() if now is None else now
    with _Locked(project) as lock:
        events = lock.events()
        _need(_switch_state(lock, events) == "ON", "switch_off")
        row = _get(fold(events), ident)
        if row["status"] == "written":
            return {"id": ident, "file": row["file"], "result": "existing"}
        _need(row["status"] == "approved", "packet_not_approved")
        _need(not _expired(row, now), "packet_expired")
        packet = clean(row["packet"])
        try:
            who = approval.verify(_payload(project, row), row["signature"])
        except approval.ApprovalError:
            raise PacketError("approval_required") from None
        _need(who == row["signed_by"], "approval_required")
        try:
            campaign = campaign_config.load(project)
        except ValueError:
            raise PacketError("campaign_config_invalid") from None
        name = f"{ident}-r{row['revision']}.json"
        day = _iso(row["approved_ts"])[:10]
        change = {"schema": CHANGE_SCHEMA, "packet": ident, "revision": row["revision"], "signed_by": who,
                  "approved_at": _iso(row["approved_ts"]), "kind": packet["kind"], "agency_key": packet["agency_key"],
                  "request_id": packet["request_id"], "title": packet["title"], "text": packet["text"],
                  "documents": packet["documents"], "citations": packet["citations"],
                  "target": f"content/updates/{packet['agency_key']}/{day}-{ident}.json",
                  "note": "input for a reviewed site PR; nothing is deployed by this file"}
        if campaign.campaign_id is not None:  # a configured campaign names itself; unconfigured output is unchanged
            change["campaign"] = {k: v for k, v in (
                ("campaign_id", campaign.campaign_id), ("display_name", campaign.display_name),
                ("law_package", campaign.law_package), ("public_base_url", campaign.public_base_url)) if v is not None}
        data = (json.dumps(change, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        out = lock.path(OUT)
        os.makedirs(out, mode=0o700, exist_ok=True)
        _need(not os.path.islink(out), "project_invalid")
        _replace(os.path.join(out, name), data)
        lock.append("written", now, id=ident, revision=row["revision"], file=f"{OUT}/{name}",
                    file_sha256=hashlib.sha256(data).hexdigest())
    return {"id": ident, "file": f"{OUT}/{name}", "result": "written"}


def pending(project, now=None, *, read_only=False):
    """Pending metadata only; no content, events or journal writes (an absent journal is empty)."""
    now = time.time() if now is None else now

    def use(lock):
        rows = fold(lock.events())
        out = []
        for row in rows.values():
            view = _view(row, now)
            if view["status"] not in ("proposed", "approved"):
                continue
            packet = row["packet"]
            out.append({k: view[k] for k in ("id", "revision", "status", "kind", "agency_key", "title")} | {
                "request_id": packet.get("request_id") or "", "source": packet["source"],
                "proposed_at": _iso(row["proposed_ts"]), "age_hours": max(0, int((now - row["proposed_ts"]) // 3600)),
                "expires_at": _iso(row["proposed_ts"] + EXPIRY_SECONDS),
                "hours_to_expiry": max(0, int((row["proposed_ts"] + EXPIRY_SECONDS - now) // 3600)),
                "approved_at": _iso(row["approved_ts"]) if "approved_ts" in row else None,
                "signed_by": row.get("signed_by")})
        return out
    return _reading(project, read_only, use, list)


def status(project, now=None, *, read_only=False):
    now = time.time() if now is None else now

    def use(lock):
        events = lock.events()
        rows = fold(events)
        return {"switch": _switch_state(lock, events), "packets": [_view(r, now) for r in rows.values()]}
    return _reading(project, read_only, use, lambda: {"switch": "OFF", "packets": []})


# ---- command line ------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="records public-packets", description=(
        "Update packets for documents, summaries and findings. Writes a site-content change file on G's signed approval; "
        "publishes and deploys nothing; ships OFF."))
    parser.add_argument("action", choices=("init", "status", "propose", "payload", "approve", "reject", "cancel", "write",
                                           "switch-payload", "enable", "off", "propose-document"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--packet-file")
    parser.add_argument("--id")
    parser.add_argument("--signature-file")
    parser.add_argument("--actor", default="console")
    parser.add_argument("--sha256", help="propose-document: the received original's SHA-256")
    parser.add_argument("--redaction-record", help="propose-document: SHA-256 of the applied record's ELIGIBILITY.json")
    parser.add_argument("--store", help="propose-document: redaction store holding agencies/<agency>/redaction/ "
                                        "(default: the project)")
    parser.add_argument("--title", help="propose-document: packet title (default: names the agency and the day)")
    parser.add_argument("--use-restriction", action="store_true", help="propose-document: mark a use restriction")
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            out = {"switch": init(args.project)}
        elif args.action == "propose-document":
            out = propose_document(args.project, args.sha256, args.redaction_record, store=args.store, title=args.title,
                                   use_restriction=args.use_restriction)
        elif args.action == "status":
            out = status(args.project)
        elif args.action == "propose":
            raw = _read(args.packet_file or "", MAX_TEXT * 8)
            _need(raw is not None, "packet_invalid")
            out = propose(args.project, _json(raw, "packet_invalid"))
        elif args.action in ("payload", "switch-payload"):
            sys.stdout.buffer.write(payload(args.project, args.id) if args.action == "payload" else switch_payload(args.project))
            return 0
        elif args.action in ("approve", "enable"):
            raw = _read(args.signature_file or "", 8192)
            _need(raw is not None, "approval_required")
            sig = raw.decode("utf-8", "replace")
            out = approve(args.project, args.id, sig) if args.action == "approve" else {"switch": enable(args.project, sig)}
        elif args.action in ("reject", "cancel"):
            out = decide(args.project, args.id, {"reject": "rejected", "cancel": "cancelled"}[args.action], args.actor)
        elif args.action == "write":
            out = write_change(args.project, args.id)
        else:
            out = {"switch": turn_off(args.project, args.actor)}
        print(json.dumps(out, sort_keys=True))
        return 0
    except PacketError as error:
        print(error.code + (" " + error.detail if error.detail else ""), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
