"""Backup closure for owner-approved mail-resolution policies (T4B slice 4).

A mail-export resolution row names its policy by hash (`policy_sha256`); the policy file
itself lives outside the ledger at `<root>/resolution-policies/<sha256>`. A backup that omits
it would restore a ledger whose resolutions can no longer be traced to their approved policy.
`add_resolution_policies` extends a `backup_v2.Profile` with every policy file as a
`configuration` store and one closure query on the canonical ledger, so `backup_v2` fails closed
(`database_reference_not_closed`) when a referenced policy is missing from the backup. No new
DDL, no new ledger marker: the `original_resolution_v1` schema pin is unchanged.
"""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import re
import stat

from . import backup_v2

POLICY_DIR = "resolution-policies"
_HASH = re.compile(r"[0-9a-f]{64}")
_ROOT = re.compile(r"/[A-Za-z0-9_./+@%=:,-]*")
MAX_POLICY_FILES = 1000
MAX_POLICY_BYTES = 2 * 1024 * 1024


def reference_sql(root):
    """SELECT returning (role, absolute policy path, policy hash) per mail-export resolution row."""
    text = str(root)
    backup_v2.require(_ROOT.fullmatch(text) is not None and ".." not in Path(text).parts, "policy_root_invalid")
    return ("SELECT 'configuration', '%s/%s/' || policy_sha256, policy_sha256 FROM original_resolutions "
            "WHERE json_extract(payload_json, '$.schema') = 'original_resolution_mail_export_v1' "
            "GROUP BY policy_sha256" % (text.rstrip("/"), POLICY_DIR),)


def policy_stores(root):
    """One `configuration` store per policy file; each file must hash to its own name."""
    folder = Path(root) / POLICY_DIR
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return ()
    backup_v2.require(len(names) <= MAX_POLICY_FILES, "policy_file_bound")
    stores = []
    for name in names:
        backup_v2.require(_HASH.fullmatch(name) is not None, "policy_file_name_invalid")
        path = folder / name
        info = path.lstat()
        backup_v2.require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_POLICY_BYTES, "policy_file_invalid")
        with open(path, "rb") as stream:
            raw = stream.read(MAX_POLICY_BYTES + 1)
        backup_v2.require(hashlib.sha256(raw).hexdigest() == name, "policy_file_hash_mismatch")
        stores.append(backup_v2.Store("resolution-policy-" + name[:16], str(path), ("configuration",),
                                      expected_sha256=name))
    return tuple(stores)


def add_resolution_policies(profile, root):
    """Return `profile` with the policy files and their closure query added.

    Does nothing when `<root>/resolution-policies/` does not exist, and refuses a profile
    that already carries the policy stores or the query (no silent duplication).
    """
    stores = policy_stores(root)
    if not stores:
        return profile
    query = reference_sql(root)
    ledgers = [s for s in profile.stores if s.kind == "sqlite" and "canonical_ledger" in s.roles]
    backup_v2.require(len(ledgers) == 1 and "original_resolutions" in ledgers[0].required_tables
                      and query[0] not in ledgers[0].reference_sql, "policy_closure_profile_invalid")
    existing = {s.source for s in profile.stores}
    backup_v2.require(not existing & {s.source for s in stores}, "policy_closure_profile_invalid")
    changed = tuple(replace(s, reference_sql=s.reference_sql + query) if s is ledgers[0] else s
                    for s in profile.stores)
    return replace(profile, stores=changed + stores)
