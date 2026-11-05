"""Build a newsletter manifest from a campaign's own reviewed ``content/``.

The manifest has the same shape as the Worker's ``newsletterManifest`` (see
``draft.py``), so offline campaigns get the same draft. Only published findings
are loaded by ``content.load_content``; cancelled meetings are left out here.
The since/upcoming window is applied by ``build_draft``, not duplicated. Each
finding carries its ``corrections`` (``[{"date": "YYYY-MM-DD", "note"}]``) so a
finding published before ``since`` but corrected on or after it is listed by
``build_draft`` under "Corrections", not as a new finding. Each meeting carries
``participation`` (``content.load_participation``'s dict, or None when unverified).
``records`` (DF1422-15) lists ``content/sources.json`` entries by public library fields only: title, agency name,
received date, the 12-character record prefix, the public copy link and the sources page anchor (never the
full hash, ``note``, ``pages`` or ``public_copy``).
``map`` (DF1422-16) is added only when ``content/map/history.json`` has a record: its validated records, the map's
group label and the cameras page path (nothing else from the map); without history the manifest has no ``map`` key.
``requests`` (DF1207-05) comes from each agency's public ``asked`` items only (never ``kit/``): the
agency page's own state sentence (``site.asked_sentence``) and the agency page path.
``events`` (DF1107-09) is added only when ``content/events.json`` holds events: id, title, starts_at, ends_at, venue or
online and the ``/events.html#event-<id>`` path; never the description, accessibility note or details URL.
"""
from datetime import datetime, timezone

from ..content import load_content, utc_moment
from ..site import asked_sentence


def _requests(model, today):
    """Public ``asked`` items, newest activity first; ``path`` is the agency page, else its agencies-list anchor."""
    pages = {f["agency_id"] for f in model["findings"]} | {s["agency_id"] for s in model["sources"]}
    rows = []
    for agency in model["agencies"]:
        path = f"/agencies/{agency['slug']}.html" if agency["agency_id"] in pages else f"/agencies.html#{agency['slug']}"
        for item in agency.get("asked", []):
            rows.append({"agency": agency["name"], "topic": item["topic"], "asked_on": item["asked_on"],
                         "updated_on": item.get("updated_on"), "state": item["state"],
                         "sentence": asked_sentence(item, today), "path": path})
    rows.sort(key=lambda r: (r["agency"], r["topic"]))
    rows.sort(key=lambda r: max(r["asked_on"], r["updated_on"] or ""), reverse=True)
    return rows


def _records(model):
    """Library entries, newest received first; ``agency`` is the agency name, else the ``agency_id`` shown by the sources page."""
    names = {a["agency_id"]: a["name"] for a in model["agencies"]}
    rows = [{"title": s["title"], "agency": names.get(s["agency_id"], s["agency_id"]), "received_at": s["received_at"],
             "prefix": s["sha256"][:12], "public_url": s["public_url"], "path": f"/sources.html#{s['fragment']}"}
            for s in model["sources"]]
    rows.sort(key=lambda r: (r["prefix"], r["title"]))
    rows.sort(key=lambda r: utc_moment(r["received_at"]), reverse=True)
    return rows


def _events(model):
    """``{"events": [...]}`` from the validated public events, or ``{}`` without any (the manifest then has no key)."""
    rows = [{"id": e["id"], "title": e["title"], "starts_at": e["starts_at"], "ends_at": e["ends_at"],
             **({"online": True} if e.get("online") else {"venue": e["venue"]}), "path": f"/events.html#event-{e['id']}"}
            for e in model.get("events") or ()]
    return {"events": rows} if rows else {}


def manifest_from_content(root, cfg, since=None, now=None):
    """Return a schema-1 manifest; ``since`` is an ISO timestamp with offset, or None; ``now`` dates the request sentences."""
    model = load_content(root)
    if model is None:
        raise ValueError("--from-content needs a content/ directory")
    findings = [{"title": f["title"], "summary": f["summary"], "classification": f["classification"],
                 "confidence": f["confidence"], "path": f"/findings/{f['slug']}.html",
                 "published_at": f["published_at"], "corrected": bool(f["corrections"]),
                 "corrections": [{"date": c["date"], "note": c["note"]} for c in f["corrections"]]}
                for f in model["findings"]]
    meetings = [{"body": m["body"], "starts_at": m["starts_at"], "agenda_item": m["agenda_item"],
                 "agenda_url": m["agenda_url"], "relevance": None,
                 "participation": m.get("participation")}  # DF2037-15: verified facts, or None (check the agenda)
                for m in model["meetings"] if m.get("status") != "cancelled"]
    # DF2237-12: only outcomes an organizer recorded (verified_at on or after the day of `since`);
    # a meeting without one is never listed as decided.
    day = since[:10] if since else ""
    recorded = sorted((m for m in model["meetings"] if m.get("outcome") and m.get("status") != "cancelled"
                       and m["outcome"]["verified_at"] >= day), key=lambda m: (m["outcome"]["on"], m["id"]))
    decisions = [{"body": m["body"], "on": m["outcome"]["on"], "result": m["outcome"]["result"],
                  "vote": m["outcome"].get("vote"), "source_url": m["outcome"]["source_url"],
                  "meeting_path": f"/meetings/{m['id']}.html"} for m in recorded]
    history = (model.get("map") or {}).get("history")
    camera_map = {"map": {"records": history, "label": model["map"]["group_label"], "path": "/cameras.html"}} if history else {}
    return {"schema_version": 1,
            "campaign": {"name": cfg["name"], "base_url": model["site"]["base_url"], "county_name": cfg["county"]},
            "since": since, "findings": findings, "meetings": meetings, "decisions": decisions,
            "requests": _requests(model, (now or datetime.now(timezone.utc)).date()), "records": _records(model), **camera_map,
            **_events(model)}
