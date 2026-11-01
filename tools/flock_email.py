#!/usr/bin/env python3
"""Wrap FOLLOWUP-DRAFT.md into a private EMAIL-DRAFT.md. Draft only: it writes files and cannot send. `status STORE` reads each run's email stage back."""
import csv, os, re, sys, tempfile
from datetime import date
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]
HEAD = "DRAFT - not sent - G approval required"
COLS = "agency,role,email,source_url,source_sha256,checked_at,added_by".split(",")
CHECKS = {"role": r"records", "email": r"[^@\s,;<>]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "source_url": r"https://\S+",
          "source_sha256": r"[0-9a-f]{64}", "checked_at": r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "added_by": r".*\S.*"}
USAGE = ("usage: flock_email.py draft RUNDIR --drafts DIR  (writes EMAIL-DRAFT.md; draft only, never sends)\n"
         "       flock_email.py check STORE  (reads every STORE/agencies/<slug>/CONTACT.csv; writes nothing)\n"
         "       check and draft also take [--max-age-days N [--today YYYY-MM-DD]]  (a contact checked more than N days ago is stale)\n"
         "       check and draft warn (never refuse) when the address domain is not the source_url host's site; a human checks the page\n"
         "       flock_email.py status STORE  (each run's email stage; exit 1 on malformed or sent without approval)")
STAGES = ("follow-up drafted", "email drafted", "approval recorded", "sent recorded", "malformed", "inconsistent: sent without approval")
LABELS = "runs followup drafted approved sent malformed inconsistent".split()

def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

def ages(rest):
    """(max_age, today) from [--max-age-days N [--today D]]: (None, None) when empty, None when malformed or duplicated."""
    got = dict(zip(rest[0::2], rest[1::2]))
    if len(got) * 2 != len(rest) or not set(got) <= {"--max-age-days", "--today"}: return None
    n, d = got.get("--max-age-days", ""), got.get("--today", date.today().isoformat())
    ok = re.fullmatch(r"[0-9]{1,4}", n) and 1 <= int(n) <= 3650 and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", d)
    try: return ((int(n), date.fromisoformat(d)) if ok else None) if got else (None, None)
    except ValueError: return None

def contact(path, folder, max_age=None, today=None):
    """Return (the one records address, error lines, its source_url) from CONTACT.csv."""
    try: rows = list(csv.reader(Path(path).read_text(encoding="utf-8").splitlines()))
    except (OSError, UnicodeDecodeError): return None, ["contact: CONTACT.csv missing or unreadable"], None
    if not rows or rows[0] != COLS: return None, ["row 1: header does not match CONTACT.csv v1"], None
    errs, found = [], []
    for n, cells in enumerate(rows[1:], 2):
        row = dict(zip(COLS, cells)) if len(cells) == len(COLS) else {}
        bad = [k for k, p in CHECKS.items() if not re.fullmatch(p, row.get(k, ""))]
        bad += ["agency"] if slug(row.get("agency", "")) != folder else []
        errs += [f"row {n}: {k} is invalid" for k in bad]
        found += [] if bad else [row]
    errs = errs or ([] if len(found) == 1 else ["contact: no records row" if not found else "contact: more than one records row"])
    if not errs and max_age is not None:
        try: age = ((today or date.today()) - date.fromisoformat(found[0]["checked_at"])).days
        except ValueError: age = None  # shape-valid but not a calendar date
        errs = [] if age is not None and 0 <= age <= max_age else ["contact: checked_at is not a real date" if age is None else
                "contact: checked_at is in the future" if age < 0 else f"contact: checked_at {found[0]['checked_at']} is {age} days old (max {max_age})"]
    got = (found or [{"email": None, "source_url": None}])[0]
    return got["email"], errs, got["source_url"]

def host(url):
    """Lower-case host of an https source_url (userinfo and port dropped), or "(none)" when it has none."""
    got = re.match(r"https://(?:[^/?#]*@)?([^/?#:]*)", url or "")
    return (got[1].lower() if got else "") or "(none)"

def same_site(domain, page):
    """True when an address domain and a page host are one site: case-insensitive, one leading www. dropped, equal or a dot-suffix."""
    a, b = (re.sub(r"^www\.", "", x.lower()) for x in (domain, page))
    return a == b or a.endswith("." + b) or b.endswith("." + a)

def draft(rundir, drafts, max_age=None, today=None):
    run, out_dir = Path(rundir).resolve(), Path(drafts).resolve()
    if not (len(run.parents) >= 4 and run.parent.name == "runs" and run.parents[2].name == "agencies"):
        return print("draft: RUNDIR must be <store>/agencies/<slug>/runs/<date>", file=sys.stderr) or 1
    if REPO in (run, *run.parents): return print("draft: RUNDIR is inside the repository", file=sys.stderr) or 1
    name = run.parent.parent.name
    try: lines = (run / "FOLLOWUP-DRAFT.md").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError): lines = []
    ends = [i for i, line in enumerate(lines) if line.startswith("END OF REQUEST DRAFT.")]
    if not (ends and lines[0] == HEAD and re.fullmatch(r"Subject: \S.*", lines[2] if len(lines) > 2 else "")):
        return print("draft: follow-up draft malformed", file=sys.stderr) or 1
    to, errs, src = contact(run.parents[3] / "agencies" / name / "CONTACT.csv", name, max_age, today)
    outs = [run / "EMAIL-DRAFT.md", out_dir / f"{name}-{run.name}-EMAIL-DRAFT.md"]
    errs = errs or (["draft: --drafts is inside the repository"] if REPO in (out_dir, *out_dir.parents) else [])
    errs = errs or (["draft: EMAIL-DRAFT already exists"] if any(os.path.lexists(p) for p in outs) else [])
    if errs: return print("\n".join(errs), file=sys.stderr) or 1
    body = "\n".join(lines[3:ends[0]]).strip("\n")
    ids = re.search(r"related to requests ([^\n]*?)\.(?:\s|$)", body)
    text = "\n".join([HEAD, f"To: {to}", lines[2], "---", body, "", "Please reference this request by your agency's request number in any response.",
                      f"Related prior requests: {ids.group(1) if ids else 'none on file'}.", "---", "G APPROVED: ____",
                      "Sent by: ____ (operator, by hand, after G's reply)"]) + "\n"
    out_dir.mkdir(mode=0o700, exist_ok=True)
    linked = []  # only paths this call created; the rollback never touches anything else
    try:
        for path in outs:
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh: fh.write(text)
                os.link(tmp, path)
                linked.append(path)
            finally: os.unlink(tmp)
    except OSError as exc:
        for path in linked: os.unlink(path)
        why = "EMAIL-DRAFT already exists" if isinstance(exc, FileExistsError) else f"io {type(exc).__name__}"
        return print(f"draft: {why}", file=sys.stderr) or 1
    if not same_site(to.rsplit("@", 1)[1], host(src)): print(f"draft: warn: domain differs from source host {host(src)}", file=sys.stderr)
    return print(f"written=2 to_domain={to.rsplit('@', 1)[1]}") or 0

def check(store, show=None, max_age=None, today=None):
    """One readiness line per agency folder, then totals. Never prints an address local part; reads only."""
    root = Path(store).resolve()
    if REPO in (root, *root.parents): return print("check: STORE is inside the repository", file=sys.stderr) or 2
    base = root / "agencies"
    if base.is_symlink() or not base.is_dir(): return print("check: no agencies folder", file=sys.stderr) or 2
    try: folders = sorted(p for p in base.iterdir() if p.is_symlink() or p.is_dir())
    except OSError: return print("check: agencies folder unreadable", file=sys.stderr) or 2
    if show: return show(folders)  # status shares the STORE walk and its errors
    lines, ready, stale, mismatch = [], 0, 0, 0
    for folder in folders:  # a symlinked agency folder is refused, never followed
        to, errs, src = (None, ["link refused"], None) if folder.is_symlink() else contact(folder / "CONTACT.csv", folder.name, max_age, today)
        old = re.fullmatch(r"contact: checked_at (\S+) is ([0-9]+) days old \(max [0-9]+\)", errs[0]) if errs else None
        ready += not errs
        stale += bool(old)
        lines.append(f"contact | {folder.name} | " + (f"ready | domain={to.rsplit('@', 1)[1]}" if not errs else
                     f"stale | checked {old[1]}, {old[2]} days old" if old else f"not ready | {errs[0]}"))
        mismatch += (odd := not errs and not same_site(to.rsplit("@", 1)[1], host(src)))  # warning only: a portal or county domain can be right
        lines[-1] += f" | warn: domain differs from source host {host(src)}" * odd
    print("\n".join(lines + [f"contact | agencies={len(folders)} ready={ready} not_ready={len(folders) - ready - stale}{f' stale={stale}' * bool(max_age)}"
                             f" mismatch={mismatch} | draft only; G approval required before any send"]))
    return 0 if ready == len(folders) else 1

def stage(run):
    """The email stage of one run folder, or None without a draft file. Never returns a filled value."""
    email, follow = run / "EMAIL-DRAFT.md", run / "FOLLOWUP-DRAFT.md"
    if run.is_symlink() or email.is_symlink() or follow.is_symlink(): return "refused: link"  # never read
    if not email.exists(): return "follow-up drafted" if follow.exists() else None
    try: lines = email.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError): return "malformed"
    ok = [[ln.split(":", 1)[1].strip() for ln in lines if ln.startswith(k)] for k in ("G APPROVED:", "Sent by:")]
    if lines[:1] != [HEAD] or [len(v) for v in ok] != [1, 1]: return "malformed"
    yes, sent = ok[0][0] not in ("", "____"), bool(ok[1][0]) and not ok[1][0].startswith("____")
    return ("sent recorded" if yes else STAGES[5]) if sent else "approval recorded" if yes else "email drafted"

def status(folders):
    """One stage line per agency run holding a draft, then totals. Prints no address, subject or filled value."""
    lines, seen = [], []
    for folder in folders:
        runs = folder / "runs"
        try: dates = [] if folder.is_symlink() or runs.is_symlink() or not runs.is_dir() else sorted(p for p in runs.iterdir()
                      if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", p.name) and (p.is_symlink() or p.is_dir()))
        except OSError: dates = [None]  # an unreadable runs folder fails closed
        for run in dates:
            got = stage(run) if run else "malformed"
            if got: seen.append(got); lines.append(f"email | {folder.name} | {run.name if run else 'runs'} | {got}")
        if folder.is_symlink(): lines.append(f"email | {folder.name} | link refused")  # never entered
    n = [len(seen)] + [seen.count(s) for s in STAGES]
    print("\n".join(lines + ["email | " + " ".join(f"{k}={v}" for k, v in zip(LABELS, n)) + " | records only what the files say"]))
    return 1 if n[5] or n[6] else 0

def main(argv):
    if argv in (["--help"], ["-h"]): return print(USAGE) or 0
    opt = ages(argv[2:] if argv[:1] == ["check"] else argv[4:] if argv[:1] == ["draft"] and argv[2:3] == ["--drafts"] else [])
    if opt and len(argv) >= 2 and argv[0] == "check": return check(argv[1], None, *opt)
    if len(argv) == 2 and argv[0] == "status": return check(argv[1], status)
    if opt and len(argv) >= 4 and argv[0] == "draft" and argv[2] == "--drafts": return draft(argv[1], argv[3], *opt)
    return print(USAGE, file=sys.stderr) or 2
if __name__ == "__main__": sys.exit(main(sys.argv[1:]))
