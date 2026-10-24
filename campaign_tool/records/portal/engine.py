"""Fetch state machine with injected no-redirect streaming transport.

No network implementation is installed by this module. Integrators must provide
an independently permitted egress checker and a bounded transport.
"""
import hashlib
import json
import math
import os
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from urllib.parse import urljoin, urlsplit
from .policy import PortalError, url_parts
from .store import check_hash, digest_bytes, identifier

COLLECTION_REQUEST = "collection-page-v1"


class PayloadKind(Enum):
    DOCUMENT = "document"
    COLLECTION_PAGE = "collection_page"


@dataclass(frozen=True)
class PageBinding:
    profile_id: str
    parser_sha256: str

    def __post_init__(self):
        identifier(self.profile_id)
        check_hash(self.parser_sha256)


class Transport(Protocol):
    def open(self, url: str, *, timeout: float):
        """Return Response; no automatic redirects; chunks <= 64 KiB; timeout applies to reads."""


class Ledger(Protocol):
    def record_original(self, receipt: dict, object_path):
        """Atomically upsert by receipt['id']; preserve provenance, never promote review."""


@dataclass
class Response:
    status: int
    headers: dict
    chunks: object = field(default_factory=tuple)
    close: object = lambda: None


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 25 * 1024 * 1024
    max_items: int = 60
    max_redirects: int = 3
    timeout: float = 25
    max_attempts: int = 3
    backoff: float = 30
    zip_members: int = 1000
    zip_expanded: int = 100 * 1024 * 1024
    zip_ratio: int = 100
    max_total_bytes: int = 256 * 1024 * 1024
    run_timeout: float = 1200

    def __post_init__(self):
        caps = {"max_total_bytes":1024*1024*1024,"max_bytes":100*1024*1024,"max_items":1000,"max_redirects":10,
                "max_attempts":10,"zip_members":10000,"zip_expanded":1024*1024*1024,"zip_ratio":1000}
        for name, cap in caps.items():
            value = getattr(self,name)
            if type(value) is not int or not 1 <= value <= cap:
                raise ValueError("invalid_limit")
        if self.max_total_bytes < self.max_bytes:
            raise ValueError("invalid_limit")
        if any(type(v) not in (int,float) or not math.isfinite(v)
               for v in (self.timeout,self.backoff,self.run_timeout)) or not 0 < self.run_timeout <= 2400 or not 0 < self.timeout <= 120 or not 0 <= self.backoff <= 86400:
            raise ValueError("invalid_limit")


def archive_check(path, limits):
    """Inspect central-directory bounds only. Never extract; downstream must enforce again."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > limits.zip_members:
                raise PortalError("archive_limits")
            total = 0
            for entry in entries:
                name = entry.filename
                if name.startswith("/") or "\\" in name or any(p in ("..", ".") for p in name.split("/")) or ":" in name:
                    raise PortalError("archive_unsafe_path")
                if (entry.external_attr >> 16) & 0o170000 == 0o120000 or entry.flag_bits & 1:
                    raise PortalError("archive_unsupported_member")
                total += entry.file_size
                if total > limits.zip_expanded or entry.file_size > max(entry.compress_size,1)*limits.zip_ratio:
                    raise PortalError("archive_limits")
    except (zipfile.BadZipFile, OSError, NotImplementedError):
        raise PortalError("archive_invalid") from None


def _download(queue, item, approval, egress, transport, limits, evidence, wall, mono,
              *, kind=PayloadKind.DOCUMENT, budget=None, approval_loader=None):
    if type(kind) is not PayloadKind or kind is PayloadKind.COLLECTION_PAGE and item["request_id"] != COLLECTION_REQUEST:
        raise PortalError("payload_kind_mismatch")
    url = item["last_url_private"]
    if hasattr(transport,"for_origin"):
        transport=transport.for_origin(item["host"])
    deadline = min(mono() + limits.timeout, budget['deadline']) if budget else mono() + limits.timeout
    for hop in range(limits.max_redirects + 1):
        if kind is PayloadKind.COLLECTION_PAGE and re.search(
                r"/(?:cookiecheck|login|signin|challenge|auth)(?:\.aspx)?(?:/|$)", url_parts(url).path, re.I):
            raise PortalError("authorization_required")
        if approval_loader:
            approval = approval_loader()
        host = approval.authorize(url, item["host"], datetime.fromtimestamp(wall(), timezone.utc))
        entry = {"host":host,"path_sha256":digest_bytes(urlsplit(url).path.encode()),
                 "http_status":None,"content_type":None,"bytes":0}
        evidence.append(entry)
        if egress(url) is not True:
            raise PortalError("egress_denied")
        remaining = deadline - mono()
        if remaining <= 0:
            raise PortalError("timeout")
        permitted = min(limits.max_bytes, budget['remaining']) if budget else limits.max_bytes
        if permitted <= 0:
            raise PortalError('total_size_limit')
        bounded = transport.for_budget(permitted) if hasattr(transport,'for_budget') else transport
        response = bounded.open(url, timeout=remaining)
        try:
            if type(response.status) is not int:
                raise PortalError("invalid_http_response")
            entry["http_status"] = response.status
            headers = {str(k).lower():str(v) for k,v in response.headers.items()}
            content_type = headers.get("content-type", "").split(";",1)[0].strip().lower()
            # Untrusted header values can contain credentials; reports retain only MIME syntax.
            entry["content_type"] = content_type if re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+",content_type) else "unknown"
            if response.status in (301,302,303,307,308):
                location = headers.get("location")
                if not location:
                    raise PortalError("redirect_without_location")
                if hop == limits.max_redirects:
                    raise PortalError("redirect_limit")
                next_url = urljoin(url,location)
                next_host = url_parts(next_url).hostname
                # Include denied redirect host without retaining path, query or Location.
                try:
                    approval.authorize(next_url,item["host"],datetime.fromtimestamp(wall(),timezone.utc))
                except PortalError:
                    evidence.append({"host":next_host,"http_status":None,"content_type":None,"bytes":0})
                    raise
                url = next_url
                continue
            if response.status in (401,403,410):
                raise PortalError("expired_or_denied_link")
            if response.status == 429 or 500 <= response.status <= 599:
                retry_after = headers.get('retry-after','')
                if retry_after.isdigit() and len(retry_after) <= 5:
                    entry['retry_after'] = min(int(retry_after),86400)
                raise PortalError("transient_http")
            if response.status != 200:
                raise PortalError("http_failure")
            if kind is PayloadKind.DOCUMENT and content_type in ("text/html","application/xhtml+xml"):
                raise PortalError("login_page")
            if kind is PayloadKind.COLLECTION_PAGE and content_type not in ("text/html", "application/xhtml+xml"):
                raise PortalError("unsupported_collection_media")
            encoding = headers.get("content-encoding", "identity").lower()
            if encoding not in ("", "identity"):
                raise PortalError("encoded_body_unsupported")
            declared = headers.get("content-length")
            if declared is not None and (not declared.isdigit() or len(declared)>12):
                raise PortalError("invalid_length")
            if declared is not None and int(declared)>limits.max_bytes:
                raise PortalError("size_limit")
            if declared is not None and int(declared)>permitted:
                raise PortalError("total_size_limit")
            fd, name = tempfile.mkstemp(prefix=".partial-", dir=queue.objects)
            path = queue.objects / os.path.basename(name)
            sha = hashlib.sha256()
            prefix = b""
            sniff_tail = b""
            try:
                with os.fdopen(fd,"wb") as stream:
                    for chunk in response.chunks:
                        if mono() > deadline:
                            raise PortalError("timeout")
                        if not isinstance(chunk,bytes) or len(chunk)>65536:
                            raise PortalError("transport_chunk_contract")
                        previous = entry['bytes']
                        admitted = min(len(chunk), limits.max_bytes - previous,
                                       budget['remaining'] if budget is not None else len(chunk))
                        entry["bytes"] += admitted
                        if budget is not None:
                            budget['remaining'] -= admitted
                            budget['received'] += admitted
                        if previous + len(chunk)>limits.max_bytes:
                            raise PortalError("size_limit")
                        if admitted < len(chunk):
                            raise PortalError('total_size_limit')
                        prefix = (prefix+chunk)[:512]
                        # PDF (XFA/JavaScript) and ZIP bodies may legitimately contain
                        # markup bytes; only sniff bodies without a binary signature.
                        # Any regex match needs >=5 bytes, so the prefix is decided by then.
                        if kind is PayloadKind.DOCUMENT and not prefix.startswith((b"%PDF-", b"PK\x03\x04")):
                            sniff = (sniff_tail+chunk).lower().replace(b"\x00",b"")
                            if re.search(br"<(?:!doctype\s+html|html\b|form\b|input\b|script\b)",sniff):
                                raise PortalError("login_page")
                            sniff_tail = sniff[-128:]
                        sha.update(chunk)
                        stream.write(chunk)
                    if mono()>deadline:
                        raise PortalError("timeout")
                    if not entry["bytes"]:
                        raise PortalError("empty_document")
                    if declared is not None and entry["bytes"] != int(declared):
                        raise PortalError("length_mismatch")
                    if content_type == "application/pdf" and not prefix.startswith(b"%PDF-"):
                        raise PortalError("signature_mismatch")
                    stream.flush()
                    os.fsync(stream.fileno())
                if kind is PayloadKind.DOCUMENT and (prefix.startswith(b"PK") or content_type in ("application/zip","application/x-zip-compressed")):
                    archive_check(path,limits)
                if mono()>deadline:
                    raise PortalError("timeout")
                digest = sha.hexdigest()
                queue.promote(path,digest,entry["bytes"])
                return digest,entry["bytes"]
            finally:
                path.unlink(missing_ok=True)
        finally:
            response.close()
    raise PortalError("redirect_limit")


def _attempt_item(queue, item, *, approval_loader, egress, transport, limits,
                  wall, mono, fault, outcome, kind=PayloadKind.DOCUMENT, binding=None, budget=None):
    """Shared attempt/stream/CAS/version/backoff state machine for both kinds."""
    evidence=[]
    with queue.db:
        attempt=queue.db.execute("INSERT INTO portal_attempts(item,generation,started,result,evidence_json) VALUES(?,?,?,'running','[]')",(item["id"],item["generation"],wall())).lastrowid
        queue.db.execute("UPDATE portal_items SET state='fetching',tries=tries+1 WHERE id=?",(item["id"],))
    outcome["attempted"] += 1
    try:
        approval=approval_loader()
        digest,size=_download(queue,item,approval,egress,transport,limits,evidence,wall,mono,
                              kind=kind,budget=budget,approval_loader=approval_loader)
        if fault:
            fault("after_object")
        with queue.db:
            if item["current_sha256"] != digest:
                receipt_id=digest_bytes(json.dumps([item["id"],digest,item["current_sha256"],attempt],separators=(",",":")).encode())
                provenance={"host":item["host"],"request_id":item["request_id"],"item_id":item["item_id"],
                    "generation":item["generation"],"source_sha256s":[r[0] for r in queue.db.execute(
                        "SELECT source_sha256 FROM portal_notices WHERE item=? ORDER BY source_sha256",(item["id"],))],
                    "http_evidence":evidence}
                provenance.update(queue_item_id=item['id'],
                    source_path_sha256=digest_bytes(urlsplit(item['last_url_private']).path.encode()),
                    fetched_at=datetime.fromtimestamp(wall(),timezone.utc).isoformat(),
                    mail_message_sha256s=provenance['source_sha256s'])
                ledger_state = "pending"
                if kind is PayloadKind.COLLECTION_PAGE:
                    ledger_state = "collection_page"
                    provenance.update(payload_kind=kind.value, profile_id=binding.profile_id,
                                      parser_sha256=binding.parser_sha256)
                queue.db.execute("INSERT INTO portal_versions(id,item,sha256,byte_count,previous_sha256,attempt,provenance_json,ledger_state) VALUES(?,?,?,?,?,?,?,?)",(receipt_id,item["id"],digest,size,item["current_sha256"],attempt,json.dumps(provenance,sort_keys=True),ledger_state))
            queue.db.execute("UPDATE portal_items SET state='received',reason=NULL,current_sha256=? WHERE id=?",(digest,item["id"]))
            queue.db.execute("UPDATE portal_attempts SET result='received',finished=?,evidence_json=? WHERE id=?",(wall(),json.dumps(evidence),attempt))
        outcome["received"]+=1
        outcome.setdefault('receipts',[]).append({'queue_item_id':item['id'],
            'sha256':digest,'host':item['host'],'bytes':size,'status':'received',
            'http_status':next((e['http_status'] for e in reversed(evidence) if e.get('http_status') is not None),None)})
    except Exception as exc:
        if isinstance(exc,PortalError):
            reason=str(exc)
        elif isinstance(exc,TimeoutError):
            reason="timeout"
        else:
            reason="transport_or_storage_failure"
        # Never serialize exception repr, URLs, response bodies or headers.
        if not re.fullmatch(r"[a-z_]{1,80}",reason):
            reason="adapter_failure"
        deferred = reason == 'total_size_limit'
        state="retry" if deferred or reason in ("timeout","dns_timeout","dns_failed","https_connect_failed","transient_http","transport_or_storage_failure") and item["tries"]+1<limits.max_attempts else "blocked"
        delay=max([limits.backoff*(2**item['tries'])]+[e.get('retry_after',0) for e in evidence])
        with queue.db:
            queue.db.execute("UPDATE portal_items SET state=?,reason=?,next_attempt=?,tries=tries-? WHERE id=?",(state,reason,0 if deferred else wall()+delay,int(deferred),item["id"]))
            queue.db.execute("UPDATE portal_attempts SET result=?,finished=?,evidence_json=? WHERE id=?",(reason,wall(),json.dumps(evidence),attempt))
        if deferred:
            outcome['deferred_by_run_cap'] = outcome.get('deferred_by_run_cap',0)+1
        else:
            outcome[state]+=1
        outcome.setdefault('receipts',[]).append({'queue_item_id':item['id'],
            'sha256':None,'host':item['host'],'bytes':sum(e['bytes'] for e in evidence),
            'status':'deferred_by_run_cap' if deferred else state,'reason':reason,
            'http_status':next((e['http_status'] for e in reversed(evidence) if e.get('http_status') is not None),None)})


def fetch_collection_page(queue, item_id, *, binding, apply=False, approval_loader=None,
                          egress=None, transport=None, limits=None, wall=time.time,
                          mono=time.monotonic):
    """Acquire one explicitly enrolled typed page through the shared engine.

    Page versions stay outside the canonical document outbox. This function does
    not parse, select a provider, enroll document members or grant network access.
    """
    if type(apply) is not bool or type(binding) is not PageBinding:
        raise PortalError("typed_page_binding_required")
    check_hash(item_id)
    if not apply:
        return {"mode": "inventory_only", "attempted": 0}
    if approval_loader is None or egress is None or transport is None:
        raise PortalError("collection_transport_unconfigured")
    approval_loader()
    limits = limits or Limits()
    if limits.max_bytes > 25 * 1024 * 1024 or limits.max_attempts > 3 or limits.max_redirects > 3 or limits.timeout > 25:
        raise PortalError("collection_limit_exceeded")
    outcome = {"attempted": 0, "received": 0, "blocked": 0, "retry": 0}
    with queue.lock():
        item = queue.db.execute("SELECT * FROM portal_items WHERE id=?", (item_id,)).fetchone()
        if item is None or item["request_id"] != COLLECTION_REQUEST:
            raise PortalError("payload_kind_mismatch")
        if item["state"] == "fetching":
            with queue.db:
                queue.db.execute("UPDATE portal_attempts SET result='interrupted',finished=? WHERE item=? AND result='running'", (wall(), item_id))
                queue.db.execute("UPDATE portal_items SET state='retry',reason='interrupted' WHERE id=?", (item_id,))
            item = queue.db.execute("SELECT * FROM portal_items WHERE id=?", (item_id,)).fetchone()
        if item["state"] in {"pending", "retry"}:
            if item["tries"] >= limits.max_attempts:
                with queue.db:
                    queue.db.execute("UPDATE portal_items SET state='blocked',reason='retry_exhausted' WHERE id=?", (item_id,))
            elif item["next_attempt"] <= wall():
                _attempt_item(queue, item, approval_loader=approval_loader, egress=egress,
                    transport=transport, limits=limits, wall=wall, mono=mono, fault=None,
                    outcome=outcome, kind=PayloadKind.COLLECTION_PAGE, binding=binding)
        item = queue.db.execute("SELECT * FROM portal_items WHERE id=?", (item_id,)).fetchone()
        outcome.update(state=item["state"], reason=item["reason"])
        last_attempt = queue.db.execute("SELECT evidence_json FROM portal_attempts WHERE item=? ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
        if last_attempt:
            evidence = json.loads(last_attempt[0])
            outcome["http_status"] = next((row["http_status"] for row in reversed(evidence)
                                            if type(row.get("http_status")) is int), None)
        if item["state"] == "received":
            version = queue.db.execute("SELECT * FROM portal_versions WHERE item=? AND sha256=? ORDER BY attempt DESC LIMIT 1",
                                       (item_id, item["current_sha256"])).fetchone()
            if version is None:
                raise PortalError("collection_version_missing")
            provenance = json.loads(version["provenance_json"])
            if (provenance.get("payload_kind"), provenance.get("profile_id"), provenance.get("parser_sha256"), provenance.get("generation")) != (
                    PayloadKind.COLLECTION_PAGE.value, binding.profile_id, binding.parser_sha256, item["generation"]):
                raise PortalError("collection_parser_binding_conflict")
            if version["ledger_state"] != "collection_page":
                raise PortalError("collection_version_state_conflict")
            queue.verify_object(version["sha256"], version["byte_count"])
            outcome.update(version=version["id"], sha256=version["sha256"], bytes=version["byte_count"])
    return outcome


def fetch_queue(queue, *, apply=False, approval_loader=None, egress=None, transport=None,
                ledger=None, limits=None, wall=time.time, mono=time.monotonic, fault=None, preflight=None):
    if not apply:
        return {"mode":"inventory_only",**queue.status()}
    if approval_loader is None:
        raise PortalError("approval_required")
    # Validate approval even on an empty queue. Never infer egress from it.
    approval_loader()
    if egress is None:
        raise PortalError("egress_unconfigured")
    if transport is None:
        raise PortalError("transport_unconfigured")
    limits = limits or Limits()
    outcome = {"attempted":0,"received":0,"blocked":0,"retry":0,"ledger_delivered":0}
    # Deliberately serial, including shared redirect hosts; the lock excludes
    # concurrent fetch processes for this queue. One globally is at most two/host.
    budget = {'remaining':limits.max_total_bytes,'received':0,'deadline':mono()+limits.run_timeout}
    with queue.lock():
        if preflight is not None:
            preflight(queue)
        with queue.db:
            queue.db.execute("UPDATE portal_attempts SET result='interrupted',finished=? WHERE result='running'",(wall(),))
            queue.db.execute("UPDATE portal_items SET state='retry',reason='interrupted' WHERE state='fetching'")
            queue.db.execute("UPDATE portal_items SET state='blocked',reason='retry_exhausted' WHERE state='retry' AND tries>=?",(limits.max_attempts,))
        items = queue.db.execute("SELECT * FROM portal_items WHERE request_id!=? AND state IN ('pending','retry') AND next_attempt<=? AND tries<? ORDER BY id LIMIT ?",(COLLECTION_REQUEST,wall(),limits.max_attempts,limits.max_items)).fetchall()
        for item in items:
            if budget['remaining'] <= 0 or mono() >= budget['deadline']:
                outcome['deferred_by_run_cap'] = len(items)-outcome['attempted']
                break
            _attempt_item(queue,item,approval_loader=approval_loader,egress=egress,
                          transport=transport,limits=limits,wall=wall,mono=mono,
                          fault=fault,outcome=outcome,budget=budget)
            if outcome.get('deferred_by_run_cap'):
                outcome['deferred_by_run_cap'] += len(items)-outcome['attempted']
                break
        outcome['body_bytes_received']=budget['received']
        delivery=queue.deliver(ledger,limits.max_items)
        outcome["ledger_delivery"]=delivery
        outcome["ledger_delivered"]=delivery["delivered"]
        outcome["ledger_pending"]=queue.status()["ledger_pending"]
        if delivery["failed"]:
            outcome["ledger_error"]="ledger_delivery_pending"
    return outcome
