"""Offline, counts-only portal pointer reconciliation (design item M-09).

Compares a mail-derived set of portal pointers with an export of the portal
queue and reports how many pointers fall into each class. The report contains
counts only. It never contains a URL, path, host, request id, item id,
filename, subject or address, and it never contains free text from the inputs.

Inputs, both JSON and both synthetic or operator supplied:

  observations  {"schema": "portal-pointer-observations/v1",
                 "observations": [{"host_class": ..., "url_sha256": ...,
                   "host"?: ..., "request_id"?: ..., "item_id"?: ...,
                   "path"?: ..., "allowlisted"?: bool}, ...]}
  queue export  {"schema": "portal-queue-export/v1",
                 "items": [{"host": ..., "request_id": ..., "item_id": ...,
                   "state": ..., "has_version"?: bool, "reason"?: ...}, ...]}

The queue identity is (host, request_id, item_id), the same as the engine's
portal_items table. When an observation has no item_id, "path" (path only, no
query string or fragment) is parsed with the engine's own DOCUMENT_LINK
pattern, so the "/documents/<item>" shape cannot diverge from the engine.

Each unique URL (unique url_sha256) gets exactly one class:

  held_policy               host class is not on the root-owned allowlist
                            (govqa, microsoft_delivery, other_delivery) or the
                            observation says allowlisted=false
  held_redirect             tracked redirect; unresolvable offline
  missing_request_identity  allowlisted pointer without a request id (caller
                            gap for the engine's inventory_notice)
  fetched                   matching queue item has a version receipt
  queued                    matching queue item in pending/fetching/retry,
                            no version receipt
  failed                    matching queue item blocked, no version receipt
                            (reason codes counted)
  unproven                  no item id, no matching queue item, or an
                            ambiguous (duplicated) queue identity

Held classes are decided before any queue lookup, so a held pointer is never
reported as fetched even when a queue row with the same identity exists.
Malformed rows are counted by reason and never silently dropped. Duplicate
observations of the same URL are counted once; conflicting duplicates are
counted as conflicting URLs. Output is canonical JSON (sorted keys, no
timestamps), so identical inputs give byte-identical output.

This tool reads two files and writes one new file or stdout. It opens no
network connection, database, mailbox, ledger, allowlist or queue store.
Exit 0 means the report was produced; it is not acceptance of the counts.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool.records.portal.notice import DOCUMENT_LINK  # noqa: E402
from campaign_tool.records.portal.policy import PortalError, hostname  # noqa: E402
from campaign_tool.records.portal.store import identifier  # noqa: E402

OBSERVATION_SCHEMA = "portal-pointer-observations/v1"
QUEUE_SCHEMA = "portal-queue-export/v1"
REPORT_SCHEMA = "portal-pointer-reconciliation/v1"

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_ROWS = 1_000_000

ALLOWLISTED_CLASSES = {"nextrequest"}
POLICY_HELD_CLASSES = {"govqa", "microsoft_delivery", "other_delivery"}
REDIRECT_CLASSES = {"tracked_redirect"}
HOST_CLASSES = ALLOWLISTED_CLASSES | POLICY_HELD_CLASSES | REDIRECT_CLASSES

# Engine portal_items states (campaign_tool/records/portal/engine.py, store.py).
QUEUED_STATES = {"pending", "fetching", "retry"}
FAILED_STATES = {"blocked"}
RECEIVED_STATES = {"received"}
QUEUE_STATES = QUEUED_STATES | FAILED_STATES | RECEIVED_STATES

CLASSES = ("fetched", "queued", "failed", "unproven", "held_policy",
           "held_redirect", "missing_request_identity")
UNPROVEN_REASONS = ("no_item_id", "not_in_queue", "queue_identity_ambiguous")

OBSERVATION_KEYS = {"host_class", "url_sha256", "host", "request_id", "item_id",
                    "path", "allowlisted"}
QUEUE_KEYS = {"host", "request_id", "item_id", "state", "has_version", "reason"}

SHA256 = re.compile(r"[0-9a-f]{64}")
REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")


class InputError(Exception):
    """Fatal input problem; the message is a fixed code, never input text."""


def read_json(path, schema, list_key):
    """Read one bounded regular JSON file; refuse symlinks and FIFOs."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise InputError("input_unreadable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InputError("input_not_regular_file")
        if info.st_size > MAX_INPUT_BYTES:
            raise InputError("input_too_large")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError("input_too_large")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise InputError("input_not_json") from None
    if not isinstance(document, dict) or document.get("schema") != schema:
        raise InputError("input_schema_mismatch")
    rows = document.get(list_key)
    if not isinstance(rows, list):
        raise InputError("input_rows_missing")
    if len(rows) > MAX_ROWS:
        raise InputError("input_too_many_rows")
    return rows


def _optional_id(row, key):
    value = row.get(key)
    if value is None:
        return None
    identifier(value)
    return value


def normalize_observation(row):
    """Return a normalized tuple or raise ValueError(<fixed reason code>)."""
    if not isinstance(row, dict):
        raise ValueError("not_object")
    if set(row) - OBSERVATION_KEYS:
        raise ValueError("unknown_field")
    host_class = row.get("host_class")
    if host_class not in HOST_CLASSES:
        raise ValueError("unknown_host_class")
    url_sha256 = row.get("url_sha256")
    if not isinstance(url_sha256, str) or not SHA256.fullmatch(url_sha256):
        raise ValueError("invalid_url_sha256")
    allowlisted = row.get("allowlisted", True)
    if type(allowlisted) is not bool:
        raise ValueError("invalid_allowlisted")
    host = row.get("host")
    if host is not None:
        try:
            hostname(host)
        except PortalError:
            raise ValueError("invalid_host") from None
    try:
        request_id = _optional_id(row, "request_id")
        item_id = _optional_id(row, "item_id")
    except PortalError:
        raise ValueError("invalid_identity") from None
    path = row.get("path")
    if path is not None:
        if (not isinstance(path, str) or not path.startswith("/") or len(path) > 2048
                or any(ch in path for ch in "?#") or any(ord(ch) < 33 or ord(ch) == 127 for ch in path)):
            raise ValueError("invalid_path")
        match = DOCUMENT_LINK.fullmatch(path)
        if match:
            if item_id is not None and item_id != match.group(1):
                raise ValueError("item_id_path_mismatch")
            item_id = match.group(1)
    if host_class in ALLOWLISTED_CLASSES and allowlisted and host is None:
        raise ValueError("missing_host")
    return (url_sha256, host_class, host, request_id, item_id, allowlisted)


def normalize_queue_row(row):
    if not isinstance(row, dict):
        raise ValueError("not_object")
    if set(row) - QUEUE_KEYS:
        raise ValueError("unknown_field")
    try:
        hostname(row.get("host"))
        identifier(row.get("request_id"))
        identifier(row.get("item_id"))
    except PortalError:
        raise ValueError("invalid_identity") from None
    state = row.get("state")
    if state not in QUEUE_STATES:
        raise ValueError("unknown_state")
    has_version = row.get("has_version")
    if has_version is not None and type(has_version) is not bool:
        raise ValueError("invalid_has_version")
    if has_version is None:
        # The engine sets state 'received' only in the same transaction that
        # commits a verified version receipt and current_sha256.
        has_version = state in RECEIVED_STATES
    elif has_version is False and state in RECEIVED_STATES:
        raise ValueError("received_without_version")
    reason = row.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("invalid_reason")
    if reason is not None and not REASON.fullmatch(reason):
        reason = "unrecognized_reason"
    return (row["host"], row["request_id"], row["item_id"]), state, has_version, reason


def _count(mapping, key, amount=1):
    mapping[key] = mapping.get(key, 0) + amount


def build_queue(rows):
    malformed = {}
    seen = {}
    for row in rows:
        try:
            identity, state, has_version, reason = normalize_queue_row(row)
        except ValueError as exc:
            _count(malformed, str(exc))
            continue
        seen.setdefault(identity, []).append((state, has_version, reason))
    items = {}
    ambiguous = set()
    for identity, entries in seen.items():
        if len(entries) > 1:
            _count(malformed, "duplicate_identity", len(entries) - 1)
            ambiguous.add(identity)
            continue
        items[identity] = entries[0]
    return items, ambiguous, malformed


def classify(observation, items, ambiguous):
    """Return (class, detail) for one normalized observation."""
    _, host_class, host, request_id, item_id, allowlisted = observation
    # Held classes first: a held pointer is never matched against the queue.
    if host_class in REDIRECT_CLASSES:
        return "held_redirect", host_class
    if host_class in POLICY_HELD_CLASSES or not allowlisted:
        return "held_policy", host_class
    if request_id is None:
        return "missing_request_identity", None
    if item_id is None:
        return "unproven", "no_item_id"
    identity = (host, request_id, item_id)
    if identity in ambiguous:
        return "unproven", "queue_identity_ambiguous"
    entry = items.get(identity)
    if entry is None:
        return "unproven", "not_in_queue"
    state, has_version, reason = entry
    if has_version:
        return "fetched", identity
    if state in FAILED_STATES:
        return "failed", reason or "no_reason"
    return "queued", state


def reconcile(observation_rows, queue_rows):
    items, ambiguous, queue_malformed = build_queue(queue_rows)

    malformed = {}
    duplicates = 0
    by_url = {}
    conflicting = set()
    for row in observation_rows:
        try:
            observation = normalize_observation(row)
        except ValueError as exc:
            _count(malformed, str(exc))
            continue
        previous = by_url.get(observation[0])
        if previous is None:
            by_url[observation[0]] = observation
        elif previous == observation:
            duplicates += 1
        else:
            duplicates += 1
            conflicting.add(observation[0])

    classes = {name: 0 for name in CLASSES}
    by_host_class = {name: {cls: 0 for cls in CLASSES} for name in sorted(HOST_CLASSES)}
    unproven = {name: 0 for name in UNPROVEN_REASONS}
    failed_reasons = {}
    held_policy = {name: 0 for name in sorted(POLICY_HELD_CLASSES | ALLOWLISTED_CLASSES)}
    queued_states = {name: 0 for name in sorted(QUEUED_STATES)}
    fetched_items = set()
    referenced = set()
    for url_sha256 in sorted(by_url):
        if url_sha256 in conflicting:
            continue
        observation = by_url[url_sha256]
        cls, detail = classify(observation, items, ambiguous)
        classes[cls] += 1
        by_host_class[observation[1]][cls] += 1
        if cls == "unproven":
            unproven[detail] += 1
        elif cls == "failed":
            _count(failed_reasons, detail)
        elif cls == "held_policy":
            held_policy[detail] += 1
        elif cls == "queued":
            queued_states[detail] += 1
        elif cls == "fetched":
            fetched_items.add(detail)
        if cls in ("fetched", "queued", "failed"):
            referenced.add((observation[2], observation[3], observation[4]))

    queue_states = {name: 0 for name in sorted(QUEUE_STATES)}
    queue_failed = {}
    queue_fetched = 0
    for state, has_version, reason in items.values():
        queue_states[state] += 1
        if has_version:
            queue_fetched += 1
        elif state in FAILED_STATES:
            _count(queue_failed, reason or "no_reason")

    unique_urls = len(by_url)
    classified = sum(classes.values())
    return {
        "schema": REPORT_SCHEMA,
        "counts_only": True,
        "review_required": True,
        "mail": {
            "observation_rows": len(observation_rows),
            "malformed_rows": sum(malformed.values()),
            "malformed_reasons": dict(sorted(malformed.items())),
            "duplicate_observations": duplicates,
            "unique_urls": unique_urls,
            "conflicting_urls": len(conflicting),
            "classified_urls": classified,
        },
        "classes": classes,
        "by_host_class": by_host_class,
        "detail": {
            "unproven": unproven,
            "failed_reasons": dict(sorted(failed_reasons.items())),
            "held_policy_by_host_class": held_policy,
            "queued_by_state": queued_states,
            "fetched_queue_items": len(fetched_items),
        },
        "queue": {
            "rows": len(queue_rows),
            "malformed_rows": sum(queue_malformed.values()),
            "malformed_reasons": dict(sorted(queue_malformed.items())),
            "items": len(items),
            "ambiguous_identities": len(ambiguous),
            "by_state": queue_states,
            "with_version_receipt": queue_fetched,
            "failed_reasons": dict(sorted(queue_failed.items())),
            "referenced_by_mail": len(referenced),
            "not_referenced_by_mail": len(set(items) - referenced),
        },
    }


def render(report):
    return json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def write_new(path, text):
    """Create a new 0600 file; never overwrite or follow a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Counts-only portal pointer reconciliation (offline).")
    parser.add_argument("--observations", required=True, help="mail-derived pointer observations JSON")
    parser.add_argument("--queue-export", required=True, help="portal queue export JSON")
    parser.add_argument("--output", help="new report file (refuses to overwrite); default stdout")
    args = parser.parse_args(argv)
    try:
        observations = read_json(args.observations, OBSERVATION_SCHEMA, "observations")
        queue = read_json(args.queue_export, QUEUE_SCHEMA, "items")
    except InputError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    text = render(reconcile(observations, queue))
    if args.output:
        try:
            write_new(args.output, text)
        except OSError:
            print(json.dumps({"error": "output_not_created"}), file=sys.stderr)
            return 2
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
