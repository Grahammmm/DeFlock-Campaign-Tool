#!/usr/bin/env python3
"""Private CPRA request reminder list (REQUESTS.csv v1): --check and due. Prints only."""
import csv
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool import law  # noqa: E402

HEADER = ["request_id", "agency", "sent", "receipt_evidence", "receipt_date", "determination_date",
          "extension_notice_date", "status", "source_sha256"]
STATUSES = {"open", "determined", "produced", "closed", "withdrawn"}
DATES = ("sent", "receipt_date", "determination_date", "extension_notice_date")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def day(value):
    try:
        return date.fromisoformat(value) if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) else None
    except ValueError:
        return None


def check_row(r, folder, ids, bad):
    rid = r["request_id"]
    identity = (slug(r["agency"]), rid)
    if not rid or identity in ids:
        bad("request_id", "is empty or duplicates an earlier row")
    ids.add(identity)
    if not r["agency"] or (folder and slug(r["agency"]) != folder):
        bad("agency", "is empty or does not match its folder")
    d = {k: day(r[k]) for k in DATES}
    for k in DATES:
        if d[k] is None and (r[k] or k == "sent"):
            bad(k, "must be YYYY-MM-DD")
    if r["status"] not in STATUSES:
        bad("status", "must be one of " + ", ".join(sorted(STATUSES)))
    ev = r["receipt_evidence"]
    if len(ev) > 120:
        bad("receipt_evidence", "is longer than 120 characters")
    if ev:
        if not r["receipt_date"]:
            bad("receipt_date", "is required with receipt_evidence")
        if not re.fullmatch(r"[0-9a-f]{64}", r["source_sha256"]):
            bad("source_sha256", "must be 64 lowercase hex")
    elif r["receipt_date"]:
        bad("receipt_date", "is given without receipt_evidence")
    floor = d["receipt_date"] or d["sent"]
    for k, lo in (("receipt_date", d["sent"]), ("extension_notice_date", floor), ("determination_date", floor)):
        if d[k] and lo and d[k] < lo:
            bad(k, "is before the date it must follow")
    if (r["status"] == "open" and r["determination_date"]) or (r["status"] == "determined" and not r["determination_date"]):
        bad("determination_date", "conflicts with status")


def load(path):
    p = Path(path)
    files = [(f.parent.name, f) for f in sorted(p.glob("agencies/*/REQUESTS.csv"))] if p.is_dir() else [(None, p)]
    rows, ids, errs = [], set(), [] if files else ["store: no agencies/*/REQUESTS.csv found"]
    for folder, f in files:
        pre = f"{folder}: " if folder else ""
        try:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                table = list(csv.reader(fh))
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            errs.append(f"{pre}row 1: file unreadable ({type(exc).__name__})")
            continue
        if not table or table[0] != HEADER:
            errs.append(f"{pre}row 1: header does not match REQUESTS.csv v1")
            continue
        for n, cells in enumerate(table[1:], 2):
            if not cells:
                continue
            if len(cells) != len(HEADER):
                errs.append(f"{pre}row {n}: columns expected {len(HEADER)}, found {len(cells)}")
                continue
            r = dict(zip(HEADER, (c.strip() for c in cells)))
            check_row(r, folder, ids, lambda field, why, n=n: errs.append(f"{pre}row {n}: {field} {why}"))
            rows.append(r)
    return rows, errs


def due(rows):
    pkg = law.load_package("us-ca")
    lw = pkg["records_law"]
    items, unknown = [], 0
    for r in (r for r in rows if r["status"] == "open"):
        head = f"provisional reminder | {r['request_id']} | {r['agency']} | "
        if not r["receipt_evidence"]:
            unknown += 1
            items.append((1, "", r["request_id"], head + "receipt unknown | no date computed"))
            continue
        ext = bool(r["extension_notice_date"])
        when = law.deadline(pkg, r["receipt_date"], extension=ext).isoformat()
        basis = f"receipt {r['receipt_date']} + {lw['determination_days']} {lw['day_type']} days"
        if ext:
            basis += f" + {lw['determination_extension_days']} extension notice"
        items.append((0, when, r["request_id"],
                      f"{head}determination {when} | {basis} | Gov. Code 7922.535 | not a legal due date"))
    lines = [i[3] for i in sorted(items, key=lambda i: i[:3])]
    lines.append(f"provisional reminder | open={len(items)} receipt_unknown={unknown} | " + law.HOLIDAY_NOTE)
    return lines


def main(argv):
    if len(argv) != 2 or argv[0] not in ("--check", "due"):
        print("usage: flock_requests.py --check PATH | due PATH", file=sys.stderr)
        return 2
    rows, errs = load(argv[1])
    if argv[0] == "--check":
        print("\n".join(errs + [f"requests={len(rows)} errors={len(errs)}"]))
        return 1 if errs else 0
    if errs:
        print("\n".join(errs), file=sys.stderr)
        return 1
    try:
        lines = due(rows)
    except ValueError as exc:
        print(f"due: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
