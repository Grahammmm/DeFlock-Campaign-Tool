"""Private release-link register: portal links found in agency mail, with stated expiry.

LISTS ONLY. This module never fetches, resolves, opens or tests a link: it imports no
network library and reads only text already extracted into the ledger (same hash-bound
membership as received_items). Fetching stays with the owner and the root-owned portal
allowlist. Links are deterministic rule matches on the platforms in PLATFORMS (data);
nothing is agency- or jurisdiction-specific. Query strings and token-like path segments
are never stored: a link is kept as host + masked path + a one-way hash for dedupe.
Output is review-required, private, and not for publication.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import stat
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..ledger import store
from ..ledger_catalog import scope_excluded
from ..understanding.evidence import is_hash, read_pinned, sha
from . import received_items as ri

SCHEMA = "records-release-links-v1"
ARTIFACT = "release-links.json"
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_LINKS = 20000
WINDOW = 300  # characters between a link and the expiry wording that governs it
TIE = 40      # two different expiries this close in distance to one link are ambiguous
DISCLAIMER = ("Private register of links found in agency mail; REVIEW REQUIRED. Links are listed, "
              "never fetched or tested by this tool. An expiry is the date the agency's message "
              "states, not a legal conclusion. Not for publication.")

# (platform, host pattern). Data, not code paths: add a row to cover another portal vendor.
PLATFORMS = (
    ("nextrequest", re.compile(r"(?:^|\.)nextrequest\.com$")),
    ("sharepoint", re.compile(r"(?:^|\.)sharepoint\.com$")),
    ("opengov", re.compile(r"(?:^|\.)opengov(?:suite)?\.com$")),
    ("govqa", re.compile(r"(?:^|\.)govqa\.(?:us|com)$")),
)
URL = re.compile(r"https?://[^\s<>\"'\\^`{|}]+", re.I)
SAFELINK = re.compile(r"(?:^|\.)safelinks\.protection\.outlook\.com$")
TOKENISH = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_~=-]{16,}$")
_I = re.I
_DATE = (r"(?:(?:%s)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})"
         % ri.MONTH_RE)
LEAD = (r"(?:expires?|expiring|expiration(?:\s+date)?|(?:active|available|accessible|valid|open)\s+(?:until|through|till|by)|"
        r"(?:removed|deleted|disabled|deactivated)\s+(?:on|after))")
EXPIRY_DATE = re.compile(r"\b" + LEAD + r"\b[^.\n]{0,40}?\b(?:on\s+|by\s+)?(" + _DATE + r")", _I)
EXPIRY_REL = re.compile(r"\b(?:expires?|active|available|accessible|valid)\b[^.\n]{0,30}?\b(?:in|for|within|next)\s+(\d{1,3})\s+"
                        r"(hour|day|week)s?\b", _I)
NEGATED = re.compile(r"\b(?:does not|doesn't|never|no)\s+expire", _I)


def _need(condition, code):
    if not condition:
        raise ValueError(code)


def classify_url(raw):
    """-> (platform, host, path, query_present, normalised) or None. No network."""
    raw = html.unescape(raw).rstrip(".,;:!?)]>")
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if SAFELINK.search(host):  # Outlook wrapper: the real destination is the url= parameter
        inner = parse_qs(parts.query).get("url")
        if not inner:
            return None
        try:
            parts = urlsplit(inner[0])
            host = (parts.hostname or "").lower()
        except ValueError:
            return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    platform = next((name for name, pattern in PLATFORMS if pattern.search(host)), None)
    if platform is None:
        return None
    normalised = "%s://%s%s%s" % (parts.scheme, host, parts.path, "?" + parts.query if parts.query else "")
    return platform, host, parts.path, bool(parts.query), normalised


OPAQUE = re.compile(r"^[A-Za-z0-9_~=+%-]{16,}$")


def _opaque(segment):
    """Conservative: long single-blob, percent-encoded, mixed-case or digit-bearing segments are tokens."""
    if TOKENISH.match(segment):
        return True
    if not OPAQUE.match(segment):
        return False
    decoded = unquote(segment)
    if "%" in segment or re.search(r"\d|[A-Z]", decoded):
        return True
    return "-" not in decoded and "_" not in decoded


def redact_path(path):
    segments = [("[token]" if _opaque(s) else s[:80]) for s in path.split("/")]
    return "/".join(segments)[:200]


def redact_text(text):
    """Replace every URL in free text with its redacted form so no token reaches the output."""
    def swap(match):
        info = classify_url(match.group(0))
        if info is None:
            return "[link]"
        return "https://%s%s" % (info[1], redact_path(info[2]))
    return URL.sub(swap, text)


def link_kind(platform, path):
    low = path.lower()
    if platform == "nextrequest" and "/documents/" in low:
        return "document_download"
    if platform == "nextrequest" and "/requests/" in low:
        return "request_page"
    if platform == "sharepoint":
        return "shared_file"
    return "other"


def _expiry_matches(text, received_at):
    """[(position, expiry-dict)] for each stated expiry, without regard to which link it governs."""
    anchor = received_at.date() if received_at is not None else None
    found = []
    for hit in EXPIRY_DATE.finditer(text):
        if NEGATED.search(text[max(0, hit.start() - 20):hit.end()]):
            continue
        raw = hit.group(1)
        iso = None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            iso = raw
        elif re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw):
            month, day, year = (int(x) for x in raw.split("/"))
            iso = "%04d-%02d-%02d" % (year, month, day)
        else:
            iso = ri._date_from_text(raw, anchor)
        try:
            if iso:
                date.fromisoformat(iso)
        except ValueError:
            iso = None
        found.append((hit.start(), {"date_text": raw, "date": iso, "basis": "stated_date",
                                    "year_inferred": iso is not None and not re.search(r"\d{4}", raw)}))
    for hit in EXPIRY_REL.finditer(text):
        amount, unit = int(hit.group(1)), hit.group(2).lower()
        iso = None
        if received_at is not None:
            hours = amount * {"hour": 1, "day": 24, "week": 168}[unit]
            iso = (received_at + timedelta(hours=hours)).date().isoformat()
        found.append((hit.start(), {"date_text": hit.group(0), "date": iso, "basis": "relative_to_received",
                                    "year_inferred": False}))
    return found


def scan_text(text, received_at):
    """-> [{url info..., expiry (or None), expiry_state, quote}] for one unit's text, in order."""
    spans = [(m.start(), classify_url(m.group(0))) for m in URL.finditer(text)]
    spans = [(pos, info) for pos, info in spans if info is not None]
    out = []
    matches = _expiry_matches(text, received_at)
    for upos, info in spans:
        near = sorted((abs(epos - upos), epos, expiry) for epos, expiry in matches if abs(epos - upos) <= WINDOW)
        expiry, state = None, "none_stated"
        if len(near) > 1 and near[1][0] - near[0][0] <= TIE and near[1][2]["date"] != near[0][2]["date"]:
            state = "ambiguous"  # two different statements equally close: never guess which governs
        elif near:
            _, epos, expiry = near[0]
            state = "stated" if expiry["date"] else "unresolved"
            lo = max(0, min(epos, upos) - 60)
            hi = min(len(text), max(epos, upos) + 160)
            while lo > 0 and text[lo - 1] not in " \t\r\n<>\"'":  # never cut a URL: a cut tail would skip redaction
                lo -= 1
            while hi < len(text) and text[hi] not in " \t\r\n<>\"'":
                hi += 1
            expiry = dict(expiry, quote=" ".join(redact_text(text[lo:hi]).split())[:400])
        out.append({"platform": info[0], "host": info[1], "path": info[2], "query_present": info[3],
                    "normalised": info[4], "expiry": expiry, "expiry_state": state})
    return out


def link_id(url_sha256):
    return ri._digest(["records-release-link-v1", url_sha256])


def collect(db, processing, *, run_id):
    """Pure read-only register. Same admission and hash-bound text as received_items."""
    db.row_factory = sqlite3.Row
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    report = {"schema": SCHEMA, "run_id": run_id, "review_required": True, "legal_conclusion": False,
              "never_fetched": True, "disclaimer": DISCLAIMER, "links": [], "diagnostics": []}
    needed = {"originals", "occurrences", "mail_messages", "joins", "requests"}
    if not needed <= tables or not {"extraction_adapter_current", "extraction_adapter_imports", "units"} <= tables:
        report["diagnostics"].append("inventory_tables_unavailable")
        return report
    mail_times = {}
    for row in ri._rows(db, "SELECT eml_sha256,received_at FROM mail_messages ORDER BY id"):
        when = ri._utc(row["received_at"])
        if when is not None and row["eml_sha256"]:
            mail_times.setdefault(row["eml_sha256"], []).append(when)
    occurrences = {}
    for row in ri._rows(db, "SELECT id,original_sha256,parent_occurrence_id,acquired_at FROM occurrences ORDER BY id"):
        occurrences.setdefault(row["original_sha256"], []).append(row)
    owner = {r["id"]: r["original_sha256"] for rows in occurrences.values() for r in rows}
    joins = {}
    for row in ri._rows(db, "SELECT j.original_sha256,j.agency_id,j.request_id,r.agency_id AS request_agency "
                            "FROM joins j LEFT JOIN requests r ON r.id=j.request_id WHERE j.status='typed' ORDER BY j.id"):
        joins.setdefault(row["original_sha256"], []).append(row)
    merged = {}
    for subject in [r["sha256"] for r in ri._rows(db, "SELECT sha256 FROM originals ORDER BY sha256")]:
        _need(is_hash(subject), "release_links_original_hash")
        try:
            original = store.effective_original(db, subject)
        except ValueError:
            original = None
        if original is None or scope_excluded(original) or original["role"] not in ri.ADMITTED_ROLES:
            if original is None:
                report["diagnostics"].append("effective_original_unavailable")
            continue
        agencies = {j["agency_id"] for j in joins.get(subject, [])}
        requests = {j["request_id"] for j in joins.get(subject, []) if j["request_id"] is not None}
        agency_id = next(iter(agencies)) if len(agencies) == 1 else None
        request_id = next(iter(requests)) if len(requests) == 1 else None
        if any(j["request_id"] is not None and j["request_agency"] != j["agency_id"] for j in joins.get(subject, [])):
            agency_id = request_id = None
        if request_id is not None and agency_id is None:
            request_id = None
        times = sorted(mail_times.get(subject, []))
        received_at = times[0] if times else None
        if received_at is None:
            parents = {owner.get(o["parent_occurrence_id"]) for o in occurrences.get(subject, [])
                       if o["parent_occurrence_id"]} - {None}
            parent_times = [t for p in parents for t in mail_times.get(p, [])]
            if len(parents) == 1 and parent_times:
                received_at = sorted(parent_times)[0]
            else:
                acquired = [t for t in (ri._utc(o["acquired_at"]) for o in occurrences.get(subject, [])) if t]
                received_at = sorted(acquired)[0] if acquired else None
        try:
            units, diagnostics, _ = ri._current_units(db, processing, subject, paged=False)
        except ValueError as error:  # a corrupt binding omits this original's text, never the whole register
            report["diagnostics"].append("text_binding_refused:" + str(error))
            continue
        for unit in units:
            for found in scan_text(unit["text"], received_at):
                digest = sha(found["normalised"].encode())
                entry = merged.setdefault(digest, {
                    "link_id": link_id(digest), "url_sha256": digest, "platform": found["platform"],
                    "kind": link_kind(found["platform"], found["path"]), "host": found["host"],
                    "url_redacted": "https://%s%s" % (found["host"], redact_path(found["path"])),
                    "query_present": found["query_present"], "sources": [], "_expiries": [], "diagnostics": []})
                source = {"item_id": ri.item_id(subject), "original_sha256": subject, "agency_id": agency_id,
                          "request_id": request_id, "page": unit["page"], "unit_id": unit["unit_id"],
                          "received_at": received_at.isoformat().replace("+00:00", "Z") if received_at else None}
                if source not in entry["sources"]:
                    entry["sources"].append(source)
                entry["_expiries"].append((found["expiry_state"], found["expiry"], source))
        report["diagnostics"] += [d for d in diagnostics if d != "page_locator_unavailable"]
    _need(len(merged) <= MAX_LINKS, "release_links_bound")
    for entry in merged.values():
        states = entry.pop("_expiries")
        dated = [(e["date"], e, s) for st, e, s in states if st == "stated"]
        entry["sources"].sort(key=lambda s: (s["received_at"] or "", s["original_sha256"], s["unit_id"]))
        entry["agency_ids"] = sorted({s["agency_id"] for s in entry["sources"] if s["agency_id"]})
        entry["expiry"], entry["expiry_state"] = None, "none_stated"
        if dated:
            first = min(dated, key=lambda d: (d[0], d[2]["original_sha256"]))
            entry["expiry"] = dict(first[1], original_sha256=first[2]["original_sha256"], unit_id=first[2]["unit_id"],
                                   page=first[2]["page"])
            entry["expiry_state"] = "stated"
            if len({d[0] for d in dated}) > 1:
                entry["diagnostics"].append("expiry_conflict_earliest_used")
        elif any(st == "ambiguous" for st, _, _ in states):
            entry["expiry_state"] = "ambiguous"
        elif any(st == "unresolved" for st, _, _ in states):
            entry["expiry_state"] = "unresolved"
        entry["diagnostics"].sort()
        report["links"].append(entry)
    report["links"].sort(key=lambda e: (e["expiry"]["date"] if e["expiry"] else "9999-99-99", e["link_id"]))
    report["diagnostics"] = sorted(set(report["diagnostics"]))
    return report


def status(link, now, warn_days=7):
    """Status at `now` (aware datetime). Computed on read; the stored artifact is clock-free."""
    expiry = link.get("expiry")
    if not expiry or not expiry.get("date"):
        return "expiry_unknown", None
    days = (date.fromisoformat(expiry["date"]) - now.astimezone(timezone.utc).date()).days
    if days < 0:
        return "expired", days
    return ("expiring_soon" if days <= warn_days else "active"), days


def write_run_release_links(project, run_id):
    """Outputs-stage hook, called after write_run_received_items. Writes only
    run-outputs/<run>/outcomes/release-links.json (0700/0600). Identical replay is a
    no-op; a different existing artifact is refused. Nothing is fetched."""
    from ..run_stages import LedgerBusy, snapshot_ro
    try:
        _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is not None, "release_links_run_id")
        project = ri._safe_dir(project)
        processing = ri._safe_dir(project / "processing-v1")
        run_dir = ri._safe_dir(project / "run-outputs" / run_id)
        _need(not (processing / "ledger.sqlite").is_symlink(), "release_links_symlink_path")
        with snapshot_ro(processing / "ledger.sqlite") as db:
            report = collect(db, processing, run_id=run_id)
        encoded = (ri._canon(report) + "\n").encode()
        _need(len(encoded) <= MAX_OUTPUT_BYTES, "release_links_output_bound")
        outcomes = ri._safe_dir(run_dir / "outcomes")
        outcomes.mkdir(mode=0o700, exist_ok=True)
        os.chmod(outcomes, 0o700)
        target = outcomes / ARTIFACT
        if os.path.lexists(target):
            info = target.lstat()
            _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "release_links_existing_output")
            _need(read_pinned(target, sha(encoded), MAX_OUTPUT_BYTES) == encoded, "release_links_existing_output")
            return report
        temporary = outcomes / ("." + ARTIFACT + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if os.path.lexists(target):
                raise ValueError("release_links_existing_output")
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return report
    except (ValueError, sqlite3.Error, LedgerBusy) as error:
        raise OSError("RELEASE_LINKS_UNAVAILABLE") from error


def load(project, run_id):
    path = Path(os.path.abspath(project)) / "run-outputs" / run_id / "outcomes" / ARTIFACT
    _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is not None, "run_id_invalid")
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ValueError("register_missing") from None
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_OUTPUT_BYTES, "register_file")
    try:
        report = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise ValueError("register_json") from None
    _need(type(report) is dict and report.get("schema") == SCHEMA and report.get("run_id") == run_id
          and type(report.get("links")) is list, "register_stale")
    return report


def render(report, now, warn_days):
    lines = ["Release-link register (run %s) at %s: %d link(s). Listed only, never fetched. Review required."
             % (report["run_id"], now.strftime("%Y-%m-%d"), len(report["links"]))]
    for link in report["links"]:
        state, days = status(link, now, warn_days)
        when = link["expiry"]["date"] if link["expiry"] else link["expiry_state"]
        warn = {"expired": "EXPIRED %dd ago" % -days if days is not None else "EXPIRED",
                "expiring_soon": "EXPIRES in %dd" % days if days is not None else "EXPIRES soon"}.get(state, state)
        lines.append("%-22s %-11s %s  %s  agency=%s  sources=%d  [%s]" % (
            warn, link["platform"], when, link["url_redacted"], ",".join(link["agency_ids"]) or "unattributed",
            len(link["sources"]), link["link_id"][:12]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records release-links",
                                     description="List release links found in agency mail, with expiry warnings. Never fetches.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--now", help="ISO-8601 clock override for tests and reviews")
    parser.add_argument("--warn-days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now_invalid")
        report = load(args.project, args.run_id)
    except ValueError as error:
        print("records release-links: refused " + (str(error) or "now_invalid"), file=sys.stderr)
        return 2
    if args.json:
        listed = dict(report, links=[dict(l, status=status(l, now, args.warn_days)[0]) for l in report["links"]])
        print(json.dumps(listed, sort_keys=True))
    else:
        print(render(report, now, args.warn_days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
