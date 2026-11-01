#!/usr/bin/env python3
"""Flock verify: a different-family verifier's confirm/downgrade/reject per finding -> PUBLIC-OK.csv, QUESTIONS.md.
  start RUNDIR --provider P --model M  |  --check RUNDIR  |  finish RUNDIR [--golden CSV]  |  current RUNDIR [--golden CSV]
  diff OLD NEW  (two runs of one agency: added/removed/changed findings, public-ok lost/gained; read-only)
Exit 1 = errors, 2 = usage, 3 = family refusal (equal, missing or unknown); for diff, 3 = a public-ok row was lost.
Prints ids, labels and counts only.
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

def outputs(run, golden):  # (errs, PUBLIC-OK.csv text, QUESTIONS.md text, counts) that finish would write now
    errs, findings, facts, verify = load(run)
    final = [(n, dict(f, rating=verify[f["finding_id"]]["new_rating"])) for n, f in enumerate(findings, 2)
             if not errs and verify[f["finding_id"]]["verdict"] != "reject"]
    errs += compare.golden_errors(golden, final) if golden and not errs else []
    if errs:
        return errs, None, None, None
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
    return [], out.getvalue(), "\n".join(questions) + "\n", (len(public) - 1, len(questions) - 1, rejected)

def finish(run, golden):
    errs, public, questions, counts = outputs(run, golden)
    if errs or any((run / name).exists() for name in ("PUBLIC-OK.csv", "QUESTIONS.md")):
        errs = errs or ["finish refused: PUBLIC-OK.csv or QUESTIONS.md already exists"]
        return print("\n".join(errs + ["errors=%d" % len(errs)])) or 1
    write(run / "PUBLIC-OK.csv", public)
    try:  # finish refused above if either file existed, so this PUBLIC-OK.csv is ours to remove
        write(run / "QUESTIONS.md", questions)
    except BaseException:
        with contextlib.suppress(OSError): os.unlink(run / "PUBLIC-OK.csv")
        raise
    return print("public_ok=%d questions=%d rejected=%d" % counts) or 0

def on_disk(run, name, want):  # (bytes or None, state) of one output vs the fresh text; a symlink is never followed
    data = None if (run / name).is_symlink() or not (run / name).is_file() else (run / name).read_bytes()
    return data, "missing" if data is None else "current" if data == want.encode("utf-8") else "stale"

def current(run, golden):  # read-only: on-disk outputs vs a fresh finish; a symlink is never followed and counts as missing
    errs, public, questions, _ = outputs(run, golden)
    if errs:
        return print("\n".join(errs + ["current refused: errors=%d" % len(errs)])) or 1
    states, rows = [], lambda b: [r for r in csv.reader(io.StringIO(b.decode("utf-8", "replace"), newline="")) if any(c.strip() for c in r)]
    for name, want in (("PUBLIC-OK.csv", public), ("QUESTIONS.md", questions)):
        data, state = on_disk(run, name, want)
        states.append(state)
        print("current: %s %s" % (name, "is a symlink (refused)" if (run / name).is_symlink() else states[-1]))
        if states[-1] == "stale" and name == "PUBLIC-OK.csv":  # undecodable or wrong header: no rows on disk
            have, need = ([r[0].strip() for r in t[1:]] if t[:1] == [PUBLIC] else [] for t in (rows(data) if data.decode("utf-8", "replace").encode("utf-8") == data else [], rows(want.encode("utf-8"))))
            print("current: PUBLIC-OK.csv rows on_disk=%d expected=%d missing=%d extra=%d" % (len(have), len(need), len(set(need) - set(have)), len(set(have) - set(need))))
    return print("current=%d stale=%d missing=%d | outputs vs a fresh finish, not a review verdict" % tuple(map(states.count, ("current", "stale", "missing")))) or int(states != ["current"] * 2)

def outcomes(run):  # ({finding_id: (verdict, rating)}, public-ok ids, PUBLIC-OK text) of a fresh finish, or (error count, None, None)
    errs, public, _, _ = outputs(run, None)
    if errs:
        return len(errs), None, None
    _, findings, _, verify = load(run)
    return ({f["finding_id"]: (verify[f["finding_id"]]["verdict"], verify[f["finding_id"]]["new_rating"] or f["rating"]) for f in findings},
            {r[0].strip() for r in list(csv.reader(io.StringIO(public, newline="")))[1:]}, public)

def diff(olddir, newdir):  # read-only: verifier outcomes and public-ok ids of two runs of one agency; ids and labels only
    runs = {"OLD": Path(olddir), "NEW": Path(newdir)}
    if any(p.is_symlink() for p in runs.values()):
        return print("diff: link refused") or 1
    runs = {k: p.resolve() for k, p in runs.items()}
    if any(len(p.parents) < 2 or p.parent.name != "runs" for p in runs.values()) or runs["OLD"].parents[1] != runs["NEW"].parents[1]:
        return print("diff: runs belong to different agencies") or 1
    for label, run in runs.items():
        refusal = family(run)
        if refusal:
            return print("diff: %s %s" % (label, refusal)) or 1
    got = {label: outcomes(run) for label, run in runs.items()}
    for label in (label for label in got if got[label][1] is None):
        return print("diff: %s has %d error(s); run --check first" % (label, got[label][0])) or 1
    (a, ok_a, _), (b, ok_b, public) = got["OLD"], got["NEW"]
    state = on_disk(runs["NEW"], "PUBLIC-OK.csv", public)[1]
    out = ["diff: NEW PUBLIC-OK.csv on disk is %s; diff uses a fresh finish" % state] * (state != "current")
    n = dict.fromkeys(("added", "removed", "changed", "public_lost", "public_gained"), 0)
    for fid in sorted(set(a) | set(b)):
        kind = "added" if fid not in a else "removed" if fid not in b else "changed" if a[fid] != b[fid] else None
        if kind:
            n[kind] += 1
            out.append("diff | %s %s%s" % (kind, fid, " %s/%s -> %s/%s" % (a[fid] + b[fid]) if kind == "changed" else ""))
        for gone, key, word in ((fid in ok_a and fid not in ok_b, "public_lost", "lost"), (fid in ok_b and fid not in ok_a, "public_gained", "gained")):
            if gone:
                n[key] += 1
                out.append("diff | public-ok %s %s" % (word, fid))
    print("\n".join(out + ["diff | old=%d new=%d %s | ids only, not a review verdict" % (len(a), len(b), " ".join("%s=%d" % kv for kv in n.items()))]))
    return 3 if n["public_lost"] else 0

def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["diff"]:  # two run folders; the single-RUNDIR flow below is unchanged
        parser = argparse.ArgumentParser(prog="flock_verify.py diff", description="diff OLD NEW: two verified runs of one agency")
        parser.add_argument("old")
        parser.add_argument("new")
        args = parser.parse_args(argv[1:])
        try:
            return diff(args.old, args.new)
        except (OSError, UnicodeDecodeError, csv.Error) as exc:  # class name only
            return print("io: %s" % type(exc).__name__) or 1
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["start", "finish", "current"])
    parser.add_argument("rundir", nargs="?")
    for name in ("--check", "--golden", "--provider", "--model"):
        parser.add_argument(name)
    args = parser.parse_args(argv)
    target = args.check if not args.command else args.rundir
    if not target or (args.check and (args.command or args.rundir or args.golden or args.provider or args.model)) or \
            (args.command == "start" and (args.golden or not (args.provider or "").strip() or not (args.model or "").strip())) or \
            (args.command in ("finish", "current") and (args.provider or args.model)):
        parser.error("usage: start RUNDIR --provider P --model M | --check RUNDIR | finish RUNDIR [--golden CSV] | current RUNDIR [--golden CSV]")
    run = Path(target).resolve()
    if args.command in ("start", "finish") and (run == REPO or REPO in run.parents):  # current is read-only, like --check
        return print("flock-verify: RUNDIR is inside the repository") or 1
    refusal = family(run, args.provider if args.command == "start" else None)
    if refusal:
        return print(refusal) or 3
    try:
        if args.command == "start":
            return start(run, args.provider, args.model.strip())
        if args.command == "finish":
            return finish(run, args.golden)
        if args.command == "current":
            return current(run, args.golden)
        errs, _, _, verify = load(run)
        return print("\n".join(errs + ["verified=%d errors=%d" % (len(verify), len(errs))])) or (1 if errs else 0)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:  # class name only: paths and contents stay out of the output
        return print("io: %s" % type(exc).__name__) or 1

if __name__ == "__main__":
    sys.exit(main())
