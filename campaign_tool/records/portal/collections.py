"""Durable expectation inventory in the existing portal sidecar.

No network or browser is created. A separately reviewed collection parser submits
pages whose original bytes are already in Queue.objects. Until then Browse URLs
are pending with an unknown denominator. No generic HTML parser guesses that a
page with zero links is an empty collection.
"""
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .notice import Links
from .policy import PortalError, url_parts
from .store import check_hash, digest_bytes, identifier

SCHEMA = """
CREATE TABLE IF NOT EXISTS export_inventory_meta(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS export_events(
 id TEXT PRIMARY KEY, identity_json TEXT UNIQUE NOT NULL, receipt_sha256 TEXT NOT NULL,
 eml_sha256 TEXT NOT NULL, attachments_json TEXT NOT NULL, scope_sha256 TEXT NOT NULL,
 discovery TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS export_body_links(
 event TEXT REFERENCES export_events(id), part TEXT, ordinal INTEGER,
 url_private TEXT NOT NULL, kind TEXT NOT NULL, target TEXT, reason TEXT,
 PRIMARY KEY(event,part,ordinal));
CREATE TABLE IF NOT EXISTS export_collections(
 id TEXT PRIMARY KEY, url_private TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 cursor_private TEXT NOT NULL, expected_total INTEGER, reason TEXT, scope_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS export_collection_pages(
 collection TEXT REFERENCES export_collections(id), cursor_private TEXT,
 source_sha256 TEXT NOT NULL, parser_sha256 TEXT NOT NULL, manifest_json TEXT NOT NULL,
 PRIMARY KEY(collection,cursor_private));
CREATE TABLE IF NOT EXISTS export_collection_members(
 collection TEXT REFERENCES export_collections(id), member TEXT, kind TEXT NOT NULL,
 url_private TEXT NOT NULL, target TEXT NOT NULL,
 PRIMARY KEY(collection,member));
"""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _require_member_shape(member):
    # Provider members are untrusted evidence: check the shape before any
    # subscript or set membership, with a fixed code that never echoes values.
    # Extra producer keys stay accepted and ignored; nothing is repaired.
    if not isinstance(member, dict) or any(not isinstance(member.get(field), str)
                                           for field in ("id", "kind", "url")):
        raise PortalError("invalid_collection_member")


def normalized(url):
    p = url_parts(url)
    # Do not reorder/decode a potentially signed query. Fragments, credentials,
    # non-HTTPS and unsafe paths are rejected by the existing portal policy.
    return urlunsplit(("https", p.hostname, p.path or "/", p.query, ""))


class BodyLinks(Links):
    def handle_data(self, data):
        # Insecure HTTP record links are exceptions, never silently invisible.
        if not self.in_inert_content:
            self.urls.extend(re.findall(r'https?://[^\s<>"\']+', data))


@dataclass(frozen=True)
class Scope:
    hosts: frozenset
    excluded_hosts: frozenset = frozenset()
    max_depth: int = 3
    max_pages: int = 100
    max_members: int = 1000
    max_links: int = 1000

    def __post_init__(self):
        from .policy import hostname
        for host in self.hosts | self.excluded_hosts:
            hostname(host)
        if self.hosts & self.excluded_hosts:
            raise PortalError("scope_conflict")
        for name, ceiling in (("max_depth", 3), ("max_pages", 100),
                              ("max_members", 1000), ("max_links", 1000)):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= ceiling:
                raise PortalError("invalid_inventory_bound")


class CollectionInventory:
    def __init__(self, queue, scope):
        self.queue, self.scope = queue, scope
        with queue.lock():
            queue.db.executescript(SCHEMA)
            versions = queue.db.execute("SELECT version FROM export_inventory_meta").fetchall()
            if not versions:
                queue.db.execute("INSERT INTO export_inventory_meta VALUES(1)")
                queue.db.commit()
            elif len(versions) != 1 or versions[0][0] != 1:
                raise PortalError("export_inventory_version_unsupported")

    def _require_writer(self):
        from campaign_tool.records.run import require_root_lock
        require_root_lock(self.queue.root.parent)

    def _scope_hash(self):
        return digest_bytes(encoded({"hosts": sorted(self.scope.hosts),
            "excluded": sorted(self.scope.excluded_hosts), "depth": self.scope.max_depth,
            "pages": self.scope.max_pages, "members": self.scope.max_members,
            "links": self.scope.max_links}).encode())

    def _event(self, event):
        row = self.queue.db.execute("SELECT * FROM export_events WHERE id=?", (event,)).fetchone()
        if row is None:
            raise PortalError("export_event_missing")
        if row["scope_sha256"] != self._scope_hash():
            raise PortalError("export_event_scope_mismatch")
        return row

    def _node(self, collection):
        node = self.queue.db.execute("SELECT * FROM export_collections WHERE id=?", (collection,)).fetchone()
        if node is None:
            raise PortalError("collection_missing_or_blocked")
        # Nodes may be shared by events only under the exact same immutable
        # plan. A broader event cannot populate an old narrow event's graph.
        if node["scope_sha256"] != self._scope_hash():
            raise PortalError("collection_scope_mismatch")
        return node

    def register(self, preserved, identity):
        self._require_writer()
        scope_hash = self._scope_hash()
        values = (preserved.message_id, encoded(identity), preserved.receipt_sha256,
                  preserved.eml_sha256, encoded(preserved.attachments), scope_hash)
        with self.queue.lock(), self.queue.db:
            old = self.queue.db.execute("SELECT id,identity_json,receipt_sha256,eml_sha256,attachments_json,scope_sha256 "
                                        "FROM export_events WHERE id=? OR identity_json=?", values[:2]).fetchall()
            if old and (len(old) != 1 or tuple(old[0]) != values):
                raise PortalError("export_event_conflict")
            self.queue.db.execute("INSERT OR IGNORE INTO export_events"
                                  "(id,identity_json,receipt_sha256,eml_sha256,attachments_json,scope_sha256) VALUES(?,?,?,?,?,?)", values)

    def _collection(self, url):
        key = digest_bytes(url.encode())
        with self.queue.lock(), self.queue.db:
            self.queue.db.execute("INSERT OR IGNORE INTO export_collections(id,url_private,cursor_private,scope_sha256) "
                                  "VALUES(?,?,?,?)", (key, url, url, self._scope_hash()))
            self._node(key)
        return key

    def _document(self, url, stable_id, source):
        host = url_parts(url).hostname
        item_id = digest_bytes(encoded([host, stable_id]).encode())
        # Keep an existing exhausted/blocked lineage. A changed signed URL is
        # explicitly held for the existing generation-aware refresh interface.
        row = self.queue.db.execute("SELECT last_url_private FROM portal_items "
                                    "WHERE host=? AND request_id=? AND item_id=?",
                                    (host, "export-bridge", item_id)).fetchone()
        if row and row[0] != url:
            raise PortalError("explicit_link_refresh_required")
        return self.queue.inventory(host, "export-bridge", item_id, url, source)

    def _classify(self, literal):
        url = normalized(literal)
        p = urlsplit(url)
        if p.hostname in self.scope.excluded_hosts:
            return url, "excluded", "explicit_scope_exclusion"
        if p.hostname not in self.scope.hosts:
            return url, "blocked", "unapproved_origin"
        if p.path.lower().endswith(("/browse.aspx", "/browse", "/collection", "/index")):
            return url, "collection", None
        if re.fullmatch(r"/documents/[A-Za-z0-9_-]+(?:/download)?", p.path) or re.search(
                r"\.(?:pdf|txt|csv|xlsx?|docx?|pptx?|zip|tiff?|png|jpe?g)$", p.path, re.I):
            return url, "document", None
        return url, "blocked", "unclassified_link"

    def discover(self, event, bodies):
        self._require_writer()
        row = self._event(event)
        planned, count, total = [], 0, 0
        for part, text, content_type in bodies:
            total += len(text.encode("utf-8"))
            if total > 2 * 1024 * 1024 or content_type not in {"text/plain", "text/html"}:
                raise PortalError("body_discovery_bound")
            if content_type == "text/html":
                parser = BodyLinks()
                parser.feed(text)
                urls = parser.urls
            else:
                urls = re.findall(r'https?://[^\s<>"\']+', text)
            for ordinal, literal in enumerate(urls):
                count += 1
                if count > self.scope.max_links:
                    raise PortalError("body_link_bound")
                try:
                    url, kind, reason = self._classify(literal)
                except PortalError:
                    # Retain an explicit exception without storing malformed raw
                    # values that might contain userinfo/credential material.
                    url, kind, reason = "sha256:" + digest_bytes(literal.encode()), "blocked", "invalid_url"
                parsed = urlsplit(url)
                stable_path = re.sub(r"(/documents/[A-Za-z0-9_-]+)/download$", r"\1", parsed.path)
                stable_id = urlunsplit((parsed.scheme, parsed.netloc, stable_path, "", ""))
                target = self._collection(url) if kind == "collection" else self._document(
                    url, stable_id, row["eml_sha256"]) if kind == "document" else None
                planned.append((event, part, ordinal, url, kind, target, reason))
        with self.queue.lock(), self.queue.db:
            existing = [tuple(r) for r in self.queue.db.execute(
                "SELECT event,part,ordinal,url_private,kind,target,reason FROM export_body_links "
                "WHERE event=? ORDER BY part,ordinal", (event,))]
            if existing and existing != sorted(planned, key=lambda r: (r[1], r[2])):
                raise PortalError("body_discovery_conflict")
            self.queue.db.executemany("INSERT OR IGNORE INTO export_body_links VALUES(?,?,?,?,?,?,?)", planned)
            self.queue.db.execute("UPDATE export_events SET discovery='enumerated' WHERE id=?", (event,))

    def discovery_failed(self, event):
        self._require_writer()
        self._event(event)
        with self.queue.lock(), self.queue.db:
            self.queue.db.execute("UPDATE export_events SET discovery='blocked' WHERE id=?", (event,))

    def accept_page(self, collection, *, cursor, source_sha256, source_bytes, parser_sha256,
                    members, next_cursor=None, expected_total=None, event):
        """Commit one hash-bound inventory page; no live loader or network callback.

        members is a list of {id,kind,url}; kind is document or collection. A
        separately reviewed provider parser must prove denominator/pagination
        semantics. Unknown total, partial pages or exceptions never imply empty.
        Caller serializes this method under the canonical root lock.
        """
        self._require_writer()
        self._event(event)
        node = self._node(collection)
        check_hash(source_sha256)
        check_hash(parser_sha256)
        if type(source_bytes) is not int or not 1 <= source_bytes <= 25 * 1024 * 1024:
            raise PortalError("collection_page_size_bound")
        self.queue.verify_object(source_sha256, source_bytes)
        cursor = normalized(cursor)
        if node["state"] == "blocked":
            raise PortalError("collection_missing_or_blocked")
        # Collection enumeration stays same-origin. Approved cross-origin file
        # downloads still pass the existing per-hop transport authorization.
        origin = url_parts(node["url_private"]).hostname
        if origin not in self.scope.hosts or url_parts(cursor).hostname != origin:
            raise PortalError("collection_origin_mismatch")
        if next_cursor is not None:
            next_cursor = normalized(next_cursor)
            if url_parts(next_cursor).hostname != origin:
                raise PortalError("collection_origin_mismatch")
        if expected_total is not None and (type(expected_total) is not int or
                                           not 0 <= expected_total <= self.scope.max_members):
            raise PortalError("collection_member_bound")
        if type(members) is not list or len(members) > self.scope.max_members:
            raise PortalError("collection_member_bound")
        plan, ids = [], set()
        for member in members:
            _require_member_shape(member)
            key = identifier(member["id"])
            url = normalized(member["url"])
            kind = member["kind"]
            if key in ids or kind not in {"document", "collection"}:
                raise PortalError("collection_member_invalid")
            if url_parts(url).hostname not in self.scope.hosts:
                raise PortalError("unapproved_origin")
            ids.add(key)
            plan.append({"id": key, "kind": kind, "url": url})
        manifest = encoded({"members": plan, "next": next_cursor, "total": expected_total})
        pages = self.queue.db.execute("SELECT * FROM export_collection_pages WHERE collection=?", (collection,)).fetchall()
        old = next((r for r in pages if r["cursor_private"] == cursor), None)
        if old:
            if (old["source_sha256"], old["parser_sha256"], old["manifest_json"]) != (source_sha256, parser_sha256, manifest):
                raise PortalError("collection_page_conflict")
            return  # Immutable replay, including exhausted document lineages.
        if not any(item["collection"] == collection for item in self.pending_pages(event)):
            raise PortalError("collection_not_in_bounded_worklist")
        if cursor != node["cursor_private"] or node["state"] == "enumerated":
            raise PortalError("collection_cursor_conflict")
        if len(pages) >= self.scope.max_pages or next_cursor == cursor or any(
                r["cursor_private"] == next_cursor for r in pages):
            self.block(collection, "pagination_loop_or_bound")
            raise PortalError("pagination_loop_or_bound")
        if node["expected_total"] is not None and expected_total != node["expected_total"]:
            raise PortalError("collection_denominator_conflict")
        existing = {r["member"]: r for r in self.queue.db.execute(
            "SELECT * FROM export_collection_members WHERE collection=?", (collection,))}
        if len(set(existing) | ids) > self.scope.max_members:
            raise PortalError("collection_member_bound")
        for member in plan:
            old = existing.get(member["id"])
            if old and (old["kind"], old["url_private"]) != (member["kind"], member["url"]):
                raise PortalError("collection_member_conflict")
        total = len(set(existing) | ids)
        if expected_total is not None and (total > expected_total or next_cursor is None and total != expected_total):
            self.block(collection, "missing_or_conflicting_members")
            raise PortalError("collection_denominator_mismatch")
        rows = []
        for member in plan:
            target = self._collection(member["url"]) if member["kind"] == "collection" else self._document(
                member["url"], member["id"], source_sha256)
            rows.append((collection, member["id"], member["kind"], member["url"], target))
        with self.queue.lock(), self.queue.db:
            self.queue.db.executemany("INSERT OR IGNORE INTO export_collection_members VALUES(?,?,?,?,?)", rows)
            self.queue.db.execute("INSERT INTO export_collection_pages VALUES(?,?,?,?,?)",
                                  (collection, cursor, source_sha256, parser_sha256, manifest))
            state = "enumerated" if next_cursor is None and expected_total is not None else "partial"
            self.queue.db.execute("UPDATE export_collections SET state=?,cursor_private=?,expected_total=? WHERE id=?",
                                  (state, next_cursor or cursor, expected_total, collection))

    def block(self, collection, reason):
        self._require_writer()
        self._node(collection)
        if reason not in {"authorization_required", "expired_link", "unsupported_navigation",
                          "pagination_loop_or_bound", "retry_exhausted", "missing_or_conflicting_members"}:
            raise PortalError("invalid_collection_exception")
        with self.queue.lock(), self.queue.db:
            self.queue.db.execute("UPDATE export_collections SET state='blocked',reason=? WHERE id=?", (reason, collection))

    def pending_pages(self, event, limit=60):
        """Bounded local worklist, NOT permission to fetch any listed URL.

        A network adapter still needs the existing Approval/egress/transport
        checks and a qualified parser/containment binding. No automatic retry
        or URL replacement is introduced here.
        """
        self._event(event)
        if type(limit) is not int or not 1 <= limit <= 60:
            raise PortalError("invalid_inventory_bound")
        todo = [(r[0], ()) for r in self.queue.db.execute(
            "SELECT DISTINCT target FROM export_body_links WHERE event=? AND kind='collection'", (event,))]
        seen, result = set(), []
        while todo and len(seen) < self.scope.max_members and len(result) < limit:
            key, ancestors = todo.pop(0)
            if key in seen or key in ancestors or len(ancestors) >= self.scope.max_depth:
                continue
            seen.add(key)
            node = self._node(key)
            if node["state"] in {"pending", "partial"}:
                result.append({"collection": key, "url_private": node["cursor_private"],
                               "depth": len(ancestors), "denominator_known": node["expected_total"] is not None})
            capacity = max(0, self.scope.max_members - len(seen) - len(todo))
            todo.extend((r[0], (*ancestors, key)) for r in self.queue.db.execute(
                "SELECT target FROM export_collection_members WHERE collection=? AND kind='collection' LIMIT ?", (key, capacity)))
        return result

    def readiness(self, event):
        row = self._event(event)
        gaps, documents, visited = [], [], 0

        def visit(kind, target, ancestors):
            nonlocal visited
            visited += 1
            if visited > self.scope.max_members:
                if "inventory_walk_bound" not in gaps:
                    gaps.append("inventory_walk_bound")
                return
            if kind == "document":
                item = self.queue.db.execute("SELECT state,current_sha256 FROM portal_items WHERE id=?", (target,)).fetchone()
                delivered = self.queue.db.execute("SELECT 1 FROM portal_versions WHERE item=? AND sha256=? "
                                                  "AND ledger_state='delivered'", (target, item[1] if item else None)).fetchone()
                if not item or item[0] != "received" or not delivered:
                    gaps.append("document_pending_or_exception")
                else:
                    documents.append(item[1])
            elif kind == "collection":
                if target in ancestors or len(ancestors) >= self.scope.max_depth:
                    gaps.append("collection_cycle_or_depth")
                    return
                node = self._node(target)
                if node["state"] != "enumerated":
                    gaps.append("collection_membership_unknown_or_exception")
                for member in self.queue.db.execute("SELECT kind,target FROM export_collection_members WHERE collection=?", (target,)):
                    visit(member[0], member[1], (*ancestors, target))
                    if visited > self.scope.max_members:
                        break
            elif kind != "excluded":
                gaps.append("link_unresolved")

        if row["discovery"] != "enumerated":
            gaps.append("body_discovery_incomplete")
        for link in self.queue.db.execute("SELECT kind,target FROM export_body_links WHERE event=?", (event,)):
            visit(link[0], link[1], ())
            if visited > self.scope.max_members:
                break
        attached = [pair[1] for pair in json.loads(row["attachments_json"])]
        return {"event": event, "retrieval_complete": not gaps,
                "attachment_occurrences": len(attached), "linked_document_occurrences": len(documents),
                "unique_preserved_documents": len(set(attached + documents)),
                "gaps": sorted(set(gaps)), "digestion_complete": False, "publication_authorized": False}
