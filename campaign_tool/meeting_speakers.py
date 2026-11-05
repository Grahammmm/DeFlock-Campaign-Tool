"""Private speaker coverage plan for one staged meeting (DF2207-15): kit/meetings/speakers/<id>.json (0600).

Each pair is a volunteer alias and one point of the meeting's staged comment kit (a cited finding id or a numbered ask).
Aliases are labels the team chose, not verified identities and not consent. The only time limit shown is the staged
meeting's organizer-verified ``participation.speaker_minutes``. Nothing is sent, published, staged or exported here.
"""
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .content import FINDING_ID, SHA256, SLUG, load_comment_asks, load_meeting
from .kit_assign import owner_label
from .meetings import ASK_COUNT_WORDS

MAX_PAIRS = 200
MAX_STAGED_BYTES = 4 * 1024 * 1024
MAX_STORE_BYTES = 1 << 20
ASK_POINT = re.compile(r"ask:[1-3]")
CANCELLED = "meeting cancelled; no speaker plan"
PAST = "meeting is past or has started; no speaker plan"
UNKNOWN_POINT = "unknown point in this meeting's comment kit"
UNAVAILABLE = "comment kit points unavailable; re-stage the meeting"
STALE = "speaker plan is stale; remove its labels and add them again"
SYMLINK = "symlink on speaker-plan path; use real directories and files"
BAD_STORE = "speaker plan is not valid private JSON"
NOT_STAGED = "meeting not staged; run meetings-stage first"


class MeetingSpeakersError(ValueError):
    pass


def _pairs_hook(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _check_chain(root, parts):
    """Refuse a symlink on the campaign root, its ancestors or any listed component; stop at the first missing one."""
    here = Path(os.path.abspath(root))
    for ancestor in [here, *here.parents]:
        if ancestor.is_symlink():
            raise MeetingSpeakersError(SYMLINK)
    for part in parts:
        here = here / part
        if here.is_symlink():
            raise MeetingSpeakersError(SYMLINK)
        if not os.path.lexists(here):
            return


def _read_regular(path, limit, absent_ok, on_bad):
    """The bytes of a regular, unlinked-once file; None when absent and ``absent_ok``. Never follows a link."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if absent_ok:
            return None
        raise on_bad from None
    if stat.S_ISLNK(info.st_mode):
        raise MeetingSpeakersError(SYMLINK)
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_nlink != 1:
        raise on_bad
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if os.fstat(fd).st_size > limit:
            raise on_bad
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(limit + 1)
    finally:
        os.close(fd)


def _json(data, on_bad):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs_hook)
    except (UnicodeDecodeError, ValueError):
        raise on_bad from None


def _meeting_id(meeting_id):
    if not isinstance(meeting_id, str) or not SLUG.fullmatch(meeting_id):
        raise MeetingSpeakersError(NOT_STAGED)
    return meeting_id


def _staged(root, meeting_id, now):
    """(loaded meeting, raw comment kit text) after every gate; raises for cancelled, past or unstaged."""
    meeting_id = _meeting_id(meeting_id)
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise MeetingSpeakersError("now must be a timezone-aware datetime")
    _check_chain(root, ["content", "meetings", f"{meeting_id}.json"])
    path = Path(root) / "content" / "meetings" / f"{meeting_id}.json"
    bad = MeetingSpeakersError(f"content/meetings/{meeting_id}.json is not a regular UTF-8 JSON file up to 4 MiB")
    data = _read_regular(path, MAX_STAGED_BYTES, True, bad)
    if data is None:
        raise MeetingSpeakersError(NOT_STAGED)
    document = _json(data, bad)
    if not isinstance(document, dict) or document.get("id") != meeting_id:
        raise MeetingSpeakersError(f"content/meetings/{meeting_id}.json: id must match the file name")
    loaded = load_meeting(document, f"meetings/{meeting_id}.json", today=now.date())
    if loaded["status"] == "cancelled":
        raise MeetingSpeakersError(CANCELLED)
    _check_imported(root, meeting_id)
    starts = datetime.fromisoformat(loaded["starts_at"].replace("Z", "+00:00"))
    if starts <= now:
        raise MeetingSpeakersError(PAST)
    return loaded, document["comment_kit_md"] if isinstance(document.get("comment_kit_md"), str) else ""


def _check_imported(root, meeting_id):
    """A matching cancelled entry in kit/meetings.json wins over older staged content; a missing import is fine."""
    _check_chain(root, ["kit", "meetings.json"])
    bad = MeetingSpeakersError("kit/meetings.json is not a regular UTF-8 JSON file up to 4 MiB")
    data = _read_regular(Path(root) / "kit" / "meetings.json", MAX_STAGED_BYTES, True, bad)
    if data is None:
        return
    document = _json(data, bad)
    entries = document.get("meetings") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise bad
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == meeting_id and entry.get("status") == "cancelled":
            raise MeetingSpeakersError(CANCELLED)


def _ask_points(kit_md):
    unavailable = MeetingSpeakersError(UNAVAILABLE)
    lines = kit_md.split("\n")
    heads = {f"## {words}": count for count, words in ASK_COUNT_WORDS.items()}
    starts = [i for i, line in enumerate(lines) if line in heads]
    if len(starts) != 1:
        raise unavailable
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    texts = []
    for line in lines[start + 1:end]:
        if not line.strip():
            continue
        if not line.startswith("- ") or not line[2:].strip():
            raise unavailable
        texts.append(line[2:])
    if len(texts) != heads[lines[start]]:
        raise unavailable
    try:
        return load_comment_asks(texts, "asks")
    except ValueError:
        raise unavailable from None


def meeting_points(root, meeting_id, *, now=None):
    """Ordered ``[{point, text}]``: the staged cited findings, then the staged comment kit's asks."""
    loaded, kit_md = _staged(root, meeting_id, now)
    return _points(loaded, kit_md)


def _points(loaded, kit_md):
    cited = loaded.get("cited_findings")
    if cited is None:
        sources = kit_md.split("\n## Sources", 1)[1] if "\n## Sources" in kit_md else ""
        if any(line.startswith("- **") for line in sources.split("\n")):
            raise MeetingSpeakersError(UNAVAILABLE)  # a legacy kit that cites findings but never stored which
        cited = []
    points = [{"point": f"finding:{fid}", "text": fid} for fid in cited]
    return points + [{"point": f"ask:{n}", "text": text} for n, text in enumerate(_ask_points(kit_md), 1)]


def _point_ok(point):
    return isinstance(point, str) and (bool(ASK_POINT.fullmatch(point)) or (
        point.startswith("finding:") and bool(FINDING_ID.fullmatch(point[8:]))))


def _store_path(root, meeting_id):
    return Path(root) / "kit" / "meetings" / "speakers" / f"{meeting_id}.json"


def read_plan(root, meeting_id):
    """``(kit_sha256 or None, [(label, point), ...])`` from the private file; empty when absent. Never repairs it."""
    meeting_id = _meeting_id(meeting_id)
    _check_chain(root, ["kit", "meetings", "speakers", f"{meeting_id}.json"])
    bad = MeetingSpeakersError(BAD_STORE)
    data = _read_regular(_store_path(root, meeting_id), MAX_STORE_BYTES, True, bad)
    if data is None:
        return None, []
    document = _json(data, bad)
    if (not isinstance(document, dict) or set(document) != {"schema_version", "meeting_id", "kit_sha256", "assignments"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or document["meeting_id"] != meeting_id or not isinstance(document["kit_sha256"], str)
            or not SHA256.fullmatch(document["kit_sha256"]) or not isinstance(document["assignments"], list)
            or len(document["assignments"]) > MAX_PAIRS):
        raise bad
    pairs = []
    for item in document["assignments"]:
        if not isinstance(item, dict) or set(item) != {"label", "point"} or not _point_ok(item["point"]):
            raise bad
        try:
            if owner_label(item["label"], "--add") != item["label"]:
                raise bad
        except ValueError:
            raise bad from None
        pairs.append((item["label"], item["point"]))
    if len(set(pairs)) != len(pairs):
        raise bad
    return document["kit_sha256"], sorted(pairs)


def _label(value, flag):
    try:
        return owner_label(value, flag)
    except ValueError as exc:
        raise MeetingSpeakersError(str(exc)) from None


def _write(root, meeting_id, digest, pairs):
    base = Path(root) / "kit"
    for folder, mode in ((base, 0o700), (base / "meetings", 0o700), (base / "meetings" / "speakers", 0o700)):
        if folder.is_symlink():
            raise MeetingSpeakersError(SYMLINK)
        if not folder.exists():
            folder.mkdir(mode=mode)
        elif not folder.is_dir():
            raise MeetingSpeakersError(BAD_STORE)
    target = _store_path(root, meeting_id)
    if target.is_symlink():
        raise MeetingSpeakersError(SYMLINK)
    document = {"schema_version": 1, "meeting_id": meeting_id, "kit_sha256": digest,
                "assignments": [{"label": label, "point": point} for label, point in sorted(pairs)]}
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix="." + target.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def update_plan(root, meeting_id, *, add=None, point=None, remove=None, now=None):
    """Add one label/point pair or remove every pair of one label; returns ``{id, changed, assignments}``."""
    if (add is None) == (remove is None) or (add is None) != (point is None):
        raise MeetingSpeakersError("give either add with point, or remove")
    loaded, kit_md = _staged(root, meeting_id, now)
    digest = hashlib.sha256(kit_md.encode("utf-8")).hexdigest()
    if add is not None:
        label = _label(add, "--add")
        catalog = {entry["point"] for entry in _points(loaded, kit_md)}
        if not _point_ok(point) or point not in catalog:
            raise MeetingSpeakersError(UNKNOWN_POINT)
    else:
        label = _label(remove, "--remove")
    stored, pairs = read_plan(root, meeting_id)
    if add is not None:
        if pairs and stored != digest:
            raise MeetingSpeakersError(STALE)
        if (label, point) in pairs:
            return {"id": meeting_id, "changed": False, "assignments": len(pairs)}
        if len(pairs) >= MAX_PAIRS:
            raise MeetingSpeakersError(f"speaker plan is full ({MAX_PAIRS} pairs)")
        pairs, binding = pairs + [(label, point)], digest
    else:
        kept = [pair for pair in pairs if pair[0] != label]
        if len(kept) == len(pairs):
            return {"id": meeting_id, "changed": False, "assignments": len(pairs)}
        pairs, binding = kept, (stored if kept else digest)  # leftover pairs keep their old binding
    _write(root, meeting_id, binding, pairs)
    return {"id": meeting_id, "changed": True, "assignments": len(pairs)}


def list_plan(root, meeting_id, *, now=None):
    """Every point with its speakers, the uncovered points and the labels on two or more points. Read-only."""
    loaded, kit_md = _staged(root, meeting_id, now)
    points = _points(loaded, kit_md)
    stored, pairs = read_plan(root, meeting_id)
    if pairs and stored != hashlib.sha256(kit_md.encode("utf-8")).hexdigest():
        raise MeetingSpeakersError(STALE)
    order = [entry["point"] for entry in points]
    if any(point not in order for _, point in pairs):
        raise MeetingSpeakersError(STALE)
    shown = [{**entry, "speakers": sorted(label for label, point in pairs if point == entry["point"])}
             for entry in points]
    labels = {}
    for label, point in pairs:
        labels.setdefault(label, []).append(point)
    return {"id": meeting_id,
            "speaker_minutes": (loaded.get("participation") or {}).get("speaker_minutes"),
            "points": shown,
            "uncovered": [entry["point"] for entry in shown if not entry["speakers"]],
            "multiple_points": [{"label": label, "points": sorted(found, key=order.index)}
                                for label, found in sorted(labels.items()) if len(found) >= 2]}
