"""``content/redirects.json`` -> ``public/_redirects`` (DF2037-16): organizer-listed old paths to built pages.

Each line is ``<from> <to> 301``; 301 is the only status (no 302 or 200 rewrites, no splats or placeholders). Build
and content-check share these rules; every refusal starts ``redirects.json:``, names its row and is one line.
"""
import re
from pathlib import Path

from .content import SLUG, ContentError, _read_json

MAX_ROWS, MAX_FROM = 500, 300
FROM = re.compile(r"/[A-Za-z0-9/._~%-]*")  # ASCII path characters only: no ?, #, *, :, space or non-ASCII
FINDING = re.compile(r"/findings/([^/]+)\.html")
OPEN_FOLDERS = {"findings", "meetings"}  # generated folders whose unwritten paths may be redirected (renamed slugs)
# RF1405-2: a retired agency page (renamed slug) may be redirected too, but only as exactly /agencies/<slug>.html; the
# folder stays closed (not in OPEN_FOLDERS) so unwritten calendars, assets and subfolders are still refused, and
# ``_key(source) in owned`` still refuses every path this build writes, including live agency pages.
AGENCY_PAGE = re.compile(r"/agencies/(" + SLUG.pattern + r")\.html")


def _key(path):
    """Comparison form: a trailing slash is dropped, so ``/a`` and ``/a/`` are the same source."""
    return path.rstrip("/") or "/"


def _safe(text):
    """Diagnostic text only (RF1025-2): printable characters stay verbatim; every other one (line breaks, terminal
    controls, bidi marks, lone surrogates) becomes its visible ASCII escape, so a malformed ``to`` stays one
    terminal-safe, UTF-8-encodable problem line. Never applied to values the build compares or writes."""
    return "".join(c if c.isprintable() else ascii(c)[1:-1] for c in text)


def _refuse(problems):
    if problems:
        raise ContentError("\n".join(dict.fromkeys("redirects.json: " + _safe(line) for line in problems)))


def redirect_rows(document):
    """Shape and per-row syntax: ``[(from, to), ...]`` in file order, or ContentError listing every problem."""
    if not isinstance(document, dict) or set(document) != {"schema_version", "redirects"}:
        _refuse(['expected exactly {"schema_version": 1, "redirects": [...]} (unknown keys are refused)'])
    if document["schema_version"] != 1:
        _refuse(["schema_version must be 1"])
    if not isinstance(document["redirects"], list) or len(document["redirects"]) > MAX_ROWS:
        _refuse([f"redirects must be a list of at most {MAX_ROWS} rows"])
    problems, rows = [], []
    for n, row in enumerate(document["redirects"]):
        if not isinstance(row, dict) or set(row) != {"from", "to"}:
            problems.append(f"redirects[{n}]: keys must be exactly from and to (unknown keys are refused)")
        elif not isinstance(row["from"], str) or not isinstance(row["to"], str):
            problems.append(f"redirects[{n}]: from and to must be strings")
        elif (not FROM.fullmatch(row["from"]) or len(row["from"]) > MAX_FROM or "//" in row["from"]
              or ".." in row["from"].split("/")):
            problems.append(f"redirects[{n}]: from {row['from']!r} must be an ASCII path starting with / of at most "
                            f"{MAX_FROM} characters, without .. segments, //, ?, #, *, : or spaces")
        elif "." in row["from"].split("/"):
            # RF1420-4: a literal single-dot segment is another spelling of the path without it, which the textual
            # owned-page and withdrawn-finding checks would miss. Refused, never normalized, before those checks.
            problems.append(f"redirects[{n}]: from {row['from']!r} must not contain . path segments")
        else:
            rows.append((n, row["from"], row["to"]))
    _refuse(problems)
    return rows


def _owned(written):
    """Comparison keys of every path the build writes, its extensionless form, folder form and parent folders."""
    owned = set()
    for name in written:
        parts = name.split("/")
        owned.update(_key("/" + "/".join(parts[:i])) for i in range(1, len(parts)))
        owned.add(_key("/" + name))
        if name.endswith(".html"):
            owned.add(_key("/" + name[:-len(".html")]))
        if name.endswith("index.html"):
            owned.add(_key("/" + name[:-len("index.html")]))
    return owned


def load_redirects(document, built_paths, withdrawn_slugs):
    """``_redirects`` text (sorted ``<from> <to> 301`` lines, "" without rows) for ``document`` checked against
    ``built_paths`` (every file this build writes, relative to public/) and the withdrawn finding slugs."""
    from .site import GENERATED_TOP_LEVEL  # local: site imports this module
    rows, built_paths = redirect_rows(document), list(built_paths)
    pages = {"/" + (name[:-len("index.html")] if name.endswith("index.html") else name)
             for name in built_paths if name.endswith(".html") and name != "404.html"}
    owned, sources, problems = _owned(built_paths), {}, []
    for n, source, target in rows:
        if _key(source) in sources:
            problems.append(f"redirects[{n}]: from {source} repeats redirects[{sources[_key(source)][0]}]")
        sources.setdefault(_key(source), (n, source, target))
    for n, source, target in rows:
        top = source.split("/")[1]
        if _key(source) in owned or (top in GENERATED_TOP_LEVEL and top not in OPEN_FOLDERS
                                          and not AGENCY_PAGE.fullmatch(source)):
            problems.append(f"redirects[{n}]: from {source} is a page this build writes")
        if _key(target) in sources:
            m, next_source, onward = sources[_key(target)]
            if m == n:
                problems.append(f"redirects[{n}]: from {source} points at itself (a loop)")
            elif _key(onward) == _key(source):
                pair = sorted([(n, source), (m, next_source)])
                problems.append(f"redirects[{pair[0][0]}] and redirects[{pair[1][0]}]: a loop between {pair[0][1]} "
                                f"and {pair[1][1]}; loops are refused")
            else:
                problems.append(f"redirects[{n}] -> redirects[{m}]: a chain ({source} -> {target} -> {onward}); "
                                f"point redirects[{n}] at the final page")
        elif target.startswith("//") or not target.startswith("/"):
            problems.append(f"redirects[{n}]: to {target!r} is external; only pages this build writes are targets")
        elif target not in pages:
            problems.append(f"redirects[{n}]: to {target} is not a page this build writes")
        withdrawn, other = FINDING.fullmatch(_key(source)), FINDING.fullmatch(target)
        if withdrawn and withdrawn.group(1) in withdrawn_slugs and other:
            problems.append(f"redirects[{n}]: from {source}: a withdrawn finding must not point at a different "
                            "finding (use /findings/ or /corrections.html)")
    _refuse(problems)
    return "".join(f"{source} {target} 301\n" for _, source, target in sorted(rows, key=lambda r: r[1:]))


def check_file(root, problems):
    """content-check: list ``content/redirects.json`` refusals with build's own text. The page-set rules need the
    build's plan, so they run (read-only, nothing written) once every other content problem is clear."""
    path = Path(root) / "content" / "redirects.json"
    if not (path.is_file() or path.is_symlink()):
        return
    from .site import _plan_site  # local: site imports this module
    try:
        if redirect_rows(_read_json(path)) and not problems:
            _plan_site(root)
    except ValueError as exc:  # ContentError, or read_config's plain ValueError
        problems.extend(str(exc).splitlines())
