#!/usr/bin/env python3
"""Flock report: one agency's FINDINGS.csv + its independent VERIFY.csv -> REPORT-DRAFT.md + SOURCES.json (numbered cites).
  build CONFIG [--out DIR]    (G1 config; default DIR is <compare.out>/report, refused inside the repository)
  check CONFIG [--out DIR]    (read-only: are REPORT-DRAFT.md + SOURCES.json byte-equal to a fresh build? last line state=)
  region --matrix DIR --agency SLUG=CONFIG ... --out DIR [--replace]   (private REGION-REPORT-DRAFT.md + SOURCES.json from
      COMPARE-MATRIX.csv; one --agency per matrix column; an agency whose report is stale or missing is excluded, counted only)
Rows from not-posted records and rows the verifier disputed or could not source never appear; a final lint refuses to write
otherwise. Prints counts and `line N: <rule>` / `row <id>: <rule>` errors only, never the offending text. No network.
Exit 0 written (check: current), 1 validation/lint/IO (check: stale/missing/invalid), 2 usage. The draft is private: public use
goes through F9, redaction, review and G."""
import argparse, contextlib, csv, errno, hashlib, importlib.util, json, os, re, stat, sys, tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

TOOLS, REPO = Path(__file__).resolve().parent, Path(__file__).resolve().parents[1]
def _tool(name):  # F1/F3 are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_report_" + name, TOOLS / (name + ".py"))
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module
facts_tool, compare, privacy = _tool("flock_facts"), _tool("flock_compare"), _tool("flock_privacy")
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
EMAIL, PHONE, PLATE, GROUP = privacy.EMAIL, privacy.PHONE, privacy.PLATE, re.compile(r"\[([^\[\]]*)\]")
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
                                        ("email-like string", EMAIL), ("phone-like string", PHONE),
                                        ("plate-like string", PLATE)) if rx.search(line)]
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
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            out.write(text)
    except BaseException:  # never leave our own temp behind, then re-raise
        with contextlib.suppress(OSError): os.unlink(tmp)
        raise
    return tmp, path
def fault(exc): return str(exc) if isinstance(exc, Fail) else "io: %s" % type(exc).__name__
def summary(errs, n): return "\n".join(errs + [" ".join("%s=%d" % (k, n[k]) for k in COUNTS) + " errors=%d" % len(errs)])
def prepare(config, out):  # build and check share it: -> (c, report, sources_text, n, errs); writes nothing
    c, report, sources_text, errs, n = None, None, None, [], Counter({k: 0 for k in COUNTS})
    try:
        c = context(config, out)
        findings, plan, verify, facts, inventory, reqs, errs = load(c)
        c.update(findings=findings, facts=facts, inventory=inventory)  # region reads these back
        report, sources_text, marks, body_end, entries, n = render(c, findings, plan, verify, facts, inventory, reqs, errs)
        c.update(marks=marks, entries=entries)
        lint(c, report, sources_text, marks, body_end, entries, facts, inventory, errs)
    except (Fail, OSError, ValueError, KeyError, TypeError) as exc:
        errs.append(fault(exc))
    return c, report, sources_text, n, errs
def on_disk(path):  # -> bytes, or why not; read-only, O_NOFOLLOW never follows a link, O_NONBLOCK never waits on a fifo
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as handle:
            return handle.read() if stat.S_ISREG(os.fstat(handle.fileno()).st_mode) else "is not a regular file (refused)"
    except OSError as exc:
        return {errno.ENOENT: "missing (run build)", errno.ELOOP: "is a link (refused)"}.get(exc.errno, "unreadable (%s)"
                                                                                               % type(exc).__name__)
def check(config, out):  # never writes, never prints draft text: names files, counts and the build errors only
    c, report, sources_text, n, errs = prepare(config, out)
    state = disk_state(c, report, sources_text, errs)
    print(summary(errs := list(dict.fromkeys(errs)), n) + " state=" + state)
    return 1 if errs else 0
def disk_state(c, report, sources_text, errs):  # check and region: -> invalid|missing|stale|current; appends check: errors
    state = "invalid"
    if not errs:
        names = (("REPORT-DRAFT.md", report), ("SOURCES.json", sources_text))
        got = {name: on_disk(c["out"] / name) for name, _ in names}
        missing = [k for k, v in got.items() if isinstance(v, str) and v.startswith("missing")]
        hard = [k for k, v in got.items() if isinstance(v, str) and k not in missing]  # link / not a file / unreadable first
        errs += ["check: %s %s" % (k, got[k]) for k in hard or missing]
        errs += [] if hard or missing else ["check: %s differs from a fresh build (rerun build)" % k for k, text in names
                                            if got[k] != text.encode("utf-8")]
        state = "invalid" if hard else "missing" if missing else "stale" if errs else "current"
    return state
def build(config, out):
    c, report, sources_text, n, errs = prepare(config, out)
    try:
        if not errs:
            c["out"].mkdir(mode=0o700, parents=True, exist_ok=True)
            made, renamed = [], []
            try:
                for name, text in (("REPORT-DRAFT.md", report), ("SOURCES.json", sources_text)):
                    made.append(write(c["out"] / name, text))
                for tmp, path in made:
                    os.replace(tmp, path)
                    renamed.append(tmp)
            except BaseException as exc:  # unlink only temps we created and did not rename; foreign .tmp files stay
                for tmp in (tmp for tmp, _ in made if tmp not in renamed):
                    with contextlib.suppress(OSError): os.unlink(tmp)
                if len(renamed) == 1 and isinstance(exc, Exception):
                    raise Fail("io: partial write (REPORT-DRAFT.md replaced, SOURCES.json not); rerun build") from exc
                raise
    except (Fail, OSError, ValueError, KeyError, TypeError) as exc:
        errs.append(fault(exc))
    print(summary(errs := list(dict.fromkeys(errs)), n))
    return 1 if errs else 0
ORDER, RNAMES = {"rejected": 0, "weak": 1, "medium": 2, "strong": 3}, ("REGION-REPORT-DRAFT.md", "SOURCES.json")
RNOTE = ("Ratings come from the cross-agency matrix. Each agency is cited only from its own current findings draft, and only "
         "findings on that topic. Rejected, unrated, not current and uncited agencies are counted, never named.")
def region(args, parser):  # -> exit code; reads every agency draft, writes nothing unless every check and the lint pass
    configs, errs, agencies, out = {}, [], {}, Path(os.path.abspath(os.path.expanduser(args.out)))
    for slug, eq, config in (a.partition("=") for a in args.agency):
        if not (eq and slug and config) or slug in configs:
            parser.error("--agency %s: give SLUG=CONFIG once per matrix column" % slug)
        configs[slug] = config
    try:
        path = Path(os.path.expanduser(args.matrix)) / "COMPARE-MATRIX.csv"
        rows = table(path, need=("topic", "claim_class"))
        with open(path, encoding="utf-8-sig", newline="") as handle:
            head = next(csv.reader(handle))
        slugs = head[2:]
        if head[:2] != ["topic", "claim_class"] or len(set(slugs)) != len(slugs) or not slugs:
            raise Fail("matrix: COMPARE-MATRIX.csv header is not topic, claim_class, then each agency slug once")
        if any(v not in ORDER and v != "-" for r in rows for v in (r[s] for s in slugs)) or not rows:
            raise Fail("matrix: COMPARE-MATRIX.csv has no rows or a cell that is not strong|medium|weak|rejected|-")
    except (Fail, OSError, ValueError, StopIteration) as exc:
        print(fault(exc) if not isinstance(exc, StopIteration) else "matrix: COMPARE-MATRIX.csv is empty")
        return 1
    for name, bad in (("matrix column %s has no --agency", [s for s in slugs if s not in configs]),
                      ("--agency %s has no matrix column", [s for s in configs if s not in slugs])):
        if bad:
            parser.error(name % ", ".join(bad))
    for slug in slugs:  # the existing check, in-process: invalid stops the region; stale or missing excludes the agency
        c, report, sources_text, _, aerrs = prepare(configs[slug], None)
        aerrs += ["config: review.slug is not the matrix column"] * bool(not aerrs and c["slug"] != slug)
        agencies[slug] = c, report, disk_state(c, report, sources_text, aerrs) if not aerrs else "invalid"
        errs += ["agency %s: %s" % (slug, e) for e in aerrs] if agencies[slug][2] == "invalid" else []
    loaded = [c for c, _, _ in agencies.values() if c and "inventory" in c]
    if under(out.resolve(), REPO):
        errs.append("out: is inside the repository")
    elif any(under(out.resolve(), c[k].resolve()) for c in loaded for k in ("FO", "CO")):
        errs.append("out: is inside an agency store")
    if errs:
        print("\n".join(dict.fromkeys(errs)))
        return 1
    current = [s for s in slugs if agencies[s][2] == "current"]
    cites = {}  # (slug, topic) -> that agency's SOURCES numbers cited by its shown findings on the topic
    for slug in current:
        c, report, _ = agencies[slug]
        topic, lines = {f["finding_id"]: f["topic"] for f in c["findings"]}, report.splitlines()
        for i, fid in c["marks"].items():
            items = GROUP.findall(lines[i])[-1].split(", ")
            cites.setdefault((slug, topic[fid]), set()).update(int(t.split()[0]) for t in items if re.match(r"\d+( |$)", t))
    best = {}
    for r in rows:
        for s in (s for s in slugs if r[s] != "-"):
            best[r["topic"], s] = max(r[s], best.get((r["topic"], s), r[s]), key=ORDER.__getitem__)
    number, entries = {}, []
    def cite(slug, topic):
        for k in sorted(cites[slug, topic]):
            if (slug, k) not in number:
                e = next(e for e in agencies[slug][0]["entries"] if e["n"] == k)
                number[slug, k] = len(number) + 1
                entries.append({"n": number[slug, k], "agency": slug, "kind": e["kind"], "label": e["label"], "pages": e["pages"]})
        return "%s [%s]" % (clean(agencies[slug][0]["agency"]), ", ".join(str(number[slug, k]) for k in sorted(cites[slug, topic])))
    lines = [LINE1, "", "# Regional license-plate reader records review - cross-agency draft", "", RNOTE, ""]
    for topic in sorted({r["topic"] for r in rows}):
        count, shown = Counter(), {k: [] for k in ("strong", "medium", "weak")}
        for s in slugs:
            b = best.get((topic, s))
            key = ("not current" if s not in current else "no rating" if not b else "rejected" if b == "rejected"
                   else "no current source" if (s, topic) not in cites else b)
            shown[key].append(s) if key in shown else count.update([key])
        lines.append("- %s: %s; %s" % (clean(topic), "; ".join("%s %s" % (k, ", ".join(cite(s, topic) for s in v) or "none")
                                                              for k, v in shown.items()),
                                        ", ".join("%s: %d" % (k, count[k]) for k in ("rejected", "no rating", "not current",
                                                                                      "no current source"))))
    body_end = len(lines)
    lines += ["", "## Sources", ""] + ["[%d] %s: %s" % (e["n"], clean(agencies[e["agency"]][0]["agency"]), e["label"]) for e in entries]
    report, sources_text = "\n".join(lines) + "\n", json.dumps(entries, indent=1, ensure_ascii=False) + "\n"
    combined = {"deny": [d for c in loaded for d in c["deny"]]}  # every agency's hidden ids and denylist, current or not
    facts = {(c["slug"], k): v for c in loaded for k, v in c["facts"].items()}
    lint(combined, report, sources_text, {}, body_end, entries, facts, [r for c in loaded for r in c["inventory"]], errs)
    for name in RNAMES:
        if os.path.lexists(out / name) and not (args.replace and stat.S_ISREG(os.lstat(out / name).st_mode)):
            errs.append("out: %s %s" % (name, "exists; pass --replace to supersede it" if not args.replace else "is not a regular file"))
    if errs:
        print("\n".join(dict.fromkeys(errs)))
        return 1
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    made, stamp = [], datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        for name, text in zip(RNAMES, (report, sources_text)):
            made.append(write(out / name, text))
        for tmp, target in made:
            if os.path.lexists(target):
                os.link(target, target.with_name(target.name + ".superseded-" + stamp))  # the old file is kept aside, never deleted
            os.replace(tmp, target)
    except OSError as exc:
        for tmp in (tmp for tmp, _ in made if os.path.lexists(tmp)):
            with contextlib.suppress(OSError): os.unlink(tmp)
        print(fault(exc))
        return 1
    print("region topics=%d agencies=%d current=%d excluded=%d sources=%d" % (
        len({r["topic"] for r in rows}), len(slugs), len(current), len(slugs) - len(current), len(entries)))
    return 0
def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["region"]:
        rp = argparse.ArgumentParser(prog="flock_report.py region", description="Private cross-agency draft from COMPARE-MATRIX.csv.")
        rp.add_argument("--matrix", required=True), rp.add_argument("--out", required=True)
        rp.add_argument("--agency", action="append", required=True, metavar="SLUG=CONFIG")
        rp.add_argument("--replace", action="store_true")
        return region(rp.parse_args(argv[1:]), rp)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["build", "check"]), parser.add_argument("config"), parser.add_argument("--out")
    args = parser.parse_args(argv)
    return (check if args.command == "check" else build)(args.config, args.out)
if __name__ == "__main__":
    sys.exit(main())
