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
