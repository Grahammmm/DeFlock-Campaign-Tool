"""finding new --search-log: a private draft skeleton from a verified kit-search-log summary (DF1107-03).

The summary (kit-search-log --json) is never trusted: its input hash must be one library entry of the named agency,
ingested in the ledger with its object in private/objects, and a fresh read of that stored record must reproduce every
count. Only recomputed aggregates enter the draft; no purpose text, user name, case number, plate, row, path or other
summary field is copied. The draft is written only by findings._write_draft, after every check. Nothing is published.
Every error is fixed text: never a value from the summary or the log.
"""
import json
import re
from pathlib import Path

from . import content, search_log
from .evidence import PREFIX, _ledger_hashes, _object_present
from .findings import EDIT, _draft_slug, _skeleton, _text, _write_draft
from .privacy_patterns import categories as privacy_categories
from .sources_cmd import _known_agencies, _library

MAX_SUMMARY_BYTES = 4 * 1024 * 1024
MAX_LABEL = 120
TOP_ORGS = 10
FULL_SHA = re.compile(r"[0-9a-f]{64}")
PURPOSE_LABELS = {"blank_or_na": "Purpose blank or N/A", "immigration_related": "Purpose mentions immigration terms",
                  "vague": "Purpose vague (for example 'investigation')", "other": "Purpose other"}
ROLES = ("time", "reason", "org", "state")


def _no_constant(_name):
    raise ValueError("summary is not valid JSON")


def _pairs(items):
    keys = [key for key, _ in items]
    if len(set(keys)) != len(keys):
        raise ValueError("summary has a duplicate key")
    return dict(items)


def _load_summary(path):
    """The summary object. A filesystem OSError after the path was named (admission fstat/fdopen, read, close) becomes
    one fixed ValueError, so the caller never prints the path or an OS message; existing fixed ValueErrors pass through."""
    try:
        handle, _ = search_log._open_regular(path, "--search-log file", MAX_SUMMARY_BYTES)
        with handle:
            data = handle.read(MAX_SUMMARY_BYTES + 1)
    except OSError:
        raise ValueError("--search-log file could not be read") from None
    if len(data) > MAX_SUMMARY_BYTES:
        raise ValueError("--search-log file is larger than 4 MiB")
    try:
        summary = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise ValueError("--search-log file is not valid UTF-8 JSON") from None
    if not isinstance(summary, dict):
        raise ValueError("--search-log file must hold one kit-search-log --json object")
    return summary


def _structure(summary, agency_id):
    """-> (full sha256, column header per role); the rest of the summary is compared whole against the fresh read."""
    sha = columns = custom = None
    try:
        source = summary["input"]
        sha, columns, custom = source["sha256"], source["columns"], source["categories_sha256"]
        ok = (type(summary["schema_version"]) is int and summary["schema_version"] == 1
              and isinstance(sha, str) and FULL_SHA.fullmatch(sha)
              and isinstance(columns, dict) and set(columns) == set(ROLES)
              and all(columns[role] is None or isinstance(columns[role], str) for role in ROLES))
    except (KeyError, TypeError):
        ok = False
    if not ok:
        raise ValueError("summary is not a version 1 kit-search-log --json object")
    if custom is not None:
        raise ValueError("summary used custom categories, which a summary cannot replay; "
                         "rerun kit-search-log without --categories")
    if summary.get("agency_id") != agency_id:
        raise ValueError("summary is for a different agency than --agency")
    return sha, columns


def _verified_object(root, agency_id, sha):
    """The stored record's path after the library, ledger and object checks; reads, never writes."""
    private = root / "private"
    objects, ledger = private / "objects", private / "ledger.sqlite"
    if private.is_symlink() or objects.is_symlink() or ledger.is_symlink():
        raise ValueError("private/, private/objects and private/ledger.sqlite must not be symlinks")
    entries = content.load_sources(_library(root)[1])
    same = [entry for entry in entries if entry["sha256"] == sha]
    if not same:
        raise ValueError("the summary's input is not registered in content/sources.json")
    if any(entry["agency_id"] != agency_id for entry in same):
        raise ValueError("the summary's input is registered for a different agency")
    if len(same) != 1:
        raise ValueError(f"the summary's input {sha[:PREFIX]} has several library entries; keep one")
    if any(entry["sha256"] != sha and entry["sha256"].startswith(sha[:PREFIX]) for entry in entries):
        raise ValueError(f"the 12-hex prefix {sha[:PREFIX]} does not identify one library record")
    if not ledger.is_file() or sha not in _ledger_hashes(ledger):
        raise ValueError(f"record {sha[:PREFIX]} is not ingested in the ledger")
    if not _object_present(objects, sha):
        raise ValueError(f"record {sha[:PREFIX]} has no stored object in private/objects")
    return objects / sha


def _same_json(left, right):
    """Equal values AND exact JSON types at every node (bool, int and float never alias); an explicit stack, no recursion."""
    stack = [(left, right)]
    while stack:
        a, b = stack.pop()
        if type(a) is not type(b):
            return False
        if type(a) is dict:
            if a.keys() != b.keys():
                return False
            stack.extend((value, b[key]) for key, value in a.items())
        elif type(a) is list:
            if len(a) != len(b):
                return False
            stack.extend(zip(a, b))
        elif a != b or (type(a) is float and repr(a) != repr(b)):
            return False
    return True


def _verify(root, agency, sha, columns, summary):
    path = _verified_object(root, agency["agency_id"], sha)
    fresh = search_log.summarize_csv(path, agency, columns=columns)
    if fresh["input"]["sha256"] != sha:
        raise ValueError("the stored record's bytes no longer match its sha256")
    fresh, claimed = dict(fresh, agency_name=None), dict(summary, agency_name=None)
    if not _same_json(fresh, claimed):
        raise ValueError("the summary's counts no longer match a fresh read of the stored record")
    return fresh


def _cell(value, field):
    if not isinstance(value, str) or len(value) > MAX_LABEL:
        raise ValueError(f"{field}: label is too long for a draft table")
    text = "".join(" " if ch.isspace() else ch for ch in value).strip()
    text = text.replace("<", "\u2039").replace(">", "\u203a").replace("[", "(").replace("]", ")")
    for ch in "\\|`*_#!~&":
        text = text.replace(ch, "\\" + ch)
    return text


def _body(summary, prefix):
    rows, period, columns = summary["rows"], summary["period"], summary["input"]["columns"]
    dated = f"{period['first_date']} to {period['last_date']}" if period["first_date"] else "no dated searches"
    lines = [f"## Search log counts, record {prefix}", "", search_log.WARNING + ".", "",
             f"Counted from the record cited as Search log {prefix}. The period is the range of dates stated in that "
             "log, not the agency's whole history. Purpose text is classified and dropped; no names, case numbers or "
             "plates are shown.", "",
             "| Measure | Value |", "| --- | --- |", f"| Period (dates as stated in the log) | {dated} |",
             f"| Total searches counted | {rows['counted']} |", f"| Rows read | {rows['read']} |",
             f"| Rows skipped, not counted | {sum(rows['skipped'].values())} |"]
    lines += [f"| {PURPOSE_LABELS[name]} | {summary['purposes']['counts'][name]} |" for name in search_log.PURPOSES]
    lines += ["", "| Searching organization | State | Searches |", "| --- | --- | --- |"]
    if columns["org"] is None:
        lines.append(f"| organization column not available | n/a | {summary['organizations']['unattributed']} unattributed |")
    else:
        top = sorted(summary["organizations"]["rows"], key=lambda r: (-r["count"], r["name"], r["state"] or ""))
        lines += [f"| {_cell(r['name'], 'organization')} | {_cell(r['state'], 'state') if r['state'] else 'not given'}"
                  f" | {r['count']} |" for r in top[:TOP_ORGS]]
        if not top:
            lines.append("| none counted | n/a | 0 |")
    lines += ["", EDIT + "what these counts do and do not show: interpretation and limitations]"]
    return "\n".join(lines) + "\n"


def draft_from_summary(root, agency_id, summary_path, author=None):
    """Write a private finding draft from a verified summary; returns {"id", "written", "source"}."""
    root = Path(root)
    agencies = [a for a in _known_agencies(root) if a["agency_id"] == agency_id]
    if len(agencies) != 1:
        raise ValueError(f"--agency {agency_id!r} is not one of the campaign's agencies")
    author = EDIT + "who wrote this finding]" if author is None else _text("author", author, 120)
    summary = _load_summary(summary_path)
    sha, columns = _structure(summary, agency_id)
    fresh = _verify(root, agencies[0], sha, columns, summary)
    prefix = sha[:PREFIX]
    body = _body(fresh, prefix)
    hit = privacy_categories(body)
    if hit:
        raise ValueError("privacy lint: draft table: " + ", ".join(hit))
    title = EDIT + f"title] (search log {prefix})"
    draft = _skeleton(title, _draft_slug(title), agency_id, author,
                      [{"sha256": sha, "title": f"Search log {prefix}", "locator": EDIT + "page or section]"}])
    draft["body_md"] = body
    return {**_write_draft(root, draft), "source": prefix}
