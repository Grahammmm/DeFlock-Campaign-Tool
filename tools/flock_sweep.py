#!/usr/bin/env python3
"""Flock sweep: each watch-list agency's F1->F3->F4 stage, blocker and next command; CARDS plan; rating matrix.
  status|plan|matrix --watchlist CSV --roster CSV --store DIR [--out FILE|DIR] [--max N] [--date YYYY-MM-DD]
Reads the store only; never runs a model, kanban or hermes. Prints slugs, stages, counts and commands, never claim,
quote or fact text. Summary: blocked = agencies - finished - ready - unmapped; unwatched = in_scope=yes roster rows
with no watch-list row, named (roster order) on the line before it. Exit 0 ok, 1 errors, 2 usage."""
import argparse, csv, datetime, importlib.util, io, json, re, shlex, sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

def _tool(name):  # F1/F2/F3/F4/F7 are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_sweep_" + name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

monitor, verify = _tool("flock_monitor"), _tool("flock_verify")
compare = verify.compare
facts_tool, law_policy, Fail = compare.facts_tool, compare.law_policy, monitor.Fail
ROSTER = ["watch_agency", "slug", "in_scope", "applicability", "site_slug"]
STATUS = ["slug", "watch_agency", "stage", "facts", "unreadable", "policy_rows", "run_date", "author_family",
          "verifier_family", "public_ok", "questions", "next"]
OTHER = {"anthropic": "openai", "openai": "anthropic"}
ORDER = {"rejected": 0, "weak": 1, "medium": 2, "strong": 3}
READY = ("ready-compare", "ready-verify")
MD1 = "Research draft - not legal advice. Ratings are research ratings, not legal conclusions."
GATE = "golden gate: Morro Bay F3/F4 golden must pass first"
CAPS = "caps: 40 turns / 2 h, chunked by document, REMAINING.md on cap"

def table(path, header, label, hint=""):
    try:
        return monitor.read_csv(path, header)
    except Fail as exc:
        raise Fail(label + str(exc) + (hint if str(exc).startswith("row 1:") else "")) from None

def load_inputs(args):
    store = monitor.private_store(args.store)
    if not store.is_dir(): raise Fail("store is not a directory")
    watch = table(args.watchlist, monitor.WATCH_HEADER, "watchlist ", " (convert L3 to the F7 header first)")
    errs = ["watchlist row %d: %s" % (n, e) for n, row in watch for e in monitor.row_errors(row)]
    agencies, roster = list(dict.fromkeys(r["agency"].strip() for _, r in watch)), []
    for n, r in table(args.roster, ROSTER, "roster "):
        r = {k: v.strip() for k, v in r.items()}
        dup = r["watch_agency"] in {x["watch_agency"] for x in roster}
        checks = (("watch_agency", r["watch_agency"] and not dup, "is empty or duplicated"),
                  ("slug", re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", r["slug"]), "is not a store slug"),
                  ("in_scope", r["in_scope"] in ("yes", "no", "hold"), "is not yes, no or hold"),
                  ("applicability", len(r["applicability"]) <= 200, "is over 200 chars"),
                  ("applicability", r["applicability"] or r["in_scope"] != "yes", "is required when in_scope=yes"),
                  ("site_slug", re.fullmatch(r"([a-z][a-z0-9-]*)?", r["site_slug"]), "is invalid"))
        errs += ["roster row %d: %s %s" % (n, field, why) for field, ok, why in checks if not ok]
        roster.append(r)
    if errs: raise Fail("\n".join(errs + ["errors=%d" % len(errs)]))
    return store, agencies, roster

def provider(path, key):
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get(key)
    except (OSError, ValueError, AttributeError):
        return ""
    return value.strip().lower() if isinstance(value, str) else ""

def assess(store, slug, watch_agency, r, date):
    out = dict(dict.fromkeys(STATUS, ""), slug=slug, watch_agency=watch_agency, date=date)
    if r is None:
        return dict(out, stage="store-only" if slug else "unmapped", next="-" if slug else "add a ROSTER row")
    agency, law_path = store / "agencies" / slug, store / "LAW.csv"
    facts_path, policy_path = agency / "FACTS.csv", agency / "POLICY.csv"
    fact_rows = facts_tool.load(facts_path, slug)[1] if facts_path.is_file() else []
    out.update(facts=len(verify.rows(facts_path)), policy_rows=len(verify.rows(policy_path)),
               unreadable=sum(f["value"].strip() == "UNREADABLE" for _, f in fact_rows))
    runs = sorted(p for p in (agency / "runs").glob("*") if p.is_dir() and facts_tool.valid_date(p.name))
    run = runs[-1] if runs else None
    if run:
        qs = (run / "QUESTIONS.md").read_text(encoding="utf-8").splitlines() if (run / "QUESTIONS.md").is_file() else []
        out.update(run_date=run.name, author_family=provider(run / "RUN.json", "author_provider"),
                   verifier_family=provider(run / "VERIFY.json", "verifier_provider"),
                   public_ok=len(verify.rows(run / "PUBLIC-OK.csv")), questions=sum(x.startswith("- Q ") for x in qs))
    app, q = r["applicability"], lambda p: shlex.quote(str(p))
    other = out["other"] = OTHER.get(out["author_family"], "<operator picks>")
    scaffold = "python3 %s scaffold --store %s --agency %s --date %s --provider <author family> --model <model> " \
               "--applicability \"%s\"; compare card" % (q(TOOLS / "flock_compare.py"), q(store), slug, date,
                                                         re.sub(r'([\\"$`])', r"\\\1", app))
    start = "python3 %s start %s --provider %s --model <model>; verify card" % (q(TOOLS / "flock_verify.py"), q(run), other)
    steps = ((lambda: r["in_scope"] == "no", "out-of-scope", "-"),
             (lambda: r["in_scope"] == "hold" or not app, "scope-hold", "operator: applicability line"),
             (lambda: not out["facts"], "no-facts", "legwork facts + flock_facts.py import"),
             (lambda: fact_rows and out["unreadable"] == len(fact_rows), "facts-unreadable", "coordinator: UNREADABLE pages"),
             (lambda: not out["policy_rows"], "no-policy", "legwork POLICY rows (flock-law-policy)"),
             (lambda: not verify.rows(law_path), "no-law", "legwork LAW rows"),
             (lambda: facts_tool.check_file(facts_path, slug) or law_policy.load_law(law_path)[1]
              or law_policy.load_policy(policy_path, slug)[1], "store-errors", "fix store rows (flock_facts.py --check)"),
             (lambda: run is None, "ready-compare", scaffold),
             (lambda: (run / "REMAINING.md").exists() or (lambda c: c[0] or not c[1])(compare.check(run)),
              "compare-open", "resume compare card (REMAINING.md)"),
             (lambda: not (run / "VERIFY.json").exists(), "ready-verify", start),
             (lambda: verify.family(run), "family-refused", "operator: re-run verify in another family"),
             (lambda: verify.load(run)[0] or not (run / "PUBLIC-OK.csv").is_file() or not (run / "QUESTIONS.md").is_file(),
              "verify-open", "resume verify card"))
    for cond, stage, nxt in steps:
        if cond():
            return dict(out, stage=stage, next=nxt)
    return dict(out, stage="finished", next="F5b drafts; F9 export if public_ok>0")

def status_rows(store, agencies, roster, date):
    rows = [assess(store, r["slug"], r["watch_agency"], r, date) for r in roster]
    mapped, slugs = {r["watch_agency"] for r in roster}, {r["slug"] for r in roster}
    rows += [assess(store, "", a, None, date) for a in agencies if a not in mapped]
    return rows + [assess(store, p.name, "", None, date) for p in sorted((store / "agencies").glob("*"))
                   if p.is_dir() and p.name not in slugs]

def outside(path, store):
    path = Path(path).resolve()
    for base, what in ((monitor.REPO, "repository"), (store, "store")):
        if path == base or base in path.parents: raise Fail("--out inside " + what)
    return path

def as_csv(header, rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows([header] + rows)
    return buf.getvalue()

def plan(mapped, cap):  # mapped = roster rows in roster order
    gate = [r for r in mapped if r["slug"] == "morro-bay" and r["stage"] != "finished"][:1]
    lines = ["CARDS"]
    for r in gate or [r for r in mapped if r["stage"] in READY][:cap]:
        if r["stage"] not in READY:
            lines.append("- %s %s: %s" % (r["slug"], r["stage"], r["next"]))
            continue
        family = "author records its own provider; the verify card must use another family" if not r["run_date"] else \
            "verifier %s (author %s; never the author's family)" % (r["other"], r["author_family"] or "unknown")
        lines += ["## [RUN] %s %s %s" % (r["slug"], r["stage"][6:], r["run_date"] or r["date"]), "commands: " + r["next"],
                  CAPS, "family: " + family]
    print("\n".join(lines + ([GATE] if gate else [] if len(lines) > 1 else ["- none ready"])))

def matrix(store, mapped, out):
    done = list(dict.fromkeys(r["slug"] for r in mapped if r["stage"] == "finished"))
    dates, cells, loaded, refused = {r["slug"]: r["run_date"] for r in mapped}, {}, {}, []
    for slug in done:  # check every finished run before mkdir or any write; counts only, never ids, quotes or paths
        errs, findings, _, ver = verify.load(store / "agencies" / slug / "runs" / dates[slug])
        missing = [f for f in findings if f["finding_id"] not in ver]
        bad = [v for v in ver.values() if v["verdict"] != "reject" and v.get("new_rating") not in ORDER]
        if errs or missing or bad:
            refused.append("matrix: %s verification unusable (errors=%d unverified=%d bad_rating=%d)"
                           % (slug, len(errs), len(missing), len(bad)))
        loaded[slug] = findings, ver
    if refused:
        raise Fail("\n".join(refused))
    for slug in done:
        findings, ver = loaded[slug]
        for f in findings:
            v, cur = ver[f["finding_id"]], cells.setdefault((f["topic"], f["claim_class"]), {})
            final = "rejected" if v["verdict"] == "reject" else v["new_rating"]
            cur[slug] = max(final, cur.get(slug, final), key=ORDER.__getitem__)
    keys, lines = sorted(cells), [MD1]
    for topic in sorted({t for t, _ in keys}):
        best = {s: max([cells[k][s] for k in keys if k[0] == topic and s in cells[k]] or [""], key=ORDER.get) for s in done}
        lines.append("- %s: " % topic + " | ".join("%s %s" % (k, ";".join(s for s in done if best[s] == k) or "-")
                                                   for k in ("strong", "medium", "weak", "rejected")))
    pending = [s for s in dict.fromkeys(r["slug"] for r in mapped) if s not in done]
    lines.append("- not finished: " + (";".join(pending) or "-"))
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    verify.write(out / "COMPARE-MATRIX.csv", as_csv(["topic", "claim_class"] + done,
                                                    [list(k) + [cells[k].get(s, "-") for s in done] for k in keys]))
    verify.write(out / "COMPARE.md", "\n".join(lines) + "\n")
    print("matrix rows=%d agencies=%d" % (len(keys), len(done)))

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["status", "plan", "matrix"])
    for name in ("--watchlist", "--roster", "--store", "--out"):
        parser.add_argument(name)
    parser.add_argument("--max", type=int, default=2)
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    a = parser.parse_args(argv)
    if not (a.watchlist and a.roster and a.store) or a.max < 1 or not facts_tool.valid_date(a.date) \
            or (a.command == "matrix") != bool(a.out) and a.command != "status":
        parser.error("usage: status|plan|matrix --watchlist CSV --roster CSV --store DIR [--out PATH] [--max N] [--date D]")
    try:
        store, agencies, roster = load_inputs(a)
        out = outside(a.out, store) if a.out else None
        rows = status_rows(store, agencies, roster, a.date)
        if a.command != "status":
            return (plan(rows[:len(roster)], a.max) if a.command == "plan" else matrix(store, rows[:len(roster)], out)) or 0
        text = as_csv(STATUS, [[r[k] for k in STATUS] for r in rows])
        if out:
            verify.write(out, text)
        fin, ready, unmapped = (sum(r["stage"] in s for r in rows) for s in (("finished",), READY, ("unmapped",)))
        names = [r["watch_agency"] for r in roster if r["in_scope"] == "yes" and r["watch_agency"] not in agencies]
        named = "unwatched: %s\n" % "; ".join(names) if names else ""  # stdout only, never --out
        print(text + named + "agencies=%d finished=%d ready=%d blocked=%d unmapped=%d unwatched=%d" % (
            len(rows), fin, ready, len(rows) - fin - ready - unmapped, unmapped, len(names)))
        return 0
    except Fail as exc:
        return print(exc) or 1
    except (OSError, ValueError, csv.Error) as exc:
        return print("file unreadable (%s)" % type(exc).__name__) or 1

if __name__ == "__main__":
    sys.exit(main())
