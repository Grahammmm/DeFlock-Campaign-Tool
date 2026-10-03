# Bounded records-backup-v2 candidate

Separate programmatic library only: campaign_tool.records.recovery.backup_v2.
No CLI changes. Both existing campaign_tool/backup.py and records/recovery/backup.py
remain byte-identical to the supplied generic baseline wheel. V1 selected-file
scope, categories, one-ledger rule and limits are unchanged.

## Provenance

Parent-declared canonical commit: 0e7e2add4509624041d255830ac1c73d7b86dec0.
Baseline wheel SHA-256:
e515adbbbae725fd7dc9c4b743409aa02a4d3ea01164ff39929867558f8f2037.
The wheel identity is independently checked. Its association with that commit
is parent-supplied, not independent Git-tree verification. Baseline package bytes
are retained under baseline/. No network fetch, law-file edit, merge, PR or push.

## API and trust boundary

from campaign_tool.records.recovery.backup_v2 import (
    Store, Profile, CallerQuiescence, backup_v2, verify_v2, restore_v2
)

backup_v2(absolute_new_destination, profile, quiescence=caller_attestation)
verify_v2(absolute_backup_set, manifest_sha256=exact_separately_trusted_digest)
restore_v2(absolute_backup_set, absolute_new_destination,
           manifest_sha256=exact_separately_trusted_digest)

Profile roots and Store sources are explicit absolute paths. Each store has an
opaque unique name, included/excluded classification and required roles. Required
roles are canonical_ledger, intake_ledger, original_blob, receipt, cursor and
review_bundle. At least two distinct SQLite stores are required. Cursor roles on
SQLite require mail_checkpoints. File cursors are allowed but are opaque bytes;
profile authors remain responsible for identifying actual operational cursor state.

Each SQLite declaration supplies required tables, the exact schema SHA-256 and
read-only SELECT closure queries returning (role, historical_absolute_source_path,
sha256). Every returned reference must resolve to an included store with matching
role and copied hash. Original, receipt and review roles must be database-bound.
Additional real schema adapters and exhaustive semantic inventories are NOT supplied
by this candidate. Profile authors are trusted engine callers, not untrusted JSON.
SELECT queries are bounded and query-only, not a general untrusted plugin interface.

CallerQuiescence must cover exactly every declared root, confirm all writers stopped,
declare independent_all_writer_audit, and reference an independently obtained evidence
digest with recent UTC observation (120 seconds maximum). The library does not read
or authenticate that external evidence and cannot establish that the caller told
the truth. run.py root_lock alone is explicitly not an accepted basis. Do not use
a manufactured attestation to operate on live data. The synthetic WAL test deliberately
tests snapshot mechanics separately from actual writer-boundary proof.

All snapshots occur during the same caller-required quiescent interval. Existing
SQLite online backup captures existing WAL. When a caller-quiescent source has no
WAL/SHM/journal sidecars, a read-only immutable SQLite connection performs the backup
without creating source sidecars. This relies on the independently established caller
boundary; existing sidecars always select the WAL-aware path. Strict metadata scope
comparison remains in place, with no blanket sidecar exemption or source cleanup.
Independent integrity, foreign-key, exact schema
and reference-closure checks run on each snapshot and again during verify/restore.
Source metadata inventory is compared before/after. This catches some changes but is
NOT a substitute for independently established all-writer quiescence. No writers are
stopped, signalled, restarted or discovered by this library.

## Allowlist, permissions and privacy

The scope walk is FD-anchored, rejects symlinks, special files, hardlinks, wrong owners
and shared permissions. Directories must be 0700; files 0400 or 0600. Every file and
directory under declared roots must be classified or be a required containing directory.
Unknown/missing stores fail. Exclusions are credential, sqlite_sidecar, process_state
or rebuildable; sidecars must pair with a declared SQLite store. Credential filename
guards deny common .runtime/.env/key/secrets paths, but are not a content secret scanner.
Do not place credentials inside included SQLite/evidence payloads; caller profiles
must separately establish that boundary. No credential bytes are read/copied from
excluded files. Raw private provenance remains private, not publicly redacted.

Format is a bounded private directory set, not tar. payload/<opaque-name> files and
manifest.json only are accepted by verify. Existing v1 256 MiB/member, 1 GiB/total,
1,000-entry and 1 MiB-manifest limits are reused. At most 10,000 references/database,
20 closure queries, and 16 roots; large real datasets may intentionally be refused.
Operation checks use a 60-second budget; SQLite snapshots also have a 30-second
snapshot bound. Backup checks its whole-operation deadline after every payload copy
and database validation, around scope closure and source inventory, after manifest
serialization and digest calculation, immediately before manifest publication, and
again before returning success. An over-budget final payload copy is rejected before
manifest publication. A slow publication/fsync cannot return over-budget success,
but may leave a manifest behind; existence alone is not a success acknowledgement.
Restore's single budget starts before source verification and is checked
before and after each copy, manifest publication and final verification; elapsed copying
cannot return success. A reused copy may run past its cooperative deadline before control
returns, but the over-budget restore fails rather than reporting success.
These are cooperative checks, not protection against uninterruptible
filesystem I/O or a host failure. No subprocess host watchdog or quota is installed.

Directories are private; copied payloads and manifests are mode 0400, application
write-once via O_EXCL, fsynced by reused helpers. Owner can still chmod/delete them;
this is not WORM or cryptographic authorization. Store-source paths, references,
SQL closure declarations, hashes and excluded classifications in the manifest are
private. API errors use fixed reason codes; no CLI/stdout channel is installed.

## Restore semantics and limitations

Only a nonexistent destination is accepted, a deliberately stricter contract than
accepting a caller-owned empty target. Existing empty/nonempty/symlink targets are
never overwritten or repaired. Destination parent must already be owner-only.
Source/destination ancestors and descendants are rejected. Restore also rejects targets
inside or containing any original profile root recorded in the trusted manifest, even
when historical roots no longer exist. Historical paths are compared, never traversed.

Restore validates the exact separately trusted manifest digest, archive allowlist,
permissions, bytes, schema identities and references. It copies original manifest,
database, receipt and review bytes without rebasing absolute historical paths or
changing signatures. Restored SQLite files are read-only recovery snapshots, not a
runnable pipeline. Leases/checkpoints/history are preserved as evidence, not claimed
live ownership. No review authentication or signature authority is manufactured.
Trusted keys/policies and path-dependent review reconciliation remain external.

No activation, sends, deployments, timers or automatic replay. Replay acceptance
uses a distinct synthetic writable clone created by the test, NOT by restore.
No claim of live pipeline recovery, exhaustive real-store coverage, verified external
writer quiescence, restored signature authorization or runnable reconciliation.

Interrupted operations may leave a private incomplete new directory. Export writes
its manifest only after successful scope/snapshot checks; restore copies its manifest
late and returns success only after re-verification. Absence of a success return is
actionable; manifest existence alone is NOT proof that restore completed. Do not
silently retry into the same destination. No automatic deletion of partial trees.

## Synthetic tests and review

Run from this candidate root only:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 tests/test_records_backup_v2.py

Tests use only generated SQLite/files and deny socket.connect in the test process.
They exercise WAL mechanics, references, hashes, permission/path/link/unknown-store
failures, unchanged v1 selected-file behavior, interrupted operations and explicit
synthetic replay. No real records, subscribers, mailbox, keys, OG3 or production API.

Independent reviewer must decide profile completeness and caller-boundary evidence
before any future operational wiring. Existing held adapter corrections remain held.


## Post-correction pre-push verification boundary

This wheel-derived candidate does not supply the canonical checkout's pre-push tools.
The correction receipt records existence checks and actual exits (or explicit missing
status) for node scripts/scan-secrets.mjs and python3 -B tools/check_public_tree.py.
A missing script is NOT a passing privacy scan. No checkout tree is fetched to replace
it. Full-source packaging and canonical pre-push/privacy verification remain pending
the parent's independently reviewed, canonical-base three-file draft.
