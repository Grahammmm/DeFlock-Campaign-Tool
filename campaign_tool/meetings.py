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
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

from .agency_cards import text as plain_text

API_BASE = "https://webapi.legistar.com/v1"
KEYWORDS = re.compile(r"license[ -]?plate|\bALPRs?\b|\bFlock\b|surveillance", re.IGNORECASE)
CLIENT = re.compile(r"[a-z0-9-]{2,64}")
MAX_EVENTS = 60
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 20


class MeetingsError(ValueError):
    """Fetch or parse failure; the kit is not written."""


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
        return self._get(f"events/{event_id}/eventitems")


def _starts_at(event):
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
    return f"{day}T{parsed.strftime('%H:%M:%S') if parsed else '00:00:00'}"


def _item_text(item):
    parts = []
    for key in ("EventItemTitle", "EventItemMatterName", "EventItemMatterFile", "EventItemActionText"):
        value = item.get(key)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


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


def relevant_events(client, since=None, max_events=MAX_EVENTS):
    """Upcoming events with at least one ALPR-related agenda item, as meeting dicts."""
    meetings = []
    for event in client.events(since=since, top=max_events)[:max_events]:
        if not isinstance(event, dict) or type(event.get("EventId")) is not int:
            continue
        hits = match_items(client.event_items(event["EventId"]))
        if not hits:
            continue
        starts_at = _starts_at(event)
        if not starts_at:
            continue
        body = " ".join(str(event.get("EventBodyName") or "Unknown body").split())
        agenda_url = event.get("EventInSiteURL") or event.get("EventAgendaFile")
        if not isinstance(agenda_url, str) or not agenda_url.startswith("https://"):
            agenda_url = None
        item_text, terms = hits[0]
        meetings.append({
            "id": f"legistar-{client.client}-{event['EventId']}",
            "body": _plain(body),
            "starts_at": starts_at,
            "agenda_url": agenda_url,
            "agenda_item": _plain(item_text),
            "relevance": "alpr_item",
            "source": "legistar",
            "event_id": event["EventId"],
            "matched_terms": sorted({t for _, ts in hits for t in ts}),
            "matched_items": len(hits),
        })
    meetings.sort(key=lambda m: m["starts_at"])
    return meetings


def _plain(value):
    cleaned = value.replace("<", "").replace(">", "")
    if not plain_text(cleaned):
        return ""
    return cleaned


def write_meetings(root, meetings, client_name):
    kit = root / "kit"
    kit.mkdir(mode=0o700, exist_ok=True)
    target = kit / "meetings.json"
    document = {"schema_version": 1, "source": "legistar", "client": client_name,
                "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "keywords": KEYWORDS.pattern, "meetings": meetings,
                "note": "Agenda matches are keyword hits, not confirmation that ALPR will be discussed. "
                        "Verify the posted agenda before publishing a meeting."}
    target.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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


def comment_kit_md(meeting, findings, campaign_name, base_url=None):
    """Markdown comment kit; ``findings`` are published finding dicts with title, summary,
    classification, confidence, path and sources[{title, locator, sha256}]."""
    lines = []
    when = str(meeting.get("starts_at", ""))[:10]
    lines.append(f"## Public comment: {_clean(meeting.get('body'))}, {when}")
    if meeting.get("agenda_item"):
        lines.append(f"Agenda item: {_clean(meeting['agenda_item'])}")
    lines.append("")
    lines.append("## Two-minute comment")
    lines.append(f"Good evening. My name is [name] and I live in [city]. I am speaking with "
                 f"{_clean(campaign_name)} about automated license plate readers.")
    if findings:
        plural = "" if len(findings) == 1 else "s"
        lines.append(f"We requested public records and published {len(findings)} reviewed finding{plural}, "
                     "each tied to a document hash:")
        for finding in findings[:3]:
            lines.append(f"- {_clean(finding.get('summary'))} "
                         f"({_clean(finding.get('classification')).replace('_', ' ')}, "
                         f"{_clean(finding.get('confidence')).replace('_', ' ')})")
    else:
        lines.append("We have requested the policy, the vendor agreement and the sharing records and will "
                     "publish what we receive with source hashes.")
    lines.append("We are not asking you to take our word for it. Every claim links to the record it came "
                 "from. Please read them before this item moves forward.")
    lines.append("")
    lines.append("## Three asks")
    lines.append("- " + ASKS.get(meeting.get("relevance") or "", DEFAULT_ASK))
    lines.append("- Require an annual public audit of searches, retention and outside-agency sharing, "
                 "presented at a noticed meeting.")
    lines.append("- Answer in writing which agencies can query this data today and under what written agreement.")
    lines.append("")
    lines.append("## Sources")
    if not findings:
        lines.append("- No published findings yet; cite the records request itself and its date.")
    for finding in findings:
        path = str(finding.get("path") or "")
        link = (base_url.rstrip("/") + path) if base_url else path
        lines.append(f"- **{_clean(finding.get('title') or finding.get('summary'))}** {link}")
        for source in finding.get("sources") or []:
            if isinstance(source, dict):
                lines.append(f"- {_clean(source.get('title') or 'Source record')}, "
                             f"{_clean(source.get('locator'))}, sha256 {str(source.get('sha256', ''))[:16]}")
    lines.append("")
    lines.append("Findings describe records and published rules as of the event date. This is not legal advice.")
    return "\n".join(lines) + "\n"
