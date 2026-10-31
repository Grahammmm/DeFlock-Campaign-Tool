#!/usr/bin/env python3
"""Flock review verify: a reproducible verify plan, a plan-coverage check and an upstream-count recheck for one FINDINGS.csv.
  plan FINDINGS.csv --seed N --size K --out PLAN.json
  check PLAN.json FINDINGS.csv VERIFY.csv [--not-posted ID ...] [--md VERIFY.md]
  counts FINDINGS.csv BUILD-REPORT.txt
Priority = every contradicted / not met row; sample = random.Random(seed).sample of the met / cannot tell rows (file order).
Exit 0 ok, 1 errors, 2 usage. Prints ids, labels and counts only, never reason, basis, requirement or quote text.
A verify pass is not acceptance: machine-valid output is review-required."""
import argparse, ast, collections, contextlib, csv, hashlib, json, os, random, re, sys, tempfile
from pathlib import Path

VERDICTS = ("met", "not met", "contradicted", "cannot tell", "n/a")  # = flock_report.VERDICTS; n/a rows are neither priority nor pool
HEADER, RESULTS = ["row_id", "result", "reason", "page_cite"], ("agree", "disagree", "needs-source")
CHECKS = ["findings freeze: FINDINGS.csv sha256 must equal the plan's; a changed FINDINGS needs a new plan",
          "sample: reproducible random.Random(seed).sample of met and cannot tell ids; never re-sample after seeing results",
          "priority: every contradicted and not met row is checked, none sampled",
          "coverage: every planned id has exactly one row; a partial pass fails with missing ids",
          "log totals: by-user summary sums are not full-log totals; recount from the source workbook",
          "not-posted ids stay analysis-only and out of VERIFY.csv and VERIFY.md"]

class PlanError(Exception): pass
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def table(path):
    with open(path, newline="", encoding="utf-8-sig") as handle: return list(csv.reader(handle))  # BOM is not header text; sha() hashes raw bytes
def findings(path):
    with open(path, newline="", encoding="utf-8-sig") as handle: return list(csv.DictReader(handle))
def cell(row, name): return (row.get(name) or "").strip()
def load_plan(path):
    try: spec = json.loads(Path(path).read_text(encoding="utf-8"))  # a missing or unreadable file (OSError) stays an io: error
    except ValueError: spec = None  # not JSON, or not UTF-8
    ids = lambda v: isinstance(v, list) and all(isinstance(i, str) for i in v)  # noqa: E731
    if not (isinstance(spec, dict) and spec.get("schema") == 1 and ids(spec.get("priority_ids")) and ids(spec.get("sample_ids"))
            and isinstance(spec.get("findings_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", spec["findings_sha256"])):
        raise PlanError("plan: PLAN.json is not a verify plan")
    return spec

def plan(args):
    rows, digest, out = findings(args.findings), sha(args.findings), Path(args.out)
    if out.exists():
        try: prior = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError): prior = None
        if not isinstance(prior, dict) or prior.get("findings_sha256") != digest:
            return print("plan: %s exists for other findings" % out.name) or 1
        if prior.get("seed") != args.seed or prior.get("sample_size") != args.size:
            return print("plan: %s exists with different parameters" % out.name) or 1
        return print("plan: %s reused (frozen)" % out.name) or 0
    recs, errs, seen = table(args.findings), [], set()  # every row is judged before anything is written; header is row 1
    for n, rec in enumerate(recs[1:], 2):
        if not rec: continue  # a blank line, skipped by DictReader as well
        r = dict(zip(recs[0], rec))
        if cell(r, "verdict").lower() not in VERDICTS: errs.append("row %d: verdict is unknown" % n)
        if not cell(r, "finding_id"): errs.append("row %d: finding_id is empty" % n)
        elif cell(r, "finding_id") in seen: errs.append("row %d: finding_id is duplicated" % n)
        seen.add(cell(r, "finding_id"))
    if errs: return print("\n".join(errs + ["plan: errors=%d" % len(errs)])) or 1
    ids = lambda *verdicts: [cell(r, "finding_id") for r in rows if cell(r, "verdict").lower() in verdicts]  # noqa: E731
    priority, pool = ids("contradicted", "not met"), ids("met", "cannot tell")
    sample = random.Random(args.seed).sample(pool, min(args.size, len(pool)))
    body = {"schema": 1, "seed": args.seed, "sample_size": args.size, "findings_sha256": digest, "pool_size": len(pool),
            "priority_ids": priority, "sample_ids": sample, "checks": CHECKS}
    fd, tmp = tempfile.mkstemp(prefix="." + out.name + ".", suffix=".tmp", dir=out.parent)  # fresh temp, never a fixed .tmp
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle: handle.write(json.dumps(body, indent=1) + "\n")
        os.replace(tmp, out)
    except BaseException:
        with contextlib.suppress(OSError): os.unlink(tmp)
        raise
    print("priority=%d pool=%d sample=%d findings_sha256=%s" % (len(priority), len(pool), len(sample), digest))
    return 0

def check(args):
    spec = load_plan(args.plan)
    want, errs, seen, tally, extra, rows = spec["priority_ids"] + spec["sample_ids"], [], set(), collections.Counter(), 0, 0
    if sha(args.findings) != spec["findings_sha256"]:
        errs.append("plan: findings changed since plan")
    verify = table(args.verify)
    if not verify or verify[0] != HEADER:
        errs.append("row 1: header is not " + ",".join(HEADER))
        verify = [HEADER]
    for n, rec in enumerate(verify[1:], 2):
        if not any(c.strip() for c in rec):
            continue
        rows += 1
        if len(rec) != len(HEADER):
            errs.append("row %d: columns expected %d, found %d" % (n, len(HEADER), len(rec)))
            continue
        rid, result, reason, cite = (c.strip() for c in rec)
        if rid in seen: errs.append("row %d: row_id is duplicated" % n)
        elif rid not in want: errs, extra = errs + ["row %d: row_id is extra (not in the plan)" % n], extra + 1
        seen.add(rid)
        if result not in RESULTS: errs.append("row %d: result is invalid" % n)
        else: tally[result] += 1
        errs += ["row %d: %s is empty" % (n, name) for name, value in (("reason", reason), ("page_cite", cite)) if not value]
    missing = [i for i in want if i not in seen]
    errs += ["plan: missing %s" % i for i in missing]
    for label, path in (("VERIFY.csv", args.verify), ("VERIFY.md", args.md)):
        if path is None: continue
        try: text = Path(path).read_text(encoding="utf-8")
        except OSError: errs.append("%s: file is missing" % label); continue
        if any(i and i in text for i in args.not_posted): errs.append("not-posted: %s contains a configured not-posted id" % label)
    print("\n".join(errs + ["planned=%d rows=%d agree=%d disagree=%d needs_source=%d missing=%d extra=%d errors=%d" % (
        len(want), rows, tally["agree"], tally["disagree"], tally["needs-source"], len(missing), extra, len(errs))]))
    return 1 if errs else 0

def counts(args):
    rows, text, errs = findings(args.findings), Path(args.report).read_text(encoding="utf-8"), []
    kinds = {}
    for r in rows: kinds.setdefault(cell(r, "kind"), collections.Counter())[cell(r, "verdict")] += 1
    found = re.search(r"^findings=(\d+)\b", text, re.M)
    if not found or int(found.group(1)) != len(rows): errs.append("counts: findings differs")
    for kind, tally in kinds.items():
        line = next((x for x in text.splitlines() if x.startswith(kind + ": ")), "")
        try:
            total, literal = line[len(kind) + 2:].split(" ", 1)
            same = int(total) == sum(tally.values()) and ast.literal_eval(literal) == dict(tally)
        except (ValueError, SyntaxError):
            same = False
        if not same: errs.append("counts: %s differs" % kind)
    only = re.search(r"^analysis_only rows: (\d+)\b", text, re.M)
    if not only or int(only.group(1)) != sum(cell(r, "analysis_only").lower() == "yes" for r in rows): errs.append("counts: analysis_only rows differs")
    print("\n".join(errs + ["counts: findings=%d kinds=%d errors=%d" % (len(rows), len(kinds), len(errs))]))
    return 1 if errs else 0

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan"); p.add_argument("findings"); p.add_argument("--seed", type=int, required=True)
    p.add_argument("--size", type=int, required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("check"); [p.add_argument(n) for n in ("plan", "findings", "verify")]
    p.add_argument("--not-posted", nargs="*", default=[]); p.add_argument("--md")
    p = sub.add_parser("counts"); p.add_argument("findings"); p.add_argument("report")
    args = parser.parse_args(argv)
    if args.command == "plan" and args.size < 0: parser.error("plan: --size must be 0 or more")
    try:
        return {"plan": plan, "check": check, "counts": counts}[args.command](args)
    except PlanError as exc:
        return print(exc) or 1
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:
        return print("io: %s" % type(exc).__name__) or 1

if __name__ == "__main__":
    sys.exit(main())
