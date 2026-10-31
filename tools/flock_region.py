#!/usr/bin/env python3
"""Flock region: draft a county region config from the engine agency seed, check it, and scaffold the private store.
  new --county-fips FIPS --region SLUG --out PATH [--jurisdiction us-ca] [--seed-dir DIR]
  --check CONFIG [--seed-dir DIR]  |  init CONFIG --store DIR [--seed-dir DIR]
init writes agencies/<slug>/ (yes|hold) and regions/<region>/WATCHLIST.csv (F7), ROSTER.csv (F10), PLAN.md. No network;
the seed is read, never edited or verified; FACTS/POLICY/REQUESTS/LAW are never written. Exit 0 ok, 1 errors, 2 usage."""
import argparse, contextlib, csv, importlib.util, io, json, os, re, sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS.parent))
from campaign_tool import discovery, law  # noqa: E402

def _tool(name):  # F1/F7 are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_region_" + name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

monitor, facts_tool = _tool("flock_monitor"), _tool("flock_facts")
Fail = monitor.Fail
SLUG, SITE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*"), re.compile(r"[a-z][a-z0-9-]*")
RESERVED = {"index", "records", "sources", "privacy", "confirmed"}  # the site's own routes
NEW_KINDS = ("sheriff", "police")  # CHP and county boards are added by hand with a seed_note
ROSTER = ["watch_agency", "slug", "in_scope", "applicability", "site_slug"]
KEY = ("agency", "kind", "url", "query")
HEAD = "Region plan - %s - private working checklist, not a publication"
text = lambda value: value if isinstance(value, str) else ""  # noqa: E731

def outside(path, label):
    try:
        return monitor.private_store(path)
    except Fail:
        raise Fail("%s inside repository" % label) from None

def county_of(jurisdiction, fips, seed_dir):  # -> (seed or None, county or None)
    try:
        seed = discovery.load_seed(jurisdiction, seed_dir) if law.JURISDICTION_PATTERN.fullmatch(jurisdiction) else None
    except (ValueError, OSError):
        seed = None
    return seed, next((c for c in (seed or {}).get("counties", ()) if c["county_fips"] == fips), None)

def load_config(path):
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Fail("config: cannot read (%s)" % type(exc).__name__) from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("agencies"), list):
        raise Fail("config: agencies is not a list")
    return cfg

def check(cfg, seed_dir):  # -> (errors, F7 watch rows in config order); each error names its agency or 'config'
    errs, rows, keys, slugs, names = [], [], set(), set(), set()
    bad = lambda where, field, why: errs.append("%s: %s %s" % (where, field, why))
    seed, county = county_of(text(cfg.get("jurisdiction")), cfg.get("county_fips"), seed_dir)
    package = law.package_status(cfg.get("law_package"))[0] != "missing"  # loads + validates; never raises
    for field, ok, why in (("schema", cfg.get("schema") == 1, "is not 1"),
                           ("region", SLUG.fullmatch(text(cfg.get("region"))), "is not a lowercase slug"),
                           ("jurisdiction", seed, "is not cc-ss with a valid agency seed"),
                           ("county_fips", county or not seed, "is not a county in the seed"),
                           ("law_package", package, "does not load")):
        if not ok: bad("config", field, why)
    ids = {a["agency_id"] for a in county["agencies"]} if county else set()
    for n, a in enumerate(cfg["agencies"], 1):
        where, a = "agency %d" % n, a if isinstance(a, dict) else {}
        name, slug, scope, app, dom = (text(a.get(k)) for k in ROSTER[:4] + ["official_domain"])
        watch, site, sid = a.get("watch"), a.get("site_slug"), a.get("seed_agency_id")
        watch = watch if isinstance(watch, list) else None
        checks = (("slug", SLUG.fullmatch(slug), "is not a lowercase slug"), ("slug", slug not in slugs, "is duplicated"),
                  ("watch_agency", name.strip(), "is empty"), ("watch_agency", name not in names, "is duplicated"),
                  ("seed_agency_id", sid is not None or text(a.get("seed_note")).strip(), "is null without a seed_note"),
                  ("seed_agency_id", sid is None or sid in ids, "is not an agency_id of this county in the seed"),
                  ("in_scope", scope in ("yes", "no", "hold"), "is not yes, no or hold"),
                  ("applicability", isinstance(a.get("applicability"), str) and len(app) <= 200, "is over 200 chars"),
                  ("applicability", app.strip() or scope != "yes", "is required when in_scope=yes"),
                  ("official_domain", not dom or monitor.DOMAIN.fullmatch(dom), "is not a bare lowercase hostname"),
                  ("official_domain", dom or all(isinstance(w, dict) and w.get("kind") == "social" for w in watch or ()),
                   "is required with a non-social watch row"),
                  ("site_slug", site is None or (SITE.fullmatch(text(site)) and site not in RESERVED),
                   "is not null or a non-reserved [a-z][a-z0-9-]* slug"), ("watch", watch is not None, "is not a list"))
        for field, ok, why in checks:
            if not ok: bad(where, field, why)
        slugs.add(slug)
        names.add(name)
        for m, w in enumerate(watch or (), 1):
            w = w if isinstance(w, dict) else {}
            row = {"agency": name, "kind": text(w.get("kind")), "url": text(w.get("url")),
                   "query": text(w.get("query")), "official_domain": dom, "last_seen_hash": ""}
            dup = ["entry duplicates an earlier watch row"] if tuple(row[k] for k in KEY) in keys else []
            for e in monitor.row_errors(row) + dup:
                bad(where, "watch %d" % m, e)
            keys.add(tuple(row[k] for k in KEY))
            rows.append(row)
    return errs, rows

def cmd_check(args):
    cfg = load_config(args.check)
    errs, rows = check(cfg, args.seed_dir)
    scopes = [a.get("in_scope") for a in cfg["agencies"] if isinstance(a, dict)]
    print("\n".join(errs + ["agencies=%d in_scope=%d hold=%d watch_rows=%d errors=%d" % (
        len(cfg["agencies"]), scopes.count("yes"), scopes.count("hold"), len(rows), len(errs))]))
    return 1 if errs else 0

def cmd_new(args):
    out = outside(args.out, "out")
    if out.exists() or out.is_symlink() or Path(args.out).is_symlink(): raise Fail("out: exists; refusing to overwrite")
    seed, county = county_of(args.jurisdiction, args.county_fips, args.seed_dir)
    if county is None: raise Fail("config: county_fips is not a county in a valid %s seed" % args.jurisdiction)
    agencies = [{"slug": facts_tool.slugify(e["name"]), "watch_agency": e["name"], "seed_agency_id": e["agency_id"],
                 "in_scope": "hold", "applicability": "", "official_domain": "", "site_slug": None, "watch": []}
                for e in county["agencies"] if e["kind"] in NEW_KINDS]
    cfg = {"schema": 1, "region": args.region, "jurisdiction": args.jurisdiction, "county_fips": args.county_fips,
           "law_package": args.jurisdiction, "agencies": agencies}
    errs, _ = check(cfg, args.seed_dir)
    if errs: raise Fail("\n".join(errs))
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # O_EXCL: this call created the file
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(cfg, indent=2) + "\n")
    except BaseException:  # no half-written draft: remove only the file this call created, then re-raise
        with contextlib.suppress(OSError): os.unlink(out)
        raise
    print("agencies=%d county=%s draft=%s" % (len(agencies), county["county_fips"], out))
    return 0

def put(path, header=None, rows=(), body=""):  # atomic 0600 write; an unchanged file is left untouched
    if header:
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows([header] + [[r[k] for k in header] for r in rows])
        body = buf.getvalue()
    if not (path.is_file() and path.read_bytes() == body.encode("utf-8")):
        monitor.write_atomic(path, body)

def plan(cfg, store, rdir):
    w, r, t = rdir / "WATCHLIST.csv", rdir / "ROSTER.csv", "python3 tools/"
    lists = "--watchlist %s --roster %s --store %s" % (w, r, store)
    lines = [HEAD % cfg["region"]]
    for a in cfg["agencies"]:
        slug, folder, site = a["slug"], store / "agencies" / a["slug"], a["site_slug"]
        lines += ["", "## " + slug, "watch_agency: %s | in_scope: %s" % (a["watch_agency"], a["in_scope"])]
        if a["in_scope"] == "no":
            lines.append("out of scope")
            continue
        lines += ["1. legwork facts (L2-style, luna-worker): FACTS.csv `agency` column = `%s`; F1 must slugify it to `%s`"
                  % (a["watch_agency"], slug),
                  "2. %sflock_facts.py import <FACTS.csv> --store %s --added-by <name>" % (t, store),
                  "3. R1 policy URL (research-grok), then L1-style POLICY rows: %sflock_law_policy.py --check %s"
                  % (t, folder / "POLICY.csv"),
                  "4. REQUESTS rows: %sflock_requests.py due %s" % (t, folder / "REQUESTS.csv"),
                  "5. anyone, no network: %sflock_monitor.py check %s; then operator only (fetches official index pages): "
                  "%sflock_monitor.py scan %s --store %s" % (t, w, t, w, store),
                  "6. %sflock_sweep.py status %s" % (t, lists),
                  "7. [RUN] compare/verify cards per %sflock_sweep.py plan %s" % (t, lists),
                  "8. F9 export, then a site location card data/locations/%s.json" % site if site
                  else "8. F9 export (no site_slug, so no site location card)"]
        if a["in_scope"] == "hold":
            lines.append("HOLD: operator applicability line required before step 7")
    return "\n".join(lines) + "\n"

def cmd_init(args):
    store, cfg = outside(args.store, "store"), load_config(args.config)
    errs, rows = check(cfg, args.seed_dir)
    region, mine = cfg.get("region"), {} if errs else {a["slug"]: n for n, a in enumerate(cfg["agencies"], 1)}
    bad = []  # init never writes through a symlink or a non-folder inside the store; checked before anything is made
    for rel in [] if errs else ["agencies", "regions", "regions/" + region] + [
            "agencies/" + a["slug"] for a in cfg["agencies"] if a["in_scope"] != "no"]:
        path = store / rel
        if rel.rpartition("/")[0] not in bad and (path.is_symlink() or os.path.lexists(path) and not path.is_dir()):
            bad.append(rel)
            errs.append("%s: %s" % (rel, "is a symlink; refusing" if path.is_symlink() else "exists and is not a folder"))
    for other in [] if errs else sorted((store / "regions").glob("*/ROSTER.csv")):
        if other.parent.name != region:
            errs += ["agency %d: slug already belongs to region %s" % (mine[r["slug"]], other.parent.name)
                     for _, r in monitor.read_csv(other, ROSTER) if r["slug"] in mine]
    if errs: raise Fail("\n".join(errs + ["errors=%d" % len(errs)]))
    rdir, created = store / "regions" / region, 0
    for a in cfg["agencies"]:
        if a["in_scope"] != "no" and not (store / "agencies" / a["slug"]).is_dir():
            (store / "agencies").mkdir(mode=0o700, parents=True, exist_ok=True)
            (store / "agencies" / a["slug"]).mkdir(mode=0o700)
            created += 1
    old = {tuple(r[k] for k in KEY): r["last_seen_hash"]
           for _, r in monitor.read_csv(rdir / "WATCHLIST.csv", monitor.WATCH_HEADER, missing_ok=True)}
    for row in rows:
        row["last_seen_hash"] = old.get(tuple(row[k] for k in KEY), "")
    dropped = len(set(old) - {tuple(row[k] for k in KEY) for row in rows})
    roster = [dict({k: a[k] for k in ROSTER[:4]}, site_slug=a["site_slug"] or "") for a in cfg["agencies"]]
    put(rdir / "WATCHLIST.csv", monitor.WATCH_HEADER, rows)
    put(rdir / "ROSTER.csv", ROSTER, roster)
    put(rdir / "PLAN.md", body=plan(cfg, store, rdir))
    print("agencies=%d folders_created=%d watch_rows=%d watch_dropped=%d roster_rows=%d"
          % (len(cfg["agencies"]), created, len(rows), dropped, len(roster)))
    return 0

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", choices=("new", "init"))
    p.add_argument("config", nargs="?", help="region config (init)")
    for flag in ("--check", "--county-fips", "--region", "--out", "--store", "--seed-dir"):
        p.add_argument(flag)
    p.add_argument("--jurisdiction", default="us-ca")
    a = p.parse_args(argv)
    need = {None: (a.check,), "new": (a.county_fips, a.region, a.out), "init": (a.config, a.store)}
    if (a.check is None) == (a.command is None) or not all(need[a.command]) or (a.command != "init" and a.config):
        p.error("use exactly one of: new, --check CONFIG, init CONFIG")
    try:
        return cmd_check(a) if a.check else cmd_new(a) if a.command == "new" else cmd_init(a)
    except Fail as exc:
        return print(exc) or 1
    except OSError as exc:  # the class only, never a path or content
        return print("io: %s" % type(exc).__name__) or 1

if __name__ == "__main__":
    raise SystemExit(main())
