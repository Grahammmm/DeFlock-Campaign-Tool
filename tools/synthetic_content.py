"""Fixed synthetic ``content/`` for a campaign; synthetic text only, no real records.

``write_synthetic_content(root, label)`` gives the site builder a small but
complete campaign (site settings, two published findings, one meeting, one
agency) so tools can exercise every generated page offline. Source hashes are
computed from the synthetic bytes at run time.
"""
import hashlib
import json
from pathlib import Path

AGENCY_ID = "ca-synthetic-county-sheriff"


def _source_bytes(label):
    return [f"Synthetic {label} record {number}: placeholder text, not a real document.\n".encode("utf-8")
            for number in (1, 2)]


def _finding(number, label, digest, corrections):
    return {
        "id": f"synthetic-{number}",
        "title": f"Synthetic {label} finding {number}",
        "slug": f"synthetic-finding-{number}",
        "summary": f"Synthetic summary {number} for the {label} fixture.",
        "body_md": f"Synthetic body text for finding {number}.\n\nIt describes no real agency or record.",
        "classification": "documented_fact",
        "confidence": "verified",
        "event_date": f"2026-01-0{number}",
        "sources": [{"sha256": digest, "locator": f"synthetic-record-{number}.txt page 1",
                     "title": f"Synthetic record {number}"}],
        "published_at": f"2026-02-0{number}T12:00:00Z",
        "corrections": corrections,
        "state": "published",
        "author": "Synthetic author",
        "limitations": ["Synthetic fixture text only."],
        "counterevidence": [],
        "agency_id": AGENCY_ID,
    }


def write_synthetic_content(root, label):
    """Write the fixed synthetic content/ under ``root`` and return its counts and source bytes."""
    if not isinstance(label, str) or not label.isalnum():
        raise ValueError("label must be a short alphanumeric word")
    content = Path(root) / "content"
    if content.exists():
        raise ValueError(f"{content} already exists; refusing to overwrite it")
    (content / "findings").mkdir(parents=True)
    (content / "meetings").mkdir()
    source_bytes = _source_bytes(label)
    digests = [hashlib.sha256(data).hexdigest() for data in source_bytes]
    documents = {
        "site.json": {
            "schema_version": 1,
            "tagline": f"Synthetic {label} campaign",
            "mission_md": "Synthetic mission text.",
            "about_md": "Synthetic about text. No real records are described.",
            "contact_email": "organizer@example.invalid",
        },
        "findings/synthetic-1.json": _finding(1, label, digests[0], []),
        "findings/synthetic-2.json": _finding(2, label, digests[1], [
            {"date": "2026-02-10", "note": "Synthetic correction to the summary."}]),
        "meetings/synthetic-meeting.json": {
            "id": "synthetic-meeting",
            "body": "Synthetic County Board",
            "starts_at": "2026-03-03T18:00:00-08:00",
            "agenda_item": "Synthetic agenda item",
            "agency_id": AGENCY_ID,
            "status": "scheduled",
        },
        "agencies.json": [{
            "agency_id": AGENCY_ID,
            "name": "Synthetic County Sheriff",
            "kind": "sheriff",
            "jurisdiction_name": "Synthetic County",
            "portal": {"vendor": "none"},
        }],
    }
    for name, document in documents.items():
        (content / name).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return {"findings": 2, "meetings": 1, "source_bytes": source_bytes}
