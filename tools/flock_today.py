#!/usr/bin/env python3
"""Flock today (NB-DF-11): one ranked, read-only organizer to-do list for one date.
  --store DIR --watchlist CSV --roster CSV --date YYYY-MM-DD [--findings SLUG=CSV ...]
Ranks: 1 reminder-passed/reminder-today, 2 reminder-week (1-7 days out), 3 stale-cited (needs --findings for that
slug), 4 lead-new (LEADS.csv state new), 5 review-wait (sweep stage ready-verify, verify-open, compare-open,
family-refused), 6 facts-unreviewed (per slug count), 7 receipt-unknown; inside a rank by date, slug, id.
Each line ends with that tool's own next command or step; never a value, quote, title, URL, address or evidence.
Writes nothing, never runs a model, kanban or hermes. Reminders are provisional, not a legal status.
A source that fails to load is one rank-0 source-error line and the others still run.
Exit 0 ok, 1 any source-error or a store inside the repository, 2 usage."""
import argparse, contextlib, csv, datetime, importlib.util, io, re, shlex, sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent


def _tool(name):  # siblings are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_today_" + name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


requests_tool, sweep = _tool("flock_requests"), _tool("flock_sweep")
monitor, facts_tool, Fail = sweep.monitor, sweep.facts_tool, sweep.Fail
REVIEW = ("ready-verify", "verify-open", "compare-open", "family-refused")
SLUG_CSV = re.compile(r"([a-z0-9]+(?:-[a-z0-9]+)*)=(.+)")
ERRORS = (Fail, OSError, UnicodeDecodeError, csv.Error, ValueError, KeyError)
TOTALS = (("reminders", ("reminder-passed", "reminder-today", "reminder-week")), ("leads", ("lead-new",)),
          ("stale", ("stale-cited",)), ("reviews", ("review-wait",)), ("unreviewed", ("facts-unreviewed",)),
          ("receipt_unknown", ("receipt-unknown",)))


def q(value):
    return shlex.quote(str(value))


def cmd(tool, *args):
    return " ".join(["python3", q(TOOLS / tool)] + [str(a) for a in args])


def reminders(store, date):  # RF1300-1 due --as-of phrases per open request; reminder arithmetic only
    if not any(store.glob("agencies/*/REQUESTS.csv")):
        return []
    rows, errs = requests_tool.load(store)
    if errs:
        raise Fail("REQUESTS.csv rows")
    as_of, items = datetime.date.fromisoformat(date), []
    due = cmd("flock_requests.py", "due", q(store), "--as-of", date)
    for r in (r for r in rows if r["status"] == "open"):
        phrase = requests_tool.due([r], as_of)[0].rpartition(" | as of %s: " % as_of)[2]
        slug, rid = requests_tool.slug(r["agency"]), r["request_id"]
        if not r["receipt_evidence"]:
            items.append((7, "receipt-unknown", r["sent"], slug, rid, "receipt unknown, %s; %s" % (phrase, cmd(
                "flock_requests.py", "record", q(store), "--agency", slug, "--request", q(rid), "--step receipt"))))
            continue
        m = re.fullmatch(r"in (\d+) days?|reminder date today|reminder date passed (\d+) days? ago", phrase)
        n = int(m[1]) if m[1] else -int(m[2]) if m[2] else 0
        if n <= 7:
            kind = "reminder-week" if n > 0 else "reminder-today" if n == 0 else "reminder-passed"
            when = (as_of + datetime.timedelta(days=n)).isoformat()
            items.append((1 if n <= 0 else 2, kind, when, slug, rid, "%s (provisional); %s" % (phrase, due)))
    return items


def leads(store):  # NB-DF-07 LEADS.csv rows still in state new; a missing file is no leads
    nxt = "%s --item %%s --state requested|ignored --by <name>" % cmd("flock_monitor.py", "lead", "--store", q(store))
    return [(4, "lead-new", r["first_seen"], monitor.slug(r["agency"]), r["item_id"], nxt % q(r["item_id"]))
            for r in monitor.leads_rows(store / "LEADS.csv") if r["state"] == "new"]


def stale_cited(store, date, slug, path):  # NB-DF-08 stale --findings, run in-process; finding ids only
    args = argparse.Namespace(store=str(store), date=date, agency=slug, findings=path)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = facts_tool.stale(args)
    if code not in (0, 3):
        raise Fail("stale")
    again = cmd("flock_facts.py", "stale", "--store", q(store), "--date", date, "--agency", slug, "--findings", q(path))
    found = (re.fullmatch(r"stale \| finding (.*) \| cites (F-[0-9a-f]{12}) \((.*)\)", line)
             for line in out.getvalue().splitlines())
    return [(3, "stale-cited", "", slug, m[1], "cites %s (%s); %s" % (m[2], m[3], again)) for m in found if m]


def unreviewed(store):  # per-slug count of FACTS.csv rows with status unreviewed
    files, errors = facts_tool.store_files(store, "")
    errors = errors or [e for f in files for e in facts_tool.check_file(f, f.parent.name)]
    if errors:
        raise Fail("FACTS.csv rows")
    items = []
    for f in files:
        n = sum(r["status"] == "unreviewed" for _, r in facts_tool.load(f, f.parent.name)[1])
        if n:
            items.append((6, "facts-unreviewed", "", f.parent.name, str(n), cmd(
                "flock_facts.py", "status", "--store", q(store), "--agency", f.parent.name, "--list unreviewed")))
    return items


def reviews(store, watchlist, roster, date):  # flock_sweep status stages waiting on a compare or verify card
    _, agencies, rows = sweep.load_inputs(argparse.Namespace(store=str(store), watchlist=watchlist, roster=roster))
    return [(5, "review-wait", r["run_date"], r["slug"], "%s %s" % (r["stage"], r["run_date"] or "-"), r["next"])
            for r in sweep.status_rows(store, agencies, rows, date) if r["stage"] in REVIEW]


def findings_arg(parser, values):
    found = {}
    for value in values or []:
        m = SLUG_CSV.fullmatch(value)
        if not m or m[1] in found:
            parser.error("--findings must be SLUG=CSV, one per slug")
        found[m[1]] = m[2]
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("--store", "--watchlist", "--roster", "--date"):
        parser.add_argument(name, required=True)
    parser.add_argument("--findings", action="append", metavar="SLUG=CSV")
    a = parser.parse_args(argv)
    if not facts_tool.valid_date(a.date):
        parser.error("--date must be YYYY-MM-DD")
    findings = findings_arg(parser, a.findings)
    try:
        store = monitor.private_store(a.store)
    except Fail as exc:
        return print(exc) or 1
    if not store.is_dir():
        return print("store is not a directory") or 1
    sources = [("requests", lambda: reminders(store, a.date)), ("leads", lambda: leads(store))]
    sources += [("stale:" + s, lambda s=s, p=p: stale_cited(store, a.date, s, p)) for s, p in findings.items()]
    sources += [("facts", lambda: unreviewed(store)), ("sweep", lambda: reviews(store, a.watchlist, a.roster, a.date))]
    errors, items = [], []
    for name, load in sources:
        try:
            items += load()
        except ERRORS as exc:
            errors.append("today | 0 | source-error | %s | %s" % (name, type(exc).__name__))
    items.sort(key=lambda i: (i[0], i[2], i[3], i[4]))  # stable: equal keys keep each tool's own order
    lines = errors + ["today | %d | %s | %s | %s | %s" % (i[0], i[1], i[3], i[4], i[5]) for i in items]
    count = {key: sum(i[1] in kinds for i in items) for key, kinds in TOTALS}
    count["stale"] = count["stale"] if findings else "-"
    print(*lines, sep="\n") if lines else None
    print("today | date=%s items=%d " % (a.date, len(items)) + " ".join("%s=%s" % (k, count[k]) for k, _ in TOTALS)
          + " | provisional; not a legal status")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
