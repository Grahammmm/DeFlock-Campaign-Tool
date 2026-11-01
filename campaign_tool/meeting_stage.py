"""Stage one kit/meetings.json entry as content/meetings/<id>.json with its comment kit.

Offline only: nothing is fetched or published. The comment kit cites published findings
only, and the document is validated by ``content.load_meeting`` before it is written.
"""
import json
import os
from datetime import datetime
from pathlib import Path

from .cli import read_config
from .content import _read_json, load_content, load_finding, load_meeting, load_participation
from .kit import _kit_folder, _write_json
from .meetings import _clean, comment_kit_md

MAX_KIT_BYTES = 4 * 1024 * 1024
NOTE = "keyword lead from Legistar; verify the posted agenda before you build or publish"


class MeetingStageError(ValueError):
    pass


def _read_kit(root):
    path = root / "kit" / "meetings.json"
    if os.path.islink(path):
        raise MeetingStageError("kit/meetings.json must not be a symlink")
    if not path.is_file():
        raise MeetingStageError("kit/meetings.json not found; run `meetings` first")
    if path.stat().st_size > MAX_KIT_BYTES:
        raise MeetingStageError("kit/meetings.json: larger than 4 MiB")
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MeetingStageError(f"kit/meetings.json: not UTF-8 JSON ({exc.__class__.__name__})") from None
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise MeetingStageError("kit/meetings.json: unsupported schema_version (expected 1)")
    if not isinstance(document.get("meetings"), list):
        raise MeetingStageError("kit/meetings.json: meetings must be a list")
    return document["meetings"]


def _find(meetings, meeting_id):
    for meeting in meetings:
        if isinstance(meeting, dict) and meeting.get("id") == meeting_id:
            return meeting
    known = [str(m.get("id")) for m in meetings if isinstance(m, dict) and "id" in m][:5]
    raise MeetingStageError(f"no meeting {meeting_id} in kit/meetings.json; known: {', '.join(known) or 'none'}")


def stage(root, meeting_id, agency_id=None, location=None, replace=False, participation=None, today=None):
    cfg = read_config(root)
    meeting = _find(_read_kit(root), meeting_id)
    if meeting.get("status") == "cancelled":
        raise MeetingStageError(f"meeting {meeting_id} is cancelled; no comment kit")
    try:
        naive = datetime.fromisoformat(str(meeting.get("starts_at")).replace("Z", "+00:00")).tzinfo is None
    except ValueError:
        naive = False  # unparseable: load_meeting names it below
    if naive:
        raise MeetingStageError("kit/meetings.json predates time zones; run meetings again")
    model = load_content(root)
    if model is None:
        raise MeetingStageError("content/ is required (the site reads content/meetings/); create content/site.json first")
    findings = [f for f in model["findings"] if agency_id is None or f.get("agency_id") == agency_id]
    cited = [{**f, "path": f"/findings/{f['slug']}.html"} for f in findings]
    entry = {**meeting, "body": _clean(meeting.get("body")), "agenda_item": _clean(meeting.get("agenda_item"))}
    if participation is not None:  # organizer-verified JSON; validated before anything is written
        try:
            if os.path.islink(participation) or os.path.getsize(participation) > 64 * 1024:
                raise ValueError("symlink or larger than 64 KB")
            with open(participation, "rb") as handle:
                part = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise MeetingStageError(f"--participation: not a regular UTF-8 JSON file up to 64 KB ({exc})") from None
        for value in part.values() if isinstance(part, dict) else ():
            if isinstance(value, dict) and isinstance(value.get("how"), str):
                value["how"] = _clean(value["how"])
        entry["participation"] = load_participation(part, str(meeting.get("starts_at")),
                                                    f"meetings/{meeting_id}.json.participation", today)
    document = {
        "id": meeting_id,
        "body": entry["body"],
        "starts_at": meeting.get("starts_at"),
        "agenda_item": entry["agenda_item"],
        "comment_kit_md": comment_kit_md(entry, cited, cfg["name"], base_url=model["site"].get("base_url")),
    }
    if isinstance(meeting.get("agenda_url"), str):
        document["agenda_url"] = meeting["agenda_url"]
    if agency_id is not None:
        document["agency_id"] = agency_id
    if location is not None:
        document["location"] = location
    document.update({"participation": entry["participation"]} if participation is not None else {})
    load_meeting(document, f"meetings/{meeting_id}.json", today=today)

    folder = root / "content" / "meetings"
    _kit_folder(folder, mode=0o755)
    target = folder / f"{meeting_id}.json"
    for path in sorted(folder.glob("*.json")):
        if path.name == target.name:
            continue
        other = json.loads(path.read_text(encoding="utf-8"))
        if (other.get("id") if isinstance(other, dict) and "id" in other else path.stem) == meeting_id:
            raise MeetingStageError(f"id {meeting_id} is already used by meetings/{path.name}")
    if os.path.islink(target):
        raise MeetingStageError(f"content/meetings/{target.name} must not be a symlink")
    replaced = target.exists()
    if replaced and not replace:
        raise MeetingStageError(f"content/meetings/{target.name} exists; pass --replace to overwrite")
    _write_json(target, document)
    return {"written": f"content/meetings/{target.name}", "findings_cited": len(cited), "replaced": replaced,
            "note": NOTE}


def stale_kits(root):
    """Read-only: staged kits citing a corrected finding without its latest correction date.

    Unreadable or invalid files are skipped; ``load_content`` and the site build report them."""
    content = Path(root) / "content"
    latest = {}
    for path in sorted((content / "findings").glob("*.json")):
        try:
            finding = load_finding(_read_json(path), "findings/" + path.name)
        except (ValueError, OSError, TypeError, KeyError):
            continue
        if finding["corrections"]:
            latest[finding["slug"]] = max(c["date"] for c in finding["corrections"])
    stale = []
    for path in sorted((content / "meetings").glob("*.json")):
        try:
            meeting = load_meeting(_read_json(path), "meetings/" + path.name)
        except (ValueError, OSError, TypeError, KeyError):
            continue
        kit = meeting["comment_kit_md"]
        for slug, date in latest.items():
            if f"/findings/{slug}.html" in kit and f"/findings/{slug}.html (corrected {date};" not in kit:
                stale.append({"meeting": meeting["id"] or path.stem, "finding": slug, "latest_correction": date})
    return sorted(stale, key=lambda entry: (entry["meeting"], entry["finding"]))
