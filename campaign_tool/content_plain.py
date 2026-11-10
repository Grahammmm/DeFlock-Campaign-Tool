"""DF1622-07: `content check-plain --directory D [--json]` - an advisory plain-language check of site content.

Runs the finding checker's sentence, abbreviation and paragraph rules (``plain_language.check_fields``, the same
counting and positions) over ``content/pages/*.json`` (title, body_md), ``content/faq.json`` (each entry's question,
answer_md) and ``content/glossary.json`` (each term's definition_md). It reads only those files, never builds or loads
the whole content, writes nothing and is not a gate: 0 with or without suggestions, 1 only when a checked file cannot
be read. Nothing in build, publish, review receipts or content hashes imports this module or ``plain_language``.

RF2135-2: each checked file is read through its own read-only descriptor and at most ``MAX_JSON_BYTES + 1`` bytes are
ever requested from it, so an oversized or growing file is refused at the limit instead of being read whole. The limit
is per file, not a total-process or whole-directory quota.
"""
import errno
import json
import os
import stat
from pathlib import Path

from . import plain_language
from .content import MAX_JSON_BYTES, ContentError


def _fail(label, why):
    raise ContentError(f"{label}: {why}")


def _is_real_dir(path, label):
    """True for a real folder, False when absent; a link or other non-folder is refused."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    except OSError:
        _fail(label, "could not be read")
    if not stat.S_ISDIR(mode):
        _fail(label, "must be a real folder, not a symlink or file")
    return True


def _read_bounded(fd, limit):
    """Up to ``limit`` + 1 bytes from ``fd``; each request is only the remaining allowance, so no more is ever read."""
    chunks, total = [], 0
    while total <= limit:
        chunk = os.read(fd, limit + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


NOT_REGULAR = "must be a regular file, not a symlink, folder or special file"


def _read(path, label):
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        _fail(label, "could not be read")
    if not stat.S_ISREG(mode):
        _fail(label, NOT_REGULAR)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        _fail(label, NOT_REGULAR if exc.errno == errno.ELOOP else "could not be read")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _fail(label, NOT_REGULAR)
        if info.st_size > MAX_JSON_BYTES:
            _fail(label, "file too large")
        data = _read_bounded(fd, MAX_JSON_BYTES)
    except OSError:
        _fail(label, "could not be read")
    finally:
        os.close(fd)
    if len(data) > MAX_JSON_BYTES:
        _fail(label, "file too large")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(label, f"not valid UTF-8 JSON ({exc})")
    except RecursionError:
        # The decoder, not this module, sets the nesting limit: refuse with a fixed line (no cause, path or fragment).
        raise ContentError(f"{label}: JSON nesting too deep") from None


def _string(document, key, label):
    if not isinstance(document, dict) or not isinstance(document.get(key), str):
        _fail(label, f"{key} must be a string")
    return document[key]


def _entries(document, key, label):
    if not isinstance(document, dict) or not isinstance(document.get(key), list):
        _fail(label, f"{key} must be a list")
    for index, entry in enumerate(document[key]):
        if not isinstance(entry, dict):
            _fail(f"{label} {key}[{index}]", "must be an object")
    return document[key]


def read_groups(root):
    """[(file, entry, [(field, text), ...])] in scan order, and the glossary's capital aliases.

    Every file is read and validated before this returns, so no partial report can follow a read problem."""
    base = Path(root) / "content"
    groups, terms = [], []
    if _is_real_dir(base, "content"):
        pages = base / "pages"
        if _is_real_dir(pages, "content/pages"):
            for name in sorted(os.listdir(pages)):
                if not name.endswith(".json"):
                    continue
                label = f"content/pages/{name}"
                document = _read(pages / name, label)
                groups.append((label, name[:-5], [("title", _string(document, "title", label)),
                                                  ("body_md", _string(document, "body_md", label))]))
        if os.path.lexists(base / "faq.json"):
            label = "content/faq.json"
            for index, entry in enumerate(_entries(_read(base / "faq.json", label), "entries", label)):
                where = f"{label} entries[{index}]"
                groups.append((label, f"entries[{index}]", [("question", _string(entry, "question", where)),
                                                            ("answer_md", _string(entry, "answer_md", where))]))
        if os.path.lexists(base / "glossary.json"):
            label = "content/glossary.json"
            for index, term in enumerate(_entries(_read(base / "glossary.json", label), "terms", label)):
                where = f"{label} terms[{index}]"
                aliases = term.get("aliases", [])
                if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
                    _fail(where, "aliases must be a list of strings")
                terms.append({"aliases": aliases})
                groups.append((label, f"terms[{index}]", [("definition_md", _string(term, "definition_md", where))]))
    return groups, plain_language._glossary_acronyms({"terms": terms})


def check_content(root):
    """``{"advisory": True, "warnings": [...]}``; each warning starts with ``file`` and ``entry``."""
    groups, aliases = read_groups(root)
    warnings = []
    for file, entry, items in groups:
        for row in plain_language.check_fields(items, aliases=aliases):
            warnings.append({"file": file, "entry": entry, **row})
    return {"advisory": True, "warnings": warnings}


def render_report(report):
    rows = report["warnings"]
    return [plain_language.HEADING,
            *[f"{row['file']} {row['entry']}: {plain_language.row_text(row)}" for row in rows],
            f"{len(rows)} suggestion(s). Nothing was changed."]


def run_cli(root, as_json=False):
    """Print the report once (text, or one JSON object with ``as_json``); 0 whenever the content was readable."""
    report = check_content(root)
    print(json.dumps(report, ensure_ascii=False) if as_json else "\n".join(render_report(report)))
    return 0
