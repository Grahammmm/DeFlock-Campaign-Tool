"""DF0237-04: which receiving agencies occur across the organizer-transcribed vendor-portal sharing lists (DF1207-08).

A view only: it reads the already validated ``agency["sharing"]`` objects, stores nothing and never changes a name. The
count is how many of the campaign's agencies list a receiver; it is not a number of plates, transfers, searches or
accesses. Two spellings merge only when they differ in case, whitespace or one trailing period.
"""

GROUPS = ("out_of_state", "federal", "in_state")  # display order; the groups are disjoint


def recipient_key(name, state, kind):
    """The identity of one listed receiver: the name casefolded with whitespace collapsed and ONE trailing ASCII period
    removed, plus its exact state and kind. No accent folding, abbreviation expansion or fuzzy matching. A name that
    normalizes to nothing keeps a tagged copy of the original instead of sharing one empty key."""
    text = " ".join(name.casefold().split())
    if text.endswith("."):
        text = text[:-1].strip()
    return ((0, text) if text else (1, name), state, kind)


def _group(state, kind, home_state):
    if kind == "federal" or state == "US":
        return "federal"
    return "in_state" if state == home_state else "out_of_state"


def summarize_sharing(agencies, home_state):
    """-> fresh rows ``{group, key, name, aliases, state, kind, listed_by}``, grouped out of state, federal, then in state and
    ordered inside a group by normalized name, state, kind and display name (never by count).

    ``name`` is the smallest original spelling (code-point order); ``aliases`` are the other exact spellings, sorted.
    ``listed_by`` has one entry per sending agency, sorted by name then agency id, each with that agency's own
    ``checked_on`` and ``source_url``; a sender that lists a receiver twice counts once."""
    found = {}
    for agency in agencies:
        sharing = agency.get("sharing")
        if not sharing:
            continue
        for recipient in sharing["recipients"]:
            key = recipient_key(recipient["name"], recipient["state"], recipient["kind"])
            entry = found.setdefault(key, {"names": set(), "senders": {}})
            entry["names"].add(recipient["name"])
            entry["senders"].setdefault(agency["agency_id"], {
                "agency_id": agency["agency_id"], "agency_name": agency["name"], "slug": agency["slug"],
                "checked_on": sharing["checked_on"], "source_url": sharing["source_url"]})
    rows = []
    for key, entry in found.items():
        names = sorted(entry["names"])
        state, kind = key[1], key[2]
        rows.append({"group": _group(state, kind, home_state), "key": key, "name": names[0], "aliases": names[1:],
                     "state": state, "kind": kind,
                     "listed_by": sorted(entry["senders"].values(), key=lambda s: (s["agency_name"].casefold(), s["agency_id"]))})
    rows.sort(key=lambda r: (GROUPS.index(r["group"]), r["key"][0], r["state"], r["kind"], r["name"]))
    return rows
