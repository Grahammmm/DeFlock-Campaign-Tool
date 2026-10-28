"""Build a newsletter draft (subject, HTML, text) from a manifest.

The manifest shape matches ``newsletterManifest`` in the workspace Worker::

    {"schema_version": 1,
     "campaign": {"name": "...", "base_url": "https://..." | null, "county_name": "..."},
     "since": "<ISO>" | null,
     "findings": [{"title", "summary", "classification", "confidence", "path",
                   "published_at", "corrected": bool}],
     "meetings": [{"body", "starts_at", "agenda_item", "agenda_url", "relevance"}]}

HTML is rendered from a Markdown subset through ``markdown_lite`` so raw HTML
never reaches the draft; the only additions are a wrapper and the provider's
unsubscribe placeholder. The text version is the same Markdown source.
"""
import json
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from ..agency_cards import text as plain_text
from ..markdown_lite import render

UNSUBSCRIBE_PLACEHOLDER = "{{ unsubscribe }}"
MAX_FINDINGS = 12
MAX_MEETINGS = 8
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
    return parsed


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
    """Return ``{"subject", "html", "text", "counts"}`` for the manifest.

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
    since = _date(manifest["since"], "since") if manifest.get("since") else None
    findings = manifest.get("findings") or []
    meetings = manifest.get("meetings") or []
    if not isinstance(findings, list) or not isinstance(meetings, list):
        raise DraftError("findings and meetings must be lists")

    lines = [f"## {name}: {_period_label(since, now)}"]
    lines.append("")
    if findings:
        lines.append(f"We published {len(findings)} reviewed finding{'s' if len(findings) != 1 else ''} "
                     "since our last update. Every one links to the record it came from.")
        lines.append("")
        lines.append("## New findings")
        for index, finding in enumerate(findings[:MAX_FINDINGS]):
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
    if meetings:
        lines.append("## Upcoming meetings")
        for index, meeting in enumerate(meetings[:MAX_MEETINGS]):
            label = f"meetings[{index}]"
            body = _clean(meeting.get("body"), label + ".body", 200)
            starts = _date(meeting.get("starts_at"), label + ".starts_at")
            item = _clean(meeting.get("agenda_item"), label + ".agenda_item", 300)
            url = meeting.get("agenda_url")
            when = starts.strftime("%a %b %d, %Y %H:%M %Z").strip()
            entry = f"- **{body}**, {when}"
            if item:
                entry += f": {item}"
            if isinstance(url, str) and _HTTPS.fullmatch(url):
                entry += f" [Agenda]({url})"
            lines.append(entry)
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
    return {"subject": subject[:200], "html": html, "text": text,
            "counts": {"findings": len(findings), "meetings": len(meetings)},
            "consent_footer": "campaign" if isinstance(footer, str) and footer.strip() else "template"}


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
    kit.mkdir(mode=0o700, exist_ok=True)
    paths = []
    for name, content in (("newsletter-draft.html", draft["html"]), ("newsletter-draft.txt", draft["text"]),
                          ("newsletter-draft.json", json.dumps(draft, indent=2, ensure_ascii=False) + "\n")):
        target = kit / name
        target.write_text(content, encoding="utf-8")
        paths.append(target)
    return paths
