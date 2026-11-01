"""Build a newsletter manifest from a campaign's own reviewed ``content/``.

The manifest has the same shape as the Worker's ``newsletterManifest`` (see
``draft.py``), so offline campaigns get the same draft. Only published findings
are loaded by ``content.load_content``; cancelled meetings are left out here.
The since/upcoming window is applied by ``build_draft``, not duplicated.

Known limit: a finding corrected after ``since`` but published before it is
still excluded, because ``build_draft`` windows on ``published_at``.
"""
from ..content import load_content


def manifest_from_content(root, cfg, since=None):
    """Return a schema-1 manifest; ``since`` is an ISO timestamp with offset, or None."""
    model = load_content(root)
    if model is None:
        raise ValueError("--from-content needs a content/ directory")
    findings = [{"title": f["title"], "summary": f["summary"], "classification": f["classification"],
                 "confidence": f["confidence"], "path": f"/findings/{f['slug']}.html",
                 "published_at": f["published_at"], "corrected": bool(f["corrections"])}
                for f in model["findings"]]
    meetings = [{"body": m["body"], "starts_at": m["starts_at"], "agenda_item": m["agenda_item"],
                 "agenda_url": m["agenda_url"], "relevance": None}
                for m in model["meetings"] if m.get("status") != "cancelled"]
    return {"schema_version": 1,
            "campaign": {"name": cfg["name"], "base_url": model["site"]["base_url"], "county_name": cfg["county"]},
            "since": since, "findings": findings, "meetings": meetings}
