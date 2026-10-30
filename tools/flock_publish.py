#!/usr/bin/env python3
"""Flock publish: a verified run's confirmed strong findings -> PUBLISH.json for human review (every entry pending).
  export RUNDIR --site-slug S  |  --check FILE
Exit 1 = errors or nothing exportable, 2 = usage, 3 = family refusal. Prints ids, categories and counts only.
The tool never writes "approved": a human sets it after redaction review and G's approval (skills/flock-publish)."""
import argparse, importlib.util, json, re, sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

_spec = importlib.util.spec_from_file_location("_flock_verify", Path(__file__).resolve().parent / "flock_verify.py")
verify_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_tool)
compare = verify_tool.compare
HEADLINE = "Research draft - not legal advice"
HOSTS = ("leginfo.legislature.ca.gov", "oag.ca.gov")
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
PRIVACY = (("email", re.compile(r"[^\s@]+@[^\s@]+\.[a-z]{2,}", re.I)), ("phone", re.compile(r"\(?\d{3}\)?[ .-]\d{3}-\d{4}")),
           ("url", re.compile(r"https?://|www\.", re.I)), ("plate", re.compile(r"\b\d[A-Z]{3}\d{3}\b")),
           ("hex", re.compile(r"[0-9a-f]{32,}", re.I)))
FINDING_KEYS = {"finding_id", "topic", "claim_class", "rating", "text", "cites", "review"}
TOP_KEYS = {"schema_version", "slug", "agency", "run_date", "header", "author_family", "verifier_family", "findings"}

def text_rules(text, vigilant_only):  # categories that refuse publication; GATED is banned even with a quote (PLAN §3)
    cats = [name for name, rx in ((("always", compare.ALWAYS), ("gated", compare.GATED)) + PRIVACY) if rx.search(text)]
    return cats + (["vendor"] if vigilant_only and compare.FLOCK.search(text) else [])

def law_url_ok(url):
    parts = urlsplit(url) if isinstance(url, str) else None
    return bool(parts) and parts.scheme == "https" and parts.hostname in HOSTS

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

def export(run, site_slug):
    refusal = verify_tool.family(run)
    if refusal:
        return print(refusal) or 3
    target = run / "PUBLISH.json"
    if target.exists():
        return print("export refused: PUBLISH.json already exists") or 1
    errs, findings, facts, verify = verify_tool.load(run)
    package = {r["rule_id"]: r for r in compare.law.load_package("us-ca")["rules"]}
    ok, more = eligible(run, findings, verify, package) if not errs else ([], [])
    if errs or more:
        return print("\n".join(errs + more + ["export refused: errors=%d" % len(errs + more)])) or 1
    agency, store = run.parents[1], run.parents[3]
    law_rows = {r["rule_id"]: r for _, r in compare.law_policy.load_law(store / "LAW.csv")[0]}
    policy_rows = {r["policy_row_id"]: r for _, r in compare.law_policy.load_policy(agency / "POLICY.csv", agency.name)[0]}
    vendors = [(r["value"] + " " + r["verbatim_quote"]).lower() for r in facts.values() if r["fact_type"] == "vendor"]
    vigilant = any("vigilant" in v or "motorola" in v for v in vendors) and not any("flock" in v for v in vendors)
    out, refused = [], 0
    for f in (f for f in findings if f["finding_id"] in ok):
        text, cites = compare.squash(f["claim"]), cites_for(f, law_rows, policy_rows, package, facts)
        cats = text_rules(text, vigilant) + (["law-host"] if any(c["kind"] == "law" and not law_url_ok(c["url"]) for c in cites) else [])
        if cats:
            refused += 1
            print("finding %s: refused %s" % (f["finding_id"], ",".join(cats)))
            continue
        out.append({"finding_id": f["finding_id"], "topic": f["topic"], "claim_class": f["claim_class"], "rating": "strong",
                    "text": text, "cites": cites,
                    "review": {"status": "pending", "approved_by": "", "approved_on": "", "approval_ref": ""}})
    if not out:
        return print("exported=0 refused=%d" % refused) or 1
    meta = json.loads((run / "RUN.json").read_text(encoding="utf-8"))
    signed = json.loads((run / "VERIFY.json").read_text(encoding="utf-8"))
    doc = {"schema_version": 1, "slug": site_slug, "agency": agency.name, "run_date": run.name, "header": HEADLINE,
           "author_family": meta["author_provider"].strip().lower(), "verifier_family": signed["verifier_provider"].strip().lower(),
           "findings": out}
    verify_tool.write(target, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return print("exported=%d refused=%d" % (len(out), refused)) or 0

def finding_errors(f):
    if not isinstance(f, dict) or set(f) != FINDING_KEYS:
        return ["keys differ from the schema"]
    errs = [] if f["rating"] == "strong" else ["rating is not strong"]
    errs += ["text refused " + c for c in text_rules(f["text"], False)] if isinstance(f["text"], str) and f["text"].strip() \
        else ["text is empty"]
    cites = f["cites"] if isinstance(f["cites"], list) else []
    kinds = [c.get("kind") if isinstance(c, dict) else None for c in cites]
    if any(k not in ("law", "policy", "record") or not isinstance(c.get("label"), str) or not c["label"].strip()
           for k, c in zip(kinds, cites)) or "record" not in kinds or not {"law", "policy"} & set(kinds):
        errs.append("cites need kind law/policy/record with labels, a law or policy cite and a record cite")
    if any(k == "law" and not law_url_ok(c.get("url")) for k, c in zip(kinds, cites)):
        errs.append("law cite url is not https on " + " or ".join(HOSTS))
    r = f["review"] if isinstance(f["review"], dict) else {}
    on = r.get("approved_on") if isinstance(r.get("approved_on"), str) else ""
    if r.get("status") not in ("pending", "approved"):
        errs.append("review status is not pending or approved")
    elif r["status"] == "approved" and not (all(isinstance(r.get(k), str) and r[k].strip() for k in ("approved_by", "approval_ref"))
                                            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", on) and compare.law_policy.day(on)):
        errs.append("approved needs approved_by, an ISO approved_on and approval_ref")
    return errs

def check(path):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or set(doc) != TOP_KEYS or doc["schema_version"] != 1 or doc["header"] != HEADLINE or \
            not isinstance(doc["slug"], str) or not SLUG.fullmatch(doc["slug"]) or not isinstance(doc["findings"], list):
        return print("file: schema or header is invalid\nfindings=0 approved=0 errors=1") or 1
    errs, approved = [], 0
    for n, f in enumerate(doc["findings"], 1):
        fid = f.get("finding_id") if isinstance(f, dict) else None
        name = fid if isinstance(fid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", fid) else "#%d" % n
        errs += ["finding %s: %s" % (name, e) for e in finding_errors(f)]
        approved += isinstance(f, dict) and isinstance(f.get("review"), dict) and f["review"].get("status") == "approved"
    print("\n".join(errs + ["findings=%d approved=%d errors=%d" % (len(doc["findings"]), approved, len(errs))]))
    return 1 if errs else 0

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["export"])
    parser.add_argument("rundir", nargs="?")
    parser.add_argument("--site-slug")
    parser.add_argument("--check")
    args = parser.parse_args(argv)
    if args.check and not (args.command or args.rundir or args.site_slug):
        return check(args.check)
    if args.command != "export" or not args.rundir or args.check or not SLUG.fullmatch(args.site_slug or ""):
        parser.error("usage: export RUNDIR --site-slug S | --check FILE")
    return export(Path(args.rundir).resolve(), args.site_slug)

if __name__ == "__main__":
    sys.exit(main())
