"""Read-only evidence check for published findings (RF2010-1).

For every published finding in ``content/findings/`` and every record it cites
by SHA-256, report whether the hash is in the public library
``content/sources.json`` and whether its original was ingested: a row in the
``objects`` table of ``private/ledger.sqlite`` and a regular, non-symlink file
``private/objects/<sha256>``. The ledger is opened read-only. Receipts (source
ids, original names) are never read, objects are never hashed (``status
--check`` does that) and the report names only the first 12 hex of a hash.

Problem codes: ``unregistered`` (not in sources.json), ``not_ingested`` (no
ledger row), ``object_missing`` (ledger row but no regular file).
"""
import json
import os
import sqlite3
import stat

from .content import load_content

PREFIX = 12


def _ledger_hashes(path):
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            return {row[0] for row in db.execute("SELECT sha256 FROM objects")}
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise ValueError("ledger unreadable: " + type(exc).__name__) from None


def _object_present(objects, sha):
    try:
        info = os.lstat(objects / sha)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat.S_ISREG(info.st_mode)


def evidence_report(root):
    """Return the evidence report dict for the campaign at ``root``; never writes."""
    model = load_content(root)
    if model is None:
        return {"findings": 0, "citations": 0, "problems": [], "content": "absent"}
    private = root / "private"
    objects = private / "objects"
    ledger = private / "ledger.sqlite"
    if private.is_symlink() or objects.is_symlink() or ledger.is_symlink():
        raise ValueError("private/, private/objects and private/ledger.sqlite must not be symlinks")
    present = ledger.is_file()
    stored = _ledger_hashes(ledger) if present else set()
    registered = {entry["sha256"] for entry in model["sources"]}
    citations, problems = 0, []
    for finding in model["findings"]:
        for sha in sorted({source["sha256"] for source in finding["sources"]}):
            citations += 1
            codes = []
            if sha not in registered:
                codes.append("unregistered")
            if sha not in stored:
                codes.append("not_ingested")
            elif not _object_present(objects, sha):
                codes.append("object_missing")
            problems.extend({"finding": finding["slug"], "sha256": sha[:PREFIX], "code": code}
                            for code in codes)
    problems.sort(key=lambda item: (item["finding"], item["sha256"], item["code"]))
    return {"findings": len(model["findings"]), "citations": citations, "problems": problems,
            "ledger": "present" if present else "absent"}


def run_cli(root, check=False):
    """Print the report; with ``check`` exit 3 when any problem is listed."""
    report = evidence_report(root)
    print(json.dumps(report, indent=2))
    if check and report["problems"]:
        raise SystemExit(3)
