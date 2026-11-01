#!/usr/bin/env python3
"""DF1537-12 flock-publish brief: one agency's human-approved PUBLISH.json entries -> one private Markdown draft.
Run as `flock_publish.py brief STORE --agency SLUG --out FILE.md` (or this file with the same arguments).
Fixed template only: no model text and no new prose about the agency. The draft is never sent, posted or built;
any send needs G's approval. Stdout carries counts only, never entry text or cite labels."""
import contextlib, importlib.util, io, json, os, re, sys
from pathlib import Path
TOOLS = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("_flock_publish", TOOLS / "flock_publish.py")
publish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish)
FIRST = "DRAFT - not sent - G approval required"
LAST = "END OF BRIEF DRAFT. Do not send, post or share without G's approval. Contact details are intentionally omitted."
def name(path):  # a folder name is echoed only when it is a plain short token
    return path.name if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", path.name) else "?"
def inside(path, root):
    return path == root or root in path.parents
def refuse(lines):
    print("".join("brief refused: %s\n" % line for line in lines), end="")
    return 1
def out_errors(raw, store, repo):  # -> named problems; --out is a new file outside the repo and the store's agencies tree
    out = raw.resolve()
    errs = ["--out is inside the repository"] if inside(out, repo) else []
    if inside(out, (store / "agencies").resolve()):
        errs.append("--out is inside STORE/agencies")
    if raw.is_symlink() or raw.exists():  # a dangling link is refused too, never followed
        errs.append("--out already exists")
    elif not out.parent.is_dir():
        errs.append("--out folder does not exist")
    return errs
def checked(run):  # -> (skip reason or None, approved entries in file order)
    target = run / "PUBLISH.json"
    if run.is_symlink() or target.is_symlink():  # links are never followed or read
        return "link", []
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):  # check's per-entry lines are captured, never printed
            failed = publish.check(target, run)
    except Exception:  # noqa: BLE001 - fail closed: a run that cannot be re-derived is skipped
        failed = 1
    if failed:
        return "check failed", []
    doc = json.loads(target.read_text(encoding="utf-8"))
    return None, [f for f in doc["findings"] if f["review"]["status"] == "approved"]
def collect(runs):  # newest run first, file order within a run; the same text twice -> listed once (newest)
    entries, by_text, lines, used, skipped = [], {}, [], 0, 0
    for run in sorted((p for p in runs.iterdir() if p.is_symlink() or os.path.lexists(p / "PUBLISH.json")), reverse=True):
        why, approved = checked(run)
        if why:
            lines.append("brief | %s | skipped: %s" % (name(run), why))
            skipped += 1
            continue
        used += 1
        for f in approved:
            if f["text"] in by_text:
                by_text[f["text"]]["also"].append(run.name)
                continue
            by_text[f["text"]] = {"entry": f, "run": run.name, "also": []}
            entries.append(by_text[f["text"]])
    return entries, lines, used, skipped
def cite(c):
    return "- %s (%s)" % (c["label"], c["url"]) if c["kind"] == "law" else "- %s" % c["label"]
def render(agency, entries):
    out = [FIRST, "# %s — findings brief (%d approved findings)" % (agency, len(entries)), publish.HEADLINE, ""]
    for item in entries:
        f, also = item["entry"], "".join(" (also run %s)" % d for d in item["also"])
        out += ["## %s (%s, %s, run %s)%s" % (f["finding_id"], f["topic"], f["claim_class"], item["run"], also), ""]
        out += ["> " + line for line in f["text"].splitlines()] + ["", "Sources:"] + [cite(c) for c in f["cites"]]
        out += ["", "Approval: %s (%s)" % (f["review"]["approval_ref"], f["review"]["approved_on"]), ""]
    return "\n".join(out + [LAST]) + "\n"
def link(path, data):  # temp + os.link: an existing file is never replaced and the temp never outlives the call
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.link(tmp, path)
    finally:
        os.unlink(tmp)
def brief(store, agency, out, repo):
    raw = Path(out)
    if not publish.SLUG.fullmatch(agency):
        return refuse(["--agency is not a slug"])
    errs = out_errors(raw, store, repo)
    if errs:
        return refuse(errs)
    folder = store / "agencies" / agency
    if (store / "agencies").is_symlink() or folder.is_symlink() or (folder / "runs").is_symlink():
        return refuse(["agency folder is a link"])
    if not (folder / "runs").is_dir():
        return refuse(["agency has no runs folder"])
    entries, lines, used, skipped = collect(folder / "runs")
    print("".join(line + "\n" for line in lines), end="")
    if not entries:
        return refuse(["nothing approved"])
    body = render(agency, entries)
    if publish.privacy.EMAIL.search(body) or publish.privacy.PHONE.search(body):  # contact details never enter a brief
        return refuse(["an approved entry carries an email or phone pattern"])
    link(raw.resolve(), body.encode("utf-8"))
    print("brief | agency=%s runs=%d approved=%d skipped_runs=%d written=%s" % (agency, used, len(entries), skipped, raw.name))
    return 0
if __name__ == "__main__":
    sys.exit(publish.main(["brief", *sys.argv[1:]]))
