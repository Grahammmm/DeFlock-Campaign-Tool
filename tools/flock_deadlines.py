#!/usr/bin/env python3
"""Flock deadlines (BF-df-deadline-engine): open requests past a dated law-package rule, each with a follow-up draft filed
`pending`. Drafts only: nothing here sends or approves. docs/APPROVALS.md and skills/flock-followup describe the flow.
  flock_deadlines.py due --store STORE --date YYYY-MM-DD                 read-only: prints the rows and the status line
  flock_deadlines.py run --store STORE --date YYYY-MM-DD [--out NEWDIR]  drafts, files `pending`, writes one receipt
Clock rules: package rules whose rule_id carries `-determination`, in force on the REQUESTS.csv `sent` date; days from
records_law (+ extension days for flock-state open-extension-noticed) via campaign_tool.law.deadline. Due = the day after
the reminder date. Missing day count/effective_from: `rule-incomplete`; no sent date: `clock-unknown`. One draft per
(request, rule, reminder date) via flock_requests.draft; an edited draft is kept, fresh text goes to `<cycle>.suggested/`.
QUEUE.csv and CURSOR are written after every draft. Until the package is `reviewed` every date is a provisional reminder,
not a legal due date. Receipt flock-deadlines-v1 (ids, dates, enums, counts): STORE/deadlines/RECEIPT.json or
NEWDIR/deadlines.json. Exit 0 clean, 1 any source failed (rows still written), 2 usage."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(REPO))
from campaign_tool import law  # noqa: E402


def _tool(name):  # siblings are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_deadlines_" + name, TOOLS / (name + ".py"))
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module


state_tool, privacy = _tool("flock_state"), _tool("flock_privacy")
req = state_tool.requests_tool  # flock_requests: load, draft, day, read_log, replace, csv_text, slug
OPEN = ("open-receipt-unknown", "open-receipt-recorded", "open-extension-noticed")
QUEUE_HEADER = ["key", "agency", "request_id", "rule_id", "cycle", "draft", "sha256", "state"]
PROVISIONAL = "provisional reminder; not a legal due date"
REVIEWED = "reminder date from the reviewed law package"
COUNTS = ("due", "drafted", "already", "edited-kept", "clock-unknown", "rule-incomplete", "inside")
USAGE = ("usage: flock_deadlines.py due --store STORE --date YYYY-MM-DD | "
         "run --store STORE --date YYYY-MM-DD [--out NEWDIR]  (drafts only; never sends or approves)")


class Refuse(Exception): pass


def label(pkg): return REVIEWED if pkg.get("status") == "reviewed" else PROVISIONAL  # the field, at run time


def clock_rules(pkg, sent, ext):
    """[(rule_id, rule_days or None)] for rules in force on `sent`; None marks a rule-incomplete one."""
    lw, out = pkg.get("records_law") or {}, []
    days = lw.get("determination_days")
    days = days + lw.get("determination_extension_days", 0) if ext and isinstance(days, int) else days
    for rule in (r for r in pkg.get("rules", ()) if "-determination" in str(r.get("rule_id", ""))):
        start, end = req.day(str(rule.get("effective_from") or "")), rule.get("effective_to")
        if start is None or not isinstance(days, int) or isinstance(days, bool) or lw.get("day_type") not in ("calendar", "business"):
            out.append((rule["rule_id"], None))
        elif sent and start <= sent and (not end or (req.day(str(end)) or date.max) >= sent):
            out.append((rule["rule_id"], days))
    return out


def requests(store):
    """([(slug, row, state)], source errors) from every agency folder; state is flock_state's field-only state."""
    out, errs = [], []
    for folder in sorted(p for p in (store / "agencies").iterdir() if p.is_dir() and not p.is_symlink()):
        try:
            states = state_tool.folder_requests(folder)
        except state_tool.Refuse as exc:
            errs.append(str(exc))
            continue
        rows, _ = req.load(folder / "REQUESTS.csv", folder.name) if (folder / "REQUESTS.csv").exists() else ([], [])
        for r, (shown, state, basis) in zip(rows, states):
            if state == "record-error" and basis != ["sent"]:
                errs.append(f"{state_tool.label(folder.name)}: request {shown} has record errors ({','.join(basis)})")
            elif shown != r["request_id"]:
                errs.append(f"{state_tool.label(folder.name)}: request (unsafe-label) skipped")
            else:
                out.append((folder.name, r, "clock-unknown" if state == "record-error" else state))
    return out, errs


def compose(store, slug, r, rule_id, sent, when, as_of, rule_days, pkg):
    """The follow-up text through flock_requests.draft (a staged one-row REQUESTS.csv), plus internal deadline lines."""
    rid = r["request_id"]
    with tempfile.TemporaryDirectory(prefix="flock-deadlines-") as tmp:  # private staging; removed on exit
        run = Path(tmp) / slug / "runs" / "draft"
        run.mkdir(parents=True)
        (Path(tmp) / "REQUESTS.csv").write_text(req.csv_text([req.HEADER, [r[k] if k != "agency" else slug
                                                                            for k in req.HEADER]]), encoding="utf-8")
        (run / "QUESTIONS.md").write_text(f"{req.QLINE1}\n- Q {rid} | records request {rid} | prior - | Existing records "
                                          f"showing the determination on records request {rid} sent {sent}, including any "
                                          "extension notice\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err := io.StringIO()):
            code = req.draft(run, Path(tmp) / "REQUESTS.csv", sent.isoformat(), as_of.isoformat())
        if code:
            raise Refuse(f"{slug}: request {rid}: draft refused ({err.getvalue().strip().splitlines()[-1]})")
        text = (run / "FOLLOWUP-DRAFT.md").read_text(encoding="utf-8")
    return text + (f"Deadline (internal, not part of the request): rule {rule_id} | sent {sent} | reminder date {when} | "
                   f"days_elapsed={(as_of - sent).days} rule_days={rule_days} | {label(pkg)}\n"
                   "Approval (internal): pending human approval in the deadline queue; this tool never sends.\n")


def sha(data): return hashlib.sha256(data).hexdigest()


def read_queue(base):
    text, table, why = req.read_log(base / "QUEUE.csv")
    if why or (text is not None and (table[:1] != [QUEUE_HEADER] or any(len(c) != 8 for c in table[1:] if c))):
        raise Refuse(why or "QUEUE.csv does not match its header")
    return {c[0]: dict(zip(QUEUE_HEADER, c)) for c in table[1:] if c}


def read_file(path):  # bytes or None; a link is never followed
    try:
        with open(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except OSError:
        return b""  # a link or unreadable file counts as edited: it is never replaced


def _write_new(path, text):  # 0600, O_EXCL|O_NOFOLLOW: never replaces a file
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w", encoding="utf-8") as fh:
        fh.write(text)


def plan(store, as_of, pkg, read_only=False):
    """(rows, writes [(path, text)], queue entries, source errors); reads only."""
    base, (reqs, errs), rows, writes = store / "deadlines", requests(store), [], []
    queue = read_queue(base)
    for slug, r, state in reqs:
        sent = req.day(r["sent"])
        rules = clock_rules(pkg, sent, state == "open-extension-noticed") if state != "clock-unknown" else []
        if state not in OPEN and state != "clock-unknown":
            continue
        cell = {"request_id": r["request_id"], "agency": slug, "sent_date": sent and sent.isoformat(), "days_elapsed":
                sent and (as_of - sent).days, "draft_path_relative": None, "approval_state": None}
        if state == "clock-unknown":
            rows.append(dict(cell, rule_id=None, rule_days=None, state="clock-unknown", reminder_date=None))
        for rule_id, rule_days in rules:
            row = dict(cell, rule_id=rule_id, rule_days=rule_days, reminder_date=None, state="rule-incomplete")
            rows.append(row)
            if rule_days is None:
                continue
            when = law.deadline({"records_law": dict(pkg["records_law"], determination_days=rule_days,
                                                     determination_extension_days=0)}, sent)
            row.update(reminder_date=when.isoformat(), state="inside")
            if as_of <= when:
                continue
            key = f"{slug}/{r['request_id']}/{rule_id}/{when}"
            rel = Path("deadlines", "drafts", slug, r["request_id"], rule_id, when.isoformat(), "FOLLOWUP-DRAFT.md")
            text = compose(store, slug, r, rule_id, sent, when, as_of, rule_days, pkg)
            have, entry = read_file(store / rel), queue.get(key)
            if have is None:
                writes.append((store / rel, text))
                queue[key] = entry = dict(zip(QUEUE_HEADER, [key, slug, r["request_id"], rule_id, when.isoformat(),
                                                             rel.as_posix(), sha(text.encode()), "pending"]))
                row["state"] = "due" if read_only else "drafted"
            elif entry and sha(have) == entry["sha256"]:
                row["state"] = "already"
            else:  # edited by the organizer (or not ours): kept; the fresh draft goes to the .suggested sibling
                row["state"] = "edited-kept"
                sug = store / rel.parent.with_name(when.isoformat() + ".suggested") / rel.name
                writes += [] if read_file(sug) == text.encode() else [(sug, text)]
            row.update(draft_path_relative=rel.as_posix(), approval_state=entry and entry["state"])
    return rows, writes, queue, errs


def apply(store, as_of, writes, queue):
    """Drafts first, then QUEUE.csv, then CURSOR; any failure removes the files this call created and changes no queue."""
    base, made = store / "deadlines", []
    try:
        for path, text in writes:
            if path.name == "FOLLOWUP-DRAFT.md" and path.parent.name.endswith(".suggested") and path.exists():
                req.replace(path, text)
                continue
            _write_new(path, text)
            made.append(path)
        rows = [QUEUE_HEADER] + [[e[k] for k in QUEUE_HEADER] for _, e in sorted(queue.items())]
        req.replace(base / "QUEUE.csv", req.csv_text(rows))
        req.replace(base / "CURSOR", as_of.isoformat() + "\n")
    except OSError as exc:
        for path in made:
            os.unlink(path)
        raise Refuse(f"cannot write the deadline queue ({type(exc).__name__})") from None


def check_out(out, store):
    p = Path(os.path.normpath(out))
    real = p.parent.resolve() / p.name if p.is_absolute() else None
    return ("--out must be absolute" if real is None
            else "--out already exists" if os.path.lexists(p)
            else "--out parent folder is missing or a link" if not p.parent.is_dir() or p.parent.resolve() != p.parent
            else "--out must be outside the repository" if real == REPO or REPO in real.parents
            else "--out must be outside STORE/agencies" if (store / "agencies") in (real, *real.parents) else None)


def receipt(rows, as_of, pkg, errs):
    keys = ("request_id", "agency", "rule_id", "sent_date", "days_elapsed", "rule_days", "state", "draft_path_relative",
            "approval_state")
    totals = {k: sum(r["state"] == k for r in rows) for k in COUNTS if k != "due"}
    totals["due"] = sum(r["state"] in ("due", "drafted", "already", "edited-kept") for r in rows)
    return {"schema": "flock-deadlines-v1", "review_required": True, "as_of": as_of.isoformat(),
            "law_package_status": pkg.get("status", "missing"), "rows": [{k: r[k] for k in keys} for r in rows],
            "totals": totals, "source_errors": errs}


def lines(doc, pkg, read_only, rows):
    out = [f"deadline | {r['agency']} | {r['request_id']} | {r['rule_id'] or '-'} | sent {r['sent_date'] or '-'} | "
           f"reminder date {r['reminder_date'] or '-'} | days_elapsed={'-' if r['days_elapsed'] is None else r['days_elapsed']} "
           f"rule_days={r['rule_days'] or '-'} | {r['state']} | {label(pkg)}" for r in rows]
    t = doc["totals"]
    tail = "read-only" if read_only else "pending human approval"
    out.append(f"deadlines | due={t['due']} drafted={t['drafted']} already={t['already']} edited-kept={t['edited-kept']} "
               f"clock-unknown={t['clock-unknown']} | {tail}; " + ("provisional" if label(pkg) == PROVISIONAL else "reviewed package"))
    return out


def notes(texts):  # alert-only: the shared legal-claim guard and privacy patterns name a text, never fail closed
    return [f"review-required: generated text {n}: {','.join(f)}" for n, t in enumerate(texts, 1)
            if (f := privacy.legal_claims(t) + privacy.categories(t))]


def engine(store, as_of, pkg, read_only=False, out=None):
    """(printed lines, exit code); refusals raise Refuse before anything is written."""
    store = Path(store).resolve()
    if store == REPO or REPO in store.parents or not (store / "agencies").is_dir() or (store / "agencies").is_symlink():
        raise Refuse("store must be a folder with an agencies folder, outside the repository")
    base = store / "deadlines"
    if base.is_symlink() or any((base / n).is_symlink() for n in ("QUEUE.csv", "CURSOR", "RECEIPT.json", "drafts")):
        raise Refuse("deadlines folder and its files must not be links")
    why = check_out(out, store) if out else None
    if why:
        raise Refuse(why)
    rows, writes, queue, errs = plan(store, as_of, pkg, read_only)
    doc = receipt(rows, as_of, pkg, errs)
    printed = lines(doc, pkg, read_only, rows)
    for n in notes([t for _, t in writes] + printed):
        print(n, file=sys.stderr)
    if not read_only:
        base.mkdir(mode=0o700, exist_ok=True)
        apply(store, as_of, writes, queue)
        data = json.dumps(doc, indent=1, sort_keys=True) + "\n"
        if out:
            Path(os.path.normpath(out)).mkdir(mode=0o700)
            _write_new(Path(os.path.normpath(out)) / "deadlines.json", data)
        else:
            req.replace(base / "RECEIPT.json", data)
    return printed + [f"source error | {e}" for e in errs], 1 if errs else 0


def main(argv):
    if argv in (["--help"], ["-h"]):
        return print(__doc__ + "\n" + USAGE) or 0
    o = dict(zip(argv[1::2], argv[2::2]))
    as_of = req.day(o.get("--date", ""))
    allowed = {"--store", "--date"} | ({"--out"} if argv[:1] == ["run"] else set())
    if not (argv[:1] in (["due"], ["run"]) and len(argv) % 2 and 2 * len(o) == len(argv) - 1 and set(o) <= allowed
            and {"--store", "--date"} <= set(o) and as_of):
        return print(USAGE, file=sys.stderr) or 2
    status, why = law.package_status("us-ca")
    pkg = law.load_package("us-ca") if why is None and status != "missing" else {"status": "missing", "rules": []}
    try:
        printed, code = engine(o["--store"], as_of, pkg, argv[0] == "due", o.get("--out"))
    except (Refuse, OSError) as exc:
        return print(f"deadlines: {exc if isinstance(exc, Refuse) else 'store unreadable (' + type(exc).__name__ + ')'}",
                     file=sys.stderr) or 1
    print("\n".join(printed))
    return 1 if code or why or status == "missing" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
