#!/usr/bin/env python3
"""Flock alerts (BF-df-overdue-alerts): provisional reminder alerts for the organizer, as stdout and flock-alerts-v1.
  reminders --store DIR --date YYYY-MM-DD --receipt-days N [--json-out NEWFILE] [--prev OLD.json]
One alert line per open request whose provisional reminder date is today, passed or within 7 days, and per open
request with no receipt recorded N or more days after it was sent. N has no default: the organizer chooses it.
Phrases come from flock_today.reminders; dates come from the us-ca law package and are not legal due dates.
Nothing is sent, acknowledged or snoozed; an alert clears only when the request row itself changes.
--json-out writes a new 0600 file outside the repository after the whole run succeeded; otherwise nothing is written.
--prev reads an earlier flock-alerts-v1 file (as_of before --date): each alert line ends `| new` or `| still`, one
`alert | cleared | <id> | <kind>` line follows per earlier id absent now, and the summary adds new/cleared/since.
Ids include the kind, so a reminder moving from week to today shows as one new and one cleared alert.
Exit 0 no action alert, 3 at least one action alert, 1 store error or refusal (no alert lines), 2 usage."""
import argparse, datetime, importlib.util, json, os, re, stat, sys
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
PREV_MAX = 2 * 1024 * 1024
SAFE_MIDDLE = re.compile(r"[a-z0-9-]{1,60}:[A-Za-z0-9._:-]{1,80}")


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


def load_prev(value, date):  # ({id: kind}, as_of) from an earlier flock-alerts-v1 file; reasons never echo a value
    path = Path(value)
    if not path.is_absolute():
        raise Refused("prev must be an absolute path")
    if any(os.path.islink(p) for p in [path] + list(path.parents)):
        raise Refused("prev is a symlink")
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise OSError
            raw = fh.read(PREV_MAX + 1)
    except OSError:
        raise Refused("prev is not a regular file") from None
    if len(raw) > PREV_MAX:
        raise Refused("prev larger than 2 MiB")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise Refused("prev not UTF-8 JSON") from None
    if not isinstance(doc, dict) or doc.get("schema") != "flock-alerts-v1":
        raise Refused("prev schema")
    if doc.get("review_required") is not True:
        raise Refused("prev review_required")
    as_of = doc.get("as_of")
    old = requests_tool.day(as_of) if isinstance(as_of, str) else None
    if old is None or old >= datetime.date.fromisoformat(date):
        raise Refused("prev as_of")
    if not isinstance(doc.get("alerts"), list):
        raise Refused("prev alerts")
    kinds = {}
    for n, a in enumerate(doc["alerts"]):
        if not isinstance(a, dict):
            raise Refused("prev alerts[%d]" % n)
        i, kind = a.get("id"), a.get("kind")
        tail = i.rpartition(":")[2] if isinstance(i, str) else None
        if (tail not in SEVERITY or len(i) > 200 or not i.startswith("remind:") or len(i) < len(tail) + 9
                or "|" in i or not i.isprintable()):
            raise Refused("prev alerts[%d].id" % n)
        if kind != tail:
            raise Refused("prev alerts[%d].kind" % n)
        if i in kinds:
            raise Refused("prev alerts[%d].id duplicate" % n)
        kinds[i] = kind
    return kinds, as_of


def label(i, kind):  # an earlier id is echoed only when its slug:request part is plain
    return i if SAFE_MIDDLE.fullmatch(i[len("remind:"):-len(kind) - 1]) else "(unsafe-label)"


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


def line(a, status, suffix=""):
    out = "alert | %s | %s | %s | reminder date %s | %s | law=%s | %s" % (
        a["id"], a["kind"], a["severity"], a["reminder_date"] or "-", a["phrase"], status, TAIL)
    return out + (" | " + DRAFT_NOTE if status != "reviewed" else "") + suffix


def document(alerts, date, status, since=None, cleared=None):
    doc = {"schema": "flock-alerts-v1", "review_required": True, "as_of": date, "law_status": status}
    if status != "reviewed":
        doc["law_note"] = DRAFT_NOTE
    doc["alerts"] = [{k: v for k, v in a.items() if not k.startswith("_")} for a in alerts]
    if since is not None:
        doc["since"], doc["cleared"] = since, cleared
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
        prev, since = load_prev(a.prev, a.date) if a.prev is not None else (None, None)
    except Refused as exc:
        return print("alert | refused | %s" % exc) or 1
    try:
        alerts, opened, waiting = collect(store, a.date, a.receipt_days)
    except today_tool.ERRORS:
        return print("alert | store-error | REQUESTS.csv") or 1
    status = law.package_status("us-ca")[0]
    count = {k: sum(x["kind"] == k for x in alerts) for k in SEVERITY}
    lines, extra, cleared = [line(x, status) for x in alerts], "", None
    if prev is not None:
        for x in alerts:
            x["new"] = x["id"] not in prev
        lines = [line(x, status, " | new" if x["new"] else " | still") for x in alerts]
        now = {x["id"] for x in alerts}
        cleared = [{"id": label(i, prev[i]), "kind": prev[i]} for i in sorted(set(prev) - now)]
        lines += ["alert | cleared | %s | %s" % (c["id"], c["kind"]) for c in cleared]
        extra = " new=%d cleared=%d since=%s" % (sum(x["new"] for x in alerts), len(cleared), since)
    lines.append("alert | open=%d alerts=%d passed=%d today=%d week=%d receipt_unknown=%d waiting=%d law=%s%s | %s" % (
        opened, len(alerts), count["reminder-passed"], count["reminder-today"], count["reminder-week"],
        count["receipt-unknown"], waiting, status, extra,
        "recorded fields only; not a legal status; the canonical ledger wins"))
    if target:
        try:
            write_new(target, document(alerts, a.date, status, since, cleared))
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
    p.add_argument("--prev", metavar="OLD.json", help="earlier flock-alerts-v1 file: mark alerts new/still, list cleared")
    a = parser.parse_args(argv)
    if requests_tool.day(a.date) is None:
        p.error("--date must be YYYY-MM-DD")
    return reminders(a)


if __name__ == "__main__":
    sys.exit(main())
