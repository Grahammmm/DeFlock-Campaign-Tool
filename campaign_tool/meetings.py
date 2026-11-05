"""Meeting alerts from the Legistar Web API and public-comment kits.

``LegistarClient(client, opener=...)`` reads ``https://webapi.legistar.com/v1/<client>/events``
with an OData filter on ``EventDate`` (today or later), then each event's
``EventItems``; ``relevant_events`` keeps events whose agenda items mention
license plate readers (``license plate``, ``ALPR``, ``Flock``, ``surveillance``).
The opener is injectable so tests replay a recorded JSON fixture; nothing is
fetched unless the caller passes ``--online``.

``comment_kit_md(meeting, findings, campaign_name, base_url)`` mirrors the
workspace generator: a two-minute public comment, three asks and a source list
built from published findings only, in the ``markdown_lite`` subset.
"""
import hashlib
import json
import os
import re
import stat
import xml.etree.ElementTree as ET
from xml.parsers import expat
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agency_cards import text as plain_text
from .kit import _kit_folder, _write_text

DEFAULT_ZONE = "America/Los_Angeles"
API_BASE = "https://webapi.legistar.com/v1"
KEYWORDS_FILE = Path(__file__).resolve().parents[1] / "data" / "meeting-keywords.json"
CLIENT = re.compile(r"[a-z0-9-]{2,64}")
MAX_EVENTS = 60
MAX_MATTERS = 100
GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 20
GRANICUS_HOST = re.compile(r"[a-z0-9-]{1,63}\.granicus\.com")
MAX_FEED_BYTES = 2 * 1024 * 1024


class MeetingsError(ValueError):
    """Fetch or parse failure; the kit is not written."""


def load_keywords(path=KEYWORDS_FILE):
    """Compile the one ALPR keyword list (also generated for the Workers); no built-in fallback."""
    problem = None
    try:
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
        alternatives = spec["alternatives"]
        if type(spec["schema_version"]) is not int or spec["schema_version"] != 1 or spec["flags"] != "i":
            problem = "ValueError: expected schema_version 1 and flags \"i\""
        elif not isinstance(alternatives, list) or not alternatives or not all(
                isinstance(a, str) and a for a in alternatives):
            problem = "ValueError: alternatives must be a non-empty list of non-empty strings"
        else:
            return re.compile("|".join(alternatives), re.IGNORECASE)
    except (OSError, ValueError, TypeError, KeyError, re.error) as exc:
        problem = f"{type(exc).__name__}: {exc}"
    raise MeetingsError(f"meeting keywords {path}: {problem}")


KEYWORDS = load_keywords()


def default_opener(url):
    """Fetch ``url`` (https only) and return bytes; bounded size and timeout."""
    if not url.startswith("https://"):
        raise MeetingsError("only https URLs are fetched")
    request = urllib.request.Request(url, headers={"accept": "application/json",
                                                   "user-agent": "deflock-campaign-tool/0.1 (+meetings)"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise MeetingsError(f"fetch failed for {url}: {exc}") from None
    if len(data) > MAX_BYTES:
        raise MeetingsError("response too large")
    return data


def fixture_opener(mapping):
    """Opener replaying ``{url_or_path_suffix: json_value}`` recorded responses."""
    def opener(url):
        path = urllib.parse.urlsplit(url).path
        for key, value in mapping.items():
            if url == key or path.endswith(key):
                return json.dumps(value).encode("utf-8")
        raise MeetingsError(f"no recorded response for {url}")
    return opener


class LegistarClient:
    source = "legistar"

    def __init__(self, client, opener=None, base=API_BASE):
        if not isinstance(client, str) or not CLIENT.fullmatch(client):
            raise MeetingsError("Legistar client must be a short lowercase slug (e.g. the city name)")
        self.client = client
        self.opener = opener or default_opener
        self.base = base.rstrip("/")

    def _get(self, path, params=None):
        url = f"{self.base}/{self.client}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        data = self.opener(url)
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MeetingsError(f"{url}: not JSON ({exc})") from None
        if not isinstance(payload, list):
            raise MeetingsError(f"{url}: expected a JSON list")
        return payload

    def events(self, since=None, top=MAX_EVENTS):
        """Events on or after ``since`` (date, default today), oldest first."""
        since = since or date.today()
        return self._get("events", {
            "$filter": f"EventDate ge datetime'{since.isoformat()}'",
            "$orderby": "EventDate",
            "$top": str(min(int(top), MAX_EVENTS)),
        })

    def event_items(self, event_id):
        if type(event_id) is not int or event_id < 0:
            raise MeetingsError("event id must be an integer")
        return self._get(f"events/{event_id}/eventitems", {"Attachments": "1"})

    def matter_attachments(self, matter_id):
        if type(matter_id) is not int or matter_id < 0:
            raise MeetingsError("matter id must be an integer")
        return self._get(f"matters/{matter_id}/attachments")

    def matters(self, since=None, top=MAX_MATTERS):
        """Matters modified on or after ``since`` (date, default today minus 365 days), newest first."""
        since = since or date.today() - timedelta(days=365)
        return self._get("matters", {
            "$filter": f"MatterLastModifiedUtc ge datetime'{since.isoformat()}'",
            "$orderby": "MatterLastModifiedUtc desc",
            "$top": str(min(int(top), MAX_MATTERS)),
        })


class CivicClerkClient(LegistarClient):
    """CivicClerk public events API (``https://<client>.api.civicclerk.com/v1``), adapted to the Legistar-shaped keys."""
    source = "civicclerk"

    def __init__(self, client, opener=None):
        super().__init__(client, opener=opener, base=f"https://{client}.api.civicclerk.com/v1")
        self._agenda = {}

    def _get(self, path, params=None):
        url = f"{self.base}/{path}" + ("?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote) if params else "")
        try:
            payload = json.loads(self.opener(url).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MeetingsError(f"{url}: not JSON ({exc})") from None
        if isinstance(payload, dict) and "value" in payload:
            payload = payload["value"]
        if not isinstance(payload, (list, dict)) or (isinstance(payload, dict) and path == "Events"):
            raise MeetingsError(f"{url}: expected a JSON list or a value envelope")
        return payload

    def _agenda_url(self, event):
        for f in event.get("publishedFiles") or []:
            if isinstance(f, dict) and f.get("type") == "Agenda":
                url = f.get("url")
                if isinstance(url, str) and "://" in url:
                    host = urllib.parse.urlsplit(url).hostname or ""
                    return url if url.startswith("https://") and (host == "civicclerk.com" or host.endswith(".civicclerk.com")) else None
                if type(f.get("fileId")) is int:
                    return f"{self.base}/Meetings/GetMeetingFileStream(fileId={f['fileId']},plainText=false)"
        return None

    def events(self, since=None, top=MAX_EVENTS):
        since = since or date.today()
        raw = self._get("Events", {"$filter": f"startDateTime ge {since.isoformat()}", "$orderby": "startDateTime asc",
                                   "$top": str(min(int(top), MAX_EVENTS))})
        out = []
        for e in raw[:MAX_EVENTS]:
            if not isinstance(e, dict) or type(e.get("id")) is not int or not isinstance(e.get("startDateTime"), str):
                continue
            start = e["startDateTime"]
            if start[:10] < since.isoformat():
                continue
            self._agenda[e["id"]] = e.get("agendaId")
            aware = start.endswith("Z") or "+" in start[10:]
            out.append({"EventId": e["id"], "EventBodyName": e.get("categoryName") or e.get("eventName"),
                        "EventStartUtc": start if aware else None, "EventDate": start,
                        "EventComment": f"{e.get('eventName') or ''} {e.get('eventNotice') or ''}",
                        "EventInSiteURL": self._agenda_url(e)})
        return out

    def event_items(self, event_id):
        agenda = self._agenda.get(event_id)
        if type(agenda) is not int or agenda <= 0:
            return []
        doc = self._get(f"Meetings/{agenda}")
        items, stack = [], list(doc.get("items") or []) if isinstance(doc, dict) else list(doc)
        while stack:
            item = stack.pop(0)
            if isinstance(item, dict):
                stack = list(item.get("childItems") or []) + stack
                items.append({"EventItemTitle": re.sub(r"<[^>]*>", " ", str(item.get("agendaObjectItemName") or "")),
                              "EventItemMatterName": re.sub(r"<[^>]*>", " ", str(item.get("agendaObjectItemDescription") or ""))})
        return items


def read_feed_file(path):
    """Bytes of a saved Granicus feed: a regular file (not a symlink) of at most 2 MiB."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise MeetingsError(f"{path}: not a readable regular file (symlinks are refused)") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise MeetingsError(f"{path}: not a regular file")
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        raise MeetingsError(f"{path}: larger than 2 MiB")
    return data


def parse_feed(data, label="feed"):
    """Root element of an RSS feed; DOCTYPE/ENTITY declarations (any encoding) and malformed XML are refused."""
    def refuse(*_):
        raise MeetingsError(f"{label}: DOCTYPE and ENTITY declarations are refused")
    # Pre-pass with expat in any encoding (UTF-16 included): a DTD or entity declaration is refused before ElementTree sees it.
    scan = expat.ParserCreate()
    scan.StartDoctypeDeclHandler = scan.EntityDeclHandler = refuse
    try:
        scan.Parse(data, True)
        return ET.fromstring(data)
    except (expat.ExpatError, ET.ParseError) as exc:
        raise MeetingsError(f"{label}: not well-formed XML ({exc})") from None


class GranicusClient:
    """Granicus ViewPublisher agenda RSS (``https://<host>/ViewPublisherRSS.php?view_id=<n>&mode=agendas``), Legistar-shaped."""
    source = "granicus"
    TITLE_DATE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? +(\d{1,2}),? +(\d{4})\b")

    def __init__(self, host, view_id=None, opener=None, label="feed"):
        if not isinstance(host, str) or not GRANICUS_HOST.fullmatch(host):
            raise MeetingsError("Granicus host must look like <name>.granicus.com")
        if type(view_id) is not int or not 1 <= view_id <= 9999:
            raise MeetingsError("Granicus view_id must be a number from 1 to 9999")
        self.host, self.view_id, self.label = host, view_id, label
        self.client = host.split(".")[0]
        self.opener = opener or default_opener
        self.cut = 0
        self._scanned = {}

    def _date(self, title, pub):
        try:
            if pub:
                return parsedate_to_datetime(pub).date().isoformat()
        except (TypeError, ValueError, IndexError):
            pass
        m = self.TITLE_DATE.search(title)
        if m:
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat()
            except ValueError:
                pass
        return None

    def events(self, since=None, top=MAX_EVENTS):
        url = f"https://{self.host}/ViewPublisherRSS.php?view_id={self.view_id}&mode=agendas"
        since = since or date.today()
        root = parse_feed(self.opener(url), self.label)
        events = []
        for item in root.iter("item"):
            def field(name):
                node = item.find(name)
                return " ".join((node.text or "").split()) if node is not None else ""
            title, link, guid = field("title"), field("link"), field("guid")
            day = self._date(title, field("pubDate"))
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
            ident = guid if guid.isdigit() else next((v[0] for k in ("clip_id", "event_id")
                                                      for v in [query.get(k) or []] if v and v[0].isdigit()), None)
            if not day or not ident or len(ident) > 12 or day < since.isoformat():
                continue
            parts = urllib.parse.urlsplit(link)
            ok = parts.scheme == "https" and (parts.hostname or "").lower() == self.host and not parts.username
            description = _clean(re.sub(r"<[^>]*>", " ", field("description")))
            self._scanned[int(ident)] = bool(description)
            events.append({"EventId": int(ident), "EventBodyName": title, "EventDate": day, "EventComment": title,
                           "EventInSiteURL": link if ok else None, "_description": description})
        events.sort(key=lambda e: (e["EventDate"], e["EventId"]))
        self.cut = max(0, len(events) - min(top, MAX_EVENTS))
        self._events = {e["EventId"]: e for e in events}
        return events[:min(top, MAX_EVENTS)]

    def event_items(self, event_id):
        e = self._events.get(event_id, {})
        if not e.get("_description"):
            return []  # nothing but a title: not scanned, never a match
        return [{"EventItemTitle": e.get("EventBodyName", ""), "EventItemMatterName": e["_description"]}]

    def scanned(self, event_id):
        return self._scanned.get(event_id, False)


AGENDACENTER_HOST = re.compile(r"[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){1,5}")
AGENDACENTER_DATE = re.compile(r"_(\d{2})(\d{2})(\d{4})-")
AGENDACENTER_ID = re.compile(r"-(\d{1,12})/?$")


class AgendaCenterClient:
    """CivicPlus AgendaCenter public RSS (``https://<host>/RSSFeed.aspx?ModID=65&CID=All-0``), Legistar-shaped.

    Only the feed text (title, category, description) is scanned; agenda PDF links are recorded, never fetched.
    """
    source = "agendacenter"

    def __init__(self, host, opener=None, label="feed"):
        # 2-6 lowercase DNS labels; an all-digit last label is an IP literal; ports, userinfo and paths never match.
        if (not isinstance(host, str) or not AGENDACENTER_HOST.fullmatch(host) or len(host) > 253
                or host.rsplit(".", 1)[1].isdigit()):
            raise MeetingsError("AgendaCenter host must be a lowercase DNS name (2-6 labels), no IP, port, user or path")
        self.host, self.label = host, label
        self.client = host.split(".")[0]
        self.opener = opener or default_opener
        self.cut = 0
        self.dropped_no_date = 0
        self._scanned = {}
        self._events = {}

    @staticmethod
    def _date(link, pub):
        m = AGENDACENTER_DATE.search(urllib.parse.urlsplit(link).path)
        if m:
            try:
                return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
            except ValueError:
                pass  # an impossible link date falls back to pubDate
        try:
            if pub:
                return parsedate_to_datetime(pub).date().isoformat()
        except (TypeError, ValueError, IndexError):
            pass
        return None

    def events(self, since=None, top=MAX_EVENTS):
        since = since or date.today()
        root = parse_feed(self.opener(f"https://{self.host}/RSSFeed.aspx?ModID=65&CID=All-0"), self.label)
        events, seen = [], set()
        self.dropped_no_date = 0
        for item in root.iter("item"):
            def field(name):
                node = item.find(name)
                return " ".join((node.text or "").split()) if node is not None else ""
            title, link, category = field("title"), field("link"), field("category")
            day = self._date(link, field("pubDate"))
            if not day:
                self.dropped_no_date += 1
                continue
            if day < since.isoformat():
                continue
            m = AGENDACENTER_ID.search(urllib.parse.urlsplit(link).path)
            ident = int(m.group(1)) if m else int(hashlib.sha256((link or title).encode("utf-8")).hexdigest()[:12], 16)
            if ident in seen:
                continue
            seen.add(ident)
            parts = urllib.parse.urlsplit(link)
            ok = (parts.scheme == "https" and (parts.hostname or "").lower() == self.host and not parts.username
                  and parts.path.startswith("/AgendaCenter/"))
            description = _clean(re.sub(r"<[^>]*>", " ", field("description")))
            self._scanned[ident] = bool(description)
            body = category or re.split(r" [-\u2013] ", title, maxsplit=1)[0]
            events.append({"EventId": ident, "EventBodyName": body, "EventDate": day, "EventComment": title,
                           "EventInSiteURL": link if ok else None, "_title": title, "_description": description})
        events.sort(key=lambda e: (e["EventDate"], e["EventId"]))
        self.cut = max(0, len(events) - min(top, MAX_EVENTS))
        self._events = {e["EventId"]: e for e in events}
        return events[:min(top, MAX_EVENTS)]

    def event_items(self, event_id):
        e = self._events.get(event_id, {})
        if not e.get("_description"):
            return []  # nothing but a title: not scanned, never a match
        return [{"EventItemTitle": e["_title"], "EventItemMatterName": e["_description"]}]

    def scanned(self, event_id):
        return self._scanned.get(event_id, False)


def feed_opener(path):
    return lambda url: read_feed_file(path)


def make_client(args, opener):
    """The agenda-platform client for the ``meetings`` command; refuses flags that belong to another platform."""
    granicus, center = args.platform == "granicus", args.platform == "agendacenter"
    feed = granicus or center
    if (feed and (args.from_json or (args.online and args.from_xml))
            or (args.from_xml or args.all_events) and not feed
            or args.view_id is not None and not granicus):
        raise MeetingsError("--from-xml and --all-events go with --platform granicus or agendacenter (which read --online or --from-xml, not --from-json); "
                            "--view-id goes with granicus")
    if center:
        client = AgendaCenterClient(args.client, opener=opener, label=args.from_xml or "feed")
        client.report_extra = lambda found: {"not_scanned": sum(bool(m.get("not_scanned")) for m in found),
                                             "cut_by_item_cap": client.cut, "dropped_no_date": client.dropped_no_date}
        return client
    if granicus:
        client = GranicusClient(args.client, args.view_id, opener=opener, label=args.from_xml or "feed")
        client.report_extra = lambda found: {"not_scanned": sum(bool(m.get("not_scanned")) for m in found), "cut_by_item_cap": client.cut}
        return client
    return (CivicClerkClient if args.platform == "civicclerk" else LegistarClient)(args.client, opener=opener)


def resolve_zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


def _starts_at(event, zone):
    """(local time with the zone's offset for that date, time_known); None without a date."""
    utc = event.get("EventStartUtc")
    if isinstance(utc, str):  # CivicClerk: an absolute instant, shown in the --tz zone
        try:
            return datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(zone).isoformat(timespec="seconds"), True
        except ValueError:
            return None
    day = str(event.get("EventDate") or "")[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    time = str(event.get("EventTime") or "").strip()
    try:
        parsed = datetime.strptime(time.upper(), "%I:%M %p").time() if time else None
    except ValueError:
        parsed = None
    local = datetime.fromisoformat(f"{day}T{parsed.strftime('%H:%M:%S') if parsed else '00:00:00'}")
    return local.replace(tzinfo=zone).isoformat(timespec="seconds"), parsed is not None


def _moved(old, new):
    """Start changed? An old value without an offset is wall time in the new value's zone."""
    try:
        before, after = (datetime.fromisoformat(v.replace("Z", "+00:00")) for v in (old, new))
    except ValueError:
        return old != new
    naive = before.tzinfo is None or after.tzinfo is None
    return before.replace(tzinfo=None) != after.replace(tzinfo=None) if naive else before != after


def _item_text(item):
    parts = []
    for key in ("EventItemTitle", "EventItemMatterName", "EventItemMatterFile", "EventItemActionText"):
        value = item.get(key)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


MAX_ATTACHMENTS = 20
ATTACHMENT_HOST = re.compile(r"(?:[a-z0-9-]+\.)+legistar\.com|legistar\.granicus\.com|legistar\d+\.granicus\.com")
ATTACHMENT_BAD = re.compile(r"[\s<>\"'\\\x00-\x1f\x7f]")


def _attachment(obj):
    """DF2022-13: ``{"name", "url"}`` for one Legistar attachment object, or None when its link is not an official https Legistar URL."""
    url = obj.get("MatterAttachmentHyperlink") if isinstance(obj, dict) else None
    if not isinstance(url, str) or len(url) > 500 or ATTACHMENT_BAD.search(url) or not url.startswith("https://"):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None or port is not None or not ATTACHMENT_HOST.fullmatch(parts.hostname or ""):
        return None
    name = obj.get("MatterAttachmentName")
    name = " ".join(_plain(name if isinstance(name, str) else "").replace("[", "").replace("]", "").split())[:200]
    return {"name": name or "Attachment", "url": url.replace("(", "%28").replace(")", "%29")}


def _attachments(objects):
    """(kept, dropped): de-duplicated by URL, at most MAX_ATTACHMENTS; dropped counts the links refused. Never fetched."""
    kept, seen, dropped = [], set(), 0
    for obj in objects:
        found = _attachment(obj)
        if found is None:
            dropped += 1
        elif found["url"] not in seen and len(kept) < MAX_ATTACHMENTS:
            seen.add(found["url"])
            kept.append(found)
    return kept, dropped


def match_items(items):
    """Return ``[(item_text, [terms])]`` for agenda items matching the ALPR keywords."""
    hits = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = " ".join(_item_text(item).split())
        terms = sorted({m.group(0).lower() for m in KEYWORDS.finditer(text)})
        if terms:
            hits.append((text[:300], terms))
    return hits


CANCELLED_COMMENT = re.compile(r"\bcancel(?:l)?ed\b", re.IGNORECASE)


def event_status(event):
    """``cancelled`` when Legistar's agenda status or event comment says so, else ``scheduled``."""
    status, comment = event.get("EventAgendaStatusName"), event.get("EventComment")
    if isinstance(status, str) and status.strip().casefold() in ("cancelled", "canceled"):
        return "cancelled"
    return "cancelled" if isinstance(comment, str) and CANCELLED_COMMENT.search(comment) else "scheduled"


def relevant_events(client, since=None, max_events=MAX_EVENTS, zone=None, all_events=False):
    """Upcoming events with at least one ALPR-related agenda item, as meeting dicts."""
    meetings = []
    for event in client.events(since=since, top=max_events)[:max_events]:
        if not isinstance(event, dict) or type(event.get("EventId")) is not int:
            continue
        items = client.event_items(event["EventId"])
        hits = match_items(items)
        unscanned = not hits and all_events and hasattr(client, "scanned") and not client.scanned(event["EventId"])
        if not hits and not unscanned:
            continue
        starts = _starts_at(event, zone or ZoneInfo(DEFAULT_ZONE))
        if not starts:
            continue
        body = " ".join(str(event.get("EventBodyName") or "Unknown body").split())
        agenda_url = event.get("EventInSiteURL") or event.get("EventAgendaFile")
        if not isinstance(agenda_url, str) or not agenda_url.startswith("https://"):
            agenda_url = None
        item_text, terms = hits[0] if hits else ("", [])
        matched = [i for i in items if isinstance(i, dict) and match_items([i])]
        attached, dropped = _attachments(a for i in matched for a in (i.get("EventItemMatterAttachments") or [])
                                         if isinstance(i.get("EventItemMatterAttachments"), list))
        meetings.append({
            "id": f"{client.source}-{client.client}-{event['EventId']}",
            "body": _plain(body),
            "starts_at": starts[0],
            "time_known": starts[1],
            "agenda_url": agenda_url,
            "agenda_item": _plain(item_text),
            "relevance": "alpr_item",
            "source": client.source,
            "event_id": event["EventId"],
            "matched_terms": sorted({t for _, ts in hits for t in ts}),
            "matched_items": len(hits),
            "status": event_status(event),
            "attachments": attached,
            "attachments_dropped": dropped,
            **({"not_scanned": True, "relevance": "not_scanned"} if unscanned else {}),
        })
    meetings.sort(key=lambda m: m["starts_at"])
    return meetings


def _plain(value):
    cleaned = value.replace("<", "").replace(">", "")
    if not plain_text(cleaned):
        return ""
    return cleaned


def compare_meetings(previous_doc, meetings, client, since):
    """What changed between the previous ``kit/meetings.json`` document and ``meetings``.

    ``past`` holds previous meetings dated before ``since`` (out of the window, not removed).
    """
    if previous_doc is None:
        return {"previous": "none"}
    old = previous_doc.get("meetings") if isinstance(previous_doc, dict) else None
    if (not isinstance(previous_doc, dict) or previous_doc.get("schema_version") != 1
            or not isinstance(old, list)
            or not all(isinstance(m, dict) and isinstance(m.get("id"), str)
                       and isinstance(m.get("starts_at"), str) for m in old)):
        return {"previous": "unreadable"}
    if previous_doc.get("client") != client:
        return {"previous": "other_client", "previous_client": str(previous_doc.get("client"))[:64]}
    before = {m["id"]: m for m in old}  # attachments are not compared: a changed list alone is never agenda_changed
    after = {m["id"]: m for m in meetings}
    cutoff = since.isoformat()
    gone = sorted(set(before) - set(after))
    both = sorted(set(before) & set(after))
    generated = previous_doc.get("generated")
    # An old file without ``status`` reads as all scheduled; a cancelled meeting is never agenda_changed.
    now_cancelled = {i for i in after if after[i].get("status") == "cancelled"}
    return {
        "previous": "compared",
        "previous_generated": generated if isinstance(generated, str) else None,
        "added": sorted(set(after) - set(before)),
        "removed": [i for i in gone if before[i]["starts_at"][:10] >= cutoff],
        "rescheduled": [{"id": i, "from": before[i]["starts_at"], "to": after[i]["starts_at"]} for i in both
                        if _moved(before[i]["starts_at"], after[i]["starts_at"])],
        "agenda_changed": [i for i in both if i not in now_cancelled and any(before[i].get(k) != after[i].get(k)
                                                                         for k in ("agenda_item", "agenda_url"))],
        "past": [i for i in gone if before[i]["starts_at"][:10] < cutoff],
        "cancelled": [i for i in both if i in now_cancelled and before[i].get("status") != "cancelled"],
    }


def write_meetings(root, meetings, client_name, changes=None, zone=None, source="legistar"):
    kit = root / "kit"
    _kit_folder(kit, mode=0o700)
    target = kit / "meetings.json"
    document = {"schema_version": 1, "source": source, "client": client_name,
                "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "time_zone": getattr(zone, "key", None) or DEFAULT_ZONE,
                "keywords": KEYWORDS.pattern, "meetings": meetings,
                "note": "Agenda matches are keyword hits, not confirmation that ALPR will be discussed. "
                        "Verify the posted agenda before publishing a meeting."}
    if changes is not None:
        document["changes"] = changes
    _write_text(target, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    return target


def _matter_date(value):
    """YYYY-MM-DD from a Legistar date string, else None."""
    try:
        return date.fromisoformat(value[:10]).isoformat() if isinstance(value, str) else None
    except ValueError:
        return None


def relevant_matters(client, since=None):
    """Matters whose file number, name or title match the ALPR keywords, as keyword leads."""
    out = []
    for raw in client.matters(since=since)[:MAX_MATTERS]:
        if not isinstance(raw, dict) or type(raw.get("MatterId")) is not int:
            continue
        fields = [raw.get(k) if isinstance(raw.get(k), str) else "" for k in ("MatterFile", "MatterName", "MatterTitle")]
        text = " ".join(" ".join(fields).split())
        terms = sorted({h.group(0).lower() for h in KEYWORDS.finditer(text)})
        if not terms:
            continue

        def clean(key):
            value = raw.get(key)
            return _plain(" ".join(value.split()))[:300] if isinstance(value, str) else ""
        guid = raw.get("MatterGuid")
        url = (f"https://{client.client}.legistar.com/LegislationDetail.aspx?ID={raw['MatterId']}&GUID={guid}"
               if isinstance(guid, str) and GUID.fullmatch(guid) else None)
        try:
            listed = client.matter_attachments(raw["MatterId"])
        except MeetingsError:
            listed = []
        attached, dropped = _attachments(listed if isinstance(listed, list) else [])
        out.append({"id": f"{client.source}-{client.client}-matter-{raw['MatterId']}",
                    "file": clean("MatterFile"), "title": clean("MatterTitle") or clean("MatterName"),
                    "type": clean("MatterTypeName"), "status": clean("MatterStatusName"), "body": clean("MatterBodyName"),
                    "intro_date": _matter_date(raw.get("MatterIntroDate")), "agenda_date": _matter_date(raw.get("MatterAgendaDate")),
                    "passed_date": _matter_date(raw.get("MatterPassedDate")), "url": url, "matched_terms": terms,
                    "attachments": attached, "attachments_dropped": dropped})
    out.sort(key=lambda r: r["id"])
    out.sort(key=lambda r: r["intro_date"] or "", reverse=True)
    return out


def write_matters(root, matters, client_name, since=None):
    kit = root / "kit"
    _kit_folder(kit, mode=0o700)
    target = kit / "matters.json"
    document = {"schema_version": 1, "source": "legistar", "client": client_name,
                "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "keywords": KEYWORDS.pattern, "since": (since or date.today() - timedelta(days=365)).isoformat(),
                "matters": matters,
                "note": "Keyword hits on matter titles, not confirmation of ALPR use. Verify on the official Legistar page before citing."}
    _write_text(target, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    return target


# --- comment kit ------------------------------------------------------------

ASKS = {
    "alpr_item": "Continue this item until the public has had at least 30 days with the written policy, "
                 "the vendor agreement and the sharing list.",
    "budget": "Separate the license plate reader line from the consent calendar and hear it as a "
              "discussion item with a staff report.",
    "consent_calendar": "Pull this item from the consent calendar so residents can comment on it individually.",
}
DEFAULT_ASK = ("Direct staff to publish the current license plate reader policy, agreement and sharing "
               "list before any renewal.")


def _clean(text):
    return " ".join(str(text or "").replace("<", "").replace(">", "").split())


ASK_COUNT_WORDS = {1: "One ask", 2: "Two asks", 3: "Three asks"}
ASKS_ABOVE = {1: "the ask", 2: "the two asks", 3: "the three asks"}


COSIGN_HEADING = "**Co-signed by**"  # the shared Markdown subset renders only "## " headings, so the roster is bold text
_MARKUP = re.compile(r"([\\*_\[\]()`<>#!|~])")


def cosign_block_md(names):
    """DF2207-14: the generated roster under a written comment: a bold ``Co-signed by`` line and one bullet per
    organization name, each name kept literal (markup characters backslash-escaped). No URL, contact or evidence."""
    return "\n".join([COSIGN_HEADING] + ["- " + _MARKUP.sub(r"\\\1", _clean(name)) for name in names])


def default_asks(meeting):
    return [ASKS.get(meeting.get("relevance") or "", DEFAULT_ASK),
            "Require an annual public audit of searches, retention and outside-agency sharing, "
            "presented at a noticed meeting.",
            "Answer in writing which agencies can query this data today and under what written agreement."]


def comment_kit_md(meeting, findings, campaign_name, base_url=None, *, asks=None, cosign_names=None):
    """Markdown comment kit; ``findings`` are published finding dicts with title, summary,
    classification, confidence, path and sources[{title, locator, sha256}].

    ``asks`` (DF2207-07): the campaign's own 1-3 asks, validated here; they replace the relevance-based defaults in the
    asks section, the written letter and (with a verified speaker limit) the spoken version. None keeps the defaults.

    ``cosign_names`` (DF2207-14): 1-30 consenting organization names, rendered by ``cosign_block_md`` after the
    signature of the written letter only. The roster is a separate bounded block: it is not counted in the 400-word
    letter body and never displaces a finding bullet. Without a real letter (no written comment, or the over-cap
    advisory) the call raises MeetingsError rather than claim a co-signed letter. None leaves the kit unchanged."""
    if cosign_names is not None and not (isinstance(cosign_names, (list, tuple)) and 1 <= len(cosign_names) <= 30
                                         and all(isinstance(name, str) and name.strip() for name in cosign_names)):
        raise MeetingsError("co-signers: give 1 to 30 organization names")
    if asks is not None:
        from .content import load_comment_asks
        asks = load_comment_asks(asks, "asks")
    resolved = list(asks) if asks is not None else default_asks(meeting)
    def fixed(finding):  # latest correction date; "" when the finding was never corrected
        dates = [str(c.get("date") or "") for c in finding.get("corrections") or [] if isinstance(c, dict)]
        return _clean(max(dates, default=""))

    lines = []
    when = str(meeting.get("starts_at", ""))[:10]
    lines.append(f"## Public comment: {_clean(meeting.get('body'))}, {when}")
    if meeting.get("agenda_item"):
        lines.append(f"Agenda item: {_clean(meeting['agenda_item'])}")
    lines.append("")
    lines.append("## Two-minute comment")
    start = len(lines)
    lines.append(f"Good evening. My name is [name] and I live in [city]. I am speaking with "
                 f"{_clean(campaign_name)} about automated license plate readers.")
    if findings:
        plural = "" if len(findings) == 1 else "s"
        lines.append(f"We requested public records and published {len(findings)} reviewed finding{plural}, "
                     "each tied to a document hash:")
        for finding in findings[:3]:
            lines.append(f"- {_clean(finding.get('summary'))} "
                         f"({_clean(finding.get('classification')).replace('_', ' ')}, "
                         f"{_clean(finding.get('confidence')).replace('_', ' ')})"
                         + (f" (corrected {fixed(finding)})" if fixed(finding) else ""))
    else:
        lines.append("We have requested the policy, the vendor agreement and the sharing records and will "
                     "publish what we receive with source hashes.")
    lines.append("We are not asking you to take our word for it. Every claim links to the record it came "
                 "from. Please read them before this item moves forward.")
    lines.append("")
    minutes = (meeting.get("participation") or {}).get("speaker_minutes")
    if type(minutes) is int and 1 <= minutes <= 10:  # organizer-verified limit only; never assumed
        budget, body = 130 * minutes, lines[start:-1]  # 130 spoken words a minute
        if asks is not None:  # the campaign's asks are spoken too, and never trimmed to fit
            body = body + ["I ask you to:"] + [f"{n}. {ask}" for n, ask in enumerate(resolved, 1)]
        words = lambda rows: sum(len(row.split()) for row in rows)  # noqa: E731
        while words(body) > budget and any(row.startswith("- ") for row in body):  # whole bullets, from the end
            del body[max(i for i, row in enumerate(body) if row.startswith("- "))]
        lines.append(f"## Spoken comment ({minutes} minute{'' if minutes == 1 else 's'}, about {budget} words)")
        if words(body) > budget:
            lines.append("The speaker limit is too short for this kit; use the first sentence and one ask.")
            if asks is not None:
                lines.append("The asks section below lists every ask in order.")
        else:
            lines.extend(body + [f"Word count: {words(body)} of {budget}."])
        lines.append("")

    def due_text(written):  # "YYYY-MM-DD HH:MM (UTC±hh:mm)" in the deadline's own offset; "" when not stated
        due = written.get("deadline")
        due = datetime.fromisoformat(due.replace("Z", "+00:00")) if due else None
        return f"{due:%Y-%m-%d %H:%M} (UTC{due.isoformat()[-6:]})" if due else ""

    lines.append("## " + ASK_COUNT_WORDS[len(resolved)])
    lines.extend("- " + ask for ask in resolved)
    lines.append("")
    letter_for = (meeting.get("participation") or {}).get("written_comment")
    if letter_for:  # a letter to adapt and send by hand; no recipient or address is ever added here
        letter = [f"Send by: {due_text(letter_for) or 'deadline not stated by the source'}",
                  f"How: {_clean(letter_for.get('how'))}", f"To the {_clean(meeting.get('body'))},"]
        if meeting.get("agenda_item"):
            letter.append(f"Re: {_clean(meeting['agenda_item'])}")
        letter.append(f"I live in [city] and write with {_clean(campaign_name)} about automated license plate "
                      "readers. " + ("Each finding below links to its published page and to the first 12 "
                                     "characters of the SHA-256 hash of every source record, so you can check "
                                     "each claim yourself." if findings else "We have requested the policy, the "
                                     "vendor agreement and the sharing records and will publish what we receive."))
        for finding in findings:
            path = str(finding.get("path") or "")
            link = _clean((base_url.rstrip("/") + path) if base_url else path)
            hashes = [_clean(s.get("sha256"))[:12] for s in finding.get("sources") or [] if isinstance(s, dict)]
            refs = "; ".join([link] * bool(link) + ["sources " + ", ".join(h for h in hashes if h)] * any(hashes))
            title, note = _clean(finding.get("title")), fixed(finding)
            letter.append(f"- {title + ': ' if title else ''}{_clean(finding.get('summary'))}"
                          + (f" ({refs})" if refs else "") + (f" (corrected {note})" if note else ""))
        letter.append("I ask you to:")
        letter.extend(f"{n}. {ask}" for n, ask in enumerate(resolved, 1))
        letter.extend(["Sincerely,", "[name]", "[city]"])
        words = lambda rows: sum(len(row.split()) for row in rows)  # noqa: E731
        while words(letter) > 400 and any(row.startswith("- ") for row in letter):  # whole bullets, from the end
            del letter[max(i for i, row in enumerate(letter) if row.startswith("- "))]
        if words(letter) > 400:  # body, agenda item or campaign name alone over the cap: no letter, say so
            letter = letter[:2] + ["The letter would be over 400 words for this meeting; write a short one by "
                                   f"hand from {ASKS_ABOVE[len(resolved)] if asks is not None else 'the three asks'} above."]
        if cosign_names is not None:
            if words(letter) > 400 or not letter[-1] == "[city]":
                raise MeetingsError("co-signers: the letter would be over 400 words, so no letter is written to sign")
            letter.extend(["", *cosign_block_md(cosign_names).split("\n")])
        lines.extend(["## Written comment"] + letter + [""])
    elif cosign_names is not None:
        raise MeetingsError("co-signers: this meeting has no verified written comment to sign")
    lines.append("## How to take part")
    part = meeting.get("participation")
    if not part:
        lines.append("Check the posted agenda for how and when to comment; participation details were not verified.")
    else:
        if part.get("in_person") is not None:
            lines.append("- In person: " + ("yes" if part["in_person"] else "no"))
        remote, written = part.get("remote"), part.get("written_comment")
        if remote:
            how = _clean(remote.get("how")) if remote.get("available") else ""
            lines.append("- Remote: " + ("yes" if remote.get("available") else "no") + (f", {how}" if how else ""))
        if written:
            due = due_text(written)
            lines.append(f"- Written comment: {_clean(written.get('how'))}" + (f", by {due}" if due else ""))
        lines.append(f"Source: {_clean(part.get('source_url'))} (checked {_clean(part.get('verified_at'))})")
    lines.append("")
    attachments = [a for a in meeting.get("attachments") or [] if isinstance(a, dict) and str(a.get("url")).startswith("https://")]
    if attachments:  # DF2022-13: official agenda-system links, never fetched
        lines.append("## Read first")
        lines.extend(f"- [{_clean(a.get('name') or 'Attachment').replace('[', '').replace(']', '')}]({a['url']})" for a in attachments)
        lines.extend(["Attachments are official links from the agenda system; read them before you speak.", ""])
    lines.append("## Sources")
    if not findings:
        lines.append("- No published findings yet; cite the records request itself and its date.")
    for finding in findings:
        path = str(finding.get("path") or "")
        link = (base_url.rstrip("/") + path) if base_url else path
        note = f" (corrected {fixed(finding)}; corrections: {_clean(link)}#corrections)" if fixed(finding) else ""
        lines.append(f"- **{_clean(finding.get('title') or finding.get('summary'))}** {link}{note}")
        for source in finding.get("sources") or []:
            if isinstance(source, dict):
                lines.append(f"- {_clean(source.get('title') or 'Source record')}, "
                             f"{_clean(source.get('locator'))}, sha256 {str(source.get('sha256', ''))[:16]}")
    lines.append("")
    lines.append("Findings describe records and published rules as of the event date. This is not legal advice.")
    return "\n".join(lines) + "\n"
