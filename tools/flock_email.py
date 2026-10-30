#!/usr/bin/env python3
"""Wrap FOLLOWUP-DRAFT.md into a private EMAIL-DRAFT.md. Draft only: it writes files and cannot send."""
import csv
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HEAD = "DRAFT - not sent - G approval required"
COLS = "agency,role,email,source_url,source_sha256,checked_at,added_by".split(",")
CHECKS = {"role": r"records", "email": r"[^@\s,;<>]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "source_url": r"https://\S+",
          "source_sha256": r"[0-9a-f]{64}", "checked_at": r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "added_by": r".*\S.*"}
USAGE = "usage: flock_email.py draft RUNDIR --drafts DIR  (writes EMAIL-DRAFT.md; draft only, never sends)"


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def contact(path, folder):
    """Return (the one records address, error lines) from CONTACT.csv."""
    try:
        rows = list(csv.reader(Path(path).read_text(encoding="utf-8").splitlines()))
    except (OSError, UnicodeDecodeError):
        return None, ["contact: CONTACT.csv missing or unreadable"]
    if not rows or rows[0] != COLS:
        return None, ["row 1: header does not match CONTACT.csv v1"]
    errs, found = [], []
    for n, cells in enumerate(rows[1:], 2):
        row = dict(zip(COLS, cells)) if len(cells) == len(COLS) else {}
        bad = [k for k, p in CHECKS.items() if not re.fullmatch(p, row.get(k, ""))]
        bad += ["agency"] if slug(row.get("agency", "")) != folder else []
        errs += [f"row {n}: {k} is invalid" for k in bad]
        found += [] if bad else [row["email"]]
    return (found or [None])[0], errs or ([] if len(found) == 1 else ["contact: no records row" if not found else "contact: more than one records row"])


def draft(rundir, drafts):
    run, out_dir = Path(rundir).resolve(), Path(drafts).resolve()
    name = run.parent.parent.name
    try:
        lines = (run / "FOLLOWUP-DRAFT.md").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    ends = [i for i, line in enumerate(lines) if line.startswith("END OF REQUEST DRAFT.")]
    if not (ends and lines[0] == HEAD and re.fullmatch(r"Subject: \S.*", lines[2] if len(lines) > 2 else "")):
        return print("draft: follow-up draft malformed", file=sys.stderr) or 1
    to, errs = contact(run.parents[3] / "agencies" / name / "CONTACT.csv", name)
    outs = [run / "EMAIL-DRAFT.md", out_dir / f"{name}-{run.name}-EMAIL-DRAFT.md"]
    errs = errs or (["draft: --drafts is inside the repository"] if REPO in (out_dir, *out_dir.parents) else [])
    errs = errs or (["draft: EMAIL-DRAFT already exists"] if any(p.exists() for p in outs) else [])
    if errs:
        return print("\n".join(errs), file=sys.stderr) or 1
    body = "\n".join(lines[3:ends[0]]).strip("\n")
    ids = re.search(r"related to requests ([^\n]*?)\.(?:\s|$)", body)
    text = "\n".join([HEAD, f"To: {to}", lines[2], "---", body, "", "Please reference this request by your agency's request number in any response.",
                      f"Related prior requests: {ids.group(1) if ids else 'none on file'}.", "---", "G APPROVED: ____",
                      "Sent by: ____ (operator, by hand, after G's reply)"]) + "\n"
    out_dir.mkdir(mode=0o700, exist_ok=True)
    for path in outs:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.link(tmp, path)
        finally:
            os.unlink(tmp)
    return print(f"written=2 to_domain={to.rsplit('@', 1)[1]}") or 0


def main(argv):
    if argv in (["--help"], ["-h"]):
        return print(USAGE) or 0
    if len(argv) == 4 and argv[0] == "draft" and argv[2] == "--drafts":
        return draft(argv[1], argv[3])
    return print(USAGE, file=sys.stderr) or 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
