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
            if not _external_or_empty(href) and resolve(public, href, page, ids_of):
                broken.add((page, href))
    return sorted(broken)


def resolve(public, href, page="index.html", ids_of=None):
    """``None`` when a site-local ``href`` on ``page`` reaches a file (and id) in ``public/``, else the reason."""
    public = Path(public)
    target, _, fragment = href.strip().partition("#")
    path = target.split("?", 1)[0]
    rel = page if path == "" else _resolve(public, page, unquote(path))
    if rel is None:
        return "no_page"
    fragment = unquote(fragment)
    try:
        if fragment and rel.endswith((".html", ".htm")) and fragment not in (ids_of or (lambda r: _parse(public / r).ids))(rel):
            return "no_fragment"
    except (OSError, ValueError):
        return "no_fragment"
    return None


ASSET_RELS = {"stylesheet", "preload", "modulepreload"}


class _AssetParser(HTMLParser):
    """Site-local subresources a first visit downloads: stylesheets, preloads, scripts, img/source src."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = {name: value for name, value in attrs if value is not None}
        if tag == "link" and ASSET_RELS & set(attrs.get("rel", "").lower().split()):
            self.urls.append(attrs.get("href"))
        elif tag in ("script", "img", "source"):
            self.urls.append(attrs.get("src"))

    handle_startendtag = handle_starttag


def page_weights(public: Path) -> list:
    """Per-page first-party weight of ``public/``: on-disk (uncompressed) bytes of each page plus its assets.

    One dict per ``*.html`` page, sorted by page: ``{"page", "html_bytes", "assets": [(path, bytes)], "total"}``.
    Each asset counts once per page; external, missing, escaping and symlinked targets are skipped (``--check``
    owns broken links) and never fetched. ``fetch()``/XHR targets inside scripts are not seen: only markup counts.
    """
    public = Path(public)
    rows = []
    for path in sorted(public.rglob("*.html")):
        if not path.is_file() or path.is_symlink():
            continue
        page = path.relative_to(public).as_posix()
        parser = _AssetParser()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        parser.close()
        assets = {}
        for url in parser.urls:
            if url is None or _external_or_empty(url):
                continue
            target = url.strip().partition("#")[0].split("?", 1)[0]
            rel = _resolve(public, page, unquote(target)) if target else None
            if rel is not None and rel != page and rel not in assets:
                assets[rel] = (public / rel).stat().st_size
        html_bytes = path.stat().st_size
        rows.append({"page": page, "html_bytes": html_bytes, "assets": sorted(assets.items()),
                     "total": html_bytes + sum(assets.values())})
    return sorted(rows, key=lambda row: row["page"])
