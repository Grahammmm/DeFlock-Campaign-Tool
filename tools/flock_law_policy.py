#!/usr/bin/env python3
"""Private LAW.csv + per-agency POLICY.csv: --check and versions. Prints ids, labels, dates, counts only."""
import csv
import hashlib
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool import law  # noqa: E402

LAW_HEADER = ["rule_id", "section", "duty", "official_url", "text_sha256", "accessed", "effective_from",
              "effective_to", "review", "added_by", "added_at", "status"]
POLICY_HEADER = ["policy_row_id", "agency", "policy_id", "version_label", "doc_sha256", "footer_version_date",
                 "adoption_evidence", "adoption_sha256", "governs_from", "governs_to", "added_by", "added_at", "status"]
HOSTS = {"leginfo.legislature.ca.gov", "oag.ca.gov"}
DAY, HEX, SET = "must be YYYY-MM-DD", "must be 64 lowercase hex", "is empty"
ISDAY, ISHEX = (lambda v: day(v)), (lambda v: re.fullmatch("[0-9a-f]{64}", v))
COMMON = {"added_by": (bool, SET), "added_at": (ISDAY, DAY),
          "status": (lambda v: v in ("unreviewed", "checked", "disputed"), "must be unreviewed, checked or disputed")}
FIELDS = {  # field: (predicate, reason); rules needing the package or other fields follow in problems()
    "law": {"section": (bool, SET), "accessed": (ISDAY, DAY), "text_sha256": (ISHEX, HEX), **COMMON,
            "duty": (lambda v: 0 < len(v) <= 200, "must be 1 to 200 characters"),
            "official_url": (lambda v: v.startswith("https://") and v[8:].split("/")[0] in HOSTS, "host not official")},
    "policy": {"policy_id": (bool, SET), "version_label": (bool, SET), "doc_sha256": (ISHEX, HEX), **COMMON,
               "footer_version_date": (ISDAY, DAY), "adoption_evidence": (lambda v: 0 < len(v) <= 160, "must be 1 to 160 characters")}}

def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

def day(v):
    try:
        return date.fromisoformat(v) if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v) else None
    except ValueError:
        return None

def derived_id(r):
    key = (slug(r["agency"]), r["policy_id"], r["version_label"], r["doc_sha256"])
    return "P-" + hashlib.sha256("\x1f".join(key).encode()).hexdigest()[:12]

def problems(r, kind, ctx):  # yield (field, reason); ctx = package rules (law) or agency folder (policy)
    for field, (ok, why) in FIELDS[kind].items():
        if not ok(r[field]):
            yield field, why
    lo, hi = ("effective_from", "effective_to") if kind == "law" else ("governs_from", "governs_to")
    ev = r.get("adoption_evidence")
    if kind == "law":
        rule = ctx.get(r["rule_id"])
        if rule is None:
            yield "rule_id", "not in law package"
        for k, want in (("review", "review"), (lo, "effective_from")):
            if rule and r[k] != rule[want]:
                yield k, "differs from law package"
    else:
        if r["policy_row_id"] != derived_id(r):
            yield "policy_row_id", "is not the derived id"
        if not r["agency"] or (ctx and slug(r["agency"]) != ctx):
            yield "agency", "is empty or does not match its folder"
        if ev not in ("", "unverified") and not re.fullmatch("[0-9a-f]{64}", r["adoption_sha256"]):
            yield "adoption_sha256", HEX
        if ev == "unverified":
            for k in (lo, hi):
                if r[k]:
                    yield k, "needs adoption evidence"
            return
    if (kind == "law" or ev) and not day(r[lo]):
        yield lo, DAY
    if r[hi] and (not day(r[hi]) or (day(r[lo]) and day(r[hi]) < day(r[lo]))):
        yield hi, "must be empty or a date not before " + lo

def load(path, kind, folder=None):
    header = LAW_HEADER if kind == "law" else POLICY_HEADER
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            table = list(csv.reader(fh))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return [], [f"row 1: file unreadable ({type(exc).__name__})"]
    if not table or table[0] != header:
        return [], [f"row 1: header does not match {kind.upper()}.csv"]
    ctx, rows, errs, seen = folder, [], [], set()
    if kind == "law":
        try:
            ctx = {x["rule_id"]: x for x in law.load_package("us-ca")["rules"]}
        except (ValueError, KeyError, TypeError):
            return [], ["row 1: law package unreadable"]
    for n, cells in enumerate(table[1:], 2):
        if len(cells) != len(header):
            errs += [f"row {n}: columns expected {len(header)}, found {len(cells)}"] if cells else []
            continue
        r = dict(zip(header, (c.strip() for c in cells)))
        rows.append((n, r))
        errs += [f"row {n}: {f} {why}" for f, why in problems(r, kind, ctx)]
        if r[header[0]] in seen:
            errs.append(f"row {n}: {header[0]} duplicates an earlier row")
        seen.add(r[header[0]])
    return rows, errs

def load_law(path): return load(path, "law")
def load_policy(path, folder=None): return load(path, "policy", folder)

def load_all(path):  # -> (law_rows, policy_rows, errors) for LAW.csv, POLICY.csv or a store dir
    p = Path(path)
    if not p.is_dir():
        is_policy = p.is_file() and p.read_bytes()[:400].decode("utf-8-sig", "replace").splitlines()[:1] == [",".join(POLICY_HEADER)]
        rows, errs = load_policy(p) if is_policy else load_law(p)
        return ([], rows, errs) if is_policy else (rows, [], errs)
    law_rows, errs = load_law(p / "LAW.csv") if (p / "LAW.csv").is_file() else ([], [])
    policy_rows, files = [], sorted(p.glob("agencies/*/POLICY.csv"))
    if not files and not law_rows and not errs:
        errs.append("store: no LAW.csv or agencies/*/POLICY.csv found")
    for f in files:
        rows, e = load_policy(f, f.parent.name)
        policy_rows += rows
        errs += [f"{f.parent.name}: {x}" for x in e]
    return law_rows, policy_rows, errs

def versions(rows, agency, on):
    out = []
    for r in sorted((r for _, r in rows if slug(r["agency"]) == agency), key=lambda r: (r["footer_version_date"], r["policy_row_id"])):
        adopted = r["adoption_evidence"] != "unverified"
        if on and not (adopted and r["governs_from"] <= on and (not r["governs_to"] or on <= r["governs_to"])):
            continue
        span = f"{r['governs_from']}..{r['governs_to'] or 'open'}" if adopted else "not established"
        out.append(f"{r['policy_row_id']} | {r['policy_id']} | {r['version_label']} | footer {r['footer_version_date']}"
                   f" | adoption {'verified' if adopted else 'unverified'} | governs {span}")
    return out

USAGE = "usage: flock_law_policy.py --check PATH | versions PATH --agency SLUG [--on YYYY-MM-DD]"

def main(argv):
    if argv in (["--help"], ["-h"]):
        print(USAGE); return 0  # noqa: E702
    if len(argv) == 2 and argv[0] == "--check":
        law_rows, policy_rows, errs = load_all(argv[1])
        print("\n".join(errs + [f"law={len(law_rows)} policy={len(policy_rows)} errors={len(errs)}"]))
        return 1 if errs else 0
    on = argv[5] if len(argv) == 6 else None
    if len(argv) not in (4, 6) or argv[0] != "versions" or argv[2] != "--agency" or (on and (argv[4] != "--on" or not day(on))):
        print(USAGE, file=sys.stderr)
        return 2
    _, policy_rows, errs = load_all(argv[1])
    if errs:
        print("\n".join(errs), file=sys.stderr)
        return 1
    lines = versions(policy_rows, slug(argv[3]), on)
    if on and not lines:
        print(f"no adopted version established for {on}")
        return 3
    print("\n".join(lines))
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
