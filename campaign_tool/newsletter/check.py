"""``newsletter --directory D --check [--json]``: does the saved offline draft still match current content? (DF1622-13)

Read-only. It reads ``kit/newsletter-draft.json`` and its private ``provenance`` object (written only by
``--from-content``) and compares the findings, corrections and upcoming meetings it lists with ``content/``.
It never rebuilds, edits, records, archives or sends, and does not look at requests, records, the map or decisions.
"""
import json
import shlex
from datetime import datetime, timezone
from pathlib import Path

from ..content import load_content
from .draft import DraftError, _date, _day
from .sent import DRAFT_NAME, MAX_DRAFT_BYTES, _read_regular

MATCH = "draft matches current content (findings, corrections and meetings; requests, records, map and decisions not checked)"
NOT_CHECKED = "not checked: this draft has no content provenance; rebuild it with newsletter --from-content"
_FINDING_KEYS = {"id", "slug", "published_at", "section", "corrections"}
_MEETING_KEYS = {"id", "starts_at", "agenda_url"}


class CheckError(ValueError):
    """The draft or the content cannot be read; the check stops (exit 1)."""


def _unique_pairs(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _stamp(value, label):
    try:
        return _date(value, label)
    except DraftError:
        raise CheckError(f"draft provenance is invalid: {label} needs an ISO timestamp with a UTC offset") from None


def _provenance(raw):
    """Validate a schema-1 provenance object; returns it with parsed instants. Never echoes input values."""
    if not isinstance(raw, dict) or not {"built_at", "since", "findings", "meetings"} <= set(raw):
        raise CheckError("draft provenance is invalid: built_at, since, findings and meetings are required")
    _stamp(raw["built_at"], "built_at")
    since = None if raw["since"] is None else _stamp(raw["since"], "since")
    if not isinstance(raw["findings"], list) or not isinstance(raw["meetings"], list):
        raise CheckError("draft provenance is invalid: findings and meetings must be lists")
    seen, findings, meetings = set(), [], []
    for index, row in enumerate(raw["findings"]):
        label = f"findings[{index}]"
        if not isinstance(row, dict) or set(row) != _FINDING_KEYS:
            raise CheckError(f"draft provenance is invalid: {label} needs exactly {', '.join(sorted(_FINDING_KEYS))}")
        if not all(isinstance(row[k], str) and row[k] for k in ("id", "slug")) or row["section"] not in ("new", "correction"):
            raise CheckError(f"draft provenance is invalid: {label} id, slug or section")
        _stamp(row["published_at"], label + ".published_at")
        corrections = row["corrections"]
        if not isinstance(corrections, list) or not all(isinstance(c, dict) and set(c) == {"date", "note"}
                                                         and isinstance(c["note"], str) for c in corrections):
            raise CheckError(f"draft provenance is invalid: {label}.corrections")
        for c in corrections:
            try:
                _day(c["date"], label + ".corrections")
            except DraftError:
                raise CheckError(f"draft provenance is invalid: {label}.corrections date") from None
        if ("finding", row["id"]) in seen:
            raise CheckError("draft provenance is invalid: duplicate finding id")
        seen.add(("finding", row["id"]))
        findings.append(row)
    for index, row in enumerate(raw["meetings"]):
        label = f"meetings[{index}]"
        if not isinstance(row, dict) or set(row) != _MEETING_KEYS:
            raise CheckError(f"draft provenance is invalid: {label} needs exactly {', '.join(sorted(_MEETING_KEYS))}")
        if not isinstance(row["id"], str) or not row["id"] or not (row["agenda_url"] is None or isinstance(row["agenda_url"], str)):
            raise CheckError(f"draft provenance is invalid: {label} id or agenda_url")
        _stamp(row["starts_at"], label + ".starts_at")
        if ("meeting", row["id"]) in seen:
            raise CheckError("draft provenance is invalid: duplicate meeting id")
        seen.add(("meeting", row["id"]))
        meetings.append(row)
    return {"since_text": raw["since"], "since": since, "findings": findings, "meetings": meetings}


def _read_draft(root):
    kit = Path(root) / "kit"
    if kit.is_symlink() or (kit.exists() and not kit.is_dir()):
        raise CheckError("kit/ must be a folder, not a link or a file")
    try:
        data = _read_regular(kit / DRAFT_NAME, MAX_DRAFT_BYTES, "kit/" + DRAFT_NAME)
    except (OSError, ValueError) as exc:
        raise CheckError(f"kit/{DRAFT_NAME} unreadable: {exc}") from None
    if data is None:
        raise CheckError(f"no draft: kit/{DRAFT_NAME} does not exist")
    try:
        draft = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (ValueError, RecursionError):
        raise CheckError(f"kit/{DRAFT_NAME} unreadable: not valid JSON with unique keys") from None
    if not isinstance(draft, dict):
        raise CheckError(f"kit/{DRAFT_NAME} unreadable: a JSON object is required")
    return draft


def _rebuild(root, since_text):
    command = f"python3 -m campaign_tool.newsletter --directory {shlex.quote(str(root))} --from-content"
    return command + (f" --since {since_text[:10]}" if since_text else "")


def _corrections_reason(old, new):
    old_dates, new_dates = [c["date"] for c in old], [c["date"] for c in new]
    added, removed = list(new_dates), list(old_dates)
    for day in old_dates:
        if day in added:
            added.remove(day)
    for day in new_dates:
        if day in removed:
            removed.remove(day)
    if not added and not removed:
        return "corrections changed (dates unchanged; correction details changed)"
    parts = ([f"added {', '.join(sorted(added))}"] if added else []) + ([f"removed {', '.join(sorted(removed))}"] if removed else [])
    return "corrections changed (" + "; ".join(parts) + ")"


def check_draft(root, now=None):
    """Return ``{"status": match|different|not_checked, "message", "differences": [...]}``; raises ``CheckError``."""
    now = now or datetime.now(timezone.utc)
    draft = _read_draft(root)
    raw = draft.get("provenance")
    version = raw.get("schema_version") if isinstance(raw, dict) else None
    if type(version) is not int or version != 1:
        return {"status": "not_checked", "message": NOT_CHECKED, "differences": []}
    saved = _provenance(raw)
    try:
        model = load_content(root)
    except (OSError, ValueError) as exc:  # ContentError is a ValueError
        raise CheckError(f"content unreadable: {str(exc)[:200]}") from None
    if model is None:
        raise CheckError("content/ does not exist")
    current_findings = {f["id"]: f for f in model["findings"]}
    current_meetings = {m["id"]: m for m in model["meetings"]}
    rebuild = _rebuild(root, saved["since_text"])
    rows = []  # (kind, id, label, reasons)
    listed = set()
    for entry in saved["findings"]:
        listed.add(entry["id"])
        found = current_findings.get(entry["id"])
        if found is None:
            reasons = ["withdrawn or unpublished"]
        else:
            reasons = []
            if found["slug"] != entry["slug"]:
                reasons.append(f"slug changed from {entry['slug']} to {found['slug']}")
            if found["published_at"] != entry["published_at"]:
                reasons.append(f"published_at changed from {entry['published_at']} to {found['published_at']}")
            now_corrections = [{"date": c["date"], "note": c["note"]} for c in found["corrections"]]
            if now_corrections != entry["corrections"]:
                reasons.append(_corrections_reason(entry["corrections"], now_corrections))
        if reasons:
            rows.append(("finding", entry["id"], f"finding {entry['id']} ({entry['slug']})", reasons))
    for ident, found in current_findings.items():
        if ident in listed:
            continue
        published = _date(found["published_at"], "published_at")
        recent = saved["since"] is not None and any(c["date"] >= saved["since"].date().isoformat() for c in found["corrections"])
        if saved["since"] is None or published >= saved["since"]:
            why = "published in the draft's window but not in the draft"
        elif recent:
            why = "corrected in the draft's window but not in the draft"
        else:
            continue
        rows.append(("finding", ident, f"finding {ident} ({found['slug']})", [why]))
    listed = set()
    for entry in saved["meetings"]:
        listed.add(entry["id"])
        found = current_meetings.get(entry["id"])
        if found is None:
            reasons = ["removed"]
        else:
            reasons = []
            if found.get("status") == "cancelled":
                reasons.append("cancelled")
            if found["starts_at"] != entry["starts_at"]:
                reasons.append(f"rescheduled from {entry['starts_at']} to {found['starts_at']}")
            if found["agenda_url"] != entry["agenda_url"]:
                reasons.append("agenda link changed")
            if _date(found["starts_at"], "starts_at") < now:
                reasons.append("already started")
        if reasons:
            rows.append(("meeting", entry["id"], f"meeting {entry['id']}", reasons))
    for ident, found in current_meetings.items():
        if ident not in listed and found.get("status") != "cancelled" and _date(found["starts_at"], "starts_at") >= now:
            rows.append(("meeting", ident, f"meeting {ident}", ["upcoming but not in the draft"]))
    rows.sort(key=lambda r: (r[0], r[1]))
    differences = [{"kind": kind, "id": ident, "reasons": reasons, "rebuild_command": rebuild}
                   for kind, ident, _, reasons in rows]
    lines = [f"{label}: {'; '.join(reasons)}; rebuild: {rebuild}" for _, _, label, reasons in rows]
    if not differences:
        return {"status": "match", "message": MATCH, "differences": []}
    return {"status": "different", "message": f"draft differs from current content in {len(differences)} place"
            f"{'s' if len(differences) != 1 else ''}", "differences": differences, "_lines": lines}


def report_text(report):
    return "\n".join(report.get("_lines") or [report["message"]])


def report_json(report):
    return json.dumps({k: report[k] for k in ("status", "message", "differences")}, indent=2)
