#!/usr/bin/env python3
"""NB-DF-17 flock-publish stage: human-approved PUBLISH.json entries -> private site-finding staging files.
Run as `flock_publish.py stage RUNDIR --copy COPY.json --out OUTDIR`. All-or-nothing: any refusal writes nothing.
Staged files carry `state: "staged"` and are review-required; this tool never builds, deploys or sets `published`.
COPY.json is the organizer's human-written copy per approved finding_id; the tool writes no public text itself."""
import contextlib, hashlib, importlib, importlib.util, io, json, os, re, sys
from pathlib import Path
TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
_spec = importlib.util.spec_from_file_location("_flock_publish", TOOLS / "flock_publish.py")
publish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish)
compare = publish.compare
def _content():  # campaign_tool/content.py by file path: its package is registered by location, sys.path is untouched
    if "campaign_tool" not in sys.modules:
        spec = importlib.util.spec_from_file_location("campaign_tool", REPO / "campaign_tool" / "__init__.py",
                                                      submodule_search_locations=[str(REPO / "campaign_tool")])
        sys.modules["campaign_tool"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules["campaign_tool"])
    return importlib.import_module("campaign_tool.content")
content = _content()
COPY_REQUIRED = {"title", "slug", "event_date", "limitations", "counterevidence", "author"}
COPY_OPTIONAL = {"agency_id", "classification"}
CONFLICTS = ("apparent_conflict", "confirmed_conflict")
KINDS = ("documented_fact", "information_gap")
MANIFEST = "STAGE-MANIFEST.json"
def name(fid, n):  # an id is echoed only when it is a plain short token
    return fid if isinstance(fid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", fid) else "#%d" % n
def inside(path, root):
    return path == root or root in path.parents
def refuse(lines, code=1):
    print("\n".join(lines + ["stage refused | errors=%d | nothing written" % len(lines)]))
    return code
def texts(value, minimum):
    return isinstance(value, list) and len(value) >= minimum and all(isinstance(v, str) and v.strip() for v in value)
def copy_errors(block, claim_class):  # the human copy for one approved entry -> named problems
    if not isinstance(block, dict) or not COPY_REQUIRED <= set(block) <= COPY_REQUIRED | COPY_OPTIONAL:
        return ["keys differ from the copy schema"]
    errs, kind = [], block.get("classification")
    if kind in CONFLICTS:
        errs.append("conflict classification %s refused" % kind)
    elif "classification" in block and kind not in KINDS:
        errs.append("classification is not documented_fact or information_gap")
    elif "classification" not in block and claim_class == "duty-concern":
        errs.append("duty-concern needs a classification")
    if not all(isinstance(block[k], str) and block[k].strip() for k in ("title", "slug", "event_date", "author")):
        errs.append("title, slug, event_date and author must be non-empty text")
    if not texts(block["limitations"], 1):
        errs.append("limitations needs at least one non-empty text")
    if not texts(block["counterevidence"], 0):
        errs.append("counterevidence must be a list of non-empty texts")
    if block.get("agency_id") is not None and not isinstance(block["agency_id"], str):
        errs.append("agency_id must be text or null")
    for key, values in (() if errs else (("title", [block["title"]]), ("limitations", block["limitations"]),
                                          ("counterevidence", block["counterevidence"]))):
        cats = sorted({c for v in values for c in publish.text_rules(v, False)})
        errs += ["%s refused %s" % (key, ",".join(cats))] if cats else []
    return errs
def sources(fact_ids, facts):  # one source per distinct (sha256, page) of the finding's facts, sorted
    pairs = {(facts[i]["source_sha256"].strip(), facts[i]["page"].strip()) for i in compare.ids(fact_ids)}
    return [{"sha256": sha, "locator": "page:" + page, "title": "Source record", "rule_id": None}
            for sha, page in sorted(pairs, key=lambda p: (p[0], int(p[1]) if p[1].isdigit() else -1, p[1]))]
def site_doc(site_slug, entry, block, fact_ids, facts):
    doc = {"id": "fnd_" + hashlib.sha256(("%s\x1f%s" % (site_slug, entry["finding_id"])).encode()).hexdigest()[:16],
           "title": block["title"], "slug": block["slug"], "summary": entry["text"],
           "classification": block.get("classification", "information_gap"), "confidence": "verified",
           "event_date": block["event_date"], "sources": sources(fact_ids, facts), "published_at": None,
           "updated_at": None, "corrections": [], "state": "staged", "author": block["author"],
           "limitations": block["limitations"], "counterevidence": block["counterevidence"]}
    return {**doc, "agency_id": block["agency_id"]} if "agency_id" in block else doc
def plan(run, copy, doc):  # -> (errors, [(finding_id, site doc)], skipped_pending)
    approved = [f for f in doc["findings"] if f["review"]["status"] == "approved"]
    if not approved:
        return ["PUBLISH.json: nothing approved"], [], 0
    try:
        blocks = json.loads(copy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        blocks = None
    if not isinstance(blocks, dict):
        return ["copy: not a JSON object"], [], 0
    status = {f["finding_id"]: f["review"]["status"] for f in doc["findings"]}
    errs = ["copy %s: %s" % (name(k, n), "not a finding in PUBLISH.json" if k not in status else "entry is not approved")
            for n, k in enumerate(blocks, 1) if status.get(k) != "approved"]
    _, findings, facts, _ = publish.verify_tool.load(run)
    fact_ids, out, slugs = {f["finding_id"]: f["fact_ids"] for f in findings}, [], {}
    for n, f in enumerate(approved, 1):
        fid, label = f["finding_id"], name(f["finding_id"], n)
        why = copy_errors(blocks[fid], f["claim_class"]) if fid in blocks else ["approved entry has no copy block"]
        if not why and blocks[fid]["slug"] in slugs:
            why = ["slug duplicates the block of %s" % slugs[blocks[fid]["slug"]]]
        errs += ["copy %s: %s" % (label, w) for w in why]
        if why:
            continue
        slugs[blocks[fid]["slug"]] = label
        staged = site_doc(doc["slug"], f, blocks[fid], fact_ids[fid], facts)
        try:  # the site loader itself, as if published on the approval day; nothing is published here
            content.load_finding({**staged, "state": "published", "published_at": f["review"]["approved_on"] + "T12:00:00+00:00"},
                                 label)
        except content.ContentError as exc:
            errs.append("schema %s" % exc)
            continue
        out.append((label, staged))
    return errs, out, len(doc["findings"]) - len(approved)
def link(path, data):  # temp + os.link: an existing file is never replaced and the temp never outlives the call
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.link(tmp, path)
    finally:
        os.unlink(tmp)
def write(out, files):  # dirs 0700, files 0600; on any failure every file and folder made here is removed again
    made, written = [], []
    try:
        for folder in (out, out / "content", out / "content" / "findings"):
            if not folder.exists():
                folder.mkdir(mode=0o700)
                made.append(folder)
            os.chmod(folder, 0o700)
        for path, data in files:
            link(path, data)
            written.append(path)
    except BaseException:
        for path in reversed(written):
            with contextlib.suppress(OSError): path.unlink()  # noqa: E701
        for folder in reversed(made):
            with contextlib.suppress(OSError): folder.rmdir()  # noqa: E701
        raise
def stage(run, copy, out, repo=REPO):
    raw = Path(out)
    out, copy = raw.resolve(), Path(copy).resolve()
    errs = ["--%s is inside the repository" % k for k, p in (("out", out), ("copy", copy)) if inside(p, repo)]
    if raw.is_symlink():
        errs.append("--out is a symlink")
    elif inside(out, run):
        errs.append("--out is inside RUNDIR")
    elif out.exists() and not (out.is_dir() and not any(out.iterdir())):
        errs.append("--out exists and is not an empty folder")
    if errs:
        return refuse(errs)
    code, lines, _ = publish.derive(run, "stage")
    if code == 3:  # same-family run: unchanged refusal
        return refuse(lines, 3)
    target, buf = run / "PUBLISH.json", io.StringIO()
    if target.is_symlink() or not target.is_file():
        return refuse(["PUBLISH.json: missing"])
    with contextlib.redirect_stdout(buf):
        failed = publish.check(target, run)
    if failed:  # check lines name ids and rules only, never text
        return refuse(buf.getvalue().splitlines() + ["PUBLISH.json: check failed"])
    doc = json.loads(target.read_text(encoding="utf-8"))
    errs, staged, pending = plan(run, copy, doc)
    if errs:
        return refuse(errs)
    files, rows = [], []
    for fid, site in staged:
        data = (json.dumps(site, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        files.append((out / "content" / "findings" / (site["slug"] + ".json"), data))
        rows.append({"finding_id": fid, "slug": site["slug"], "sha256": hashlib.sha256(data).hexdigest()})
    refs = sorted({f["review"]["approval_ref"] for f in doc["findings"] if f["review"]["status"] == "approved"})
    manifest = {"schema_version": 1, "site_slug": doc["slug"], "run_date": doc["run_date"], "agency": doc["agency"],
                "staged": rows, "skipped_pending": pending, "approval_refs": refs}
    write(out, files + [(out / MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))])  # manifest last
    print("".join("staged %s -> content/findings/%s.json\n" % (r["finding_id"], r["slug"]) for r in rows), end="")
    print("stage | approved=%d staged=%d skipped_pending=%d | review-required; not built, not deployed; G decides any site use"
          % (len(doc["findings"]) - pending, len(rows), pending))
    return 0
