"""DF1109-12: ``officials add|recheck|remove|list`` maintain ``content/officials.json`` without hand-editing.

The roster is the one the site build reads (DF2237-13): ``{"schema_version": 1, "bodies": [{"agency_id", "officials":
[{"name", "role", "profile_url"?, "source_url", "checked_on"}]}]}``. Every change is validated as a whole prospective file
with the build's own ``content.load_officials`` before one atomic write in ``content/``; a refusal leaves the old bytes.

* add appends one person to a body (or one new body) and refuses a name already in that body (NFC, whitespace runs
  collapsed, case-sensitive: the rule ``meetings-outcome --votes`` matches names by). It alone creates the file.
* recheck moves one person's ``checked_on`` forward to a day the organizer re-checked the official source (never back,
  never in the future, at most 365 days old). The same day is a no-op that writes nothing.
* remove drops one person, refused while a recorded meeting vote or a ``content/statements.json`` row names them; a body
  left empty is dropped and the last removal leaves ``"bodies": []``.
* list prints the roster in stored order with each ``checked_on`` age against the build's 365-day rule; it writes nothing.

Listing and re-checking read the roster without the 365-day limit (``require_fresh=False``) so a stale roster can be
repaired one person at a time; the build itself stays strict. Nothing is fetched: a source address is the organizer's
record of where they checked, never verified here. Refusals name the flag or stored place, never a supplied value.
One organizer writes at a time; there is no concurrent-writer promise. Each write is bound to the content/ folder (device and
inode) its roster, site and agencies were read from, checked before staging and again before publishing: a folder
replaced by another one is refused, never published into.
"""
import json
import os
import re
import stat
import unicodedata
from datetime import date
from pathlib import Path

from .content import (MAX_JSON_BYTES, ContentError, _calendar_day, default_agencies, load_agencies, load_meeting,
                      load_officials, load_site)
from .privacy_patterns import refusal

FILE = "officials.json"
LABEL = "content/" + FILE
STALE_AFTER = 365
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
UNTOUCHED = "nothing was written"
DONE = "Recorded only. Nothing was fetched, built, published or sent."


class OfficialsError(ContentError):
    """The roster, or a requested change to it, is not acceptable; the message never repeats a supplied value."""


class _Duplicate(ValueError):
    pass


def same_name(name):
    """The ambiguity key within one body: NFC, whitespace runs collapsed, case kept (as ``meetings-outcome --votes``)."""
    return " ".join(unicodedata.normalize("NFC", name).split())


# --- reading (anchored at content/, links never followed) ------------------------------------------------------

def _content_fd(root):
    try:
        return os.open(Path(root) / "content", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise OfficialsError("content/ must be a real folder of this campaign (links are never followed); "
                             + UNTOUCHED) from None


def _read(folder_fd, name, label):
    """-> (bytes, stat) of one regular file in the folder, or None when absent; a link, FIFO, folder or oversized file is
    refused unread."""
    try:
        info = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise OfficialsError(f"{label}: cannot be inspected") from None
    if stat.S_ISLNK(info.st_mode):
        raise OfficialsError(f"{label}: is a symbolic link; links are never followed")
    if not stat.S_ISREG(info.st_mode):
        raise OfficialsError(f"{label}: must be a regular file")
    if info.st_size > MAX_JSON_BYTES:
        raise OfficialsError(f"{label}: file too large")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            data = handle.read(MAX_JSON_BYTES + 1)
    except OSError:
        raise OfficialsError(f"{label}: cannot be read") from None
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        raise OfficialsError(f"{label}: changed while it was being read")
    if len(data) > MAX_JSON_BYTES:
        raise OfficialsError(f"{label}: file too large")
    return data, opened


def _hook(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _Duplicate(key)
        seen[key] = value
    return seen


def _parse(data, label):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_hook)
    except _Duplicate:
        raise OfficialsError(f"{label}: a JSON object repeats a key") from None
    except (ValueError, RecursionError):
        raise OfficialsError(f"{label}: not valid UTF-8 JSON") from None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _folder_identity(fd):
    """The (device, inode) of an open folder: what stays the same while staging changes its size and mtime."""
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino)


def _state(root, today, *, with_folder_identity=False):
    """-> (agencies, roster document or None, loaded (identity, mode) or None) for a campaign with a valid site.json.

    With ``with_folder_identity`` a fourth item is the identity of the very content/ folder those files were read from,
    so a writer can refuse to publish into a different folder later (statements.py keeps the three-item default)."""
    root = Path(root)
    folder = _content_fd(root)
    try:
        folder_identity = _folder_identity(folder)
        site = _read(folder, "site.json", "content/site.json")
        if site is None:
            raise OfficialsError("content/site.json is required; " + UNTOUCHED)
        load_site(_parse(site[0], "content/site.json"), root / "content" / "public-assets")
        stored = _read(folder, "agencies.json", "content/agencies.json")
        agencies = (default_agencies(root, today) if stored is None else
                    load_agencies(_parse(stored[0], "content/agencies.json"), "content/agencies.json", today))
        roster = _read(folder, FILE, LABEL)
    finally:
        os.close(folder)
    if roster is None:
        state = (agencies, None, None)
    else:
        state = (agencies, _parse(roster[0], LABEL), (_identity(roster[1]), stat.S_IMODE(roster[1].st_mode)))
    return (*state, folder_identity) if with_folder_identity else state


def _validate(document, agencies, today, *, fresh):
    try:
        return load_officials(document, agencies, today, require_fresh=fresh)
    except ContentError as exc:
        raise OfficialsError(f"{exc}; {UNTOUCHED}") from None


def _existing(root, today, *, with_folder_identity=False):
    """The state of a roster that must already exist, validated without the age limit."""
    state = _state(root, today, with_folder_identity=with_folder_identity)
    agencies, document, loaded = state[:3]
    if loaded is None:
        raise OfficialsError(f"{LABEL}: does not exist; add an official first ({UNTOUCHED})")
    _validate(document, agencies, today, fresh=False)
    return state


def _day(value, flag):
    if not (isinstance(value, str) and DAY.fullmatch(value)):
        raise OfficialsError(f"{flag}: must be a real YYYY-MM-DD date; {UNTOUCHED}")
    try:
        return _calendar_day(value, flag)
    except ContentError:
        raise OfficialsError(f"{flag}: must be a real YYYY-MM-DD date; {UNTOUCHED}") from None


def _body(document, agency_id):
    return next((body for body in document["bodies"] if body["agency_id"] == agency_id), None)


def _target(document, agency_id, name):
    """-> (body, index) of the one person stored under exactly this agency and name."""
    body = _body(document, agency_id)
    found = [n for n, person in enumerate(body["officials"]) if person["name"] == name] if body else []
    if not found:
        raise OfficialsError("--name: no official with exactly this name is listed for this --agency "
                             f"(names match exactly; see officials list); {UNTOUCHED}")
    if len(found) > 1:
        raise OfficialsError(f"--name: this exact name is listed more than once for this --agency; edit {LABEL} by hand "
                             f"to tell them apart; {UNTOUCHED}")
    return body, found[0]


def _age(person, today):
    return (today - _calendar_day(person["checked_on"], "checked_on")).days


# --- writing ---------------------------------------------------------------------------------------------------

def _dump(document):
    data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise OfficialsError(f"{LABEL}: would be larger than {MAX_JSON_BYTES // 1024} KiB; {UNTOUCHED}")
    return data


FOLDER_CHANGED = "content/: changed while it was being edited; " + UNTOUCHED


def _folder_changed(root, expected_folder):
    """Refuse unless root/content is still, as a real folder, the one whose files were read (never follows a link)."""
    try:
        check = os.open(Path(root) / "content", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise OfficialsError(FOLDER_CHANGED) from None
    try:
        same = _folder_identity(check) == expected_folder
    finally:
        os.close(check)
    if not same:
        raise OfficialsError(FOLDER_CHANGED)


def _install(root, data, loaded, *, expected_folder):
    """Stage ``data`` beside the roster, fsync, then publish it with one rename (or a no-replace link for the first
    file). The folder must still be the very content/ folder that was read (device and inode, checked before staging
    and again before publishing), and the target must still be the very file read, or still absent; every ordinary
    failure keeps the old bytes. Not a promise against a namespace change after the final check."""
    folder = _content_fd(root)
    temp = f".officials-{os.getpid()}-{os.urandom(4).hex()}.tmp"
    staged = False
    committed = False  # True once the rename/link has published the new roster: from then on the old bytes are gone
    try:
        if _folder_identity(folder) != expected_folder:
            raise OfficialsError(FOLDER_CHANGED)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        staged = True
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600 if loaded is None else loaded[1])
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _folder_changed(root, expected_folder)
        try:
            current = os.stat(FILE, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if loaded is None:
            if current is not None:
                raise FileExistsError
            os.link(temp, FILE, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)  # never replaces
            committed = True
        else:
            if current is None or not stat.S_ISREG(current.st_mode) or _identity(current) != loaded[0]:
                raise FileExistsError
            os.replace(temp, FILE, src_dir_fd=folder, dst_dir_fd=folder)
            committed = True
            staged = False
        os.fsync(folder)
    except FileExistsError:
        raise OfficialsError(f"{LABEL}: changed while it was being edited; {UNTOUCHED}") from None
    except OSError:
        if committed:  # the new roster is already in place; only its durability could not be confirmed
            raise OfficialsError(f"{LABEL}: the new roster was written but the folder could not be flushed to disk; "
                                 "check the file with officials list before relying on it") from None
        raise OfficialsError(f"{LABEL}: write failed; the previous file was kept") from None
    finally:
        if staged:
            try:
                os.unlink(temp, dir_fd=folder)
            except OSError:
                pass
        os.close(folder)


# --- commands --------------------------------------------------------------------------------------------------

def add_official(root, *, agency_id, name, role, source_url, checked_on, profile_url=None, today=None):
    """Append one organizer-checked official to a body. -> {"agency_id", "position" (1-based), "created"}."""
    today = today or date.today()
    agencies, document, loaded, folder = _state(root, today, with_folder_identity=True)
    if loaded is not None:  # a stored JSON null is a broken file, never an absent one
        _validate(document, agencies, today, fresh=False)
    if agency_id not in {agency["agency_id"] for agency in agencies}:
        raise OfficialsError(f"--agency: not a loaded agency (content/agencies.json or the kit's agency list); {UNTOUCHED}")
    fields = (("--name", name), ("--role", role), ("--profile-url", profile_url), ("--source-url", source_url))
    for flag, value in fields:
        if value is not None and (found := refusal(flag, value)):
            raise OfficialsError(f"{found}; {UNTOUCHED}")
    _day(checked_on, "--checked-on")
    document = document if loaded is not None else {"schema_version": 1, "bodies": []}
    body = _body(document, agency_id)
    if body is not None and isinstance(name, str) and any(same_name(person["name"]) == same_name(name)
                                                          for person in body["officials"]):
        raise OfficialsError(f"--name: {agency_id} already lists an official with this name (compared after Unicode "
                             f"NFC and whitespace collapsing, case-sensitive); {UNTOUCHED}")
    person = {"name": name, "role": role, **({"profile_url": profile_url} if profile_url is not None else {}),
              "source_url": source_url, "checked_on": checked_on}
    if body is None:
        body = {"agency_id": agency_id, "officials": []}
        document["bodies"].append(body)
    body["officials"].append(person)
    _validate(document, agencies, today, fresh=True)
    _install(root, _dump(document), loaded, expected_folder=folder)
    return {"agency_id": agency_id, "position": len(body["officials"]), "created": loaded is None}


def recheck_official(root, *, agency_id, name, checked_on, today=None):
    """Move one official's ``checked_on`` forward after the organizer re-checked the official source.
    -> {"agency_id", "position", "changed", "still_stale"} (still_stale counts the others older than 365 days)."""
    today = today or date.today()
    agencies, document, loaded, folder = _existing(root, today, with_folder_identity=True)
    new = _day(checked_on, "--checked-on")
    if new > today:
        raise OfficialsError(f"--checked-on: must not be after today; {UNTOUCHED}")
    if (today - new).days > STALE_AFTER:
        raise OfficialsError(f"--checked-on: older than {STALE_AFTER} days; re-check on the official site; {UNTOUCHED}")
    body, index = _target(document, agency_id, name)
    person = body["officials"][index]
    old = _calendar_day(person["checked_on"], "checked_on")
    if new < old:
        raise OfficialsError(f"--checked-on: earlier than the stored checked_on; a re-check never moves back; {UNTOUCHED}")
    stale = sum(1 for other in document["bodies"] for someone in other["officials"]
                if someone is not person and _age(someone, today) > STALE_AFTER)
    result = {"agency_id": agency_id, "position": index + 1, "changed": new != old, "still_stale": stale}
    if new == old:
        return result
    person["checked_on"] = checked_on
    _validate(document, agencies, today, fresh=False)
    _validate({"schema_version": 1, "bodies": [{"agency_id": agency_id, "officials": [person]}]}, agencies, today,
              fresh=True)
    _install(root, _dump(document), loaded, expected_folder=folder)
    return result


def _meetings(root, today):
    """Every staged meeting the build would read, validated; a link, FIFO or broken file is refused, never skipped."""
    folder = _content_fd(root)
    try:
        try:
            meetings_fd = os.open("meetings", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
        except FileNotFoundError:
            return []
        except OSError:
            raise OfficialsError("content/meetings: must be a real folder (links are never followed); "
                                 + UNTOUCHED) from None
    finally:
        os.close(folder)
    try:
        meetings = []
        for name in sorted(os.listdir(meetings_fd)):
            if not name.endswith(".json"):  # the build's Path.glob("*.json") reads dot-prefixed names too (RF0145-4)
                continue
            label = f"content/meetings/{name}"
            loaded = _read(meetings_fd, name, label)
            if loaded is None:
                raise OfficialsError(f"{label}: changed while it was being read")
            try:
                meeting = load_meeting(_parse(loaded[0], label), "meetings/" + name, today=today)
            except ContentError as exc:
                raise OfficialsError(f"{exc}; {UNTOUCHED}") from None
            meetings.append((name[:-len(".json")], meeting))
        return meetings
    finally:
        os.close(meetings_fd)


def _votes(meetings, agency_id):
    return [(stem, vote["name"]) for stem, meeting in meetings if meeting["agency_id"] == agency_id
            for vote in (meeting["outcome"] or {}).get("votes", [])]


def remove_official(root, *, agency_id, name, today=None):
    """Drop one official, refused while a recorded vote or statement names them. -> {"agency_id", "body_dropped"}."""
    from .content_statements import check_statements, read_statements  # local: content_statements imports content
    today = today or date.today()
    agencies, document, loaded, folder = _existing(root, today, with_folder_identity=True)
    body, index = _target(document, agency_id, name)
    meetings = _meetings(root, today)
    try:
        statements = read_statements(root, load_officials(document, agencies, today, require_fresh=False), today) or []
    except ContentError as exc:
        raise OfficialsError(f"{exc}; {UNTOUCHED}") from None
    places = sorted({f"a recorded vote in content/meetings/{stem}.json" for stem, voter in _votes(meetings, agency_id)
                     if voter == name})
    places += [f"statements.json[{n}]" for n, row in enumerate(statements)
               if row["agency_id"] == agency_id and row["official"] == name]
    if places:
        raise OfficialsError(f"--name: still named by {', '.join(places)}; recorded votes and statements are never "
                             f"rewritten, so the official stays listed; {UNTOUCHED}")
    del body["officials"][index]
    dropped = not body["officials"]
    if dropped:
        document["bodies"].remove(body)
    prospective = _validate(document, agencies, today, fresh=True)
    listed = {person["name"] for person in prospective.get(agency_id, [])}  # every reference still resolves
    problems = [f"content/meetings/{stem}.json: a recorded vote would no longer name a listed official"
                for stem, voter in _votes(meetings, agency_id) if voter not in listed]
    problems += check_statements(statements, prospective, today)[1]
    if problems:
        raise OfficialsError(f"after the removal {problems[0]}; {UNTOUCHED}")
    _install(root, _dump(document), loaded, expected_folder=folder)
    return {"agency_id": agency_id, "body_dropped": dropped}


def list_officials(root, *, today=None):
    """The roster in stored order with each check's age; None when there is no roster file. Writes nothing."""
    today = today or date.today()
    agencies, document, loaded = _state(root, today)
    if loaded is None:
        return None
    _validate(document, agencies, today, fresh=False)
    rows = []
    for body in document["bodies"]:
        for person in body["officials"]:
            age = _age(person, today)
            rows.append({"agency_id": body["agency_id"], "name": person["name"], "role": person["role"],
                         "profile_url": person.get("profile_url"), "source_url": person["source_url"],
                         "checked_on": person["checked_on"], "age_days": age,
                         "status": "current" if age <= STALE_AFTER else "stale"})
    return {"as_of": today.isoformat(), "bodies": len(document["bodies"]), "officials": rows,
            "stale": sum(1 for row in rows if row["status"] == "stale")}


def run_cli(root, args):
    """-> the text the CLI prints for one ``officials`` verb (date.today(); no override)."""
    if args.officials_command == "list":
        report = list_officials(root)
        if not report or not report["officials"]:
            return "No officials listed."
        summary = (f"{len(report['officials'])} official(s) in {report['bodies']} body/bodies; {report['stale']} stale "
                   f"(checked more than {STALE_AFTER} days ago; the build refuses until each is re-checked).")
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n" + summary + "\nListed only. Nothing was fetched or written."
    if args.officials_command == "add":
        done = add_official(root, agency_id=args.agency, name=args.name, role=args.role, source_url=args.source_url,
                            checked_on=args.checked_on, profile_url=args.profile_url)
        lines = [f"Official added to {done['agency_id']} (position {done['position']})"
                 + (f"; created {LABEL}." if done["created"] else ".")]
    elif args.officials_command == "recheck":
        done = recheck_official(root, agency_id=args.agency, name=args.name, checked_on=args.checked_on)
        lines = [f"checked_on {'updated' if done['changed'] else 'unchanged (already that day)'} for {done['agency_id']} "
                 f"position {done['position']}."]
        if done["still_stale"]:
            lines.append(f"Warning: {done['still_stale']} other official(s) are still older than {STALE_AFTER} days; "
                         "re-check each with officials recheck.")
    else:
        done = remove_official(root, agency_id=args.agency, name=args.name)
        lines = [f"Official removed from {done['agency_id']}"
                 + ("; the body had no one left and was dropped." if done["body_dropped"] else ".")]
    return "\n".join(lines + [DONE])
