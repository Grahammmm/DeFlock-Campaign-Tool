"""Build a newsletter draft (subject, HTML, text) from a manifest.

The manifest shape matches ``newsletterManifest`` in the workspace Worker::

    {"schema_version": 1,
     "campaign": {"name": "...", "base_url": "https://..." | null, "county_name": "..."},
     "since": "<ISO>" | null,
     "findings": [{"title", "summary", "classification", "confidence", "path",
                   "published_at", "corrected": bool,
                   "corrections"?: [{"date": "YYYY-MM-DD", "note"}]}],
     "meetings": [{"body", "starts_at", "agenda_item", "agenda_url", "relevance",
                   "participation"?: {...} | null}],  # --from-content: content.load_participation's dict
     "decisions": [{"body", "on", "result", "vote", "source_url", "meeting_path"}]}  # optional, --from-content

``corrections`` is optional (``--from-content`` sets it; Worker manifests omit it).
``participation`` likewise: an object adds a "How to take part" line, null the
posted-agenda sentence, and a meeting without the key renders as before.

HTML is rendered from a Markdown subset through ``markdown_lite`` so raw HTML
never reaches the draft; the only additions are a wrapper and the provider's
unsubscribe placeholder. The text version is the same Markdown source.
"""
import contextlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path

from ..agency_cards import text as plain_text
from ..kit import _kit_folder
from ..legal_claims import legal_notes as legal_claim_notes
from ..markdown_lite import render

UNSUBSCRIBE_PLACEHOLDER = "{{ unsubscribe }}"
MAX_FINDINGS = 12
MAX_MEETINGS = 8
MAX_CORRECTIONS = 50
NOTE_LIMIT = 200
_PATH = re.compile(r"/[A-Za-z0-9._/-]{0,200}")
_HTTPS = re.compile(r"https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~/?#&=%-]*)?")
# Patterns that must never appear in a draft: emails and phone-like numbers
# would mean personal data leaked into the manifest.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")


class DraftError(ValueError):
    """The manifest is malformed or would leak something into the newsletter."""


def _clean(value, label, limit=500):
    if value is None:
        return ""
    if not isinstance(value, str) or not plain_text(value):
        raise DraftError(f"{label}: plain text without angle brackets required")
    value = " ".join(value.split())
    if len(value) > limit:
        raise DraftError(f"{label}: longer than {limit} characters")
    if _EMAIL.search(value):
        raise DraftError(f"{label}: contains an email address; newsletters carry no personal data")
    return value


def _date(value, label):
    if not isinstance(value, str):
        raise DraftError(f"{label}: ISO-8601 timestamp required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DraftError(f"{label}: invalid timestamp {value!r}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DraftError(f"{label}: timestamp needs a UTC offset")
    return parsed


def _entries(items, label):
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise DraftError(f"{label}[{index}]: must be an object")
    return items


def _day(value, label):
    if not isinstance(value, str):
        raise DraftError(f"{label}: YYYY-MM-DD day required")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != value:
        raise DraftError(f"{label}: invalid day {value!r}, YYYY-MM-DD required")
    return parsed


def _corrections(finding, label):
    """``[(day, index, note)]`` from a finding's optional ``corrections``; malformed ones are refused."""
    if "corrections" not in finding:
        return []
    items, label = finding["corrections"], label + ".corrections"
    if not isinstance(items, list) or len(items) > MAX_CORRECTIONS:
        raise DraftError(f"{label}: a list of at most {MAX_CORRECTIONS} corrections required")
    out = []
    for index, item in enumerate(_entries(items, label)):
        day = _day(item.get("date"), f"{label}[{index}].date")
        if not isinstance(item.get("note"), str):
            raise DraftError(f"{label}[{index}].note: string required")
        out.append((day, index, item["note"]))
    return out


def _note(value):
    value = " ".join(value.split())
    return value if len(value) <= NOTE_LIMIT else value[:NOTE_LIMIT - 1] + "…"


# Same wording as site.OUTCOME_LABELS (DF2237-11); kept local so the draft builder stays standalone.
DECISION_LABELS = {"approved": "Approved", "denied": "Denied", "continued": "Continued to a later meeting",
                   "amended": "Approved with amendments", "no_action": "No action taken",
                   "informational": "Informational item only"}


def _decisions(items):
    """Lines for ``decisions`` (DF2237-12): organizer-recorded outcomes only; nothing is inferred."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise DraftError("decisions must be a list")
    lines = []
    for index, item in enumerate(_entries(items, "decisions")):
        label = f"decisions[{index}]"
        result, on, url = item.get("result"), item.get("on"), item.get("source_url")
        if not isinstance(result, str) or result not in DECISION_LABELS:
            raise DraftError(f"{label}.result: must be one of {', '.join(DECISION_LABELS)}")
        if not isinstance(on, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", on):
            raise DraftError(f"{label}.on: YYYY-MM-DD date required")
        day = _date(on + "T00:00:00+00:00", label + ".on")
        if not isinstance(url, str) or not _HTTPS.fullmatch(url):
            raise DraftError(f"{label}.source_url: must be an https:// URL")
        body = _clean(item.get("body"), label + ".body", 200)
        if not body:
            raise DraftError(f"{label}.body: required")
        vote = _clean(item.get("vote"), label + ".vote", 40)
        entry = f"- **{body}**, {day.strftime('%b %d, %Y')}: {DECISION_LABELS[result]}"
        lines.append(entry + (f" (vote {vote})" if vote else "") + f" [Official record]({url})")
    return lines


def _how(value, label, excluded):
    """Organizer ``how`` text, or "see official details" when it holds an email or phone (DF2037-15)."""
    if isinstance(value, str) and (_EMAIL.search(value) or _PHONE.search(value)):
        excluded["contact_text_withheld"] = excluded.get("contact_text_withheld", 0) + 1
        return "see official details"
    return _clean(value, label, 200)


def _participation(facts, starts, now, label, excluded):
    """The indented "How to take part" line for one meeting's verified participation facts (DF2037-15)."""
    if facts is None:
        return "  Check the posted agenda for how to comment."
    if not isinstance(facts, dict):
        raise DraftError(f"{label}: must be an object or null")
    remote, written, minutes = facts.get("remote"), facts.get("written_comment"), facts.get("speaker_minutes")
    parts = [f"In person: {'yes' if facts['in_person'] else 'no'}"] if isinstance(facts.get("in_person"), bool) else []
    if isinstance(remote, dict) and remote.get("available") is False:
        parts.append("Remote: not offered")
    elif isinstance(remote, dict) and remote.get("available") is True:
        parts.append("Remote: " + (_how(remote.get("how"), label + ".remote.how", excluded) or "available"))
    if isinstance(written, dict):
        due = _date(written["deadline"], label + ".written_comment.deadline") if written.get("deadline") else None
        if due is not None and due <= now:
            excluded["comment_deadlines_passed"] = excluded.get("comment_deadlines_passed", 0) + 1
        else:
            how = _how(written.get("how"), label + ".written_comment.how", excluded) or "see official details"
            by = due.astimezone(starts.tzinfo).strftime(", by %a %b %d, %H:%M %Z").rstrip() if due else ""
            parts.append(f"Written comments: {how}{by}")
    if type(minutes) is int and 1 <= minutes <= 10:
        parts.append(f"Speaker limit: {minutes} minute{'s' if minutes != 1 else ''}")
    url = facts.get("source_url")
    if not isinstance(url, str) or not _HTTPS.fullmatch(url):
        raise DraftError(f"{label}.source_url: must be an https:// URL")
    checked = _day(facts.get("verified_at"), label + ".verified_at").isoformat()
    return (f"  How to take part: {'; '.join(parts) or 'see official details'}"
            f" (checked {checked}) [Official details]({url})")


def _link(base_url, path, label):
    if not isinstance(path, str) or not _PATH.fullmatch(path) or ".." in path.split("/"):
        raise DraftError(f"{label}: invalid public path")
    if base_url:
        return base_url.rstrip("/") + path
    return path


def _period_label(since, now):
    if since:
        return f"since {since.date().isoformat()}"
    return now.strftime("%B %Y")


# Appended when the campaign has not supplied its own reviewed consent footer. The send
# executor refuses a draft that still carries it, so the engine's wording is never sent as
# a campaign's consent statement by accident.
CONSENT_FOOTER_TEMPLATE_MARKER = "[TEMPLATE CONSENT FOOTER: replace with the campaign's reviewed wording in Settings before sending]"
DEFAULT_CONSENT_FOOTER = "You receive this because you signed up for {name} updates. Unsubscribe at any time."


def build_draft(manifest, now=None):
    """Return ``{"subject", "html", "text", "counts", "excluded", "consent_footer"}``.

    Only findings published at or after ``since`` and meetings starting at or
    after ``now`` (soonest first) are listed; ``excluded`` counts the rest.
    A finding published before ``since`` with a correction dated on or after
    the since day is listed under "Corrections" (latest such correction) and
    counted in ``corrections_listed``, present only when non-zero.

    ``campaign.consent_footer`` is the campaign's reviewed consent and sender
    wording (it must keep the sentence ``Unsubscribe at any time.`` for the
    provider placeholder). Without it the engine's template footer is used and
    marked, and the send executor refuses the draft.
    """
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise DraftError("manifest.schema_version must be 1")
    campaign = manifest.get("campaign") or {}
    name = _clean(campaign.get("name"), "campaign.name", 120) or "Campaign"
    base_url = campaign.get("base_url")
    if base_url is not None and (not isinstance(base_url, str) or not _HTTPS.fullmatch(base_url)):
        raise DraftError("campaign.base_url: must be an https:// URL")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    since = _date(manifest["since"], "since") if manifest.get("since") else None
    findings = manifest.get("findings") or []
    meetings = manifest.get("meetings") or []
    if not isinstance(findings, list) or not isinstance(meetings, list):
        raise DraftError("findings and meetings must be lists")
    decisions = _decisions(manifest.get("decisions"))
    # The engine applies the documented window itself: findings published since the
    # last send, meetings that have not started yet (soonest first). Labels keep the
    # manifest index so a bad entry is named where the organizer can find it.
    published = [(i, _date(f.get("published_at"), f"findings[{i}].published_at"))
                 for i, f in enumerate(_entries(findings, "findings"))]
    starts = [(_date(m.get("starts_at"), f"meetings[{i}].starts_at"), i)
              for i, m in enumerate(_entries(meetings, "meetings"))]
    kept_findings = [(i, findings[i]) for i, when in published if since is None or when >= since]
    kept_meetings = [(i, meetings[i], when) for when, i in sorted(s for s in starts if s[0] >= now)]
    # A finding left out by the window whose latest correction is on or after the
    # since day is listed once under "Corrections", never as new (DF1822-04).
    kept_ids = {i for i, _ in kept_findings}
    corrections = []
    for i, finding in enumerate(findings):
        recent = [c for c in _corrections(finding, f"findings[{i}]") if since and c[0] >= since.date()]
        if recent and i not in kept_ids:
            day, _, note = max(recent)
            label = f"findings[{i}]"
            corrections.append((day, _clean(finding.get("title") or finding.get("summary"), label + ".title", 200),
                                _note(note), _link(base_url, finding.get("path"), label + ".path")))
    corrections.sort(key=lambda c: (-c[0].toordinal(), c[1]))
    excluded = {"findings_before_since": len(findings) - len(kept_findings) - len(corrections),
                "meetings_past": len(meetings) - len(kept_meetings)}
    findings, meetings = kept_findings, kept_meetings

    lines = [f"## {name}: {_period_label(since, now)}"]
    lines.append("")
    if findings:
        lines.append(f"We published {len(findings)} reviewed finding{'s' if len(findings) != 1 else ''} "
                     "since our last update. Every one links to the record it came from.")
        lines.append("")
        lines.append("## New findings")
        for index, finding in findings[:MAX_FINDINGS]:
            label = f"findings[{index}]"
            title = _clean(finding.get("title") or finding.get("summary"), label + ".title", 200)
            summary = _clean(finding.get("summary"), label + ".summary", 600)
            link = _link(base_url, finding.get("path"), label + ".path")
            kind = _clean(finding.get("classification"), label + ".classification", 40).replace("_", " ")
            confidence = _clean(finding.get("confidence"), label + ".confidence", 40).replace("_", " ")
            tail = " (corrected)" if finding.get("corrected") is True else ""
            entry = f"- **{title}**{tail}: {summary} ({kind}, {confidence})"
            entry += f" [Read the finding]({link})" if link.startswith("https://") else f" {link}"
            lines.append(entry)
        if len(findings) > MAX_FINDINGS:
            lines.append(f"- and {len(findings) - MAX_FINDINGS} more on the site.")
    else:
        lines.append("No new findings were published in this period. Records requests are still open; "
                     "we publish only what independent reviewers have checked against the originals.")
    lines.append("")
    if corrections:
        lines.append("## Corrections")
        for day, title, note, link in corrections:
            entry = f"- **{title}**: corrected {day.isoformat()}" + (f" — {note}" if note else "")
            entry += f" ([Read the finding]({link}))" if link.startswith("https://") else f" ({link})"
            lines.append(entry)
        lines.append("")
    if decisions:
        lines.append("## Decisions")
        lines += decisions[:MAX_MEETINGS]
        if len(decisions) > MAX_MEETINGS:
            lines.append(f"- and {len(decisions) - MAX_MEETINGS} more on the site.")
        lines.append("")
    if meetings:
        lines.append("## Upcoming meetings")
        for index, meeting, starts in meetings[:MAX_MEETINGS]:
            label = f"meetings[{index}]"
            body = _clean(meeting.get("body"), label + ".body", 200)
            item = _clean(meeting.get("agenda_item"), label + ".agenda_item", 300)
            url = meeting.get("agenda_url")
            when = starts.strftime("%a %b %d, %Y %H:%M %Z").strip()
            entry = f"- **{body}**, {when}"
            if item:
                entry += f": {item}"
            if isinstance(url, str) and _HTTPS.fullmatch(url):
                entry += f" [Agenda]({url})"
            lines.append(entry)
            if "participation" in meeting:  # Worker manifests carry no such key and render as before
                lines.append(_participation(meeting["participation"], starts, now, label + ".participation", excluded))
        lines.append("")
    if base_url:
        lines.append(f"Read everything, with source hashes, at [{base_url}]({base_url}).")
    lines.append("Findings describe records and published rules as of the event date. This is not legal advice.")
    lines.append("")
    footer = campaign.get("consent_footer")
    if isinstance(footer, str) and footer.strip():
        footer = _clean(footer, "campaign.consent_footer", 600)
        if "Unsubscribe at any time." not in footer:
            raise DraftError("campaign.consent_footer must contain the sentence 'Unsubscribe at any time.'")
        lines.append(footer)
    else:
        lines.append(DEFAULT_CONSENT_FOOTER.format(name=name))
        lines.append("")
        lines.append(CONSENT_FOOTER_TEMPLATE_MARKER)
    markdown = "\n".join(lines) + "\n"

    subject = f"{name}: {len(findings)} new finding{'s' if len(findings) != 1 else ''}" if findings else f"{name}: {_period_label(since, now)} update"
    if meetings and len(subject) < 70:
        subject += f", {len(meetings)} meeting{'s' if len(meetings) != 1 else ''} coming up"
    html = _wrap(name, render(markdown))
    # markdown_lite only links absolute https targets, so the provider placeholder is
    # inserted here, after rendering, exactly once and outside any user-supplied text.
    html = html.replace("Unsubscribe at any time.", f'<a href="{UNSUBSCRIBE_PLACEHOLDER}">Unsubscribe</a> at any time.', 1)
    text = markdown.replace("Unsubscribe at any time.", f"Unsubscribe at any time: {UNSUBSCRIBE_PLACEHOLDER}", 1)
    for label, blob in (("html", html), ("text", text)):
        if "<script" in blob.lower():
            raise DraftError(f"{label}: script content refused")
        if _EMAIL.search(blob) or _PHONE.search(blob):
            raise DraftError(f"{label}: contact details found; newsletters carry no personal data")
        if UNSUBSCRIBE_PLACEHOLDER not in blob:
            raise DraftError(f"{label}: unsubscribe placeholder missing")
    # DF-4 alert-only: wording that asserts a violation never blocks the draft; the notes travel with it to the approval card.
    review_flags = legal_claim_notes([("subject", subject)] + [("text line %d" % n, line) for n, line in enumerate(text.splitlines(), 1)])
    result = {"subject": subject[:200], "html": html, "text": text,
              "counts": {"findings": len(findings), "meetings": len(meetings),
                         **({"decisions": len(decisions)} if decisions else {})}, "excluded": excluded,
              "consent_footer": "campaign" if isinstance(footer, str) and footer.strip() else "template"}
    if review_flags:  # absent otherwise, so unflagged drafts stay byte-identical (same convention as corrections_listed)
        result["review_flags"] = review_flags
    if corrections:  # absent otherwise, so drafts without listed corrections stay byte-identical
        result["corrections_listed"] = len(corrections)
    return result


def _wrap(name, body):
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{escape(name)}</title></head>"
            "<body style=\"font-family:Georgia,serif;max-width:40rem;margin:auto;padding:1rem;color:#111\">"
            f"{body}</body></html>\n")


def load_manifest(path):
    data = Path(path).read_bytes()
    if len(data) > 2 * 1024 * 1024:
        raise DraftError("manifest too large")
    return json.loads(data.decode("utf-8"))


def write_draft(root, draft):
    """Write kit/newsletter-draft.html, .txt and .json under ``root``; returns the paths."""
    kit = Path(root) / "kit"
    _kit_folder(kit, mode=0o700)
    files = (("newsletter-draft.html", draft["html"]), ("newsletter-draft.txt", draft["text"]),
             ("newsletter-draft.json", json.dumps(draft, indent=2, ensure_ascii=False) + "\n"))
    temps, replaced = [], []
    try:
        for name, content in files:
            fd, temp = tempfile.mkstemp(dir=kit, prefix="." + name + ".", suffix=".tmp")
            temps.append(temp)
            try:
                handle = os.fdopen(fd, "w", encoding="utf-8")
            except BaseException:
                os.close(fd)
                raise
            with handle:
                handle.write(content)
        for (name, _), temp in zip(files, temps):
            os.replace(temp, kit / name)
            replaced.append(name)
    except BaseException as exc:
        for temp in temps[len(replaced):]:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)
        if replaced and isinstance(exc, OSError):
            raise OSError("partial newsletter draft: " + ", ".join(replaced)) from exc
        raise
    return [kit / name for name, _ in files]
