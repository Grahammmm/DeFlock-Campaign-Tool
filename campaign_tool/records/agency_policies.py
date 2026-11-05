"""Agency policy register: load and validate ``agency-policies.csv``.

One row quotes one section of one agency's own written policy (for example
"Policy 466.5(e)") so a draft can cite it and a policy-versus-practice gap can be
found by code. The register is data: nothing here names an agency, a county or a
campaign. This module only reads the file it is given. It sends, posts,
publishes and writes nothing, and keeps no network or credential access.

Columns (exact header, in order): agency, policy_id, section, quote, doc_sha256,
page, adopted_date. ``adopted_date`` may be blank when the agency has not
produced it; every other column is required.
"""
import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

HEADER = ["agency", "policy_id", "section", "quote", "doc_sha256", "page", "adopted_date"]
REQUIRED = [c for c in HEADER if c != "adopted_date"]
MAX_QUOTE = 400
MAX_BYTES = 5 * 1024 * 1024
AGENCY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
TEXT_MAX = 120


def _problem(line, field, reason):
    return {"line": line, "field": field, "reason": reason}


def validate_row(r, line):
    """Return a list of problems for one row dict (empty when valid)."""
    out = []
    for c in REQUIRED:
        if not (r.get(c) or "").strip():
            out.append(_problem(line, c, "required"))
    a = (r.get("agency") or "").strip()
    if a and not AGENCY_RE.match(a):
        out.append(_problem(line, "agency", "not a lowercase slug"))
    for c in ("policy_id", "section"):
        v = (r.get(c) or "").strip()
        if len(v) > TEXT_MAX:
            out.append(_problem(line, c, "too long"))
    q = (r.get("quote") or "").strip()
    if len(q) > MAX_QUOTE:
        out.append(_problem(line, "quote", f"longer than {MAX_QUOTE} characters (quote a short passage)"))
    sha = (r.get("doc_sha256") or "").strip()
    if sha and not SHA_RE.match(sha):
        out.append(_problem(line, "doc_sha256", "not 64 lowercase hex characters"))
    pg = (r.get("page") or "").strip()
    if pg and not (pg.isascii() and pg.isdigit() and int(pg) >= 1):
        out.append(_problem(line, "page", "not a positive whole number"))
    d = (r.get("adopted_date") or "").strip()
    if d:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                raise ValueError
            if date.fromisoformat(d) > date.today():
                out.append(_problem(line, "adopted_date", "in the future"))
        except ValueError:
            out.append(_problem(line, "adopted_date", "not an ISO date YYYY-MM-DD"))
    return out


def load(path):
    """Read a register. Returns (rows, problems); rows hold only valid, de-duplicated entries.

    A bad header or unreadable file gives no rows and one problem with line 0.
    """
    p = Path(path)
    try:
        if p.is_symlink() or not p.is_file():
            return [], [_problem(0, "file", "not a regular file")]
        if p.stat().st_size > MAX_BYTES:
            return [], [_problem(0, "file", "too large")]
        text = p.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return [], [_problem(0, "file", "unreadable or not UTF-8")]
    reader = csv.reader(text.splitlines(), strict=True)
    try:
        header = next(reader, None)
        if header != HEADER:
            return [], [_problem(0, "header", "must be exactly: " + ",".join(HEADER))]
        rows, problems, seen = [], [], {}
        for n, cells in enumerate(reader, start=2):
            if not cells:
                continue
            if len(cells) != len(HEADER):
                problems.append(_problem(n, "row", f"{len(cells)} columns, expected {len(HEADER)}"))
                continue
            r = {k: v.strip() for k, v in zip(HEADER, cells)}
            bad = validate_row(r, n)
            if bad:
                problems.extend(bad)
                continue
            key = (r["agency"], r["policy_id"], r["section"], r["doc_sha256"], r["page"])
            if key in seen:
                problems.append(_problem(n, "row", f"duplicate of line {seen[key]}"))
                continue
            seen[key] = n
            rows.append(r)
    except csv.Error as e:
        return [], [_problem(0, "file", "malformed CSV: " + str(e)[:80])]
    return rows, problems


def sections(rows, agency, policy_id=None):
    """The agency's rows (optionally one policy), in file order. Unknown agency gives []."""
    return [r for r in rows if r["agency"] == agency and (policy_id is None or r["policy_id"] == policy_id)]


def find(rows, agency, policy_id, section):
    """The first row for agency + policy + section, or None."""
    for r in sections(rows, agency, policy_id):
        if r["section"] == section:
            return r
    return None


def cite(row):
    """Short citation label, e.g. 'Policy 466 466.5(e), p. 2'."""
    return f"{row['policy_id']} {row['section']}, p. {row['page']}"


def file_sha256(path):
    """Hash of a source document, to compare with a row's doc_sha256."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def report(path):
    rows, problems = load(path)
    agencies = sorted({r["agency"] for r in rows})
    return {"schema": "agency-policies-v1", "ok": not problems, "rows": len(rows),
            "agencies": agencies, "problems": problems}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="records agency-policies",
                                 description="Validate an agency-policies.csv register (read only).")
    ap.add_argument("file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rep = report(args.file)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(f"agency-policies: {rep['rows']} valid rows, {len(rep['agencies'])} agencies, "
              f"{len(rep['problems'])} problems")
        for p in rep["problems"]:
            print(f"  line {p['line']} {p['field']}: {p['reason']}", file=sys.stderr)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
