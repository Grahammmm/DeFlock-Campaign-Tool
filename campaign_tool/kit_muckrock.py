"""Map a MuckRock request JSON the organizer saved by hand into a proposed kit-response step. Reads one local file
and kit/sent.json; nothing is fetched or sent, and no MuckRock account or token is used.
See docs/KIT-MUCKROCK-IMPORT.md for how a re-import is compared with the request's last step."""
import json
import errno
import os
import re
import stat
from datetime import datetime
from pathlib import Path

from . import kit_track

MAX_BYTES = 1 << 20
# MuckRock request status -> (kit step kind, partial, note, uses datetime_done); or a reason there is no step.
STEPS = {"processed": ("acknowledged", False, None, False), "fix": ("clarification_requested", False, None, False),
         "payment": ("fee_estimate", False, None, False), "rejected": ("denied", False, None, True),
         "no_docs": ("closed", False, "no responsive documents (MuckRock)", True),
         "partial": ("produced", True, None, True), "done": ("produced", False, None, True),
         "abandoned": ("closed", False, "withdrawn (MuckRock)", True)}
NO_STEP = {**dict.fromkeys(("started", "submitted", "ack"), "waiting for the agency"),
           **dict.fromkeys(("appealing", "lawsuit"), "record by hand")}


def _open_leaf(path):  # one non-following, nonblocking descriptor, admitted by fstat; the stream owns it on return
    flags = getattr(os, "O_NOFOLLOW", None), getattr(os, "O_NONBLOCK", None)
    if None in flags:
        raise ValueError(f"{path}: safe non-symlink file reads are unavailable")
    try:
        fd = os.open(path, os.O_RDONLY | flags[0] | flags[1])
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR):
            raise ValueError(f"{path}: not a regular file (symlinks are refused)") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path}: not a regular file (symlinks are refused)")
        if info.st_size > MAX_BYTES:
            raise ValueError(f"{path}: larger than 1 MiB")
        stream = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    return stream


class _Duplicate(Exception):  # private: carries no key, value or location, so nothing from the file can be echoed
    pass


def _members(pairs):  # json.loads keeps the last repeated member silently; refuse any repeated decoded name instead
    members = {}
    for key, value in pairs:
        if key in members:
            raise _Duplicate
        members[key] = value
    return members


def _load(path):  # a regular, small, UTF-8 JSON object without repeated members; refused before kit/sent.json is read
    path = Path(path)
    if not os.path.lexists(path) or path.is_symlink() or not path.is_file():
        raise ValueError(f"{path}: not a regular file (symlinks are refused)")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"{path}: larger than 1 MiB")
    with _open_leaf(path) as handle:  # the checks above are advisory; the descriptor is the authority
        raw = handle.read(MAX_BYTES + 1)  # bounded: the file may have grown since the descriptor check
    if len(raw) > MAX_BYTES:
        raise ValueError(f"{path}: larger than 1 MiB")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_members)
    except _Duplicate:  # at any object depth, equal or contradictory alike: ambiguous, never first- or last-wins
        raise ValueError(f"{path}: duplicate JSON keys") from None
    except RecursionError:  # translate decoder exhaustion, without imposing a separate depth limit
        raise ValueError(f"{path}: JSON nesting is too deep") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"{path}: not UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise ValueError(f"{path}: expected one JSON object (a single request)")
    return document


# YYYY-MM-DD, optionally then T/t/space and HH:MM[:SS[.fraction]] with an optional Z or +/-HH:MM zone; ASCII only, whole value.
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[Tt ](?:[01][0-9]|2[0-3]):[0-5][0-9]"
                   r"(?::[0-5][0-9](?:\.[0-9]+)?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?)?")


def _complete(value):  # the whole string is an admitted date/datetime, not just its first ten characters
    if _TIME.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _date(document, *fields):  # the written date of the first field present (no UTC conversion); refused when none or malformed
    for field in fields:
        value = document.get(field)
        if value not in (None, ""):
            day = kit_track._day(value[:10]) if isinstance(value, str) and _complete(value) else None
            if day is None:
                raise ValueError(f"{field} must be an ISO 8601 date or datetime")
            return day.isoformat()
    raise ValueError(f"the JSON has no {' or '.join(fields)} to date the step")


def _price(value):  # plain USD text for the normal --amount rule; None when the JSON carries no price
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("price must be a decimal string or number")
    text = value if isinstance(value, str) else str(value)
    kit_track._amount_cents(text)  # the --amount rule refuses a sign, exponent, NaN or a value over the limit, before any skip
    return text


def _same_mapped_step(last, proposed):  # the request's last step already says what the saved JSON would add
    kind, partial = proposed["kind"], bool(proposed.get("partial"))
    if (last.get("kind"), last.get("on"), bool(last.get("partial"))) != (kind, proposed["on"], partial):
        return False
    if kind == "fee_estimate":
        return last.get("amount_cents") == kit_track._amount_cents(proposed["amount"])
    if kind == "closed":
        return last.get("note") == proposed.get("note")
    return True


def propose(root, agency_id, path, request="main"):
    """The step the saved JSON implies for the kit's MuckRock request: a dict with ``step`` (a record_step keyword
    set, or None) and ``reason``. Raises ValueError (nothing written) when the file or the status is refused."""
    document = _load(path)
    request_id, status = document.get("id"), document.get("status")
    if type(request_id) is not int or not isinstance(status, str):
        raise ValueError("the JSON needs an integer id and a text status")
    request = kit_track._request(request)
    entry = None if "#" in agency_id else kit_track.read_sent(root).get(kit_track._key(agency_id, request))
    if entry is None:
        raise ValueError(f"no hand-sent request recorded for {kit_track._label(agency_id, request)}; run kit-sent first")
    kit_id = kit_track.muckrock_id(entry)
    if kit_id is None:
        raise ValueError(f"{kit_track._label(agency_id, request)} was not recorded with --channel muckrock and a request URL")
    if str(request_id) != kit_id:
        raise ValueError(f"this JSON is request {request_id}, not {kit_id}")
    base = {"agency_id": agency_id, "request": request, "muckrock_id": request_id, "muckrock_status": status}
    if status in NO_STEP:
        return {**base, "step": None, "reason": NO_STEP[status]}
    if status not in STEPS:
        raise ValueError(f"unknown MuckRock status {status!r}; record the step by hand with kit-response")
    kind, partial, note, finished = STEPS[status]
    on = _date(document, *(("datetime_done",) if finished else ()), "datetime_updated")
    amount = None
    if kind == "fee_estimate":
        amount = _price(document.get("price"))
        if amount is None:
            return {**base, "step": None, "reason": "no price in the JSON; record the estimate by hand"}
    step = {"on": on, "kind": kind, **({"partial": True} if partial else {}), **({"note": note} if note else {}),
            **({"amount": amount} if amount is not None else {})}
    last = entry["steps"][-1] if entry["steps"] else {}
    if _same_mapped_step(last, step):
        return {**base, "step": None, "reason": "already recorded"}
    return {**base, "step": step, "reason": None}


def run(root, agency_id, path, request="main", apply=False):
    """``propose`` plus the normal step rules: a dry run by default, one ``record_step`` write with ``apply``.
    The result carries ``recorded`` (the entry as it is or would be) or ``refusal`` (the step rule that stops it)."""
    result = propose(root, agency_id, path, request)
    if result["step"] is None:
        return {**result, "applied": False}
    step = result["step"]
    try:
        entry = kit_track.record_step(root, agency_id, step["on"], step["kind"], note=step.get("note"),
                                      amount=step.get("amount"), partial=bool(step.get("partial")), request=request,
                                      dry_run=not apply)
    except ValueError as exc:
        return {**result, "applied": False, "refusal": str(exc)}
    return {**result, "applied": apply, "recorded": entry}


def render(result):  # the plain-text proposal printed by kit-muckrock-import
    lines = [f"MuckRock request {result['muckrock_id']} has status '{result['muckrock_status']}'."]
    step = result["step"]
    if step is None:
        return "\n".join(lines + [f"Nothing to do: {result['reason']}."])
    flags = "".join(f" --{k} {step[k]}" for k in ("amount", "note") if k in step) + " --partial" * bool(step.get("partial"))
    lines.append(f"Proposed: kit-response --agency {result['agency_id']} --kind {step['kind']} --date {step['on']}{flags}")
    lines.append(f"Refused by the normal step rules: {result['refusal']}" if "refusal" in result else
                 "Recorded." if result["applied"] else "Dry run: nothing written. Add --apply to record it.")
    return "\n".join(lines)
