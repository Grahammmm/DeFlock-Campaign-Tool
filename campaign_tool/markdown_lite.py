"""Tiny, dependency-free renderer for a safe Markdown subset.

Supported: paragraphs, ``## `` headings (rendered as ``<h3>``), ``**bold**``,
``[text](https://...)`` links and unordered ``- `` lists. Everything else is
plain text. All input is HTML-escaped first, so raw HTML never reaches the
page; links whose target is not an absolute ``https://`` URL are rendered as
their text alone. This is a build-time formatter for reviewed campaign copy,
not a sanitizer for untrusted documents.
"""
import re
from html import escape, unescape

MAX_BYTES = 200 * 1024
_LINK = re.compile(r"\[([^\]\n]{1,300})\]\(([^)\s]{1,2000})\)")
_BOLD = re.compile(r"\*\*([^*\n]{1,500})\*\*")
_HTTPS = re.compile(r"https://[A-Za-z0-9][A-Za-z0-9.-]*(?::\d{1,5})?(?:/[A-Za-z0-9._~:/?#@!$&*+,;=%-]*)?")


def _link(match):
    text, target = match.group(1), match.group(2)
    # The target was HTML-escaped with the rest of the line; validate the
    # unescaped form against a strict https-only pattern (no quotes, brackets
    # or parentheses), then re-escape it for the attribute.
    raw = unescape(target)
    if not _HTTPS.fullmatch(raw):
        return text
    return '<a href="' + escape(raw, quote=True) + '" rel="noopener noreferrer">' + text + "</a>"


def inline(text):
    """Render inline markup for one already-plain-text line."""
    out = escape(text, quote=True)
    out = _BOLD.sub(lambda m: "<strong>" + m.group(1) + "</strong>", out)
    return _LINK.sub(_link, out)


def render(source):
    """Render a Markdown-subset string to an HTML fragment."""
    if not isinstance(source, str):
        raise ValueError("Markdown source must be a string")
    if len(source.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Markdown source too large")
    blocks = []
    paragraph, items = [], []

    def flush():
        if paragraph:
            blocks.append("<p>" + " ".join(inline(line) for line in paragraph) + "</p>")
            paragraph.clear()
        if items:
            blocks.append("<ul>" + "".join("<li>" + inline(item) + "</li>" for item in items) + "</ul>")
            items.clear()

    for raw in source.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("## "):
            flush()
            blocks.append("<h3>" + inline(line[3:].strip()) + "</h3>")
        elif line.startswith("- ") or line.startswith("* "):
            if paragraph:
                flush()
            items.append(line[2:].strip())
        else:
            if items:
                flush()
            paragraph.append(line)
    flush()
    return "\n".join(blocks)
