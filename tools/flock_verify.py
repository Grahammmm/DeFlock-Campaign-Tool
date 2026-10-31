#!/usr/bin/env python3
"""Flock verify: a different-family verifier's confirm/downgrade/reject per finding -> PUBLIC-OK.csv, QUESTIONS.md.
  start RUNDIR --provider P --model M  |  --check RUNDIR  |  finish RUNDIR [--golden CSV]
Exit 1 = errors, 2 = usage, 3 = family refusal (equal, missing or unknown). Prints ids, labels and counts only.
start/finish RUNDIR must lie outside the repository."""
import argparse, contextlib, csv, importlib.util, io, json, os, re, sys, tempfile
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("_flock_compare", Path(__file__).resolve().parent / "flock_compare.py")
compare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare)
HEADER = ["finding_id", "verdict", "reread_fact_id", "reread_page", "new_rating", "records_sought", "reason"]
PUBLIC = ["finding_id", "topic", "claim_class", "rating", "fact_ids", "rule_ids", "policy_refs"]
QLINE1 = "Research draft - not legal advice. Records to seek; not allegations."
ASKS = re.compile(r"\b(explain\w*|why|justif\w*|answer\w*)\b", re.I)

def family(run, verifier=None):
    try:
        author = json.loads((run / "RUN.json").read_text(encoding="utf-8")).get("author_provider")
        verifier = json.loads((run / "VERIFY.json").read_text(encoding="utf-8")).get("verifier_provider") if verifier is None else verifier
    except (OSError, ValueError, AttributeError):
        return "refused: family unknown"
    fams = [x.strip().lower() if isinstance(x, str) else "" for x in (author, verifier)]
    if any(f not in compare.PROVIDERS for f in fams):
        return "refused: family unknown"
    return "refused: verifier family equals author family" if fams[0] == fams[1] else None

def banned(text, vigilant_only):  # F3's rules for free text with no quote, so quote-gated words are banned outright
    return bool(compare.ALWAYS.search(text) or compare.GATED.search(text) or (vigilant_only and compare.FLOCK.search(text)))

def write(path, text):
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            out.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError): os.unlink(tmp)
        raise

def rows(path):
    return [r for r in compare.read_csv(path)[1:] if any(c.strip() for c in r)] if path.is_file() else []

def load(run):
    errs, agency = ["findings: " + e for e in compare.check(run)[0]], run.parents[1]
    findings = [dict(zip(compare.HEADER, (c.strip() for c in rec))) for rec in rows(run / "FINDINGS.csv")]
    facts_path = agency / "FACTS.csv"
    facts = {r["fact_id"]: r for _, r in compare.facts_tool.load(facts_path, agency.name)[1]} if facts_path.is_file() else {}
    vendors = [(r["value"] + " " + r["verbatim_quote"]).lower() for r in facts.values() if r["fact_type"] == "vendor"]
    vigilant = any("vigilant" in v or "motorola" in v for v in vendors) and not any("flock" in v for v in vendors)
    table = compare.read_csv(run / "VERIFY.csv") if (run / "VERIFY.csv").is_file() else []
    if not table or table[0] != HEADER:
        return errs + ["row 1: VERIFY.csv header is missing or wrong"], findings, facts, {}
    by_id, verify, rank = {f["finding_id"]: f for f in findings}, {}, compare.RANK
    for n, rec in ((n, rec) for n, rec in enumerate(table[1:], 2) if any(c.strip() for c in rec)):
        if len(rec) != len(HEADER):
            errs.append("row %d: columns expected %d, found %d" % (n, len(HEADER), len(rec)))
            continue
        v = dict(zip(HEADER, (c.strip() for c in rec)))
        f, bad = by_id.get(v["finding_id"]), []
        if f is None or v["finding_id"] in verify:
            errs.append("row %d: finding_id is unknown or duplicated" % n)
            continue
        verify[v["finding_id"]] = v
        if v["verdict"] not in ("confirm", "downgrade", "reject"):
            bad.append("verdict is invalid")
        if v["reread_fact_id"] not in compare.ids(f["fact_ids"]) or v["reread_fact_id"] not in facts:
            bad.append("reread_fact_id is not a fact cited by the finding")
        elif v["reread_page"] != facts[v["reread_fact_id"]]["page"].strip():
            bad.append("reread_page differs from the fact's page")
        new, old = v["new_rating"], f["rating"]
        if (v["verdict"] == "confirm" and new != old) or (v["verdict"] == "reject" and new) or \
                (v["verdict"] == "downgrade" and not (new in rank and rank[new] < rank.get(old, -1))):
            bad.append("new_rating does not fit the verdict")
        need, sought = new == "medium" or v["verdict"] == "downgrade", v["records_sought"]
        if need and not (0 < len(sought) <= 300 and sought.startswith("Existing ") and not re.search(r"[|\n?]", sought)
                         and not ASKS.search(sought) and not banned(sought, vigilant)):
            bad.append("records_sought must seek existing records (Existing ..., no questions or explanations)")
        elif not need and sought:
            bad.append("records_sought must be empty unless medium or downgrade")
        if not 0 < len(v["reason"]) <= 300 or banned(v["reason"], vigilant):
            bad.append("reason is empty, too long or uses banned wording")
        errs += ["row %d: %s" % (n, b) for b in bad]
    errs += ["FINDINGS.csv finding_id %s: has no VERIFY.csv row" % f["finding_id"] for f in findings if f["finding_id"] not in verify]
    return errs, findings, facts, verify

def start(run, provider, model):
    errs = ["findings: " + e for e in compare.check(run)[0]] + (["VERIFY.csv already has rows"] if rows(run / "VERIFY.csv") else [])
    if errs:
        return print("\n".join(errs + ["start refused: errors=%d" % len(errs)])) or 1
    write(run / "VERIFY.json", json.dumps({"schema": 1, "verifier_provider": provider.strip().lower(), "verifier_model": model}) + "\n")
    write(run / "VERIFY.csv", ",".join(HEADER) + "\n")
    return print("started verify run") or 0

def finish(run, golden):
    errs, findings, facts, verify = load(run)
    final = [(n, dict(f, rating=verify[f["finding_id"]]["new_rating"])) for n, f in enumerate(findings, 2)
             if not errs and verify[f["finding_id"]]["verdict"] != "reject"]
    errs += compare.golden_errors(golden, final) if golden and not errs else []
    if not errs and any((run / name).exists() for name in ("PUBLIC-OK.csv", "QUESTIONS.md")):
        errs.append("finish refused: PUBLIC-OK.csv or QUESTIONS.md already exists")
    if errs:
        return print("\n".join(errs + ["errors=%d" % len(errs)])) or 1
    package = {r["rule_id"]: r["review"] for r in compare.law.load_package("us-ca")["rules"]}
    out, questions, public, rejected = io.StringIO(), [QLINE1], [PUBLIC], 0
    for f in findings:
        v = verify[f["finding_id"]]
        rejected += v["verdict"] == "reject"
        if v["verdict"] == "confirm" and f["rating"] == "strong" and \
                not any(package.get(r) == "needs_attorney_review" for r in compare.ids(f["rule_ids"])):
            public.append([f[k] for k in PUBLIC])
        if v["new_rating"] == "medium" or v["verdict"] == "downgrade":
            prior = sorted({facts[i]["request"].strip() for i in compare.ids(f["fact_ids"]) if i in facts} - {""})
            questions.append("- Q %s | %s | prior %s | %s" % (f["finding_id"], f["topic"], ";".join(prior) or "-", v["records_sought"]))
    csv.writer(out, lineterminator="\n").writerows(public)
    write(run / "PUBLIC-OK.csv", out.getvalue())
    write(run / "QUESTIONS.md", "\n".join(questions) + "\n")
    return print("public_ok=%d questions=%d rejected=%d" % (len(public) - 1, len(questions) - 1, rejected)) or 0

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["start", "finish"])
    parser.add_argument("rundir", nargs="?")
    for name in ("--check", "--golden", "--provider", "--model"):
        parser.add_argument(name)
    args = parser.parse_args(argv)
    target = args.check if not args.command else args.rundir
    if not target or (args.check and (args.command or args.rundir or args.golden or args.provider or args.model)) or \
            (args.command == "start" and (args.golden or not (args.provider or "").strip() or not (args.model or "").strip())) or \
            (args.command == "finish" and (args.provider or args.model)):
        parser.error("usage: start RUNDIR --provider P --model M | --check RUNDIR | finish RUNDIR [--golden CSV]")
    run = Path(target).resolve()
    if args.command and (run == REPO or REPO in run.parents):
        return print("flock-verify: RUNDIR is inside the repository") or 1
    refusal = family(run, args.provider if args.command == "start" else None)
    if refusal:
        return print(refusal) or 3
    if args.command == "start":
        return start(run, args.provider, args.model.strip())
    if args.command == "finish":
        return finish(run, args.golden)
    errs, _, _, verify = load(run)
    return print("\n".join(errs + ["verified=%d errors=%d" % (len(verify), len(errs))])) or (1 if errs else 0)

if __name__ == "__main__":
    sys.exit(main())
