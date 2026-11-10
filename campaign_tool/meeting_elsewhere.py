"""DF1107-06: decisions elsewhere cited by a staged comment kit (``cited_elsewhere`` on content/meetings/<id>.json).

``meetings-stage --elsewhere N`` copies 1-3 validated entries of content/elsewhere.json (1-based, STORED order) into the
meeting as full snapshots. The kit text is built from the snapshot, never regenerated, so a reorder of elsewhere.json cannot
change a staged kit. Build and content-check resolve each snapshot by identity (place, body, decided_on, kind) and refuse a
missing entry or a changed cited fact by name. Nothing is fetched, ranked or sent.
"""
from .content import ContentError

MAX_CITED = 3
DISPLAY_LIMIT = 999999  # integers beyond this are omitted from refusals, not shown (not a selection range)
FILE = "content/elsewhere.json"
CITED = ("place", "body", "decided_on", "kind")  # identity: exact text, no fuzzy or case matching
SHOWN = ("state", "as_stated", "source_url")  # the facts printed in the kit line


def identity(entry):
    return tuple(entry[key] for key in CITED)


def describe(entry):
    return f"{entry['place']}, {entry['body']}, {entry['decided_on']} ({entry['kind']})"


def choose(entries, indices):
    """Snapshots of the entries at the 1-based ``indices`` (flag order). Refuses by flag and value, never deduplicates."""
    if (not isinstance(indices, (list, tuple)) or not 1 <= len(indices) <= MAX_CITED
            or any(type(i) is not int for i in indices)):
        # RF1155-1: never format the rejected operand (it may be deeply nested, huge or self-referential)
        raise ContentError(f"--elsewhere: give 1 to {MAX_CITED} whole numbers")
    if entries is None:
        raise ContentError(f"--elsewhere: {FILE} not found; nothing to cite")
    for position, index in enumerate(indices):
        if index in indices[:position]:  # an earlier equal index passed the range check below, so it is small
            raise ContentError(f"--elsewhere {index}: given more than once")
        if not 1 <= index <= len(entries):
            if -DISPLAY_LIMIT <= index <= DISPLAY_LIMIT:  # numeric comparison only: no decimal conversion of a huge int
                raise ContentError(f"--elsewhere {index}: out of range ({FILE} has {len(entries)} entries)")
            raise ContentError(f"--elsewhere: index out of range ({FILE} has {len(entries)} entries; "
                               "supplied integer omitted)")
    return [dict(entries[index - 1]) for index in indices]


def load_cited_elsewhere(value, label, today=None):
    """Structural check of a stored ``cited_elsewhere``: 1-3 full entries that pass the DF1107-04 rules, unique identities."""
    from .content_elsewhere import load_elsewhere
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CITED:
        raise ContentError(f"{label}: must be a list of 1 to {MAX_CITED} elsewhere entries")
    try:
        return load_elsewhere(value, today=today)
    except ContentError as exc:
        raise ContentError(str(exc).replace("elsewhere.json", label, 1)) from None


def reference_problems(meetings, elsewhere, skip_meeting_id=None):
    """``meetings``: [(file label, loaded meeting)]. One line per cited entry that is gone or whose printed facts changed."""
    problems = []
    for label, meeting in meetings:
        if meeting.get("id") == skip_meeting_id and skip_meeting_id is not None:
            continue  # meetings-stage --replace repairs exactly this meeting
        current = {identity(e): e for e in elsewhere or []}
        for n, snap in enumerate(meeting.get("cited_elsewhere", ())):
            where = f"{label}.cited_elsewhere[{n}]"
            found = current.get(identity(snap))
            if found is None:
                why = f"{FILE} has no entry" if elsewhere is not None else f"{FILE} not found; no entry"
                problems.append(f"{where}: {why} for {describe(snap)}; restage with meetings-stage --replace")
                continue
            changed = [key for key in SHOWN if found[key] != snap[key]]
            if changed:
                problems.append(f"{where}: {describe(snap)} changed in {FILE} ({', '.join(changed)}); "
                                "restage with meetings-stage --replace")
    return problems
