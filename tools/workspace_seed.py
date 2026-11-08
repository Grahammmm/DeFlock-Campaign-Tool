#!/usr/bin/env python3
"""ADM-SEED-1: build a minimal, reviewable D1 seed for the workspace Worker from the slo-pilot tables.

Run it locally on the records box; it reads three kinds of file whose PATHS ARE ARGUMENTS (no private path lives in this repo):
  --agencies  slo-pilot agencies.tsv           only the `agency` column is read (an `email`-like column is never read)
  --events    events.tsv files or directories  a directory means DIR/*/events.tsv
  --tracker   MASTER-REQUEST-TRACKER.csv       only `request_id` and `submitted` are read (repeatable, optional)

Export policy (minimal; the point is that no source free text can reach the output):
  * agency: name and jurisdiction_name (both the agency name from agencies.tsv); records_email is ALWAYS NULL (no address of any kind).
  * request: our own requests only. A request_ref ending "(third party)" or equal to "context" is dropped. request_ref must match
    ^[A-Za-z0-9 #./_-]{1,40}$ (the runbook set plus "_", which real refs such as PL_PRA use) or the whole seed is refused.
    subject is "<agency name> records request <request_ref>", body_md is a fixed sentence, next_action comes from a fixed map on the
    latest event_type.
  * correspondence (timeline): summary, direction and classification come from a fixed map on event_type. An unknown event_type
    refuses the seed. received_at is the event date. subject, from_addr, to_addr, provider_message_id and raw_sha256 are NULL.
    what_happened, who, source, time_local and clock_note are never read into a variable that reaches output.
  * No rows in original, receipt_occurrence, extraction, digest, finding, review_receipt, publication, meeting, subscriber_event, job.
    campaign.external_sends = 'disabled'; a setting row `seed_snapshot` records the snapshot date.
As a BACKSTOP (not clearance) every exported text value goes through the flock_privacy email, phone and plate patterns. A hit refuses
the whole seed and names the table, row key and column, never the text.

Outputs in --out-dir, all mode 0600: seed.sql, seed-summary.txt, seed-review.txt (first line is the sha256 of seed.sql, then EVERY
exported text value, for the owner's SEED-OK). Nothing is written when the seed is refused. No network access. Example:
  python3 tools/workspace_seed.py --agencies A/agencies.tsv --events A/agency --tracker R/MASTER-REQUEST-TRACKER.csv \\
      --campaign-id "$CAMPAIGN_ID" --snapshot-date 2026-11-08 --out-dir ~/seed-out
Two agency folders that hold parallel timelines for one agency (for example arroyo-grande and arroyo-grande-pd) both merge into that
agency's requests when both are passed; pass only the folder you mean.
"""
import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

_spec = importlib.util.spec_from_file_location("_seed_flock_privacy", Path(__file__).resolve().with_name("flock_privacy.py"))
_privacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_privacy)

REF_RE = re.compile(r"^[A-Za-z0-9 #./_-]{1,40}$")
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,8}-[0-9]{1,6}$")
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^[^\x00-\x1f<>]{1,120}$")
BODY = "Not exported: held on the records box."
SCOPE_ID = "scope-minimal"

# event_type -> (summary, direction, classification, request state it implies or None, next_action). The only text that reaches
# the timeline and the request list. Computed or internal rows are "outbound" (our side's record) and "unclassified".
EVENTS = {
    "request_sent": ("Request sent to the agency.", "outbound", "unclassified", "sent", "Wait for the agency's acknowledgement or determination."),
    "request_received": ("Agency confirmed it received the request.", "inbound", "acknowledgement", "acknowledged", "Wait for the agency's determination."),
    "clock_start": ("Response clock started (computed).", "outbound", "unclassified", None, "Check the computed due date."),
    "clock_due": ("Response due date computed.", "outbound", "unclassified", None, "Check whether the agency has responded by the due date."),
    "acknowledged": ("Agency acknowledged the request.", "inbound", "acknowledgement", "acknowledged", "Wait for the agency's determination."),
    "extension_notice": ("Agency gave notice of an extension.", "inbound", "extension", "extended", "Wait for the extended response date."),
    "promised_date": ("Agency gave a date for its response.", "inbound", "unclassified", None, "Check the promised date."),
    "follow_up_sent": ("Follow-up sent to the agency.", "outbound", "unclassified", None, "Wait for the agency's reply to the follow-up."),
    "clarification_sent": ("Clarification sent to the agency.", "outbound", "clarification", None, "Wait for the agency's reply to the clarification."),
    "determination": ("Agency sent a determination.", "inbound", "unclassified", None, "Read the determination on the records box."),
    "determination_production": ("Agency sent a determination and records.", "inbound", "production", "partial", "Review the records the agency produced."),
    "production": ("Agency produced records.", "inbound", "production", "partial", "Review the records the agency produced."),
    "production_link_fetched": ("Records link retrieved.", "outbound", "unclassified", None, "Review the retrieved records on the records box."),
    "denial": ("Agency denied the request.", "inbound", "denial", "denied", "Decide whether to appeal."),
    "no_records": ("Agency said it holds no responsive records.", "inbound", "unclassified", "closed", "Decide whether to ask a narrower question."),
    "fee_estimate": ("Agency sent a fee estimate.", "inbound", "fee_estimate", None, "Decide on the fee estimate."),
    "call": ("Phone call logged.", "outbound", "unclassified", None, "Read the call note on the records box."),
    "portal_check": ("Agency portal checked.", "outbound", "unclassified", None, "Check the portal again at the next follow-up."),
    "overdue_mark": ("Marked overdue on our records; not a legal finding.", "outbound", "unclassified", None, "Send a follow-up."),
    "closed": ("Request closed.", "inbound", "unclassified", "closed", "No action needed."),
    "contract_executed": ("Contract executed.", "outbound", "unclassified", None, "Read the contract note on the records box."),
    "system_record": ("Record added to the project file.", "outbound", "unclassified", None, "Read the record on the records box."),
    "reconsideration_sent": ("Request for reconsideration sent.", "outbound", "unclassified", "appealed", "Wait for the agency's reply."),
}
EVENT_COLUMNS = ("event_id", "date", "agency", "request_ref", "event_type")  # the ONLY events.tsv columns read


class Refused(Exception):
    """The seed is refused; the message names a locator and a category, never a source value."""


def _slug(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def _kind(name):
    low = name.lower()
    if "sheriff" in low:
        return "sheriff"
    if "highway patrol" in low:
        return "chp"
    if re.search(r"\b(pd|police)\b", low):
        return "police"
    return "other"


def _check_date(value, where):
    try:
        if DATE_RE.match(value):
            date.fromisoformat(value)
            return value
    except ValueError:
        pass
    raise Refused("%s: not a YYYY-MM-DD date" % where)


def read_agencies(path):
    agencies = {}  # name -> agency_id
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if "agency" not in (reader.fieldnames or []):
            raise Refused("agencies file: no 'agency' column")
        for number, row in enumerate(reader, start=2):
            name = (row["agency"] or "").strip()
            where = "agencies row %d column agency" % number
            if not NAME_RE.match(name):
                raise Refused("%s: not an acceptable agency name" % where)
            if _privacy.categories(name):
                raise Refused("privacy backstop: %s matches %s" % (where, "/".join(_privacy.categories(name))))
            agencies.setdefault(name, None)
    ids = {}
    for name in agencies:
        agency_id = _slug(name)
        if not agency_id or agency_id in ids:
            raise Refused("agencies: two agency names give the same agency_id")
        ids[agency_id] = name
        agencies[name] = agency_id
    return agencies


def events_files(args):
    files = []
    for item in args:
        path = Path(item)
        found = sorted(path.glob("*/events.tsv")) if path.is_dir() else [path]
        if not found:
            raise Refused("events: no events.tsv under a given directory")
        files.extend(found)
    return files


def read_events(files, agencies):
    """Return ([event dict], dropped_third_party). Only EVENT_COLUMNS are ever read from a row."""
    events, dropped, seen = [], 0, set()
    for path in files:
        folder = _slug(path.parent.name) or "events"
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle, delimiter="\t")
            header = next(reader, [])
            try:
                index = {name: header.index(name) for name in EVENT_COLUMNS}
            except ValueError:
                raise Refused("events file %s: missing a required column" % folder)
            for number, row in enumerate(reader, start=2):
                if not row:
                    continue
                where = "events %s row %d" % (folder, number)
                try:
                    values = {name: row[index[name]].strip() for name in EVENT_COLUMNS}
                except IndexError:
                    raise Refused("%s: short row" % where)
                ref = values["request_ref"]
                if ref == "context" or ref.endswith("(third party)"):
                    dropped += 1
                    continue
                if values["event_type"] not in EVENTS:
                    raise Refused("%s column event_type: unknown event type" % where)
                if not EVENT_ID_RE.match(values["event_id"]):
                    raise Refused("%s column event_id: unexpected shape" % where)
                if not REF_RE.match(ref):
                    raise Refused("%s column request_ref: unexpected characters or length" % where)
                if values["agency"] not in agencies:
                    raise Refused("%s column agency: not in the agencies file" % where)
                values["date"] = _check_date(values["date"], where + " column date")
                values["folder"] = folder
                values["order"] = len(events)
                key = (folder, values["event_id"])
                if key in seen:
                    raise Refused("%s column event_id: repeated within one events file" % where)
                seen.add(key)
                events.append(values)
    return events, dropped


def read_tracker(paths):
    submitted = {}
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if "request_id" not in (reader.fieldnames or []) or "submitted" not in (reader.fieldnames or []):
                raise Refused("tracker file: needs request_id and submitted columns")
            for row in reader:
                ref, when = (row["request_id"] or "").strip(), (row["submitted"] or "").strip()
                if not REF_RE.match(ref) or not DATE_RE.match(when):
                    continue  # a tracker row without a usable id or ISO date only means no fallback sent date
                try:
                    date.fromisoformat(when)
                except ValueError:
                    continue
                submitted.setdefault(ref, when)
    return submitted


def build_rows(agencies, events, tracker, campaign_id, snapshot):
    """Return [(table, pk_label, {column: value})] in insert order (parents first)."""
    stamp = snapshot + "T00:00:00Z"
    rows = [("campaign", campaign_id, dict(
        campaign_id=campaign_id, name="DeFlock SLO (minimal reviewed snapshot)", jurisdiction="us-ca", county_fips="06079",
        county_name="San Luis Obispo County", place_fips=None, place_name=None, public_hostname=None, workspace_hostname=None,
        privacy_tier="redacted_cloud", law_package_status="unreviewed", external_sends="disabled",
        created_at=stamp, updated_at=stamp))]
    rows.append(("setting", "seed_snapshot", dict(
        campaign_id=campaign_id, key="seed_snapshot", value_json=json.dumps({"date": snapshot, "kind": "minimal"}, sort_keys=True),
        created_at=stamp, updated_at=stamp)))
    for name, agency_id in sorted(agencies.items(), key=lambda item: item[1]):
        rows.append(("agency", agency_id, dict(
            agency_id=agency_id, campaign_id=campaign_id, name=name, kind=_kind(name), jurisdiction_name=name, records_url=None,
            records_email=None, portal_vendor="unknown", portal_url=None, flock_transparency_slug=None, muckrock_agency_id=None,
            selected=1, verified=0, sources_json="[]", created_at=stamp, updated_at=stamp)))
    groups = {}
    for event in events:
        groups.setdefault((agencies[event["agency"]], event["request_ref"]), []).append(event)
    request_ids = {}
    for (agency_id, ref), items in sorted(groups.items()):
        request_id = "req-%s-%s" % (agency_id, _slug(ref))
        if request_id in request_ids and request_ids[request_id] != (agency_id, ref):
            raise Refused("request ids collide for two different request_ref values")
        request_ids[request_id] = (agency_id, ref)
        ordered = sorted(items, key=lambda e: (e["date"], e["order"]))
        latest = ordered[-1]
        state = "sent"
        for event in ordered:
            implied = EVENTS[event["event_type"]][3]
            state = implied or state
        sent = next((e["date"] for e in ordered if e["event_type"] == "request_sent"), tracker.get(ref))
        dues = [e["date"] for e in ordered if e["event_type"] == "clock_due"]
        name = next(n for n, i in agencies.items() if i == agency_id)
        rows.append(("request", request_id, dict(
            request_id=request_id, campaign_id=campaign_id, agency_id=agency_id, scope_id=SCOPE_ID, scope_version=1,
            subject="%s records request %s" % (name, ref), body_md=BODY, channel="portal_manual", fee_cap_cents=0, state=state,
            sent_at=sent, determination_due=dues[-1] if dues else None, extension_claimed_until=None,
            last_activity_at=latest["date"], next_action=EVENTS[latest["event_type"]][4], external_ref=None,
            created_at=stamp, updated_at=stamp)))
    for (agency_id, ref), items in sorted(groups.items()):
        request_id = "req-%s-%s" % (agency_id, _slug(ref))
        for event in sorted(items, key=lambda e: (e["date"], e["folder"], e["event_id"])):
            summary, direction, classification, _state, _next = EVENTS[event["event_type"]]
            correspondence_id = "evt-%s-%s" % (event["folder"], event["event_id"].lower())
            if not ID_RE.match(correspondence_id):
                raise Refused("event %s: cannot form a safe id" % event["event_id"])
            rows.append(("correspondence", correspondence_id, dict(
                correspondence_id=correspondence_id, campaign_id=campaign_id, request_id=request_id, direction=direction,
                channel="seed", provider_message_id=None, from_addr=None, to_addr=None, subject=None,
                received_at=event["date"], raw_sha256=None, classification=classification, classification_confidence=None,
                summary=summary, created_at=stamp, updated_at=stamp)))
    return rows


def backstop(rows):
    """Email/phone/plate screen over every exported text value. Names table, key and column; never the value."""
    for table, key, row in rows:
        for column, value in row.items():
            if isinstance(value, str):
                hits = _privacy.categories(value)
                if hits:
                    raise Refused("privacy backstop: %s row %s column %s matches %s" % (table, key, column, "/".join(hits)))


def _q(value):
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def render_sql(rows):
    lines = ["-- workspace_seed.py minimal seed. Generated; review seed-review.txt before applying."]
    for table, _key, row in rows:
        lines.append("INSERT INTO %s (%s) VALUES (%s);" % (table, ", ".join(row), ", ".join(_q(v) for v in row.values())))
    return "\n".join(lines) + "\n"


def render_review(rows, digest):
    out = ["seed.sql sha256: " + digest, "Every exported text value (table.column [row key] = value). NULL columns are not listed.", ""]
    for table, key, row in rows:
        for column, value in row.items():
            if isinstance(value, str):
                out.append("%s.%s [%s] = %s" % (table, column, key, value))
    return "\n".join(out) + "\n"


def render_summary(rows, digest, dropped, files, snapshot, campaign_id):
    counts = {}
    for table, _key, _row in rows:
        counts[table] = counts.get(table, 0) + 1
    null_cols = {"agency": ("records_email",), "correspondence": ("subject", "from_addr", "to_addr", "provider_message_id", "raw_sha256")}
    lines = ["workspace seed summary", "snapshot date: " + snapshot, "campaign_id: " + campaign_id, "seed.sql sha256: " + digest,
             "events files read: %d" % files, "third-party or context event rows dropped: %d" % dropped, "", "rows per table:"]
    for table in ("campaign", "setting", "agency", "request", "correspondence"):
        lines.append("  %s: %d" % (table, counts.get(table, 0)))
    for table in ("original", "receipt_occurrence", "extraction", "digest", "finding", "review_receipt", "publication", "meeting",
                  "subscriber_event", "job"):
        lines.append("  %s: 0" % table)
    lines += ["", "privacy backstop (email/phone/plate) hits: 0 (a hit refuses the whole seed)"]
    for table, columns in null_cols.items():
        for column in columns:
            bad = sum(1 for t, _k, r in rows if t == table and r[column] is not None)
            lines.append("non-NULL %s.%s: %d" % (table, column, bad))
    lines.append("campaign.external_sends: " + next(r["external_sends"] for t, _k, r in rows if t == "campaign"))
    return "\n".join(lines) + "\n"


def write_private(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(text)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the minimal reviewed D1 seed (seed.sql, seed-summary.txt, seed-review.txt).")
    parser.add_argument("--agencies", required=True)
    parser.add_argument("--events", required=True, nargs="+")
    parser.add_argument("--tracker", action="append", default=[])
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--snapshot-date", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if not ID_RE.match(args.campaign_id):
            raise Refused("campaign-id: unexpected characters")
        snapshot = _check_date(args.snapshot_date, "snapshot-date")
        agencies = read_agencies(args.agencies)
        files = events_files(args.events)
        events, dropped = read_events(files, agencies)
        rows = build_rows(agencies, events, read_tracker(args.tracker), args.campaign_id, snapshot)
        backstop(rows)
    except (Refused, OSError) as error:
        print("REFUSED: %s" % (error if isinstance(error, Refused) else "cannot read an input file"), file=sys.stderr)
        return 2
    sql = render_sql(rows)
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_private(out / "seed.sql", sql)
    write_private(out / "seed-review.txt", render_review(rows, digest))
    write_private(out / "seed-summary.txt", render_summary(rows, digest, dropped, len(files), snapshot, args.campaign_id))
    print("seed.sql sha256 %s" % digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
