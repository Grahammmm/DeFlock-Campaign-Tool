#!/usr/bin/env python3
"""Private CPRA request reminder list (REQUESTS.csv v1): --check, due and summary. Prints only.
draft RUNDIR writes FOLLOWUP-DRAFT.md; RUNDIR must lie outside the repository."""
import csv
import io
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


def load(path, folder=None):
    p = Path(path)
    files = [(f.parent.name, f) for f in sorted(p.glob("agencies/*/REQUESTS.csv"))] if p.is_dir() else [(folder, p)]
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


def days(n):
    return f"{n} day" + ("" if n == 1 else "s")


def due(rows, as_of=None):
    """as_of (a date) adds a provisional distance cell per line and upcoming_7d/passed totals; reminder arithmetic only."""
    pkg = law.load_package("us-ca")
    lw = pkg["records_law"]
    items, unknown, upcoming, passed = [], 0, 0, 0
    cell = f" | as of {as_of}: "
    for r in (r for r in rows if r["status"] == "open"):
        head = f"provisional reminder | {r['request_id']} | {r['agency']} | "
        if not r["receipt_evidence"]:
            unknown += 1
            line = head + "receipt unknown | no date computed"
            if as_of:
                k = (as_of - day(r["sent"])).days
                line += cell + (f"sent {days(k)} ago" if k >= 0 else "sent after the as-of date")
            items.append((1, "", r["request_id"], line))
            continue
        ext = bool(r["extension_notice_date"])
        when = law.deadline(pkg, r["receipt_date"], extension=ext)
        basis = f"receipt {r['receipt_date']} + {lw['determination_days']} {lw['day_type']} days"
        if ext:
            basis += f" + {lw['determination_extension_days']} extension notice"
        line = f"{head}determination {when.isoformat()} | {basis} | Gov. Code 7922.535 | not a legal due date"
        if as_of:
            n = (when - as_of).days
            upcoming, passed = upcoming + (0 <= n <= 7), passed + (n < 0)
            line += cell + (f"in {days(n)}" if n > 0 else "reminder date today" if n == 0
                            else f"reminder date passed {days(-n)} ago")
        items.append((0, when.isoformat(), r["request_id"], line))
    lines = [i[3] for i in sorted(items, key=lambda i: i[:3])]
    extra = f" upcoming_7d={upcoming} passed={passed} as_of={as_of}" if as_of else ""
    lines.append(f"provisional reminder | open={len(items)} receipt_unknown={unknown}{extra} | " + law.HOLIDAY_NOTE)
    return lines


def inventory(path):
    """([(row number, row)], None) or (None, reason); same header and row checks as flock_facts coverage."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            table = list(csv.reader(fh))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return None, f"inventory unreadable ({type(exc).__name__})"
    head = table[0] if table else []
    lacks = [c for c in ("request", "doc_id", "sha256", "analysis_only") if c not in head]
    if lacks:
        return None, "inventory header lacks " + ", ".join(lacks)
    out = []
    for n, cells in ((n, c) for n, c in enumerate(table[1:], 2) if c):
        if len(cells) != len(head):
            return None, f"inventory row {n}: columns have the wrong count"
        out.append((n, dict(zip(head, cells))))
    return out, None


def documents(groups, inv):
    """{(agency slug, request_id): {sha256: withheld}} and one named line per unmatched row. Withheld: any analysis_only
    other than "no" (the coverage rule). Titles, doc_ids, shas and source_ids are never put in a line."""
    owners, docs, named = {}, {}, []
    for key, rs in groups.items():
        for r in rs:
            owners.setdefault(r["request_id"], []).append(key)
    for n, rec in inv:
        rid, held = rec["request"].strip(), owners.get(rec["request"].strip(), [])
        if rid and len(held) == 1:
            got = docs.setdefault((held[0], rid), {})
            sha = rec["sha256"].strip()
            got[sha] = got.get(sha, False) or rec["analysis_only"] != "no"
        else:
            why = f"request {rid} | " + ("ambiguous request" if held else "unknown request") if rid else "request missing"
            named.append(f"request docs | inventory row {n} | {why}")
    return docs, named


def summary(rows, inv=None):
    """One status line per agency (slug order, first-seen label), then totals. Counts only: no law package, no deadline.
    inv (from inventory()) adds a document-count line per request after its agency and names unmatched rows."""
    groups = {}
    for r in rows:
        groups.setdefault(slug(r["agency"]), []).append(r)
    docs, named = documents(groups, inv) if inv is not None else ({}, [])
    lines, unknown, posted, withheld = [], 0, 0, 0
    for key in sorted(groups):
        rs = groups[key]
        opened = [r for r in rs if r["status"] == "open"]
        unk = sum(not r["receipt_evidence"] for r in opened)
        unknown += unk
        counts = " ".join(f"{s}={sum(r['status'] == s for r in rs)}" for s in ORDER)
        oldest = min((r["sent"] for r in opened), default="-")
        lines.append(f"request status | {rs[0]['agency']} | {counts} | receipt_unknown={unk} | oldest_open_sent {oldest}")
        for r in sorted(rs, key=lambda r: r["request_id"]) if inv is not None else ():
            got = docs.get((key, r["request_id"]), {})
            held = sum(got.values())
            posted, withheld = posted + len(got) - held, withheld + held
            lines.append(f"request docs | {rs[0]['agency']} | {r['request_id']} | status={r['status']}"
                         f" | posted={len(got) - held} withheld={held}")
    opened = sum(r["status"] == "open" for r in rows)
    extra = (f" documents={posted + withheld} posted={posted} withheld={withheld} unknown_rows={len(named)}"
             if inv is not None else "")
    lines += named
    lines.append(f"request status | agencies={len(groups)} requests={len(rows)} open={opened} receipt_unknown={unknown}"
                 f"{extra} | not a legal status; the canonical ledger wins")
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


FLAGS = ("--agency", "--request", "--step", "--date", "--evidence", "--sha256")
STEP_HEADER = ["request_id", "step", "date", "evidence", "source_sha256", "recorded_at"]
STEPS = {"receipt": ("receipt_date", "receipt_evidence", "source_sha256"), "extension": ("extension_notice_date",),
         "determination": ("determination_date",), "produced": (), "closed": ()}
STEP_STATUS = {"determination": "determined", "produced": "produced", "closed": "closed"}


def csv_text(rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def replace(path, text):  # temp file in the same folder (0600) + os.replace; the temp never outlives the call
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def record(store, o, today):
    """Set one dated, sourced step on one REQUESTS.csv row and append it to REQUEST-STEPS.csv; refusals write nothing."""
    s, rid, step, when, ev, sha = (o[k] for k in FLAGS)
    root, d = Path(store).resolve(), day(when)
    folder = root / "agencies" / s
    req, log = folder / "REQUESTS.csv", folder / "REQUEST-STEPS.csv"
    link = next((p.name for p in (folder, req, log) if p.is_symlink()), None)
    why = ("store is inside the repository" if root == REPO or REPO in root.parents
           else "--agency must be a folder slug" if not s or slug(s) != s
           else f"{link} must not be a symlink" if link
           else f"agency folder {s} not found" if not folder.is_dir()
           else f"agency folder {s} has no REQUESTS.csv" if not req.is_file()
           else "--date must be YYYY-MM-DD" if d is None else "--date is after --today" if d > today
           else "--evidence must be 1-120 characters on one line" if not ev.strip() or len(ev) > 120 or re.search(r"[\r\n]", ev)
           else "--sha256 must be 64 lowercase hex" if not re.fullmatch(r"[0-9a-f]{64}", sha) else None)
    rows, errs = load(req, s) if why is None else ([], [])
    why = why or (f"REQUESTS.csv fails --check ({errs[0]})" if errs else None)
    row = next((r for r in rows if r["request_id"] == rid), None)
    why = why or (None if row else f"request {rid} not found in {s}/REQUESTS.csv")
    old_log, mine = "", []
    try:  # O_NOFOLLOW: a link swapped in after the check is still refused
        with open(os.open(log, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8", newline="") as fh:
            old_log = fh.read()
        table = list(csv.reader(io.StringIO(old_log)))
        mine = [c for c in table[1:] if c and c[0] == rid]
        why = why or (None if table and table[0] == STEP_HEADER and all(len(c) == 6 for c in table[1:] if c)
                      else "REQUEST-STEPS.csv does not match REQUEST-STEPS.csv v1")
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        why = why or (None if isinstance(exc, FileNotFoundError) else f"REQUEST-STEPS.csv unreadable ({type(exc).__name__})")
    if why:
        return print(f"record: {why}", file=sys.stderr) or 1
    line, new = [rid, step, when, ev.strip(), sha, today.isoformat()], dict(row)
    for field, value in zip(STEPS[step], (when, ev.strip(), sha)):
        why = why or (f"{field} is already set to a different value" if row[field] and row[field] != value else None)
        new[field] = value
    if not why and any(c[1:5] == line[1:5] for c in mine):
        return print(f"unchanged | {s} | {rid} | {step} | {when}") or 0
    new["status"] = STEP_STATUS.get(step, row["status"])
    bad = []
    check_row(new, s, set(), lambda field, reason: bad.append(f"{field} {reason}"))
    floor = max([day(c[2]) for c in mine if day(c[2])] + [day(row["sent"])])
    why = why or (f"request is {row['status']}; no step can follow" if row["status"] in ("closed", "withdrawn")
                  else f"status regression ({row['status']} to {new['status']})"
                  if ORDER.index(new["status"]) < ORDER.index(row["status"])
                  else f"step {step} is already recorded with different values" if any(c[1] == step for c in mine)
                  else f"order regression ({bad[0]})" if bad
                  else "order regression (--date is before the sent date or an earlier step)" if d < floor else None)
    if why:
        return print(f"record: {why}", file=sys.stderr) or 1
    before = req.read_bytes()
    with open(req, encoding="utf-8-sig", newline="") as fh:
        table = list(csv.reader(fh))
    table = table[:1] + [[new[k] for k in HEADER] if c and c[0].strip() == rid else c for c in table[1:]]
    head = "" if old_log else csv_text([STEP_HEADER])
    try:
        replace(req, csv_text(table))
        try:
            replace(log, head + old_log + ("" if not old_log or old_log.endswith("\n") else "\n") + csv_text([line]))
        except OSError:
            req.write_bytes(before)
            raise
    except OSError as exc:
        return print(f"record: cannot write in agency folder ({type(exc).__name__})", file=sys.stderr) or 1
    return print(f"recorded | {s} | {rid} | {step} | {when}") or 0


USAGE = ("usage: flock_requests.py --check PATH | due PATH [--as-of YYYY-MM-DD] | summary PATH | "
         "draft RUNDIR --requests PATH --from YYYY-MM-DD --to YYYY-MM-DD; "
         "summary PATH [--inventory CSV] adds document counts; record STORE --agency SLUG --request ID "
         "--step receipt|extension|determination|produced|closed --date D --evidence TEXT --sha256 HEX [--today D]")

def main(argv):
    if argv in (["--help"], ["-h"]):
        print(USAGE); return 0  # noqa: E702
    if argv[:1] == ["record"]:
        keys, o = argv[2::2], dict(zip(argv[2::2], argv[3::2]))
        today = day(o.pop("--today", date.today().isoformat()))
        if (len(argv) % 2 == 0 and len(set(keys)) == len(keys) and set(o) == set(FLAGS) and today
                and o["--step"] in STEPS):
            return record(argv[1], o, today)
        return print(USAGE, file=sys.stderr) or 2
    if len(argv) == 8 and argv[0] == "draft" and argv[2::2] == ["--requests", "--from", "--to"]:
        start, end = day(argv[5]), day(argv[7])
        if start and end and start <= end:
            return draft(argv[1], argv[3], argv[5], argv[7])
    as_of = inv = None
    if len(argv) == 4 and argv[0] == "summary" and argv[2] == "--inventory":
        inv, argv = argv[3], argv[:2]
    if len(argv) == 4 and argv[0] == "due" and argv[2] == "--as-of":
        as_of, argv = day(argv[3]), argv[:2]
        if as_of is None:
            return print("due: --as-of must be YYYY-MM-DD", file=sys.stderr) or 2
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
        table, why = inventory(inv) if inv is not None else (None, None)
        if why:
            return print(f"summary: {why}", file=sys.stderr) or 1
        print("\n".join(summary(rows, table)))
        return 0
    try:
        lines = due(rows, as_of)
    except ValueError as exc:
        print(f"due: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
