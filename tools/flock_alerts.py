#!/usr/bin/env python3
"""Flock alerts (BF-df-overdue-alerts): provisional reminder alerts for the organizer, as stdout and flock-alerts-v1.
  reminders --store DIR --date YYYY-MM-DD --receipt-days N [--json-out NEWFILE]
One alert line per open request whose provisional reminder date is today, passed or within 7 days, and per open
request with no receipt recorded N or more days after it was sent. N has no default: the organizer chooses it.
Phrases come from flock_today.reminders; dates come from the us-ca law package and are not legal due dates.
Nothing is sent, acknowledged or snoozed; an alert clears only when the request row itself changes.
--json-out writes a new 0600 file outside the repository after the whole run succeeded; otherwise nothing is written.
Exit 0 no action alert, 3 at least one action alert, 1 store error or refusal (no alert lines), 2 usage."""
import argparse, datetime, importlib.util, json, os, sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent


def _tool(name):  # siblings are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_alerts_" + name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


today_tool = _tool("flock_today")
requests_tool, law = today_tool.requests_tool, today_tool.requests_tool.law
SEVERITY = {"reminder-passed": "action", "reminder-today": "action", "reminder-week": "info",
            "receipt-unknown": "action"}
DRAFT_NOTE = "law package is draft: dates unconfirmed"
TAIL = "provisional; not a legal due date"


class Refused(Exception):
    pass


def receipt_days(value):
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer 1..365") from None
    if not 1 <= n <= 365:
        raise argparse.ArgumentTypeError("must be an integer 1..365")
    return n


def inside_repo(path):
    return path == REPO or REPO in path.parents


def json_target(value):  # absolute, new, outside the repository, parent an existing link-free folder
    path = Path(value)
    if not path.is_absolute():
        raise Refused("json-out must be an absolute path")
    if os.path.lexists(path):
        raise Refused("json-out already exists")
    parent = path.parent
    for p in [parent] + list(parent.parents):
        if os.path.islink(p):
            raise Refused("json-out parent is a symlink")
    if not parent.is_dir():
        raise Refused("json-out parent is not a folder")
    if inside_repo(parent.resolve() / path.name):
        raise Refused("json-out inside repository")
    return path


def phrase_of(kind, text):
    if kind == "receipt-unknown":
        return text.partition("; ")[0]
    return text.partition(" (provisional); ")[0]


def collect(store, date, n):
    """(alerts, open count, waiting count) or raise on any store/load error; no partial list."""
    rows, errs = requests_tool.load(store)
    if errs:
        raise ValueError("REQUESTS.csv rows")
    as_of, alerts, waiting = datetime.date.fromisoformat(date), [], 0
    for rank, kind, when, slug, rid, text in today_tool.reminders(store, date):
        if kind == "receipt-unknown":
            if (as_of - datetime.date.fromisoformat(when)).days < n:
                waiting += 1
                continue
            when = None
        alerts.append({"id": "remind:%s:%s:%s" % (slug, rid, kind), "source": "request-reminders", "kind": kind,
                       "severity": SEVERITY[kind], "agency": slug, "request_id": rid, "reminder_date": when,
                       "phrase": phrase_of(kind, text), "_rank": rank})
    alerts.sort(key=lambda a: (a["_rank"], a["reminder_date"] or "", a["agency"], a["id"]))
    return alerts, sum(r["status"] == "open" for r in rows), waiting


def line(a, status):
    out = "alert | %s | %s | %s | reminder date %s | %s | law=%s | %s" % (
        a["id"], a["kind"], a["severity"], a["reminder_date"] or "-", a["phrase"], status, TAIL)
    return out + (" | " + DRAFT_NOTE if status != "reviewed" else "")


def document(alerts, date, status):
    doc = {"schema": "flock-alerts-v1", "review_required": True, "as_of": date, "law_status": status}
    if status != "reviewed":
        doc["law_note"] = DRAFT_NOTE
    doc["alerts"] = [{k: v for k, v in a.items() if not k.startswith("_")} for a in alerts]
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def write_new(path, data):  # O_EXCL|O_NOFOLLOW, 0600; a failed write leaves no file behind
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        os.unlink(path)
        raise


def reminders(a):
    try:
        store = Path(a.store).resolve()
        if inside_repo(store):
            raise Refused("store inside repository")
        target = json_target(a.json_out) if a.json_out else None
    except Refused as exc:
        return print("alert | refused | %s" % exc) or 1
    try:
        alerts, opened, waiting = collect(store, a.date, a.receipt_days)
    except today_tool.ERRORS:
        return print("alert | store-error | REQUESTS.csv") or 1
    status = law.package_status("us-ca")[0]
    count = {k: sum(x["kind"] == k for x in alerts) for k in SEVERITY}
    lines = [line(x, status) for x in alerts]
    lines.append("alert | open=%d alerts=%d passed=%d today=%d week=%d receipt_unknown=%d waiting=%d law=%s | %s" % (
        opened, len(alerts), count["reminder-passed"], count["reminder-today"], count["reminder-week"],
        count["receipt-unknown"], waiting, status, "recorded fields only; not a legal status; the canonical ledger wins"))
    if target:
        try:
            write_new(target, document(alerts, a.date, status))
        except OSError as exc:
            return print("alert | refused | json-out not written (%s)" % type(exc).__name__) or 1
    print(*lines, sep="\n")
    return 3 if any(x["severity"] == "action" for x in alerts) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True, metavar="reminders")
    p = verbs.add_parser("reminders", help="provisional reminder and receipt-unknown alerts for one date")
    p.add_argument("--store", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--receipt-days", required=True, type=receipt_days, help="1..365, required, no default")
    p.add_argument("--json-out", metavar="NEWFILE")
    a = parser.parse_args(argv)
    if requests_tool.day(a.date) is None:
        p.error("--date must be YYYY-MM-DD")
    return reminders(a)


if __name__ == "__main__":
    sys.exit(main())
