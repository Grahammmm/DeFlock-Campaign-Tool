#!/usr/bin/env python3
"""Seed a SANDBOX copy of TEST with a reviewed baseline ledger (T13 receipt, G D3-A).

Test tooling for ``scripts/run_t11i_test_harness.py``. It only ever writes inside a
marked sandbox project (``t12_fault_inject.SANDBOX_MARKER``, owner-only), never TEST,
never live. The baseline backup is only read.

Why: TEST itself can sit in its post-apply state (the T4 acceptance adopted its 36
messages), and its adopted identities carry evidence paths under the TEST root. A
plain sandbox copy then has nothing left to adopt, and the adopter refuses every
copied identity (``identity_verification_path_changed``) because the sandbox root is
different. So the harness:

1. replaces the sandbox ledger and intake database with the reviewed pre-apply backup
   (the operator pins its ledger SHA-256; a different file is refused), and
2. relocates the backup's ``verified_mail_export`` identity paths (occurrence
   evidence ``verification_receipt_path``/``export_receipt_path`` and the bound
   originals' ``storage_path``) from the roots they name (``--from-root``: the live
   and TEST roots) to the sandbox root. That is the same rewrite T11j applied to
   TEST, done on the copy: every rewritten target must exist in the sandbox with the
   bound bytes, the originals immutability trigger is dropped only inside the
   rewrite transaction and must be identical and enforced afterwards, and no
   reference to a ``--from-root`` may remain anywhere in ``originals`` or
   ``occurrences``. It also relocates legacy intake ``occurrences.root`` for
   ``mail_delta`` receipts from exactly ``<from-root>/mail-history`` to the sandbox
   export. Each source must be a contained regular file with the catalogued length
   and SHA-256; no other intake identity field changes. Without this step historical
   preservation replay correctly refuses ``receipt_identity_conflict``. This is a
   sandbox repair, not permission to change live identities or relax validation.

Runs inside the Flock container as the ledger owner (``docker exec -u coder``), or
locally in synthetic tests. Prints exactly one JSON line of counts, hashes and fixed
codes (no paths, no record text). Exit 0 when seeded, 1 when refused.

Containment (T13 review): the project must be its own resolved path (no symlink to it or
through an ancestor). ``processing-v1``, ``processing-v1/intake``, the databases, their
temporaries and sidecars are reached without following any symlink, and every relocation
target must resolve inside the sandbox, all checked before the first write; anything
else is refused as ``sandbox_path_not_contained``.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from t12_fault_inject import Refused, check_sandbox  # noqa: E402

EV_KEYS = frozenset(("verification_receipt_path", "export_receipt_path"))
DATABASES = (("ledger.sqlite", Path("processing-v1/ledger.sqlite")),
             ("intake.sqlite", Path("processing-v1/intake/intake.sqlite")))
TRIGGER = "immutable_originals_update"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def contained_dir(project, rel):
    """``project/rel`` as a real directory reached without following any symlink.

    Every component from the project down is lstat-ed (a symlinked ``processing-v1`` or
    ``intake`` could point at TEST), and the resolved path must equal the lexical one,
    so a write there can only land inside the sandbox (T13 review, Sol #1)."""
    path = project
    for part in Path(rel).parts:
        path = path / part
        try:
            info = os.lstat(path)
        except OSError:
            raise Refused("sandbox_path_missing") from None
        if not stat.S_ISDIR(info.st_mode):
            raise Refused("sandbox_path_not_contained")
    if os.path.realpath(path) != str(path):
        raise Refused("sandbox_path_not_contained")
    return path


def contained_file(project, rel):
    """``project/rel`` in a contained directory, itself absent or a regular file (no symlink)."""
    target = contained_dir(project, Path(rel).parent) / Path(rel).name
    if os.path.lexists(target) and not regular(target):
        raise Refused("sandbox_path_not_contained")
    return target


def check_sidecars(db, *, clean):
    """Refuse a database whose WAL/rollback journal holds data; in the sandbox drop empty ones."""
    for suffix in ("-wal", "-journal"):
        side = Path(str(db) + suffix)
        if os.path.lexists(side):
            if not regular(side) or os.lstat(side).st_size:
                raise Refused("sidecar_not_empty")
            if clean:
                side.unlink()
    shm = Path(str(db) + "-shm")
    if clean and os.path.lexists(shm):
        if not regular(shm):
            raise Refused("sidecar_not_empty")
        shm.unlink()


def install_private_copy(source, target):
    """Copy ``source`` over ``target`` through an owner-only temporary file and os.replace."""
    temporary = target.with_name(target.name + ".t11i-baseline-tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)  # explicit: a default ACL would ignore the umask
        with open(source, "rb") as stream, os.fdopen(fd, "wb") as out:
            fd = None
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.lexists(temporary):
            temporary.unlink()


def refs(con, root):
    prefix = root + "/"
    escaped = json.dumps(prefix)[1:-1]
    return (con.execute("SELECT COUNT(*) FROM originals WHERE storage_path LIKE ? ESCAPE '\\'",
                        (_like(prefix) + "%",)).fetchone()[0]
            + con.execute("SELECT COUNT(*) FROM occurrences WHERE instr(evidence, ?) > 0",
                          (escaped,)).fetchone()[0])


def _like(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def relocate(ledger, sandbox, from_roots):
    """The T11j rewrite on the sandbox ledger: ``from_roots`` -> ``sandbox``. Returns counts."""
    con = sqlite3.connect(str(ledger), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        trigger = con.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                              (TRIGGER,)).fetchone()
        if trigger is None or con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                                          "AND tbl_name='occurrences'").fetchone()[0]:
            raise Refused("trigger_layout_unexpected")
        triggers_before = sorted(tuple(r) for r in con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"))
        plan_orig, plan_occ = [], []
        for root in from_roots:
            prefix, escaped = root + "/", json.dumps(root + "/")[1:-1]
            target_escaped = json.dumps(sandbox + "/")[1:-1]
            originals = con.execute("SELECT sha256, bytes, storage_path FROM originals WHERE "
                                    "storage_path LIKE ? ESCAPE '\\'", (_like(prefix) + "%",)).fetchall()
            occurrences = con.execute("SELECT id, original_sha256, acquisition_method, evidence FROM occurrences "
                                      "WHERE instr(evidence, ?) > 0", (escaped,)).fetchall()
            if any(row["acquisition_method"] != "verified_mail_export" for row in occurrences):
                raise Refused("foreign_root_reference_not_relocatable")
            if {row["original_sha256"] for row in occurrences} != {row["sha256"] for row in originals}:
                raise Refused("originals_not_bound_to_occurrences")
            for row in originals:
                new = sandbox + row["storage_path"][len(root):]
                if new != f"{sandbox}/processing-v1/intake/blobs/{row['sha256']}" or os.path.realpath(new) != new:
                    raise Refused("original_target_unexpected")
                if not regular(new) or os.path.getsize(new) != row["bytes"] or sha256_file(new) != row["sha256"]:
                    raise Refused("original_target_bytes_mismatch")
                plan_orig.append((new, row["sha256"], row["storage_path"]))
            for row in occurrences:
                old = row["evidence"]
                new = old.replace(escaped, target_escaped)
                before, after = json.loads(old), json.loads(new)
                changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
                if not changed or not changed <= EV_KEYS:
                    raise Refused("evidence_change_unexpected")
                receipt = after.get("verification_receipt_path")
                if receipt != f"{sandbox}/processing-v1/intake/verification-receipts/{before.get('receipt_sha256')}" \
                        or os.path.realpath(receipt) != receipt \
                        or not regular(receipt) or sha256_file(receipt) != before.get("receipt_sha256"):
                    raise Refused("verification_receipt_target_mismatch")
                export = after.get("export_receipt_path")
                if not isinstance(export, str) or not regular(export) or os.path.realpath(export) != export \
                        or not export.startswith(sandbox + "/"):
                    raise Refused("export_receipt_target_missing")
                plan_occ.append((new, row["id"], old))
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(f"DROP TRIGGER {TRIGGER}")
            moved_orig = sum(con.execute("UPDATE originals SET storage_path=? WHERE sha256=? AND storage_path=?",
                                         (new, sha, old)).rowcount for new, sha, old in plan_orig)
            con.execute(trigger[0])
            moved_occ = sum(con.execute("UPDATE occurrences SET evidence=? WHERE id=? AND evidence=?",
                                        (new, oid, old)).rowcount for new, oid, old in plan_occ)
            triggers_after = sorted(tuple(r) for r in con.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"))
            left = sum(refs(con, root) for root in from_roots)
            if (moved_orig, moved_occ) != (len(plan_orig), len(plan_occ)) or triggers_after != triggers_before \
                    or left:
                raise Refused("relocation_verification_failed")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        enforced = False
        if plan_orig:
            try:
                con.execute("UPDATE originals SET storage_path=storage_path WHERE sha256=?", (plan_orig[0][1],))
            except sqlite3.DatabaseError:
                enforced = True
        else:
            enforced = con.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                                   (TRIGGER,)).fetchone() is not None
        return {"occurrences_relocated": moved_occ, "originals_relocated": moved_orig,
                "foreign_root_refs_after": sum(refs(con, root) for root in from_roots),
                "triggers_identical": True, "immutability_trigger_enforced": enforced,
                "integrity": con.execute("PRAGMA integrity_check").fetchone()[0],
                "foreign_key_violations": len(con.execute("PRAGMA foreign_key_check").fetchall()),
                "mail_messages": con.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0],
                "originals": con.execute("SELECT COUNT(*) FROM originals").fetchone()[0]}
    finally:
        con.close()


def relocate_intake(database, project, from_roots):
    """Relocate only verified mail-export roots on the private sandbox copy.

    Legacy replay binds occurrences.root as well as canonical evidence paths.
    Keep every other identity field unchanged; never relax the replay comparator.
    """
    export = contained_dir(project, "mail-history")
    mapping = {root + "/mail-history": str(export) for root in from_roots}
    con = sqlite3.connect(str(database), isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        plan = []
        for oid, sha, path, root, receipt in con.execute(
                "SELECT oid,sha,path,root,receipt FROM occurrences"):
            if not any(root == source or root.startswith(source + "/") for source in from_roots):
                continue
            if root not in mapping or json.loads(receipt).get("source") != "mail_delta":
                raise Refused("intake_root_not_relocatable")
            # Do not reach through links or trust a hash-only destination guess.
            relative = Path(path)
            if relative.is_absolute() or ".." in relative.parts:
                raise Refused("intake_source_not_contained")
            target = contained_file(project, Path("mail-history") / relative)
            size = con.execute("SELECT bytes FROM docs WHERE sha=?", (sha,)).fetchone()
            if not regular(target) or not size or os.path.getsize(target) != size[0] or sha256_file(target) != sha:
                raise Refused("intake_source_bytes_mismatch")
            plan.append((mapping[root], oid, root))
        moved = sum(con.execute("UPDATE occurrences SET root=? WHERE oid=? AND root=?", row).rowcount
                    for row in plan)
        if moved != len(plan) or con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise Refused("intake_relocation_verification_failed")
        con.execute("COMMIT")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"intake_occurrences_relocated": moved}
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def seed(project, backup, ledger_sha256, from_roots):
    project = check_sandbox(project)
    # The sandbox itself, not a symlink to it or through one: every write below must
    # resolve inside it (T13 review, Sol #1).
    if os.path.islink(project) or os.path.realpath(project) != str(project):
        raise Refused("sandbox_path_not_contained")
    sandbox = str(project)
    backup = Path(os.path.abspath(backup))
    if not re.fullmatch(r"[0-9a-f]{64}", ledger_sha256 or ""):
        raise Refused("baseline_ledger_sha256_invalid")
    roots = []
    for root in from_roots:
        root = os.path.abspath(root).rstrip("/")
        if root in ("", sandbox) or sandbox.startswith(root + "/") or root.startswith(sandbox + "/"):
            raise Refused("from_root_invalid")
        roots.append(root)
    if not roots:
        raise Refused("from_root_missing")
    if os.path.islink(backup) or not backup.is_dir() or str(backup).startswith(sandbox + "/"):
        raise Refused("baseline_backup_invalid")
    sources = {}
    targets = {}
    for name, rel in DATABASES:
        source = backup / name
        if not regular(source):
            raise Refused("baseline_backup_incomplete")
        targets[name] = contained_file(project, rel)  # before any write: every target contained
        check_sidecars(source, clean=False)
        sources[name] = (source, sha256_file(source))
    if sources["ledger.sqlite"][1] != ledger_sha256:
        raise Refused("baseline_ledger_hash_mismatch")
    for name, rel in DATABASES:
        target = contained_file(project, rel)  # re-checked right before this write
        if target != targets[name]:
            raise Refused("sandbox_path_not_contained")
        check_sidecars(target, clean=True)
        install_private_copy(sources[name][0], target)
        if sha256_file(target) != sources[name][1]:
            raise Refused("baseline_copy_hash_mismatch")
    result = relocate(contained_file(project, DATABASES[0][1]), sandbox, roots)
    result.update(relocate_intake(contained_file(project, DATABASES[1][1]), project, roots))
    if result["foreign_root_refs_after"] or result["integrity"] != "ok" or result["foreign_key_violations"] \
            or not result["immutability_trigger_enforced"]:
        raise Refused("baseline_not_consistent")
    return {"baseline_ledger_sha256": sources["ledger.sqlite"][1],
            "baseline_intake_sha256": sources["intake.sqlite"][1],
            "from_roots": len(roots), **result}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", required=True, help="the marked sandbox records project")
    p.add_argument("--backup", required=True, help="reviewed baseline backup dir (ledger.sqlite + intake.sqlite)")
    p.add_argument("--ledger-sha256", required=True, help="pinned SHA-256 of the backup ledger.sqlite")
    p.add_argument("--from-root", action="append", default=[], help="root the backup paths may name (repeat)")
    args = p.parse_args(argv)
    os.umask(0o077)
    try:
        line = {"status": "seeded", **seed(args.project, args.backup, args.ledger_sha256, args.from_root)}
        code = 0
    except Refused as refusal:
        line, code = {"status": "refused", "code": str(refusal)}, 1
    except (OSError, sqlite3.Error, ValueError):
        line, code = {"status": "refused", "code": "baseline_seed_error"}, 1
    print(json.dumps(line, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
