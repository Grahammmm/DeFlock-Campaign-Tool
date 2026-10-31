#!/usr/bin/env python3
"""Private LAW.csv + per-agency POLICY.csv: --check, versions, governing (each agency on a date) and gaps. Prints ids, labels, dates, counts only."""
import csv
import hashlib
import os
import re
import sys
from datetime import date, timedelta
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
        except (ValueError, KeyError, TypeError, OSError):
            return [], ["row 1: law package unreadable"]
    adopted = []  # policy rows with no error of their own and adoption evidence: the governing windows
    for n, cells in enumerate(table[1:], 2):
        if len(cells) != len(header):
            errs += [f"row {n}: columns expected {len(header)}, found {len(cells)}"] if cells else []
            continue
        r = dict(zip(header, (c.strip() for c in cells)))
        rows.append((n, r))
        before = len(errs)
        errs += [f"row {n}: {f} {why}" for f, why in problems(r, kind, ctx)]
        if r[header[0]] in seen:
            errs.append(f"row {n}: {header[0]} duplicates an earlier row")
        seen.add(r[header[0]])
        if kind == "policy" and len(errs) == before and r["adoption_evidence"] != "unverified":
            adopted.append((n, r))
    return rows, errs + overlaps(adopted)

def overlaps(rows):  # one error per pair of windows of one agency + policy_id sharing a day (inclusive; open = +inf)
    groups, pairs = {}, []
    for n, r in rows:
        key = (slug(r["agency"]), r["policy_id"])
        groups.setdefault(key, []).append((r["governs_from"], n, r["governs_to"] or "9999-12-31"))
    for group in groups.values():
        group.sort()  # by (governs_from, row); ISO dates compare as text
        for i, (b_from, n, _) in enumerate(group):
            pairs += [(n, m) for _, m, a_to in group[:i] if b_from <= a_to]  # a.from <= b.from <= b.to holds
    return [f"row {n}: governs_from overlaps row {m} (same agency and policy_id)" for n, m in sorted(pairs)]

def load_law(path): return load(path, "law")
def load_policy(path, folder=None): return load(path, "policy", folder)

def load_all(path):  # -> (law_rows, policy_rows, errors) for LAW.csv, POLICY.csv or a store dir
    p = Path(path)
    if not p.is_dir():
        try:
            is_policy = p.is_file() and p.read_bytes()[:400].decode("utf-8-sig", "replace").splitlines()[:1] == [",".join(POLICY_HEADER)]
        except OSError as exc:
            return [], [], [f"row 1: file unreadable ({type(exc).__name__})"]
        rows, errs = load_policy(p) if is_policy else load_law(p)
        return ([], rows, errs) if is_policy else (rows, [], errs)
    law_rows, errs = load_law(p / "LAW.csv") if (p / "LAW.csv").is_file() else ([], [])
    linked = os.path.islink(p / "agencies")  # never follow or resolve a link: its name is not the agency it points at
    # lexists, not glob("agencies/*/POLICY.csv"): Python 3.11's glob drops a dangling POLICY.csv link unreported.
    policy_rows, files = [], [] if linked else sorted(
        d / "POLICY.csv" for d in p.glob("agencies/*") if d.is_dir() and os.path.lexists(d / "POLICY.csv"))
    errs += ["store: agencies is a link"] if linked else []
    if not files and not law_rows and not errs:
        errs.append("store: no LAW.csv or agencies/*/POLICY.csv found")
    for f in files:
        if os.path.islink(f.parent) or os.path.islink(f):
            errs.append(f"{f.parent.name}: POLICY.csv is a link")
            continue
        rows, e = load_policy(f, f.parent.name)
        policy_rows += rows
        errs += [f"{f.parent.name}: {x}" for x in e]
    return law_rows, policy_rows, errs

def governs_on(rows, agency, on):  # the agency's rows in versions() order; with on, only adopted rows governing that day
    return [r for r in sorted((r for _, r in rows if slug(r["agency"]) == agency), key=lambda r: (r["footer_version_date"], r["policy_row_id"]))
            if not on or (r["adoption_evidence"] != "unverified" and r["governs_from"] <= on and (not r["governs_to"] or on <= r["governs_to"]))]

def versions(rows, agency, on):
    out = []
    for r in governs_on(rows, agency, on):
        adopted = r["adoption_evidence"] != "unverified"
        span = f"{r['governs_from']}..{r['governs_to'] or 'open'}" if adopted else "not established"
        out.append(f"{r['policy_row_id']} | {r['policy_id']} | {r['version_label']} | footer {r['footer_version_date']}"
                   f" | adoption {'verified' if adopted else 'unverified'} | governs {span}")
    return out

def governing(law_rows, policy_rows, on):  # one line per agency (sorted slug), LAW rows in effect, counts; ids and dates only
    out, n, agencies = [], {"governing": 0, "other_dates": 0, "unverified_only": 0}, sorted({slug(r["agency"]) for _, r in policy_rows})
    for a in agencies:
        hits, mine = governs_on(policy_rows, a, on), governs_on(policy_rows, a, None)
        out += [f"governing | {a} | {r['policy_id']} | {r['version_label']} | governs {r['governs_from']}..{r['governs_to'] or 'open'}"
                f" | policy_row {r['policy_row_id']}" for r in hits]
        kind = "governing" if hits else "unverified_only" if all(r["adoption_evidence"] == "unverified" for r in mine) else "other_dates"
        out += [] if hits else [f"governing | {a} | " + (f"none on {on} (adopted versions exist for other dates)" if kind == "other_dates"
                                                       else f"none established (adoption unverified for all {len(mine)} rows)")]
        n[kind] += 1
    live = sum(r["effective_from"] <= on and (not r["effective_to"] or on <= r["effective_to"]) for _, r in law_rows)
    return out + [f"law in effect on {on}: {live} of {len(law_rows)} rows",
                  f"agencies={len(agencies)} " + " ".join(f"{k}={v}" for k, v in n.items()) + " | research view, not a legal conclusion"]

def gaps(policy_rows, on):  # per (agency, policy_id): days no adopted window governs, a closed last window, nothing adopted
    groups, out, n = {}, [], {"gaps": 0, "closed": 0, "unestablished": 0}
    for _, r in policy_rows:
        groups.setdefault((slug(r["agency"]), r["policy_id"]), []).append(r)
    for (a, pid), rows in sorted(groups.items()):
        won = sorted((r for r in rows if r["adoption_evidence"] != "unverified"), key=lambda r: r["governs_from"])
        for prev, nxt in zip(won, won[1:]):  # a clean load has no overlap, so prev is never open-ended here
            end, start = day(prev["governs_to"]), day(nxt["governs_from"])
            if end and start and (start - end).days > 1:
                out.append(f"{a} | {pid} | gap {end + timedelta(days=1)}..{start - timedelta(days=1)}")
                n["gaps"] += 1
        last = day(won[-1]["governs_to"]) if won else None
        if last and last < day(on):
            out.append(f"{a} | {pid} | closed {last} (nothing governs on {on})")
            n["closed"] += 1
        if not won:
            out.append(f"{a} | {pid} | not established ({len(rows)} unverified)")
            n["unestablished"] += 1
    agencies = len({a for a, _ in groups})
    return out + [f"agencies={agencies} policies={len(groups)} " + " ".join(f"{k}={v}" for k, v in n.items())]

USAGE = ("usage: flock_law_policy.py --check PATH | versions PATH --agency SLUG [--on YYYY-MM-DD]"
         " | governing STORE --on YYYY-MM-DD | gaps STORE --on YYYY-MM-DD")

def main(argv):
    try:
        return run(argv)
    except OSError as exc:  # belt and braces: the error class only, never a path or content
        print(f"io: {type(exc).__name__}", file=sys.stderr)
        return 1

def run(argv):
    if argv in (["--help"], ["-h"]):
        print(USAGE); return 0  # noqa: E702
    if len(argv) == 2 and argv[0] == "--check":
        law_rows, policy_rows, errs = load_all(argv[1])
        print("\n".join(errs + [f"law={len(law_rows)} policy={len(policy_rows)} errors={len(errs)}"]))
        return 1 if errs else 0
    if argv[:1] in (["governing"], ["gaps"]):  # STORE must be a directory; any store error -> stderr, nothing on stdout, exit 1
        if len(argv) != 4 or argv[2] != "--on" or not day(argv[3]) or not Path(argv[1]).is_dir():
            print(USAGE, file=sys.stderr)
            return 2
        law_rows, policy_rows, errs = load_all(argv[1])
        out = errs or (governing(law_rows, policy_rows, argv[3]) if argv[0] == "governing" else gaps(policy_rows, argv[3]))
        print("\n".join(out), file=sys.stderr if errs else sys.stdout)
        return 1 if errs else 3 if argv[0] == "gaps" and len(out) > 1 else 0  # gaps: every line but the count is a finding
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
