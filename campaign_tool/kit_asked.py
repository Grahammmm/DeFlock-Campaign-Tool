"""Propose public ``asked`` items for content/agencies.json from the private kit/sent.json (DF1207-03).

Reads ``kit/sent.json`` and ``content/agencies.json`` only; a dry run prints one line per field-level change and writes
nothing. ``--apply`` builds the whole new document, validates it with ``content.load_agencies`` and then makes one atomic
write. Only ``topic``, ``asked_on``, ``state`` and ``updated_on`` are derived, and the private request name is kept as
``asked.request`` so the next run finds the same item. Notes, bases, references and record hashes of a request never reach
content. A request closed without production is left for the organizer to choose. Nothing is fetched or sent.
"""
import copy
import json
import os
import sys
from pathlib import Path

from . import kit, kit_track, law
from .content import ContentError, _read_json, load_agencies

TOPIC_MAX = 120
NO_SCOPES = "Records request"
ORGANIZER = "organizer to choose"


def latest_step(steps):
    """``(kind, on)`` of the step with the latest date (a tie: the one recorded last), or None when no step is dated."""
    dated = [(s["on"], i) for i, s in enumerate(steps) if isinstance(s, dict) and kit_track._day(s.get("on"))]
    if not dated:
        return None
    step = steps[max(dated)[1]]
    return step.get("kind"), step["on"]


def topic_text(titles):
    """The scope titles joined with "; ", cut at the last "; " boundary within 120 characters."""
    if not titles:
        return NO_SCOPES
    text = "; ".join(titles)
    if len(text) <= TOPIC_MAX:
        return text
    kept = []
    for title in titles:
        if len("; ".join(kept + [title])) > TOPIC_MAX:
            break
        kept.append(title)
    return "; ".join(kept) if kept else titles[0][:TOPIC_MAX - 3] + "..."


def state_of(entry):
    """The public state for one sent entry, or None for a request closed without production (organizer to choose)."""
    steps = [s for s in entry["steps"] if isinstance(s, dict)]
    if not steps:
        return "awaiting_response"
    produced = [s for s in steps if s.get("kind") == "produced"]
    if produced:
        scopes, answers = entry.get("scopes"), kit_track._answers(steps)
        every = isinstance(scopes, list) and all(answers.get(i, (None,))[0] in ("produced", "no_records_stated")
                                                 for i in scopes)
        return "answered" if every and not kit_track._partial(produced[-1]) else "partly_answered"
    kind = (latest_step(steps) or (None, None))[0]
    if kind == "closed":
        return None
    return "declined_in_writing" if kind == "denied" else "acknowledged"


def _titles(root, law_base=None):
    from .cli import read_config  # local import: cli imports this module lazily
    try:
        package = law.load_package(kit._jurisdiction(read_config(root)), law_base)
    except (OSError, ValueError, KeyError):
        return {}
    return {scope["scope_id"]: scope["title"] for scope in package["request_scopes"]}


def _proposal(entry, titles):
    asked = entry.get("scopes")
    found = [titles[i] for i in asked if i in titles] if isinstance(asked, list) else []
    proposal = {"topic": topic_text(found), "asked_on": entry["sent_on"], "state": state_of(entry)}
    step = latest_step([s for s in entry["steps"] if isinstance(s, dict)])
    return {**proposal, **({"updated_on": step[1]} if step else {})}


def _show(value):
    return "(absent)" if value is None else json.dumps(value, ensure_ascii=False)


def propose(root, today=None, law_base=None):
    """``{"lines", "document", "changed", "error"}``: the printed changes, the proposed whole document, and the
    first refusal of ``load_agencies`` on it (None when it validates). Writes nothing."""
    root = Path(root)
    path = root / "content" / "agencies.json"
    if not path.is_file() and not path.is_symlink():
        raise ContentError("content/agencies.json not found; nothing to propose")
    document = _read_json(path)
    if not isinstance(document, list):
        raise ContentError("content/agencies.json: must be a list of agencies")
    sent, titles = kit_track.read_sent(root), _titles(root, law_base)
    new, lines, changed = copy.deepcopy(document), [], False
    agencies = {e["agency_id"]: e for e in new
                if isinstance(e, dict) and isinstance(e.get("agency_id"), str) and e.get("selected", True) is True}
    for key, entry in sent.items():
        agency_id, request = kit_track._split(key)
        agency = agencies.get(agency_id)
        if agency is None:
            lines.append(f"kit/sent.json {key}: agency not in content/agencies.json; skipped")
            continue
        items = agency.get("asked", [])
        if not isinstance(items, list):
            raise ContentError(f"content/agencies.json {agency_id}.asked: must be a list")
        index = next((i for i, item in enumerate(items) if isinstance(item, dict) and item.get("request") == request), None)
        proposal = _proposal(entry, titles)
        if proposal["state"] is None:  # closed without production: no state is inferred
            where = f"{agency_id}.asked[{index}]" if index is not None else f"{agency_id} request {request}"
            lines.append(f"content/agencies.json {where}: closed without production; {ORGANIZER}")
            continue
        if index is None:
            agency["asked"] = items = items + [{**proposal, "request": request}]
            lines.append(f"+ new item content/agencies.json {agency_id}.asked[{len(items) - 1}]: "
                         + json.dumps(items[-1], ensure_ascii=False))
            changed = True
            continue
        for field, value in proposal.items():
            if items[index].get(field) != value:
                lines.append(f"content/agencies.json {agency_id}.asked[{index}].{field}: "
                             f"{_show(items[index].get(field))} -> {_show(value)}")
                items[index][field] = value
                changed = True
    error = None
    try:
        load_agencies(new, "content/agencies.json", today)
    except ContentError as exc:
        error = str(exc)
    return {"lines": lines, "document": new, "changed": changed, "error": error}


def run_cli(root, apply=False, today=None):
    """Print the proposal; with ``apply`` write it once (after validation). Returns the exit code (3 = Stopped)."""
    root = Path(root)
    try:
        result = propose(root, today)
    except ValueError as exc:  # ContentError and an unreadable kit/sent.json
        print(f"Stopped: {exc}", file=sys.stderr)
        return 3
    for line in result["lines"] or ["nothing to propose: content/agencies.json already follows kit/sent.json"]:
        print(line)
    if result["error"]:
        print(f"Stopped: the result would not validate, nothing written: {result['error']}", file=sys.stderr)
        return 3
    path = root / "content" / "agencies.json"
    if apply and result["changed"]:
        if path.is_symlink():
            print("Stopped: content/agencies.json must not be a symlink", file=sys.stderr)
            return 3
        mode = path.stat().st_mode & 0o777
        kit._write_json(path, result["document"])
        os.chmod(path, mode)
        print("Written to content/agencies.json (one write). Review it, then build as usual; nothing was published.")
    elif apply:
        print("Nothing written.")
    else:
        print("Dry run: nothing written. Run again with --apply to write these changes.")
    return 0
