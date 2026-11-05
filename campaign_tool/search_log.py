"""kit-search-log: private, read-only counts from a locally saved ALPR access or search log (agency-native CSV).

Streams one UTF-8 CSV (at most 100 MiB) and prints aggregate counts only: rows read and skipped by reason, searches per
month, per searching organization (and state), and per purpose category. Purpose text is classified and then dropped; no
user name, case number, plate value or purpose text is ever printed or written. Nothing is copied, saved, fetched or
published. A count is a lead, not evidence. Every error message is fixed text: never a cell value or the file path.
"""
import csv
import hashlib
import io
import json
import os
import re
import stat
import sys
from datetime import date

from . import kit_agencies
from .privacy_patterns import categories as privacy_categories

WARNING = "A count is a lead, not evidence"
MAX_BYTES = 100 * 1024 * 1024
CATEGORIES_MAX_BYTES = 64 * 1024
CHUNK = 64 * 1024
ALIASES = {
    "time": ("timestamp", "time", "date", "search time", "search date", "searched at"),
    "reason": ("purpose", "reason", "search purpose", "stated purpose", "justification"),
    "org": ("organization", "organisation", "searching organization", "agency", "searching agency"),
    "state": ("state", "searching state", "agency state"),
}
FLAGS = {"time": "--time-col", "reason": "--reason-col", "org": "--org-col", "state": "--state-col"}
DEFAULT_TERMS = {
    "blank_or_na": ("", "n/a", "na", "none", "null", "not applicable"),
    "vague": ("investigation", "inv"),
    "immigration_related": ("immigration", "immigrant", "deportation", "deport", "ice", "cbp", "border patrol",
                            "homeland security"),
}
PURPOSES = ("blank_or_na", "immigration_related", "vague", "other")  # precedence order, then the remainder
SKIP_REASONS = ("blank_row", "column_count", "invalid_time")

DATE_ONLY = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
ISO = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.[0-9]+)?)?"
                 r"(?:Z|[+-]([0-9]{2}):([0-9]{2}))?")
US = re.compile(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4}) ([0-9]{1,2}):([0-9]{2}):([0-9]{2})(?: ?([AaPp][Mm]))?")


def _has_control(text):
    return any(ord(c) < 32 or 127 <= ord(c) < 160 or c in "\u2028\u2029" for c in text)


def _collapse(text):
    return " ".join(text.casefold().split())


def _header_key(text):
    return _collapse(text.replace("_", " "))


def _open_regular(path, what, limit):
    """(file object, fstat) of a regular non-symlink file of at most ``limit`` bytes; fixed-message ValueError otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError(f"{what} cannot be opened as a regular file (symlinks are refused)") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{what} must be a regular file")
        if info.st_size > limit:
            raise ValueError(f"{what} is larger than {limit // 1024} KiB" if limit < 1024 * 1024
                             else f"{what} is larger than {limit // (1024 * 1024)} MiB")
        return os.fdopen(fd, "rb", buffering=0), info
    except BaseException:
        os.close(fd)
        raise


class _Reader(io.RawIOBase):
    """Raw reader that hashes every byte it hands out and refuses to pass ``limit`` bytes."""

    def __init__(self, handle, limit, what):
        self.handle, self.limit, self.what = handle, limit, what
        self.sha, self.total = hashlib.sha256(), 0

    def readable(self):
        return True

    def readinto(self, buffer):
        count = self.handle.readinto(buffer) or 0
        self.sha.update(memoryview(buffer)[:count])
        self.total += count
        if self.total > self.limit:
            raise ValueError(f"{self.what} grew past its size limit while it was being read")
        return count


# ---- purpose categories -------------------------------------------------------------------------------------------

def _term_regex(terms):
    parts = sorted({re.escape(_collapse(t)) for t in terms if _collapse(t)}, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)") if parts else None


def load_terms(categories_path=None):
    """-> (terms dict, sha256 of the custom file or None). The custom file replaces all three default lists."""
    if categories_path is None:
        return DEFAULT_TERMS, None
    handle, _ = _open_regular(categories_path, "--categories file", CATEGORIES_MAX_BYTES)
    with handle:
        data = handle.read(CATEGORIES_MAX_BYTES + 1)
    if len(data) > CATEGORIES_MAX_BYTES:
        raise ValueError("--categories file is larger than 64 KiB")

    def pairs(items):
        keys = [k for k, _ in items]
        if len(set(keys)) != len(keys):
            raise ValueError("--categories file has a duplicate key")
        return dict(items)

    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except ValueError as exc:
        if str(exc) == "--categories file has a duplicate key":
            raise
        raise ValueError("--categories file is not valid UTF-8 JSON") from None
    if not isinstance(obj, dict) or set(obj) != set(DEFAULT_TERMS):
        raise ValueError("--categories file must be an object with exactly the keys blank_or_na, vague, immigration_related")
    for key, terms in obj.items():
        ok = (isinstance(terms, list) and len(terms) <= 100 and len(set(map(str, terms))) == len(terms)
              and all(isinstance(t, str) and len(t) <= 80 and not _has_control(t) for t in terms))
        if not ok:
            raise ValueError(f"--categories file: {key} must be an array of at most 100 distinct strings of at most 80 "
                             "characters without control characters")
        if key == "immigration_related" and any(not t.strip() for t in terms):
            raise ValueError("--categories file: immigration_related must not contain an empty term")
    return obj, hashlib.sha256(data).hexdigest()


def _classifier(terms):
    blank = {_collapse(t) for t in terms["blank_or_na"]} | {""}
    vague = {_collapse(t) for t in terms["vague"]}
    immigration = _term_regex(terms["immigration_related"])

    def classify(text):
        norm = _collapse(text)
        if norm in blank:
            return "blank_or_na"
        if immigration is not None and immigration.search(norm):
            return "immigration_related"
        if norm in vague:
            return "vague"
        return "other"
    return classify


# ---- dates --------------------------------------------------------------------------------------------------------

def parse_date(text):
    """-> (year, month, day) as stated in one of the accepted formats, or None. No timezone conversion."""
    text = text.strip()
    try:
        m = DATE_ONLY.fullmatch(text)
        if m:
            y, mo, d = map(int, m.groups())
            date(y, mo, d)
            return y, mo, d
        m = ISO.fullmatch(text)
        if m:
            y, mo, d, hh, mm = (int(m.group(i)) for i in range(1, 6))
            ss = int(m.group(6) or 0)
            date(y, mo, d)
            if hh > 23 or mm > 59 or ss > 59:
                return None
            if m.group(7) is not None and (int(m.group(7)) > 23 or int(m.group(8)) > 59):
                return None
            return y, mo, d
        m = US.fullmatch(text)
        if m:
            mo, d, y, hh, mm, ss = (int(m.group(i)) for i in range(1, 7))
            date(y, mo, d)
            meridiem = m.group(7)
            if mm > 59 or ss > 59 or (hh > 12 or hh < 1 if meridiem else hh > 23):
                return None
            return y, mo, d
    except ValueError:
        return None
    return None


# ---- columns ------------------------------------------------------------------------------------------------------

def _resolve_columns(header, columns):
    keys = [_header_key(h) for h in header]
    if any(not k for k in keys):
        raise ValueError("header row: a column name is empty")
    if len(set(keys)) != len(keys):
        raise ValueError("header row: two column names are the same once case, spaces and underscores are ignored")
    columns = columns or {}
    chosen = {}
    for role in ("time", "reason", "org", "state"):
        flag, explicit = FLAGS[role], columns.get(role)
        if explicit is not None:
            if not isinstance(explicit, str) or _has_control(explicit):
                raise ValueError(f"{flag} must not contain control characters")
            if explicit not in header:
                raise ValueError(f"{flag} names no column in the file")
            chosen[role] = header.index(explicit)
            continue
        found = [i for i, k in enumerate(keys) if k in ALIASES[role]]
        if len(found) > 1:
            raise ValueError(f"several columns look like the {role} column; name one with {flag}")
        if not found and role in ("time", "reason"):
            raise ValueError(f"no {role} column found; name it with {flag}")
        chosen[role] = found[0] if found else None
    used = [i for i in chosen.values() if i is not None]
    if len(set(used)) != len(used):
        raise ValueError("one column cannot be used for two roles (time, reason, org, state)")
    labels = {role: (header[i] if i is not None else None) for role, i in chosen.items()}
    for role, label in labels.items():
        if label is None:
            continue
        if _has_control(label):
            raise ValueError(f"the {role} column name contains a control character")
        hit = privacy_categories(label)
        if hit:
            raise ValueError(f"privacy lint: the {role} column name: {', '.join(hit)}")
    return chosen, labels


# ---- the summary --------------------------------------------------------------------------------------------------

def _check_label(value, row, what):
    if _has_control(value):
        raise ValueError(f"row {row}: the {what} cell contains a control character")
    hit = privacy_categories(value)
    if hit:
        raise ValueError(f"privacy lint: row {row}: {what}: {', '.join(hit)}")


def summarize_csv(csv_path, agency, *, columns=None, categories_path=None):
    """The v1 summary object (see docs/SEARCH-LOG.md) for the CSV at csv_path; ValueError with fixed text on any refusal."""
    terms, categories_sha = load_terms(categories_path)
    classify = _classifier(terms)
    handle, before = _open_regular(csv_path, "csv", MAX_BYTES)
    raw = _Reader(handle, MAX_BYTES, "csv")
    months, orgs, purposes = {}, {}, dict.fromkeys(PURPOSES, 0)
    skipped, read, first, last = dict.fromkeys(SKIP_REASONS, 0), 0, None, None
    unattributed = 0
    with handle:
        buffered = io.BufferedReader(raw, CHUNK)
        text = io.TextIOWrapper(buffered, encoding="utf-8-sig", newline="")
        reader = csv.reader(text, strict=True)
        try:
            header = next(reader, None)
            if header is None:
                raise ValueError("csv has no header row")
            chosen, labels = _resolve_columns(header, columns)
            t_i, r_i, o_i, s_i = chosen["time"], chosen["reason"], chosen["org"], chosen["state"]
            for cells in reader:
                read += 1
                if not any(c.strip() for c in cells):
                    skipped["blank_row"] += 1
                    continue
                if len(cells) != len(header):
                    skipped["column_count"] += 1
                    continue
                stamp = parse_date(cells[t_i])
                if stamp is None:
                    skipped["invalid_time"] += 1
                    continue
                org = state = None
                if o_i is not None:
                    org = cells[o_i] if cells[o_i].strip() else None
                    state = cells[s_i] if s_i is not None and cells[s_i].strip() else None
                    for value, what in ((org, "organization"), (state, "state")):
                        if value is not None:
                            _check_label(value, read, what)
                purposes[classify(cells[r_i])] += 1
                key = "%04d-%02d" % stamp[:2]
                months[key] = months.get(key, 0) + 1
                first, last = min(first or stamp, stamp), max(last or stamp, stamp)
                if org is None:
                    unattributed += 1
                else:
                    orgs[(org, state)] = orgs.get((org, state), 0) + 1
            while buffered.read(CHUNK):  # the digest covers every byte through EOF
                pass
        except csv.Error:
            raise ValueError(f"csv is malformed near line {reader.line_num}") from None
        except UnicodeDecodeError:
            raise ValueError("csv is not valid UTF-8") from None
        except OSError:
            raise ValueError("csv could not be read") from None
        after = os.fstat(handle.fileno())
    if raw.total != before.st_size or (after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev) != (
            before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev):
        raise ValueError("csv changed while it was being read (best-effort detection, not a snapshot guarantee)")
    counted = read - sum(skipped.values())
    iso = lambda stamp: "%04d-%02d-%02d" % stamp if stamp else None  # noqa: E731
    return {
        "schema_version": 1,
        "agency_id": agency["agency_id"], "agency_name": agency.get("name"),
        "input": {"sha256": raw.sha.hexdigest(), "columns": labels, "categories_sha256": categories_sha},
        "period": {"first_date": iso(first), "last_date": iso(last)},
        "rows": {"warning": WARNING, "read": read, "counted": counted, "skipped": skipped},
        "months": {"warning": WARNING, "rows": [{"month": k, "count": months[k]} for k in sorted(months)]},
        "organizations": {"warning": WARNING, "unattributed": unattributed, "rows": [
            {"name": n, "state": s, "count": c} for (n, s), c in sorted(orgs.items(), key=lambda i: (i[0][0], i[0][1] is not None, i[0][1] or ""))]},
        "purposes": {"warning": WARNING, "counts": purposes},
    }


def format_summary(summary):
    """Human text for a v1 summary; the same warning heads every section."""
    rows, columns = summary["rows"], summary["input"]["columns"]
    period = summary["period"]
    out = [f"Search log summary for {summary['agency_name']} ({summary['agency_id']})", "",
           f"== Input and rows == {WARNING}",
           f"input sha256: {summary['input']['sha256']}",
           "columns: " + ", ".join(f"{role}={columns[role]!r}" if columns[role] is not None else f"{role}=(none)"
                                   for role in ("time", "reason", "org", "state")),
           "custom categories sha256: " + (summary["input"]["categories_sha256"] or "(default term list)"),
           "dates as stated in the log: " + (f"{period['first_date']} to {period['last_date']}" if period["first_date"] else "(no counted rows)"),
           f"rows read: {rows['read']}", f"rows counted: {rows['counted']}"]
    out += [f"rows skipped, {reason}: {rows['skipped'][reason]}" for reason in SKIP_REASONS]
    out += ["", f"== Searches by month == {WARNING}"]
    out += [f"{r['month']}: {r['count']}" for r in summary["months"]["rows"]] or ["(no counted rows)"]
    orgs = summary["organizations"]
    out += ["", f"== Searches by searching organization and state == {WARNING}"]
    if columns["org"] is None:
        out.append(f"organization column not available; {orgs['unattributed']} searches are unattributed")
    else:
        out += [f"{r['name']} [{r['state'] or 'state not given'}]: {r['count']}" for r in orgs["rows"]]
        out.append(f"no organization given: {orgs['unattributed']}")
    out += ["", f"== Searches by purpose category == {WARNING}"]
    out += [f"{name}: {summary['purposes']['counts'][name]}" for name in PURPOSES]
    out += ["", "Purpose text is only counted, never shown. Read-only: the file was not copied, saved, uploaded or sent."]
    return "\n".join(out) + "\n"


def run_cli(root, args):
    """Validate the agency, summarize the whole file, then print once. Returns 0; refusals raise ValueError before any output."""
    from .cli import read_config  # local import: cli imports this module lazily
    read_config(root)
    agencies = [a for a in kit_agencies.load_agencies(root) if a["agency_id"] == args.agency]
    if len(agencies) != 1:
        raise ValueError("--agency is not an agency_id in kit/agencies.json")
    summary = summarize_csv(args.csv, agencies[0], categories_path=args.categories, columns={
        "time": args.time_col, "reason": args.reason_col, "org": args.org_col, "state": args.state_col})
    text = json.dumps(summary, ensure_ascii=True, indent=2) + "\n" if args.as_json else format_summary(summary)
    sys.stdout.write(text)
    return 0
