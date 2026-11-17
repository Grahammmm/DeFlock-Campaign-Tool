"""Count other agencies' produced logs that name this agency as the searcher (REC-XLOG-1). Counts only; read-only."""
import argparse
import csv
import hashlib
import io
import os
import re
import stat
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from campaign_tool.search_log import _header_key, normalize_org_name

MAX_BYTES = 100 * 1024 * 1024
MAX_ROWS = 2000000
ROLES = {
    "time": {"search time", "timestamp", "time", "date", "date time", "datetime", "search date", "searched at"},
    "org": {"org name", "organization", "organisation", "agency", "searching agency", "searching organization", "agency name"},
    "ori": {"ori", "agency ori", "org ori"},
    "case": {"case #", "case number", "case no", "case", "case id", "incident number"},
}
BLANK = {"", "n/a", "na", "none", "null", "-"}
NAIVE = ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y")
LA = ZoneInfo("America/Los_Angeles")
COLS = "file format rows matched unresolved dup_looking first_date last_date case_fill date_basis status".split()
FOOTER = "# counts and categories only; no plate, case number or user name is printed"


class Refuse(Exception):
    pass


def text(cell):
    return "" if cell is None else str(cell).strip()


def pacific(cell):
    """(YYYY-MM-DD, basis) with basis conv, stated or none."""
    if isinstance(cell, datetime):
        value = cell
    else:
        raw = text(cell)
        value = None
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            for fmt in NAIVE:
                try:
                    value = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
    if value is None:
        return "", "none"
    if value.tzinfo is not None:
        return value.astimezone(LA).strftime("%Y-%m-%d"), "conv"
    return value.strftime("%Y-%m-%d"), "stated"


def scan_rows(header, rows, aliases, ori):
    """Pure scan of one header and its rows; returns a counts dict with a status."""
    keys = [_header_key(text(h)) for h in header]
    where = {role: [i for i, k in enumerate(keys) if k in names] for role, names in ROLES.items()}
    if not where["time"] or not (where["org"] or where["ori"]):
        return {"status": "not_a_log"}
    if any(len(v) > 1 for v in where.values()):
        return {"status": "ambiguous_columns"}
    pos = {role: (v[0] if v else None) for role, v in where.items()}
    names = {normalize_org_name(a) for a in aliases}
    out = {"status": "ok", "rows": 0, "matched": 0, "unresolved": 0, "dup": 0, "filled": 0, "dates": [],
           "basis": {"conv": 0, "stated": 0, "none": 0}, "case_col": pos["case"] is not None}
    seen = set()

    def cell(row, role):
        i = pos[role]
        return text(row[i]) if i is not None and i < len(row) else ""

    for row in rows:
        if not any(text(c) for c in row):
            continue
        out["rows"] += 1
        if out["rows"] > MAX_ROWS:
            return {"status": "too_large"}
        org, code = cell(row, "org"), cell(row, "ori")
        if (org and normalize_org_name(org) in names) or (ori and code and code.casefold() == ori.casefold()):
            out["matched"] += 1
            digest = hashlib.sha256("\x1f".join(text(c) for c in row).encode("utf-8", "replace")).hexdigest()
            out["dup"] += digest in seen
            seen.add(digest)
            day, basis = pacific(row[pos["time"]] if pos["time"] < len(row) else "")
            out["basis"][basis] += 1
            if day:
                out["dates"].append(day)
            out["filled"] += cell(row, "case").casefold() not in BLANK
        elif not org and not code:
            out["unresolved"] += 1
    return out


def open_regular(root, rel):
    """File object of a regular non-symlink file inside root, or None; the size check uses the opened descriptor."""
    if not rel or os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        return None
    joined = root
    for part in rel.split("/"):
        joined = os.path.join(joined, part)
        if os.path.islink(joined):
            return None
    real = os.path.realpath(joined)
    if os.path.commonpath([root, real]) != root:
        return None
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        return None
    return os.fdopen(fd, "rb"), info.st_size


def file_results(root, rel, aliases, ori):
    """Yield (label, format, result) for one index path."""
    fmt = rel.rsplit(".", 1)[-1].lower()
    if fmt == "pdf":
        yield rel, fmt, {"status": "pdf_not_parsed"}
        return
    opened = open_regular(root, rel)
    if opened is None:
        yield rel, fmt, {"status": "refused_outside_root"}
        return
    handle, size = opened
    with handle:
        if size > MAX_BYTES:
            yield rel, fmt, {"status": "too_large"}
            return
        try:
            if fmt == "csv":
                reader = csv.reader(io.TextIOWrapper(handle, encoding="utf-8-sig", newline=""))
                header = next(reader, None)
                yield rel, fmt, scan_rows(header, reader, aliases, ori) if header else {"status": "not_a_log"}
                return
            try:
                from openpyxl import load_workbook
            except ImportError:
                yield rel, fmt, {"status": "xlsx_parser_missing"}
                return
            book = load_workbook(handle, read_only=True, data_only=True)
            for sheet in book.worksheets:
                rows = sheet.iter_rows(values_only=True)
                header = next((r for r in rows if any(text(c) for c in r)), None)
                yield f"{rel}#{sheet.title}", fmt, scan_rows(header, rows, aliases, ori) if header else {"status": "not_a_log"}
            book.close()
        except (csv.Error, UnicodeError, OSError, ValueError, KeyError, EOFError):
            yield rel, fmt, {"status": "unreadable"}
        except Exception:  # corrupt workbook archives raise assorted library errors
            yield rel, fmt, {"status": "unreadable"}


def line(label, fmt, r):
    label = re.sub(r"[\x00-\x1f\x7f]", "?", label)
    if r["status"] != "ok":
        return [label, fmt, "", "", "", "", "", "", "", "", r["status"]]
    dates = sorted(r["dates"])
    fill = f"{100.0 * r['filled'] / r['matched']:.1f}%" if r["case_col"] and r["matched"] else "n/a"
    basis = "/".join(str(r["basis"][k]) for k in ("conv", "stated", "none"))
    return [label, fmt, r["rows"], r["matched"], r["unresolved"], r["dup"], dates[0] if dates else "",
            dates[-1] if dates else "", fill, basis, "ok"]


def load_aliases(path):
    try:
        with open(path, encoding="utf-8") as handle:
            lines = [x.strip() for x in handle.read().splitlines()]
    except (OSError, UnicodeError):
        raise Refuse("aliases") from None
    aliases = [x for x in lines if x and not x.startswith("#")]
    if not aliases or any(len(a) > 120 for a in aliases):
        raise Refuse("aliases")
    return aliases


def load_paths(path):
    try:
        if os.path.getsize(path) > MAX_BYTES:
            raise Refuse("index")
        with open(path, encoding="utf-8-sig", newline="") as handle:
            table = list(csv.reader(handle, delimiter="\t"))
    except (OSError, UnicodeError, csv.Error):
        raise Refuse("index") from None
    head = [h.strip().casefold() for h in (table[0] if table else [])]
    if "path" not in head:
        raise Refuse("index")
    p = head.index("path")
    r = head.index("role") if "role" in head else None
    paths = []
    for row in table[1:]:
        rel = row[p].strip() if p < len(row) else ""
        role = row[r].strip().casefold() if r is not None and r < len(row) else ""
        if role != "lead" and rel.lower().endswith((".csv", ".xlsx", ".pdf")) and rel not in paths:
            paths.append(rel)
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(prog="records agency-cross-logs", description=__doc__)
    ap.add_argument("--index", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--aliases", required=True)
    ap.add_argument("--ori")
    args = ap.parse_args(argv)
    try:
        if args.ori is not None and not re.fullmatch(r"[A-Za-z0-9]{3,16}", args.ori):
            raise Refuse("ori")
        if not os.path.isdir(args.root):
            raise Refuse("root")
        aliases = load_aliases(args.aliases)
        paths = load_paths(args.index)
    except Refuse as exc:
        print(f"refused agency_cross_logs_{exc}", file=sys.stderr)
        return 2
    root = os.path.realpath(args.root)
    print("\t".join(COLS))
    total = {"rows": 0, "matched": 0, "unresolved": 0, "dup": 0, "filled": 0, "dates": [], "case": 0,
             "basis": {"conv": 0, "stated": 0, "none": 0}}
    for rel in paths:
        for label, fmt, result in file_results(root, rel, aliases, args.ori):
            print("\t".join(str(c) for c in line(label, fmt, result)))
            if result["status"] == "ok":
                for k in ("rows", "matched", "unresolved", "dup", "filled"):
                    total[k] += result[k]
                total["dates"] += result["dates"]
                total["case"] += result["matched"] if result["case_col"] else 0
                for k, v in result["basis"].items():
                    total["basis"][k] += v
    total.update(status="ok", case_col=bool(total["case"]), matched=total["matched"])
    total["filled"] = total["filled"]
    row = line("TOTAL", "", total)
    if total["case"]:
        row[8] = f"{100.0 * total['filled'] / total['case']:.1f}%"
    print("\t".join(str(c) for c in row))
    print(FOOTER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
