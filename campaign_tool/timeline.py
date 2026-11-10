"""Public campaign dates derived only from the validated public content model."""
from datetime import datetime

LEAD = "Dates from published sources, findings, corrections, records requests, meetings and recorded decisions."
KINDS = ("records_received", "finding_published", "correction", "meeting", "records_requested", "request_update",
         "decision", "agency_action", "finding_update", "statement", "coverage")
ACTIONS_NOTE = " Agency actions are transcribed from their cited official sources."  # DF0237-01: only when an event exists
# DF1109-07: the lead names a new kind only when at least one such event is shown, in this fixed order.
ALSO_SHOWN = (("finding_update", "finding updates"), ("statement", "public statements"), ("coverage", "press coverage"))


def also_shown(events):
    """`` Also shown: <kinds>.`` for the DF1109-07 kinds present in ``events``; "" when none is."""
    present = {event["kind"] for event in events}
    names = [name for kind, name in ALSO_SHOWN if kind in present]
    if not names:
        return ""
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f" Also shown: {listed}."


def _day(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()


def timeline_events(model, today, agency_pages=None):
    """Return plain-text events, newest day first; escaping belongs to rendering.

    Dates retain the source's calendar day, matching the existing meeting page.
    Equal day/kind/href events use text as a final deterministic tie-breaker.
    ``agency_pages`` maps agency_id to its page path; other agencies link to their agencies.html anchor.
    """
    from .labels import DEFAULTS
    from .site import OUTCOME_LABELS, asked_sentence

    names = {a["agency_id"]: a["name"] for a in model["agencies"]}
    labels = model.get("labels") or DEFAULTS  # the kind label the agency page shows, overrides included
    events = []

    def add(value, kind, text, href, **extra):
        events.append({"date": _day(value), "kind": kind, "text": text, "href": href, **extra})

    for agency in model["agencies"]:
        href = (agency_pages or {}).get(agency["agency_id"]) or f'/agencies.html#{agency["slug"]}'
        for action in agency.get("actions", []):  # DF0237-01: transcribed from the cited official source, never inferred
            add(action["on"], "agency_action", f'{agency["name"]}: {labels["agency.action_kind_" + action["kind"]]}: {action["as_stated"]}', href,
                source_url=action["source_url"])
        for item in agency.get("asked", []):
            add(item["asked_on"], "records_requested", f'Records requested from {agency["name"]}: {item["topic"]}', href)
            if item.get("updated_on"):
                add(item["updated_on"], "request_update",
                    f'{agency["name"]}: {item["topic"]} — {asked_sentence(item, today)}', href)

    for source in model["sources"]:
        if source.get("received_at"):
            name = names.get(source["agency_id"], "an agency")
            add(source["received_at"], "records_received",
                f'Records received from {name}: {source["title"]}',
                f'/sources.html#{source["fragment"]}')
    for finding in model["findings"]:
        href = f'/findings/{finding["slug"]}.html'
        add(finding["published_at"], "finding_published", f'Finding published: {finding["title"]}', href)
        for correction in finding["corrections"]:
            add(correction["date"], "correction", f'Correction to {finding["title"]}', href + "#corrections")
        for update in finding.get("updates", ()):  # DF1109-07: one event per update, never merged; the note stays on its page
            add(update["date"], "finding_update", f'Update to {finding["title"]}', href + "#updates")
    for meeting in model["meetings"]:
        # Cancellation must never masquerade as a scheduled or held meeting.
        if meeting.get("status") == "cancelled":
            continue
        prefix = "Scheduled meeting" if _day(meeting["starts_at"]) >= today.isoformat() else "Meeting"
        add(meeting["starts_at"], "meeting", f'{prefix}: {meeting["body"]}',
            f'/meetings.html#meeting-{meeting["id"]}')
        outcome = meeting.get("outcome")
        if outcome:
            add(outcome["on"], "decision", f'Decision at {meeting["body"]}: {OUTCOME_LABELS[outcome["result"]]}',
                f'/meetings/{meeting["id"]}.html', source_url=outcome["source_url"])
    agencies = {a["agency_id"]: a for a in model["agencies"]}
    for row in model.get("statements", ()):  # DF1109-07: the agency page that shows the statement, plus its official source
        agency = agencies[row["agency_id"]]
        href = (agency_pages or {}).get(agency["agency_id"]) or f'/agencies/{agency["slug"]}.html'
        add(row["said_on"], "statement", f'{row["official"]}, {agency["name"]}: public statement', href,
            source_url=row["source_url"])
    for entry in model.get("coverage") or ():  # DF1109-07: press is not an official record, so no source link
        # frozen exact text (spec design 4); a labels.json override of news.title does not change it (DF1109-16 owns the sweep)
        add(entry["published_on"], "coverage", f'In the news: {entry["outlet"]}', "/news.html")
    events.sort(key=lambda event: (KINDS.index(event["kind"]), event["href"], event["text"]))
    events.sort(key=lambda event: event["date"], reverse=True)
    return events


def render_timeline(site, events):
    """Render the body through the existing escaping helper and shared frame."""
    from .site import e

    note = ACTIONS_NOTE if any(event["kind"] == "agency_action" for event in events) else ""
    parts = ['<section class="section" id="content"><h2>Campaign timeline</h2>', f'<p>{LEAD}{note}{e(also_shown(events))}</p>']
    month = None
    for event in events:
        current = event["date"][:7]
        if current != month:
            if month is not None:
                parts.append('</ul>')
            label = datetime.strptime(current, "%Y-%m").strftime("%B %Y")
            parts.append(f'<h3><time datetime="{e(current)}">{e(label)}</time></h3><ul>')
            month = current
        parts.append(f'<li><time datetime="{e(event["date"])}">{e(event["date"])}</time> · '
                     f'<a href="{e(event["href"])}">{e(event["text"])}</a>'
                     + (f' · <a href="{e(event["source_url"])}" rel="noopener noreferrer">official record</a>'
                        if event.get("source_url") else "") + '</li>')
    if month is not None:
        parts.append('</ul>')
    else:
        parts.append('<p>Nothing dated has been published yet.</p>')
    parts.append('</section>\n')
    return "".join(parts)
