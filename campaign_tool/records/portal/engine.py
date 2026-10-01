"""Fetch state machine with injected no-redirect streaming transport.

No network implementation is installed by this module. Integrators must provide
an independently permitted egress checker and a bounded transport.
"""
import hashlib
import json
import os
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urljoin
from .policy import PortalError, url_parts
from .store import digest_bytes


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

    def __post_init__(self):
        caps = {"max_bytes":100*1024*1024,"max_items":1000,"max_redirects":10,
                "max_attempts":10,"zip_members":10000,"zip_expanded":1024*1024*1024,"zip_ratio":1000}
        for name, cap in caps.items():
            value = getattr(self,name)
            if type(value) is not int or not 1 <= value <= cap:
                raise ValueError("invalid_limit")
        if not 0 < self.timeout <= 120 or not 0 <= self.backoff <= 86400:
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


def _download(queue, item, approval, egress, transport, limits, evidence, wall, mono):
    url = item["last_url_private"]
    if hasattr(transport,"for_origin"):
        transport=transport.for_origin(item["host"])
    deadline = mono() + limits.timeout
    for hop in range(limits.max_redirects + 1):
        host = approval.authorize(url, item["host"], datetime.fromtimestamp(wall(), timezone.utc))
        entry = {"host":host,"http_status":None,"content_type":None,"bytes":0}
        evidence.append(entry)
        if egress(url) is not True:
            raise PortalError("egress_denied")
        remaining = deadline - mono()
        if remaining <= 0:
            raise PortalError("timeout")
        response = transport.open(url, timeout=remaining)
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
            if response.status in (429,500,502,503,504):
                raise PortalError("transient_http")
            if response.status != 200:
                raise PortalError("http_failure")
            if content_type in ("text/html","application/xhtml+xml"):
                raise PortalError("login_page")
            encoding = headers.get("content-encoding", "identity").lower()
            if encoding not in ("", "identity"):
                raise PortalError("encoded_body_unsupported")
            declared = headers.get("content-length")
            if declared is not None and (not declared.isdigit() or len(declared)>12):
                raise PortalError("invalid_length")
            if declared is not None and int(declared)>limits.max_bytes:
                raise PortalError("size_limit")
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
                        entry["bytes"] += len(chunk)
                        if entry["bytes"]>limits.max_bytes:
                            raise PortalError("size_limit")
                        prefix = (prefix+chunk)[:512]
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
                if prefix.startswith(b"PK") or content_type in ("application/zip","application/x-zip-compressed"):
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


def fetch_queue(queue, *, apply=False, approval_loader=None, egress=None, transport=None,
                ledger=None, limits=None, wall=time.time, mono=time.monotonic, fault=None):
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
    with queue.lock():
        with queue.db:
            queue.db.execute("UPDATE portal_attempts SET result='interrupted',finished=? WHERE result='running'",(wall(),))
            queue.db.execute("UPDATE portal_items SET state='retry',reason='interrupted' WHERE state='fetching'")
            queue.db.execute("UPDATE portal_items SET state='blocked',reason='retry_exhausted' WHERE state='retry' AND tries>=?",(limits.max_attempts,))
        items = queue.db.execute("SELECT * FROM portal_items WHERE state IN ('pending','retry') AND next_attempt<=? AND tries<? ORDER BY id LIMIT ?",(wall(),limits.max_attempts,limits.max_items)).fetchall()
        for item in items:
            evidence=[]
            with queue.db:
                attempt=queue.db.execute("INSERT INTO portal_attempts(item,generation,started,result,evidence_json) VALUES(?,?,?,'running','[]')",(item["id"],item["generation"],wall())).lastrowid
                queue.db.execute("UPDATE portal_items SET state='fetching',tries=tries+1 WHERE id=?",(item["id"],))
            outcome["attempted"] += 1
            try:
                approval=approval_loader()
                digest,size=_download(queue,item,approval,egress,transport,limits,evidence,wall,mono)
                if fault:
                    fault("after_object")
                with queue.db:
                    if item["current_sha256"] != digest:
                        receipt_id=digest_bytes(json.dumps([item["id"],digest,item["current_sha256"],attempt],separators=(",",":")).encode())
                        provenance={"host":item["host"],"request_id":item["request_id"],"item_id":item["item_id"],
                            "generation":item["generation"],"source_sha256s":[r[0] for r in queue.db.execute(
                                "SELECT source_sha256 FROM portal_notices WHERE item=? ORDER BY source_sha256",(item["id"],))],
                            "http_evidence":evidence}
                        queue.db.execute("INSERT INTO portal_versions(id,item,sha256,byte_count,previous_sha256,attempt,provenance_json) VALUES(?,?,?,?,?,?,?)",(receipt_id,item["id"],digest,size,item["current_sha256"],attempt,json.dumps(provenance,sort_keys=True)))
                    queue.db.execute("UPDATE portal_items SET state='received',reason=NULL,current_sha256=? WHERE id=?",(digest,item["id"]))
                    queue.db.execute("UPDATE portal_attempts SET result='received',finished=?,evidence_json=? WHERE id=?",(wall(),json.dumps(evidence),attempt))
                outcome["received"]+=1
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
                state="retry" if reason in ("timeout","transient_http","transport_or_storage_failure") and item["tries"]+1<limits.max_attempts else "blocked"
                with queue.db:
                    queue.db.execute("UPDATE portal_items SET state=?,reason=?,next_attempt=? WHERE id=?",(state,reason,wall()+limits.backoff*(2**item["tries"]),item["id"]))
                    queue.db.execute("UPDATE portal_attempts SET result=?,finished=?,evidence_json=? WHERE id=?",(reason,wall(),json.dumps(evidence),attempt))
                outcome[state]+=1
        delivery=queue.deliver(ledger,limits.max_items)
        outcome["ledger_delivery"]=delivery
        outcome["ledger_delivered"]=delivery["delivered"]
        outcome["ledger_pending"]=queue.status()["ledger_pending"]
        if delivery["failed"]:
            outcome["ledger_error"]="ledger_delivery_pending"
    return outcome
