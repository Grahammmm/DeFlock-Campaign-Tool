"""Read-only lint: a quoted passage must match the agency's own page text (REC-LINT-QUOTE-1, rules 1-2).

Prints rule codes by place only, never quoted text. Text check only; no page image is looked at.
"""
import argparse
import re
import sys
from pathlib import Path

DRAFT_MAX = 2 * 1024 * 1024
PAGE_MAX = 5 * 1024 * 1024
MIN_WORDS = 3
OPEN, CLOSE = {'"': '"', "\u201c": "\u201d"}, {'"', "\u201d"}
ATTRIB = re.compile(
    r"[ \t]*\((?:msg[ \t]+(?!p\.)([A-Za-z0-9][A-Za-z0-9._-]*)"
    r"|(?!msg[ \t])([A-Za-z0-9][A-Za-z0-9._-]*)[ \t]+p\.[ \t]*([0-9]+))[ \t]*[),]")
MARKER = re.compile(r"=== \S+ p\.[0-9]+ ===")
OMIT = re.compile(r"\[\.\.\.\]|\.\.\.|\u2026")


class Refusal(Exception):
    pass


def _norm(text):
    return " ".join(text.split())


def _quotes(text):
    """[(start_index, end_index, inner)] for every quote pair; Refusal on an unmatched mark."""
    found, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch in OPEN:
            close = text.find(OPEN[ch], i + 1)
            if close < 0:
                raise Refusal("lint_quotes_unbalanced %d" % (text.count("\n", 0, i) + 1))
            found.append((i, close, text[i + 1:close]))
            i = close
        elif ch in CLOSE:
            raise Refusal("lint_quotes_unbalanced %d" % (text.count("\n", 0, i) + 1))
        i += 1
    return found


def _verbatim(quote, page):
    pos = 0
    segments = [_norm(s) for s in OMIT.split(quote)]
    segments = [s for s in segments if s]
    if not segments:
        return False
    for seg in segments:
        at = page.find(seg, pos)
        if at < 0:
            return False
        pos = at + len(seg)
    return True


def lint(draft_text, pages_reader):
    """Return (hits, stats); hits are (line, col, rule, detail); pages_reader(doc, page) -> text or None."""
    hits, checked, messages = [], 0, 0
    for start, end, inner in _quotes(draft_text):
        if len(inner.split()) < MIN_WORDS:
            continue
        line = draft_text.count("\n", 0, start) + 1
        col = start - (draft_text.rfind("\n", 0, start) + 1) + 1
        m = ATTRIB.match(draft_text, end + 1)
        if not m:
            hits.append((line, col, "Q2", "no_pinpoint"))
        elif m.group(1):
            messages += 1
        else:
            checked += 1
            doc, page = m.group(2), int(m.group(3))
            raw = pages_reader(doc, page)
            if raw is None:
                hits.append((line, col, "Q1", "no_page_file %s p.%d" % (doc, page)))
                continue
            first, _, rest = raw.partition("\n")
            body = rest if MARKER.fullmatch(first.strip()) else raw
            if not _verbatim(inner, _norm(body)):
                hits.append((line, col, "Q1", "not_verbatim %s p.%d" % (doc, page)))
    return hits, {"checked": checked, "messages": messages}


def _read(path, limit, code):
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise Refusal(code)
    try:
        if p.stat().st_size > limit:
            raise Refusal(code)
        return p.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise Refusal(code) from None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agency-lint-quotes")
    ap.add_argument("--draft", required=True)
    ap.add_argument("--pages", required=True)
    args = ap.parse_args(argv)
    pages = Path(args.pages)
    try:
        if pages.is_symlink() or not pages.is_dir():
            raise Refusal("lint_quotes_pages_dir_invalid")
        draft = _read(args.draft, DRAFT_MAX, "lint_quotes_draft_invalid")

        def reader(doc, page):
            path = pages / ("%s_p%03d.txt" % (doc, page))
            if not path.exists() and not path.is_symlink():
                return None
            if path.is_symlink():
                return None
            return _read(path, PAGE_MAX, "lint_quotes_page_invalid")

        hits, stats = lint(draft, reader)
    except Refusal as exc:
        print("refused " + str(exc))
        return 2
    for line, col, rule, detail in hits:
        print("%d:%d %s %s" % (line, col, rule, detail))
    print("agency-lint-quotes: %d hits, %d quotes checked, %d message quotes not checked against pages"
          % (len(hits), stats["checked"], stats["messages"]))
    print("text check only; no visual check of the page images was made")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
