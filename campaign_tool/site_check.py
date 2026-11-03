"""Internal link check for a generated ``public/`` tree (``build --check``).

Every site-local ``href``/``src`` in every written ``*.html`` page must name a
regular file under ``public/`` and every ``#fragment`` on an HTML target must
match an ``id`` (or ``a[name]``) on that page. URLs with a scheme or a ``//``
prefix are external and are never fetched. Stdlib only.
"""
import posixpath
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

LINK_ATTRS = {"a": ("href",), "link": ("href",), "script": ("src",), "img": ("src", "srcset"),
              "source": ("src", "srcset")}
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _srcset_urls(value):
    """Candidate URLs of a srcset, per the HTML parsing rules: a URL keeps its own commas (data: URLs)."""
    urls, i, n = [], 0, len(value)
    while i < n:
        while i < n and (value[i].isspace() or value[i] == ","):
            i += 1
        start = i
        while i < n and not value[i].isspace():
            i += 1
        url = value[start:i]
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            depth = 0
            while i < n and (value[i] != "," or depth):
                depth += {"(": 1, ")": -1 if depth else 0}.get(value[i], 0)
                i += 1
        if url:
            urls.append(url)
    return urls


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value is None:
                continue
            if name == "id" or (tag == "a" and name == "name"):
                self.ids.add(value)
            if name in LINK_ATTRS.get(tag, ()):
                if name == "srcset":
                    self.links.extend(_srcset_urls(value))
                else:
                    self.links.append(value)

    handle_startendtag = handle_starttag


def _parse(path):
    parser = _PageParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    parser.close()
    return parser


def _external_or_empty(href):
    href = href.strip()
    return not href or href == "#" or href.startswith("//") or bool(SCHEME.match(href))


def _resolve(public, page, path):
    """Posix path of the target relative to public/, or None when it escapes or is not a plain file."""
    directory = path.endswith("/") or path == ""
    base = path.lstrip("/") if path.startswith("/") else posixpath.join(posixpath.dirname(page), path)
    rel = posixpath.normpath(base) if base else "."
    if rel == ".." or rel.startswith("../") or rel.startswith("/"):
        return None
    if directory:
        rel = "index.html" if rel == "." else rel + "/index.html"
    elif rel == ".":
        return None
    current = public
    try:
        for part in rel.split("/"):
            current = current / part
            if current.is_symlink():
                return None
        return rel if current.is_file() else None
    except (OSError, ValueError):
        return None


def check_links(public: Path) -> list:
    """Sorted, de-duplicated ``(page, href)`` pairs for every broken site-local link in ``public/``."""
    public = Path(public)
    pages = {}
    for path in sorted(public.rglob("*.html")):
        if path.is_file() and not path.is_symlink():
            pages[path.relative_to(public).as_posix()] = path
    parsed = {}

    def ids_of(rel):
        if rel not in parsed:
            parsed[rel] = _parse(public / rel)
        return parsed[rel].ids

    broken = set()
    for page in pages:
        ids_of(page)
        for href in parsed[page].links:
            if _external_or_empty(href):
                continue
            target, _, fragment = href.strip().partition("#")
            path = target.split("?", 1)[0]
            rel = page if path == "" else _resolve(public, page, unquote(path))
            if rel is None:
                broken.add((page, href))
                continue
            fragment = unquote(fragment)
            if fragment and rel.endswith((".html", ".htm")):
                try:
                    if fragment not in ids_of(rel):
                        broken.add((page, href))
                except (OSError, ValueError):
                    broken.add((page, href))
    return sorted(broken)


HEADINGS = {f"h{n}": n for n in range(1, 7)}


class _A11yParser(HTMLParser):
    """Collects the five mechanical accessibility pattern hits of one page (a lint, not an audit)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hits = set()
        self.ids = {}
        self.lang = None  # None: no <html> start tag seen
        self.heading = None
        self.link = None  # [href, text parts] of the open <a href>, if any

    def _close_link(self):
        if self.link is not None and not " ".join("".join(self.link[1]).split()):
            self.hits.add(("link-text", self.link[0]))
        self.link = None

    def handle_starttag(self, tag, attrs):
        values = {}
        for name, value in attrs:
            values.setdefault(name, value)
        if values.get("id"):
            self.ids[values["id"]] = self.ids.get(values["id"], 0) + 1
        if tag == "html" and self.lang is None:
            self.lang = (values.get("lang") or "").strip()
        elif tag in HEADINGS:
            level = HEADINGS[tag]
            if self.heading is not None and level > self.heading + 1:
                self.hits.add(("heading-skip", f"h{self.heading}\u2192h{level}"))
            self.heading = level
        elif tag == "img":
            if "alt" not in values:
                src = (values.get("src") or "").strip()
                external = src.startswith("//") or SCHEME.match(src)
                self.hits.add(("img-alt", "external" if external else (src.split("#", 1)[0].split("?", 1)[0] or "-")))
            elif self.link is not None:
                self.link[1].append(" " + (values["alt"] or ""))
        elif tag == "a":
            self._close_link()  # a new <a> implicitly ends an unclosed one
            label = " ".join(((values.get("aria-label") or "") + " " + (values.get("title") or "")).split())
            if values.get("href") is not None and not label:
                self.link = [values["href"], []]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "a":
            self._close_link()

    def handle_data(self, data):
        if self.link is not None:
            self.link[1].append(data)

    def close(self):
        super().close()
        self._close_link()
        if not self.lang:
            self.hits.add(("html-lang", "-"))
        self.hits.update(("duplicate-id", value) for value, count in self.ids.items() if count > 1)


def check_accessibility(public: Path) -> list:
    """Sorted ``(page, code, detail)`` accessibility pattern hits over every ``*.html`` in ``public/``.

    Codes: ``img-alt`` (no ``alt``; ``alt=""`` is decorative and allowed), ``heading-skip``, ``link-text``,
    ``duplicate-id`` and ``html-lang``. A pattern lint, not an accessibility audit.
    """
    public = Path(public)
    hits = set()
    for path in sorted(public.rglob("*.html")):
        if path.is_file() and not path.is_symlink():
            parser = _A11yParser()
            parser.feed(path.read_text(encoding="utf-8", errors="replace"))
            parser.close()
            page = path.relative_to(public).as_posix()
            hits.update((page, code, detail) for code, detail in parser.hits)
    return sorted(hits)
