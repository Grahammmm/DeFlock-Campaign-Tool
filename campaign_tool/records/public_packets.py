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
    if "draft_provenance" in packet:
        out["draft_provenance"] = _draft_provenance(packet["draft_provenance"], out)
    return out


def _draft_provenance(value, packet):
    """Closed optional signature material; admission here does not certify source eligibility."""
    from . import draft_import
    fields = {"schema", "source_project_id", "queue_id", "queue_revision", "body_sha256", "evidence_sha256",
              "approval_signature_sha256", "target", "citations", "source_attachment_sha256", "source_section",
              "source_run_id", "correction", "reviewed_sources_sha256"}
    code = "draft_provenance_invalid"
    _need(isinstance(value, dict) and set(value) == fields, code)
    _need(value["schema"] == "records-draft-promotion-provenance-v1"
          and packet["kind"] in ("summary", "site_update"), code)
    for key in ("queue_id", "body_sha256", "evidence_sha256", "approval_signature_sha256",
                "source_attachment_sha256", "reviewed_sources_sha256"):
        _need(isinstance(value[key], str) and SHA.fullmatch(value[key]), code)
    _need(isinstance(value["source_project_id"], str) and PACKET_ID.fullmatch(value["source_project_id"])
          and type(value["queue_revision"]) is int and value["queue_revision"] >= 0, code)
    _need(value["body_sha256"] == hashlib.sha256(packet["text"].encode("utf-8")).hexdigest(), code)
    _need(packet["source"] == "queue-" + value["queue_id"], code)
    for key in ("source_section", "source_run_id"):
        _need(isinstance(value[key], str) and draft_import.ID.fullmatch(value[key]), code)
    _need(isinstance(value["citations"], list) and 1 <= len(value["citations"]) <= 64, code)
    copy = {"kind": "draft-agency-summary" if packet["kind"] == "summary" else "draft-correction-notice",
            "target": value["target"], "citations": value["citations"]}
    correction = value["correction"]
    if packet["kind"] == "summary":
        _need(correction is None, code)
    else:
        _need(isinstance(correction, dict) and set(correction) == {
            "packet_id", "packet_revision", "text_sha256", "prior_statement", "prior_version", "linkage"}, code)
        _need(isinstance(correction["packet_id"], str) and PACKET_ID.fullmatch(correction["packet_id"])
              and type(correction["packet_revision"]) is int and correction["packet_revision"] >= 0
              and isinstance(correction["text_sha256"], str) and SHA.fullmatch(correction["text_sha256"]), code)
        copy["correction"] = {k: correction[k] for k in ("prior_statement", "prior_version", "linkage")}
    try:
        draft_import.validate_copy(copy)
        encoded = _canon(value).encode("utf-8")
        _need(len(encoded) <= 128 * 1024, code)
        # Reject surrogate text and detach caller-owned nested structures.
        aq_text = json.dumps(value, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError, draft_import.aq.ApprovalQueueError):
        raise PacketError(code) from None
    return json.loads(aq_text)


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
    text = "\n".join([f"project: {project_id(project)}", f"id: {row['id']}", f"kind: {p['kind']}", f"agency: {p['agency_key']}",
                      f"request: {p['request_id']}", f"title: {p['title']}", "text:", p["text"],
                      "documents:", _canon(p["documents"]), "citations:", _canon(p["citations"])])
    return text + ("\ndraft_provenance:\n" + _canon(p["draft_provenance"]) if "draft_provenance" in p else "")


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
        except approval.TrustUnavailable:
            raise PacketError("approval_unavailable") from None
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

def propose(project, packet, now=None, *, only_new=False, create_only=False):
    """Add a packet. Identical content is a duplicate (no event); changed content is a new revision that voids any approval.
    only_new (propose-document): an existing packet under this id that is not rejected or cancelled is left alone (exists).
    create_only (draft-promote): only absent or exact unexpired proposed revision zero; never reopen or supersede."""
    now = time.time() if now is None else now
    packet = clean(packet)
    ident = packet_id(packet)
    digest = hashlib.sha256(_canon(packet).encode()).hexdigest()
    with _Locked(project, create=True) as lock:
        row = fold(lock.events()).get(ident)
        if create_only and row is not None:
            _need(row["revision"] == 0 and row["status"] == "proposed" and not _expired(row, now)
                  and row["digest"] == digest and row["packet"] == packet, "packet_conflict")
            return {"id": ident, "revision": 0, "result": "duplicate"}
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
        except approval.TrustUnavailable:
            raise PacketError("approval_unavailable") from None
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
        except approval.TrustUnavailable:
            raise PacketError("approval_unavailable") from None
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


# ---- read-only inventory of written packets whose approval no longer verifies (FLK-CON-3) ------

def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _written_revisions(events):
    """-> [{id, revision, packet, digest, approved_ts, principal, signature, written_ts}] for every effective
    approved -> written transition, per (id, revision), including revisions a later proposal superseded.
    Refuses ``written_packet_invalid`` on malformed or ambiguous effective written history; never repairs it."""
    state, current = {}, {}  # (id, revision) -> row; id -> latest proposed revision (an older one is superseded)
    for e in events:
        kind, ident = e.get("event"), e.get("id")
        if kind not in ("packet", "approved", "rejected", "cancelled", "written"):
            continue
        rev = e.get("revision")
        if kind == "packet":
            _need(isinstance(ident, str) and PACKET_ID.fullmatch(ident) is not None and _is_int(rev) and rev >= 0
                  and _is_int(e.get("ts")) and isinstance(e.get("packet"), dict) and isinstance(e.get("digest"), str),
                  "written_packet_invalid")
            _need((ident, rev) not in state, "written_packet_invalid")  # a repeated proposal of one revision is ambiguous
            state[(ident, rev)] = {"id": ident, "revision": rev, "packet": e["packet"], "digest": e["digest"],
                                   "status": "proposed"}
            current[ident] = rev
            continue
        row = state.get((ident, rev)) if isinstance(ident, str) and _is_int(rev) else None
        if row is None or current.get(ident) != rev:
            continue
        if kind == "approved" and row["status"] == "proposed":
            row.update(status="approved", principal=e.get("principal"), signature=e.get("signature"),
                       approved_ts=e.get("ts"))
        elif kind in ("rejected", "cancelled") and row["status"] in ("proposed", "approved"):
            row["status"] = kind
        elif kind == "written" and row["status"] == "approved":
            row.update(status="written", written_ts=e.get("ts"))
    return [r for r in state.values() if r["status"] == "written"]


def _written_checked(row):
    """Bounded validation of one written revision, before any of it is used. -> its report target, written_at."""
    packet = row["packet"]
    _need(isinstance(row["principal"], str) and 0 < len(row["principal"]) <= 256, "written_packet_invalid")
    _need(isinstance(row["signature"], str) and 0 < len(row["signature"]) <= 16384, "written_packet_invalid")
    _need(_is_int(row["approved_ts"]) and _is_int(row["written_ts"]), "written_packet_invalid")
    for field in ("kind", "agency_key", "request_id", "title", "text"):
        _need(isinstance(packet.get(field), str), "written_packet_invalid")
    _need(isinstance(packet.get("documents"), list) and isinstance(packet.get("citations"), list), "written_packet_invalid")
    _need(KEY.fullmatch(packet["agency_key"]) is not None, "written_packet_invalid")
    _need(hashlib.sha256(_canon(packet).encode()).hexdigest() == row["digest"], "written_packet_invalid")
    try:
        day, written_at = _iso(row["approved_ts"])[:10], _iso(row["written_ts"])
    except (OverflowError, OSError, ValueError):
        raise PacketError("written_packet_invalid") from None
    return f"content/updates/{packet['agency_key']}/{day}-{row['id']}.json", written_at


def _anchor_ready():
    try:
        approval.anchor_status()
    except approval.TrustUnavailable:
        raise PacketError("APPROVAL_ANCHOR_UNAVAILABLE") from None


def revoked_written(project):
    """Read-only: every written packet revision whose recorded approval does not verify against the CURRENT anchor.
    -> [{id, revision, target, written_at}] sorted by (id, revision). An unavailable anchor raises
    PacketError(APPROVAL_ANCHOR_UNAVAILABLE), never an empty list. Changes nothing; sends, publishes, approves nothing."""
    def use(lock):
        _anchor_ready()
        out = []
        for row in _written_revisions(lock.events()):
            target, written_at = _written_checked(row)
            try:
                who = approval.verify(_payload(project, row), row["signature"])
            except approval.TrustUnavailable:
                raise PacketError("APPROVAL_ANCHOR_UNAVAILABLE") from None
            except approval.ApprovalError:
                who = None
            if who != row["principal"]:
                out.append({"id": row["id"], "revision": row["revision"], "target": target, "written_at": written_at})
        return sorted(out, key=lambda r: (r["id"], r["revision"]))

    def absent():
        _anchor_ready()
        return []
    try:
        return _reading(project, True, use, absent)
    except (KeyError, TypeError, ValueError, RecursionError, OverflowError):
        raise PacketError("written_packet_invalid") from None
    except OSError:
        raise PacketError("packets_unavailable") from None


REVOKED_HEADER = "Written packets with approvals that do not verify against current trust (exclude from site-PR review):"


def _print_revoked(rows, as_json):
    if as_json:
        print(json.dumps({"packets": rows}, sort_keys=True))
    elif not rows:
        print("No written packets with non-verifying approvals.")
    else:
        print(REVOKED_HEADER)
        for r in rows:
            print(f"{r['id']}\t{r['revision']}\t{r['target']}\t{r['written_at']}")


# ---- command line ------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="records public-packets", description=(
        "Update packets for documents, summaries and findings. Writes a site-content change file on G's signed approval; "
        "publishes and deploys nothing; ships OFF."))
    parser.add_argument("action", choices=("init", "status", "propose", "payload", "approve", "reject", "cancel", "write",
                                           "switch-payload", "enable", "off", "propose-document", "export-changes",
                                           "revoked-written"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--json", action="store_true", help="revoked-written: machine-readable list")
    parser.add_argument("--out", help="private host export directory; export-changes only")
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
        if args.action == "revoked-written":
            rows = revoked_written(args.project)  # accumulated first: no partial report survives a late failure
            _print_revoked(rows, args.json)
            return 0
        if args.action == "export-changes":
            from .packet_changes import export_changes
            _need(args.out, "out_required")
            try:
                out = export_changes(args.project, args.out)
            except OSError:
                raise PacketError("export_io_error") from None
        elif args.action == "init":
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
