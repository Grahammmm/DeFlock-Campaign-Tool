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
``build_meeting_social(root)`` drafts one post per scheduled, upcoming, organizer-verified staged meeting into
``kit/social/meetings/`` (same rules, never posted); finding drafts and ``check_social`` do not change.
``build_outcome_social(root)`` drafts one post per meeting with a recorded outcome dated in the last N days into
``kit/social/outcomes/`` (result and vote quoted as recorded; never posted).
"""
import hashlib
import json
import os
import re
import stat
from datetime import date, datetime, timedelta, timezone
from itertools import chain

from .content import ContentError, _read_json, _slug, load_content, load_meeting, load_site
from .kit import _kit_folder, _write_json, _write_text
from .legal_claims import legal_notes
from .privacy_patterns import refusal
from .site import CLASSIFICATION_LABELS, MONTHS, WEEKDAYS

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
    for slug, text in sorted(drafts.items()):  # the shared privacy lint, before any file or folder is touched
        if note := refusal(f"social draft {slug}", text):
            raise ValueError(note)
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


# --- meeting drafts (DF1207-11) ---------------------------------------------

SKIP_KEYS = ("cancelled", "past", "outside_window", "unverified", "unreadable")


def _when(stamp):
    """``Wed Oct 14, 6:00 pm UTC-07:00``: the timestamp's own date, time and offset, as the meeting page prints them."""
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    offset = int(moment.utcoffset().total_seconds()) // 60
    return (f"{WEEKDAYS[moment.weekday()][:3]} {MONTHS[moment.month - 1][:3]} {moment.day}, "
            f"{(moment.hour - 1) % 12 + 1}:{moment.minute:02d} {'am' if moment.hour < 12 else 'pm'} "
            f"UTC{'-' if offset < 0 else '+'}{abs(offset) // 60:02d}:{abs(offset) % 60:02d}")


def meeting_post_text(meeting, base_url, platform=None):
    """Return the post for a verified ``meeting``; only ``agenda_item`` is shortened (``platform=None``: legacy ``len()`` cap)."""
    facts = meeting["participation"]
    tail, written = "", facts["written_comment"]
    if written and written["deadline"]:
        tail += f" Comment in writing by {_when(written['deadline'])}: {written['how']}."
    remote = facts["remote"] if facts["remote"] and facts["remote"]["available"] else None
    if facts["in_person"] and remote:
        tail += f" Attend: in person or remotely: {remote['how']}."
    elif facts["in_person"]:
        tail += " Attend: in person."
    elif remote:
        tail += f" Attend: remotely: {remote['how']}."
    if base_url:
        tail += f" {base_url.rstrip('/')}/meetings/{meeting['id']}.html"
    head = f"{meeting['body']} meets {_when(meeting['starts_at'])}"
    item = meeting["agenda_item"]
    limit = PLATFORMS[platform][0] if platform is not None else SOCIAL_MAX_CHARS
    for cut in range(len(item), -1, -1) if item else (0,):
        shown = "" if not item else ": " + (item if cut == len(item) else item[:cut].rstrip() + "\u2026")
        text = f"{head}{shown}.{tail}"
        if (post_length(text, platform) if platform is not None else len(text)) <= limit:
            return text
    raise ValueError(f"meeting {meeting['id']}: post cannot fit {limit} characters" + (f" on {platform}" if platform else ""))


def _meeting_draft(meeting, base_url, platform=None):
    return f"<!-- DRAFT: not posted. Review before any use. meeting {meeting['id']} -->\n\n{meeting_post_text(meeting, base_url, platform)}\n"


def _check_window(within_days):
    if type(within_days) is not int or not 1 <= within_days <= 60:
        raise ValueError("--within-days must be a whole number from 1 to 60")


def _staged_meetings(root, now):
    """Return ``(meetings, unreadable_files, base_url, now)``; an unreadable meeting file is named, never fatal."""
    now = now or datetime.now(timezone.utc)
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    content_dir = root / "content"
    if not content_dir.is_dir():
        raise ValueError("social drafts need a content/ directory")
    try:
        site_path = content_dir / "site.json"
        base_url = load_site(_read_json(site_path), content_dir / "public-assets").get("base_url") if site_path.is_file() else None
    except ContentError as exc:
        raise ValueError(str(exc)) from None
    unreadable, loaded, seen = [], [], set()
    folder = content_dir / "meetings"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            meeting = load_meeting(_read_json(path), "meetings/" + path.name, today=now.date())
            meeting["id"] = meeting["id"] or _slug(path.stem, "meetings/" + path.name + " filename")
            if meeting["id"] in seen:
                raise ContentError(f"duplicate meeting id {meeting['id']!r}")
        except (ContentError, OSError, ValueError):
            unreadable.append(path.name)
            continue
        seen.add(meeting["id"])
        loaded.append(meeting)
    return loaded, unreadable, base_url, now


def _upcoming_verified(root, within_days, now):
    """Return ``(meetings, skipped, unreadable_files, base_url)``; an unreadable meeting file is counted and named, never fatal."""
    _check_window(within_days)
    loaded, unreadable, base_url, now = _staged_meetings(root, now)
    skipped, kept = dict.fromkeys(SKIP_KEYS, 0), []
    skipped["unreadable"] = len(unreadable)
    for meeting in loaded:
        starts = datetime.fromisoformat(meeting["starts_at"].replace("Z", "+00:00"))
        if meeting["status"] != "scheduled":
            skipped["cancelled"] += 1
        elif starts < now:
            skipped["past"] += 1
        elif starts > now + timedelta(days=within_days):
            skipped["outside_window"] += 1
        elif meeting["participation"] is None:
            skipped["unverified"] += 1
        else:
            kept.append(meeting)
    return kept, skipped, unreadable, base_url


def meeting_review_notes(root, within_days=14, now=None):
    """DF-4 alert-only: ``review-required`` notes for meeting posts whose wording asserts a violation. Never raises."""
    try:
        meetings, _, _, base_url = _upcoming_verified(root, within_days, now)
        return legal_notes((f"social/meetings/{m['id']}", meeting_post_text(m, base_url)) for m in meetings)
    except Exception:  # noqa: BLE001 - alert-only; build_meeting_social reports its own refusals
        return ["review-required: social: legal-claim wording (meeting posts could not be scanned; read them before use)"]


def build_meeting_social(root, platforms=None, within_days=14, now=None):
    """Write meeting drafts into ``kit/social/meetings/`` (``<platform>/`` each with ``platforms``) and return
    ``{meetings, skipped, unreadable_files, posted}``; ``meetings`` is ``build_social``'s dict (per platform with ``platforms``)."""
    meetings, skipped, unreadable, base_url = _upcoming_verified(root, within_days, now)
    kit_dir = root / "kit"
    social_dir = kit_dir / "social"
    meeting_dir = social_dir / "meetings"
    if platforms is None:
        drafts = {m["id"]: _meeting_draft(m, base_url) for m in meetings}
        for folder in (kit_dir, social_dir, meeting_dir, social_dir / "meetings.suggested"):
            if folder in (kit_dir, social_dir) or os.path.lexists(folder):
                _kit_folder(folder)
        result = _write_drafts(meeting_dir, social_dir / "meetings.suggested", drafts)
    else:
        platforms = list(dict.fromkeys(platforms))
        unknown = [p for p in platforms if p not in PLATFORMS]
        if unknown or not platforms:
            raise ValueError(f"unknown platform: {', '.join(unknown) or 'none given'} (choose from {', '.join(PLATFORMS)})")
        plan = {p: {m["id"]: _meeting_draft(m, base_url, p) for m in meetings} for p in platforms}
        folders = [meeting_dir / name for p in platforms for name in (p, p + ".suggested")]
        for folder in [kit_dir, social_dir, meeting_dir] + folders:
            if os.path.lexists(folder):
                _kit_folder(folder)
        for folder in (kit_dir, social_dir, meeting_dir):
            _kit_folder(folder)
        result = {"platforms": {p: _write_drafts(meeting_dir / p, meeting_dir / (p + ".suggested"), plan[p]) for p in platforms}}
    return {"meetings": result, "skipped": skipped, "unreadable_files": unreadable, "posted": False}


# --- recorded-outcome drafts (DF1952-14) -------------------------------------

OUTCOME_SKIP_KEYS = ("cancelled", "no_outcome", "outside_window", "unreadable")


def outcome_post_text(meeting, base_url, platform=None):
    """Return the post for a meeting with a recorded outcome; only ``agenda_item`` is shortened.

    Result and vote are quoted exactly as recorded (``platform=None``: legacy ``len()`` cap)."""
    outcome = meeting["outcome"]
    tail = f" Result: {outcome['result']}."
    if "vote" in outcome:
        tail += f" Vote: {outcome['vote']}."
    tail += f" Official record: {outcome['source_url']}"
    if base_url:
        tail += f" {base_url.rstrip('/')}/meetings/{meeting['id']}.html"
    head = f"{meeting['body']} \u2014 {outcome['on']}"
    item = meeting["agenda_item"]
    limit = PLATFORMS[platform][0] if platform is not None else SOCIAL_MAX_CHARS
    shown_options = (": " + (item if cut == len(item) else item[:cut].rstrip() + "\u2026")
                     for cut in range(len(item), -1, -1)) if item else ()
    for shown in chain(shown_options, ("",)):  # last resort: no agenda text at all
        text = f"{head}{shown}.{tail}"
        if (post_length(text, platform) if platform is not None else len(text)) <= limit:
            return text
    raise ValueError(f"meeting {meeting['id']}: outcome post cannot fit {limit} characters"
                     + (f" on {platform}" if platform else ""))


def _outcome_draft(meeting, base_url, platform=None):
    return (f"<!-- DRAFT: not posted. Review before any use. outcome {meeting['id']} -->\n\n"
            f"{outcome_post_text(meeting, base_url, platform)}\n")


def _recorded_outcomes(root, within_days, now):
    """Return ``(meetings, skipped, unreadable_files, base_url)`` for outcomes dated in the last ``within_days`` calendar days.

    The window is on the recorded ``outcome.on`` date, inclusive:
    ``[today - (within_days - 1), today]`` (``today`` = ``now``'s UTC date)."""
    _check_window(within_days)
    loaded, unreadable, base_url, now = _staged_meetings(root, now)
    skipped, kept = dict.fromkeys(OUTCOME_SKIP_KEYS, 0), []
    skipped["unreadable"] = len(unreadable)
    first = now.date() - timedelta(days=within_days - 1)
    for meeting in loaded:
        if meeting["status"] != "scheduled":
            skipped["cancelled"] += 1
        elif meeting["outcome"] is None:
            skipped["no_outcome"] += 1
        elif not first <= date.fromisoformat(meeting["outcome"]["on"]) <= now.date():
            skipped["outside_window"] += 1
        else:
            kept.append(meeting)
    return kept, skipped, unreadable, base_url


def outcome_review_notes(root, within_days=14, now=None):
    """DF-4 alert-only: ``review-required`` notes for outcome posts whose wording asserts a violation. Never raises."""
    try:
        meetings, _, _, base_url = _recorded_outcomes(root, within_days, now)
        return legal_notes((f"social/outcomes/{m['id']}", outcome_post_text(m, base_url)) for m in meetings)
    except Exception:  # noqa: BLE001 - alert-only; build_outcome_social reports its own refusals
        return ["review-required: social: legal-claim wording (outcome posts could not be scanned; read them before use)"]


def build_outcome_social(root, platforms=None, within_days=14, now=None):
    """Write outcome drafts into ``kit/social/outcomes/`` (``<platform>/`` each with ``platforms``) and return
    ``{outcomes, skipped, unreadable_files, posted}``; ``outcomes`` is ``build_social``'s dict (per platform with ``platforms``)."""
    meetings, skipped, unreadable, base_url = _recorded_outcomes(root, within_days, now)
    kit_dir = root / "kit"
    social_dir = kit_dir / "social"
    outcome_dir = social_dir / "outcomes"
    if platforms is None:
        drafts = {m["id"]: _outcome_draft(m, base_url) for m in meetings}
        for folder in (kit_dir, social_dir, outcome_dir, social_dir / "outcomes.suggested"):
            if folder in (kit_dir, social_dir) or os.path.lexists(folder):
                _kit_folder(folder)
        result = _write_drafts(outcome_dir, social_dir / "outcomes.suggested", drafts)
    else:
        platforms = list(dict.fromkeys(platforms))
        unknown = [p for p in platforms if p not in PLATFORMS]
        if unknown or not platforms:
            raise ValueError(f"unknown platform: {', '.join(unknown) or 'none given'} (choose from {', '.join(PLATFORMS)})")
        plan = {p: {m["id"]: _outcome_draft(m, base_url, p) for m in meetings} for p in platforms}
        folders = [outcome_dir / name for p in platforms for name in (p, p + ".suggested")]
        for folder in [kit_dir, social_dir, outcome_dir] + folders:
            if os.path.lexists(folder):
                _kit_folder(folder)
        for folder in (kit_dir, social_dir, outcome_dir):
            _kit_folder(folder)
        result = {"platforms": {p: _write_drafts(outcome_dir / p, outcome_dir / (p + ".suggested"), plan[p]) for p in platforms}}
    return {"outcomes": result, "skipped": skipped, "unreadable_files": unreadable, "posted": False}
