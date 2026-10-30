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
                "allowlisted" is the custodian-supplied, offline classification
                of the row's host against the root-owned portal allowlist. The
                producer must set it from that classification; this tool never
                reads the allowlist. Absent means host policy is unknown, which
                is never treated as allowed.
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
                            (reason codes counted from a fixed vocabulary)
  unproven                  host policy unknown (no allowlisted value), no
                            item id, no matching queue item, or an ambiguous
                            (duplicated) queue identity

Held classes are decided before any queue lookup, so a held pointer is never
reported as fetched even when a queue row with the same identity exists.
Malformed rows are counted by reason and never silently dropped. Duplicate
observations of the same URL are counted once; conflicting duplicates are
counted as conflicting URLs. A malformed row whose url_sha256 is itself well
formed taints that URL: if the URL also has a fully valid observation it is a
conflicting URL and is not classified. Malformed-only URLs are not counted as
unique URLs, and malformed rows never supply host policy or identity. Output
is canonical JSON (sorted keys, no timestamps), so identical inputs give
byte-identical output.

This tool reads two files and writes one new file or stdout, with no symlink in any path component.
It opens no
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
UNPROVEN_REASONS = ("host_policy_unknown", "no_item_id", "not_in_queue",
                    "queue_identity_ambiguous")

# Fixed vocabulary of item-level failure codes the engine can store in
# portal_items.reason (PortalError codes on the fetch path in engine.py,
# https_transport.py and policy.py, plus engine fallbacks). Only these appear
# as report keys; any other value, however code-shaped, is folded into
# UNRECOGNIZED_REASON so free text from an export can never reach the output.
KNOWN_REASONS = frozenset({
    "adapter_failure", "ambiguous_http_headers", "approval_expired",
    "archive_invalid", "archive_limits", "archive_unsafe_path",
    "archive_unsupported_member", "authorization_required", "dns_failed",
    "dns_invalid", "dns_timeout", "egress_denied", "empty_document",
    "encoded_body_unsupported", "expired_or_denied_link", "http_failure",
    "https_connect_failed", "invalid_host", "invalid_http_response",
    "invalid_length", "invalid_url", "length_mismatch", "login_page",
    "path_not_approved", "payload_kind_mismatch", "peer_address_changed",
    "portal_not_approved", "redirect_limit", "redirect_not_approved",
    "redirect_without_location", "retry_exhausted", "signature_mismatch",
    "size_limit", "ssrf_address_denied", "timeout", "total_size_limit",
    "transient_http", "transport_chunk_contract", "transport_or_storage_failure",
    "unsupported_collection_media", "unsupported_transfer_encoding",
})
UNRECOGNIZED_REASON = "unrecognized_reason"
NO_REASON = "no_reason"

OBSERVATION_KEYS = {"host_class", "url_sha256", "host", "request_id", "item_id",
                    "path", "allowlisted"}
QUEUE_KEYS = {"host", "request_id", "item_id", "state", "has_version", "reason"}

SHA256 = re.compile(r"[0-9a-f]{64}")


class InputError(Exception):
    """Fatal input problem; the message is a fixed code, never input text."""


def _pairs_recorder(duplicates):
    """Return a json object_pairs_hook that notes any repeated member name.

    The standard decoder keeps the last of repeated names, so a row such as
    {"allowlisted": false, "allowlisted": true} would silently read as
    allowlisted. Repeats (even with identical values) are recorded here and
    refused by read_json once the whole document has decoded.
    """
    def hook(pairs):
        document = dict(pairs)
        if len(document) != len(pairs):
            duplicates.append(True)
        return document
    return hook


def _walk_parent(path):
    """Return a pinned parent fd and leaf, with no symlink traversal."""
    spelling = os.fspath(path)
    parts = spelling.split("/")
    if parts[-1] in ("", ".") or ".." in parts:
        raise OSError("unsafe_path")
    parts = [part for part in parts if part and part != "."]
    if not parts:
        raise OSError("unsafe_path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open("/" if spelling.startswith("/") else ".", flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory, parts[-1]
    except BaseException:
        os.close(directory)
        raise


def read_json(path, schema, list_key):
    """Read one bounded regular JSON file; refuse symlinks and FIFOs.

    Any object anywhere in the file that repeats a member name is refused
    (duplicate_json_key) before the schema or rows are looked at.
    """
    try:
        parent, leaf = _walk_parent(path)
        try:
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=parent)
        finally:
            os.close(parent)
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
    duplicates = []
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_recorder(duplicates))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InputError("input_not_json") from None
    if duplicates:
        raise InputError("duplicate_json_key")
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
    if not isinstance(host_class, str) or host_class not in HOST_CLASSES:
        raise ValueError("unknown_host_class")
    url_sha256 = row.get("url_sha256")
    if not isinstance(url_sha256, str) or not SHA256.fullmatch(url_sha256):
        raise ValueError("invalid_url_sha256")
    # Host policy must be stated explicitly by the custodian-supplied
    # classification; absence is "unknown", never an implicit allowance.
    allowlisted = row.get("allowlisted")
    if allowlisted is not None and type(allowlisted) is not bool:
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
    if host_class in ALLOWLISTED_CLASSES and allowlisted is True and host is None:
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
    if not isinstance(state, str) or state not in QUEUE_STATES:
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
    if reason is not None and reason not in KNOWN_REASONS:
        reason = UNRECOGNIZED_REASON
    return (row["host"], row["request_id"], row["item_id"]), state, has_version, reason


def _normalized(function, row):
    """Apply a row normalizer; any per-row failure becomes a fixed code.

    Explicit checks above raise ValueError(<code>). Anything else a hostile
    JSON value could trigger is folded into one fixed code so a single row can
    never abort the report or surface exception text.
    """
    try:
        return function(row), None
    except ValueError as exc:
        code = str(exc)
        return None, code if code in MALFORMED_CODES else "invalid_value"
    except Exception:  # noqa: BLE001 - per-row containment, fixed code only
        return None, "invalid_value"


MALFORMED_CODES = frozenset({
    "not_object", "unknown_field", "unknown_host_class", "invalid_url_sha256",
    "invalid_allowlisted", "invalid_host", "invalid_identity", "invalid_path",
    "item_id_path_mismatch", "missing_host", "unknown_state",
    "invalid_has_version", "received_without_version", "invalid_reason",
})


def _declared_url_sha256(row):
    """The row's url_sha256 when it is syntactically valid, whatever the other fields hold; else None."""
    if not isinstance(row, dict):
        return None
    value = row.get("url_sha256")
    if isinstance(value, str) and SHA256.fullmatch(value):
        return value
    return None


def _count(mapping, key, amount=1):
    mapping[key] = mapping.get(key, 0) + amount


def _queue_identity(row):
    """Return the row's (host, request_id, item_id) when each field is valid
    on its own, independent of the rest of the row; otherwise None.

    A missing or invalid identity field is never inferred or repaired.
    """
    try:
        if not isinstance(row, dict):
            return None
        hostname(row.get("host"))
        identifier(row.get("request_id"))
        identifier(row.get("item_id"))
        return (row["host"], row["request_id"], row["item_id"])
    except Exception:  # noqa: BLE001 - any identity failure means "unidentifiable"
        return None


def build_queue(rows):
    """Return (items, ambiguous identities, malformed reason counts, malformed row count).

    Identities are collected before row-content validation, so a row that is
    malformed outside its identity fields still occupies its identity. Any
    identity seen more than once is withheld from items and marked ambiguous,
    whatever the validity of its rows. malformed_rows counts each intrinsically
    invalid row once plus the surplus fully valid rows of each identity group;
    duplicate_identity (group size minus one) is an overlap diagnostic.
    """
    malformed = {}
    invalid_rows = 0
    groups = {}  # identity -> [valid entries, invalid row count]
    for row in rows:
        identity = _queue_identity(row)
        normalized, code = _normalized(normalize_queue_row, row)
        if normalized is None:
            _count(malformed, code)
            invalid_rows += 1
            if identity is not None:
                groups.setdefault(identity, [[], 0])[1] += 1
            continue
        identity, state, has_version, reason = normalized
        groups.setdefault(identity, [[], 0])[0].append((state, has_version, reason))
    items = {}
    ambiguous = set()
    surplus_valid = 0
    for identity, (entries, invalid) in groups.items():
        size = len(entries) + invalid
        if size > 1:
            _count(malformed, "duplicate_identity", size - 1)
            ambiguous.add(identity)
            surplus_valid += max(len(entries) - 1, 0)
            continue
        if entries:
            items[identity] = entries[0]
    return items, ambiguous, malformed, invalid_rows + surplus_valid


def classify(observation, items, ambiguous):
    """Return (class, detail) for one normalized observation."""
    _, host_class, host, request_id, item_id, allowlisted = observation
    # Held classes first: a held pointer is never matched against the queue.
    if host_class in REDIRECT_CLASSES:
        return "held_redirect", host_class
    if host_class in POLICY_HELD_CLASSES or allowlisted is False:
        return "held_policy", host_class
    if allowlisted is not True:
        # No custodian-supplied host classification: nothing downstream may be
        # reported as policy-cleared (fetched/queued/failed).
        return "unproven", "host_policy_unknown"
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
        return "failed", reason or NO_REASON
    return "queued", state


def reconcile(observation_rows, queue_rows):
    items, ambiguous, queue_malformed, queue_malformed_rows = build_queue(queue_rows)

    malformed = {}
    duplicates = 0
    by_url = {}
    conflicting = set()
    tainted = set()
    for row in observation_rows:
        observation, code = _normalized(normalize_observation, row)
        if observation is None:
            _count(malformed, code)
            # A malformed row still declares its URL when the hash itself is
            # well formed; it never contributes host policy or identity.
            declared = _declared_url_sha256(row)
            if declared is not None:
                tainted.add(declared)
            continue
        previous = by_url.get(observation[0])
        if previous is None:
            by_url[observation[0]] = observation
        elif previous == observation:
            duplicates += 1
        else:
            duplicates += 1
            conflicting.add(observation[0])
    # A URL with a fully valid observation and a malformed one is conflicting
    # and stays unclassified. Malformed-only URLs are not unique URLs.
    conflicting.update(tainted.intersection(by_url))

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
            "malformed_rows": queue_malformed_rows,
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


def _sync_parent(directory_fd):
    """Confirm the new name is durable on the same walked directory inode."""
    os.fsync(directory_fd)


def write_new(path, text):
    """Exclusively create a 0600 report and sync both its bytes and name.

    OSError propagates on failure. A complete but not durability-confirmed
    file may remain after directory sync fails; it is never removed or reused.
    A same-name retry still refuses, so the caller must inspect the residue or
    explicitly choose a different output name. No parent directory is created.
    """
    parent, leaf = _walk_parent(path)
    try:
        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        try:
            handle = os.fdopen(fd, "w", encoding="ascii")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_parent(parent)
    finally:
        os.close(parent)


class _CodedArgumentParser(argparse.ArgumentParser):
    """Argument errors print one fixed code; argv text is never echoed."""

    def error(self, message):  # noqa: ARG002 - message may contain argv text
        _emit_error("usage_invalid")
        raise SystemExit(2)


def _emit_error(code):
    sys.stderr.write(json.dumps({"error": code}) + "\n")


def _run(argv):
    parser = _CodedArgumentParser(description="Counts-only portal pointer reconciliation (offline).",
                                  allow_abbrev=False)
    parser.add_argument("--observations", required=True, help="mail-derived pointer observations JSON")
    parser.add_argument("--queue-export", required=True, help="portal queue export JSON")
    parser.add_argument("--output", help="new report file (refuses to overwrite); default stdout")
    args = parser.parse_args(argv)
    try:
        observations = read_json(args.observations, OBSERVATION_SCHEMA, "observations")
        queue = read_json(args.queue_export, QUEUE_SCHEMA, "items")
    except InputError as exc:
        _emit_error(str(exc))
        return 2
    text = render(reconcile(observations, queue))
    if args.output:
        try:
            write_new(args.output, text)
        except OSError:
            _emit_error("output_not_created")
            return 2
    else:
        sys.stdout.write(text)
    return 0


def main(argv=None):
    try:
        return _run(argv)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - never print a traceback or input text
        _emit_error("internal_error")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
