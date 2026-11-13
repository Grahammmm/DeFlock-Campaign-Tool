"""Read-only Home summary of asked items not produced; machine reading, not a finding."""
import urllib.parse

from . import console_board as cb, console_layout, item_tiers
from .outcomes.reading import esc

LABEL = "Machine reading of the response; review required; not proof that records exist."


def outstanding(model):
    """Non-produced asks, ordered by tier, agency, request and source document order."""
    if model["tiers"]["state"] != "ok":
        return []
    requests = {}
    for req in cb._requests(model):
        for ident in (req.get("request_id"), req.get("ledger_request_id")):
            if ident is not None:
                requests[(req.get("agency_key"), ident)] = req
    rows = []
    for req in model["tiers"]["doc"]["requests"]:
        agency, ident = req.get("agency_key"), req.get("request_id")
        timeline = requests.get((agency, ident), {})
        asks = req["asks"]
        partly = any(a["state"] in item_tiers.DONE for a in asks) and any(
            a["state"] not in item_tiers.DONE for a in asks)
        for ask in asks:
            if ask["state"] in item_tiers.DONE:
                continue
            rows.append({"agency_key": agency, "agency": timeline.get("agency") or agency,
                         "request_id": ident, "ask_id": ask["ask_id"], "tier": ask["tier"],
                         "tier_name": ask["tier_name"], "state": ask["state"], "text": ask["text"],
                         "request_partly_answered": partly, "closed": bool(timeline.get("closed")),
                         "link": "/agency?agency=%s#%s" % (
                             urllib.parse.quote(agency or "", safe=""), urllib.parse.quote(ident or "", safe=""))})
    # Stable sort preserves document order within each request and tier.
    return sorted(rows, key=lambda r: (r["tier"] if r["tier"] is not None else 4,
                                       r["agency"] or "", r["agency_key"] or "", r["request_id"] or ""))


def render_outstanding(model):
    out = ['<section id="outstanding"><h2>Outstanding information</h2>', '<p>%s</p>' % LABEL]
    if model["tiers"]["state"] != "ok":
        return "\n".join(out + ['<p role="status">%s</p></section>' % esc(console_layout.unavailable_note("Outstanding information", model["tiers"].get("code")))])
    rows = outstanding(model)
    if not rows:
        out.append("<p>No outstanding items on our records.</p>")
    for tier in (1, 2, 3, None):
        group = [r for r in rows if r["tier"] == tier]
        if not group:
            continue
        heading = "Tier %s: %s" % (tier, group[0]["tier_name"]) if tier else "Untiered"
        out.append("<h3>%s (%d)</h3><table><thead><tr>%s</tr></thead><tbody>" % (
            esc(heading), len(group), "".join('<th scope="col">%s</th>' % h for h in (
                "Agency", "Request", "Item", "State", "Request partly answered", "Open"))))
        for row in group:
            request = row["request_id"] or "(no request id)"
            if row["closed"]:
                request += " (closed by agency)"
            cells = [esc(row["agency"]), esc(request), esc(row["text"][:160]), esc(row["state"]),
                     "Yes" if row["request_partly_answered"] else "No", '<a href="%s">Open</a>' % esc(row["link"])]
            out.append("<tr>%s</tr>" % "".join("<td>%s</td>" % cell for cell in cells))
        out.append("</tbody></table>")
    return "\n".join(out + ["</section>"])
