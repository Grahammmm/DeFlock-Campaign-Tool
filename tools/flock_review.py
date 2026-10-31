#!/usr/bin/env python3
"""Flock review: one agency through the agency-review method as a fixed 28-stage resumable pipeline.
  --check CONFIG [--seed-dir DIR] | run CONFIG [--until STAGE] [--seed-dir DIR] | status CONFIG | mark CONFIG STAGE --provider P --model M
Runs the private method scripts and F1/F3 tools, skipping stages whose inputs are unchanged; a wait stage closes only by mark (the card that
wrote the input states its provider/model). Never runs a model, never deletes (old outputs move to <out>/.review/superseded), no network.
status ends a stale/waiting/failed/pending line with (<reason>): the changed fingerprint part or output relpath, the wait reason, rc or need.
Prints stage ids, statuses, rc and paths only. Exit 0 done, 1 failed/validation/lock/IO, 2 usage, 3 waiting. done is not review or release."""
import argparse, contextlib, datetime, hashlib, importlib.util, json, os, re, subprocess, sys, tempfile
from pathlib import Path

TOOLS, REPO = Path(__file__).resolve().parent, Path(__file__).resolve().parents[1]
def _tool(name):  # F1/F3/F11 are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_review_" + name, TOOLS / (name + ".py"))
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module
facts_tool, compare_tool, region_tool = _tool("flock_facts"), _tool("flock_compare"), _tool("flock_region")
FI, RI, CP = "facts_intake", "rules_intake", "compare"
SLUG, DIGITS, MODEL = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*"), re.compile(r"[0-9]+"), re.compile(r"[A-Za-z0-9._:-]{1,80}")
STAGES = [  # (id, kind, deps) -- frozen; G2 appends after counts and never renumbers
    ("inventory", "run", ()), ("logs", "run", ("inventory",)), ("roster", "run", ("inventory",)), ("ocr-logs", "run", ("inventory",)),
    ("fact-drafts", "wait", ("inventory",)), ("facts-build", "run", ("logs", "roster", "ocr-logs", "fact-drafts")),
    ("facts-import", "run", ("facts-build",)), ("rules-read", "wait", ()), ("rules-build", "run", ("rules-read",)),
    ("rules-diff", "run", ("rules-build",)), ("rules-selftest", "run", ("rules-build",)), ("joins", "run", ("logs",)),
    ("assess", "wait", ("facts-import", "rules-build", "joins")), ("compare-build", "run", ("facts-import", "rules-build", "joins", "assess")),
    ("compare-selftest", "run", ("compare-build",)), ("f3-findings", "wait", ("facts-import", "rules-build")),
    ("f3-scaffold", "run", ("facts-import", "rules-build", "f3-findings")), ("f3-write", "run", ("f3-scaffold",)), ("f3-check", "run", ("f3-write",)),
    ("counts", "run", ("compare-build",)),
    ("verify-plan", "run", ("compare-build",)), ("verify-recount", "run", ("logs", "joins")), ("verify", "wait", ("verify-plan", "verify-recount")),
    ("verify-check", "run", ("verify",)), ("f4-start", "run", ("f3-check",)), ("f4", "wait", ("f4-start",)), ("f4-finish", "run", ("f4",)),
    # Report reads inventory, facts, requirements, findings, the verify plan and marked verification rows directly.
    ("report", "run", ("inventory", "facts-import", "rules-build", "compare-build", "verify-plan", "verify", "verify-check"))]
IDS = [s[0] for s in STAGES]
METHOD = {n + ".py": d + "/scripts/" + n + ".py" for d, names in (
    ("facts-intake", "inventory log_summary roster_summary ocr_log_counts facts_build"), ("rules-intake", "rules_build diff_versions check_rules"),
    ("compare", "training_vs_search compare_build check_compare f3_from_yaml summarize")) for n in names.split()}
SKIP = hashlib.sha256(b"skip").hexdigest()
class Fail(Exception): code = 1
class Usage(Fail): code = 2
class Refused(Fail): code = 3  # family refusal at mark
S = lambda v: v if isinstance(v, str) else ""  # noqa: E731
def P(value, real=True):  # ~-expanded path, or None when unset
    path = os.path.expanduser(value) if S(value).strip() else None
    return path and (Path(path).resolve() if real else Path(os.path.abspath(path)))
def under(path, root): return bool(path and root) and (path == root or root in path.parents)
def digest(obj): return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
def fhash(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path and Path(path).is_file() else "missing"
def now(fmt="%Y-%m-%dT%H:%M:%SZ"): return datetime.datetime.now(datetime.timezone.utc).strftime(fmt)
def block(i): return FI if i < 7 else RI if i < 11 else CP
def root(c, i): return c["FO"] if i < 7 else c["RO"] if i < 11 else c["CO"]
def put(path, data):  # atomic 0600 write
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)  # fresh temp, never a fixed .tmp
    try:
        with os.fdopen(fd, "wb") as out: out.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError): os.unlink(tmp)
        raise
def families(cfg):  # non-comparison policy sources by family (first-seen order), ids by footer_version_date
    found, sources = {}, cfg[RI].get("sources")
    for src in sources if isinstance(sources, list) else ():
        if isinstance(src, dict) and src.get("kind") == "policy" and not src.get("comparison_only") and src.get("family"):
            found.setdefault(str(src["family"]), []).append(src)
    return [(f, [str(s.get("id")) for s in sorted(v, key=lambda s: str(s.get("footer_version_date", "")))]) for f, v in found.items()]
def context(path):
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Fail("config: CONFIG cannot be read (%s)" % type(exc).__name__) from None
    if not isinstance(cfg, dict) or not all(isinstance(cfg.get(k), dict) for k in ("review", FI, RI, CP)) or not isinstance(cfg[CP].get("f3"), dict):
        raise Fail("config: review facts_intake rules_intake compare compare.f3 must be objects")
    r, cp, f3, store = cfg["review"], cfg[CP], cfg[CP]["f3"], P(cfg[FI].get("fact_store"))
    c = {"cfg": cfg, "path": Path(path).resolve(), "out": P(r.get("out")), "FO": store and store.parent, "RO": P(cfg[RI].get("out")),
         "CO": P(cp.get("out")), "slug": S(r.get("slug")), "date": S(r.get("run_date")), "by": S(r.get("added_by")),
         "method": P(r.get("method_dir")), "py": P(r.get("method_python"), False), "roster": r.get("roster_doc_id"),
         "ocr": r.get("ocr_log_doc_ids") or [], "tj": r.get("training_join"), "f3f": P(r.get("f3_findings")), "store": P(f3.get("store")),
         "law": P(f3.get("law")), "policy": P(f3.get("policy")), "facts": P(cp.get("facts")), "assess": P(cp.get("assessments")),
         "xlsx": P(cp.get("roster_xlsx")), "lbu": P(cp.get("log_by_user")), "golden": f3.get("golden"), "appl": S(f3.get("applicability"))}
    c["RUN"] = c["store"] and c["store"] / "agencies" / c["slug"] / "runs" / c["date"]
    c["skip"] = {sid for sid, off in (("roster", c["roster"] is None), ("ocr-logs", not c["ocr"]), ("rules-diff", not families(cfg)),
                                      ("joins", c["tj"] is None)) if off}
    return c
def check(c, seed_dir=None):  # -> ["config: <field> <reason>", ...]
    cfg, r, cp, f3, slug = c["cfg"], c["cfg"]["review"], c["cfg"][CP], c["cfg"][CP]["f3"], c["slug"]
    FO, RO, CO = (c[k] or Path("/nonexistent") for k in ("FO", "RO", "CO"))
    missing, golden = [n for n, rel in METHOD.items() if not (c["method"] and (c["method"] / rel).is_file())], c["golden"]
    rules = [("review.schema", r.get("schema") == 1, "is not 1"), ("review.slug", SLUG.fullmatch(slug), "is not a lowercase slug"),
             ("facts_intake.fact_agency", facts_tool.slugify(S(cfg[FI].get("fact_agency"))) == slug, "does not match review.slug"),
             ("rules_intake.agency_name", facts_tool.slugify(S(cfg[RI].get("agency_name"))) == slug, "does not match review.slug"),
             ("compare.f3.slug", S(f3.get("slug")) == slug, "does not match review.slug"),
             ("review.run_date", facts_tool.valid_date(c["date"]), "is not a valid YYYY-MM-DD"), ("review.added_by", c["by"].strip(), "is empty"),
             ("review.method_python", c["py"] and c["py"].is_file() and os.access(c["py"], os.X_OK), "is not an executable file"),
             ("review.method_dir", not missing, "lacks " + ", ".join(missing)),
             ("review.out", c["out"] and not under(c["out"], REPO), "is missing or inside the repository"),
             ("CONFIG", not under(c["path"], REPO), "is inside the repository")]
    rules += [(f, under(c[k], c["out"]), "is not under review.out") for f, k in (("facts_intake.fact_store", "FO"), ("rules_intake.out", "RO"),
                                                                                  ("compare.out", "CO"))]
    rules += [(f, have == want, "is not the expected path under its step output") for f, have, want in (
        ("compare.facts", c["facts"], FO / "store" / "agencies" / slug / "FACTS.csv"), ("compare.facts_meta", P(cp.get("facts_meta")), FO / "FACTS-META.csv"),
        ("compare.requirements", P(cp.get("requirements")), RO / "REQUIREMENTS.csv"),
        ("compare.version_diff", P(cp.get("version_diff")), RO / "VERSION-DIFF.csv"), ("compare.f3.law", c["law"], RO / "F2" / "LAW.csv"),
        ("compare.f3.policy", c["policy"], RO / "F2" / "agencies" / slug / "POLICY.csv"), ("compare.assessments", c["assess"], CO / "ASSESSMENTS.yaml"))]
    rules += [(f, under(have, base) and have != base, "is not under its step output") for f, have, base in (
        ("compare.log_by_user", c["lbu"], FO), ("compare.f3.store", c["store"], CO), ("review.f3_findings", c["f3f"], CO))]
    sample = r.get("verify_sample", 30)
    rules += [("review.verify_sample", isinstance(sample, int) and not isinstance(sample, bool) and 1 <= sample <= 100, "is not an integer 1-100"),
              ("compare.f3.applicability", 1 <= len(c["appl"]) <= 200, "is not 1-200 chars"),
              ("compare.f3.golden", golden is None or (S(golden) and not Path(golden).is_absolute() and under((REPO / golden).resolve(), REPO)
                                                       and (REPO / golden).is_file()), "is not a repo-relative file"),
              ("compare.roster_xlsx", c["tj"] is None or (c["xlsx"] and c["xlsx"].is_file()), "is missing (training_join set)"),
              ("review.roster_doc_id", c["roster"] is None or DIGITS.fullmatch(S(c["roster"])), "is not null or a digit string"),
              ("review.training_join", c["tj"] is None or DIGITS.fullmatch(S(c["tj"])), "is not null or a digit string"),
              ("review.ocr_log_doc_ids", isinstance(c["ocr"], list) and all(DIGITS.fullmatch(S(v)) for v in c["ocr"]), "is not a list of digit strings")]
    if r.get("region_config") is not None:
        try:
            region = region_tool.load_config(P(r.get("region_config")) or "")
            why = (region_tool.check(region, seed_dir)[0] or [None])[0]
        except (region_tool.Fail, TypeError) as exc:
            region, why = {}, str(exc)
        if not why and not any(isinstance(a, dict) and a.get("slug") == slug and a.get("in_scope") == "yes" and a.get("applicability") == c["appl"]
                               for a in region.get("agencies", ())):
            why = "has no in-scope agency with review.slug and compare.f3.applicability"
        rules.append(("region_config", not why, why))
    return ["config: %s %s" % (field, why) for field, ok, why in rules if not ok]
def plan(c, sid, state):  # -> (steps, stdout capture); a step is ("run", argv) | ("copy", src, dst) | ("aside", path)
    m = lambda n, *a: ("run", [str(c["py"]), "-B", str(c["method"] / METHOD[n])] + [str(x) for x in a])  # noqa: E731
    t = lambda n, *a: ("run", [sys.executable, "-B", str(TOOLS / n)] + [str(x) for x in a])  # noqa: E731
    cfg, FO, CO, slug, store = c["path"], c["FO"], c["CO"], c["slug"], c["store"]
    if sid == "facts-import":
        return [("aside", FO / "store")] + [t("flock_facts.py", "import", f, "--store", FO / "store", "--kind", f.name[9:-4], "--added-by", c["by"],
                                              "--status", "unreviewed", "--date", c["date"]) for f in sorted(FO.glob("FACTS-v1-*.csv"))] \
            + [t("flock_facts.py", "--check", FO / "store")], None
    if sid == "f3-scaffold":
        mark = (state["stages"].get("f3-findings") or {}).get("mark") or {}
        return [("copy", c["facts"], store / "agencies" / slug / "FACTS.csv"), ("copy", c["law"], store / "LAW.csv"),
                ("copy", c["policy"], store / "agencies" / slug / "POLICY.csv"), ("aside", c["RUN"]),
                t("flock_compare.py", "scaffold", "--store", store, "--agency", slug, "--date", c["date"], "--provider", mark.get("provider", ""),
                  "--model", mark.get("model", ""), "--applicability", c["appl"])], None
    if sid == "rules-diff":
        return [m("diff_versions.py", cfg, f, *ids) for f, ids in families(c["cfg"])], c["RO"] / "VERSION-WORDDIFF.txt"
    golden = ["--golden", REPO / c["golden"]] if c["golden"] else []
    rv, fnd, RUN, rc = "flock_review_verify.py", CO / "FINDINGS.csv", c["RUN"], CO / "verify-recount"
    mk = (state["stages"].get("verify") or {}).get("mark") or {}  # f4-start takes the verifier from the verify mark
    if sid == "verify-plan":
        return [("stale", CO / "VERIFY-PLAN.json", fnd), t(rv, "plan", fnd, "--seed", int(c["date"].replace("-", "")), "--size", c["cfg"]["review"].get("verify_sample", 30),
                                                          "--out", CO / "VERIFY-PLAN.json"), t(rv, "counts", fnd, CO / "BUILD-REPORT.txt")], None
    if sid == "verify-recount":  # re-run the method scripts into a verify-owned folder and byte-compare with the originals
        join = [m("training_vs_search.py", c["xlsx"], rc / "LOG-SUMMARY-by-user.csv"), ("cmpout", CO / ("TRAINING-VS-SEARCH-%s.txt" % c["tj"]))] if c["tj"] is not None else []
        return [("mkdir", rc), m("log_summary.py", cfg, rc), ("same", FO, rc, "LOG-SUMMARY-*.csv")] + join, None
    if sid == "verify-check":
        ids = c["cfg"].get("not_posted_analysis_only_ids") or []
        return [t(rv, "check", CO / "VERIFY-PLAN.json", fnd, CO / "VERIFY.csv", "--not-posted", *map(str, ids), "--md", CO / "VERIFY.md")], CO / "VERIFY-CHECK.txt"
    if sid == "f4-start":
        who = (mk.get("provider", ""), mk.get("model", ""))
        return [("identity", RUN / "VERIFY.json", *who), t("flock_verify.py", "start", RUN, "--provider", who[0], "--model", who[1])], None
    if sid == "f4-finish":
        return [("aside", RUN / "PUBLIC-OK.csv"), ("aside", RUN / "QUESTIONS.md"), t("flock_verify.py", "--check", RUN),
                t("flock_verify.py", "finish", RUN, *golden)], None
    return {"inventory": ([m("inventory.py", cfg, FO)], None), "logs": ([m("log_summary.py", cfg, FO)], None),
            "roster": ([m("roster_summary.py", cfg, FO, c["roster"])], None), "ocr-logs": ([m("ocr_log_counts.py", cfg, FO, *c["ocr"])], None),
            "facts-build": ([m("facts_build.py", cfg, FO, TOOLS / "flock_facts.py", *sorted(FO.glob("drafts/*.yaml")))], None),
            "rules-build": ([m("rules_build.py", cfg, "--f2-tool", TOOLS / "flock_law_policy.py")], None),
            "rules-selftest": ([m("check_rules.py", cfg)], None),
            "joins": ([m("training_vs_search.py", c["xlsx"], c["lbu"])], CO / ("TRAINING-VS-SEARCH-%s.txt" % c["tj"])),
            "compare-build": ([m("compare_build.py", cfg)], None), "compare-selftest": ([m("check_compare.py", cfg, CO / "selftest")], None),
            "f3-write": ([m("f3_from_yaml.py", c["f3f"], c["RUN"])], None), "f3-check": ([t("flock_compare.py", "--check", c["RUN"], *golden)], None),
            "counts": ([m("summarize.py", CO / "FINDINGS.csv")], CO / "COUNTS.txt"), "report": ([("aside", CO / "report"), t("flock_report.py", "build", cfg)], None)}[sid]
def listing(path):  # (relpath, size, mtime_ns) of one external input root
    files = [] if path is None or not path.exists() else [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    return [str(path), [[str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns] for p in files] if path and path.exists() else "missing"]
def external(c, sid):
    fi = c["cfg"][FI]
    if sid == "inventory":
        items = [e for k in ("record_roots", "ocr_text_dirs", "extra_sources") for e in fi.get(k) or []]
        paths = [e.get("path") if isinstance(e, dict) else e for e in items] + [fi.get("portal_manifest"), fi.get("source_manifest")]
        return [listing(P(p)) for p in paths if S(p)]
    return fhash(c["xlsx"]) if sid in ("joins", "verify-recount") and c["tj"] is not None else fhash(REPO / c["golden"]) if sid == "f3-check" and c["golden"] else None
def fingerprint_parts(c, i, dig, state):  # stage, argv, script hashes, config blocks, external inputs and the dep OUTPUT digests
    sid, kind, deps = STAGES[i]
    body = {"stage": sid, "deps": [dig[d] for d in deps], "external": external(c, sid),
            "config": digest([c["cfg"]["review"], c["cfg"].get("not_posted_analysis_only_ids"), c["cfg"][block(i)]])}
    if kind == "run":
        steps, capture = plan(c, sid, state)
        body.update(capture=str(capture), steps=[s[1] if s[0] == "run" else [s[0]] + [str(x) for x in s[1:]] for s in steps],
                    scripts=[fhash(s[1][2]) for s in steps if s[0] == "run"])
        if sid == "rules-build": body["scripts"].append(fhash(TOOLS / "flock_law_policy.py"))
    return body
def fingerprint(c, i, dig, state): return digest(fingerprint_parts(c, i, dig, state))
def later_outputs(c, state, i):  # a file a later stage rewrote or a later mark covers is verified there
    return {root(c, j) / rel for j in range(i + 1, len(STAGES)) for rel in (state["stages"].get(IDS[j]) or {}).get("outputs", {})}
def wait_files(c, sid):  # the model-authored inputs a mark covers (set config paths only)
    if sid == "fact-drafts": return [p for p in sorted(c["FO"].glob("drafts/*.yaml")) if not p.name.endswith("-computed.yaml")]
    if sid == "verify": return [c["CO"] / "VERIFY.csv", c["CO"] / "VERIFY.md"]
    if sid == "f4": return [c["RUN"] / "VERIFY.csv"]
    if sid != "rules-read": return [c["assess"] if sid == "assess" else c["f3f"]]
    ri = c["cfg"][RI]
    values = [s.get(k) for s in ri.get("sources") or [] if isinstance(s, dict) for k in ("text", "pdf")]
    return [P(v) for v in values + [*(ri.get("law_specs") or []), *(ri.get("policy_specs") or []), ri.get("f2_law_map"),
                                    *(ri.get("extra_specs") or [])] if S(v)]
def evaluate(c, state, dig, i):  # -> (state, reason, fp); dig gains the stage's output digest when done or skip
    sid, kind, deps = STAGES[i]
    if sid in c["skip"]: return dig.update({sid: SKIP}) or ("skip", None, None)
    if any(d not in dig for d in deps): return "pending", None, None
    fp, rec = fingerprint(c, i, dig, state), state["stages"].get(sid) or {}
    if kind == "wait":
        mark = rec.get("mark")
        why = "not marked" if not mark else "changed since mark" if {str(p): fhash(p) for p in wait_files(c, sid)} != mark["files"] \
            else "upstream changed" if mark["fp"] != fp else None
        return ("waiting", why, fp) if why else dig.update({sid: digest(mark["files"])}) or ("done", None, fp)
    later = later_outputs(c, state, i)
    if rec.get("status") == "done" and rec.get("fp") == fp \
            and all(root(c, i) / rel in later or fhash(root(c, i) / rel) == h for rel, h in rec.get("outputs", {}).items()):
        return dig.update({sid: digest(rec["outputs"])}) or ("done", None, fp)
    return ("failed" if rec.get("status") == "failed" else "stale" if rec else "pending"), None, fp
PARTS = (("deps", "upstream changed"), ("external", "inputs changed"), ("config", "config changed"), ("steps", "command changed"), ("capture", "command changed"), ("scripts", "scripts changed"))
def why_not_done(c, state, dig, i, word, fp):  # read-only status reason for a failed/pending/stale stage (wait stages keep evaluate's why)
    rec = state["stages"].get(IDS[i]) or {}
    if word == "failed": return "rc=%s" % rec.get("rc")
    if word == "pending": return next(("needs " + d for d in STAGES[i][2] if d not in dig), "never run")
    if word != "stale" or not isinstance(rec.get("parts"), dict): return "inputs or outputs changed" if word == "stale" else None
    cur = {k: digest(v) for k, v in fingerprint_parts(c, i, dig, state).items()}
    moved = list(dict.fromkeys(text for k, text in PARTS if rec["parts"].get(k) != cur.get(k)))
    base, later = root(c, i), later_outputs(c, state, i)
    bad = sorted(rel for rel, h in rec.get("outputs", {}).items() if base / rel not in later and fhash(base / rel) != h)
    if moved or not bad: return ", ".join(moved) or "inputs or outputs changed"
    return "output %s: %s%s" % ("missing" if fhash(base / bad[0]) == "missing" else "changed", bad[0], " +%d" % (len(bad) - 1) if len(bad) > 1 else "")
def snap(base, out):  # before/after listing of a step root; .review and the state file are excluded
    found = {}
    for dirpath, dirnames, files in os.walk(base):
        dirnames[:] = [d for d in dirnames if Path(dirpath, d) != out / ".review"]
        for path in (Path(dirpath, n) for n in files if Path(dirpath) != out or not n.startswith("REVIEW-STATE.json")):
            found[path] = (path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ino)
    return found
def aside(c, sid, path):  # never delete: move superseded outputs to .review/superseded/<stage>-<UTC>
    dest = base = c["out"] / ".review" / "superseded" / ("%s-%s" % (sid, now("%Y%m%dT%H%M%SZ")))
    while dest.exists(): dest = Path("%s-%d" % (base, len(list(base.parent.glob(base.name + "*")))))
    if path.exists():
        dest.mkdir(mode=0o700, parents=True)
        os.replace(path, dest / path.name)
def local(c, sid, step, last):  # non-subprocess steps -> names that differ (compare steps only)
    kind, path = step[0], step[1]
    if kind == "aside": aside(c, sid, path)
    elif kind == "copy": put(step[2], path.read_bytes())
    elif kind == "mkdir": path.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif kind == "stale":  # a verify plan made for other FINDINGS moves aside, never edited
        try: fresh = json.loads(path.read_text(encoding="utf-8"))["findings_sha256"] == fhash(step[2])
        except (OSError, ValueError, KeyError, TypeError): fresh = False
        if path.exists() and not fresh: aside(c, sid, path)
    elif kind == "identity":  # an F4 start for another verifier moves aside with its VERIFY.csv
        try: have = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError): have = None
        if path.exists() and (not isinstance(have, dict) or (have.get("verifier_provider"), have.get("verifier_model")) != tuple(step[2:4])):
            aside(c, sid, path), aside(c, sid, path.with_name("VERIFY.csv"))
    elif kind == "same":  # byte-compare step[3] files of two folders
        names = sorted({p.name for d in (path, step[2]) for p in d.glob(step[3])})
        return [n for n in names if not (path / n).is_file() or not (step[2] / n).is_file() or (path / n).read_bytes() != (step[2] / n).read_bytes()]
    elif kind == "cmpout": return [] if path.is_file() and path.read_bytes() == last else [path.name]
    return []
def execute(c, i, state):
    sid, base = IDS[i], root(c, i)
    steps, capture = plan(c, sid, state)
    log, before, rc, logged, stdout = c["out"] / ".review" / "logs" / (sid + ".log"), snap(base, c["out"]), 0, [], []
    for step in steps:
        try:
            if step[0] != "run":
                bad = local(c, sid, step, stdout[-1] if stdout else b"")
                for name in bad:
                    print("recount: %s differs" % name)
                    logged.append(("# differs: %s\n" % name).encode())
                if bad:
                    rc = 1
                    break
                continue
            done = subprocess.run(step[1], stdin=subprocess.DEVNULL, capture_output=True, cwd=str(c["out"]), check=False)
            logged += [("# %s\n" % Path(step[1][2]).name).encode(), done.stdout, done.stderr]
            stdout, rc = stdout + [done.stdout], done.returncode
        except OSError as exc:
            logged, rc = logged + [("# %s failed: %s\n" % (step[0], type(exc).__name__)).encode()], 1
        if rc: break
    if capture and not rc: put(capture, b"".join(stdout))
    put(log, b"".join(logged))
    return rc, {str(p.relative_to(base)): fhash(p) for p, v in snap(base, c["out"]).items() if before.get(p) != v and not (sid == "f4-start" and p.name == "VERIFY.csv")}, log
def load_state(c):
    path = c["out"] / "REVIEW-STATE.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema": 1, "slug": c["slug"], "stages": {}}
        if state["schema"] == 1 and state["slug"] == c["slug"] and isinstance(state["stages"], dict): return state
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise Fail("state: %s is unreadable or for another agency" % path)
def save(c, state):  # rewritten only when its content changes
    path, text = c["out"] / "REVIEW-STATE.json", json.dumps(state, indent=1, sort_keys=True) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != text: put(path, text.encode())
def locked(c, body):
    lock = c["out"] / ".review" / "LOCK"
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with os.fdopen(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle: handle.write("%d\n" % os.getpid())
    except FileExistsError:
        raise Fail("locked: %s" % lock) from None
    try: return body()
    finally: os.unlink(lock)  # only the tool's own LOCK
def run(c, until):
    state, dig, n, halt = load_state(c), {}, {"done": 0, "cached": 0, "skip": 0}, {"waiting": "-", "failed": "-"}
    for i, (sid, kind, _) in enumerate(STAGES):
        status, why, fp = evaluate(c, state, dig, i)
        if status in ("skip", "done"):
            word = "cached" if status == "done" and kind == "run" else status
            n[word] += 1
            print("stage %s %s" % (sid, word))
        elif kind == "wait" or fp is None:
            print("stage %s WAITING (%s)" % (sid, why or "upstream not done"))
            halt["waiting"] = sid
        else:
            parts = {k: digest(v) for k, v in fingerprint_parts(c, i, dig, state).items()}  # the fp inputs, for status reasons
            rc, outputs, log = execute(c, i, state)
            if rc == 3 and sid == "f4-start":  # F4's own family refusal: nothing is recorded
                print("stage f4-start WAITING (family refused)")
                halt["waiting"] = sid
                break
            state["stages"][sid] = {"status": "failed" if rc else "done", "fp": fp, "outputs": outputs, "rc": rc, "at": now(), "parts": parts}
            save(c, state)
            print("stage %s %s rc=%d log=%s" % (sid, "failed" if rc else "done", rc, log))
            halt["failed"], dig[sid], n["done"] = sid if rc else "-", digest(outputs), n["done"] + (not rc)
        if sid == until or halt["waiting"] != "-" or halt["failed"] != "-": break
    print("stages=%d done=%d cached=%d skip=%d waiting=%s failed=%s" % (len(STAGES), n["done"], n["cached"], n["skip"], halt["waiting"], halt["failed"]))
    return 1 if halt["failed"] != "-" else 3 if halt["waiting"] != "-" else 0
def mark(c, sid, provider, model):
    state, dig, i = load_state(c), {}, IDS.index(sid)
    if sid in ("verify", "f4"):  # the verifier must be another family than the author marks, and the same card for F4
        made = lambda s: ((state["stages"].get(s) or {}).get("mark") or {}).get("provider")  # noqa: E731
        if provider == "local" or provider in (made("assess"), made("f3-findings")): raise Refused("mark: verifier family equals author family")
        if sid == "f4" and made("verify") is None: raise Fail("mark: f4 needs the verify mark first")
        if sid == "f4" and provider != made("verify"): raise Refused("mark: f4 provider differs from the verify mark")
    for j in range(i): evaluate(c, state, dig, j)
    for dep in (d for d in STAGES[i][2] if d not in dig): raise Fail("mark: %s needs %s done first" % (sid, dep))
    files = wait_files(c, sid)
    if not files or not all(p.is_file() for p in files): raise Fail("mark: %s input files are missing" % sid)
    fp, files, at = fingerprint(c, i, dig, state), {str(p): fhash(p) for p in files}, now()
    state["stages"][sid] = {"status": "marked", "fp": fp, "outputs": files, "rc": None, "at": at,
                            "mark": {"provider": provider, "model": model, "family": provider, "files": files, "fp": fp, "at": at}}
    save(c, state)
    print("marked %s files=%d" % (sid, len(files)))
    return 0
def status(c):  # read-only: no lock, no writes, no .review creation
    state, dig, nxt = load_state(c), {}, "-"
    counts = dict.fromkeys(("done", "stale", "waiting", "pending", "failed", "skip"), 0)
    for i, (sid, kind, _) in enumerate(STAGES):
        (word, why, fp), mark = evaluate(c, state, dig, i), (state["stages"].get(sid) or {}).get("mark")
        counts[word], nxt = counts[word] + 1, sid if nxt == "-" and word not in ("done", "skip") else nxt
        print("%d %s %s %s%s%s" % (i + 1, sid, kind, word, " %s/%s" % (mark["provider"], mark["model"]) if mark else "",
                                   " (%s)" % r if (r := why or why_not_done(c, state, dig, i, word, fp)) else ""))
    print("stages=%d %s next=%s locked=%s" % (len(STAGES), " ".join("%s=%d" % kv for kv in counts.items()), nxt,
                                             "yes" if (c["out"] / ".review" / "LOCK").exists() else "no"))
    return 0
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("words", nargs="*", metavar="run|status|mark CONFIG [STAGE]")
    for name in ("--check", "--seed-dir", "--until", "--provider", "--model"): parser.add_argument(name)
    args = parser.parse_args(argv)
    cmd, rest = (args.words or [""])[0], args.words[1:]
    if not (args.check and not args.words) and not (cmd in ("run", "status") and len(rest) == 1) and not (cmd == "mark" and len(rest) == 2):
        parser.error("usage: --check CONFIG | run CONFIG [--until STAGE] | status CONFIG | mark CONFIG STAGE --provider P --model M")
    try:
        if cmd == "mark" and (rest[1] not in IDS or STAGES[IDS.index(rest[1])][1] != "wait" or args.provider not in compare_tool.PROVIDERS
                              or not MODEL.fullmatch(args.model or "")):
            raise Usage("usage: mark needs a wait stage, --provider %s and --model [A-Za-z0-9._:-]{1,80}" % "|".join(compare_tool.PROVIDERS))
        if args.until is not None and args.until not in IDS: raise Usage("usage: --until is not a stage id")
        c = context(args.check or rest[0])
        errs = check(c, args.seed_dir)
        if args.check or errs:
            print("\n".join(errs + ["stages=%d skip=%d errors=%d" % (len(STAGES), len(c["skip"]), len(errs))]))
            return 1 if errs else 0
        if cmd == "status": return status(c)
        return locked(c, (lambda: run(c, args.until)) if cmd == "run" else lambda: mark(c, rest[1], args.provider, args.model))
    except Fail as exc:
        return print(exc) or exc.code
    except OSError as exc:
        return print("io: %s" % type(exc).__name__) or 1
if __name__ == "__main__":
    sys.exit(main())
