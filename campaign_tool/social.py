"""Private social-post drafts, one per published finding.

``build_social(root)`` writes ``<root>/kit/social/<slug>.md``: a short post with the
classification label, title, public finding URL and finding id, within
``SOCIAL_MAX_CHARS``. Nothing is posted and nothing goes under ``public/``. A draft
the organizer edited is kept; the fresh draft goes to ``kit/social.suggested/``.
Drafts for slugs that are no longer published are listed as stale, never deleted.
``check_social(root)`` writes nothing: it names each published slug's draft state
(ok, missing, outdated, edited, not_regular) with problem codes and the stale drafts.
``build_social(root, platforms=[...])`` sizes drafts by each platform's own count
(``post_length``) into ``kit/social/<platform>/``; ``check_social`` reads ``kit/social/`` only.
"""
import hashlib
import json
import os
import re
import stat

from .content import load_content
from .kit import _kit_folder, _write_json, _write_text
from .legal_claims import legal_notes
from .site import CLASSIFICATION_LABELS

SOCIAL_MAX_CHARS = 280
# platform: (limit, character rule, link length or None = the link's own length)
PLATFORMS = {"x": (280, "weighted", 23), "bluesky": (300, "codepoints", None), "mastodon": (500, "codepoints", 23)}
_LINK = re.compile(r"https?://\S+")
_LIGHT = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))  # X weighs these code points 1, all others 2


def post_length(text, platform):
    """Length of ``text`` by ``platform``'s own count (links fixed or full; X weighted)."""
    _, rule, link_length = PLATFORMS[platform]
    links = _LINK.findall(text)
    rest = "".join(_LINK.split(text))
    weight = len(rest) if rule == "codepoints" else sum(
        1 if any(low <= ord(c) <= high for low, high in _LIGHT) else 2 for c in rest)
    return weight + sum(link_length or len(link) for link in links)


def post_text(finding, base_url, platform=None):
    """Return the post for ``finding``; only the title is shortened (``platform=None``: legacy ``len()`` cap)."""
    url = f"{base_url.rstrip('/')}/findings/{finding['slug']}.html"
    label = CLASSIFICATION_LABELS[finding["classification"]]
    tail = f" {url} (finding {finding['id']})"
    if finding["corrections"]:
        tail += f" Corrected {max(c['date'] for c in finding['corrections'])}."
    head = f"{label}: "
    if platform is not None:
        limit, title = PLATFORMS[platform][0], finding["title"]
        for cut in range(len(title), -1, -1):
            text = head + (title if cut == len(title) else title[:cut].rstrip() + "\u2026") + tail
            if post_length(text, platform) <= limit:
                return text
        raise ValueError(f"finding {finding['id']}: post cannot fit {limit} characters on {platform}")
    room = SOCIAL_MAX_CHARS - len(head) - len(tail)
    if room < 1:
        raise ValueError(f"finding {finding['id']}: post cannot fit {SOCIAL_MAX_CHARS} characters")
    title = finding["title"]
    if len(title) > room:
        title = title[:room - 1].rstrip() + "\u2026"
    return head + title + tail


def _draft(finding, base_url, platform=None):
    return f"<!-- DRAFT: not posted. Review before any use. finding {finding['id']} -->\n\n{post_text(finding, base_url, platform)}\n"


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


def review_notes(root):
    """DF-4 alert-only: ``review-required`` notes for generated posts whose wording asserts a violation. Never raises, never blocks."""
    try:
        model = load_content(root)
        base_url = (model or {}).get("site", {}).get("base_url")
        if not (model and base_url):
            return []
        return legal_notes((f"social/{f['slug']}", post_text(f, base_url)) for f in model["findings"])
    except Exception:  # noqa: BLE001 - alert-only; build_social reports its own refusals
        return ["review-required: social: legal-claim wording (posts could not be scanned; read them before use)"]


def _published(root):
    model = load_content(root)
    if model is None:
        raise ValueError("social drafts need a content/ directory")
    base_url = model["site"].get("base_url")
    if not base_url:
        raise ValueError("social drafts need content/site.json base_url")
    return model["findings"], base_url


def _read_regular(path):
    """Return the bytes of a regular file without following a symlink, or ``None``."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        return handle.read() if stat.S_ISREG(os.fstat(handle.fileno()).st_mode) else None


def _edit_problems(finding, data, base_url):
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines[0].lstrip().startswith("<!--") and lines[0].rstrip().endswith("-->"):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    latest = max((c["date"] for c in finding["corrections"]), default=None)
    checks = {"over_cap": len(body) > SOCIAL_MAX_CHARS,
              "no_link": f"{base_url.rstrip('/')}/findings/{finding['slug']}.html" not in body,
              "no_id": f"finding {finding['id']}" not in body,
              "missing_correction": latest is not None and f"Corrected {latest}" not in body}
    return [code for code, failed in checks.items() if failed]


def check_social(root):
    """Compare ``kit/social/`` with the published findings; read-only, returns the report."""
    findings, base_url = _published(root)
    social_dir = root / "kit" / "social"
    for folder in (root / "kit", social_dir):
        if os.path.islink(folder):
            raise ValueError(f"{folder.name}/ must not be a symlink")
        if folder.exists() and not folder.is_dir():
            raise ValueError(f"{folder.name}/ is not a folder")
    record = _load_record(social_dir / ".written.json")
    entries = []
    for finding in sorted(findings, key=lambda f: f["slug"]):
        slug, path = finding["slug"], social_dir / (finding["slug"] + ".md")
        fresh = _draft(finding, base_url).encode("utf-8")
        if not os.path.lexists(path):
            state, problems = "missing", ["missing"]
        elif (data := _read_regular(path)) is None:
            state, problems = "not_regular", ["not_regular"]
        elif data == fresh:
            state, problems = "ok", []
        elif _sha(data) == record.get(slug):
            state, problems = "outdated", ["outdated"]
        else:
            state, problems = "edited", _edit_problems(finding, data, base_url)
        entries.append({"slug": slug, "state": state, "problems": problems})
    slugs = {f["slug"] for f in findings}
    stale = sorted(p.stem for p in social_dir.glob("*.md") if p.stem not in slugs
                   and (p.is_symlink() or stat.S_ISREG(p.lstat().st_mode)))
    count = sum(len(e["problems"]) for e in entries) + len(stale)
    return {"findings": len(findings), "drafts": entries, "stale": stale, "problems": count, "posted": False}


def build_social(root, platforms=None):
    """Write the drafts and return ``{written, kept_edited, suggested, stale, posted}``, or with
    ``platforms`` (all drafts made before any write) ``{"platforms": {p: <that dict>}}`` from ``kit/social/<p>/``."""
    findings, base_url = _published(root)
    kit_dir = root / "kit"
    social_dir = kit_dir / "social"
    if platforms is None:
        drafts = {f["slug"]: _draft(f, base_url) for f in findings}
        for folder in (kit_dir, social_dir):
            _kit_folder(folder)
        return _write_drafts(social_dir, kit_dir / "social.suggested", drafts)
    platforms = list(dict.fromkeys(platforms))
    unknown = [p for p in platforms if p not in PLATFORMS]
    if unknown or not platforms:
        raise ValueError(f"unknown platform: {', '.join(unknown) or 'none given'} (choose from {', '.join(PLATFORMS)})")
    plan = {p: {f["slug"]: _draft(f, base_url, p) for f in findings} for p in platforms}
    folders = [social_dir / name for p in platforms for name in (p, p + ".suggested")]
    for folder in [kit_dir, social_dir] + folders:
        if os.path.lexists(folder):
            _kit_folder(folder)
    for folder in (kit_dir, social_dir):
        _kit_folder(folder)
    return {"platforms": {p: _write_drafts(social_dir / p, social_dir / (p + ".suggested"), plan[p])
                          for p in platforms}}


def _write_drafts(social_dir, suggested_dir, drafts):
    _kit_folder(social_dir)
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
