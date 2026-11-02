"""kit-check: what each selected law-enforcement agency's request draft still needs before it is sent (read-only).

A draft needs attention when it still has a bracket placeholder such as ``[explicit approved cap, e.g. $50]``
(a Markdown link ``[text](url)`` is not one), when kit/agencies.json does not mark its agency ``verified: true``,
or when the draft is missing or a symlink. Nothing is written and nothing is sent.
"""
import json
import re
import sys
from pathlib import Path

from . import kit

PLACEHOLDER = re.compile(r"\[[^\]\n]{1,160}\](?!\()")
HIT_CHARS, HITS_MAX = 160, 50
MISSING = "draft missing or not a regular file"


def draft_problems(text):
    """Every unfilled bracket placeholder in ``text`` as {"line", "placeholder"} (1-based line numbers)."""
    return [{"line": number, "placeholder": match.group(0)[:HIT_CHARS]}
            for number, line in enumerate(text.split("\n"), 1) for match in PLACEHOLDER.finditer(line)]


def agency_report(root, entry):
    """One kit/agencies.json entry's draft check; ``ok`` only for a regular draft with no hit and a verified agency."""
    agency_id = entry["agency_id"]
    path = Path(root) / "kit" / "requests" / (agency_id + ".md")
    row = {"agency_id": agency_id, "recipient_verified": entry.get("verified") is True}
    if path.is_symlink() or not path.is_file():
        return {**row, "draft": None, "ok": False, "error": MISSING}
    hits = draft_problems(path.read_text(encoding="utf-8", errors="replace"))
    words = [hit["placeholder"].lower() for hit in hits]
    return {**row, "draft": f"kit/requests/{agency_id}.md", "placeholders": hits[:HITS_MAX],
            "placeholder_count": len(hits), "fee_cap_missing": any("cap" in word for word in words),
            "date_range_missing": any("date range" in word for word in words),
            "ok": not hits and row["recipient_verified"]}


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
            rows.append(agency_report(root, entry))
    return {"agencies": rows, "problems": sum(not row["ok"] for row in rows)}


def _shown(text):  # edited drafts may hold control characters; never echo them to a terminal
    return "".join(c if c.isprintable() else "?" for c in text)


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
    lines.append(f"{report['problems']} of {len(report['agencies'])} drafts need attention")
    return "\n".join(lines)


def run_cli(root, as_json=False):
    """Print the report (text or JSON); exit 3 when any draft needs attention."""
    report = check(root)
    print(json.dumps(report, indent=2) if as_json else format_text(report))
    print("Read-only. Nothing was sent.", file=sys.stderr)
    if report["problems"]:
        sys.exit(3)
