#!/usr/bin/env python3
"""Flock compare: scaffold a run folder; check FINDINGS.csv fail-closed (+ golden). Prints row numbers, fields, ids, counts only."""
import argparse, contextlib, csv, importlib.util, json, os, re, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import law  # noqa: E402

HEADER = ["finding_id", "topic", "claim_class", "rating", "claim", "fact_ids", "rule_ids", "policy_refs",
          "quote", "quote_fact_id", "next_step"]
DRAFT = "Research draft - not legal advice"
PROVIDERS = ("anthropic", "openai", "xai", "google", "local")
CLASSES = ("record-gap", "duty-concern")
RANK = {"weak": 0, "medium": 1, "strong": 2}
ALWAYS = re.compile(r"no records exist|no audit records", re.I)
GATED = re.compile(r"\bviolat\w*|\billegal\w*|\badmi(t|ts|tted|ssion)\b", re.I)
FLOCK = re.compile(r"\bflock\b", re.I)
POLICY_REF = re.compile(r"(P-[0-9a-f]{12})#(.*)")

def _tool(name):
    spec = importlib.util.spec_from_file_location("_flock_" + name, REPO / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

facts_tool, law_policy = _tool("flock_facts"), _tool("flock_law_policy")

def ids(text):
    return [part.strip() for part in text.split(";") if part.strip()]

def squash(text):
    return " ".join(text.split())

def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))

def has_findings(path):
    return path.is_file() and any(any(c.strip() for c in rec) for rec in read_csv(path)[1:])

def folder_problem(path, label):  # never follow a symlinked runs/run folder; a non-folder is refused, not traced
    if path.is_symlink():
        return label + " folder is a symlink"
    return label + " folder is not a folder" if os.path.lexists(path) and not path.is_dir() else None

def scaffold(args):
    store = Path(args.store).resolve()
    agency = store / "agencies" / args.agency
    run = agency / "runs" / args.date
    folder = folder_problem(agency / "runs", "runs") or folder_problem(run, "run")
    if store == REPO or REPO in store.parents:
        reason = "store inside repository"
    elif not (agency / "FACTS.csv").is_file():
        reason = "agency FACTS.csv missing"
    elif folder:
        reason = folder
    elif has_findings(run / "FINDINGS.csv"):
        reason = "run folder already has findings"
    else:
        reason = None
    if reason:
        print("scaffold refused: " + reason)
        return 1
    (agency / "runs").mkdir(mode=0o700, exist_ok=True)
    run.mkdir(mode=0o700, exist_ok=True)
    meta = {"schema": 1, "agency": args.agency, "date": args.date, "author_provider": args.provider,
            "author_model": args.model, "applicability": args.applicability}
    for name, text in (("RUN.json", json.dumps(meta, indent=2) + "\n"), ("FINDINGS.csv", ",".join(HEADER) + "\n"),
                       ("FINDINGS.md", DRAFT + "\n")):
        fd, tmp = tempfile.mkstemp(prefix="." + name + ".", suffix=".tmp", dir=run)  # fresh temp; a final-name symlink is replaced
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out: out.write(text)
            os.replace(tmp, run / name)
        except BaseException:
            with contextlib.suppress(OSError): os.unlink(tmp)
            raise
    print("scaffolded run for %s on %s" % (args.agency, args.date))
    return 0

def check_row(n, row, ctx):
    errs = []
    if not row["finding_id"] or row["finding_id"] in ctx["seen"]:
        errs.append("finding_id is empty or duplicated")
    ctx["seen"].add(row["finding_id"])
    for field, ok in (("topic", re.fullmatch(r"[a-z0-9-]+", row["topic"])), ("claim_class", row["claim_class"] in CLASSES),
                      ("rating", row["rating"] in RANK), ("claim", 0 < len(row["claim"]) <= 400),
                      ("next_step", len(row["next_step"]) <= 300)):
        if not ok:
            errs.append(field + " is invalid")
    fact_ids, facts = ids(row["fact_ids"]), ctx["facts"]
    if not fact_ids:
        errs.append("fact_ids is empty")
    if any(f not in facts for f in fact_ids):
        errs.append("fact_ids cites a fact not in FACTS.csv")
    if any(facts[f]["value"].strip() == "UNREADABLE" for f in fact_ids if f in facts):
        errs.append("fact_ids cites an UNREADABLE fact (routes back to the coordinator)")
    rules, refs = ids(row["rule_ids"]), ids(row["policy_refs"])
    if not rules and not refs:
        errs.append("rule_ids and policy_refs are both empty")
    if any(r not in ctx["law"] or r not in ctx["package"] for r in rules):
        errs.append("rule_ids cites a rule not in LAW.csv and the law package")
    if row["rating"] == "strong" and any(ctx["package"].get(r) == "needs_attorney_review" for r in rules):
        errs.append("rating strong cites a needs_attorney_review rule")
    matches = [POLICY_REF.fullmatch(ref) for ref in refs]
    if any(not m or m.group(1) not in ctx["policy"] or not m.group(2).strip() for m in matches):
        errs.append("policy_refs needs P-<12hex>#<clause> from POLICY.csv")
    quote, qid = squash(row["quote"]), row["quote_fact_id"].strip()
    quoted = bool(quote) and qid in fact_ids and qid in facts and quote in squash(facts[qid]["verbatim_quote"])
    if quote and not quoted:
        errs.append("quote is not in the verbatim_quote of quote_fact_id")
    for field in ("claim", "next_step"):
        if ALWAYS.search(row[field]):
            errs.append(field + ' always-banned wording (say "found no responsive records")')
        if GATED.search(row[field]) and not quoted:
            errs.append(field + " quote-gated wording without a matching quote")
        if ctx["vigilant_only"] and FLOCK.search(row[field]):
            errs.append(field + " vendor-gated wording (vendor facts do not name Flock)")
    return ["row %d: %s" % (n, e) for e in errs]

def golden_errors(path, rows):
    errs, table = [], read_csv(path)
    if not table or table[0] != ["topic", "claim_class", "cited", "max_rating"]:
        return ["golden: header is not topic,claim_class,cited,max_rating"]
    for g in (dict(zip(table[0], rec)) for rec in table[1:] if rec):
        if not any(r["topic"] == g["topic"] for _, r in rows):
            errs.append("golden: topic %s missing" % g["topic"])
        same = [(n, r) for n, r in rows if (r["topic"], r["claim_class"]) == (g["topic"], g["claim_class"])]
        errs += ["row %d: rating above golden" % n for n, r in same
                 if RANK.get(r["rating"], 3) > RANK.get(g["max_rating"], -1)]
        cites = lambda r: ids(r["rule_ids"]) + [ref.partition("#")[2].strip() for ref in ids(r["policy_refs"])]
        if same and not any(g["cited"] in cites(r) for _, r in same):
            errs.append("golden: %s/%s does not cite %s" % (g["topic"], g["claim_class"], g["cited"]))
    return list(dict.fromkeys(errs))

def check(rundir, golden=None):
    run = Path(rundir).resolve()
    if len(run.parents) < 4 or run.parent.name != "runs" or run.parents[2].name != "agencies":
        return ["run: not a <store>/agencies/<slug>/runs/<date> folder"], 0
    agency, store = run.parents[1], run.parents[3]
    errs = []
    md = run / "FINDINGS.md"
    if not md.is_file() or md.read_text(encoding="utf-8").splitlines()[:1] != [DRAFT]:
        errs.append("FINDINGS.md: line 1 must be exactly '%s'" % DRAFT)
    try:
        meta = json.loads((run / "RUN.json").read_text(encoding="utf-8"))
        if meta.get("agency") != agency.name or meta.get("author_provider") not in PROVIDERS:
            errs.append("RUN.json: agency or author_provider is invalid")
    except (OSError, ValueError):
        errs.append("RUN.json: missing or unreadable")
    facts_path = agency / "FACTS.csv"
    store_errs = facts_tool.check_file(facts_path, agency.name) if facts_path.is_file() else ["FACTS.csv missing"]
    law_rows, e = law_policy.load_law(store / "LAW.csv")
    store_errs += ["LAW.csv " + x for x in e]
    policy_rows, e = law_policy.load_policy(agency / "POLICY.csv", agency.name)
    store_errs += ["POLICY.csv " + x for x in e]
    errs += ["store: " + x for x in store_errs]
    facts = {r["fact_id"]: r for _, r in facts_tool.load(facts_path, agency.name)[1]} if facts_path.is_file() else {}
    vendors = [(r["value"] + " " + r["verbatim_quote"]).lower() for r in facts.values() if r["fact_type"] == "vendor"]
    ctx = {"facts": facts, "seen": set(), "law": {r["rule_id"] for _, r in law_rows},
           "policy": {r["policy_row_id"] for _, r in policy_rows},
           "package": {r["rule_id"]: r["review"] for r in law.load_package("us-ca")["rules"]},
           "vigilant_only": any("vigilant" in v or "motorola" in v for v in vendors) and not any("flock" in v for v in vendors)}
    table = read_csv(run / "FINDINGS.csv") if (run / "FINDINGS.csv").is_file() else []
    if not table or table[0] != HEADER:
        return errs + ["row 1: FINDINGS.csv header is missing or wrong"], 0
    rows = []
    for n, rec in enumerate(table[1:], 2):
        if not any(c.strip() for c in rec):
            continue
        if len(rec) != len(HEADER):
            errs.append("row %d: columns expected %d, found %d" % (n, len(HEADER), len(rec)))
            continue
        rows.append((n, dict(zip(HEADER, (c.strip() for c in rec)))))
        errs += check_row(n, rows[-1][1], ctx)
    if golden and not Path(golden).is_file():
        return errs + ["golden: missing or not a regular file"], len(rows)
    return errs + (golden_errors(golden, rows) if golden else []), len(rows)

def main(argv=None):
    try:
        return _main(argv)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        print("io: %s" % type(exc).__name__)  # class name only: never a path or file content
        return 1

def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["scaffold"])
    for name in ("--store", "--agency", "--date", "--model", "--applicability", "--check", "--golden"):
        parser.add_argument(name)
    parser.add_argument("--provider", choices=PROVIDERS)
    args = parser.parse_args(argv)
    if args.check and not args.command:
        errs, count = check(args.check, args.golden)
        print("\n".join(errs + ["findings=%d errors=%d" % (count, len(errs))]))
        return 1 if errs else 0
    fields = (args.store, args.agency, args.date, args.provider, (args.model or "").strip(), (args.applicability or "").strip())
    if args.command != "scaffold" or args.check or args.golden or not all(fields) \
            or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", args.agency) or not law_policy.day(args.date) \
            or len(args.applicability) > 200:
        parser.error("usage: scaffold --store DIR --agency SLUG --date YYYY-MM-DD --provider P --model M "
                     "--applicability TEXT | --check RUNDIR [--golden CSV]")
    return scaffold(args)

if __name__ == "__main__":
    sys.exit(main())
