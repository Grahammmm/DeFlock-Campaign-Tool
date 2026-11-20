"""Refuse an agency letter that names another agency as a release precedent (REC-LINT-OUT-1 rule L9).

Another agency may be named only as the subject of an ask. Per sentence: refusal L9_RELEASE_PRECEDENT when it names another
agency with a release or disclosure cue; note L9_COMPARISON when it names one with like, similar, also or a comparison; clean
when the agency is only the subject of an ask (access by, sharing with, searches run by). The same refusal inside a Markdown
blockquote is a note, L9_QUOTED_PRECEDENT. The addressee agency (--agency) is never flagged.

--agencies is a tab-separated file with a header row: an agency column (the key and canonical name) and an optional aliases
column of pipe-separated names. Other columns are ignored. A mechanical check only: nothing is written and output is
LINE CLASS RULE rows, never a sentence, a name or a path. Exit 1 only on a refusal, 0 otherwise, 2 on a bad argument or file
(one fixed stderr line). A clean result is not a send approval.
"""
import bisect
import os
import re
import stat
import sys

from .lint_out import QuietParser, _literal, _read

MAX_DRAFT, MAX_AGENCIES = 2 * 1024 * 1024, 64 * 1024
REFUSED = "refused agency_lint_precedent_input"
OTHER = "\ue000"  # stands in for a matched other agency while cues are searched
QUOTE = re.compile(r"^\s*(?:>\s?)+")
ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s")
BOUNDARY = re.compile(r"[.?!]+[\"')\]]*(?=\s|$)")
BLOCK = re.compile(r"\s{0,3}(?:#{1,6}(?:\s|$)|`{3,}|~{3,}|(?:[-*_]\s*){3,}$)")  # a line that cannot lazily continue a quote
FENCE = re.compile(r"\s{0,3}(`{3,}|~{3,})(.*)$")  # (marker run, info string): a fence opens or closes a fenced code block
ABBREV = re.compile(r"(?:^|[\s(\[\"'])(?:vs|cf|mr|mrs|ms|dr|[a-z]{1,2}(?:\.[a-z]{1,2})+)$", re.I)  # never ends a sentence
ABBREV_MID = re.compile(r"(?:^|[\s(\[\"'])(?:inc|ltd|llc|co|corp|dept|depts|dist|st|jr|sr|approx)$", re.I)  # ends one only before a capital
LOWER = re.compile(r"\s+[a-z]")
ASK = re.compile(r"(?:access\s+by|sharing\s+with|searches\s+run\s+by)\s+(?:the\s+)?$", re.I)
RELEASE = re.compile(
    r"\b(?:released|produced|provided|disclosed|precedents?)\b|\bmade\s+public\b|\bhas\s+shared\s+its\s+logs\b"
    r"|\bother\s+agencies\s+have\b|\bas\s+" + OTHER + r"(?:\s+\w+){0,2}?\s+(?:also\s+)?did\b", re.I)
COMPARE = re.compile(r"\b(?:like|similar|also|unlike)\b|\bcompared\s+(?:with|to)\b|\bas\s+with\b|\bsame\s+as\b", re.I)


def _norm(text):
    return " ".join(text.split()).lower()


def _regular(path, limit):
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise ValueError
    return _read(path, limit)


def load(text, key):
    """-> ({canonical key: [names and aliases]}, addressee key); ValueError on any malformed row."""
    rows = [line.split("\t") for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
    head = [_norm(cell) for cell in rows[0]]
    if "agency" not in head or len(set(head)) != len(head):
        raise ValueError
    name = head.index("agency")
    alias = head.index("aliases") if "aliases" in head else None
    names = {}
    for row in rows[1:]:
        if len(row) != len(head) or not _norm(row[name]) or _norm(row[name]) in names:
            raise ValueError
        names[_norm(row[name])] = [_norm(row[name])] + ([_norm(a) for a in row[alias].split("|") if a.strip()] if alias is not None else [])
    if _norm(key) not in names:
        raise ValueError
    return names, _norm(key)


def matcher(names, me):
    """-> (regex over every name and alias, {phrase: owning key}); the addressee wins a shared phrase, longest phrase first."""
    owner = {}
    for who, phrases in names.items():
        for phrase in phrases:
            if who == me or owner.get(phrase) != me:
                owner[phrase] = who
    return re.compile("|".join("(?:%s)" % _literal(p).pattern for p in sorted(owner, key=lambda p: (-len(p), p))), re.I), owner


def fenced(body, fence):
    """-> the fence state after a quoted line: (char, length) while inside a fenced block, else None."""
    m = FENCE.match(body)
    if fence is None:  # a backtick fence's info string may not hold a backtick; otherwise this is an opening fence
        return (m.group(1)[0], len(m.group(1))) if m and not (m.group(1)[0] == "`" and "`" in m.group(2)) else None
    closes = m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip()  # same char, no shorter, no info
    return None if closes else fence


def runs(text):
    """-> [(quoted, one-line text, [(offset, line)])]: paragraphs and quote blocks, soft-wrapped lines joined by a space."""
    out, cur, lazy, fence = [], None, False, None
    for number, raw in enumerate(text.split("\n"), 1):
        quoted = bool(QUOTE.match(raw))
        body = QUOTE.sub("", raw, count=1) if quoted else raw
        if not body.strip():
            cur, lazy = None, False
            if not quoted:  # only a blank line outside the quote ends it; a quoted blank line keeps a fenced block open
                fence = None
            continue
        if not quoted and cur is not None and cur[0] and lazy and not ITEM.match(body) and not BLOCK.match(body):
            quoted = True  # Markdown lazy continuation: an unmarked line directly after a quoted paragraph line stays in the quote
        elif quoted:
            was = fence
            fence = fenced(body, fence)
            # only a quoted paragraph line (not a heading, rule, fence line or fenced code) can be continued lazily
            lazy = was is None and fence is None and not BLOCK.match(body)
        else:
            lazy, fence = False, None
        if cur is None or cur[0] != quoted or ITEM.match(body):
            cur = [quoted, [], [], 0]
            out.append(cur)
        cur[2].append((cur[3], number))
        cur[1].append(body + " ")
        cur[3] += len(body) + 1
    return [(q, "".join(parts), starts) for q, parts, starts, _ in out]


def abbreviation(body, m):
    """True when a period after "e.g.", "Dr." or (before a lowercase word) "Inc." continues the sentence."""
    if m.group() != ".":
        return False
    head = body[:m.start()]
    return bool(ABBREV.search(head) or (ABBREV_MID.search(head) and LOWER.match(body, m.end())))


def scan(text, names, me):
    """-> [(line, class, rule)] in sentence order, one row per sentence."""
    rx, owner = matcher(names, me)
    rows = []
    for quoted, body, starts in runs(text.replace("\r\n", "\n").replace("\r", "\n")):
        spans = [(m.start(), m.end(), owner.get(_norm(m.group())) != me) for m in rx.finditer(body)]
        begins = [s for s, _, _ in spans]
        cuts = [0]
        for m in BOUNDARY.finditer(body):  # never split inside a recognized name ("Dept." in an alias) or after an abbreviation ("Inc.", "e.g.")
            i = bisect.bisect_left(begins, m.end()) - 1
            if (i < 0 or spans[i][1] <= m.start()) and not abbreviation(body, m):
                cuts.append(m.end())
        cuts.append(len(body))
        for lo, hi in zip(cuts, cuts[1:]):
            inside = spans[bisect.bisect_left(begins, lo):bisect.bisect_left(begins, hi)]
            other = [span for span in inside if span[2]]
            if not other:
                continue
            masked, pos = [], lo
            for s, e, is_other in inside:
                masked += [body[pos:s], OTHER if is_other else " "]
                pos = e
            cue = "".join(masked) + body[pos:hi]
            first = other[0]
            if RELEASE.search(cue):
                row = ("note", "L9_QUOTED_PRECEDENT") if quoted else ("refusal", "L9_RELEASE_PRECEDENT")
            else:
                rest = [s for s in other if not ASK.search(body[max(lo, s[0] - 40):s[0]])]
                if not rest or not COMPARE.search(cue):
                    continue
                first, row = rest[0], ("note", "L9_COMPARISON")
            rows.append((starts[bisect.bisect_right([o for o, _ in starts], first[0]) - 1][1],) + row)
    return rows


def main(argv=None):
    parser = QuietParser(prog="records agency-lint-precedent", description=__doc__, refusal=REFUSED)
    for name in ("--draft", "--agencies", "--agency"):
        parser.add_argument(name, required=True)
    try:
        args = parser.parse_args(argv)
        text = _regular(args.draft, MAX_DRAFT)
        names, me = load(_regular(args.agencies, MAX_AGENCIES), args.agency)
        rows = scan(text, names, me)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    except Exception:
        print(REFUSED, file=sys.stderr)
        return 2
    for row in rows:
        print(*row)
    return 1 if any(row[1] == "refusal" for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
