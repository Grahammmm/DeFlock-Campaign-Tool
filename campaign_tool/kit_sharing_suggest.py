"""DF1107-16 kit-agency suggest --from-sharing: receiving agencies as private request leads. Read-only.

The recorded DF1207-08 sharing lists (content/agencies.json, ``profile.sharing``) name the agencies a vendor portal says
receive each campaign agency's data. This module matches the in-state receivers to the campaign's full offline seed by
exact name (the DF0237-04 rule: case, whitespace and one trailing period only; no fuzzy matching) and reports the
unique matches, the names it cannot place, and counts of what it leaves out. It never selects, writes, fetches or sends.
Receiving data is a reason to ask, not evidence of misuse.
"""
from pathlib import Path

from . import content, discovery, kit_agencies
from .sharing_summary import recipient_key

NOTICE = "Receiving data is a reason to ask, not evidence of misuse"
TRAILER = "Private, read-only; nothing was selected, written, fetched or sent."
EXCLUSIONS = (("federal", "federal"), ("private", "private"), ("out_of_state", "out-of-state"))


def _classify(row, home_state):
    """Disjoint precedence: federal, private, out of state, else the eligible in-state remainder."""
    if row["kind"] == "federal" or row["state"] == "US":
        return "federal"
    if row["kind"] == "private":
        return "private"
    return None if row["state"] == home_state else "out_of_state"


def _seed_index(seed):
    """{agency_id: entry} across every county; the same id repeated identically is one agency, differently is refused."""
    found = {}
    for county in seed["counties"]:
        for entry in county["agencies"]:
            known = found.setdefault(entry["agency_id"], entry)
            if (known["name"], known["kind"]) != (entry["name"], entry["kind"]):
                raise ValueError(f"seed defines agency_id {entry['agency_id']} inconsistently")
    return found


def build_report(root, seed=None):
    """Read config, the kit, the campaign agencies and the seed; return the report dict. Writes nothing."""
    from .cli import read_config  # local import: cli imports this module lazily
    root = Path(root)
    state = read_config(root)["state"]
    jurisdiction = "us-" + state.lower()
    kit_entries = {e["agency_id"]: e for e in kit_agencies.load_agencies(root)}
    seed = seed if seed is not None else discovery.load_seed(jurisdiction)
    seed_agencies = _seed_index(seed)
    names = {}
    for agency_id, entry in seed_agencies.items():
        names.setdefault(recipient_key(entry["name"], None, None)[0], set()).add(agency_id)

    senders = [a for a in content.load_campaign_agencies(root) if a.get("sharing")]
    excluded = {group: 0 for group, _label in EXCLUSIONS}
    receivers = {}
    for agency in senders:
        for row in agency["sharing"]["recipients"]:
            group = _classify(row, state)
            if group is not None:
                excluded[group] += 1
                continue
            entry = receivers.setdefault(recipient_key(row["name"], row["state"], row["kind"]),
                                         {"names": set(), "senders": set(), "state": row["state"], "kind": row["kind"]})
            entry["names"].add(row["name"])
            entry["senders"].add(agency["agency_id"])

    leads, unmatched, ambiguous = {}, [], []
    for key in sorted(receivers):
        entry, ids = receivers[key], names.get(key[0], set())
        item = {"names": sorted(entry["names"]), "state": entry["state"], "kind": entry["kind"],
                "senders": set(entry["senders"])}
        if len(ids) == 1:
            agency_id = next(iter(ids))
            lead = leads.setdefault(agency_id, {"agency_id": agency_id, "name": seed_agencies[agency_id]["name"],
                                                "names": set(), "senders": set()})
            lead["names"].update(item["names"])
            lead["senders"] |= item["senders"]
        elif ids:
            ambiguous.append({**item, "candidates": [{"agency_id": i, "name": seed_agencies[i]["name"]}
                                                      for i in sorted(ids)]})
        else:
            unmatched.append(item)
    suggestions = []
    for lead in leads.values():
        kit_entry = kit_entries.get(lead["agency_id"])
        status = "not in kit" if kit_entry is None else "selected" if kit_entry.get("selected", True) else "not selected"
        suggestions.append({**lead, "names": sorted(lead["names"]), "sender_count": len(lead["senders"]), "status": status})
    suggestions.sort(key=lambda s: (recipient_key(s["name"], None, None)[0], s["agency_id"]))
    for item in unmatched + ambiguous:
        item["sender_count"] = len(item["senders"])
    return {"state": state, "sender_count": len(senders), "recorded": bool(senders), "suggestions": suggestions,
            "unmatched": unmatched, "ambiguous": ambiguous, "excluded": excluded}


def _check_text(report):
    for item in report["suggestions"] + report["unmatched"] + report["ambiguous"]:
        for name in item["names"] + [item.get("name", "")]:
            if kit_agencies._has_control(name):
                raise ValueError("a receiving agency name contains a control character")
    for item in report["ambiguous"]:
        for candidate in item["candidates"]:
            if kit_agencies._has_control(candidate["name"]):
                raise ValueError("a receiving agency name contains a control character")


def render(report):
    _check_text(report)
    lines = ["Receiving agencies as request leads", NOTICE + ".", ""]
    if not report["recorded"]:
        lines += ["No recorded sharing lists.", ""]
    excluded = report["excluded"]
    lines += [f"Unique suggested seed agencies: {len(report['suggestions'])}",
              f"Eligible unmatched receiver identities: {len(report['unmatched'])}",
              f"Eligible ambiguous receiver identities: {len(report['ambiguous'])}",
              "Excluded listings (not suggested; one per sending agency and listed receiver): "
              + ", ".join(f"{label} {excluded[group]}" for group, label in EXCLUSIONS), ""]
    lines.append(f"Suggested agencies ({state_note(report)})")
    for s in report["suggestions"]:
        lines.append(f"  {s['name']} [{s['agency_id']}] - {s['status']} - listed by {s['sender_count']} of the campaign's "
                     f"agencies; written as: " + "; ".join(s["names"]))
    if not report["suggestions"]:
        lines.append("  none")
    lines += ["", "Unmatched names (no seed agency has this name)"]
    for u in report["unmatched"]:
        lines.append(f"  {'; '.join(u['names'])} - {u['state']}, {u['kind']} - listed by {u['sender_count']}")
    if not report["unmatched"]:
        lines.append("  none")
    lines += ["", "Ambiguous names (more than one seed agency has this name; none is recommended)"]
    for a in report["ambiguous"]:
        lines.append(f"  {'; '.join(a['names'])} - {a['state']}, {a['kind']} - listed by {a['sender_count']}; candidates: "
                     + "; ".join(f"{c['agency_id']} ({c['name']})" for c in a["candidates"]))
    if not report["ambiguous"]:
        lines.append("  none")
    lines += ["", "'not in kit' means the seed agency is absent from kit/agencies.json; kit-agency select does not add it.",
              "Adding one is a separate step you take yourself (kit-agency add or import).", NOTICE + ".", TRAILER]
    return "\n".join(lines)


def state_note(report):
    return f"in-state, {report['state']}; matched to the seed by exact name"


def suggest_from_sharing(root, seed=None):
    return render(build_report(root, seed=seed))
