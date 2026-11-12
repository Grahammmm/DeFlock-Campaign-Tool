"""Organizer-recorded press coverage (DF0022-03): ``content coverage add`` / ``content coverage remove``.

``content/coverage.json`` holds only what the organizer typed: the outlet, the headline as published, the article link,
its date, an optional kind and up to three published finding ids. Nothing is fetched, copied, summarized or quoted from
the article, and listing a story is not verification of it or endorsement by the outlet. A URL identifies a row: the exact
text, no canonicalization.

Both commands need an existing real ``content/`` with a valid ``site.json``; they never scaffold, build, publish or use
the network. The whole prospective store is validated by the build's own loader before one atomic same-directory replace.
"""
import functools
import json
import os
import stat
import tempfile
from datetime import date
from pathlib import Path

from .content import (COVERAGE_ABSENT, MAX_JSON_BYTES, ContentError, _read_json, coverage_entries, load_coverage, load_finding,
                      load_site, read_coverage_document)

FILE = "coverage.json"


class CoverageError(ContentError):
    """A coverage request or store is refused; nothing was changed. The CLI exits 2."""
    exit_code = 2


def _content_dir(root):
    """``root/content`` once it is a real folder holding a valid site.json."""
    content = Path(root) / "content"
    if content.is_symlink() or not content.is_dir():
        raise CoverageError("content/ does not exist (or is a symbolic link); run content init first")
    site = content / "site.json"
    if site.is_symlink() or not site.is_file():
        raise CoverageError("content/site.json is required")
    load_site(_read_json(site), content / "public-assets")
    return content


def _published_findings(content, entries):
    """The validated published findings, read only when some entry cites one (private drafts are never opened)."""
    folder = content / "findings"
    if not any(entry["findings"] for entry in entries) or not folder.is_dir():
        return []
    return [load_finding(_read_json(path), "findings/" + path.name) for path in sorted(folder.glob("*.json"))]


def _store(root, today):
    """The current normalized entries (shape checked), or [] without a store."""
    document = read_coverage_document(root)
    return [] if document is COVERAGE_ABSENT else coverage_entries(document, today=today)


def list_coverage(root, *, today=None):
    """DF1109B-05, read only: the entries in stored order with their exact URLs, checked by the build's reader, schema and
    published-finding reference check (no site.json needed); [] when there is no file."""
    today = today or date.today()
    document = read_coverage_document(root)
    if document is COVERAGE_ABSENT:
        return []
    findings = _published_findings(Path(root) / "content", coverage_entries(document, today=today))
    return [{key: entry[key] for key in ("published_on", "outlet", "headline", "url", "kind", "findings")}
            for entry in load_coverage(document, findings, today=today)]


def _write(content, document):
    try:
        data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except UnicodeEncodeError:
        raise CoverageError("coverage.json: text is not valid UTF-8") from None
    if len(data) > MAX_JSON_BYTES:
        raise CoverageError(f"coverage.json: would exceed {MAX_JSON_BYTES} bytes; nothing was changed")
    target = content / FILE
    if content.is_symlink() or target.is_symlink():
        raise CoverageError("coverage.json: symbolic links are not written")
    mode = stat.S_IMODE(os.lstat(target).st_mode) if os.path.lexists(target) else 0o644
    try:  # RF1440-1: a refused staging allocation (read-only content/, no space, ...) is the same fixed refusal, path-free
        handle, name = tempfile.mkstemp(dir=content, prefix=".coverage-", suffix=".tmp")
    except OSError:
        raise CoverageError("coverage.json: could not be written; the previous file was kept") from None
    try:
        with os.fdopen(handle, "wb") as out:
            os.fchmod(out.fileno(), mode)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, target)
    except OSError:
        raise CoverageError("coverage.json: could not be written; the previous file was kept") from None
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def _domain_errors(function):
    """Every ``ContentError`` a coverage request raises is a ``CoverageError`` (the CLI exits 2 for it)."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CoverageError:
            raise
        except ContentError as exc:
            raise CoverageError(str(exc)) from None
    return wrapper


@_domain_errors
def add_coverage(root, *, outlet, headline, url, published_on, finding_ids=None, kind="news", today=None):
    """Append one entry; -> ``{"action": "added", "url": url}``. A URL already listed is refused (no upsert)."""
    today = today or date.today()
    content = _content_dir(root)
    entries = _store(root, today)
    row = {"outlet": outlet, "headline": headline, "url": url, "published_on": published_on,
           "findings": list(finding_ids or []), "kind": kind}
    prospective = {"schema_version": 1, "entries": [*entries, row]}
    load_coverage(prospective, _published_findings(content, prospective["entries"]), today=today)
    _write(content, prospective)
    return {"action": "added", "url": url}


@_domain_errors
def remove_coverage(root, *, url, today=None):
    """Drop the one entry whose URL is exactly ``url``; -> ``{"action": "removed", "url": url}``.

    The store's shape is checked first, then the entry is removed, then the references of the rest: an entry whose finding
    was withdrawn can be removed, but another stale reference refuses (nothing is silently dropped)."""
    today = today or date.today()
    content = _content_dir(root)
    entries = _store(root, today)
    if not isinstance(url, str) or url not in {entry["url"] for entry in entries}:
        raise CoverageError("coverage.json: URL not found")
    kept = [entry for entry in entries if entry["url"] != url]
    prospective = {"schema_version": 1, "entries": kept}
    load_coverage(prospective, _published_findings(content, kept), today=today)
    _write(content, prospective)
    return {"action": "removed", "url": url}
