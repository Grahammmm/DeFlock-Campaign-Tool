"""Tiny, dependency-free renderer for a safe Markdown subset.

Supported: paragraphs, ``## `` headings (rendered as ``<h3>``), ``**bold**``,
``[text](https://...)`` links and unordered ``- `` lists. Everything else is
plain text. All input is HTML-escaped first, so raw HTML never reaches the
page; links whose target is not an absolute ``https://`` URL are rendered as
their text alone. This is a build-time formatter for reviewed campaign copy,
not a sanitizer for untrusted documents.

``lint(source)`` lists, read-only, every construct ``render`` would degrade
(dropped links, other heading levels, numbered lists, unclosed ``**``, raw
HTML, single ``*x*``/``_x_`` emphasis) as sorted ``(line_no, code, detail)``
tuples; it never echoes link text or targets and never changes ``render``.
``python3 -m campaign_tool.markdown_lite --lint FILE...`` prints
``FILE:LINE: CODE: detail`` lines and ``files=N findings=M`` (exit 0 clean,
1 findings, 2 usage error or an unreadable, non-UTF-8, oversize or missing file).
"""
import re
import sys
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


_HEADING = re.compile(r"(?:#|#{3,6}) ")
_NUMBERED = re.compile(r"\d{1,3}[.)] ")
_HTML = re.compile(r"<[A-Za-z/!]")
_EMPHASIS = re.compile(r"(?<![*\w])[*_][^*_\s][^*_\n]*[*_](?![*\w])")
_DETAILS = {
    "link-dropped": "link target is not absolute https",
    "heading-level": 'only "## " headings render',
    "numbered-list": "numbered lists render as plain text",
    "bold-unclosed": "unclosed **",
    "raw-html": "raw HTML is shown escaped",
    "emphasis": "only **bold** renders",
}


def _line_codes(line):
    codes = []
    if _HEADING.match(line):
        codes.append("heading-level")
    if _NUMBERED.match(line):
        codes.append("numbered-list")
    # Lint the same text render passes to inline(), through the same steps.
    body = line[3:].strip() if line.startswith("## ") else line
    if line.startswith("- ") or line.startswith("* "):
        body = line[2:].strip()
    marked = _BOLD.sub(lambda m: "<strong>" + m.group(1) + "</strong>", escape(body, quote=True))
    codes += ["link-dropped" for m in _LINK.finditer(marked) if not _HTTPS.fullmatch(unescape(m.group(2)))]
    unbolded = _BOLD.sub(lambda m: m.group(1), body)
    if "**" in unbolded:
        codes.append("bold-unclosed")
    if _HTML.search(body):
        codes.append("raw-html")
    plain = _LINK.sub(lambda m: m.group(1), unbolded)
    codes += ["emphasis" for _ in _EMPHASIS.finditer(plain)]
    return codes


def lint(source):
    """Return sorted (line_no, code, detail) for every construct render() degrades."""
    if not isinstance(source, str):
        raise ValueError("Markdown source must be a string")
    if len(source.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Markdown source too large")
    found = []
    for number, raw in enumerate(source.replace("\r\n", "\n").split("\n"), 1):
        line = raw.strip()
        if line:
            found += [(number, code, _DETAILS[code]) for code in _line_codes(line)]
    return sorted(found)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) < 2 or args[0] != "--lint" or any(arg.startswith("-") for arg in args[1:]):
        print("usage: python3 -m campaign_tool.markdown_lite --lint FILE [FILE...]", file=sys.stderr)
        return 2
    files = findings = 0
    failed = False
    for path in args[1:]:
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("over MAX_BYTES")
            results = lint(data.decode("utf-8"))
        except (OSError, ValueError) as error:
            reason = "not UTF-8" if isinstance(error, UnicodeDecodeError) else getattr(error, "strerror", None) or str(error)
            print(f"{path}: cannot lint ({reason})", file=sys.stderr)
            failed = True
            continue
        files += 1
        findings += len(results)
        for number, code, detail in results:
            print(f"{path}:{number}: {code}: {detail}")
    print(f"files={files} findings={findings}")
    return 2 if failed else (1 if findings else 0)


if __name__ == "__main__":
    sys.exit(main())
