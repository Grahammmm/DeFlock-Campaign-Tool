#!/usr/bin/env python3
"""Flock publish: a verified run's confirmed strong findings -> PUBLISH.json for human review (every entry pending).
Exit 1 = errors/empty, 2 = usage, 3 = family refusal. Only a human approves (skills/flock-publish).
export RUNDIR must lie outside the repository."""
import argparse, collections, contextlib, importlib.util, io, json, re, sys
from pathlib import Path
from urllib.parse import urlsplit
_spec = importlib.util.spec_from_file_location("_flock_verify", Path(__file__).resolve().parent / "flock_verify.py")
verify_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_tool)
compare = verify_tool.compare
_spec = importlib.util.spec_from_file_location("_flock_privacy", Path(__file__).resolve().parent / "flock_privacy.py")
privacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(privacy)
HEADLINE = "Research draft - not legal advice"
HOSTS = ("leginfo.legislature.ca.gov", "oag.ca.gov")
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
PRIVACY = (("email", privacy.EMAIL), ("phone", privacy.PHONE), ("url", re.compile(r"https?://|www\.", re.I)),
           ("plate", privacy.PLATE), ("hex", re.compile(r"[0-9a-f]{32,}", re.I)))
FINDING_KEYS = {"finding_id", "topic", "claim_class", "rating", "text", "cites", "review"}
TOP_KEYS = {"schema_version", "slug", "agency", "run_date", "header", "author_family", "verifier_family", "findings"}
def text_rules(text, vigilant_only):  # categories that refuse publication; GATED is banned even with a quote (PLAN §3)
    cats = [name for name, rx in ((("always", compare.ALWAYS), ("gated", compare.GATED)) + PRIVACY) if rx.search(text)]
    return cats + (["vendor"] if vigilant_only and compare.FLOCK.search(text) else [])
def matches(pattern, value):
    return isinstance(value, str) and bool(re.fullmatch(pattern, value))
def iso_day(value):
    return matches(r"\d{4}-\d{2}-\d{2}", value) and compare.law_policy.day(value)
def law_url_ok(url):
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        return bool(parts) and parts.scheme == "https" and parts.hostname in HOSTS
    except ValueError:
        return False
def eligible(run, findings, verify, package):  # re-derive PUBLIC-OK.csv: confirmed + strong + no attorney-review rule
    table = compare.read_csv(run / "PUBLIC-OK.csv") if (run / "PUBLIC-OK.csv").is_file() else []
    if not table or table[0] != verify_tool.PUBLIC:
        return [], ["PUBLIC-OK.csv: missing or header is not F4's"]
    by_id, ok, errs = {f["finding_id"]: f for f in findings}, [], []
    for rec in (r for r in table[1:] if any(c.strip() for c in r)):
        fid = rec[0].strip()
        f, v = by_id.get(fid), verify.get(fid, {})
        if not f or fid in ok or f["rating"] != "strong" or v.get("verdict") != "confirm" or \
                v.get("new_rating") != "strong" or any(package[r]["review"] == "needs_attorney_review" for r in compare.ids(f["rule_ids"])):
            errs.append("PUBLIC-OK.csv %s: not a confirmed strong finding" % fid[:40])
        ok.append(fid)
    return ok, errs
def cites_for(f, law_rows, policy_rows, package, facts):
    out = [{"kind": "law", "label": law_rows[r]["section"], "url": law_rows[r]["official_url"], "review": package[r]["review"]}
           for r in compare.ids(f["rule_ids"])]
    for ref in compare.ids(f["policy_refs"]):
        m = compare.POLICY_REF.fullmatch(ref)
        p = policy_rows[m.group(1)]
        out.append({"kind": "policy", "label": "Policy %s (%s), %s" % (p["policy_id"], p["version_label"], m.group(2).strip())})
    pages = {("Request %s, page %s" % (facts[i]["request"].strip(), facts[i]["page"].strip())) if facts[i]["request"].strip()
             else "Source page %s" % facts[i]["page"].strip() for i in compare.ids(f["fact_ids"])}
    return out + [{"kind": "record", "label": label} for label in sorted(pages)]
def derive(run, site_slug, new=False):  # export's checks and doc, no write -> (code, lines, doc); new: refuse an existing file
    if not (len(run.parents) >= 4 and run.parent.name == "runs" and run.parents[2].name == "agencies"):
        return 1, ["export refused: RUNDIR must be <store>/agencies/<slug>/runs/<date>"], None
    refusal = verify_tool.family(run)
    if refusal:
        return 3, [refusal], None
    target = run / "PUBLISH.json"
    if new and (target.is_symlink() or target.exists()):  # a dangling link is refused too, never replaced
        return 1, ["export refused: PUBLISH.json already exists"], None
    errs, findings, facts, verify = verify_tool.load(run)
    package = {r["rule_id"]: r for r in compare.law.load_package("us-ca")["rules"]}
    ok, more = eligible(run, findings, verify, package) if not errs else ([], [])
    if errs or more:
        return 1, errs + more + ["export refused: errors=%d" % len(errs + more)], None
    agency, store = run.parents[1], run.parents[3]
    law_rows = {r["rule_id"]: r for _, r in compare.law_policy.load_law(store / "LAW.csv")[0]}
    policy_rows = {r["policy_row_id"]: r for _, r in compare.law_policy.load_policy(agency / "POLICY.csv", agency.name)[0]}
    vendors = [(r["value"] + " " + r["verbatim_quote"]).lower() for r in facts.values() if r["fact_type"] == "vendor"]
    vigilant = any("vigilant" in v or "motorola" in v for v in vendors) and not any("flock" in v for v in vendors)
    out, lines = [], []
    for f in (f for f in findings if f["finding_id"] in ok):
        text, cites = compare.squash(f["claim"]), cites_for(f, law_rows, policy_rows, package, facts)
        cats = text_rules(text, vigilant) + (["law-host"] if any(c["kind"] == "law" and not law_url_ok(c["url"]) for c in cites) else [])
        if cats:
            lines.append("finding %s: refused %s" % (f["finding_id"], ",".join(cats)))
            continue
        out.append({"finding_id": f["finding_id"], "topic": f["topic"], "claim_class": f["claim_class"], "rating": "strong",
                    "text": text, "cites": cites,
                    "review": {"status": "pending", "approved_by": "", "approved_on": "", "approval_ref": ""}})
    if not out:
        return 1, lines + ["exported=0 refused=%d" % len(lines)], None
    meta = json.loads((run / "RUN.json").read_text(encoding="utf-8"))
    signed = json.loads((run / "VERIFY.json").read_text(encoding="utf-8"))
    doc = {"schema_version": 1, "slug": site_slug, "agency": agency.name, "run_date": run.name, "header": HEADLINE,
           "author_family": meta["author_provider"].strip().lower(), "verifier_family": signed["verifier_provider"].strip().lower(),
           "findings": out}
    return 0, lines + ["exported=%d refused=%d" % (len(out), len(lines))], doc
def export(run, site_slug):
    code, lines, doc = derive(run, site_slug, new=True)
    print("".join(line + "\n" for line in lines[:-1]), end="")
    if doc: verify_tool.write(run / "PUBLISH.json", json.dumps(doc, indent=2, ensure_ascii=False) + "\n")  # noqa: E701
    return print(lines[-1]) or code
def drift(doc, run):  # RF1130-2: every entry but its `review` must equal the run's re-derived export -> (notes, errors)
    code, lines, fresh = derive(run, doc["slug"])  # never echoes text, cite labels or urls
    if not fresh:
        return lines, ["run: cannot re-derive (exit %d)" % code]
    errs = ["file: %s differs from the run" % k for k in sorted(TOP_KEYS - {"findings"}) if doc[k] != fresh[k]]
    want, ids = {f["finding_id"]: f for f in fresh["findings"]}, [f["finding_id"] for f in doc["findings"]]
    for n, f in enumerate(doc["findings"], 1):
        fid, name = f["finding_id"], f["finding_id"] if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", f["finding_id"]) else "#%d" % n
        why = ["appears more than once in the file"] if fid in ids[:n - 1] else ["not in the run's eligible findings"] \
            if fid not in want else ["%s differs from the run" % k for k in ("text", "cites", "topic", "claim_class")
                                     if f[k] != want[fid][k]]
        errs += ["finding %s: %s" % (name, w) for w in why]
    missing = [fid for fid in want if fid not in ids]
    if any(f["review"]["status"] == "approved" for f in doc["findings"]):
        return [], errs + ["run: finding %s eligible but missing from the file" % fid[:40] for fid in missing]
    return ["note: run has %d eligible findings not in the file" % len(missing)] if missing else [], errs
def finding_errors(f):
    if not isinstance(f, dict) or set(f) != FINDING_KEYS:
        return ["keys differ from the schema"]
    errs = [] if f["rating"] == "strong" else ["rating is not strong"]
    if not isinstance(f["finding_id"], str) or not f["finding_id"].strip() or \
            not matches(r"[a-z0-9-]+", f["topic"]) or f["claim_class"] not in compare.CLASSES:
        errs.append("finding_id, topic or claim_class is invalid")
    errs += ["text refused " + c for c in text_rules(f["text"], False)] if isinstance(f["text"], str) and f["text"].strip() \
        else ["text is empty"]
    cites = f["cites"] if isinstance(f["cites"], list) else []
    if any(not isinstance(c, dict) or not isinstance(c.get("kind"), str) or
           set(c) != ({"kind", "label", "url", "review"} if c.get("kind") == "law" else {"kind", "label"}) or
           (c.get("kind") == "law" and c.get("review") not in ("verified", "likely", "needs_attorney_review")) for c in cites):
        return errs + ["cite keys or review differ from the schema"]
    kinds = [c["kind"] for c in cites]
    if any(k not in ("law", "policy", "record") or not isinstance(c["label"], str) or not c["label"].strip()
           for k, c in zip(kinds, cites)) or "record" not in kinds or not {"law", "policy"} & set(kinds):
        errs.append("cites need kind law/policy/record with labels, a law or policy cite and a record cite")
    if any(k == "law" and not law_url_ok(c["url"]) for k, c in zip(kinds, cites)):
        errs.append("law cite url is not https on " + " or ".join(HOSTS))
    r = f["review"] if isinstance(f["review"], dict) else {}
    if set(r) != {"status", "approved_by", "approved_on", "approval_ref"} or not all(isinstance(v, str) for v in r.values()):
        return errs + ["review keys or values differ from the schema"]
    if r["status"] not in ("pending", "approved"):
        errs.append("review status is not pending or approved")
    elif r["status"] == "approved" and not (r["approved_by"].strip() and r["approval_ref"].strip() and iso_day(r["approved_on"])):
        errs.append("approved needs approved_by, an ISO approved_on and approval_ref")
    return errs
def check(path, run=None):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or set(doc) != TOP_KEYS or type(doc["schema_version"]) is not int or doc["schema_version"] != 1 or \
            doc["header"] != HEADLINE or not all(matches(SLUG.pattern, doc[k]) for k in ("slug", "agency")) or \
            not iso_day(doc["run_date"]) or any(doc[k] not in compare.PROVIDERS for k in ("author_family", "verifier_family")) or \
            not isinstance(doc["findings"], list):
        return print("file: schema or header is invalid\nfindings=0 approved=0 errors=1" + (" drift=0" if run else "")) or 1
    errs, approved = [], 0
    for n, f in enumerate(doc["findings"], 1):
        fid = f.get("finding_id") if isinstance(f, dict) else None
        name = fid if isinstance(fid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", fid) else "#%d" % n
        errs += ["finding %s: %s" % (name, e) for e in finding_errors(f)]
        approved += isinstance(f, dict) and isinstance(f.get("review"), dict) and f["review"].get("status") == "approved"
    notes, moved = drift(doc, run) if run and not errs else ([], [])
    last = "findings=%d approved=%d errors=%d" % (len(doc["findings"]), approved, len(errs + moved))
    print("\n".join(notes + errs + moved + [last + (" drift=%d" % len(moved) if run else "")]))
    return 1 if errs or moved else 0
def stage(run):  # RF1130-3 -> (stage, counts); check's per-entry lines are captured, never printed
    if not (run / "PUBLISH.json").exists():
        return "no-export", None
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            check(run / "PUBLISH.json", run)
    except Exception:  # noqa: BLE001 - fail closed: a run that cannot be re-derived is drifted, counts unknown
        return "drifted", None
    n = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", buf.getvalue().splitlines()[-1])}
    counts = (n["findings"], n["findings"] - n["approved"], n["approved"], n["errors"])
    return ("invalid" if n["errors"] > n["drift"] else "drifted" if n["drift"] else "pending" if not n["approved"]
            else "approved" if n["approved"] == n["findings"] else "partly-approved"), counts
def status(store):  # RF1130-3: one publication-stage line per verified run (agencies/*/runs/*), read-only
    if (store / "agencies").is_symlink() or not (store / "agencies").is_dir():
        return print("status: no agencies folder", file=sys.stderr) or 2
    name, rows = lambda p: p.name if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", p.name) else "?", []  # noqa: E731
    for agency in sorted(store.joinpath("agencies").iterdir()):
        runs = agency / "runs"
        if agency.is_symlink() or runs.is_symlink():  # links are never followed
            rows.append((name(agency), "-", "link-refused", None))
            continue
        for run in sorted(runs.iterdir()) if runs.is_dir() else []:
            if run.is_symlink() or (run / "PUBLISH.json").is_symlink():
                rows.append((name(agency), name(run), "link-refused", None))
            elif run.is_dir() and (run / "VERIFY.json").exists():  # earlier stages belong to flock_sweep
                rows.append((name(agency), name(run)) + stage(run))
    tally, line = collections.Counter(row[2] for row in rows), "publish | %s | %s | %s | findings=%s pending=%s approved=%s errors=%s"
    print("".join(line % (row[:3] + (row[3] or ("-",) * 4)) + "\n" for row in rows), end="")
    print("publish | runs=%d %s | review-required; G decides any site use" % (len(rows), " ".join("%s=%d" % (
        k.replace("-", "_"), tally[k]) for k in ("no-export", "invalid", "drifted", "pending", "partly-approved", "approved"))))
    return 1 if tally["invalid"] or tally["drifted"] or tally["link-refused"] else 0
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["export", "status"])
    parser.add_argument("rundir", nargs="?")
    parser.add_argument("--site-slug"); parser.add_argument("--check"); parser.add_argument("--run")
    args = parser.parse_args(argv)
    checking = args.check and args.run != "" and not (args.command or args.rundir or args.site_slug)
    listing = args.command == "status" and args.rundir and (args.check, args.run, args.site_slug) == (None,) * 3
    if checking and args.run is None:
        return check(args.check)
    if not (checking or listing) and (args.command != "export" or not args.rundir or args.check or args.run is not None or
                                      not SLUG.fullmatch(args.site_slug or "")):
        parser.error("usage: export RUNDIR --site-slug S | --check FILE [--run RUNDIR] | status STORE")
    run = Path(args.run if checking else args.rundir).resolve()
    if run == verify_tool.REPO or verify_tool.REPO in run.parents:
        return print("flock-publish: %s is inside the repository" % ("STORE" if listing else "RUNDIR")) or 1
    try:
        return status(run) if listing else check(args.check, run) if checking else export(run, args.site_slug)
    except (OSError, ValueError) as exc:  # the class only, never a path or record text
        return print("io: %s" % type(exc).__name__) or 1
if __name__ == "__main__":
    sys.exit(main())
