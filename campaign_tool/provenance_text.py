"""Plain-language provenance; byte identity is not authorship or truth."""
from datetime import date

EXPLAINER = (
    "Record numbers are the first 12 characters of a SHA-256 fingerprint of the file exactly as we received "
    "it. A matching number shows the file has not changed since. It does not show who wrote the file or that what it says is true."
)
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def source_line(library_entry, finding_source, agency_name):
    """Return an HTML-escaped sentence and twelve-character record number."""
    from .site import e

    entry = library_entry
    sentence = f'From a document we hold: {e((entry or finding_source)["title"])}'
    if entry is not None:
        received = date.fromisoformat(entry["received_at"][:10])
        sentence += f', received {received.day} {MONTHS[received.month - 1]} {received.year}'
        if agency_name:
            sentence += f' from {e(agency_name)}'
        sentence += ". A public copy is linked." if entry.get("public_url") else ". The original is kept privately."
    else:
        sentence += ". Not yet listed in the source library."
    if finding_source.get("locator"):
        sentence += f' Where: {e(finding_source["locator"])}.'
    if finding_source.get("rule_id"):
        sentence += f' Rule {e(finding_source["rule_id"])}.'
    return sentence, e(finding_source["sha256"][:12])
