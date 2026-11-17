"""Read-only lint of a follow-up draft against what the agency already gave or answered (REC-LINT-ASK-1).

Prints rule codes by place only, never letter text. A hit is a review note, not a legal finding.
"""
import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

from .. import playbook

DRAFT_MAX = 256 * 1024
TABLE_MAX = 5 * 1024 * 1024
REPEAT_RATINGS = {"answered", "missing", "unclear"}
EXCLUSIONS = ("already have", "already hold", "do not resend", "no need to resend", "need not resend",
              "not asking for", "other than", "excluding", "except", "in addition to", "beyond")
ANSWERED_EVENTS = {"determination", "determination_production", "production", "no_records", "denial", "closed"}
CHASE = ("still waiting", "no response", "has not responded", "have not heard", "awaiting", "please respond",
         "overdue", "past the")
REQUIRED = {"inventory.tsv": ("doc_id", "title", "request_ref"),
            "asks.tsv": ("request_ref", "text", "rating"),
            "events.tsv": ("request_ref", "event_type")}
REF = re.compile(r"(?<![0-9-])[0-9]{2}-[0-9]{2,5}(?![0-9-])")
ASK_START = re.compile(r"^\s*([0-9]{1,2})[.)]\s")
JACCARD_MIN = 0.6


class Refusal(Exception):
    pass


def _read_text(path, limit, code):
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise Refusal(code)
    try:
        if p.stat().st_size > limit:
            raise Refusal(code)
        return p.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise Refusal(code) from None


def _table(agency_dir, name):
    path = Path(agency_dir) / name
    if not path.exists() and not path.is_symlink():
        raise Refusal("agency_file_missing:" + name)
    text = _read_text(path, TABLE_MAX, "agency_file_invalid:" + name)
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter="\t", quoting=csv.QUOTE_NONE))
    except csv.Error:
        raise Refusal("agency_file_invalid:" + name) from None
    header = rows[0] if rows else []
    for column in REQUIRED[name]:
        if column not in header:
            raise Refusal("column_missing:%s:%s" % (name, column))
    return [{h: (r[i].strip() if i < len(r) else "") for i, h in enumerate(header)} for r in rows[1:] if any(r)]


def _words(text):
    return set(re.findall(r"[a-z0-9]{4,}", text.lower()))


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _asks(lines):
    """[(ordinal, start_line, end_line, text)] with 1-based physical line numbers."""
    asks, current = [], None
    for number, raw in enumerate(lines, 1):
        line = re.sub(r"^(\s*>)+", "", raw)
        if ASK_START.match(line):
            current = [len(asks) + 1, number, number, [ASK_START.sub("", line, 1)]]
            asks.append(current)
        elif not line.strip():
            current = None
        elif current:
            current[2] = number
            current[3].append(line)
    return [(k, s, e, " ".join(t)) for k, s, e, t in asks]


def lint(draft_text, inventory, asks_rows, events):
    lines = draft_text.splitlines()
    asks = _asks(lines)
    if not asks:
        raise Refusal("no_asks")
    refs = set()
    for rows in (inventory, asks_rows, events):
        for row in rows:
            cell = row["request_ref"]
            if cell.strip().lower() == "context" or cell.strip().lower().endswith("(third party)"):
                continue
            refs.update(REF.findall(cell))
    answered = {REF.findall(r["request_ref"])[0] for r in events
                if r["event_type"].strip().lower() in ANSWERED_EVENTS and REF.findall(r["request_ref"])}
    hits = set()

    def ask_at(number):
        return next((k for k, s, e, _ in asks if s <= number <= e), "-")

    prior = [_words(r["text"]) for r in asks_rows if r["rating"].strip().lower() in REPEAT_RATINGS]
    titles = [t for t in (_norm(re.sub(r"\.[A-Za-z0-9]{1,5}$", "", r["title"])) for r in inventory) if len(t) >= 8]
    for k, start, _end, text in asks:
        words = _words(text)
        if words and any(p and len(words & p) / len(words | p) >= JACCARD_MIN for p in prior):
            hits.add((start, k, "R1_repeat_ask"))
        low = text.lower()
        if any(t in _norm(text) for t in titles) and not any(x in low for x in EXCLUSIONS):
            hits.add((start, k, "R7_named_held_doc"))
    for number, raw in enumerate(lines, 1):
        found = set(REF.findall(raw))
        if found - refs:
            hits.add((number, ask_at(number), "R8_foreign_ref"))
        if (found & answered) and any(c in raw.lower() for c in CHASE):
            hits.add((number, ask_at(number), "R8_custodian_answered"))
    book = playbook.load()
    first_tier1 = next((i for i, a in enumerate(asks) if playbook.tier_for(book, a[3]) == 1), None)
    if first_tier1 is not None:
        for k, start, _end, text in asks[:first_tier1]:
            if playbook.tier_for(book, text) in (2, 3):
                hits.add((start, k, "R9_tier_order"))
    return [{"line": l, "ask": k, "code": c} for l, k, c in sorted(hits, key=lambda h: (h[0], h[2]))]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records agency-lint-asks", description=__doc__)
    parser.add_argument("--draft", required=True)
    parser.add_argument("--agency-dir", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        draft = Path(args.draft)
        if not draft.exists() and not draft.is_symlink():
            raise Refusal("draft_missing")
        text = _read_text(draft, DRAFT_MAX, "draft_invalid")
        tables = [_table(args.agency_dir, name) for name in REQUIRED]
        hits = lint(text, *tables)
    except Refusal as refusal:
        print("agency-lint-asks: %s" % refusal, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": not hits, "hits": hits}))
    else:
        for h in hits:
            print("draft line %d ask %s: %s" % (h["line"], h["ask"], h["code"]))
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
