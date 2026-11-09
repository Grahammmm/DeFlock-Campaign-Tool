"""DF1422-09: a governing body's public iCalendar (RFC 5545) agenda feed, read as one more ``meetings`` platform.

Stdlib only. Only ``VEVENT`` blocks are read; ``VTIMEZONE`` blocks are ignored (zone ids resolve through ``zoneinfo``).
A floating time, an unknown zone, a recurring event (``RRULE``, ``RDATE``, ``RECURRENCE-ID``), an over-long UID or an event
without a usable start is skipped and counted. Attachment and agenda links are recorded and never fetched.
"""
import hashlib
import re
import urllib.parse
from datetime import date, datetime, timezone

from .meetings import (ATTACHMENT_BAD, DEFAULT_ZONE, MAX_ATTACHMENTS, MAX_FEED_BYTES, MeetingsError, ZoneInfo, _plain,
                       default_opener, match_items, resolve_zone)

MAX_VEVENTS = 2000
MAX_UID = 200
MAX_URL = 500
MAX_BODY = 120
SKIP_KEYS = ("floating_time", "unknown_zone", "recurring", "uid_too_long", "no_uid", "bad_start")
_BAD_TEXT = re.compile(r"[\x00-\x1f\x7f<>]")
_UNESCAPE = re.compile(r"\\([nN,;\\])")
_SEQUENCE = re.compile(r"[0-9]+")
_DTSTAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z")
MAX_SEQUENCE = 2147483647


def check_feed_url(url):
    """The feed URL: https, no userinfo, at most 500 characters, no whitespace or control characters."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL or ATTACHMENT_BAD.search(url) or not url.startswith("https://"):
        raise MeetingsError("--feed-url must be an https URL of at most 500 characters")
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port
    except ValueError:
        raise MeetingsError("--feed-url is not a valid URL") from None
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise MeetingsError("--feed-url must have a host and no user or password")
    return url


def check_body(body):
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY or _BAD_TEXT.search(body):
        raise MeetingsError("--body must be the governing body's name: plain text, 1-120 characters")
    return " ".join(body.split())


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "body"


def unfold(text):
    """RFC 5545 line unfolding (CRLF or LF, continuation lines start with a space or tab)."""
    lines = []
    for raw in re.split(r"\r\n|\n|\r", text.lstrip("\ufeff")):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def unescape(value):
    return _UNESCAPE.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def split_line(line):
    """``(NAME, {param: value}, value)`` or None; quoted parameter values may contain ``:`` and ``;``."""
    quoted, cut = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            cut = i
            break
    if cut < 1:
        return None
    head, value = line[:cut], line[cut + 1:]
    pieces, current, quoted = [], "", False
    for ch in head:
        if ch == '"':
            quoted = not quoted
        if ch == ";" and not quoted:
            pieces.append(current)
            current = ""
        else:
            current += ch
    pieces.append(current)
    params = {}
    for piece in pieces[1:]:
        key, _, val = piece.partition("=")
        params[key.upper()] = val.strip('"')
    return pieces[0].upper(), params, value


def parse_events(text):
    """List of VEVENTs, each ``{NAME: [(params, value), ...]}``; more than 2000 VEVENTs is refused."""
    events, current, depth = [], None, 0
    for line in unfold(text):
        parsed = split_line(line)
        if parsed is None:
            continue
        name, params, value = parsed
        value_up = value.strip().upper()
        if name == "BEGIN":
            if current is not None:
                depth += 1  # VALARM and the like inside a VEVENT: their properties are not the event's
            elif value_up == "VEVENT":
                if len(events) >= MAX_VEVENTS:
                    raise MeetingsError(f"feed has more than {MAX_VEVENTS} VEVENTs; nothing was written")
                current, depth = {}, 0
        elif name == "END":
            if current is not None and depth:
                depth -= 1
            elif current is not None and value_up == "VEVENT":
                events.append(current)
                current = None
        elif current is not None and not depth:
            current.setdefault(name, []).append((params, value))
    return events


def _first(event, name):
    found = event.get(name)
    return found[0] if found else None


def _uid(event):
    return (_first(event, "UID") or ({}, ""))[1].strip()


def _rank(event):
    """``(SEQUENCE, DTSTAMP)`` of one revision of a repeated UID: absent SEQUENCE is 0, absent DTSTAMP sorts first."""
    seqs, stamps = event.get("SEQUENCE", []), event.get("DTSTAMP", [])
    sequence, stamp = (seqs[0][1].strip() if seqs else "0"), (stamps[0][1].strip() if stamps else "")
    digits = sequence.lstrip("0") or "0"
    ok = len(seqs) < 2 and len(stamps) < 2 and _SEQUENCE.fullmatch(sequence) and len(digits) <= 10
    ok = ok and int(digits) <= MAX_SEQUENCE and (not stamps or _DTSTAMP.fullmatch(stamp))
    try:
        ok = ok and (not stamps or datetime.strptime(stamp, "%Y%m%dT%H%M%SZ"))
    except ValueError:
        ok = False
    if not ok:
        raise MeetingsError("feed has an invalid SEQUENCE or DTSTAMP on a repeated UID; nothing was written")
    return int(digits), stamp


def _revisions(events):
    """RF0854-4: every repeated UID reduced to its latest revision, before any filtering.

    The greatest ``(SEQUENCE, DTSTAMP)`` wins whatever the feed order; identical winners collapse and differing winners
    refuse the feed. A ``RECURRENCE-ID`` component is an instance override, not a revision: it is neither ranked nor
    able to hide a sibling, and is kept so the per-component recurring skip counts it. An ``RRULE`` master plus its
    overrides is therefore not a conflict.
    """
    groups = {}
    for event in events:
        uid = _uid(event)
        if uid and len(uid) <= MAX_UID and "RECURRENCE-ID" not in event:
            groups.setdefault(uid, []).append(event)
    out, done = [], set()
    for event in events:
        uid = _uid(event)
        group = groups.get(uid)
        if group is None or len(group) == 1 or "RECURRENCE-ID" in event:
            out.append(event)
        elif uid not in done:
            done.add(uid)
            ranked = [(_rank(e), e) for e in group]
            top = max(rank for rank, _ in ranked)
            winners = [e for rank, e in ranked if rank == top]
            if any(e != winners[0] for e in winners[1:]):
                raise MeetingsError("feed has conflicting revisions for one UID; nothing was written")
            out.append(winners[0])
    return out


def _start(event, zone):
    """``(starts_at, time_known)``; a string reason (a SKIP_KEYS entry) when the event cannot be placed."""
    prop = _first(event, "DTSTART")
    if prop is None:
        return "bad_start"
    params, raw = prop
    raw = raw.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", raw):
        try:
            day = datetime.strptime(raw, "%Y%m%d")
        except ValueError:
            return "bad_start"
        return day.replace(tzinfo=zone).isoformat(timespec="seconds"), False
    m = re.fullmatch(r"(\d{8}T\d{6})(Z?)", raw.upper())
    if not m:
        return "bad_start"
    try:
        local = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    except ValueError:
        return "bad_start"
    if m.group(2):
        return local.replace(tzinfo=timezone.utc).astimezone(zone).isoformat(timespec="seconds"), True
    tzid = params.get("TZID")
    if not tzid:
        return "floating_time"
    event_zone = resolve_zone(tzid)
    if event_zone is None:
        return "unknown_zone"
    return local.replace(tzinfo=event_zone).isoformat(timespec="seconds"), True  # fold=0: the first of a repeated hour


def _https(url):
    url = url.strip()
    if not url or len(url) > MAX_URL or ATTACHMENT_BAD.search(url) or not url.startswith("https://"):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.username is not None or parts.password is not None:
        return None
    return url.replace("(", "%28").replace(")", "%29")


def _lines(event):
    out = []
    for key in ("SUMMARY", "DESCRIPTION"):
        for _, value in event.get(key, []):
            out.extend(unescape(value).split("\n"))
    return out


class IcsClient:
    """One public ``.ics`` feed. ``relevant`` returns Legistar-shaped meeting dicts; nothing linked is fetched."""
    source = "ics"

    def __init__(self, feed_url, body, opener=None, label="feed"):
        self.feed_url, self.body, self.label = check_feed_url(feed_url), check_body(body), label
        self.client = slug(self.body)
        self.opener = opener or default_opener
        self.skipped = dict.fromkeys(SKIP_KEYS, 0)
        self.report_extra = lambda found: {"skipped": dict(self.skipped)}

    def relevant(self, since=None, zone=None):
        zone = zone or ZoneInfo(DEFAULT_ZONE)
        since = since or date.today()
        data = self.opener(self.feed_url)
        if len(data) > MAX_FEED_BYTES:
            raise MeetingsError(f"{self.label}: larger than 2 MiB; nothing was written")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise MeetingsError(f"{self.label}: not UTF-8 text") from None
        self.skipped = dict.fromkeys(SKIP_KEYS, 0)
        meetings, seen = [], set()
        events = _revisions(parse_events(text))
        for event in events:
            uid = _uid(event)
            if any(k in event for k in ("RRULE", "RDATE", "RECURRENCE-ID")):
                self.skipped["recurring"] += 1
                continue
            if not uid:
                self.skipped["no_uid"] += 1
                continue
            if len(uid) > MAX_UID:
                self.skipped["uid_too_long"] += 1
                continue
            start = _start(event, zone)
            if isinstance(start, str):
                self.skipped[start] += 1
                continue
            if start[0][:10] < since.isoformat():
                continue
            hits = match_items({"EventItemTitle": line} for line in _lines(event))
            ident = f"ics-{self.client}-{hashlib.sha256(uid.encode('utf-8')).hexdigest()[:12]}"
            if not hits or ident in seen:
                continue
            seen.add(ident)
            attached, dropped = [], 0
            for params, value in event.get("ATTACH", []):
                url = _https(value)
                if url is None:
                    dropped += 1
                elif url not in [a["url"] for a in attached] and len(attached) < MAX_ATTACHMENTS:
                    name = " ".join(_plain(params.get("FILENAME", "")).replace("[", "").replace("]", "").split())[:200]
                    attached.append({"name": name or "Attachment", "url": url})
            link = _first(event, "URL")
            agenda = (_https(unescape(link[1])) if link else None) or (attached[0]["url"] if attached else None)
            status = _first(event, "STATUS")
            meetings.append({
                "id": ident,
                "body": self.body,
                "starts_at": start[0],
                "time_known": start[1],
                "agenda_url": agenda,
                "agenda_item": _plain(hits[0][0]),
                "relevance": "alpr_item",
                "source": self.source,
                "event_id": uid,
                "matched_terms": sorted({t for _, ts in hits for t in ts}),
                "matched_items": len(hits),
                "status": "cancelled" if status and status[1].strip().upper() == "CANCELLED" else "scheduled",
                "attachments": attached,
                "attachments_dropped": dropped,
            })
        meetings.sort(key=lambda m: m["starts_at"])
        return meetings
