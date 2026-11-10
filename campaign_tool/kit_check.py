"""kit-check: what each selected law-enforcement agency's request draft still needs before it is sent (read-only).

A draft needs attention when it still has a bracket placeholder such as ``[explicit approved cap, e.g. $50]``
(a Markdown link ``[text](url)`` is not one), when kit/agencies.json does not mark its agency ``verified: true``,
or when the draft is missing or a symlink. It also needs attention when kit kept the organizer's edited draft and
kit/requests.suggested/<id>.md holds a different fresh draft (the row names the structural differences).
Nothing is written and nothing is sent.
"""
import json
import os
import re
import shlex
import stat
import sys
from pathlib import Path

from . import kit

PLACEHOLDER = re.compile(r"\[[^\]\n]{1,160}\](?!\()")
HIT_CHARS, HITS_MAX = 160, 50
MAX_DRAFT_BYTES = 1 << 20
MISSING = "draft missing or not a regular file"
SUGGESTED_DIR = "requests.suggested"
STATUS_MARK, RANGE_MARK = "Law package status:", "The date range for this request is "
CONTEXT_HEADING = "## Statutory context (for the requester, not for sending)"
CITATION = re.compile(r"- [^:\s]+: ")
TEXT_ITEMS_MAX = 10
NOT_REGULAR = "suggestion is not a regular file"
UNREADABLE = "suggestion unreadable"


def draft_problems(text):
    """Every unfilled bracket placeholder in ``text`` as {"line", "placeholder"} (1-based line numbers)."""
    return [{"line": number, "placeholder": match.group(0)[:HIT_CHARS]}
            for number, line in enumerate(text.split("\n"), 1) for match in PLACEHOLDER.finditer(line)]


def _read_regular(path):
    """Admitted regular-file bytes, or (None, absent|unsafe|unreadable|too_large)."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None, "unsafe"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        return None, "unsafe" if exc.errno in (40, 21, 6) else "unreadable"  # ELOOP, EISDIR, ENXIO
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "unsafe"
        if info.st_size > MAX_DRAFT_BYTES:
            return None, "too_large"
        chunks = []
        size = 0
        while True:
            chunk = os.read(fd, min(1 << 16, MAX_DRAFT_BYTES + 1 - size))
            if not chunk:
                return b"".join(chunks), None
            size += len(chunk)
            if size > MAX_DRAFT_BYTES:
                return None, "too_large"
            chunks.append(chunk)
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)


def _read_suggestion(root, agency_id):
    """(bytes, None) for a readable suggestion, (None, None) when there is none, (None, error) when unsafe."""
    directory = Path(root) / "kit" / SUGGESTED_DIR
    try:
        mode = os.lstat(directory).st_mode
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, UNREADABLE
    if not stat.S_ISDIR(mode):
        return None, NOT_REGULAR
    data, why = _read_regular(directory / (agency_id + ".md"))
    if why == "absent":
        return None, None
    return data, {"unsafe": NOT_REGULAR, "unreadable": UNREADABLE,
                  "too_large": "suggestion exceeds 1 MiB"}.get(why)


def _lines(data):
    return [line.rstrip("\r") for line in data.decode("utf-8", errors="replace").split("\n")]


def _unique(items):
    return list(dict.fromkeys(items))


def _summary(data):
    """Scope headings, date-range, fee-cap and law-status line groups and rule citation lines of one draft."""
    lines = _lines(data)
    scopes = _unique(line for line in lines if line.startswith("## ") and line != CONTEXT_HEADING)
    date = [line for line in lines if line.startswith(RANGE_MARK)] or [line for line in lines
                                                                       if line.startswith("Subject:")]
    fee = [line for line in lines if "Please advise" in line or "before incurring fees beyond" in line]
    status = []
    for index, line in enumerate(lines):
        if STATUS_MARK in line:
            status.append(line)
            while not status[-1].rstrip().endswith(".") and index + 1 < len(lines) and lines[index + 1].strip():
                index += 1
                status.append(lines[index])
        elif line.startswith("NO REVIEWED LAW PACKAGE:"):
            while index < len(lines) and lines[index].strip():
                status.append(lines[index])
                index += 1
    citations = []
    if "Rules the scopes relate to:" in lines:
        for line in lines[lines.index("Rules the scopes relate to:") + 1:]:
            if not line.strip():
                break
            if CITATION.match(line):
                citations.append(line)
    return {"scopes": scopes, "date": date, "fee": fee, "status": status, "citations": _unique(citations)}


def _suggested_differs(root, agency_id, primary):
    """The row's ``suggested_differs`` object, or None when there is no suggestion or it equals the draft."""
    suggested, error = _read_suggestion(root, agency_id)
    if suggested is None and error is None:
        return None
    path = f"kit/{SUGGESTED_DIR}/{agency_id}.md"
    if error:
        return {"path": path, "error": error, "compare_command": None}
    if primary is None:
        return {"path": path, "error": "primary draft unavailable for comparison", "compare_command": None}
    if primary == suggested:
        return None
    mine, fresh = _summary(primary), _summary(suggested)
    return {"path": path,
            "scopes_added": [x for x in fresh["scopes"] if x not in mine["scopes"]],
            "scopes_removed": [x for x in mine["scopes"] if x not in fresh["scopes"]],
            "date_range_differs": mine["date"] != fresh["date"],
            "fee_cap_differs": mine["fee"] != fresh["fee"],
            "law_package_status_differs": mine["status"] != fresh["status"],
            "rule_citations_added": [x for x in fresh["citations"] if x not in mine["citations"]],
            "rule_citations_removed": [x for x in mine["citations"] if x not in fresh["citations"]],
            "compare_command": "diff -u -- " + shlex.quote(f"kit/requests/{agency_id}.md") + " " + shlex.quote(path)}


def agency_report(root, entry, *, include_suggestion=False):
    """One kit/agencies.json entry's draft check; ``ok`` only for a regular draft with no hit and a verified agency.

    ``include_suggestion`` (kit-check only) also compares the draft with kit/requests.suggested/<id>.md.
    """
    agency_id = entry["agency_id"]
    path = Path(root) / "kit" / "requests" / (agency_id + ".md")
    row = {"agency_id": agency_id, "recipient_verified": entry.get("verified") is True}
    primary, why = _read_regular(path)
    if why:
        error = {"absent": MISSING, "unsafe": MISSING, "unreadable": "draft unreadable",
                 "too_large": "draft exceeds 1 MiB"}[why]
        report = {**row, "draft": None, "ok": False, "error": error}
    else:
        text = primary.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        hits = draft_problems(text)
        words = [hit["placeholder"].lower() for hit in hits]
        report = {**row, "draft": f"kit/requests/{agency_id}.md", "placeholders": hits[:HITS_MAX],
                  "placeholder_count": len(hits), "fee_cap_missing": any("cap" in word for word in words),
                  "date_range_missing": any("date range" in word for word in words),
                  "ok": not hits and row["recipient_verified"]}
    differs = _suggested_differs(root, agency_id, primary) if include_suggestion else None
    if differs is not None:
        report["suggested_differs"], report["ok"] = differs, False
    return report


def check(root):
    """Every selected law-enforcement agency in kit/agencies.json (first entry per id) with its draft check."""
    path = Path(root) / "kit" / "agencies.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("run kit first: kit/agencies.json is missing")
    rows, seen = [], set()
    for entry in kit._load_existing(path):
        agency_id = entry["agency_id"]
        if entry.get("selected") is True and entry.get("kind") in kit.LAW_ENFORCEMENT and agency_id not in seen:
            seen.add(agency_id)
            rows.append(agency_report(root, entry, include_suggestion=True))
    return {"agencies": rows, "problems": sum(not row["ok"] for row in rows)}


def _shown(text):  # edited drafts may hold control characters; never echo them to a terminal
    return "".join(c if c.isprintable() else "?" for c in text)


def _items(values):
    shown = [_shown(value[:HIT_CHARS]) for value in values[:TEXT_ITEMS_MAX]]
    if len(values) > TEXT_ITEMS_MAX:
        shown.append(f"{len(values) - TEXT_ITEMS_MAX} more")
    return "; ".join(shown) or "none"


def _suggestion_lines(name, diff):
    if diff.get("error"):
        return [f"{name}: {_shown(diff['path'])} not compared: {diff['error']}"]

    def yes(key):
        return "yes" if diff[key] else "no"
    return [f"{name}: edited draft differs from the fresh draft in requests.suggested/ ({_shown(diff['path'])})",
            f"{name}: scopes only in the fresh draft: {_items(diff['scopes_added'])}",
            f"{name}: scopes only in your edited draft: {_items(diff['scopes_removed'])}",
            f"{name}: date range line differs: {yes('date_range_differs')}; fee cap lines differ: "
            f"{yes('fee_cap_differs')}; law package status line differs: {yes('law_package_status_differs')}",
            f"{name}: rule citations only in the fresh draft: {_items(diff['rule_citations_added'])}",
            f"{name}: rule citations only in your edited draft: {_items(diff['rule_citations_removed'])}",
            f"{name}: compare with: {_shown(diff['compare_command'])}"]


def format_text(report):
    """``<agency_id>: OK`` or one line per problem, then ``N of M drafts need attention``."""
    lines = []
    for row in report["agencies"]:
        name = row["agency_id"]
        if row["ok"]:
            lines.append(f"{name}: OK")
            continue
        if row["draft"] is None:
            lines.append(f"{name}: {row['error']}")
        for hit in row.get("placeholders", []):
            lines.append(f"{name}: line {hit['line']} {_shown(hit['placeholder'])}")
        if row.get("placeholder_count", 0) > HITS_MAX:
            lines.append(f"{name}: {row['placeholder_count'] - HITS_MAX} more placeholder(s) not shown")
        lines += [f"{name}: {label}" for key, label in (("fee_cap_missing", "fee cap missing"),
                                                       ("date_range_missing", "date range missing")) if row.get(key)]
        if not row["recipient_verified"]:
            lines.append(f"{name}: recipient not verified")
        if "suggested_differs" in row:
            lines += _suggestion_lines(name, row["suggested_differs"])
    lines.append(f"{report['problems']} of {len(report['agencies'])} drafts need attention")
    return "\n".join(lines)


def run_cli(root, as_json=False):
    """Print the report (text or JSON); exit 3 when any draft needs attention."""
    report = check(root)
    print(json.dumps(report, indent=2) if as_json else format_text(report))
    print("Read-only. Nothing was sent.", file=sys.stderr)
    if report["problems"]:
        sys.exit(3)
