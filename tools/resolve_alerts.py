#!/usr/bin/env python3
"""Attended exact-key alert resolution; no engine imports or live automation."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

RETRY_CONTRACT = {
    "hold_state": "open",
    "non_open_releases": True,
    "repeat_failure": "reopen_and_increment_retained_count",
    "needs_review": "untouched_but_not_an_engine_hold",
    "resolution_preserves": ["id", "key", "first_seen", "last_seen", "count", "owner"],
}
OBSERVED_RESOLUTION_STATES = ("resolved", "resolved_with_evidence")
MAX_INPUT_BYTES = 1024 * 1024


class Refusal(Exception):
    """Fixed diagnostics only, never supplied paths, keys or SQLite text."""


def checked_file(value, *, private=False):
    path = Path(os.path.abspath(value))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise Refusal("symlink_path")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Refusal("regular_single_link_required")
    if private and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()):
        raise Refusal("owner_0600_required")
    return path, info


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink)


def frozen_bytes(value, expected_hash=None):
    path, info = checked_file(value, private=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        actual = os.fstat(fd)
        if identity(actual) != identity(info) or actual.st_size > MAX_INPUT_BYTES:
            raise Refusal("input_changed_or_oversized")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_INPUT_BYTES + 1)
        if identity(os.fstat(fd)) != identity(actual) or len(data) > MAX_INPUT_BYTES:
            raise Refusal("input_changed_or_oversized")
        if identity(checked_file(path, private=True)[1]) != identity(info):
            raise Refusal("input_changed_or_oversized")
    finally:
        os.close(fd)
    digest = hashlib.sha256(data).hexdigest()
    if expected_hash is not None and digest != expected_hash:
        raise Refusal("frozen_hash_mismatch")
    return path, info, data, digest


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal("duplicate_json_field")
        result[key] = value
    return result


def read_json(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise Refusal("invalid_json") from None


def confirmed_target(data):
    contract = read_json(data)
    fields = {"schema_version", "target_state", "confirmed_by", "confirmed_at", "confirmation_reference", "retry_semantics"}
    if not isinstance(contract, dict) or set(contract) != fields or type(contract["schema_version"]) is not int or contract["schema_version"] != 1:
        raise Refusal("contract_shape")
    if contract["target_state"] not in OBSERVED_RESOLUTION_STATES:
        raise Refusal("unconfirmed_target")
    for field in ("confirmed_by", "confirmed_at", "confirmation_reference"):
        if not isinstance(contract[field], str) or not contract[field].strip():
            raise Refusal("custodian_confirmation_required")
    # Canonical JSON distinguishes true from 1, unlike Python dictionary equality.
    if json.dumps(contract["retry_semantics"], sort_keys=True) != json.dumps(RETRY_CONTRACT, sort_keys=True):
        raise Refusal("retry_contract_mismatch")
    return contract["target_state"]


def table_counts(con):
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_schema WHERE type='table'")]
    return {name: con.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names}


def require_integrity(con):
    if con.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise Refusal("integrity_check_failed")


def resolve(*, root, keys_file, expected_count, keys_sha256, contract_file, contract_sha256, quiescence_flag):
    if type(expected_count) is not int or expected_count <= 0:
        raise Refusal("positive_expected_count_required")
    frozen = [frozen_bytes(keys_file, keys_sha256), frozen_bytes(contract_file, contract_sha256), frozen_bytes(quiescence_flag)]
    keys = read_json(frozen[0][2])
    if not isinstance(keys, list) or any(not isinstance(k, str) or not k or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in k) for k in keys):
        raise Refusal("key_list_required")
    if len(keys) != expected_count or len(set(keys)) != expected_count:
        raise Refusal("key_count_or_duplicate")
    target = confirmed_target(frozen[1][2])
    if frozen[2][2] != b"T4_QUIESCENT\n":
        raise Refusal("quiescence_attestation_required")
    path, info = checked_file(Path(root) / "ledger.sqlite", private=True)
    identities = [(s.st_dev, s.st_ino) for _, s, _, _ in frozen] + [(info.st_dev, info.st_ino)]
    if len(set(identities)) != 4:
        raise Refusal("distinct_inputs_required")
    parent = path.parent.stat()
    if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise Refusal("ledger_parent_0700_required")
    lock = path.parent / "run.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _, lock_info = checked_file(lock, private=True)
        if identity(os.fstat(fd)) != identity(lock_info) or (lock_info.st_dev, lock_info.st_ino) in identities:
            raise Refusal("distinct_lock_required")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("root_locked") from None
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_symlink() or sidecar.exists():
                checked_file(sidecar, private=True)
        return transact(path, info, frozen, keys, target, expected_count)
    finally:
        os.close(fd)


def transact(path, info, frozen, keys, target, expected_count):
    con = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1, isolation_level=None)
    try:
        _, current = checked_file(path, private=True)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise Refusal("ledger_changed")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        require_integrity(con)
        if [r[1] for r in con.execute("PRAGMA table_info(alerts)")] != ["id", "key", "first_seen", "last_seen", "count", "owner", "state"]:
            raise Refusal("alerts_schema_mismatch")
        before = table_counts(con)
        initial_changes = con.total_changes
        for key in keys:
            cursor = con.execute("UPDATE alerts SET state=? WHERE key=? COLLATE BINARY AND state='open' COLLATE BINARY", (target, key))
            if cursor.rowcount != 1:
                raise Refusal("exact_open_key_rowcount")
        if con.total_changes - initial_changes != expected_count:
            raise Refusal("unexpected_changes")
        require_integrity(con)
        if table_counts(con) != before:
            raise Refusal("ledger_row_count_changed")
        for file_path, prior, _, digest in frozen:
            _, latest, _, _ = frozen_bytes(file_path, digest)
            if identity(latest) != identity(prior):
                raise Refusal("input_changed_during_transaction")
        _, latest_ledger = checked_file(path, private=True)
        if (latest_ledger.st_dev, latest_ledger.st_ino) != (info.st_dev, info.st_ino):
            raise Refusal("ledger_changed")
        con.commit()
        return {"changed": expected_count, "alerts_before": before["alerts"], "alerts_after": before["alerts"],
                "ledger_rows_before": sum(before.values()), "ledger_rows_after": sum(before.values()), "integrity_ok": True}
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal("invalid_arguments")


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--keys-file", required=True)
    parser.add_argument("--expected-count", required=True, type=int)
    parser.add_argument("--keys-sha256", required=True)
    parser.add_argument("--contract-file", required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--quiescence-flag", required=True)
    try:
        result = resolve(**vars(parser.parse_args(argv)))
    except Refusal as error:
        print(json.dumps({"refused": str(error)}))
        return 2
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        print(json.dumps({"refused": "input_or_database_error"}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
