#!/usr/bin/env python3
"""Private CPRA request reminder list (REQUESTS.csv v1): --check, due and summary. Prints only.
draft RUNDIR writes FOLLOWUP-DRAFT.md; RUNDIR must lie outside the repository."""
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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


def read_log(path):  # (text or None when missing, csv table, refusal); O_NOFOLLOW: a swapped-in link is refused
    try:
        with open(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8", newline="") as fh:
            return (text := fh.read()), list(csv.reader(io.StringIO(text))), None
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return None, [], None if isinstance(exc, FileNotFoundError) else f"{path.name} unreadable ({type(exc).__name__})"


def locate(store, s, rid, when, ev, sha, today, name):
    """Shared record/fees refusals: (refusal or None, agency folder, rows, row, date, folder / name)."""
    root, d = Path(store).resolve(), day(when)
    folder = root / "agencies" / s
    req, log = folder / "REQUESTS.csv", folder / name
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
    return why, folder, rows, row, d, log


def record(store, o, today):
    """Set one dated, sourced step on one REQUESTS.csv row and append it to REQUEST-STEPS.csv; refusals write nothing."""
    s, rid, step, when, ev, sha = (o[k] for k in FLAGS)
    why, folder, _, row, d, log = locate(store, s, rid, when, ev, sha, today, "REQUEST-STEPS.csv")
    req, (old_log, table, bad) = folder / "REQUESTS.csv", read_log(log)
    mine = [c for c in table[1:] if c and c[0] == rid]
    fine = old_log is None or table[:1] == [STEP_HEADER] and all(len(c) == 6 for c in table[1:] if c)
    why = why or bad or (None if fine else "REQUEST-STEPS.csv does not match REQUEST-STEPS.csv v1")
    old_log = old_log or ""
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


FEE_FLAGS = ("--agency", "--request", "--kind", "--amount-cents", "--date", "--evidence", "--sha256")
FEE_HEADER = ["request_id", "kind", "amount_cents", "date", "evidence", "source_sha256", "recorded_at"]
KINDS, AMOUNT = ("estimate", "invoice", "paid"), re.compile(r"0|[1-9][0-9]{0,6}|10000000")  # no decimals/sign/text


def fee_errors(table, known):
    """Named FEES.csv v1 problems (header, columns, request, kind, amount, dates, paid only after an earlier invoice)."""
    if table[:1] != [FEE_HEADER]:
        return ["FEES.csv row 1: header does not match FEES.csv v1"]
    errs, invoiced = [], {}
    for n, c in ((n, c) for n, c in enumerate(table[1:], 2) if c):
        d = day(c[3]) if len(c) == 7 else None
        why = (f"columns expected 7, found {len(c)}" if len(c) != 7
               else f"request {c[0]} not in REQUESTS.csv" if c[0] not in known
               else "kind must be estimate, invoice or paid" if c[1] not in KINDS
               else "amount_cents must be an integer 0-10000000" if not AMOUNT.fullmatch(c[2])
               else "date must be YYYY-MM-DD, not before sent or after recorded_at"
               if not d or not day(c[6]) or d > day(c[6]) or d < day(known[c[0]]["sent"])
               else "paid has no earlier invoice row" if c[1] == "paid" and invoiced.get(c[0], date.max) > d
               else None)
        if why:
            errs.append(f"FEES.csv row {n}: {why}")
        elif c[1] == "invoice":
            invoiced[c[0]] = min(invoiced.get(c[0], d), d)
    return errs


def fee_record(store, o, today):
    """Append one sourced fee row (estimate, invoice or paid) for a known request to private FEES.csv; never pays."""
    s, rid, kind, amount, when, ev, sha = (o[k] for k in FEE_FLAGS)
    why, folder, rows, row, d, path = locate(store, s, rid, when, ev, sha, today, "FEES.csv")
    (old, table, bad), line = read_log(path), [rid, kind, amount, when, ev.strip(), sha, today.isoformat()]
    mine = [c for c in table[1:] if c and c[0] == rid]
    errs = fee_errors(table, {r["request_id"]: r for r in rows}) if old is not None and not why else []
    why = why or (f"request {rid} is withdrawn" if row["status"] == "withdrawn"
                  else "--amount-cents must be an integer 0-10000000" if not AMOUNT.fullmatch(amount)
                  else "--date is before the request's sent date" if d < day(row["sent"])
                  else bad or f"FEES.csv fails fees check ({errs[0]})" if bad or errs
                  else f"paid needs an earlier invoice row for request {rid}"
                  if kind == "paid" and not any(c[1] == "invoice" and day(c[3]) <= d for c in mine) else None)
    if why:
        return print(f"fees: {why}", file=sys.stderr) or 1
    if any(c[1:4] + c[5:6] == line[1:4] + line[5:6] for c in mine):  # same kind, amount, date and source
        return print(f"unchanged | {s} | {rid} | {kind} | {amount} | {when}") or 0
    try:
        replace(path, (old or csv_text([FEE_HEADER])) + ("" if not old or old.endswith("\n") else "\n") + csv_text([line]))
    except OSError as exc:
        return print(f"fees: cannot write in agency folder ({type(exc).__name__})", file=sys.stderr) or 1
    return print(f"fee | {s} | {rid} | {kind} | {amount} | {when}") or 0


def fee_check(store, cap):
    """Read-only; over cap = latest invoice (else estimate) > cap on an open/determined request (wins over paid over)."""
    rows, errs = load(store)
    lines, over = [], 0
    for f in sorted(Path(store).glob("agencies/*/FEES.csv")) if not errs else ():
        s, (_, table, bad) = f.parent.name, read_log(f)
        known = {r["request_id"]: r for r in rows if slug(r["agency"]) == s}
        found = [bad] if bad else fee_errors(table, known)
        errs += [f"{s}: {e}" for e in found]
        for rid in sorted({c[0] for c in table[1:] if c} if not found else ()):
            mine = sorted((c for c in table[1:] if c and c[0] == rid), key=lambda c: c[3])  # stable: ties keep file order
            by = {k: [int(c[2]) for c in mine if c[1] == k] for k in KINDS}  # [-1] is the latest
            top, paid = (by["invoice"] or by["estimate"])[-1:], sum(by["paid"])
            hit = known[rid]["status"] in ("open", "determined") and any(v > cap for v in top)
            over += hit
            word = "over cap" if hit else "paid over invoice" if by["invoice"] and paid > by["invoice"][-1] else "ok"
            shown = " ".join(f"{k}={(by[k] or ['-'])[-1]}" for k in KINDS[:2])
            lines.append(f"fees | {s} | {rid} | {shown} paid={paid} | {word}")
    if errs:
        return print("\n".join(errs), file=sys.stderr) or 1
    lines.append(f"fees | requests={len(lines)} over_cap={over} cap_cents={cap} | not a payment decision; G decides any spending")
    return print("\n".join(lines)) or (3 if over else 0)


def _tool(name):  # siblings are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_requests_" + name, Path(__file__).resolve().parent / (name + ".py"))
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module


def _stage():
    """flock_email.stage with flock_approve's signature check: only a verified approval reaches sent recorded."""
    email, check = _tool("flock_email"), _tool("flock_approve").verified
    return lambda run: email.stage(run, lambda r, rec: check(r, rec, email))


def sent_runs(store, rows):
    """Errors and a counts tail: each agencies/<slug>/runs/<date>/ whose email stage (flock_email.stage) is sent recorded
    needs a sent date on its Sent by line and a request of that agency with that sent date. Reads only; symlinked
    agency, runs or run folders are skipped, never entered. Lines carry slug, run date and sent date, no draft text.
    A sent run whose approval signature does not verify is an error, like sent without approval."""
    stage, errs, sent, matched = _stage(), [], 0, 0
    base = Path(store) / "agencies"
    try:
        folders = sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink()) if not base.is_symlink() else []
    except OSError:
        folders = []
    for folder in folders:
        runs = folder / "runs"
        try:
            dates = [] if runs.is_symlink() or not runs.is_dir() else sorted(
                p for p in runs.iterdir() if day(p.name) and p.is_dir() and not p.is_symlink())
        except OSError:
            errs.append(f"{folder.name}: runs folder unreadable")
            dates = []
        for run in dates:
            got, pre = stage(run), f"{folder.name}: run {run.name}: "
            if got == "inconsistent: sent without approval":
                errs.append(pre + "sent without approval")
            if got == "sent unverified":
                errs.append(pre + "sent, but the approval signature does not verify")
            if got != "sent recorded":
                continue
            sent += 1
            _, when, why = sent_on(run)
            why = (why if why
                   else None if any(slug(r["agency"]) == folder.name and r["sent"] == str(when) for r in rows)
                   else f"no request with sent {when}")
            matched += why is None
            errs += [pre + why] if why else []
    return errs, f" sent_runs={sent} matched={matched}"


def sent_on(run):
    """(EMAIL-DRAFT.md bytes, sent date, refusal): --check and open's one rule, the first date on Sent by, not before the run."""
    try:  # O_NOFOLLOW: a link swapped in after stage() is still refused
        with open(os.open(run / "EMAIL-DRAFT.md", os.O_RDONLY | os.O_NOFOLLOW), "rb") as fh:
            data = fh.read()
        value = next(ln for ln in data.decode("utf-8").splitlines() if ln.startswith("Sent by:"))
    except (OSError, UnicodeDecodeError, StopIteration):
        return None, None, "EMAIL-DRAFT.md unreadable"
    token = re.search(r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])", value)
    when = day(token.group()) if token else None
    return data, when, ("sent date missing on the Sent by line" if when is None
                        else f"sent date {when} is before the run date" if when < day(run.name) else None)


def appended(old, head, line):  # old text (or a new file's header) + one row, never glued onto an unterminated last row
    return (old or csv_text([head])) + ("" if not old or old.endswith("\n") else "\n") + csv_text([line])


def open_run(rundir, rid, today):
    """An open REQUESTS row + an `opened` REQUEST-STEPS row from a sent recorded run; prints ids and dates, never draft text."""
    given = Path(os.path.abspath(rundir))
    if not (len(given.parents) >= 4 and given.parent.name == "runs" and given.parents[2].name == "agencies" and day(given.name)):
        return print("open: RUNDIR must be <store>/agencies/<slug>/runs/<YYYY-MM-DD>", file=sys.stderr) or 1
    root, s = given.parents[3].resolve(), given.parents[1].name
    run, req, log = (folder := root / "agencies" / s) / "runs" / given.name, folder / "REQUESTS.csv", folder / "REQUEST-STEPS.csv"
    link = next((p.name for p in (root / "agencies", folder, folder / "runs", run, req, log) if p.is_symlink()), None)
    got = None if link or not run.is_dir() else _stage()(run)
    data, when, why = sent_on(run) if got == "sent recorded" else (None, None, None)
    why = ("store is inside the repository" if root == REPO or REPO in root.parents
           else f"{link} must not be a symlink" if link
           else "agency folder name must be a slug" if slug(s) != s
           else "RUNDIR missing or not a folder" if not run.is_dir()
           else f"email stage is {got or 'none'}, not sent recorded" if got != "sent recorded"
           else why or (f"sent date {when} is after --today" if when > today else None))
    rows, errs = load(req, s) if not why and req.exists() else ([], [])
    old_log, table, bad = read_log(log) if not why else (None, [], None)
    first = next((c[0] for c in table[1:] if len(c) == 6 and c[1:2] + c[3:4] == ["opened", f"email run {run.name}"]), None)
    fine = old_log is None or table[:1] == [STEP_HEADER] and all(len(c) == 6 for c in table[1:] if c)
    why = why or (f"REQUESTS.csv fails --check ({errs[0]})" if errs else bad
                  or (None if fine else "REQUEST-STEPS.csv does not match REQUEST-STEPS.csv v1")
                  or (f"run already opened as {first}" if first else None)
                  or (f"request {rid} already exists in {s}/REQUESTS.csv" if any(r["request_id"] == rid for r in rows)
                      else None))
    old_req, _, bad = read_log(req) if not why else (None, [], None)
    if why or bad:
        return print(f"open: {why or bad}", file=sys.stderr) or 1
    try:
        replace(req, appended(old_req, HEADER, [rid, rows[0]["agency"] if rows else s, str(when), "", "", "", "", "open", ""]))
        try:
            replace(log, appended(old_log, STEP_HEADER, [rid, "opened", str(when), f"email run {run.name}",
                                                         hashlib.sha256(data).hexdigest(), today.isoformat()]))
        except OSError:  # roll the REQUESTS row back: restore the old bytes, or remove the file this call created
            replace(req, old_req) if old_req is not None else os.unlink(req)
            raise
    except OSError as exc:
        return print(f"open: cannot write in agency folder ({type(exc).__name__})", file=sys.stderr) or 1
    return print(f"opened | {s} | {rid} | sent {when} | run {run.name}") or 0


def read_report(path):
    """(correspondence report, None) or (None, reason). The records owner's export is only read; reasons never echo it."""
    p, cap = Path(path), 64 << 20
    why = ("must not be a symlink" if p.is_symlink()
           else "is inside the repository" if (real := p.resolve()) == REPO or REPO in real.parents
           else "must be a regular file of at most 64 MiB" if not p.is_file() or p.stat().st_size > cap else None)
    if why:
        return None, why
    try:  # O_NOFOLLOW/O_NONBLOCK: a link or FIFO swapped in after the checks is refused and cannot hang the read
        with open(os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as fh:
            data = json.loads(fh.read(cap + 1).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        return None, f"unreadable or not JSON ({type(exc).__name__})"
    if not isinstance(data, dict) or data.get("schema") != "correspondence-report-v1":
        return None, "schema is not correspondence-report-v1"
    ok = all(isinstance(data.get(k), list) and all(isinstance(x, dict) for x in data[k]) for k in ("agencies", "requests"))
    return (data, None) if ok else (None, "agencies and requests must be lists of objects")


def report_day(v):
    """'' when empty, a date (an aware timestamp's America/Los_Angeles day), or None when unresolved (naive or junk)."""
    try:  # non-text, or an aware timestamp overflowing at the calendar edge, is unresolved too
        t = datetime.fromisoformat(v) if isinstance(v, str) and v and not day(v) else None
        return "" if v in (None, "") else day(v) or (t.astimezone(ZoneInfo("America/Los_Angeles")).date() if t and t.tzinfo else None)
    except (TypeError, ValueError, OverflowError):
        return None


def reconcile(rows, data):
    """(lines, exit code): each REQUESTS row against the request with that external_ref in the report agency of its slug.
    Prints REQUESTS ids, slugs and dates only, never report ids, refs or text; an ambiguous agency's rows are unmatched."""
    ids, groups, lines, n = {}, {}, [], dict.fromkeys(("matched", "disagreements", "unmatched", "report_only", "unresolved"), 0)
    for a in (a for a in data["agencies"] if isinstance(a.get("name"), str) and isinstance(a.get("id"), (str, int))):
        ids.setdefault(slug(a["name"]), set()).add(a["id"])
    for r in rows:
        groups.setdefault(slug(r["agency"]), []).append(r)
    for key in sorted(groups):
        mine, theirs, refs = sorted(groups[key], key=lambda r: r["request_id"]), ids.get(key, set()), {}
        if len(theirs) > 1:
            lines.append(f"reconcile | {key} | report agency ambiguous for {key}")
            n["unmatched"] += len(mine)
            continue
        for q in (q for q in data["requests"] if isinstance(q.get("agency_id"), (str, int)) and q["agency_id"] in theirs):
            refs.setdefault(q["external_ref"].strip() if isinstance(q.get("external_ref"), str) else None, []).append(q)
        for r in mine:
            got, pre = refs.get(r["request_id"], []), f"reconcile | {key} | {r['request_id']} | "
            if len(got) != 1:
                lines.append(pre + ("report request ambiguous" if got else "not in report"))
                n["unmatched"] += 1
                continue
            n["matched"] += 1
            for label, mine_v, field in (("sent", r["sent"], "submitted_at"),
                                         ("determination", r["determination_date"], "determination_at")):
                other = report_day(got[0].get(field))
                if other is None or str(other) != mine_v:
                    n["unresolved" if other is None else "disagreements"] += 1
                    lines.append(pre + f"{label} {mine_v or '-'} vs " + ("submitted " if label == "sent" else "")
                                 + ("unresolved" if other is None else str(other) or "-"))
            st = got[0].get("status")
            if st not in ORDER:
                shown = st if isinstance(st, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", st) else "other"
                lines.append(pre + f"status not comparable ({shown})")
            elif st != r["status"]:
                lines.append(pre + f"status {r['status']} vs {st}")
                n["disagreements"] += 1
        extra = sum(len(qs) for ref, qs in refs.items() if ref not in {r["request_id"] for r in mine})
        n["report_only"] += extra
        lines += [f"reconcile | {key} | report-only requests={extra}"] if extra else []
    as_of, snap = data.get("as_of"), str(data.get("snapshot_sha256"))
    as_of = as_of if isinstance(as_of, str) and report_day(as_of) else "unresolved"
    lines.append(" ".join(f"{k}={v}" for k, v in [("rows", len(rows))] + list(n.items())) + f" | report as_of {as_of}"
                 + f" snapshot {snap[:12] if re.fullmatch(r'[0-9a-f]{64}', snap) else '-'}")
    return lines, 3 if n["disagreements"] + n["unmatched"] + n["report_only"] else 0


USAGE = ("usage: flock_requests.py --check PATH | due PATH [--as-of YYYY-MM-DD] | summary PATH | "
         "draft RUNDIR --requests PATH --from YYYY-MM-DD --to YYYY-MM-DD; "
         "summary PATH [--inventory CSV] adds document counts; record STORE --agency SLUG --request ID "
         "--step receipt|extension|determination|produced|closed --date D --evidence TEXT --sha256 HEX [--today D]; "
         "fees record STORE --agency SLUG --request ID --kind estimate|invoice|paid --amount-cents N --date D "
         "--evidence TEXT --sha256 HEX [--today D]; fees check STORE --cap-cents N (required; there is no default cap); "
         "reconcile STORE --report REPORT.json; open --from-email RUNDIR --request-id ID [--today D]")

def main(argv):
    if argv in (["--help"], ["-h"]):
        print(USAGE); return 0  # noqa: E702
    if argv[:1] == ["open"]:  # ID is the organizer's own label; the tool never invents one
        keys, o = argv[1::2], dict(zip(argv[1::2], argv[2::2]))
        today = day(o.pop("--today", date.today().isoformat()))
        ok = len(argv) % 2 and len(set(keys)) == len(keys) and today and set(o) == {"--from-email", "--request-id"}
        ok = ok and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", o["--request-id"])
        return open_run(o["--from-email"], o["--request-id"], today) if ok else print(USAGE, file=sys.stderr) or 2
    if len(argv) == 4 and argv[0] == "reconcile" and argv[2] == "--report":
        data, why = read_report(argv[3])
        rows, errs = load(argv[1]) if not why else ([], [f"report: {why}"])
        lines, code = (errs, 1) if errs else reconcile(rows, data)
        return print("\n".join(lines), file=sys.stderr if errs else sys.stdout) or code
    if argv[:2] == ["fees", "check"]:  # --cap-cents is required: the organizer sets the cap, there is no default
        ok = len(argv) == 5 and argv[3] == "--cap-cents" and re.fullmatch(r"[0-9]+", argv[4])
        return fee_check(argv[2], int(argv[4])) if ok else print(USAGE, file=sys.stderr) or 2
    fee = argv[:2] == ["fees", "record"]
    if argv[:1] == ["record"] or fee:
        argv, flags, choice, verb = (argv[1:], FEE_FLAGS, KINDS, fee_record) if fee else (argv, FLAGS, STEPS, record)
        keys, o = argv[2::2], dict(zip(argv[2::2], argv[3::2]))
        today = day(o.pop("--today", date.today().isoformat()))
        if (len(argv) % 2 == 0 and len(set(keys)) == len(keys) and set(o) == set(flags) and today
                and o[flags[2]] in choice):
            return verb(argv[1], o, today)
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
        more, tail = sent_runs(argv[1], rows) if Path(argv[1]).is_dir() else ([], "")
        errs += more
        print("\n".join(errs + [f"requests={len(rows)} errors={len(errs)}{tail}"]))
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
