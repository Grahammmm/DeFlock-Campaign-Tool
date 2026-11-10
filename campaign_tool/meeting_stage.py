"""Stage one kit/meetings.json entry as content/meetings/<id>.json with its comment kit.

Offline only: nothing is fetched or published. The comment kit cites published findings
only, and the document is validated by ``content.load_meeting`` before it is written.
"""
import json
import os
import stat
from datetime import date, datetime
from pathlib import Path

from .cli import read_config
from .content import (_read_json, load_comment_asks, load_content, load_finding, load_meeting,
                      load_participation)
from .kit import _kit_folder, _write_json
from .legal_claims import legal_notes
from .meeting_elsewhere import choose
from .meetings import _clean, comment_kit_md, manual_note
from .meetings_manual import ManualMeetingError, read_meetings
from .supporters import MAX_COSIGNERS, cosign_problems, resolve_cosigners

MAX_KIT_BYTES = 4 * 1024 * 1024
PARTICIPATION_LIMIT = 64 * 1024  # docs/MEETINGS.md: --participation FILE is a regular file of at most 64 KB
MAX_CITED = 3  # comment_kit_md speaks at most three findings
NOTE = "keyword lead from Legistar; verify the posted agenda before you build or publish"


class MeetingStageError(ValueError):
    pass


def _read_kit(root):
    """DF1103-09: kit/meetings.json (scan) and kit/meetings-manual.json (organizer-entered), merged by id."""
    try:
        return read_meetings(root)
    except ManualMeetingError as exc:
        raise MeetingStageError(str(exc)) from None


def _read_participation(path):
    """RF1025-3: the decoded --participation JSON from one admitted regular-file descriptor of at most 64 KB.

    The final component is checked without following links, opened nonfollowing and nonblocking, and the opened
    descriptor must itself be a regular file of at most the limit and the very inode admitted; at most limit+1 bytes
    are then read from that descriptor, never from the pathname again. This bounds file acquisition only (not parser
    memory or time) and is no snapshot of in-place edits of the same inode. Where O_NOFOLLOW is missing, the
    pre-/post-open nonfollowing type and identity checks are the weaker substitute. Every refusal is one fixed
    message that names no path, payload or OS text.
    """
    refusal = MeetingStageError("--participation: not a regular UTF-8 JSON file up to 64 KB")
    nonblock = getattr(os, "O_NONBLOCK", None)
    if nonblock is None:  # a blocking open of a swapped-in special file could hang
        raise refusal
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        admitted = os.lstat(path)
        if not stat.S_ISREG(admitted.st_mode):
            raise refusal
        fd = os.open(path, os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0))
    except (OSError, ValueError):  # ValueError: an embedded NUL in the path
        raise refusal from None
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size > PARTICIPATION_LIMIT
                or (opened.st_dev, opened.st_ino) != (admitted.st_dev, admitted.st_ino)):
            raise refusal
        if not nofollow:
            after = os.lstat(path)
            if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
                raise refusal
        chunks, remaining = [], PARTICIPATION_LIMIT + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > PARTICIPATION_LIMIT:
            raise refusal
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise refusal from None
    finally:
        os.close(fd)


def _find(meetings, meeting_id):
    for meeting in meetings:
        if isinstance(meeting, dict) and meeting.get("id") == meeting_id:
            return meeting
    known = [str(m.get("id")) for m in meetings if isinstance(m, dict) and "id" in m][:5]
    raise MeetingStageError(f"no meeting {meeting_id} in kit/meetings.json; known: {', '.join(known) or 'none'}")


def _default_findings(findings, agency_id):
    """At most 3: the meeting agency's findings first, then the rest; each newest event_date first, ties by id."""
    return sorted(findings, key=lambda f: (agency_id is not None and f.get("agency_id") != agency_id,
                                           -date.fromisoformat(f["event_date"]).toordinal(), f["id"]))[:MAX_CITED]


def _chosen_findings(findings, finding_ids):
    """The organizer's --finding ids, in the order given; each must be a loaded (published) finding."""
    if not 1 <= len(finding_ids) <= MAX_CITED:
        raise MeetingStageError(f"--finding: give 1 to {MAX_CITED} ids (got {len(finding_ids)})")
    published = {f["id"]: f for f in findings}
    for index, finding_id in enumerate(finding_ids):
        if finding_id in finding_ids[:index]:
            raise MeetingStageError(f"--finding {finding_id}: given more than once")
        if finding_id not in published:
            raise MeetingStageError(f"--finding {finding_id}: not a published finding")
    return [published[finding_id] for finding_id in finding_ids]


def _chosen_cosigners(cosign_ids):
    """DF2207-14: the organizer's --cosign ids as given (order kept); never deduplicated or trimmed silently."""
    if not 1 <= len(cosign_ids) <= MAX_COSIGNERS:
        raise MeetingStageError(f"--cosign: give 1 to {MAX_COSIGNERS} supporter ids (got {len(cosign_ids)})")
    for index, supporter_id in enumerate(cosign_ids):
        if supporter_id in cosign_ids[:index]:
            raise MeetingStageError(f"--cosign {supporter_id}: given more than once")
    return list(cosign_ids)


def stage(root, meeting_id, agency_id=None, location=None, replace=False, participation=None, today=None,
          finding_ids=None, asks=None, cosign_ids=None, elsewhere_indices=None):
    override = load_comment_asks(asks, "--ask") if asks is not None else None  # DF2207-07: before anything is read or written
    cosign_ids = _chosen_cosigners(cosign_ids) if cosign_ids is not None else None
    cfg = read_config(root)
    meeting = _find(_read_kit(root), meeting_id)
    manual = meeting.get("source") == "manual"
    if manual and agency_id is None:
        agency_id = meeting["agency_id"]  # the saved agency; the kit and its lookup use this one entry
    elif manual and agency_id != meeting["agency_id"]:
        raise MeetingStageError(f"--agency-id: meeting {meeting_id} is saved for another agency")
    if meeting.get("status") == "cancelled":
        raise MeetingStageError(f"meeting {meeting_id} is cancelled; no comment kit")
    try:
        naive = datetime.fromisoformat(str(meeting.get("starts_at")).replace("Z", "+00:00")).tzinfo is None
    except ValueError:
        naive = False  # unparseable: load_meeting names it below
    if naive:
        raise MeetingStageError("kit/meetings.json predates time zones; run meetings again")
    model = load_content(root, _replacing_cosign_id=meeting_id if replace else None)  # DF2207-14: --replace repairs it
    if model is None:
        raise MeetingStageError("content/ is required (the site reads content/meetings/); create content/site.json first")
    site_asks = model["site"].get("comment_asks")  # --ask wins as a whole, then site.json, then the built-in defaults
    custom = override if override is not None else site_asks
    findings = (_chosen_findings(model["findings"], list(finding_ids)) if finding_ids is not None
                else _default_findings(model["findings"], agency_id))
    snapshots = []
    if elsewhere_indices is not None:  # DF1107-06: stored order of content/elsewhere.json, never the page's sorting
        try:
            snapshots = choose(model.get("elsewhere"), elsewhere_indices)
        except ValueError as exc:
            raise MeetingStageError(str(exc)) from None
    cited = [{**f, "path": f"/findings/{f['slug']}.html"} for f in findings]
    entry = {**meeting, "body": _clean(meeting.get("body")), "agenda_item": _clean(meeting.get("agenda_item"))}
    if participation is not None:  # organizer-verified JSON; validated before anything is written
        part = _read_participation(participation)
        for value in part.values() if isinstance(part, dict) else ():
            if isinstance(value, dict) and isinstance(value.get("how"), str):
                value["how"] = _clean(value["how"])
        entry["participation"] = load_participation(part, str(meeting.get("starts_at")),
                                                    f"meetings/{meeting_id}.json.participation", today)
    cosigners = []
    if cosign_ids is not None:  # DF2207-14: only a validated written comment and this exact meeting's grants count
        if participation is None or not entry["participation"].get("written_comment"):
            raise MeetingStageError("--cosign: needs --participation with a verified written_comment to sign")
        cosigners = resolve_cosigners(root, meeting_id, cosign_ids, today=today)
    document = {
        "id": meeting_id,
        "body": entry["body"],
        "starts_at": meeting.get("starts_at"),
        "agenda_item": entry["agenda_item"],
        "comment_kit_md": comment_kit_md(entry, cited, cfg["name"], base_url=model["site"].get("base_url"),
                                    asks=custom, **({"elsewhere": snapshots} if snapshots else {}), **({"cosign_names": [row["name"] for row in cosigners]} if cosigners else {})),
    }
    if isinstance(meeting.get("agenda_url"), str):
        document["agenda_url"] = meeting["agenda_url"]
    if agency_id is not None:
        document["agency_id"] = agency_id
    if location is not None:
        document["location"] = location
    document.update({"participation": entry["participation"]} if participation is not None else {})
    if cosigners:
        document["cosigners"] = list(cosign_ids)
    if cited:  # DF2207-06: the exact ids the kit cites, once; a kit citing none omits the field
        document["cited_findings"] = [f["id"] for f in cited]
    if snapshots:  # full snapshots, flag order: a later reorder of elsewhere.json cannot change this kit
        document["cited_elsewhere"] = snapshots
    kept = root / "content" / "meetings" / f"{meeting_id}.json"  # an organizer-verified outcome survives --replace
    try:
        old = _read_json(kept) if replace and kept.is_file() and not os.path.islink(kept) else {}
    except ValueError:
        old = {}  # unreadable: overwritten as before
    # RF0834-3: only these recorded historical objects are carried over, raw; the submitted comment is exact text
    # (not draft-kit prose) and an invalid one is refused by load_meeting below, never dropped.
    document.update({key: old[key] for key in ("outcome", "submitted_comment") if isinstance(old, dict) and key in old})
    loaded = load_meeting(document, f"meetings/{meeting_id}.json", today=today)
    if cosigners:  # the whole new document is checked against the grants and the generated block before any write
        problems = cosign_problems(root, [{**loaded, "id": meeting_id}], today=today)
        if problems:
            raise MeetingStageError(problems[0])

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
    report = {"written": f"content/meetings/{target.name}", "findings_cited": len(cited),
              "findings": [f["id"] for f in cited], "replaced": replaced,
              "note": manual_note(meeting) if manual else NOTE}
    if cosigners:
        report["cosigners"] = list(cosign_ids)
    flags = _ask_notes(custom, "--ask" if override is not None else "site.json.comment_asks")
    if snapshots:  # raw as_stated scanned; positions and rules only
        report["elsewhere"] = [{key: s[key] for key in ("place", "body", "decided_on")} for s in snapshots]
        flags += legal_notes((f"--elsewhere[{n}]", s["as_stated"]) for n, s in enumerate(snapshots, 1))
    if flags:  # advisory only: positions and rule names, never the text; the kit is written either way
        report["review_flags"] = flags
    return report


def _ask_notes(custom, label):
    """review-required notes for the resolved custom asks; a guard fault is a note, never a failure."""
    if custom is None:
        return []
    try:
        return legal_notes((f"{label}[{n}]", ask) for n, ask in enumerate(custom, 1))
    except Exception:  # noqa: BLE001 - alert-only
        return ["review-required: legal-claim wording (guard-error)"]


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
