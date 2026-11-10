"""DF1109B-04: ``meetings-status`` — a private, read-only inventory of every staged meeting's workflow state.

One row per ``content/meetings/*.json`` (upcoming first, then past newest first), then the ``kit/meetings.json`` and
``kit/meetings-manual.json`` leads not yet staged. Each row names only tokens (verified or not, recorded or not) and the
first missing step as a command template; it never prints comment text, speaker labels, contact details, verifier
names or vote prose. A broken input is its own ``unreadable`` row and hides nothing else (exit 1). Nothing is written,
created, fetched, sent or published; no symlink is followed and only expected regular files are opened.
"""
import json
import os
import re
import shlex
import stat
import sys
import unicodedata
from collections import Counter
from datetime import date, datetime

from .content import MAX_JSON_BYTES, SLUG, load_finding, load_meeting
from .meeting_comments import _entry as sent_entry
from .meeting_speakers import read_plan
from .meetings import _clean
from .meetings_manual import MAX_BYTES as KIT_BYTES, ManualMeetingError, _stored_entry

DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SCAN, MANUAL = "kit/meetings.json", "kit/meetings-manual.json"
SCAN_KEYS = ("id", "body", "starts_at", "status")  # the public projection of a scan lead; nothing else is read
FAULTS = (ValueError, OSError, TypeError, KeyError, RecursionError)
PRIVATE = "private; review-required"
BAD_DATE = "--as-of must be a real YYYY-MM-DD date"
BAD_ROOT = "campaign directory does not exist or is not a directory"
TOOL = "python3 -m campaign_tool "
STEPS = {  # next_step code -> command template (D and ID are filled in, shell-quoted; capitals are yours to supply)
    "verify_participation": "meetings-stage --directory {d} --id {i} --replace --participation FILE",
    "restage_kit": "meetings-stage --directory {d} --id {i} --replace --participation FILE",
    "record_comment_sent": "meetings-comment-sent --directory {d} --id {i} --on YYYY-MM-DD --channel CHANNEL",
    "plan_speakers": "meetings-speakers --directory {d} --id {i} --list",
    "record_outcome": ("meetings-outcome --directory {d} --id {i} --result RESULT --on YYYY-MM-DD "
                       "--source OFFICIAL_URL --verified-by NAME"),
    "record_minutes": ("meetings-outcome --directory {d} --id {i} --minutes-url OFFICIAL_MINUTES_URL "
                       "--minutes-approved-on YYYY-MM-DD"),
    "review_optional_votes": "meetings-outcome --help",
    "stage_meeting": "meetings-stage --directory {d} --id {i}",
    "repair_input": None,
}


class MeetingsStatusError(ValueError):
    pass


class _Bad(Exception):
    """An input that cannot be read safely; reported as one row, never with its exception text."""


def _label(text):
    """A path label without lone surrogates (undecodable file names) so it always prints."""
    return os.fsencode(text).decode("utf-8", "backslashreplace") if isinstance(text, str) else str(text)


def _plain(text):
    return "".join(f"\\u{ord(c):04x}" if unicodedata.category(c) == "Cs" else c for c in text)


def _kind(root, rel):
    """'dir', 'file', None (absent) or 'other'; every component is checked with lstat, never followed."""
    here, mode = root, 0
    for part in rel.split("/"):
        here = os.path.join(here, part)
        try:
            mode = os.lstat(here).st_mode
        except FileNotFoundError:
            return None
        except OSError:
            return "other"
        if stat.S_ISLNK(mode):
            return "other"
    return "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "other"


def _read(path, limit):
    """Parsed JSON of a regular, non-symlink file of at most ``limit`` bytes; None when absent."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise _Bad from None
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise _Bad
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise _Bad from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Bad
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise _Bad
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        raise _Bad from None
    finally:
        os.close(fd)


def _listing(root, rel, errors):
    """Sorted ``*.json`` names of a real directory, [] when absent; None (and one error) when it cannot be listed."""
    kind = _kind(root, rel)
    if kind is None:
        return []
    if kind != "dir":
        errors.append(_error(rel))
        return None
    try:
        return sorted(n for n in os.listdir(os.path.join(root, rel)) if n.endswith(".json"))
    except OSError:
        errors.append(_error(rel))
        return None


def _error(path, index=None):
    path = _label(path) + (f"[{index}]" if index is not None else "")
    return {"kind": "unreadable", "path": path, "error": "unreadable_input", "next_step": "repair_input", "command": None}


def _wall(starts_at):
    """The typed local wall time; the offset is kept in the row and never converted."""
    return datetime.fromisoformat(starts_at.replace("Z", "+00:00")).replace(tzinfo=None)


def _hint(row, directory):
    code = row["next_step"]
    template = STEPS[code] if code else None
    row["command"] = TOOL + template.format(d=shlex.quote(directory), i=shlex.quote(row["id"])) if template else None
    return row


def _staged_step(row, written):
    if row["status"] == "cancelled":
        return None
    if row["period"] == "past" or row["status"] == "continued":  # a continued meeting is never given a new date
        return ("record_outcome" if row["outcome"] == "not_recorded" else "record_minutes"
                if row["minutes"] == "not_recorded" else "review_optional_votes" if row["votes"] == "not_recorded"
                else None)
    if row["participation"] == "not_verified":
        return "verify_participation"
    checks = [(row["comment_kit"], {"missing", "stale"}, "restage_kit"),
              *([(row["comment_sent"], {"not_recorded"}, "record_comment_sent")] if written else []),
              (row["speakers_plan"], {"absent"}, "plan_speakers")]
    for state, missing, code in checks:
        if state == "unknown":
            return "repair_input"
        if state in missing:
            return code
    return None


def _staged(root, today, errors):
    """(loaded meetings, staged-file errors, observed ids) from content/meetings/*.json, each file validated alone."""
    if _kind(root, "content") in (None, "dir"):
        names = _listing(root, "content/meetings", errors)
    else:
        names = None
        errors.append(_error("content"))
    file_errors, observed, candidates = [], set(), []
    for name in names or []:
        stem, rel = name[:-5], f"content/meetings/{name}"
        observed.add(stem)
        try:
            meeting = load_meeting(_read(os.path.join(root, rel), MAX_JSON_BYTES), "meetings/" + name, today=today)
        except (_Bad, *FAULTS):
            file_errors.append(_error(rel))
            continue
        if meeting["id"] is not None:
            observed.add(meeting["id"])
        candidates.append((rel, stem, meeting))
    claims = Counter(stem if meeting["id"] is None else meeting["id"] for _, stem, meeting in candidates)
    loaded = []
    for rel, stem, meeting in candidates:
        effective = stem if meeting["id"] is None else meeting["id"]
        if claims[effective] > 1 or meeting["id"] not in (None, stem) or not SLUG.fullmatch(stem):
            file_errors.append(_error(rel))  # a mismatched, duplicated or unsafe identity gets no row and no hint
            continue
        loaded.append((rel, stem, meeting))
    return loaded, file_errors, observed


def _findings_ok(root, errors):
    """Completeness and latest corrections from this call's bounded, validated findings."""
    names = _listing(root, "content/findings", errors)
    if names is None:
        return False, {}
    ok, latest = True, {}
    for name in names:
        rel = f"content/findings/{name}"
        try:
            finding = load_finding(_read(os.path.join(root, rel), MAX_JSON_BYTES), "findings/" + name)
        except (_Bad, *FAULTS):
            errors.append(_error(rel))
            ok = False
            continue
        if finding["corrections"]:
            slug = finding["slug"]
            latest[slug] = max(latest.get(slug, ""), *(c["date"] for c in finding["corrections"]))
    return ok, latest


def _stale(root, loaded, latest):
    """Stale identities from accepted objects, or None when the metadata safety gate is unavailable."""
    for folder in ("content/findings", "content/meetings"):
        kind = _kind(root, folder)
        if kind not in (None, "dir"):
            return None
        try:
            for name in os.listdir(os.path.join(root, folder)) if kind else []:
                mode = os.lstat(os.path.join(root, folder, name)).st_mode
                if name.endswith(".json") and not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                    return None  # preserve unknown for unsafe content trees, without opening their contents
        except OSError:
            return None
    return {meeting["id"] or stem for _, stem, meeting in loaded
            if any(f"/findings/{slug}.html" in meeting["comment_kit_md"]
                   and f"/findings/{slug}.html (corrected {day};" not in meeting["comment_kit_md"]
                   for slug, day in latest.items())}


def _sent(root, kit_ok, errors):
    """{meeting id: entry} from kit/comments-sent.json, or None (unknown) with one error row."""
    if not kit_ok:
        return None
    kind = _kind(root, "kit/comments-sent.json")
    if kind is None:
        return {}
    try:
        if kind != "file":
            raise _Bad
        document = _read(os.path.join(root, "kit", "comments-sent.json"), MAX_JSON_BYTES)
        if (not isinstance(document, dict) or document.get("schema_version") != 1
                or set(document) != {"schema_version", "comments"} or not isinstance(document["comments"], dict)):
            raise _Bad
        return {str(key): sent_entry(value, key) for key, value in document["comments"].items()}
    except (_Bad, *FAULTS):
        errors.append(_error("kit/comments-sent.json"))
        return None


def _speakers(root, meeting_id, kit_ok, errors):
    if not kit_ok:
        return "unknown"
    try:
        return "absent" if read_plan(root, meeting_id)[0] is None else "present"
    except FAULTS:
        errors.append(_error(f"kit/meetings/speakers/{meeting_id}.json"))
        return "unknown"


def _staged_row(root, rel, stem, meeting, as_of, sent, stale, findings_ok, kit_ok, errors):
    outcome = meeting["outcome"] or {}
    kit = meeting["comment_kit_md"]
    row = {
        "kind": "staged", "path": _label(rel), "id": stem, "body": _plain(meeting["body"]),
        "starts_at": meeting["starts_at"], "period": "upcoming" if meeting["starts_at"][:10] >= as_of else "past",
        "status": ("cancelled" if meeting["status"] == "cancelled"
                   else "continued" if outcome.get("result") == "continued" else "scheduled"),
        "participation": "verified" if meeting["participation"] is not None else "not_verified",
        "comment_kit": ("missing" if not kit.strip() else "stale" if stale is not None and stem in stale
                        else "current" if stale is not None and findings_ok else "unknown"),
        "comment_sent": "unknown" if sent is None else "recorded" if stem in sent else "not_recorded",
        "speakers_plan": _speakers(root, stem, kit_ok, errors),
        "outcome": outcome.get("result", "not_recorded"),
        "minutes": "recorded" if "minutes_url" in outcome else "not_recorded",
        "votes": "recorded" if "vote" in outcome or "votes" in outcome else "not_recorded",
    }
    written = bool((meeting["participation"] or {}).get("written_comment"))
    row["next_step"] = _staged_step(row, written)
    return row, _wall(meeting["starts_at"])


def _leads(root, kit_ok, today, errors):
    """(lead, path label, index) for each healthy scan/manual entry; one error per bad file or list item."""
    if not kit_ok:
        return []
    found = []
    for rel, limit in ((SCAN, KIT_BYTES), (MANUAL, KIT_BYTES)):
        try:
            document = _read(os.path.join(root, rel), limit)
        except _Bad:
            errors.append(_error(rel))
            continue
        if document is None:
            continue
        if rel == SCAN:
            valid = isinstance(document, dict) and document.get("schema_version") == 1 and isinstance(
                document.get("meetings"), list)
        else:
            valid = (isinstance(document, dict) and set(document) == {"schema_version", "meetings"}
                     and type(document["schema_version"]) is int and document["schema_version"] == 1
                     and isinstance(document["meetings"], list))
        if not valid:
            errors.append(_error(rel))
            continue
        for index, raw in enumerate(document["meetings"]):
            try:
                if rel == SCAN:
                    if not isinstance(raw, dict):
                        raise _Bad
                    projected = {k: raw[k] for k in SCAN_KEYS if k in raw}
                    projected["body"] = _clean(projected.get("body"))
                    if "id" not in projected:
                        raise _Bad
                else:
                    entry = _stored_entry(raw, f"meetings[{index}]")
                    projected = {k: entry[k] for k in SCAN_KEYS}
                lead = load_meeting(projected, "meeting", today=today)
            except (_Bad, ManualMeetingError, *FAULTS):
                errors.append(_error(rel, index))
                continue
            found.append((lead, rel, index))
    counts = {}
    for lead, _, _ in found:
        counts[lead["id"]] = counts.get(lead["id"], 0) + 1
    healthy = []
    for lead, rel, index in found:
        if counts[lead["id"]] > 1:
            errors.append(_error(rel, index))  # meetings-stage refuses an id listed twice; never pick one
        else:
            healthy.append((lead, rel))
    return healthy


def _unstaged_row(lead, rel, as_of):
    row = {"kind": "unstaged", "path": rel, "id": lead["id"], "body": _plain(lead["body"]),
           "starts_at": lead["starts_at"], "period": "upcoming" if lead["starts_at"][:10] >= as_of else "past",
           "status": lead["status"], "next_step": None if lead["status"] == "cancelled" else "stage_meeting"}
    return row, _wall(lead["starts_at"])


def _calendar(pairs):
    """Upcoming ascending by local wall time, then past newest first; ties by id, then path."""
    upcoming = sorted((p for p in pairs if p[0]["period"] == "upcoming"), key=lambda p: (p[1], p[0]["id"], p[0]["path"]))
    past = sorted((p for p in pairs if p[0]["period"] == "past"), key=lambda p: (p[0]["id"], p[0]["path"]))
    past = sorted(past, key=lambda p: p[1], reverse=True)  # stable: equal times keep id/path ascending
    return [p[0] for p in upcoming + past]


def _error_key(row):
    match = re.fullmatch(r"(.*)\[([0-9]+)\]", row["path"])
    return (match[1], int(match[2])) if match else (row["path"], -1)


def inventory(root, as_of=None, directory=None, today=None):
    """``{schema_version, as_of, private, rows}``; ``root`` is used as given (not resolved). Reads only."""
    today = today or date.today()  # sampled once; validation uses the real day, grouping uses as_of
    as_of = as_of or today.isoformat()
    root = os.fspath(root)
    directory = os.fspath(directory if directory is not None else root)
    kind = os.lstat(root).st_mode if os.path.lexists(root) else None
    if kind is None or not stat.S_ISDIR(kind):
        raise MeetingsStatusError(BAD_ROOT)
    errors = []
    loaded, file_errors, observed = _staged(root, today, errors)
    findings_ok, latest = _findings_ok(root, errors)
    stale = _stale(root, loaded, latest) if loaded else set()
    kit_ok = _kind(root, "kit") in (None, "dir")
    if not kit_ok:
        errors.append(_error("kit"))
    sent = _sent(root, kit_ok, errors)
    staged = [_staged_row(root, rel, stem, meeting, as_of, sent, stale, findings_ok, kit_ok, errors)
              for rel, stem, meeting in loaded]
    leads = [_unstaged_row(lead, rel, as_of) for lead, rel in _leads(root, kit_ok, today, errors)
             if lead["id"] not in observed]
    rows = (_calendar(staged) + sorted(file_errors, key=_error_key) + _calendar(leads)
            + sorted(errors, key=_error_key))
    for row in rows:
        if row["kind"] != "unreadable":
            _hint(row, directory)
    return {"schema_version": 1, "as_of": as_of, "private": True, "rows": rows}


def _cell(text):
    """One physical line: backslash, the | delimiter and every control/format/separator character escaped."""
    out = []
    for char in str(text):
        if char in "\\|":
            out.append("\\" + char)
        elif unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)


def render_text(report):
    lines = []
    for row in report["rows"]:
        if row["kind"] == "unreadable":
            lines.append(f"unreadable | {_cell(row['path'])} | review input locally | {PRIVATE}")
            continue
        nxt = _cell(row["command"]) if row["command"] else row["next_step"] or "-"
        head = f"{row['kind']} | {_cell(row['id'])} | {_cell(row['body'])} | {_cell(row['starts_at'])} | {row['status']}"
        if row["kind"] == "staged":
            head += (f" | participation={row['participation']} | kit={row['comment_kit']} | sent={row['comment_sent']}"
                     f" | speakers={row['speakers_plan']} | outcome={row['outcome']} | minutes={row['minutes']}"
                     f" | votes={row['votes']}")
        lines.append(f"{head} | next={nxt} | {PRIVATE}")
    if not lines:
        lines.append(f"No staged meetings or unstaged matches. | {PRIVATE}")
    return "\n".join(lines) + "\n"


def run_cli(args, root):
    """Exit code: 1 when any input is unreadable (or the date/folder is refused), else 0. Missing work is not a failure."""
    as_of = args.as_of
    if as_of is not None:
        try:
            if not DAY.fullmatch(as_of):
                raise ValueError
            date.fromisoformat(as_of)
        except ValueError:
            print("Stopped: " + BAD_DATE, file=sys.stderr)
            return 1
    try:
        report = inventory(root, as_of, directory=str(root))
    except MeetingsStatusError as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_text(report), end="")
    return 1 if any(row["kind"] == "unreadable" for row in report["rows"]) else 0
