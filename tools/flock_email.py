#!/usr/bin/env python3
"""Wrap FOLLOWUP-DRAFT.md into a private EMAIL-DRAFT.md. Draft only: it writes files and cannot send. `status STORE` reads each run's email stage back.

G's approval is a FIELD, never a sentence: EMAIL-APPROVAL.json beside the draft, {"state": "approved", ...}, bound by
content_sha256 to the exact draft text and holding G's SSH signature. Only tools/flock_approve.py writes it, after the
signature verifies; `flock_approve.py verify RUNDIR` re-checks the signature. A "G APPROVED:" line grants nothing. This
file stays free of subprocess/campaign_tool imports (no send path), so here only the field's shape and text binding count."""
import csv, hashlib, importlib.util, json, os, re, sys, tempfile
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
         "       flock_email.py status STORE  (each run's email stage; exit 1 on malformed or sent without approval)\n"
         "       flock_email.py lint RUNDIR --drafts DIR  (run after any edit and before asking G to approve; reads only, exit 1 on findings)")
PLACEHOLDER = re.compile(r"\{\{|\}\}|(?<![a-z])(?:todo|xxx|tbd)|[\[<]insert|____", re.I)
STAGES = ("follow-up drafted", "email drafted", "approval recorded", "sent recorded", "malformed", "inconsistent: sent without approval")
LABELS = "runs followup drafted approved sent malformed inconsistent".split()
APPROVAL, KEYS = "EMAIL-APPROVAL.json", {"v", "state", "scope", "subject", "content_sha256", "approved_by", "signature"}

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

def layout(run):
    """Why RUNDIR breaks the <store>/agencies/<slug>/runs/<date>-outside-the-repository rule, or None."""
    if not (len(run.parents) >= 4 and run.parent.name == "runs" and run.parents[2].name == "agencies"):
        return "RUNDIR must be <store>/agencies/<slug>/runs/<date>"
    return "RUNDIR is inside the repository" if REPO in (run, *run.parents) else None

def draft(rundir, drafts, max_age=None, today=None):
    run, out_dir = Path(rundir).resolve(), Path(drafts).resolve()
    if bad := layout(run): return print(f"draft: {bad}", file=sys.stderr) or 1
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
                      f"Related prior requests: {ids.group(1) if ids else 'none on file'}.", "---",
                      f"Approval: only a signed {APPROVAL} beside this file records G's approval; no line here can.",
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

def marks(lines):
    """The values of every `Sent by:` line (stage and lint share this parse). Approval is never read from a line."""
    return [ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("Sent by:")]

def approved_text(run):
    """(subject, text G approves) for a run: EMAIL-DRAFT.md without its `Sent by:` record line. Links are refused."""
    with open(os.open(run / "EMAIL-DRAFT.md", os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as fh: text = fh.read()
    return f"{run.parent.parent.name}/{run.name}", "\n".join(ln for ln in text.splitlines() if not ln.startswith("Sent by:")) + "\n"

def approval_field(run):
    """The approval record when well-formed and bound to the current draft text; False when absent; None when bad."""
    if not os.path.lexists(run / APPROVAL): return False
    try:
        with open(os.open(run / APPROVAL, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as fh: rec = json.load(fh)
        subject, body = approved_text(run)
        ok = isinstance(rec, dict) and set(rec) == KEYS and rec["v"] == 1 and rec["state"] == "approved" \
            and rec["scope"] == "flock-email" and rec["subject"] == subject and isinstance(rec["approved_by"], str) \
            and rec["approved_by"].strip() != "" and isinstance(rec["signature"], str) and "BEGIN SSH SIGNATURE" in rec["signature"] \
            and rec["content_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
        return rec if ok else None
    except (OSError, ValueError, UnicodeDecodeError): return None

def stage(run):
    """The email stage of one run folder, or None without a draft file. Approval comes from the state field only."""
    email, follow = run / "EMAIL-DRAFT.md", run / "FOLLOWUP-DRAFT.md"
    if run.is_symlink() or email.is_symlink() or follow.is_symlink() or (run / APPROVAL).is_symlink(): return "refused: link"
    if not email.exists(): return "follow-up drafted" if follow.exists() else None
    try: lines = email.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError): return "malformed"
    sent, yes = marks(lines), approval_field(run)
    if lines[:1] != [HEAD] or len(sent) != 1 or yes is None: return "malformed"
    sent = bool(sent[0]) and not sent[0].startswith("____")
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

def lint(rundir, drafts):
    """One `lint | slug | date | rule` line per broken rule, then a summary. Never prints the offending text; writes nothing."""
    run = Path(rundir).resolve()
    if bad := layout(run): return print(f"lint: {bad}", file=sys.stderr) or 1
    name, copy = run.parent.parent.name, run / "EMAIL-DRAFT.md"
    twin = Path(drafts).resolve() / f"{name}-{run.name}-EMAIL-DRAFT.md"
    if copy.is_symlink() or twin.is_symlink(): return print("lint: link refused", file=sys.stderr) or 1  # never read
    try: raw = copy.read_bytes()
    except OSError: return print("lint: EMAIL-DRAFT.md missing", file=sys.stderr) or 1
    try: other = twin.read_bytes()
    except OSError: other = None
    rules, lines = [], raw.decode("utf-8", "replace").splitlines()
    if other is None: rules.append("twin missing")
    elif other != raw:
        a, b = raw.split(b"\n"), other.split(b"\n")
        rules.append(f"copies differ (first differing line {next((i for i, (x, y) in enumerate(zip(a, b), 1) if x != y), min(len(a), len(b)) + 1)})")
    cut = [i for i, ln in enumerate(lines) if ln == "---"]
    to = lines[1][4:].strip() if len(lines) > 1 and lines[1].startswith("To: ") else None
    subject = lines[2][9:] if len(lines) > 2 and lines[2].startswith("Subject: ") else ""
    if lines[:1] != [HEAD] or to is None or not subject or len(cut) < 2 or len(marks(lines)) != 1: rules.append("malformed")
    body = "\n".join(lines[cut[0] + 1:cut[1]] if len(cut) > 1 else lines[3:])  # no --- pair: scan the rest (fail closed)
    addr, errs, _ = contact(run.parents[3] / "agencies" / name / "CONTACT.csv", name)
    if errs or to != addr: rules.append(f"to: CONTACT.csv {errs[0]}" if errs else "to: not the current records address")
    if PLACEHOLDER.search(subject + "\n" + body): rules.append("placeholder")
    privacy = _privacy()
    allowed = {a.lower() for a in (to, addr) if a}  # the records address may appear; every other address is private
    scan = privacy.EMAIL.sub(lambda m: " " if m.group(0).lower().lstrip("<([\"'") in allowed else m.group(0), subject + "\n" + body)
    rules += [f"private: {c}" for c in privacy.categories(scan)]
    if len(subject) > 150 or len(body) > 8000: rules.append("length")
    print("\n".join([f"lint | {name} | {run.name} | {r}" for r in rules] +
                    [f"lint | rules={len(rules)} | {stage(run)} | draft only; G approval required before any send"]))
    return 1 if rules else 0

def _privacy():  # loaded by file path like report/page/publish do; the patterns are never copied
    spec = importlib.util.spec_from_file_location("_email_flock_privacy", REPO / "tools" / "flock_privacy.py")
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module

def main(argv):
    if argv in (["--help"], ["-h"]): return print(USAGE) or 0
    opt = ages(argv[2:] if argv[:1] == ["check"] else argv[4:] if argv[:1] == ["draft"] and argv[2:3] == ["--drafts"] else [])
    if opt and len(argv) >= 2 and argv[0] == "check": return check(argv[1], None, *opt)
    if len(argv) == 2 and argv[0] == "status": return check(argv[1], status)
    if opt and len(argv) >= 4 and argv[0] == "draft" and argv[2] == "--drafts": return draft(argv[1], argv[3], *opt)
    if len(argv) == 4 and argv[0] == "lint" and argv[2] == "--drafts": return lint(argv[1], argv[3])
    return print(USAGE, file=sys.stderr) or 2
if __name__ == "__main__": sys.exit(main(sys.argv[1:]))
