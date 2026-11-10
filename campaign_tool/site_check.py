"""Internal link check for a generated ``public/`` tree (``build --check``).

Every site-local ``href``/``src`` in every written ``*.html`` page must name a
regular file under ``public/`` and every ``#fragment`` on an HTML target must
match an ``id`` (or ``a[name]``) on that page. URLs with a scheme or a ``//``
prefix are external and are never fetched. Stdlib only.
"""
import json
import os
import posixpath
import re
import stat
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


ASSET_RELS = {"stylesheet", "preload", "modulepreload"}


class _AssetParser(HTMLParser):
    """Site-local subresource references in markup: stylesheets, preloads, scripts, img/source ``src`` and every
    img/source ``srcset`` candidate. A conservative inventory of references, not what one visit downloads; see
    ``docs/SITE-WEIGHTS.md``."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = {name: value for name, value in attrs if value is not None}
        if tag == "link" and ASSET_RELS & set(attrs.get("rel", "").lower().split()):
            self.urls.append(attrs.get("href"))
        elif tag in ("script", "img", "source"):
            self.urls.append(attrs.get("src"))
            if tag != "script":  # responsive candidates: the shared tokenizer, never a second one
                self.urls.extend(_srcset_urls(attrs.get("srcset", "")))

    handle_startendtag = handle_starttag


def page_weights(public: Path) -> list:
    """Per-page first-party weight of ``public/``: on-disk (uncompressed) bytes of each page plus its assets.

    One dict per ``*.html`` page, sorted by page: ``{"page", "html_bytes", "assets": [(path, bytes)], "total"}``.
    Each asset counts once per page (deduplicated by normalized path across src, srcset, source, script and
    preload references); external, missing, escaping and symlinked targets are skipped (``--check`` owns broken
    links) and never fetched. ``fetch()``/XHR targets inside scripts are not seen: only markup counts.
    Every img/source ``srcset`` candidate counts, including picture alternatives a browser would not pick: a
    conservative inventory, not a viewport-selected download estimate (``docs/SITE-WEIGHTS.md``).
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


WEIGHTS_SCHEMA = "page-weights-v1"
WEIGHTS_MAX_BYTES = 2 * 1024 * 1024


def weights_record(rows, budget):
    """The private ``page-weights-v1`` record of ``page_weights`` rows; no timestamp, so equal builds match."""
    return {"schema": WEIGHTS_SCHEMA, "budget_kb": budget,
            "pages": [{"page": r["page"], "html_bytes": r["html_bytes"], "total": r["total"],
                       "assets": [[p, n] for p, n in r["assets"]]} for r in rows]}


def read_weights(path):
    """A ``page-weights-v1`` baseline as a dict; anything else raises ``ValueError`` naming the file."""
    label = f"--weights-baseline {path}"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError(f"{label}: cannot be read as a regular file, not a symlink ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > WEIGHTS_MAX_BYTES:
            raise ValueError(f"{label}: must be a regular file of at most {WEIGHTS_MAX_BYTES} bytes")
        raw = handle.read(WEIGHTS_MAX_BYTES + 1)
        if len(raw) > WEIGHTS_MAX_BYTES:  # the file grew after the descriptor size check: refuse, never use a prefix
            raise ValueError(f"{label}: must be a regular file of at most {WEIGHTS_MAX_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:  # includes UnicodeDecodeError
        raise ValueError(f"{label}: not valid JSON") from None
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("schema") != WEIGHTS_SCHEMA or not isinstance(pages, list):
        raise ValueError(f"{label}: not a {WEIGHTS_SCHEMA} record")
    seen = set()
    for item in pages:
        page, total = item.get("page") if isinstance(item, dict) else None, item.get("total") if isinstance(item, dict) else None
        if (not isinstance(page, str) or not page or not page.isprintable() or page in seen
                or type(total) is not int or total < 0):
            raise ValueError(f"{label}: malformed or duplicate page entry")
        seen.add(page)
    return data


def weight_changes(rows, base):
    """Output lines comparing ``page_weights`` rows with a ``read_weights`` baseline (rows first, then removals)."""
    before = {item["page"]: item["total"] for item in base["pages"]}
    lines = []
    for row in rows:
        old = before.get(row["page"])
        change = "new page" if old is None else ("±0" if row["total"] == old else f"{row['total'] - old:+d}") + " since baseline"
        lines.append(f"  {row['page']} {row['total']} bytes ({change})")
    now = {row["page"] for row in rows}
    return lines + [f"removed since baseline: {page}" for page in before if page not in now]


def write_weights(root, out, record):
    """Write the record to a new private file (0600, ``O_EXCL``); refused inside ``public/`` or ``content/``."""
    out = Path(out)
    parent = out.parent.resolve()
    if not parent.is_dir():
        raise ValueError(f"--weights-out {out}: folder {out.parent} does not exist")
    target = parent / out.name
    root = Path(root)
    for folder in ("public", "content"):
        for base in {(root / folder).resolve(), root.resolve() / folder}:
            if target == base or target.is_relative_to(base):
                raise ValueError(f"--weights-out {out}: must not be inside the campaign's {folder}/ folder; "
                                 "this file is private")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise ValueError(f"--weights-out {out}: exists; choose a new file name") from None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(json.dumps(record, indent=2) + "\n")
    except BaseException:
        if fd is not None:
            os.close(fd)
        os.unlink(target)  # the file this call created with O_EXCL
        raise
    return target


def check_data_catalogue(public):
    """DF2207-12: raise ``ContentError`` with sorted ``path: reason`` diagnostics when ``data.html`` or
    ``data/datapackage.json`` is missing, malformed or disagrees with a listed file's current size or SHA-256."""
    from .site_data_catalogue import check_data_catalogue as check
    check(public)
