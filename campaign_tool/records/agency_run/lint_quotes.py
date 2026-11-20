"""Read-only lint: a quoted passage must match the agency's own page text (REC-LINT-QUOTE-1, rules 1-3).

Prints rule codes by place only, never quoted text. Q1/Q2 are a text check only; no page image is looked at.
Q3 (--ledger/--inventory/--original-root) only checks that the recorded original-page ledger is consistent
with the inventory and the original file bytes; it never performs or claims a visual check.
"""
import argparse
import csv
import hashlib
import io
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from .evidence_index import MAX_PDF, inside
from .pages import INVENTORY

LEDGER = "quote_id doc_id page original_sha256 method render_sha256 verdict checked_by checked_at".split()
METHODS = ("native-reextract", "visual-render", "none")
VERDICTS = ("VERIFIED-ORIGINAL", "UNVERIFIED-OCR-ONLY", "UNVERIFIED-UNREADABLE")
HEX = re.compile(r"[0-9a-f]{64}")
QUOTE_ID = re.compile(r"[0-9]+:[0-9]+:[0-9a-f]{64}")
DOC = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ROLE = re.compile(r"[a-z][a-z0-9_-]{1,39}")
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?(Z|[+-][0-9]{2}:[0-9]{2})")
CLAIM = re.compile(r"[ \t]*\[VERIFIED-ORIGINAL\]")
NOTE_PAGE = re.compile(r"p\.([0-9]+)")
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


def _table(path, header, code):
    rows = list(csv.reader(io.StringIO(_read(path, DRAFT_MAX, code)), delimiter="\t", quoting=csv.QUOTE_NONE))
    if not rows or rows[0] != header or any(len(r) != len(header) for r in rows):
        raise Refusal(code)
    return [dict(zip(header, r)) for r in rows[1:]]


def _stamp_ok(text):
    try:
        return bool(STAMP.fullmatch(text)) and datetime.fromisoformat(text).tzinfo is not None
    except ValueError:
        return False


def load_ledger(path):
    """{quote_id: row} from a quote-verify.tsv; fixed Refusal on any malformed row or duplicate."""
    code, out = "lint_quotes_ledger_invalid", {}
    for d in _table(path, LEDGER, code):
        ok = (QUOTE_ID.fullmatch(d["quote_id"]) and DOC.fullmatch(d["doc_id"])
              and re.fullmatch(r"[1-9][0-9]{0,5}", d["page"]) and HEX.fullmatch(d["original_sha256"])
              and d["method"] in METHODS and (not d["render_sha256"] or HEX.fullmatch(d["render_sha256"]))
              and d["verdict"] in VERDICTS and ROLE.fullmatch(d["checked_by"]) and _stamp_ok(d["checked_at"]))
        if not ok or d["quote_id"] in out:
            raise Refusal(code)
        out[d["quote_id"]] = d
    return out


def load_inventory(path):
    """{doc_id: row} from an agency-pages inventory.tsv; fixed Refusal on malformed or duplicate rows."""
    code, out = "lint_quotes_inventory_invalid", {}
    for d in _table(path, INVENTORY, code):
        if not DOC.fullmatch(d["doc_id"]) or d["doc_id"] in out:
            raise Refusal(code)
        out[d["doc_id"]] = d
    return out


def resolve_original(root, row):
    """Validated regular, symlink-free PDF path for an inventory row inside root; fixed Refusal otherwise."""
    code, rel = "lint_quotes_original_invalid", row["source"]
    real = None
    if rel.lower().endswith(".pdf") and ".." not in rel.split(os.sep) and os.path.isdir(root):
        real = inside(os.path.realpath(root), rel)
    try:
        if real is None or not os.path.isfile(real) or os.path.getsize(real) > MAX_PDF:
            raise Refusal(code)
    except OSError:
        raise Refusal(code) from None
    return real


def _digest(ctx, doc, row):
    if doc not in ctx["digests"]:
        try:
            with open(resolve_original(ctx["root"], row), "rb") as handle:
                ctx["digests"][doc] = hashlib.sha256(handle.read()).hexdigest()
        except (OSError, Refusal):
            ctx["digests"][doc] = None
    return ctx["digests"][doc]


def _q3(ctx, inner, line, col, m, text):
    """First failing Q3 reason for one quote occurrence, or None."""
    if not m or m.group(1):
        return "source_unsupported"
    row = ctx["ledger"].get("%d:%d:%s" % (line, col, hashlib.sha256(inner.encode("utf-8")).hexdigest()))
    if row is None:
        return "ledger_missing"
    doc, page = m.group(2), int(m.group(3))
    inv = ctx["inventory"].get(doc)
    if row["doc_id"] != doc or int(row["page"]) != page or (
            inv and (not re.fullmatch("[0-9]+", inv["pages"]) or not 1 <= page <= int(inv["pages"]))):
        return "pinpoint_mismatch"
    if inv is None or row["original_sha256"] != inv["sha256"] or row["original_sha256"] != _digest(ctx, doc, inv):
        return "original_mismatch"
    verified = row["verdict"] == "VERIFIED-ORIGINAL"
    if verified:
        blank = {int(n) for n in NOTE_PAGE.findall(inv["notes"].partition("no text layer:")[2])}
        render = row["method"] == "visual-render" and row["render_sha256"]
        native = row["method"] == "native-reextract" and inv["text_layer"] == "native" and page not in blank
        if not (render or native):
            return "method_invalid"
    nl = text.find("\n", m.end())
    after = m.end() if text[m.end() - 1] == ")" else text.find(")", m.end(), len(text) if nl < 0 else nl) + 1
    if after > 0 and CLAIM.match(text, after) and not verified:
        return "claim_unverified"
    return None


def lint(draft_text, pages_reader, q3=None):
    """Return (hits, stats); hits are (line, col, rule, detail); pages_reader(doc, page) -> text or None.

    q3 is {"ledger", "inventory", "root"} to add the original-page ledger check (rule Q3).
    """
    hits, checked, messages = [], 0, 0
    ctx = q3 and {**q3, "digests": {}}
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
            else:
                first, _, rest = raw.partition("\n")
                body = rest if MARKER.fullmatch(first.strip()) else raw
                if not _verbatim(inner, _norm(body)):
                    hits.append((line, col, "Q1", "not_verbatim %s p.%d" % (doc, page)))
        if ctx:
            reason = _q3(ctx, inner, line, col, m, draft_text)
            if reason:
                hits.append((line, col, "Q3", reason))
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
    ap.add_argument("--ledger", help="quote-verify.tsv; with --inventory and --original-root adds rule Q3")
    ap.add_argument("--inventory", help="agency-pages inventory.tsv (Q3)")
    ap.add_argument("--original-root", help="the root given to agency-pages (Q3)")
    args = ap.parse_args(argv)
    pages = Path(args.pages)
    q3_opts = (args.ledger, args.inventory, args.original_root)
    try:
        if any(q3_opts) and not all(q3_opts):
            raise Refusal("lint_quotes_q3_options_incomplete")
        if pages.is_symlink() or not pages.is_dir():
            raise Refusal("lint_quotes_pages_dir_invalid")
        draft = _read(args.draft, DRAFT_MAX, "lint_quotes_draft_invalid")
        q3 = None
        if all(q3_opts):
            q3 = {"ledger": load_ledger(args.ledger), "inventory": load_inventory(args.inventory),
                  "root": args.original_root}

        def reader(doc, page):
            path = pages / ("%s_p%03d.txt" % (doc, page))
            if not path.exists() and not path.is_symlink():
                return None
            if path.is_symlink():
                return None
            return _read(path, PAGE_MAX, "lint_quotes_page_invalid")

        hits, stats = lint(draft, reader, q3)
    except Refusal as exc:
        print("refused " + str(exc))
        return 2
    for line, col, rule, detail in hits:
        print("%d:%d %s %s" % (line, col, rule, detail))
    print("agency-lint-quotes: %d hits, %d quotes checked, %d message quotes not checked against pages"
          % (len(hits), stats["checked"], stats["messages"]))
    print("text check only; no visual check of the page images was made")
    if q3:
        print("Q3 checked the ledger for consistency only; no visual check of the original pages was made")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
