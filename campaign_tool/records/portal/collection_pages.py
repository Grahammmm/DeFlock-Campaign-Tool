"""Typed page acquisition and operator-selected parsers; no live provider profile.

The sole built-in profile is deliberately synthetic and restricted to .invalid
hosts. Importing this module opens no connection and reads no configuration.
"""
from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from types import MappingProxyType
from urllib.parse import parse_qs, urljoin

from .collections import normalized
from .engine import COLLECTION_REQUEST, Limits, PageBinding, fetch_collection_page
from .policy import PortalError, url_parts
from .store import digest_bytes, identifier

MAX_PAGE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Member:
    id: str
    kind: str
    url: str


@dataclass(frozen=True)
class ParsedPage:
    members: tuple
    next_cursor: str | None
    expected_total: int


class SyntheticHTML(HTMLParser):
    """Strict artificial DOM contract, never a WebLink selector heuristic."""
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base = normalized(base_url)
        self.host = url_parts(self.base).hostname
        # Keep blank values: a dropped "id=" or unknown "key=" would otherwise
        # hide a duplicate identity declaration from the checks below.
        self.query = parse_qs(url_parts(self.base).query, keep_blank_values=True, strict_parsing=True)
        self.active = False
        self.closed = False
        self.roots = 0
        self.tags = 0
        self.total = None
        self.terminal = False
        self.empty = False
        self.members = []
        self.next = None

    def link(self, literal):
        url = normalized(urljoin(self.base, literal))
        if url_parts(url).hostname != self.host:
            raise PortalError("unapproved_origin")
        return url

    def handle_starttag(self, tag, attrs):
        self.tags += 1
        if self.tags > 10000 or len(attrs) > 30 or len({k for k, v in attrs}) != len(attrs):
            raise PortalError("collection_markup_bound")
        values = dict(attrs)
        if tag in {"form", "input"} or values.get("data-auth-required") == "true":
            raise PortalError("authorization_required")
        if tag in {"script", "iframe", "base"}:
            raise PortalError("unsupported_navigation")
        if tag == "main":
            self.roots += 1
            if self.roots != 1 or values.get("data-fixture-provider") != "synthetic-archive-v1":
                raise PortalError("unsupported_navigation")
            collection = identifier(values.get("data-collection"))
            if self.query.get("id") != [collection] or not set(self.query) <= {"id", "page"}:
                raise PortalError("collection_identity_mismatch")
            raw_total = values.get("data-total", "")
            if not isinstance(raw_total, str) or not re.fullmatch(r"[0-9]{1,4}", raw_total) or int(raw_total) > 1000:
                raise PortalError("collection_denominator_unknown")
            self.total = int(raw_total)
            if values.get("data-terminal") not in {None, "true"}:
                raise PortalError("unsupported_navigation")
            self.terminal = values.get("data-terminal") == "true"
            self.active = True
        if "data-record-id" in values and not self.active:
            raise PortalError("unsupported_navigation")
        if not self.active:
            return
        if values.get("data-empty") == "true":
            self.empty = True
        if tag == "a":
            href = values.get("href")
            if not href:
                raise PortalError("unsupported_navigation")
            url = self.link(href)
            if values.get("rel") == "next":
                if self.next is not None or "data-record-id" in values:
                    raise PortalError("unsupported_navigation")
                p = url_parts(url)
                q = parse_qs(p.query, keep_blank_values=True, strict_parsing=True)
                if p.path != url_parts(self.base).path or q.get("id") != self.query.get("id") or not set(q) <= {"id", "page"}:
                    raise PortalError("collection_identity_mismatch")
                self.next = url
            else:
                key = identifier(values.get("data-record-id"))
                kind = values.get("data-record-kind", "document")
                if kind not in {"document", "collection"} or any(m.id == key for m in self.members):
                    raise PortalError("collection_member_invalid")
                self.members.append(Member(key, kind, url))
                if len(self.members) > 1000:
                    raise PortalError("collection_member_bound")

    def handle_endtag(self, tag):
        if tag == "main":
            if not self.active or self.closed:
                raise PortalError("unsupported_navigation")
            self.active = False
            self.closed = True

    def result(self):
        if self.roots != 1 or self.active or not self.closed or self.total is None:
            raise PortalError("unsupported_navigation")
        if self.terminal != (self.next is None):
            raise PortalError("collection_pagination_unknown")
        if self.total == 0 and (not self.empty or self.members or not self.terminal):
            raise PortalError("collection_denominator_unknown")
        return ParsedPage(tuple(self.members), self.next, self.total)


@dataclass(frozen=True)
class SyntheticProfile:
    id: str = "synthetic-archive-v1"

    def accepts(self, url):
        return url_parts(url).hostname.endswith(".example.invalid")

    def parse(self, raw, url):
        if not self.accepts(url):
            raise PortalError("unsupported_provider")
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PAGE_BYTES:
            raise PortalError("collection_page_size_bound")
        try:
            text = raw.decode("utf-8", errors="strict")
            parser = SyntheticHTML(url)
            parser.feed(text)
            parser.close()
            return parser.result()
        except (UnicodeError, ValueError) as exc:
            if isinstance(exc, PortalError):
                raise
            raise PortalError("unsupported_navigation") from None


# Trusted source registry, never a module name or executable selected from mail.
PROFILES = MappingProxyType({"synthetic-archive-v1": SyntheticProfile()})


def _binding(profile):
    return PageBinding(profile.id, hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def run_collection_pages(inventory, event, *, profile_id, apply=False, approval_loader=None,
                         egress=None, transport=None, limits=None, wall=time.time, mono=time.monotonic):
    """Bounded page inventory only; no document download/stage/publication run.

    Raw pages use the existing sidecar attempt/CAS/version state machine, with
    separate ledger_state values so they never enter the canonical document
    outbox. Successful page inventories use the existing immutable scope guard.
    """
    if type(apply) is not bool or not isinstance(profile_id, str) or profile_id not in PROFILES:
        raise PortalError("unsupported_provider")
    profile = PROFILES[profile_id]
    event_row = inventory._event(event)
    pending = inventory.pending_pages(event)
    if any(not profile.accepts(item["url_private"]) for item in pending):
        raise PortalError("unsupported_provider")
    if not apply:
        return {"mode": "plan_only", "pending_pages": len(pending), "network_calls": 0}
    inventory._require_writer()
    limits = limits or Limits(max_bytes=MAX_PAGE_BYTES)
    if limits.max_items > 60 or limits.max_bytes > MAX_PAGE_BYTES:
        raise PortalError("collection_limit_exceeded")
    queue = inventory.queue
    binding = _binding(profile)
    outcome = {"attempted": 0, "indexed": 0, "blocked": 0, "pending": 0,
               "profile": profile.id, "parser_sha256": binding.parser_sha256,
               "document_downloads": 0, "stage_promotions": 0, "publication_authorized": False}
    visited = set()
    for _ in range(limits.max_items):
        work = next((item for item in inventory.pending_pages(event) if item["url_private"] not in visited), None)
        if work is None:
            break
        url = work["url_private"]
        visited.add(url)
        if not profile.accepts(url):
            raise PortalError("unsupported_provider")
        item_id = digest_bytes(json.dumps(["collection-page", url], separators=(",", ":")).encode())
        item = queue.inventory(url_parts(url).hostname, COLLECTION_REQUEST, item_id, url, event_row["eml_sha256"])
        fetched = fetch_collection_page(queue, item, binding=binding, apply=True,
            approval_loader=approval_loader, egress=egress, transport=transport, limits=limits, wall=wall, mono=mono)
        outcome["attempted"] += fetched["attempted"]
        if fetched["state"] != "received":
            outcome["blocked" if fetched["state"] == "blocked" else "pending"] += 1
            if fetched["state"] == "blocked":
                reason = fetched["reason"]
                if fetched.get("http_status") == 410:
                    disposition = "expired_link"
                elif fetched.get("http_status") in {401, 403} or reason in {
                        "authorization_required", "egress_denied", "approval_expired",
                        "portal_not_approved", "redirect_not_approved", "path_not_approved"}:
                    disposition = "authorization_required"
                elif reason in {"retry_exhausted", "transient_http", "timeout", "transport_or_storage_failure"}:
                    disposition = "retry_exhausted"
                else:
                    disposition = "unsupported_navigation"
                inventory.block(work["collection"], disposition)
            continue
        try:
            from campaign_tool.records.intake.folder import secure_open
            with secure_open(queue.objects / fetched["sha256"]) as source:
                raw = source.read(MAX_PAGE_BYTES + 1)
            if hashlib.sha256(raw).hexdigest() != fetched["sha256"]:
                raise PortalError("object_integrity_failed")
            parsed = profile.parse(raw, url)
            inventory.accept_page(work["collection"], cursor=url, source_sha256=fetched["sha256"],
                source_bytes=fetched["bytes"], parser_sha256=binding.parser_sha256,
                members=[{"id": m.id, "kind": m.kind, "url": m.url} for m in parsed.members],
                next_cursor=parsed.next_cursor, expected_total=parsed.expected_total, event=event)
            outcome["indexed"] += 1
        except PortalError as exc:
            reason = str(exc)
            if not re.fullmatch(r"[a-z_]{1,80}", reason):
                reason = "collection_parser_failure"
            with queue.lock(), queue.db:
                queue.db.execute("UPDATE portal_items SET state='blocked',reason=? WHERE id=?", (reason, item))
                queue.db.execute("UPDATE portal_versions SET ledger_state='collection_blocked' WHERE id=?", (fetched["version"],))
            inventory.block(work["collection"], "authorization_required" if reason == "authorization_required" else "unsupported_navigation")
            outcome["blocked"] += 1
    outcome["retrieval_complete"] = inventory.readiness(event)["retrieval_complete"]
    return outcome
