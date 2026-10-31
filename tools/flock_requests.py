#!/usr/bin/env python3
"""Private CPRA request reminder list (REQUESTS.csv v1): --check, due and summary. Prints only.
draft RUNDIR writes FOLLOWUP-DRAFT.md; RUNDIR must lie outside the repository."""
import csv
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import law  # noqa: E402

HEADER = ["request_id", "agency", "sent", "receipt_evidence", "receipt_date", "determination_date",
          "extension_notice_date", "status", "source_sha256"]
ORDER = ("open", "determined", "produced", "closed", "withdrawn")
STATUSES = set(ORDER)
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


def summary(rows):
    """One status line per agency (slug order, first-seen label), then totals. Counts only: no law package, no deadline."""
    groups = {}
    for r in rows:
        groups.setdefault(slug(r["agency"]), []).append(r)
    lines, unknown = [], 0
    for key in sorted(groups):
        rs = groups[key]
        opened = [r for r in rs if r["status"] == "open"]
        unk = sum(not r["receipt_evidence"] for r in opened)
        unknown += unk
        counts = " ".join(f"{s}={sum(r['status'] == s for r in rs)}" for s in ORDER)
        oldest = min((r["sent"] for r in opened), default="-")
        lines.append(f"request status | {rs[0]['agency']} | {counts} | receipt_unknown={unk} | oldest_open_sent {oldest}")
    opened = sum(r["status"] == "open" for r in rows)
    lines.append(f"request status | agencies={len(groups)} requests={len(rows)} open={opened} receipt_unknown={unknown}"
                 " | not a legal status; the canonical ledger wins")
    return lines


QLINE1 = "Research draft - not legal advice. Records to seek; not allegations."
QLINE = re.compile(r"- Q (\S+) \| ([^|]+?) \| prior (\S+) \| ([^|]+)")
ASKS = re.compile(r"\b(explain\w*|why|justif\w*|answer\w*)\b", re.I)
LEGAL = re.compile(r"\b(late|overdue)\b|violat", re.I)
INTRO = ("Under the California Public Records Act, I request electronic copies of the existing records listed below. "
         "This is a new, focused records request related to requests %s.")
FORMAT = ("Please provide existing electronic files in their maintained native format with their existing fields, or an "
          "ordinarily available electronic export, with reasonably segregable nonexempt portions released under "
          "Government Code section 7922.525(b). This request does not require custom programming, a new compilation, "
          "answers to questions, opinions or written explanations.")
WITHHELD = "If any responsive record is withheld in whole or in part, please identify records withheld and the basis for withholding."
END = "END OF REQUEST DRAFT. Do not submit without G's approval. Requester and contact details are intentionally omitted."


def parse_questions(text, mine):
    """Return (items, errors); item = (finding_id, topic, prior ids, records_sought, held reason or None)."""
    items, errs, seen, lines = [], [], set(), text.splitlines()
    if not lines or lines[0] != QLINE1:
        return [], ["line 1: header does not match QUESTIONS.md"]
    status = {r["request_id"]: r["status"] for r in mine}
    for n, line in enumerate(lines[1:], 2):
        m = QLINE.fullmatch(line)
        if not m:
            errs += [f"line {n}: not a '- Q <id> | <topic> | prior <ids> | <records_sought>' line"] if line.strip() else []
            continue
        fid, topic, prior, sought = m.groups()
        prior = [] if prior == "-" else prior.split(";")
        if not sought.startswith("Existing ") or "?" in line or ASKS.search(sought) or LEGAL.search(line):
            errs.append(f"line {n}: records_sought must ask for existing records only, without lateness wording")
        elif any(i not in status for i in prior):
            errs.append(f"line {n}: prior {next(i for i in prior if i not in status)} not in REQUESTS")
        else:
            opened = [i for i in prior if status[i] == "open"]
            held = "open " + ",".join(opened) if opened else "duplicate" if sought in seen else None
            items.append((fid, topic, prior, sought, held))
        seen.add(sought)
    return items, errs


def draft(rundir, requests, start, end):
    run = Path(rundir).resolve()
    if run == REPO or REPO in run.parents:
        return print("flock-followup: RUNDIR is inside the repository", file=sys.stderr) or 1
    if not run.is_dir():
        return print("draft: RUNDIR missing or not a folder", file=sys.stderr) or 1
    rows, errs = load(requests)
    mine = [r for r in rows if slug(r["agency"]) == run.parent.parent.name]
    errs = errs or ([] if mine else ["draft: no requests for agency"])
    text, qfile = "", run / "QUESTIONS.md"
    try:  # O_NOFOLLOW: a link swapped in after a check is still refused
        with open(os.open(qfile, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        errs = errs or ["draft: QUESTIONS.md missing"]
    except (OSError, UnicodeDecodeError) as exc:
        why = "must not be a symlink" if os.path.islink(qfile) else f"unreadable ({type(exc).__name__})"
        errs = errs or [f"draft: QUESTIONS.md {why}"]
    items, qerrs = parse_questions(text, mine) if not errs else ([], [])
    out, errs = run / "FOLLOWUP-DRAFT.md", errs + qerrs
    inc, held = [i for i in items if not i[4]], [i for i in items if i[4]]
    errs = errs or ([] if inc else ["draft: no items to draft"])
    if errs:
        return print("\n".join(errs), file=sys.stderr) or 1
    topics = ", ".join(dict.fromkeys(i[1] for i in inc))
    prior = ", ".join(sorted({p for i in inc for p in i[2]})) or "none on file"
    body = ["DRAFT - not sent - G approval required", f"# {mine[0]['agency']} follow-up records request - DRAFT ONLY",
            f"Subject: CPRA request - existing records follow-up ({topics})", "", INTRO % prior, "",
            f"Unless a narrower period is stated, the period is {start} through {end}.", ""]
    body += [f"{n}. {i[3]}" for n, i in enumerate(inc, 1)] + ["", FORMAT, "", WITHHELD, "", END]
    if held:
        body.append("Held (internal, not part of the request): " + "; ".join(f"{i[0]} {i[4]}" for i in held))
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=run, prefix=".FOLLOWUP-DRAFT.md.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(body) + "\n")
        os.link(tmp, out)
    except FileExistsError:
        return print("draft: FOLLOWUP-DRAFT.md already exists", file=sys.stderr) or 1
    except OSError as exc:
        return print(f"draft: cannot write in RUNDIR ({type(exc).__name__})", file=sys.stderr) or 1
    finally:
        if tmp:
            os.unlink(tmp)
    return print(f"items={len(inc)} held={len(held)} written=FOLLOWUP-DRAFT.md") or 0


USAGE = "usage: flock_requests.py --check PATH | due PATH | summary PATH | draft RUNDIR --requests PATH --from YYYY-MM-DD --to YYYY-MM-DD"

def main(argv):
    if argv in (["--help"], ["-h"]):
        print(USAGE); return 0  # noqa: E702
    if len(argv) == 8 and argv[0] == "draft" and argv[2::2] == ["--requests", "--from", "--to"]:
        start, end = day(argv[5]), day(argv[7])
        if start and end and start <= end:
            return draft(argv[1], argv[3], argv[5], argv[7])
    if len(argv) != 2 or argv[0] not in ("--check", "due", "summary"):
        print(USAGE, file=sys.stderr)
        return 2
    rows, errs = load(argv[1])
    if argv[0] == "--check":
        print("\n".join(errs + [f"requests={len(rows)} errors={len(errs)}"]))
        return 1 if errs else 0
    if errs:
        print("\n".join(errs), file=sys.stderr)
        return 1
    if argv[0] == "summary":
        print("\n".join(summary(rows)))
        return 0
    try:
        lines = due(rows)
    except ValueError as exc:
        print(f"due: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
