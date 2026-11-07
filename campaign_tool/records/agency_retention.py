"""Read-only retention statements; validation is not source or legal acceptance."""
import argparse
import csv
import io
import re
from itertools import combinations
from pathlib import Path

HEADER = "agency,system,vendor,data_layer,stated_period,period_days,bound,source_kind,quote,doc_sha256,page,confidence,checked".split(",")
ENUMS = {
    "vendor": {"flock", "motorola-vigilant", "axon", "other", "unknown"},
    "data_layer": {"detections", "search_logs", "access_logs", "audit_trail", "downloaded_data", "unspecified"},
    "bound": {"exact", "min", "max", "unknown"},
    "source_kind": {"policy", "order_form", "contract", "agency_letter", "website"},
    "confidence": {"high", "medium", "low"},
    "checked": {"VERIFIED-ORIGINAL", "UNVERIFIED"},
}


def load(path):
    """Return valid rows and per-physical-line problems, preserving quoted text."""
    rows, problems, seen = [], [], set()

    def problem(line, field, reason):
        problems.append({"line": line, "field": field, "reason": reason})

    try:
        p = Path(path)
        if p.is_symlink() or not p.is_file() or p.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("not a bounded regular file")
        reader = csv.reader(io.StringIO(p.read_bytes().decode("utf-8-sig"), newline=""), strict=True)
        if next(reader, None) != HEADER:
            return [], [{"line": 0, "field": "header", "reason": "must be exactly: " + ",".join(HEADER)}]
        while True:
            line = reader.line_num + 1
            cells = next(reader, None)
            if cells is None:
                break
            if not cells:
                continue
            if len(cells) != len(HEADER):
                problem(line, "row", "wrong column count")
                continue
            r = dict(zip(HEADER, cells))
            before = len(problems)
            for field in HEADER:
                if field != "period_days" and not r[field].strip():
                    problem(line, field, "required")
            for field, choices in ENUMS.items():
                if r[field] not in choices:
                    problem(line, field, "unknown enum")
            for field, limit in (("stated_period", 80), ("quote", 400)):
                if len(r[field]) > limit:
                    problem(line, field, "too long")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", r["doc_sha256"]):
                problem(line, "doc_sha256", "not 64 hex characters")
            for field, minimum in (("page", 1), ("period_days", 0)):
                value = r[field]
                if field == "period_days" and not value:
                    continue
                if not re.fullmatch(r"[0-9]{1,10}", value) or int(value) < minimum:
                    problem(line, field, "not a valid whole number")
            if r["confidence"] == "high" and r["checked"] != "VERIFIED-ORIGINAL":
                problem(line, "checked", "high confidence requires VERIFIED-ORIGINAL")
            if len(problems) != before:
                continue
            key = tuple(r[k] for k in ("agency", "system", "data_layer")) + (r["doc_sha256"].lower(), int(r["page"]))
            if key in seen:
                problem(line, "row", "duplicate key")
                continue
            seen.add(key)
            rows.append(r)
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        return [], [{"line": 0, "field": "file", "reason": str(exc)[:100]}]
    return rows, problems


def summary(rows, agency):
    """Group one agency's complete cited statements by data layer in file order."""
    grouped = {}
    for row in rows:
        if row["agency"] == agency:
            grouped.setdefault(row["data_layer"], []).append(row)
    return grouped


def compare(rows, agency):
    """Describe stated differences, not compliance or actual archiving practices."""
    selected = [r for r in rows if r["agency"] == agency]
    if not selected:
        return ["No retention statement on file."]
    lines = []
    for a, b in combinations(selected, 2):
        if a["system"] != b["system"]:
            continue
        same = a["data_layer"] == b["data_layer"] and a["data_layer"] != "unspecified"
        if same and a["bound"] == b["bound"] == "exact" and a["period_days"] and b["period_days"] and int(a["period_days"]) != int(b["period_days"]):
            lines.append(f"Different stated periods for the same data: {a['stated_period']} ({a['doc_sha256']} p.{a['page']}) and {b['stated_period']} ({b['doc_sha256']} p.{b['page']}). Review.")
        for order, policy in ((a, b), (b, a)):
            if policy["source_kind"] != "policy":
                continue
            if order["source_kind"] == "order_form" and order["vendor"] != "unknown" and order["data_layer"] != policy["data_layer"]:
                lines.append(f"{order['vendor']} {order['data_layer']}: {order['stated_period']} (order form). Policy, {policy['data_layer']}: {policy['stated_period']}. These cover different data; they differ in scope, not necessarily in conflict.")
            if same and order["vendor"] == "flock" and order["source_kind"] != "policy" and order["bound"] == "exact" and order["period_days"] and int(order["period_days"]) == 30 and policy["period_days"] and policy["bound"] in {"exact", "min"} and int(policy["period_days"]) > 30:
                lines.append("Policy period is longer than the Flock 30-day default for this data. Whether data is archived is not shown by these documents.")
    return list(dict.fromkeys(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records agency-retention", description=__doc__)
    parser.add_argument("file")
    args = parser.parse_args(argv)
    rows, problems = load(args.file)
    print(f"agency-retention: {len(rows)} valid rows, {len(problems)} problems (review required)")
    for p in problems:
        print(f"line {p['line']} {p['field']}: {p['reason']}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
