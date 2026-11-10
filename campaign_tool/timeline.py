"""Public campaign dates derived only from the validated public content model."""
from .labels import DEFAULTS, html_text

LEAD = DEFAULTS["timeline.lead"]
KINDS = ("records_received", "finding_published", "correction", "meeting", "records_requested", "request_update",
         "decision", "agency_action", "finding_update", "statement", "coverage")
ACTIONS_NOTE = " " + DEFAULTS["timeline.actions_note"]  # DF0237-01: only when an event exists
# DF1109-07: the lead names a new kind only when at least one such event is shown, in this fixed order.
ALSO_SHOWN = tuple((kind, DEFAULTS["timeline.kind_" + kind]) for kind in ("finding_update", "statement", "coverage"))


def also_shown(events, labels=DEFAULTS):
    """`` Also shown: <kinds>.`` for the DF1109-07 kinds present in ``events``; "" when none is.

    Plain text; the one boundary space is added here, never taken from a label (DF1109-16)."""
    present = {event["kind"] for event in events}
    names = [labels["timeline.kind_" + kind] for kind, _ in ALSO_SHOWN if kind in present]
    if not names:
        return ""
    if len(names) == 1:
        listed = names[0]
    elif len(names) == 2:
        listed = labels["timeline.join_two"].format(first=names[0], second=names[1])
    else:
        listed = labels["timeline.join_three"].format(first=names[0], second=names[1], third=names[2])
    return " " + labels["timeline.also_shown"].format(kinds=listed)


def _day(value):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()


def timeline_events(model, today, agency_pages=None):
    """Return plain-text events, newest day first; escaping belongs to rendering.

    Dates retain the source's calendar day, matching the existing meeting page.
    Equal day/kind/href events use text as a final deterministic tie-breaker.
    ``agency_pages`` maps agency_id to its page path; other agencies link to their agencies.html anchor.
    Wording comes from the ``timeline.event_*`` labels (DF1109-16); content values are inserted once, never re-read
    as a template.
    """
    from .site import OUTCOME_LABELS, asked_sentence

    names = {a["agency_id"]: a["name"] for a in model["agencies"]}
    labels = model.get("labels") or DEFAULTS  # the kind label the agency page shows, overrides included
    events = []

    def add(value, kind, text, href, **extra):
        events.append({"date": _day(value), "kind": kind, "text": text, "href": href, **extra})

    def say(key, **values):
        return labels["timeline.event_" + key].format(**values)

    for agency in model["agencies"]:
        href = (agency_pages or {}).get(agency["agency_id"]) or f'/agencies.html#{agency["slug"]}'
        for action in agency.get("actions", []):  # DF0237-01: transcribed from the cited official source, never inferred
            add(action["on"], "agency_action", say("agency_action", agency=agency["name"], statement=action["as_stated"],
                                                   kind=labels["agency.action_kind_" + action["kind"]]), href,
                source_url=action["source_url"])
        for item in agency.get("asked", []):
            add(item["asked_on"], "records_requested", say("records_requested", agency=agency["name"], topic=item["topic"]),
                href)
            if item.get("updated_on"):
                add(item["updated_on"], "request_update",
                    say("request_update", agency=agency["name"], topic=item["topic"], state=asked_sentence(item, today)), href)

    for source in model["sources"]:
        if source.get("received_at"):
            name = names.get(source["agency_id"], labels["timeline.unknown_agency"])
            add(source["received_at"], "records_received", say("records_received", agency=name, title=source["title"]),
                f'/sources.html#{source["fragment"]}')
    for finding in model["findings"]:
        href = f'/findings/{finding["slug"]}.html'
        add(finding["published_at"], "finding_published", say("finding_published", title=finding["title"]), href)
        for correction in finding["corrections"]:
            add(correction["date"], "correction", say("correction", title=finding["title"]), href + "#corrections")
        for update in finding.get("updates", ()):  # DF1109-07: one event per update, never merged; the note stays on its page
            add(update["date"], "finding_update", say("finding_update", title=finding["title"]), href + "#updates")
    for meeting in model["meetings"]:
        # Cancellation must never masquerade as a scheduled or held meeting.
        if meeting.get("status") == "cancelled":
            continue
        key = "meeting_scheduled" if _day(meeting["starts_at"]) >= today.isoformat() else "meeting"
        add(meeting["starts_at"], "meeting", say(key, body=meeting["body"]), f'/meetings.html#meeting-{meeting["id"]}')
        outcome = meeting.get("outcome")
        if outcome:
            add(outcome["on"], "decision", say("decision", body=meeting["body"], outcome=OUTCOME_LABELS[outcome["result"]]),
                f'/meetings/{meeting["id"]}.html', source_url=outcome["source_url"])
    agencies = {a["agency_id"]: a for a in model["agencies"]}
    for row in model.get("statements", ()):  # DF1109-07: the agency page that shows the statement, plus its official source
        agency = agencies[row["agency_id"]]
        href = (agency_pages or {}).get(agency["agency_id"]) or f'/agencies/{agency["slug"]}.html'
        add(row["said_on"], "statement", say("statement", official=row["official"], agency=agency["name"]), href,
            source_url=row["source_url"])
    for entry in model.get("coverage") or ():  # DF1109-07: press is not an official record, so no source link
        add(entry["published_on"], "coverage", say("coverage", outlet=entry["outlet"]), "/news.html")
    events.sort(key=lambda event: (KINDS.index(event["kind"]), event["href"], event["text"]))
    events.sort(key=lambda event: event["date"], reverse=True)
    return events


def render_timeline(site, events):
    """Render the body through the existing escaping helper and shared frame.

    Month headings come from the ``month.01``..``month.12`` labels by the numeric ``YYYY-MM``, never from the
    process locale (DF1109-16)."""
    from .site import e

    labels = getattr(site, "labels", None) or DEFAULTS
    note = " " + html_text(labels["timeline.actions_note"]) if any(event["kind"] == "agency_action" for event in events) else ""
    parts = [f'<section class="section" id="content"><h2>{html_text(labels["timeline.heading"])}</h2>',
             f'<p>{html_text(labels["timeline.lead"])}{note}{e(also_shown(events, labels))}</p>']
    month = None
    for event in events:
        current = event["date"][:7]
        if current != month:
            if month is not None:
                parts.append('</ul>')
            year, number = current.split("-")
            label = labels["timeline.month_year"].format(month=labels["month." + number], year=year)
            parts.append(f'<h3><time datetime="{e(current)}">{e(label)}</time></h3><ul>')
            month = current
        parts.append(f'<li><time datetime="{e(event["date"])}">{e(event["date"])}</time> · '
                     f'<a href="{e(event["href"])}">{e(event["text"])}</a>'
                     + (f' · <a href="{e(event["source_url"])}" rel="noopener noreferrer">'
                        f'{html_text(labels["timeline.source_link"])}</a>' if event.get("source_url") else "") + '</li>')
    if month is not None:
        parts.append('</ul>')
    else:
        parts.append(f'<p>{html_text(labels["timeline.empty"])}</p>')
    parts.append('</section>\n')
    return "".join(parts)
