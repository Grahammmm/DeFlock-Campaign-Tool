"""Historical mail coverage verdict over offline receipt diffs (design item M-26, L-B1).

Reads offline-mail-diff-v1 files (the shape written by the accepted packet's
receipt_inventory.py diff subcommand) and optional historical-root-attestation-v1
files supplied by the custodian. Prints and writes, per historical root, the
counts and a verdict; it never fetches mail, never edits an input and never
produces or fills an attestation.

Verdict rule (docs/HISTORICAL-COVERAGE.md):
  * a diff "removed" occurrence is HISTORICAL_ONLY: still on file in the frozen
    historical root, counted and retained in the historical denominator;
  * a diff "changed" occurrence is CONTENT_CONFLICT for the custodian;
  * a root without a valid attestation bound to its diff by diff_sha256 is
    UNSUPPLIED;
  * the overall verdict is never COVERED while any root is UNSUPPLIED or any
    supplied attestation is bound to no supplied diff.

Argv is exactly: --diff ABS (repeatable) [--attestation ABS (repeatable)] --output NEWABS
Exit: 0 overall COVERED, 1 report written with another overall verdict,
2 typed refusal (no report printed, no output file created).
Output is private (0600) and REVIEW REQUIRED; it is not an acceptance.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


DIFF_SCHEMA = "offline-mail-diff-v1"
ATTESTATION_SCHEMA = "historical-root-attestation"
ATTESTATION_VERSION = 1
REPORT_SCHEMA = "historical-coverage-verdict-v1"
COVERED, CONFLICT, UNSUPPLIED = "COVERED", "NOT_COVERED_CONFLICT", "UNSUPPLIED"
MAX_INPUT_BYTES = 64 * 1024 ** 2
HEX64 = re.compile(r"[0-9a-f]{64}")
ROOT_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
UID_TEXT = re.compile(r"[1-9][0-9]{0,19}")
DIFF_KEYS = {"schema", "review_required", "added", "removed", "changed", "counts", "limits"}
COUNT_KEYS = {"added", "removed", "changed", "unchanged"}
RECORD_KEYS = {"identity", "original", "attachments"}
ATTESTATION_KEYS = {"schema", "version", "root_label", "layer", "diff_sha256", "inventory_sha256",
                    "root_manifest_sha256", "capture_time_utc", "count_unit",
                    "producer_statement_sha256", "attested_by_role", "status"}
COUNT_UNITS = {"receipts", "occurrences", "unique_messages"}
NOTE = ("HISTORICAL_ONLY occurrences stay on file in their frozen historical root and are retained "
        "in the denominator; CONTENT_CONFLICT occurrences go to the custodian; UNSUPPLIED means no "
        "custodian attestation is bound to the diff. Review required; not an acceptance.")


class Refusal(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(condition, code):
    if not condition:
        raise Refusal(code)


def is_int(value):
    return type(value) is int


def canonical_path(text):
    require(isinstance(text, str) and text and "\x00" not in text, "path_invalid")
    path = Path(text)
    require(path.is_absolute(), "path_not_absolute")
    require(path.resolve() == path and os.path.normpath(text) == text, "path_not_canonical")
    return path


def read_input(text):
    path = canonical_path(text)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refusal("input_unreadable") from None
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode), "input_not_regular")
        require(before.st_size <= MAX_INPUT_BYTES, "input_size_bound")
        data = stream.read(MAX_INPUT_BYTES + 1)
        after = os.fstat(stream.fileno())
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
            and len(data) == before.st_size, "input_changed")
    return path, data


def parse_json(data):
    def no_duplicates(pairs):
        keys = [k for k, _v in pairs]
        require(len(keys) == len(set(keys)), "json_invalid")
        return dict(pairs)

    def no_constants(_name):
        raise Refusal("json_invalid")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicates,
                          parse_constant=no_constants)
    except (UnicodeDecodeError, ValueError):
        raise Refusal("json_invalid") from None


def check_object(item, code):
    require(isinstance(item, dict) and set(item) == {"sha256", "bytes"} or
            isinstance(item, dict) and set(item) == {"part", "sha256", "bytes"}, code)
    require(isinstance(item["sha256"], str) and HEX64.fullmatch(item["sha256"]), code)
    require(is_int(item["bytes"]) and item["bytes"] >= 0, code)


def check_identity(identity, code):
    require(isinstance(identity, list) and len(identity) == 4, code)
    for value in identity[:2]:
        require(isinstance(value, str) and 0 < len(value) <= 256 and "\x00" not in value, code)
    for value in identity[2:]:
        require(isinstance(value, str) and UID_TEXT.fullmatch(value), code)
    return tuple(identity)


def check_record(item):
    code = "diff_record_shape"
    require(isinstance(item, dict) and set(item) == RECORD_KEYS, code)
    key = check_identity(item["identity"], code)
    original = item["original"]
    require(isinstance(original, dict) and set(original) == {"sha256", "bytes"}, code)
    check_object(original, code)
    attachments = item["attachments"]
    require(isinstance(attachments, list), code)
    parts = set()
    for attachment in attachments:
        require(isinstance(attachment, dict) and set(attachment) == {"part", "sha256", "bytes"}, code)
        check_object(attachment, code)
        part = attachment["part"]
        require(isinstance(part, str) and part and part != "0" and part not in parts, code)
        parts.add(part)
    return key


def validate_diff(document):
    require(isinstance(document, dict), "diff_schema")
    require(document.get("schema") == DIFF_SCHEMA, "diff_schema")
    require(set(document) == DIFF_KEYS, "diff_keys")
    require(document["review_required"] is True, "diff_review_required")
    require(isinstance(document["limits"], str), "diff_keys")
    for key in ("added", "removed", "changed"):
        require(isinstance(document[key], list), "diff_list_shape")
    counts = document["counts"]
    require(isinstance(counts, dict) and set(counts) == COUNT_KEYS, "diff_counts_shape")
    require(all(is_int(counts[k]) and counts[k] >= 0 for k in COUNT_KEYS), "diff_counts_shape")
    for key in ("added", "removed", "changed"):
        require(counts[key] == len(document[key]), "diff_counts_mismatch")
    seen = set()
    for key in ("added", "removed"):
        for item in document[key]:
            identity = check_record(item)
            require(identity not in seen, "diff_identity_overlap")
            seen.add(identity)
    for item in document["changed"]:
        require(isinstance(item, dict) and set(item) == {"identity", "before", "after"}, "diff_record_shape")
        identity = check_identity(item["identity"], "diff_record_shape")
        require(check_record(item["before"]) == identity == check_record(item["after"]), "diff_record_shape")
        require(item["before"] != item["after"], "diff_changed_not_different")
        require(identity not in seen, "diff_identity_overlap")
        seen.add(identity)
    return {"added": counts["added"], "removed": counts["removed"],
            "changed": counts["changed"], "unchanged": counts["unchanged"]}


def validate_attestation(document):
    require(isinstance(document, dict), "attestation_schema")
    require(document.get("schema") == ATTESTATION_SCHEMA, "attestation_schema")
    require(set(document) == ATTESTATION_KEYS, "attestation_keys")
    require(is_int(document["version"]) and document["version"] == ATTESTATION_VERSION,
            "attestation_version")
    require(document["layer"] == "L-B1", "attestation_layer")
    require(document["status"] == "ATTESTED", "attestation_status")
    require(document["attested_by_role"] == "custodian", "attestation_role")
    label = document["root_label"]
    require(isinstance(label, str) and ROOT_LABEL.fullmatch(label), "attestation_root_label")
    for key in ("diff_sha256", "producer_statement_sha256"):
        require(isinstance(document[key], str) and HEX64.fullmatch(document[key]), "attestation_digest")
    for key in ("inventory_sha256", "root_manifest_sha256"):
        value = document[key]
        require(value is None or (isinstance(value, str) and HEX64.fullmatch(value)), "attestation_digest")
    capture = document["capture_time_utc"]
    require(isinstance(capture, str), "attestation_capture_time")
    try:
        moment = datetime.datetime.fromisoformat(capture)
    except ValueError:
        raise Refusal("attestation_capture_time") from None
    require(moment.tzinfo is not None and moment.utcoffset() is not None, "attestation_capture_time")
    require(document["count_unit"] in COUNT_UNITS, "attestation_count_unit")
    return document


def historical_denominator(counts):
    """Every historical occurrence: HISTORICAL_ONLY + CONTENT_CONFLICT + unchanged."""
    return counts["removed"] + counts["changed"] + counts["unchanged"]


def root_verdict(counts, bound):
    if not bound:
        return UNSUPPLIED
    if counts["changed"]:
        return CONFLICT
    return COVERED


def evaluate(diffs, attestations):
    """diffs: [(file_name, sha256, counts)]; attestations: [validated documents]."""
    by_digest, labels = {}, set()
    for document in attestations:
        require(document["diff_sha256"] not in by_digest, "attestation_duplicate_binding")
        require(document["root_label"] not in labels, "attestation_duplicate_label")
        by_digest[document["diff_sha256"]] = document
        labels.add(document["root_label"])
    roots, used = [], set()
    totals = {"added": 0, "historical_only": 0, "content_conflict": 0, "unchanged": 0,
              "historical_denominator": 0}
    for name, digest, counts in diffs:
        bound = by_digest.get(digest)
        if bound is not None:
            used.add(digest)
        shown = {"added": counts["added"], "historical_only": counts["removed"],
                 "content_conflict": counts["changed"], "unchanged": counts["unchanged"],
                 "historical_denominator": historical_denominator(counts)}
        for key in totals:
            totals[key] += shown[key]
        roots.append({
            "diff_file": name,
            "diff_sha256": digest,
            "attestation": "BOUND" if bound else "MISSING",
            "root_label": bound["root_label"] if bound else None,
            "capture_time_utc": bound["capture_time_utc"] if bound else None,
            "count_unit": bound["count_unit"] if bound else None,
            "counts": shown,
            "verdict": root_verdict(counts, bound is not None),
        })
    roots.sort(key=lambda r: (r["root_label"] or "", r["diff_file"], r["diff_sha256"]))
    unbound = sorted(({"root_label": d["root_label"], "diff_sha256": d["diff_sha256"]}
                      for digest, d in by_digest.items() if digest not in used),
                     key=lambda x: x["root_label"])
    verdicts = {r["verdict"] for r in roots}
    if UNSUPPLIED in verdicts or unbound:
        overall = UNSUPPLIED
    elif CONFLICT in verdicts:
        overall = CONFLICT
    else:
        overall = COVERED
    return {"schema": REPORT_SCHEMA, "review_required": True, "layer": "L-B1",
            "roots": roots, "unbound_attestations": unbound, "totals": totals,
            "overall_verdict": overall, "note": NOTE}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal("argv_invalid")


def parse_argv(argv):
    parser = Parser(prog="historical_coverage_check", allow_abbrev=False, add_help=False)
    parser.add_argument("--diff", action="append", required=True)
    parser.add_argument("--attestation", action="append", default=[])
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def write_new_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def run(argv):
    args = parse_argv(argv)
    output = canonical_path(args.output)
    require(not os.path.lexists(output), "output_exists")
    require(output.parent.is_dir(), "output_parent_missing")
    diffs, seen = [], set()
    for text in args.diff:
        path, data = read_input(text)
        require(path != output, "output_exists")
        digest = hashlib.sha256(data).hexdigest()
        require(digest not in seen, "duplicate_diff")
        seen.add(digest)
        diffs.append((path.name, digest, validate_diff(parse_json(data))))
    attestations = []
    for text in args.attestation:
        path, data = read_input(text)
        attestations.append(validate_attestation(parse_json(data)))
    report = evaluate(diffs, attestations)
    encoded = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
    write_new_private(output, encoded)
    return report, encoded


def main(argv=None):
    try:
        report, encoded = run(sys.argv[1:] if argv is None else argv)
    except Refusal as refusal:
        sys.stderr.write(json.dumps({"status": "refused", "code": refusal.code}, sort_keys=True) + "\n")
        return 2
    except FileExistsError:
        sys.stderr.write(json.dumps({"status": "refused", "code": "output_exists"}, sort_keys=True) + "\n")
        return 2
    sys.stdout.write(encoded.decode("utf-8"))
    return 0 if report["overall_verdict"] == COVERED else 1


if __name__ == "__main__":
    sys.exit(main())
