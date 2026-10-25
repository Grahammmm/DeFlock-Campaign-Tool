"""Read-only correspondence proposals over the existing canonical ledger.

No mailbox access, writes, scheduling, legal deadline calculation, or sends.
Output is private: identities and source locators are not a public export.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .ledger.store import canonical, checked_path, ledger
from .ledger_catalog import scope_excluded

SCHEMA = "correspondence-report-v1"
MAX_ROWS = 50000
MAX_REVIEW_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 131072
TABLES = {
    "agencies": "id,name,jurisdiction",
    "requests": "id,agency_id,external_ref,submitted_at,determination_due_at,determination_at,production_at,status",
    "originals": "sha256,bytes,mime_detected,preservation_status,role,scope",
    "occurrences": "id,original_sha256,kind,source_ref,parent_occurrence_id,acquired_at,evidence",
    "mail_messages": "id,account,folder,uidvalidity,uid,message_id,eml_sha256,headers,received_at,account_provenance_status",
    "joins": "id,original_sha256,agency_id,request_id,join_type,evidence,status,blocked_reason",
    "portal_items": "id,request_id,agency_id,notice_occurrence_id,retrieval_status,original_sha256",
}
MESSAGE_ID = re.compile(r"<[^<>\s]{1,2048}>")


class CorrespondenceError(ValueError):
    """Stable errors deliberately omit source content and local paths."""


def require(condition, code):
    if not condition:
        raise CorrespondenceError(code)


def timestamp(value):
    require(isinstance(value, str) and len(value) <= 64, "timestamp_requires_timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(result.tzinfo is not None, "timestamp_requires_timezone")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise CorrespondenceError("timestamp_requires_timezone") from None


def snapshot(path):
    """One SQLite read transaction; never initialize/migrate an unknown database."""
    try:
        with ledger(path, readonly=True) as con:
            con.execute("BEGIN")
            data = {}
            for table, columns in TABLES.items():
                rows = con.execute(f"SELECT {columns} FROM {table} ORDER BY 1 LIMIT ?", (MAX_ROWS + 1,)).fetchall()
                require(len(rows) <= MAX_ROWS, "snapshot_row_limit")
                data[table] = [dict(row) for row in rows]
            data["ledger_meta"] = [dict(row) for row in con.execute("SELECT key,value FROM ledger_meta ORDER BY key")]
            con.rollback()
        return data
    except CorrespondenceError:
        raise
    except (ValueError, OSError, sqlite3.Error):
        raise CorrespondenceError("canonical_ledger_unavailable_or_unsupported") from None


def _digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _text(value, code, maximum=1000):
    require(isinstance(value, str) and 0 < len(value.strip()) <= maximum, code)
    return value


def _reject_duplicate_members(pairs):
    """JSON object hook: a repeated member is ambiguous, so neither declaration is chosen."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_member")
        result[key] = value
    return result


def _header_parent(message):
    """Only root EML header evidence; subject similarity is never a join."""
    raw = message["headers"]
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_HEADER_BYTES:
        return None, "header_projection_invalid"
    try:
        obj = json.loads(raw, object_pairs_hook=_reject_duplicate_members)
        if not isinstance(obj, dict) or obj.get("schema") != "verified-eml-headers-v1":
            return None, "thread_headers_unsupported"
        if obj.get("source_original_sha256") != message["eml_sha256"] or obj.get("parser_defects"):
            return None, "header_projection_invalid"
        fields = obj["fields"]
        if not isinstance(fields, list) or any(not isinstance(pair, list) or len(pair) != 2 or
                                               any(not isinstance(x, str) for x in pair) for pair in fields):
            return None, "header_projection_invalid"
        indexed = defaultdict(list)
        for key, value in fields:
            indexed[key.lower()].append(value)
        if message["message_id"] is None:
            return None, "message_id_missing_or_ambiguous"
        if not isinstance(message["message_id"], str) or not MESSAGE_ID.fullmatch(message["message_id"]):
            return None, "message_id_invalid"
        if message["message_id"] is not None and [v.strip() for v in indexed["message-id"]] != [message["message_id"]]:
            return None, "header_message_id_mismatch"
        for key in ("in-reply-to", "references"):
            values = indexed[key]
            if len(values) > 1:
                return None, "ambiguous_reply_headers"
            if values:
                ids = MESSAGE_ID.findall(values[0])
                # Fail closed for comments/garbage instead of inventing an RFC parser.
                if not ids or MESSAGE_ID.sub("", values[0]).strip():
                    return None, "unsupported_reply_header_syntax"
                if key == "in-reply-to" and len(ids) != 1:
                    return None, "ambiguous_reply_headers"
                return ids[-1], None
        return None, None
    except (ValueError, KeyError, TypeError):
        return None, "header_projection_invalid"


def _threads(messages, gaps):
    by_id = defaultdict(list)
    parsed = {}
    for message in messages:
        parent, reason = _header_parent(message)
        parsed[message["id"]] = (parent, reason)
        if reason:
            gaps.append({"code": reason, "mail_id": message["id"]})
        if not reason and isinstance(message["message_id"], str) and MESSAGE_ID.fullmatch(message["message_id"]):
            by_id[(message["account"], message["message_id"])].append(message)
    candidates = []
    for message in messages:
        parent, _ = parsed[message["id"]]
        if parent is None:
            continue
        matches = by_id[(message["account"], parent)]
        # The same bytes in multiple folders remain multiple delivery occurrences.
        shas = {m["eml_sha256"] for m in matches}
        if len(shas) != 1:
            gaps.append({"code": "thread_parent_missing" if not matches else "message_id_collision",
                         "mail_id": message["id"]})
            continue
        if message["eml_sha256"] in shas:
            gaps.append({"code": "self_reply_reference", "mail_id": message["id"]})
            continue
        candidates.append({"mail_id": message["id"], "parent_mail_ids": sorted(m["id"] for m in matches),
                           "basis": "declared_reply_header", "status": "unverified_candidate"})
    return candidates


def _request_membership(data, gaps):
    requests = {r["id"]: r for r in data["requests"]}
    agencies = {r["id"] for r in data["agencies"]}
    originals = {r["sha256"]: r for r in data["originals"]}
    excluded = {sha for sha, row in originals.items() if scope_excluded(row)}
    direct = defaultdict(set)
    join_ids = defaultdict(list)
    for join in data["joins"]:
        rid = join["request_id"]
        if rid is None:
            continue
        if join["status"] != "typed":
            gaps.append({"code": "request_join_unverified", "join_id": join["id"]})
            continue
        try:
            evidence = json.loads(join["evidence"])
        except (ValueError, TypeError):
            evidence = None
        if (rid not in requests or join["agency_id"] not in agencies or
                join["agency_id"] != requests[rid]["agency_id"] or
                join["original_sha256"] not in originals or not isinstance(evidence, dict) or
                not isinstance(evidence.get("source_sha256"), str) or evidence["source_sha256"] not in originals or
                not isinstance(evidence.get("locator"), str) or not 0 < len(evidence["locator"].strip()) <= 500):
            gaps.append({"code": "request_join_invalid", "join_id": join["id"]})
            continue
        if join["original_sha256"] in excluded or evidence["source_sha256"] in excluded:
            gaps.append({"code": "request_join_scope_excluded"})
            continue
        direct[join["original_sha256"]].add(rid)
        join_ids[rid].append(join["id"])
    occurrences = {r["id"]: r for r in data["occurrences"]}
    members = {}
    excluded_occurrences = set()
    for oid, occurrence in occurrences.items():
        chain, seen, current = [], set(), occurrence
        while current:
            if current["id"] in seen or len(seen) >= 100:
                gaps.append({"code": "occurrence_ancestry_invalid", "occurrence_id": oid})
                chain = []
                break
            seen.add(current["id"])
            chain.append(current)
            parent = current["parent_occurrence_id"]
            if parent and parent not in occurrences:
                gaps.append({"code": "occurrence_parent_missing", "occurrence_id": oid})
                chain = []
                break
            current = occurrences.get(parent)
        if occurrence["original_sha256"] in excluded or any(r["original_sha256"] in excluded for r in chain):
            excluded_occurrences.add(oid)
            members[oid] = set()
            continue
        contexts = [direct[r["original_sha256"]] for r in chain if direct[r["original_sha256"]]]
        inherited = set.intersection(*contexts) if contexts else set()
        if contexts and len(inherited) != 1:
            gaps.append({"code": "request_binding_conflict" if not inherited else "request_binding_ambiguous",
                         "occurrence_id": oid, "request_ids": sorted(set.union(*contexts))})
            inherited = set()
        # Original-level joins cannot override a conflicting delivery context.
        # Ambiguous associations stay visible as gaps and cannot support a packet.
        members[oid] = inherited
    return members, join_ids, occurrences, originals, excluded_occurrences


def _evidence(value, rid, members, occurrences):
    require(isinstance(value, list) and 0 < len(value) <= 100, "evidence_required")
    result = []
    for item in value:
        require(isinstance(item, dict) and set(item) == {"occurrence_id", "locator"}, "evidence_invalid")
        oid = _text(item["occurrence_id"], "evidence_invalid")
        _text(item["locator"], "evidence_locator_required", 500)
        require(oid in occurrences and rid in members[oid], "evidence_request_binding_missing")
        result.append({**item, "original_sha256": occurrences[oid]["original_sha256"]})
    return result


def _review(value, snapshot_hash, requests, members, occurrences):
    if value is None:
        return {}
    require(isinstance(value, dict) and set(value) == {"schema", "snapshot_sha256", "requests"}, "review_packet_invalid")
    require(value["schema"] == "correspondence-review-v1", "review_schema_unsupported")
    require(value["snapshot_sha256"] == snapshot_hash, "review_snapshot_stale")
    require(isinstance(value["requests"], list) and len(value["requests"]) <= 1000, "review_packet_invalid")
    output = {}
    for entry in value["requests"]:
        require(isinstance(entry, dict) and set(entry) == {"request_id", "reviewed_by", "outstanding_items", "follow_up"}, "review_request_invalid")
        rid = _text(entry["request_id"], "review_request_invalid")
        require(rid in requests and rid not in output, "review_request_unknown_or_duplicate")
        _text(entry["reviewed_by"], "review_author_required", 200)
        items = entry["outstanding_items"]
        require(isinstance(items, list) and len(items) <= 1000, "review_items_invalid")
        seen, reviewed_items = set(), []
        for item in items:
            require(isinstance(item, dict) and set(item) == {"item_id", "description", "disposition", "evidence"}, "review_item_invalid")
            identity = _text(item["item_id"], "review_item_invalid", 200)
            require(identity not in seen, "review_item_duplicate")
            seen.add(identity)
            _text(item["description"], "review_item_invalid", 2000)
            require(item["disposition"] in ("outstanding", "received", "withheld", "withdrawn", "unknown"), "review_disposition_invalid")
            reviewed_items.append({**item, "evidence": _evidence(item["evidence"], rid, members, occurrences)})
        follow = entry["follow_up"]
        if follow is not None:
            require(isinstance(follow, dict) and set(follow) == {"at", "reason", "evidence"}, "review_follow_up_invalid")
            timestamp(follow["at"])
            _text(follow["reason"], "review_follow_up_invalid", 2000)
            follow = {**follow, "evidence": _evidence(follow["evidence"], rid, members, occurrences)}
        output[rid] = {"reviewed_by": entry["reviewed_by"], "items": reviewed_items, "follow_up": follow,
                       "authority": "reviewer_declaration_only", "status": "proposal"}
    return output


def report(path, *, as_of, review=None):
    moment = timestamp(as_of)
    data = snapshot(path)
    snapshot_hash = _digest(data)
    gaps = []
    members, join_ids, occurrences, originals, excluded_occurrences = _request_membership(data, gaps)
    requests = {r["id"]: r for r in data["requests"]}
    reviews = _review(review, snapshot_hash, requests, members, occurrences)
    reminders, output = [], []
    for rid, request in requests.items():
        linked = [row for oid, row in occurrences.items() if rid in members[oid]]
        proposal = reviews.get(rid)
        due = request["determination_due_at"]
        if due and not request["determination_at"]:
            try:
                if timestamp(due) <= moment:
                    reminders.append({"request_id": rid, "kind": "recorded_determination_due", "at": due,
                                      "basis": {"table": "requests", "id": rid, "field": "determination_due_at"}})
            except CorrespondenceError:
                gaps.append({"code": "determination_due_timezone_or_format_unresolved", "request_id": rid})
        if proposal and proposal["follow_up"] and timestamp(proposal["follow_up"]["at"]) <= moment:
            reminders.append({"request_id": rid, "kind": "reviewed_follow_up", **proposal["follow_up"]})
        if proposal is None:
            gaps.append({"code": "outstanding_items_and_follow_up_unreviewed", "request_id": rid})
        output.append({**request, "source": {"table": "requests", "id": rid}, "typed_join_ids": sorted(join_ids[rid]),
                       "source_binding": "recorded_typed_join_not_reverified",
                       "occurrences": [{k: v for k, v in row.items() if k != "evidence"} for row in linked],
                       "delivery_occurrences": len(linked),
                       "unique_originals": len({r["original_sha256"] for r in linked}),
                       "outstanding_items": None if proposal is None else proposal["items"],
                       "follow_up": None if proposal is None else proposal["follow_up"],
                       "review": None if proposal is None else {k: proposal[k] for k in ("reviewed_by", "authority", "status")},
                       "fulfillment": "not_assessed"})
    for reminder in reminders:
        # A stable semantic key is a future scheduler handoff, not a scheduled job.
        reminder["dedupe_key"] = _digest(["correspondence-reminder-v1", reminder["request_id"], reminder["kind"], timestamp(reminder["at"]).isoformat()])
        reminder.update(status="proposal_only", recorded_request_status=requests[reminder["request_id"]]["status"],
                        requires_current_owner_review=True)
    groups = defaultdict(list)
    for occurrence in data["occurrences"]:
        if occurrence["id"] in excluded_occurrences:
            continue
        groups[occurrence["original_sha256"]].append(occurrence["id"])
    unresolved_portals = [{"portal_item_id": row["id"], "external_request_ref": row["request_id"],
                           "retrieval_status": row["retrieval_status"], "notice_occurrence_id": row["notice_occurrence_id"]}
                          for row in data["portal_items"] if not row["original_sha256"] and row["notice_occurrence_id"] not in excluded_occurrences]
    for row in unresolved_portals:
        gaps.append({"code": "portal_bytes_not_received", "portal_item_id": row["portal_item_id"]})
    messages = [row for row in data["mail_messages"] if row["id"] not in excluded_occurrences and
                row["eml_sha256"] in originals and not scope_excluded(originals[row["eml_sha256"]])]
    threads = _threads(messages, gaps)
    return {"schema": SCHEMA, "as_of": moment.isoformat(), "snapshot_sha256": snapshot_hash,
            "review_sha256": None if review is None else _digest(review), "visibility": "private",
            "ledger_meta": data["ledger_meta"], "agencies": data["agencies"], "requests": output,
            "mail": [{k: v for k, v in row.items() if k not in ("headers", "message_id")} for row in messages],
            "thread_candidates": threads,
            "duplicate_originals": [{"original_sha256": sha, "occurrence_ids": sorted(ids)} for sha, ids in sorted(groups.items()) if len(ids) > 1],
            "unassigned_occurrence_ids": sorted(oid for oid in occurrences if not members[oid] and oid not in excluded_occurrences),
            "unretrieved_portal_items": unresolved_portals, "reminder_proposals": reminders, "gaps": gaps,
            "counts": {"requests": len(requests), "mail_deliveries": len(data["mail_messages"]),
                       "occurrences": len(occurrences), "unique_originals": len(originals),
                       "scope_excluded_occurrences": len(excluded_occurrences), "visible_mail_deliveries": len(messages)},
            "limits": ["Current ledger snapshot; as_of selects reminder candidates, not historical state.",
                       "Typed source joins and reply headers do not establish request fulfillment or message authenticity.",
                       "Originals, attachments, portal bytes and reviewer assertions have not been reverified by this report.",
                       "No legal deadlines calculated, messages sent, state changed, or reminders scheduled."]}


def load_review(path):
    try:
        path = checked_path(path)
        with path.open("rb") as stream:
            data = stream.read(MAX_REVIEW_BYTES + 1)
        require(len(data) <= MAX_REVIEW_BYTES, "review_packet_too_large")
        return json.loads(data)
    except CorrespondenceError:
        raise
    except (ValueError, OSError):
        raise CorrespondenceError("review_packet_unreadable") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--as-of", required=True, help="Timezone-aware ISO timestamp; affects proposals only")
    parser.add_argument("--review", help="Private transient review packet bound to a current snapshot")
    args = parser.parse_args(argv)
    try:
        result = report(args.ledger, as_of=args.as_of, review=load_review(args.review) if args.review else None)
    except CorrespondenceError as error:
        print(canonical({"schema": SCHEMA, "error": str(error)}))
        return 2
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
