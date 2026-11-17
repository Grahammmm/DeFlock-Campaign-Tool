"""Phone-sized 'which agency next' list from the slo-pilot agencies.tsv (read only).

Required columns: agency, current_stage, last_contact, next_action and exactly one
column whose name starts with 'overdue'. Optional: days_since_last_contact, owner.
Only agency, current_stage, next_action and owner are ever printed.
"""
import argparse
import csv
import datetime
import io
import json
import re
import stat
import sys
from pathlib import Path

MAX_BYTES = 1 << 20
MAX_LINES = 8
LIMITS = (16, 43, 22, 8)
OVERDUE_WHY = "overdue on our records, not a legal finding"
REQUIRED = ("agency", "current_stage", "last_contact", "next_action")
_SCRUB = re.compile(r"https?://\S+|[^\s@]+@[^\s@]+")


class Refusal(Exception):
    pass


def _clean(text):
    return " ".join(_SCRUB.sub(" ", text or "").split())


def _cut(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _date(text):
    try:
        return datetime.date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def load(path):
    p = Path(path)
    try:
        st = p.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            raise Refusal("file_invalid")
        text = p.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise Refusal("file_invalid")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    names = [(n or "").strip() for n in (reader.fieldnames or [])]
    reader.fieldnames = names
    for name in REQUIRED:
        if name not in names:
            raise Refusal("column_missing:" + name)
    over = [n for n in names if n.startswith("overdue")]
    if not over:
        raise Refusal("column_missing:overdue*")
    if len(over) > 1:
        raise Refusal("column_ambiguous:overdue*")
    return list(reader), over[0]


def rank(rows, over_col, today=None):
    out = []
    for r in rows:
        agency = _clean(r.get("agency"))
        if not agency:
            continue
        overdue = (r.get(over_col) or "").strip().lower().startswith("yes")
        nxt = _clean(r.get("next_action"))
        if nxt.lower().startswith("none") and not overdue:
            continue
        days = None
        d = _date(r.get("last_contact")) if today else None
        if d:
            days = (today - d).days
        else:
            m = re.match(r"\s*(\d+)", r.get("days_since_last_contact") or "")
            days = int(m.group(1)) if m else None
        why = OVERDUE_WHY if overdue else _clean(r.get("current_stage")) or "unknown"
        nxt = nxt if nxt and not nxt.lower().startswith("none") else "none recorded"
        who = _clean(r.get("owner")) or "unset"
        fields = [_cut(v, n) for v, n in zip((agency, why, nxt, who), LIMITS)]
        out.append({"overdue": overdue, "days": days, "agency": agency, "f": fields})
    out.sort(key=lambda x: (not x["overdue"], x["days"] is None, -(x["days"] or 0), x["agency"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="records agency-next",
                                 description="Rank which agency to work next (read only).")
    ap.add_argument("--agencies", required=True)
    ap.add_argument("--today")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        today = None
        if args.today is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.today) or not _date(args.today):
                raise Refusal("today_invalid")
            today = _date(args.today)
        rows, over_col = load(args.agencies)
    except Refusal as e:
        print("agency-next: %s" % e, file=sys.stderr)
        return 1
    ranked = rank(rows, over_col, today)
    top = ranked[:MAX_LINES]
    lines = [" | ".join(x["f"]) for x in top]
    if args.json:
        print(json.dumps({"count": len(top), "total_ranked": len(ranked), "rows": [
            {"rank": i + 1, "agency": x["f"][0], "overdue": x["overdue"], "days": x["days"],
             "why": x["f"][1], "next": x["f"][2], "who": x["f"][3], "line": ln}
            for i, (x, ln) in enumerate(zip(top, lines))]}, indent=2))
    else:
        for ln in lines:
            print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
