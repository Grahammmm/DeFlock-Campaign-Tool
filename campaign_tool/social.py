"""Private social-post drafts, one per published finding.

``build_social(root)`` writes ``<root>/kit/social/<slug>.md``: a short post with the
classification label, title, public finding URL and finding id, within
``SOCIAL_MAX_CHARS``. Nothing is posted and nothing goes under ``public/``. A draft
the organizer edited is kept; the fresh draft goes to ``kit/social.suggested/``.
Drafts for slugs that are no longer published are listed as stale, never deleted.
"""
import hashlib
import json
import os

from .content import load_content
from .kit import _kit_folder, _write_json, _write_text
from .site import CLASSIFICATION_LABELS

SOCIAL_MAX_CHARS = 280


def post_text(finding, base_url):
    """Return the post for ``finding``; only the title is shortened to fit the cap."""
    url = f"{base_url.rstrip('/')}/findings/{finding['slug']}.html"
    label = CLASSIFICATION_LABELS[finding["classification"]]
    tail = f" {url} (finding {finding['id']})"
    if finding["corrections"]:
        tail += f" Corrected {max(c['date'] for c in finding['corrections'])}."
    head = f"{label}: "
    room = SOCIAL_MAX_CHARS - len(head) - len(tail)
    if room < 1:
        raise ValueError(f"finding {finding['id']}: post cannot fit {SOCIAL_MAX_CHARS} characters")
    title = finding["title"]
    if len(title) > room:
        title = title[:room - 1].rstrip() + "\u2026"
    return head + title + tail


def _draft(finding, base_url):
    return f"<!-- DRAFT: not posted. Review before any use. finding {finding['id']} -->\n\n{post_text(finding, base_url)}\n"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _load_record(path):
    try:
        record = json.loads(path.read_text(encoding="utf-8")) if path.is_file() and not path.is_symlink() else {}
    except (OSError, ValueError):
        record = None
    if isinstance(record, dict) and all(isinstance(v, str) for v in record.values()):
        return record
    return {}


def build_social(root):
    """Write the drafts and return ``{written, kept_edited, suggested, stale, posted}``."""
    model = load_content(root)
    if model is None:
        raise ValueError("social drafts need a content/ directory")
    base_url = model["site"].get("base_url")
    if not base_url:
        raise ValueError("social drafts need content/site.json base_url")
    drafts = {f["slug"]: _draft(f, base_url) for f in model["findings"]}
    kit_dir = root / "kit"
    social_dir, suggested_dir = kit_dir / "social", kit_dir / "social.suggested"
    for folder in (kit_dir, social_dir):
        _kit_folder(folder)
    record_path = social_dir / ".written.json"
    record = _load_record(record_path)
    written, kept, new_record = [], [], {}
    for slug, text in sorted(drafts.items()):
        path, data = social_dir / (slug + ".md"), text.encode("utf-8")
        if os.path.lexists(path):
            current = b"" if path.is_symlink() or not path.is_file() else path.read_bytes()
            if path.is_symlink() or (current != data and _sha(current) != record.get(slug)):
                _kit_folder(suggested_dir)
                _write_text(suggested_dir / path.name, text)
                kept.append(slug)
                if slug in record:
                    new_record[slug] = record[slug]
                continue
        _write_text(path, text)
        written.append(slug)
        new_record[slug] = _sha(data)
    stale = sorted(p.stem for p in social_dir.glob("*.md") if p.stem not in drafts)
    new_record.update({slug: record[slug] for slug in stale if slug in record})
    _write_json(record_path, dict(sorted(new_record.items())))
    return {"written": written, "kept_edited": kept, "suggested": len(kept), "stale": stale, "posted": False}
