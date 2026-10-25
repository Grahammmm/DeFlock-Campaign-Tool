"""Read-only per-agency contact timeline over the canonical ledger.

Counts, identifiers and dates only. The ledger is read through the existing
correspondence projection reader (`campaign_tool.records.correspondence.snapshot`),
which opens `ledger/store.py` `ledger(path, readonly=True)` in one read
transaction. This module performs no file, network, model, mailbox or
subprocess operation, never parses headers or bodies, and never emits
addresses, subjects, filenames, names, titles, locators or document text.

Output is PRIVATE analysis and REVIEW REQUIRED: machine validity is not
semantic, legal, source or publication acceptance.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
import hashlib
import re

from .. import correspondence
from ..ledger.store import canonical
from ..ledger_catalog import scope_excluded

SCHEMA = "agency-timeline-report-v1"
UNKNOWN_LABEL = "(UNKNOWN)"
# Direction is derived from the IMAP folder name only (last hierarchy segment,
# case-folded). Headers are never parsed, so no address can leak or be guessed.
SENT_FOLDERS = frozenset({"sent", "sent mail", "sent items", "sent messages"})
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SAFE_STATUS = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
MAX_ANCESTRY = 100
ROW_COUNTS = ("messages_sent", "messages_received", "undated_messages", "request_count",
              "attachment_occurrences", "portal_items", "portal_items_unretrieved",
              "unresolved_joins", "unknown_domain_messages", "agency_conflict_messages")


class TimelineError(ValueError):
    """Stable error codes; never carries source content or local paths."""


def read_snapshot(path):
    """Existing read-only projection reader; failures become one stable code."""
    try:
        return correspondence.snapshot(path)
    except correspondence.CorrespondenceError as error:
        raise TimelineError(str(error)) from None


def snapshot_sha256(data):
    """Same binding as the correspondence report: canonical JSON of the snapshot."""
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


class _Withheld:
    def __init__(self):
        self.values = 0
        self.dates = 0

    def ident(self, value):
        if isinstance(value, str) and SAFE_ID.match(value):
            return value
        self.values += 1
        return "withheld-id-" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]

    def status(self, value):
        if isinstance(value, str) and SAFE_STATUS.match(value):
            return value
        self.values += 1
        return "status_not_token"

    def when(self, value):
        """Normalized UTC instant, a plain calendar date, or None."""
        if value is None:
            return None
        if isinstance(value, str) and DATE_ONLY.match(value):
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError:
                pass
        moment = instant(value)
        if moment is None:
            self.dates += 1
            return None
        return iso(moment)


def instant(value):
    """Timezone-aware instant in UTC, or None (missing, date-only, naive or invalid)."""
    if not isinstance(value, str) or not 0 < len(value) <= 64 or DATE_ONLY.match(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        # UTC normalization itself can overflow at the calendar bounds
        # (e.g. 0001-01-01T00:00:00+01:00); that is an invalid date, not a crash.
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, TypeError):
        return None


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def direction(folder):
    if not isinstance(folder, str):
        return "received"
    leaf = re.split(r"[/.]", folder.strip())[-1].strip().casefold()
    return "sent" if leaf in SENT_FOLDERS else "received"


def _chains(occurrences):
    """Per occurrence id: (encountered links, valid?).

    The encountered links are kept even when the lineage is invalid (missing
    parent, cycle or deeper than MAX_ANCESTRY), so an excluded ancestor met
    before the failure still excludes the occurrence (fail closed). An invalid
    lineage is never used as agency evidence.
    """
    result = {}
    for oid, row in occurrences.items():
        chain, seen, current, valid = [], set(), row, True
        while current is not None:
            if current["id"] in seen or len(seen) >= MAX_ANCESTRY:
                valid = False
                break
            seen.add(current["id"])
            chain.append(current)
            parent = current["parent_occurrence_id"]
            if parent and parent not in occurrences:
                valid = False
                break
            current = occurrences.get(parent) if parent else None
        result[oid] = (chain, valid)
    return result


def build(data):
    """Pure projection of one snapshot into the frozen report shape."""
    withheld = _Withheld()
    agencies = {row["id"]: row for row in data["agencies"]}
    requests = {row["id"]: row for row in data["requests"]}
    originals = {row["sha256"]: row for row in data["originals"]}
    excluded_originals = {sha for sha, row in originals.items() if scope_excluded(row)}
    occurrences = {row["id"]: row for row in data["occurrences"]}
    chains = _chains(occurrences)
    gaps = {"mail_messages_total": len(data["mail_messages"]), "visible_messages": 0,
            "scope_excluded_messages": 0, "scope_excluded_occurrences": 0,
            "scope_excluded_joins": 0, "ancestry_invalid_occurrences": 0,
            "ancestry_invalid_messages": 0, "ancestry_invalid_portal_items": 0,
            "distinct_attachment_originals": 0, "withheld_values": 0, "unparseable_dates": 0}

    # Exclusion is decided over every encountered link, valid lineage or not, and
    # takes precedence. Invalid, non-excluded lineage is withheld (fail closed).
    excluded_occurrences, invalid_occurrences = set(), set()
    for oid, (chain, valid) in chains.items():
        if any(link["original_sha256"] in excluded_originals for link in chain):
            excluded_occurrences.add(oid)
        elif not valid:
            invalid_occurrences.add(oid)
    gaps["scope_excluded_occurrences"] = len(excluded_occurrences)
    gaps["ancestry_invalid_occurrences"] = len(invalid_occurrences)

    # Typed agency (and request) context per original. A typed join only counts
    # when its agency exists and any named request belongs to that agency.
    typed_agencies = defaultdict(set)
    typed_requests = defaultdict(set)
    unresolved = defaultdict(int)
    for join in data["joins"]:
        if join["original_sha256"] in excluded_originals:
            gaps["scope_excluded_joins"] += 1
            continue
        agency = join["agency_id"] if join["agency_id"] in agencies else None
        rid = join["request_id"]
        valid = (join["status"] == "typed" and agency is not None and
                 (rid is None or (rid in requests and requests[rid]["agency_id"] == agency)))
        if not valid:
            unresolved[agency] += 1
            continue
        typed_agencies[join["original_sha256"]].add(agency)
        if rid is not None:
            typed_requests[join["original_sha256"]].add(rid)

    def context(chain):
        """(agency or None, conflict?) by intersecting each level's typed context."""
        levels = [typed_agencies[link["original_sha256"]] for link in chain
                  if typed_agencies[link["original_sha256"]]]
        if not levels:
            return None, False
        common = set.intersection(*levels)
        if len(common) == 1:
            return next(iter(common)), False
        return None, True

    rows = {key: {"months": defaultdict(lambda: {"sent": 0, "received": 0}), "times": [],
                  "messages_sent": 0, "messages_received": 0, "undated_messages": 0,
                  "attachment_occurrences": 0, "portal_items": 0, "portal_items_unretrieved": 0,
                  "unknown_domain_messages": 0, "agency_conflict_messages": 0}
            for key in [*sorted(agencies), None]}
    linked = defaultdict(list)

    for message in data["mail_messages"]:
        oid, sha = message["id"], message["eml_sha256"]
        if oid in excluded_occurrences or sha not in originals or sha in excluded_originals:
            gaps["scope_excluded_messages"] += 1
            continue
        # A mail row whose occurrence is absent or whose lineage is invalid is
        # never attributed by its own original: it is withheld and counted.
        if oid not in chains or oid in invalid_occurrences:
            gaps["ancestry_invalid_messages"] += 1
            continue
        chain = chains[oid][0]
        gaps["visible_messages"] += 1
        agency, conflict = context(chain)
        row = rows[agency]
        if agency is None:
            row["agency_conflict_messages" if conflict else "unknown_domain_messages"] += 1
        way = direction(message["folder"])
        row["messages_" + way] += 1
        moment = instant(message["received_at"])
        if moment is None:
            row["undated_messages"] += 1
        else:
            row["times"].append(moment)
            row["months"][moment.strftime("%Y-%m")][way] += 1
        if agency is not None:
            for rid in typed_requests[sha]:
                if requests[rid]["agency_id"] == agency:
                    linked[rid].append(moment)

    attachment_originals = set()
    for oid, occurrence in occurrences.items():
        if (occurrence["kind"] != "attachment" or oid in excluded_occurrences
                or oid in invalid_occurrences):
            continue
        agency, _ = context(chains[oid][0])
        rows[agency]["attachment_occurrences"] += 1
        attachment_originals.add(occurrence["original_sha256"])
    gaps["distinct_attachment_originals"] = len(attachment_originals)

    for item in data["portal_items"]:
        notice = item["notice_occurrence_id"]
        if notice in excluded_occurrences or item["original_sha256"] in excluded_originals:
            continue
        # A recorded notice occurrence must have a valid lineage (fail closed).
        if notice is not None and (notice not in chains or notice in invalid_occurrences):
            gaps["ancestry_invalid_portal_items"] += 1
            continue
        agency = item["agency_id"] if item["agency_id"] in agencies else None
        rows[agency]["portal_items"] += 1
        if not item["original_sha256"]:
            rows[agency]["portal_items_unretrieved"] += 1

    request_rows = defaultdict(list)
    for rid in sorted(requests):
        request = requests[rid]
        times = [moment for moment in linked[rid] if moment is not None]
        request_rows[request["agency_id"] if request["agency_id"] in agencies else None].append({
            "request_id": withheld.ident(rid), "status": withheld.status(request["status"]),
            "submitted_at": withheld.when(request["submitted_at"]),
            "determination_due_at": withheld.when(request["determination_due_at"]),
            "determination_at": withheld.when(request["determination_at"]),
            "production_at": withheld.when(request["production_at"]),
            "linked_messages": len(linked[rid]),
            "last_linked_message_at": iso(max(times)) if times else None})

    output = []
    for key, row in rows.items():
        months = [{"month": month, **row["months"][month]} for month in sorted(row["months"])]
        output.append({
            "agency_id": None if key is None else withheld.ident(key),
            "bucket": "unknown" if key is None else "agency",
            "first_contact_at": iso(min(row["times"])) if row["times"] else None,
            "last_contact_at": iso(max(row["times"])) if row["times"] else None,
            "messages_sent": row["messages_sent"], "messages_received": row["messages_received"],
            "undated_messages": row["undated_messages"], "months": months,
            "request_count": len(request_rows[key]), "requests": request_rows[key],
            "attachment_occurrences": row["attachment_occurrences"],
            "portal_items": row["portal_items"], "portal_items_unretrieved": row["portal_items_unretrieved"],
            "unresolved_joins": unresolved[key],
            "unknown_domain_messages": row["unknown_domain_messages"],
            "agency_conflict_messages": row["agency_conflict_messages"]})

    totals = {name: sum(row[name] for row in output) for name in ROW_COUNTS}
    merged = defaultdict(lambda: {"sent": 0, "received": 0})
    for row in output:
        for month in row["months"]:
            merged[month["month"]]["sent"] += month["sent"]
            merged[month["month"]]["received"] += month["received"]
    # Overall first/last contact compares datetime instants, never the
    # variable-width ISO strings (fractional seconds sort wrongly as text).
    moments = [moment for row in rows.values() for moment in row["times"]]
    totals.update(agency_rows=len(output), months=[{"month": m, **merged[m]} for m in sorted(merged)],
                  first_contact_at=iso(min(moments)) if moments else None,
                  last_contact_at=iso(max(moments)) if moments else None)
    gaps["withheld_values"] = withheld.values
    gaps["unparseable_dates"] = withheld.dates
    return {"schema": SCHEMA, "review_required": True, "semantic_assessment": "NOT_ASSESSED",
            "visibility": "private", "snapshot_sha256": snapshot_sha256(data),
            "agencies": output, "totals": totals, "gaps": gaps,
            "limits": ["Counts, identifiers and dates from one read-only ledger snapshot; no message content.",
                       "Agency attribution uses recorded typed joins only; hinted/blocked/invalid joins are unresolved.",
                       "UNKNOWN messages lack a typed agency join; domain hints are a custodian task, never guessed.",
                       "Direction is folder-derived (sent folder names); headers and addresses are not read.",
                       "Recorded request status and dates are reproduced, not reassessed; no deadlines are computed."]}


def report(path):
    return build(read_snapshot(path))


def _table(headers, rows):
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]
    lines = []
    for cells in (headers, *rows):
        lines.append("  ".join(str(cell).ljust(width) for cell, width in zip(cells, widths)).rstrip())
    return lines


def _label(row):
    return UNKNOWN_LABEL if row["bucket"] == "unknown" else row["agency_id"]


def _cell(value):
    return "-" if value is None else value


def render_table(result):
    """Deterministic plain-text table of the same report; no other content."""
    contacts = [(_label(row), _cell(row["first_contact_at"]), _cell(row["last_contact_at"]),
                 row["messages_sent"], row["messages_received"], row["undated_messages"],
                 row["request_count"], row["attachment_occurrences"], row["portal_items"],
                 row["unresolved_joins"], row["unknown_domain_messages"]) for row in result["agencies"]]
    totals = result["totals"]
    contacts.append(("TOTAL", _cell(totals["first_contact_at"]), _cell(totals["last_contact_at"]),
                     totals["messages_sent"], totals["messages_received"], totals["undated_messages"],
                     totals["request_count"], totals["attachment_occurrences"], totals["portal_items"],
                     totals["unresolved_joins"], totals["unknown_domain_messages"]))
    months = [(_label(row), month["month"], month["sent"], month["received"])
              for row in result["agencies"] for month in row["months"]]
    months += [("TOTAL", month["month"], month["sent"], month["received"]) for month in totals["months"]]
    requests = [(_label(row), item["request_id"], item["status"], _cell(item["submitted_at"]),
                 _cell(item["determination_due_at"]), _cell(item["determination_at"]),
                 _cell(item["production_at"]), item["linked_messages"], _cell(item["last_linked_message_at"]))
                for row in result["agencies"] for item in row["requests"]]
    lines = ["AGENCY TIMELINE REPORT " + result["schema"] + " (REVIEW REQUIRED; counts, ids and dates only)",
             "snapshot_sha256 " + result["snapshot_sha256"], "", "CONTACTS"]
    lines += _table(("AGENCY", "FIRST_CONTACT", "LAST_CONTACT", "SENT", "RECEIVED", "UNDATED", "REQUESTS",
                     "ATTACHMENTS", "PORTAL_ITEMS", "UNRESOLVED_JOINS", "UNKNOWN_DOMAIN"), contacts)
    lines += ["", "MONTHS (UTC)"]
    lines += _table(("AGENCY", "MONTH", "SENT", "RECEIVED"), months)
    lines += ["", "REQUESTS"]
    lines += _table(("AGENCY", "REQUEST", "STATUS", "SUBMITTED", "DETERMINATION_DUE", "DETERMINED",
                     "PRODUCED", "LINKED_MESSAGES", "LAST_LINKED"), requests)
    lines += ["", "GAPS"]
    lines += [name + "=" + str(result["gaps"][name]) for name in sorted(result["gaps"])]
    return "\n".join(lines) + "\n"
