#!/usr/bin/env python3
"""Flock report: one agency's FINDINGS.csv + its independent VERIFY.csv -> REPORT-DRAFT.md + SOURCES.json (numbered cites).
  build CONFIG [--out DIR]    (G1 config; default DIR is <compare.out>/report, refused inside the repository)
Rows from not-posted records and rows the verifier disputed or could not source never appear; a final lint refuses to write
otherwise. Prints counts and `line N: <rule>` / `row <id>: <rule>` errors only, never the offending text. No network.
Exit 0 written, 1 validation/lint/IO, 2 usage. The draft is private: public use goes through F9, redaction, review and G."""
import argparse, csv, hashlib, importlib.util, json, os, re, sys, tempfile
from collections import Counter
from pathlib import Path

TOOLS, REPO = Path(__file__).resolve().parent, Path(__file__).resolve().parents[1]
def _tool(name):  # F1/F3 are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_report_" + name, TOOLS / (name + ".py"))
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module
facts_tool, compare = _tool("flock_facts"), _tool("flock_compare")
HEADER = ("finding_id kind req_id source version section obligation topic requirement verdict basis fact_ids record_ids pages "
          "other_refs related_req_ids analysis_only next_step").split()
VHEADER = ["row_id", "result", "reason", "page_cite"]
LINE1 = "Research draft - not legal advice. Built from independently checked findings; review required before any use."
PHRASE = {"contradicted": "the records show the opposite", "not met": "not found in the policy or records",
          "met": "the records support it", "cannot tell": "the records do not show it either way"}
PRIORITY, KINDS = ("contradicted", "not met"), ("requirement", "claim", "setting", "comparison")
VERDICTS = ("met", "not met", "contradicted", "cannot tell", "n/a")
KEY = ("Verdict key. Met: the cited text or records show the requirement is satisfied (for a claim, the records support it). "
       "Not met: the required element is missing from the cited text or records, or the agency says it does not exist. "
       "Contradicted: a cited record or statement shows the opposite. Cannot tell: the produced records neither show nor rule it "
       "out. A missing record does not prove something did not happen. N/a: a definition, a remedy, permissive or discretionary "
       "text, or a comparator row.")
EMAIL, PHONE, GROUP = re.compile(r"\S+@\S+\.\w+"), re.compile(r"\(?\d{3}\)?[ .-]\d{3}-\d{4}"), re.compile(r"\[([^\[\]]*)\]")
COUNTS = ("included", "verified_agree", "unchecked", "withheld_analysis_only", "withheld_verify", "sources")
class Fail(Exception): pass
def P(value): return Path(os.path.expanduser(value)).resolve() if isinstance(value, str) and value.strip() else None
def under(path, root): return path == root or root in path.parents
def squash(text): return " ".join(str(text).split())
def clean(text): return squash(re.sub(r"[\[\],]", " ", text))  # cite details never carry brackets or commas
def table(path, header=None, need=()):  # -> [row dict]; header must match exactly or contain `need`
    with open(path, encoding="utf-8-sig", newline="") as handle:
        recs = [r for r in csv.reader(handle) if any(c.strip() for c in r)]
    if not recs or (header and recs[0] != header) or not set(need) <= set(recs[0]) or any(len(r) != len(recs[0]) for r in recs):
        raise Fail("%s: header is missing or wrong, or a row has the wrong column count" % path.name)
    return [{k: c.strip() for k, c in zip(recs[0], r)} for r in recs[1:]]
def stem(row):  # title without its extension
    ext = row.get("ext", "").lstrip(".")
    return row["title"][:-len(ext) - 1] if ext and row["title"].lower().endswith("." + ext.lower()) \
        else re.sub(r"\.[A-Za-z][A-Za-z0-9]{1,4}$", "", row["title"])
def context(config, out):
    try:
        cfg = json.loads(Path(config).read_text(encoding="utf-8"))
        fi, ri, cp, rv = (cfg.get(k) if isinstance(cfg.get(k), dict) else {} for k in ("facts_intake", "rules_intake", "compare", "review"))
    except (OSError, ValueError, AttributeError) as exc:
        raise Fail("config: CONFIG cannot be read (%s)" % type(exc).__name__) from None
    store, ro, co, slug, agency = P(fi.get("fact_store")), P(ri.get("out")), P(cp.get("out")), rv.get("slug"), fi.get("fact_agency")
    for field, ok in (("facts_intake.fact_store", store), ("rules_intake.out", ro), ("compare.out", co),
                      ("review.slug", isinstance(slug, str) and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug)),
                      ("facts_intake.fact_agency", isinstance(agency, str) and agency.strip())):
        if not ok:
            raise Fail("config: %s is missing or invalid" % field)
    deny = [cfg.get("not_posted_analysis_only_ids"), rv.get("report_denylist")]
    if not all(isinstance(d, (list, type(None))) and all(isinstance(s, str) for s in d or ()) for d in deny):
        raise Fail("config: not_posted_analysis_only_ids and review.report_denylist must be lists of strings")
    out = Path(os.path.abspath(os.path.expanduser(out))) if out else co / "report"
    if under(out.resolve(), REPO):
        raise Fail("out: is inside the repository")
    if not ((co / "VERIFY.csv").is_file() and (co / "VERIFY-PLAN.json").is_file()):
        raise Fail("verify: missing (run the G2 verify first)")
    return {"FO": store.parent, "RO": ro, "CO": co, "slug": slug, "agency": squash(agency), "out": out,
            "deny": [s for d in deny for s in d or ()]}
def load(c):
    raw = (c["CO"] / "FINDINGS.csv").read_bytes()
    plan = json.loads((c["CO"] / "VERIFY-PLAN.json").read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or plan.get("findings_sha256") != hashlib.sha256(raw).hexdigest():
        raise Fail("verify: VERIFY-PLAN findings_sha256 does not match FINDINGS.csv")
    findings, errs, verify = table(c["CO"] / "FINDINGS.csv", HEADER), [], {}
    for v in table(c["CO"] / "VERIFY.csv", VHEADER):
        if v["row_id"] in verify or v["result"] not in ("agree", "disagree", "needs-source"):
            errs.append("row %s: verify row is duplicated or its result is invalid" % v["row_id"])
        verify[v["row_id"]] = v["result"]
    v2, facts, ferrs = facts_tool.load(c["FO"] / "store" / "agencies" / c["slug"] / "FACTS.csv", c["slug"])
    if ferrs or not v2:
        raise Fail("facts: FACTS.csv has %d errors (check it with flock_facts.py --check)" % max(len(ferrs), 1))
    inventory = table(c["FO"] / "INVENTORY.csv", need=("request", "doc_id", "title", "sha256", "analysis_only"))
    reqs = {}
    for r in table(c["RO"] / "REQUIREMENTS.csv", need=("source", "version", "section", "req_id", "page")):
        reqs.setdefault(r["req_id"], r)
    return findings, plan, verify, {r["fact_id"]: r for _, r in facts}, inventory, reqs, errs
def render(c, findings, plan, verify, facts, inventory, reqs, errs):
    sample, n, shown = set(plan.get("sample_ids") or ()), Counter({k: 0 for k in COUNTS}), {"main": [], "sample": []}
    for f in findings:  # inclusion: first match wins
        res, fid = verify.get(f["finding_id"]), f["finding_id"]
        key = ("withheld_analysis_only" if f["analysis_only"] != "no" else "withheld_verify" if res in ("disagree", "needs-source")
               else "main" if f["verdict"] in PRIORITY and res == "agree" else "sample" if fid in sample and res == "agree"
               else "missing" if f["verdict"] in PRIORITY else "unchecked")
        shown.setdefault(key, []).append(f)
    n.update({k: len(shown.get(k, ())) for k in ("withheld_analysis_only", "withheld_verify", "unchecked")})
    errs += ["row %s: priority row has no verify result" % f["finding_id"] for f in shown.get("missing", ())]
    n["included"] = n["verified_agree"] = len(shown["main"]) + len(shown["sample"])
    by_sha, sources, marks = {}, {}, {}
    for r in inventory:
        by_sha.setdefault(r["sha256"], []).append(r)
    def source(key, **entry):
        return sources.setdefault(key, dict(entry, n=len(sources) + 1, pages=[], sections=[]))
    def bullet(f):
        fid, items = f["finding_id"], {}
        for fact_id in compare.ids(f["fact_ids"]):
            fact = facts.get(fact_id)
            inv = by_sha.get(fact["source_sha256"].strip(), []) if fact else []
            if not fact or not inv or any(r["analysis_only"] != "no" for r in inv):
                errs.append("row %s: %s" % (fid, "cited fact is not in FACTS.csv" if not fact else "cited fact's record has no "
                                            "INVENTORY row" if not inv else "cite resolves to an analysis-only record"))
                continue
            doc, page = inv[0], fact["page"].strip()
            e = source(("record", doc["sha256"]), kind="record", request=doc["request"], document_id=doc["doc_id"], sha256=doc["sha256"],
                       base="%sdocument %s (%s)" % ("Request %s, " % doc["request"] if doc["request"] else "", doc["doc_id"], stem(doc)))
            e["pages"] += [page] * (page not in e["pages"])
            items.setdefault(e["n"], []).append("p." + clean(page))
        req = reqs.get(f["req_id"]) if f["req_id"] else None
        if f["req_id"] and not req:
            errs.append("row %s: req_id is not in REQUIREMENTS.csv" % fid)
        elif req:
            e = source(("rule", req["source"], req["version"]), kind="rule", request=None, document_id=None, sha256=None,
                       base="%s, %s" % (req["source"], req["version"]))
            for value, key in ((req["section"], "sections"), (req["page"], "pages")):
                e[key] += [value] * bool(value and value not in e[key])
            items.setdefault(e["n"], []).append(clean("section " + req["section"] + (" page " + req["page"] if req["page"] else "")))
        errs.extend("row %s: %s" % (fid, why) for why, bad in (("bullet has no cite", not items),
                                                               ("verdict has no report phrase", f["verdict"] not in PHRASE)) if bad)
        cite = "[%s]" % ", ".join("%d %s" % (k, " and ".join(dict.fromkeys(v))) for k, v in items.items())
        return squash("- **%s** - %s. %s %s" % (squash(f["requirement"]), PHRASE.get(f["verdict"], ""), squash(f["basis"]), cite))
    lines = [LINE1, "", "# %s license-plate reader records review - findings draft" % c["agency"], "", "## What this covers", "",
             "Rows reviewed: %d. Rows from records not posted publicly: %d (withheld, counted only). Rows the independent check "
             "disputed or could not source: %d (withheld). Rows not independently checked: %d (counted only, not described). Rows "
             "shown below: %d, each agreed by the independent check." % (len(findings), n["withheld_analysis_only"],
                                                                        n["withheld_verify"], n["unchecked"], n["included"]), "", KEY, ""]
    for title, key in (("## Main results", "main"), ("## Checked in the sample", "sample")):
        lines += [title, ""]
        for f in sorted(shown[key], key=lambda r: (KINDS.index(r["kind"]) if r["kind"] in KINDS else 9, r["finding_id"])):
            marks[len(lines)] = f["finding_id"]
            lines.append(bullet(f))
        lines += ([] if shown[key] else ["None."]) + [""]
    grid = Counter((f["kind"], f["verdict"]) for f in findings)  # counts over all rows, withheld included
    kinds = [k for k in KINDS if any(g[0] == k for g in grid)] + sorted({g[0] for g in grid} - set(KINDS))
    cols = list(VERDICTS) + sorted({g[1] for g in grid} - set(VERDICTS))
    lines += ["## Counts", "", "| kind | %s | total |" % " | ".join(cols), "|---" * (len(cols) + 2) + "|"]
    lines += ["| %s | %s | %d |" % (k, " | ".join(str(grid[(k, v)]) for v in cols), sum(grid[(k, v)] for v in cols)) for k in kinds]
    body_end, entries = len(lines), []
    for e in sorted(sources.values(), key=lambda e: e["n"]):
        e["pages"].sort(key=lambda v: (not v.isdigit(), int(v) if v.isdigit() else 0, v))
        extra = (", section " + "; ".join(e["sections"]) if e["sections"] else "") + (", page " + ", ".join(e["pages"]) if e["pages"] else "")
        entries.append({"n": e["n"], "kind": e["kind"], "label": squash(e["base"] + extra), "request": e["request"],
                        "document_id": e["document_id"], "sha256": e["sha256"], "pages": e["pages"]})
    lines += ["", "## Sources", ""] + ["[%d] %s" % (e["n"], e["label"]) for e in entries]
    n["sources"] = len(entries)
    return "\n".join(lines) + "\n", json.dumps(entries, indent=1, ensure_ascii=False) + "\n", marks, body_end, entries, n
def lint(c, report, sources_text, marks, body_end, entries, facts, inventory, errs):  # on the full rendered text of both files
    hidden = [r for r in inventory if r["analysis_only"] != "no"]
    deny = [s.lower() for s in c["deny"] + [v for r in hidden for v in (r["doc_id"], r["sha256"], stem(r))] if s.strip()]
    vendors = [(r["value"] + " " + r["verbatim_quote"]).lower() for r in facts.values() if r["fact_type"] == "vendor"]
    vigilant = any("vigilant" in v or "motorola" in v for v in vendors) and not any("flock" in v for v in vendors)
    def hits(line):
        found = ["denylisted string"] * any(d in line.lower() for d in deny)
        found += [name for name, rx in (("always-banned wording", compare.ALWAYS), ("quote-gated wording", compare.GATED),
                                        ("email-like string", EMAIL), ("phone-like string", PHONE)) if rx.search(line)]
        return found + ["vendor-gated wording"] * bool(vigilant and compare.FLOCK.search(line))
    cited, listed = {}, {e["n"] for e in entries}
    for i, line in enumerate(report.splitlines()):
        where = "row %s" % marks[i] if i in marks else "line %d" % (i + 1)
        errs += ["%s: %s" % (where, h) for h in hits(line)]
        for group in GROUP.findall(line) if i < body_end else ():
            for item in (item for item in group.split(", ") if re.match(r"\d+( |$)", item)):
                cited.setdefault(int(item.split()[0]), where)
    errs += ["%s: cite has no SOURCES entry" % where for k, where in cited.items() if k not in listed]
    errs += ["line %d: SOURCES entry never cited" % (body_end + 4 + i) for i, e in enumerate(entries) if e["n"] not in cited]
    errs += ["line %d: %s (SOURCES.json)" % (i, h) for i, line in enumerate(sources_text.splitlines(), 1) for h in hits(line)]
def write(path, text):  # fresh exclusive 0600 temp (mkstemp: O_EXCL, random name, never reuses/follows one); caller renames both after
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
        out.write(text)
    return tmp, path
def build(config, out):
    errs, n = [], Counter({k: 0 for k in COUNTS})
    try:
        c = context(config, out)
        findings, plan, verify, facts, inventory, reqs, errs = load(c)
        report, sources_text, marks, body_end, entries, n = render(c, findings, plan, verify, facts, inventory, reqs, errs)
        lint(c, report, sources_text, marks, body_end, entries, facts, inventory, errs)
        if not errs:
            c["out"].mkdir(mode=0o700, parents=True, exist_ok=True)
            for tmp, path in [write(c["out"] / "REPORT-DRAFT.md", report), write(c["out"] / "SOURCES.json", sources_text)]:
                os.replace(tmp, path)
    except (Fail, OSError, ValueError, KeyError, TypeError) as exc:
        errs.append(str(exc) if isinstance(exc, Fail) else "io: %s" % type(exc).__name__)
    print("\n".join((errs := list(dict.fromkeys(errs))) + [" ".join("%s=%d" % (k, n[k]) for k in COUNTS) + " errors=%d" % len(errs)]))
    return 1 if errs else 0
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["build"]), parser.add_argument("config"), parser.add_argument("--out")
    args = parser.parse_args(argv)
    return build(args.config, args.out)
if __name__ == "__main__":
    sys.exit(main())
