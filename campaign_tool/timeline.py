"""Public campaign dates derived only from the validated public content model."""
from datetime import datetime

LEAD = ("Dates from published sources, findings, corrections and meetings. "
        "Request and response dates are not shown here yet.")
KINDS = ("records_received", "finding_published", "correction", "meeting")


def _day(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()


def timeline_events(model, today):
    """Return plain-text events, newest day first; escaping belongs to rendering.

    Dates retain the source's calendar day, matching the existing meeting page.
    Equal day/kind/href events use text as a final deterministic tie-breaker.
    """
    names = {a["agency_id"]: a["name"] for a in model["agencies"]}
    events = []

    def add(value, kind, text, href):
        events.append({"date": _day(value), "kind": kind, "text": text, "href": href})

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
    for meeting in model["meetings"]:
        # Cancellation must never masquerade as a scheduled or held meeting.
        if meeting.get("status") == "cancelled":
            continue
        prefix = "Scheduled meeting" if _day(meeting["starts_at"]) >= today.isoformat() else "Meeting"
        add(meeting["starts_at"], "meeting", f'{prefix}: {meeting["body"]}',
            f'/meetings.html#meeting-{meeting["id"]}')
    events.sort(key=lambda event: (KINDS.index(event["kind"]), event["href"], event["text"]))
    events.sort(key=lambda event: event["date"], reverse=True)
    return events


def render_timeline(site, events):
    """Render the body through the existing escaping helper and shared frame."""
    from .site import e

    parts = ['<section class="section" id="content"><h2>Campaign timeline</h2>', f'<p>{LEAD}</p>']
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
                     f'<a href="{e(event["href"])}">{e(event["text"])}</a></li>')
    if month is not None:
        parts.append('</ul>')
    else:
        parts.append('<p>Nothing dated has been published yet.</p>')
    parts.append('</section>\n')
    return "".join(parts)
