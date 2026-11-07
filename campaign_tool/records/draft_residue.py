"""Exact draft markers only; pure inspection of the approved letter text."""
from .draft_templates import DRAFT_BANNER, _FOLLOWUP_FOOTER, _LADDER
from .followup_drafter import BANNER


def codes(body):
    """Return sorted, unique residue codes without IO or heuristic matching."""
    found = set()
    if any(line.startswith("# Draft (") for line in body.splitlines()):
        found.add("draft_heading")
    if any(banner in body for banner in (DRAFT_BANNER, BANNER)):
        found.add("draft_banner")
    for marker, code in (("[EDIT ME", "edit_placeholder"),
                         ("Notes for the reviewer", "reviewer_notes"),
                         ("BEFORE SENDING", "before_sending")):
        if marker in body:
            found.add(code)
    if any(footer in body for footer in (_LADDER, _FOLLOWUP_FOOTER)):
        found.add("draft_footer")
    return sorted(found)
