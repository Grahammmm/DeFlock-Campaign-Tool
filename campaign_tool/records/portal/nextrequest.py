"""Offline NextRequest HTML adapter. Parsing is not network authorization.

Supports server-rendered request document lists: a documents container/table,
/document(s) links, an explicit total, and ordinary rel=next pagination. Unknown
SPA shells/layouts fail closed; this is not a browser or a JSON API client.
No live provider page was acquired to qualify this version.
"""
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urljoin, urlunsplit

from .notice import DOCUMENT_LINK
from .policy import PortalError, url_parts

REQUEST_PATH = re.compile(r"/requests/[A-Za-z0-9][A-Za-z0-9_-]{0,159}(?:/documents)?/?")
VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                  "link", "meta", "param", "source", "track", "wbr"})


@dataclass(frozen=True)
class NextRequestMember:
    id: str
    kind: str
    url: str
    filename: str
    released_date: str | None


class Node:
    def __init__(self, tag, attrs, parent=None):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children = []

    def walk(self):
        yield self
        if self.tag in {"script", "style", "template"}:
            return
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        if self.tag in {"script", "style", "template"}:
            return ""
        return " ".join(c.text() if isinstance(c, Node) else c for c in self.children)


class DOM(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 10000 or len(self.stack) > 100 or len(attrs) > 40:
            raise PortalError("collection_markup_bound")
        if len(dict(attrs)) != len(attrs):
            raise PortalError("unsupported_navigation")
        node = Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag == "base":
            raise PortalError("unsupported_navigation")
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if len(self.stack) == 1 or self.stack[-1].tag != tag:
            raise PortalError("unsupported_navigation")
        self.stack.pop()

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _clean_text(value):
    text = " ".join(value.split())
    if not text or len(text) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise PortalError("collection_member_invalid")
    return text


def _query(url):
    try:
        query = parse_qs(url_parts(url).query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise PortalError("collection_identity_mismatch") from None
    if set(query) - {"page"} or any(len(v) != 1 or not re.fullmatch(r"[1-9][0-9]{0,3}", v[0])
                                   for v in query.values()):
        raise PortalError("collection_identity_mismatch")
    return query


@dataclass(frozen=True)
class NextRequestProfile:
    id: str = "nextrequest-v1"

    def accepts(self, url):
        p = url_parts(url)
        if not (p.hostname or "").endswith(".nextrequest.com") or not REQUEST_PATH.fullmatch(p.path):
            return False
        try:
            _query(url)
        except PortalError:
            return False
        return True

    def parse(self, raw, url):
        # Import lazily: registry initialization in collection_pages imports us.
        from .collection_pages import MAX_PAGE_BYTES, ParsedPage
        if not self.accepts(url):
            raise PortalError("unsupported_provider")
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PAGE_BYTES:
            raise PortalError("collection_page_size_bound")
        base = url_parts(url)
        _query(url)
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeError:
            raise PortalError("inaccessible") from None
        dom = DOM()
        dom.feed(text)
        dom.close()
        if len(dom.stack) != 1:
            raise PortalError("unsupported_navigation")
        nodes = list(dom.root.walk())
        visible = " ".join(dom.root.text().lower().split())
        headings = " ".join(n.text().lower() for n in nodes
                            if n.tag in {"title", "h1", "h2", "h3"}
                            or n.attrs.get("role") == "alert")
        for n in nodes:
            action = (n.attrs.get("action") or "").lower()
            if (n.tag == "input" and (n.attrs.get("type") or "").lower() == "password"
                    or n.tag == "form" and re.search(r"(?:sign_in|login|sessions)", action)
                    or any("captcha" in k.lower() for k in n.attrs)
                    or re.search(r"captcha|challenge-platform", " ".join(
                        n.attrs.get(k) or "" for k in ("id", "class", "src")), re.I)
                    or "challenge-platform" in (n.attrs.get("src") or "")):
                raise PortalError("authorization_required")
        if re.search(r"\b((?:link|document) (?:has )?expired|expired link|document no longer available)\b", visible):
            raise PortalError("expired_link")
        if any(re.fullmatch(r"\s*(?:sign in|log in|login)\s*", n.text(), re.I)
               for n in nodes if n.tag in {"title", "h1"}):
            raise PortalError("authorization_required")
        if re.search(r"\b(sign in required|log in to (?:view|access)|verify you are human|"
                     r"checking your browser|security verification|access denied|just a moment|"
                     r"you do not have permission)\b", visible):
            raise PortalError("authorization_required")
        if re.search(r"\b(error|not found|unavailable|forbidden|something went wrong|404|500)\b", headings):
            raise PortalError("inaccessible")

        containers = [n for n in nodes if n.attrs.get("id") in {"documents", "request-documents"}]
        if len(containers) != 1:
            raise PortalError("unsupported_navigation")
        container = containers[0]
        raw_totals = [n.attrs["data-total"] for n in container.walk() if "data-total" in n.attrs]
        # Common heading spelling is also accepted, never a count guessed from links.
        raw_totals += re.findall(r"\bDocuments\s*\(([0-9]+)\)", container.text(), re.I)
        if not raw_totals or any(not re.fullmatch(r"[0-9]{1,4}", t or "") for t in raw_totals):
            raise PortalError("collection_denominator_unknown")
        totals = {int(t) for t in raw_totals}
        if len(totals) != 1 or max(totals) > 1000:
            raise PortalError("collection_denominator_unknown")
        total = totals.pop()
        members = {}
        for n in container.walk():
            if n.tag != "a" or not n.attrs.get("href"):
                continue
            literal = n.attrs["href"]
            p = url_parts(urljoin(url, literal))
            match = DOCUMENT_LINK.fullmatch(p.path)
            if not match:
                continue
            if p.hostname != base.hostname:
                raise PortalError("unapproved_origin")
            # Tokens are not retained in member output or inventory manifests.
            safe_url = urlunsplit(("https", p.hostname, p.path, "", ""))
            filename = _clean_text(n.attrs.get("download") or n.text())
            row = n.parent
            while row is not None and row is not container and row.tag not in {"tr", "li", "article"}:
                row = row.parent
            dates = [t.attrs.get("datetime") for t in row.walk() if t.tag == "time"] if row is not None and row is not container else []
            released = None
            if dates:
                if len(dates) != 1 or not dates[0] or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dates[0]):
                    raise PortalError("collection_member_invalid")
                try:
                    released = date.fromisoformat(dates[0]).isoformat()
                except ValueError:
                    raise PortalError("collection_member_invalid") from None
            member = NextRequestMember(match[1], "document", safe_url, filename, released)
            if member.id in members and members[member.id] != member:
                raise PortalError("collection_member_conflict")
            members[member.id] = member
            if len(members) > 1000:
                raise PortalError("collection_member_bound")

        next_links = [n for n in nodes if n.tag in {"a", "link"}
                      and "next" in (n.attrs.get("rel") or "").split()]
        next_url = None
        if next_links:
            if len(next_links) != 1 or not next_links[0].attrs.get("href"):
                raise PortalError("collection_pagination_unknown")
            candidate = urljoin(url, next_links[0].attrs["href"])
            p = url_parts(candidate)
            query = _query(candidate)
            if p.hostname != base.hostname or p.path != base.path or "page" not in query:
                raise PortalError("collection_identity_mismatch")
            if int(query["page"][0]) != int(_query(url).get("page", ["1"])[0]) + 1:
                raise PortalError("pagination_loop_or_bound")
            next_url = urlunsplit(("https", p.hostname, p.path, p.query, ""))
        if total == 0 and (members or next_url is not None or not re.search(
                r"\bno (?:released )?documents\b", container.text(), re.I)):
            raise PortalError("collection_denominator_unknown")
        return ParsedPage(tuple(members.values()), next_url, total)
