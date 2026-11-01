#!/usr/bin/env python3
"""Flock store check: run every flock checker over one private store in-process, name agency slugs that appear in one
place but not the others, and the age of the newest store backup. Reads only: writes, creates or chmods nothing.
  python3 tools/flock_store_check.py STORE [--backups DIR --max-backup-age-days N] [--today YYYY-MM-DD]
Prints row numbers, field names, slugs and counts only. Exit 0 no errors, 1 errors, 2 usage or refusal."""
import argparse, contextlib, csv, importlib.util, io, json, os, re, sys, tarfile
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CAP = 20
SECTIONS = ("facts", "law-policy", "requests", "contacts", "watchlists", "publish", "slugs", "backup")

def _tool(name):  # loaded by file path, never copied (tools/flock_compare.py pattern)
    spec = importlib.util.spec_from_file_location("_store_check_" + name, REPO / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

facts_tool, law_policy, requests_tool = _tool("flock_facts"), _tool("flock_law_policy"), _tool("flock_requests")
email_tool, monitor, publish, region, backup = (_tool(n) for n in ("flock_email", "flock_monitor", "flock_publish",
                                                                     "flock_region", "flock_backup"))
RAISED = (OSError, ValueError, csv.Error, tarfile.TarError, monitor.Fail, backup.Fail)  # ValueError covers UnicodeDecodeError

def shown(name):  # a folder name is printed only when it looks like a slug
    return name if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) else "?"

def folders(base):  # -> (real child folders, linked child names); a link is named, never followed
    if base.is_symlink() or not base.is_dir():
        return [], []
    kids = sorted(base.iterdir())
    return [p for p in kids if not p.is_symlink() and p.is_dir()], [p.name for p in kids if p.is_symlink()]

def requests(store):
    errs = requests_tool.load(store)[1]
    return ([], ["none"]) if errs == ["store: no agencies/*/REQUESTS.csv found"] else (errs, [])

def contacts(store):
    real = folders(store / "agencies")[0]
    return [f"{shown(f.name)}: {e}" for f in real for e in email_tool.contact(f / "CONTACT.csv", f.name)[1]], \
        [] if real else ["none"]

def watchlists(store):
    errs, seen = [], 0
    for rdir in folders(store / "regions")[0]:
        path, name = rdir / "WATCHLIST.csv", shown(rdir.name)
        if path.is_symlink():
            errs.append(f"{name}: WATCHLIST.csv is a link; not followed")
        elif path.is_file():
            seen += 1
            try:
                monitor.validated(path)
            except monitor.Fail as exc:
                errs += [f"{name}: {line}" for line in str(exc).splitlines()]
    return errs, [] if seen or errs else ["none"]

def publish_runs(store):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = publish.status(store)
    if code == 2:
        return [], ["none"]
    lines = out.getvalue().splitlines()
    bad = [ln.split(" | ", 1)[1] for ln in lines[:-1] if ln.split(" | ")[3:4] in (["invalid"], ["drifted"], ["link-refused"])]
    return bad, [lines[-1].split(" | ", 1)[1]] if lines else []

def slugs(store):
    real, linked = folders(store / "agencies")
    errs = [f"agencies/{shown(n)} is a link; not followed" for n in linked]
    regions, rlinked = folders(store / "regions")
    errs += [f"regions/{shown(n)} is a link; not followed" for n in rlinked]
    have, in_roster, rosters = {p.name for p in real}, set(), 0
    for rdir in regions:
        name = shown(rdir.name)
        for file, header, key in (("ROSTER.csv", region.ROSTER, "slug"), ("WATCHLIST.csv", monitor.WATCH_HEADER, "agency")):
            path = rdir / file
            if path.is_symlink():
                errs.append(f"{name}: {file} is a link; not followed")
                continue
            if not path.is_file():
                continue
            try:
                rows = monitor.read_csv(path, header)
            except monitor.Fail as exc:
                if file == "ROSTER.csv":  # an unreadable WATCHLIST is reported once, under watchlists
                    errs.append(f"{name}: ROSTER.csv {exc}")
                continue
            got = {r["slug"].strip() if key == "slug" else monitor.slug(r[key]) for _, r in rows} - {""}
            if file == "ROSTER.csv":
                rosters, in_roster = rosters + 1, in_roster | got
            what = "ROSTER slug" if file == "ROSTER.csv" else "WATCHLIST agency"
            errs += [f"{name}: {what} {shown(s)} has no agency folder" for s in sorted(got - have)]
    notes = [f"note: agency folder {shown(n)} is in no ROSTER" for n in sorted(have - in_roster)] if rosters else []
    return errs, notes

def newest_backup(folder):  # -> (created_at date or None, skipped links, ignored tars); hashes are not re-verified
    best, links, ignored = None, 0, 0
    for path in sorted(Path(folder).glob("*.tar")):
        if path.is_symlink() or not path.is_file():
            links += path.is_symlink()
            continue
        try:
            with tarfile.open(path, mode="r:") as tar:
                first = tar.next()
                if first is None or first.name != backup.MANIFEST or not first.isreg() \
                        or first.size > backup.MAX_MANIFEST_BYTES:
                    raise ValueError("no manifest first")
                manifest = json.loads(tar.extractfile(first).read().decode("utf-8"))
            if not isinstance(manifest, dict) or manifest.get("kind") != backup.KIND:
                raise ValueError("not a flock-store manifest")
            made = datetime.fromisoformat(manifest["created_at"]).date()
        except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
            ignored += 1
            continue
        best = made if best is None or made > best else best
    return best, links, ignored

def backups(args):
    if args.backups is None:
        return [], ["not checked"]
    if Path(args.backups).is_symlink() or not Path(args.backups).is_dir():
        return ["backup folder missing or a link; not followed"], []
    made, links, ignored = newest_backup(args.backups)
    tail, n = "; hashes not re-verified (flock_backup.py verify)", args.max_backup_age_days
    counts = f"skipped_links={links} ignored={ignored}"
    if made is None:
        return ["none found"], [counts]
    age = (args.today - made).days
    if age < 0:
        return [f"newest is dated after --today{tail}"], [counts]
    line = f"newest is {age} days old (max {n})"
    return ([line + tail], [counts]) if age > n else ([], [line + " ok" + tail, counts])

def run(store, args):
    checks = {"facts": lambda s: (facts_tool.check_path(s), []), "law-policy": lambda s: (law_policy.load_all(s)[2], []),
              "requests": requests, "contacts": contacts,
              "watchlists": watchlists, "publish": publish_runs, "slugs": slugs, "backup": lambda _: backups(args)}
    total, notes = 0, 0
    for section in SECTIONS:
        try:
            errs, info = checks[section](store)
        except RAISED as exc:  # the class only, never a path or record text
            errs, info = [f"failed: {type(exc).__name__}"], []
        lines = errs[:CAP] + ([f"… and {len(errs) - CAP} more"] if len(errs) > CAP else []) + info
        print("".join(f"{section} | {line}\n" for line in lines + [f"errors={len(errs)}"]), end="")
        total += len(errs)
        notes += sum(line.startswith("note:") for line in info)
    print(f"store check | sections={len(SECTIONS)} errors={total} notes={notes} | machine validity only; review-required")
    return 1 if total else 0

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store")
    parser.add_argument("--backups", metavar="DIR")
    parser.add_argument("--max-backup-age-days", type=int, metavar="N")
    parser.add_argument("--today", type=date.fromisoformat, default=date.today(), metavar="YYYY-MM-DD")
    args = parser.parse_args(argv)
    if (args.backups is None) != (args.max_backup_age_days is None):
        parser.error("--backups and --max-backup-age-days go together")
    if args.max_backup_age_days is not None and not 1 <= args.max_backup_age_days <= 365:
        parser.error("--max-backup-age-days must be 1..365")
    store = Path(args.store).resolve()
    if store == REPO or REPO in store.parents:
        return print("store check: STORE is inside the repository", file=sys.stderr) or 2
    if not store.is_dir():
        return print("store check: STORE is not a folder", file=sys.stderr) or 2
    return run(store, args)

if __name__ == "__main__":
    sys.exit(main())
