"""Tiny, dependency-free renderer for a safe Markdown subset.

Supported: paragraphs, ``## `` headings (rendered as ``<h3>``), ``**bold**``,
``[text](https://...)`` links, unordered ``- `` lists, ordered ``1. `` lists
(``<ol start="N">`` when the first number is not 1) and simple pipe tables (a
``|...|`` header, a ``|---|:---:|`` separator with the same 1-12 cells, then
same-width rows; ``\\|`` is a literal pipe; colons add an ``align-*`` class).
Everything else is plain text. A line that is exactly ``![alt](/path)`` (alt
1-200 chars, ``/path`` a key of the ``images`` map the site passes, giving the
file's width and height) renders as ``<p><img ... loading="lazy"></p>``; with
``images=None`` (newsletter, CLI) nothing does. All input is HTML-escaped first, so raw HTML never reaches the
page; links whose target is not an absolute ``https://`` URL, or whose host is
not a public DNS name (2-10 labels of 1-63 letters/digits/inner hyphens, an
alphabetic last label, at most 253 chars, no IP literal or single label, port
1-65535 without a leading zero), are rendered as their text alone. This is a
build-time formatter for reviewed campaign copy, not a sanitizer for
untrusted documents.

``lint(source)`` lists, read-only, every construct ``render`` would degrade
(``link-dropped`` non-https links, ``link-host`` https links with a bad host,
other heading levels, ``1) `` numbered lists, malformed ``|`` tables, unclosed ``**``, raw HTML, single
``*x*``/``_x_`` emphasis, ``image`` for any ``![`` line render would not show as an image; with
``images=None`` only the image syntax is checked) as sorted ``(line_no, code, detail)`` tuples; it
never echoes link text or targets and never changes ``render``.
``python3 -m campaign_tool.markdown_lite --lint FILE...`` prints
``FILE:LINE: CODE: detail`` lines and ``files=N findings=M`` (exit 0 clean,
1 findings, 2 usage error or an unreadable, non-UTF-8, oversize or missing file).
"""
import re
import sys
from html import escape, unescape

MAX_BYTES = 200 * 1024
_LINK = re.compile(r"\[([^\]\n]{1,300})\]\(([^)\s]{1,2000})\)")
_IMAGE = re.compile(r"!\[([^\]\n]{1,200})\]\((/[A-Za-z0-9._/-]{1,240})\)")
_BOLD = re.compile(r"\*\*([^*\n]{1,500})\*\*")
_HTTPS = re.compile(r"https://[A-Za-z0-9][A-Za-z0-9.-]*(?::\d{1,5})?(?:/[A-Za-z0-9._~:/?#@!$&*+,;=%-]*)?")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_TLD = re.compile(r"[A-Za-z]{2,63}")
_PORT = re.compile(r"[1-9][0-9]{0,4}")
_ORDERED = re.compile(r"(\d{1,3})\. (.*)")
_SEPARATOR = re.compile(r"\|(?:\s*:?-{3,}:?\s*\|)+")
_PIPE = re.compile(r"(?<!\\)\|")
_ALIGN = {(False, False): "", (True, False): ' class="align-left"', (True, True): ' class="align-center"',
          (False, True): ' class="align-right"'}
MAX_COLUMNS = 12


def _host_ok(netloc):
    """True only for a public DNS host name with an optional port 1-65535."""
    host, colon, port = netloc.partition(":")
    if colon and not (_PORT.fullmatch(port) and int(port) <= 65535):
        return False
    labels = host.split(".")
    return (len(host) <= 253 and 2 <= len(labels) <= 10 and bool(_TLD.fullmatch(labels[-1]))
            and all(_LABEL.fullmatch(label) for label in labels))


def _target_code(raw):
    """None if render links the unescaped target, else the lint code naming why not."""
    if raw.startswith("https://") and not _host_ok(re.split(r"[/?#]", raw[len("https://"):], maxsplit=1)[0]):
        return "link-host"
    return None if _HTTPS.fullmatch(raw) else "link-dropped"


def _link(match):
    text, target = match.group(1), match.group(2)
    # The target was HTML-escaped with the rest of the line; validate the
    # unescaped form against a strict https-only pattern (no quotes, brackets
    # or parentheses) and a public DNS host, then re-escape it for the attribute.
    raw = unescape(target)
    if _target_code(raw):
        return text
    return '<a href="' + escape(raw, quote=True) + '" rel="noopener noreferrer">' + text + "</a>"


def inline(text):
    """Render inline markup for one already-plain-text line."""
    out = escape(text, quote=True)
    out = _BOLD.sub(lambda m: "<strong>" + m.group(1) + "</strong>", out)
    return _LINK.sub(_link, out)


def _image_html(line, images):
    """The ``<p><img ...></p>`` block for a line that is exactly ``![ALT](PATH)`` with PATH in ``images``, else None."""
    match = _IMAGE.fullmatch(line)
    if not images or not match or not match.group(1).strip() or match.group(2) not in images:
        return None
    width, height = images[match.group(2)]
    return (f'<p><img src="{escape(match.group(2), quote=True)}" alt="{escape(match.group(1).strip(), quote=True)}" '
            f'width="{int(width)}" height="{int(height)}" loading="lazy"></p>')


def render(source, images=None):
    """Render a Markdown-subset string to an HTML fragment; ``images`` maps ``/path`` -> (w, h) for image lines."""
    if not isinstance(source, str):
        raise ValueError("Markdown source must be a string")
    if len(source.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Markdown source too large")
    lines = [raw.strip() for raw in source.replace("\r\n", "\n").split("\n")]
    tables = _tables(lines)[0]
    blocks = []
    paragraph, items, numbered = [], [], []

    def flush():
        if paragraph:
            blocks.append("<p>" + " ".join(inline(line) for line in paragraph) + "</p>")
            paragraph.clear()
        if items:
            blocks.append("<ul>" + "".join("<li>" + inline(item) + "</li>" for item in items) + "</ul>")
            items.clear()
        if numbered:
            start = "" if numbered[0][0] == 1 else f' start="{numbered[0][0]}"'
            blocks.append(f"<ol{start}>" + "".join("<li>" + inline(item) + "</li>" for _, item in numbered) + "</ol>")
            numbered.clear()

    end = 0
    for index, line in enumerate(lines):
        ordered = _ORDERED.fullmatch(line)
        if index < end:
            continue
        if index in tables:
            flush()
            end = tables[index][0]
            blocks.append(_table_html(*tables[index][1:]))
        elif not line:
            flush()
        elif image := _image_html(line, images):
            flush()
            blocks.append(image)
        elif line.startswith("## "):
            flush()
            blocks.append("<h3>" + inline(line[3:].strip()) + "</h3>")
        elif line.startswith("- ") or line.startswith("* "):
            if paragraph or numbered:
                flush()
            items.append(line[2:].strip())
        elif ordered:
            if paragraph or items:
                flush()
            numbered.append((int(ordered.group(1)), ordered.group(2).strip()))
        else:
            if items or numbered:
                flush()
            paragraph.append(line)
    flush()
    return "\n".join(blocks)


def _cells(line):
    """Cells of a ``|...|`` row (unescaped ``|`` splits, ``\\|`` is a literal), else None."""
    parts = _PIPE.split(line)
    if len(parts) < 3 or parts[0] or parts[-1]:
        return None
    return [part.strip().replace("\\|", "|") for part in parts[1:-1]]


def _tables(lines):
    """({start: (end, header, aligns, rows)}, {index: table-malformed detail}) for stripped lines."""
    tables, bad, end = {}, {}, 0
    for index, line in enumerate(lines):
        head = _cells(line) if index >= end else None
        if head is None:
            continue
        after = lines[index + 1] if index + 1 < len(lines) else ""
        width = after.count("|") - 1 if _SEPARATOR.fullmatch(after) else 0
        if len(head) != width or width > MAX_COLUMNS:
            too_wide = max(len(head), width) > MAX_COLUMNS
            bad.setdefault(index, "at most 12 columns" if too_wide else "table needs a header and a --- separator row")
            continue
        end = index + 2
        while end < len(lines) and len(_cells(lines[end]) or ()) == width:
            end += 1
        bad.pop(index, None)
        if end < len(lines) and _cells(lines[end]) is not None:
            bad[end] = "table row cell count differs from the header"
        aligns = [_ALIGN[(cell.strip()[0] == ":", cell.strip()[-1] == ":")] for cell in after[1:-1].split("|")]
        tables[index] = (end, head, aligns, [_cells(row) for row in lines[index + 2:end]])
    return tables, bad


def _table_html(head, aligns, rows):
    def row(cells, tag):
        return "<tr>" + "".join(f"<{tag}{align}>{inline(cell)}</{tag}>" for cell, align in zip(cells, aligns)) + "</tr>"
    body = "<tbody>" + "".join(row(cells, "td") for cells in rows) + "</tbody>" if rows else ""
    return "<table><thead>" + row(head, "th") + "</thead>" + body + "</table>"


_HEADING = re.compile(r"(?:#|#{3,6}) ")
_NUMBERED = re.compile(r"\d{1,3}[.)] ")
_HTML = re.compile(r"<[A-Za-z/!]")
_EMPHASIS = re.compile(r"(?<![*\w])[*_][^*_\s][^*_\n]*[*_](?![*\w])")
_DETAILS = {
    "link-dropped": "link target is not absolute https",
    "link-host": "link host is not a public DNS name",
    "heading-level": 'only "## " headings render',
    "numbered-list": 'only "1. " numbered lists render',
    "bold-unclosed": "unclosed **",
    "raw-html": "raw HTML is shown escaped",
    "emphasis": "only **bold** renders",
    "image": "images render only as their own line: ![alt 1-200 chars](/path-in-public-assets)",
}


def _line_codes(line):
    codes = []
    ordered = _ORDERED.fullmatch(line)
    if _HEADING.match(line):
        codes.append("heading-level")
    if _NUMBERED.match(line) and not ordered:
        codes.append("numbered-list")
    # Lint the same text render passes to inline(), through the same steps.
    body = line[3:].strip() if line.startswith("## ") else line
    if line.startswith("- ") or line.startswith("* "):
        body = line[2:].strip()
    elif ordered:
        body = ordered.group(2).strip()
    return codes + _inline_codes(body)


def _inline_codes(body):
    codes = []
    marked = _BOLD.sub(lambda m: "<strong>" + m.group(1) + "</strong>", escape(body, quote=True))
    # A "![...](...)" span is reported once as ``image`` by lint(), not again as a link.
    codes += [code for m in _LINK.finditer(marked)
              if marked[m.start() - 1:m.start()] != "!" and (code := _target_code(unescape(m.group(2))))]
    unbolded = _BOLD.sub(lambda m: m.group(1), body)
    if "**" in unbolded:
        codes.append("bold-unclosed")
    if _HTML.search(body):
        codes.append("raw-html")
    plain = _LINK.sub(lambda m: m.group(1), unbolded)
    codes += ["emphasis" for _ in _EMPHASIS.finditer(plain)]
    return codes


def _image_line(line, images):
    """True if render shows ``line`` as an image; with ``images=None`` only the syntax is checked."""
    match = _IMAGE.fullmatch(line)
    return bool(match and match.group(1).strip() and (images is None or match.group(2) in images))


def lint(source, images=None):
    """Return sorted (line_no, code, detail) for every construct render() degrades."""
    if not isinstance(source, str):
        raise ValueError("Markdown source must be a string")
    if len(source.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Markdown source too large")
    lines = [raw.strip() for raw in source.replace("\r\n", "\n").split("\n")]
    tables, bad = _tables(lines)
    cells = {}  # line index -> the cells render passes to inline() for that table line
    for start, (_, head, _, rows) in tables.items():
        cells[start], cells[start + 1] = head, []
        cells.update((start + 2 + k, row) for k, row in enumerate(rows))
    found = [(index + 1, "table-malformed", detail) for index, detail in bad.items()]
    for index, line in enumerate(lines):
        shown = _image_line(line, images)  # never a table line: those start with "|"
        if index in cells:
            codes = [code for cell in cells[index] for code in _inline_codes(cell)]
        else:
            codes = _line_codes(line) if line and not shown else []
        if "![" in line and not shown:
            codes.append("image")
        found += [(index + 1, code, _DETAILS[code]) for code in codes]
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
