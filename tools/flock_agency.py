#!/usr/bin/env python3
"""Flock agency timeline (BF-df-agency-timeline): one agency's history in date order, from the private store's recorded fields.
  flock_agency.py timeline STORE --agency SLUG [--as-of YYYY-MM-DD] [--inventory CSV] [--json]
Requests sent, receipts, extension notices, determinations, productions, closures and withdrawals (REQUESTS.csv and
REQUEST-STEPS.csv), fee rows (FEES.csv), email-run states (flock_state) and received-document counts (an inventory CSV),
oldest first, then a "still open" block with each open request's state and provisional reminder date. Ids, counts, enum words
and amounts only. Prints and reads only. Exit 0 clean; 1 on a store refusal, a source-error or a failing run state; 2 usage.
JSON schema flock-agency-timeline-v1 is review_required. Fields only; not a legal status; the canonical ledger wins."""
import collections
import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
KINDS = ("sent", "receipt", "extension-notice", "determination", "produced", "closed", "withdrawn", "step", "fee-invoice",
         "fee-paid", "email-", "documents")
STEP_KIND = {"receipt": "receipt", "extension": "extension-notice", "determination": "determination", "produced": "produced",
             "closed": "closed"}
SLUG = re.compile(r"[a-z0-9-]{1,60}")
USAGE = ("usage: flock_agency.py timeline STORE --agency SLUG [--as-of YYYY-MM-DD] [--inventory CSV] [--json]"
         "  (one agency's recorded history, oldest first; prints only, writes nothing)")


class Refuse(Exception): pass


def _tool(name):  # siblings are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_agency_" + name, TOOLS / (name + ".py"))
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module


state = _tool("flock_state")
rq = state.requests_tool  # the one flock_requests that flock_state already loaded


def safe(text):  # a loader reason keeps its plain words; any id, address, hash or banned word in it is masked
    return " ".join(w if state.SAFE.fullmatch(w.strip("(),:;")) and not state.BANNED.search(w) else "(unsafe-label)"
                    for w in str(text).split())


def kind_rank(kind):
    return KINDS.index("email-") if kind.startswith("email-") else KINDS.index(kind)


def requests_source(folder, errors):
    """(good rows with their state) from REQUESTS.csv; record-error rows are named as source-errors and left out."""
    try:
        states = state.folder_requests(folder)
    except state.Refuse:
        return errors.append(("REQUESTS.csv", "unreadable, a link or not v1")) or []
    rows = rq.load(folder / "REQUESTS.csv", folder.name)[0] if states else []
    errors += [("REQUESTS.csv", "record-error %s (%s)" % (i, ",".join(b))) for i, s, b in states if s == "record-error"]
    return [(r, s) for r, (_, s, _) in zip(rows, states) if s != "record-error"]


def request_events(good):
    for r, _ in good:
        rid = state.label(r["request_id"])
        yield r["sent"], "sent", rid, ""
        for field, kind in (("receipt_date", "receipt"), ("extension_notice_date", "extension-notice"),
                            ("determination_date", "determination")):
            if r[field]:
                yield r[field], kind, rid, ""
        if r["status"] in ("produced", "closed", "withdrawn"):
            yield None, r["status"], rid, ""  # the status has no date of its own in REQUESTS.csv


def steps_events(folder, errors):
    text, table, bad = rq.read_log(folder / "REQUEST-STEPS.csv")
    if bad or (text is not None and (table[:1] != [rq.STEP_HEADER] or any(len(c) != 6 for c in table[1:] if c))):
        return errors.append(("REQUEST-STEPS.csv", safe(bad or "does not match REQUEST-STEPS.csv v1"))) or []
    out = []
    for n, c in ((n, c) for n, c in enumerate(table[1:], 2) if c):
        if not rq.day(c[2]):
            errors.append(("REQUEST-STEPS.csv", "row %d: date must be YYYY-MM-DD" % n))
            continue
        kind = STEP_KIND.get(c[1], "step")
        out.append((c[2], kind, state.label(c[0]), "" if kind != "step" else state.label(c[1])))
    return out


def fee_events(folder, good, errors):
    text, table, bad = rq.read_log(folder / "FEES.csv")
    found = [bad] if bad else rq.fee_errors(table, {r["request_id"]: r for r, _ in good}) if text is not None else []
    if found:
        return errors.extend(("FEES.csv", safe(e.removeprefix("FEES.csv "))) for e in found) or []
    names = {"invoice": ("fee-invoice", ""), "paid": ("fee-paid", ""), "estimate": ("step", "fee-estimate ")}
    return [(c[3], names[c[1]][0], state.label(c[0]), "%sUSD %d.%02d" % (names[c[1]][1], int(c[2]) // 100, int(c[2]) % 100))
            for c in table[1:] if c]


def run_events(folder):
    runs = state.folder_runs(folder, state.stage_fn())
    failing = sum(s in state.FAILING or not mapped for _, s, mapped in runs)
    return [(d if rq.day(d) else None, "email-" + s, "-", "") for d, s, _ in runs], failing


def document_events(path, slug, good, errors):
    inv, why = rq.inventory(path)
    if why:
        return errors.append(("inventory", safe(why))) or []
    parts = collections.defaultdict(list)
    for n, rec in inv:
        parts[str(rq.day(rec.get("date", "").strip()) or "")].append((n, rec))
    out = []
    for when, part in sorted(parts.items()):
        docs, _ = rq.documents({slug: [r for r, _ in good]}, part)  # rows of other agencies' requests are not ours
        for (_, rid), got in sorted(docs.items()):
            out.append((when or None, "documents", state.label(rid), "%d received, %d analysis-only" % (len(got), sum(got.values()))))
    return out


def merge(events):
    """Duplicate facts (same date, kind, request, detail) render once; an undated status a dated step also records is dropped."""
    counts = collections.Counter(events)
    dated = {(k, i) for d, k, i, _ in counts if d and k in ("produced", "closed", "withdrawn")}
    out = []
    for (d, k, i, detail), n in counts.items():
        if d is None and (k, i) in dated:
            continue
        twice = "" if n == 1 else "(recorded twice)" if n == 2 else "(recorded %d times)" % n
        out.append({"date": d or "undated", "kind": k, "request": i, "detail": " ".join(filter(None, (detail, twice))) or "-"})
    return sorted(out, key=lambda e: (e["date"] == "undated", e["date"], kind_rank(e["kind"]), e["request"], e["detail"]))


def still_open(good, as_of):
    out = []
    for r, s in sorted((g for g in good if g[0]["status"] == "open"), key=lambda g: g[0]["request_id"]):
        try:
            line = rq.due([r], as_of)[0]
        except ValueError:
            line = ""
        m = re.search(r"\| determination ([0-9-]{10}) \|", line)
        dist = line.rpartition(": ")[2].replace("reminder date ", "") if " | as of " in line else ""
        out.append({"request": state.label(r["request_id"]), "state": s,
                    "reminder": "reminder %s (provisional)%s" % (m[1] if m else "-", ", " + dist if m and dist else "")})
    return out


def timeline(store, slug, as_of, inventory):
    root = Path(store).resolve()
    if root == REPO or REPO in root.parents:
        raise Refuse("store is inside the repository")
    base, folder = root / "agencies", root / "agencies" / slug
    if not root.is_dir() or base.is_symlink() or not base.is_dir():
        raise Refuse("store is not a folder with an agencies folder")
    if folder.is_symlink() or not folder.is_dir():
        raise Refuse("agency folder is missing or a link")
    errors = []
    good = requests_source(folder, errors)
    events = list(request_events(good)) + steps_events(folder, errors) + fee_events(folder, good, errors)
    runs, failing = run_events(folder)
    events += runs + (document_events(inventory, slug, good, errors) if inventory else [])
    return merge(events), still_open(good, as_of), [{"file": f, "reason": r} for f, r in errors], failing


def render(slug, as_of, events, opened, errors, failing, as_json):
    shown = slug if not state.BANNED.search(slug) else "(unsafe-label)"
    if as_json:
        print(json.dumps({"schema": "flock-agency-timeline-v1", "review_required": True, "agency": shown, "as_of": str(as_of),
                          "note": state.NOTE, "events": events, "open": opened, "errors": errors,
                          "failing_runs": failing}, ensure_ascii=True))
    else:
        lines = ["agency %s | as of %s | %s" % (shown, as_of, state.NOTE)]
        lines += ["%s | %s | %s | %s" % (e["date"], e["kind"], e["request"], e["detail"]) for e in events]
        lines += ["source-error | %s | %s" % (e["file"], e["reason"]) for e in errors]
        lines += ["still open | %s | %s | %s" % (o["request"], o["state"], o["reminder"]) for o in opened]
        lines.append("totals | events=%d open=%d errors=%d failing_runs=%d" % (len(events), len(opened), len(errors), failing))
        print("\n".join(lines))
    return 1 if errors or failing else 0


def main(argv):
    if argv in (["--help"], ["-h"]):
        return print(USAGE) or 0
    as_json, rest = "--json" in argv, [a for a in argv if a != "--json"]
    keys, opts = rest[2::2], dict(zip(rest[2::2], rest[3::2]))
    ok = (len(rest) >= 4 and len(rest) % 2 == 0 and rest[0] == "timeline" and argv.count("--json") <= 1
          and len(set(keys)) == len(keys) and "--agency" in opts and set(opts) <= {"--agency", "--as-of", "--inventory"})
    if not ok:
        return print(USAGE, file=sys.stderr) or 2
    slug, as_of = opts["--agency"], rq.day(opts.get("--as-of", date.today().isoformat()))
    if not SLUG.fullmatch(slug):
        return print("timeline: --agency must be a folder slug ([a-z0-9-]{1,60})", file=sys.stderr) or 2
    if as_of is None:
        return print("timeline: --as-of must be a real YYYY-MM-DD date", file=sys.stderr) or 2
    try:
        found = timeline(rest[1], slug, as_of, opts.get("--inventory"))
    except (Refuse, state.Refuse, OSError) as exc:
        why = str(exc) if isinstance(exc, (Refuse, state.Refuse)) else "store unreadable (%s)" % type(exc).__name__
        return print("timeline: " + why, file=sys.stderr) or 1
    return render(slug, as_of, *found, as_json)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
