# Records backup operations (backup_v2 profile and adapter)

`records backup` and `records backup-verify` wrap the `backup_v2` library with a reviewed profile of the records project
layout. Output is codes and counts only. The adapter writes nothing off-box, deletes nothing and never treats
`run-records.lock` as quiescence. The attended writer procedure that produces the evidence is added by DFGL-12B.

## Commands

- `records backup --check-profile --project P` prints coverage counts (files per role, excluded per class, totals against
  the limits) and exits 1 on `PROFILE_INCOMPLETE`, `LIMIT_ENTRIES` or `LIMIT_BYTES`. It writes nothing.
- `records backup --project P --into DIR --quiescence FILE [--quiescence-sha256 HEX]` refuses with `QUIESCENCE_REFUSED`
  unless FILE is a fresh (at most 120 s) `records-quiescence-v1` document from `records quiescence-audit` with
  `basis=independent_all_writer_audit`, `all_writers_stopped=true` and `roots` equal to `[P]`. `--quiescence-sha256` is the
  digest the audit printed; a mismatch (tampered file) is refused. DIR must be outside P. A set is written to
  `DIR/set-<utc>` and `{set, manifest_sha256, evidence_sha256, at}` is appended to `DIR/backup-digests.jsonl` (0600).
- `records backup-verify --set DIR/set-... --digests FILE` verifies the set against the digest recorded for it. Keep a copy
  of `backup-digests.jsonl` apart from the sets; a list stored beside a tampered set proves nothing.

## Profile coverage (path relative to the project root)

| Path | Class |
| --- | --- |
| `processing-v1/ledger.sqlite` | canonical_ledger (+ cursor when `mail_checkpoints` exists) |
| `processing-v1/intake/intake.sqlite` | intake_ledger |
| `portal-queue-v1/portal.sqlite` | queue_store |
| `processing-v1/intake/blobs/<sha>`, `portal-queue-v1/objects/<sha>` | original_blob (name = content sha) |
| `processing-v1/intake/verification-receipts/<sha>`, `.../local-capture-receipts/<sha>`, `processing-v1/mail/<sha>/receipt.json` | receipt |
| `processing-v1/intake/resolution-policies/<sha>` | resolution_policy |
| `approvals/queue.jsonl`, `public-packets.jsonl` | review_bundle |
| `public-packets.json`, `site-changes/**`, `processing-v1/proposals/**` | publication_store |
| `processing-v1/awaiting-ocr.jsonl`, `processing-v1/outbox/**` | queue_store |
| `processing-v1/mail/**`, `extract/**`, `ocr/**`, `artifacts/evidence/*` | derived_resume |
| `run-outputs/**`, `run-report.json`, `processing-v1/runs/**` | excluded: rebuildable |
| `*.lock`, `public-packets.KILL`, sandbox marker | excluded: process_state |
| `*.sqlite-wal`, `-shm`, `-journal` of a declared database | excluded: sqlite_sidecar |
| `.runtime`, `.env*`, keys, tokens | excluded: credential |

Any other file, any symlink or special file leaves the profile incomplete. Directories are declared (empty ones
included) through `Profile.directories`; the library still refuses an undeclared directory.

Closure queries: ledger `originals.storage_path` and `extraction_adapter_imports.receipt_sha256` (evidence file), the
intake `preservations`, portal `portal_versions`, and `original_resolutions` through the library's fixed reader. The
two journals have no database row, so the profile binds each with a constant closure row holding its digest at profile
build time. Supported schemas are pinned from the reviewed migration modules (ledger, intake and portal), never taken
from the source database: a missing, changed or extra table, trigger or index is refused as `schema_drift`, and optional
migration groups are accepted only exactly or absent. The schema hash handed to the library is computed over the
reviewed reference rows built from those modules, not over the source database's `sqlite_master`. A corrupt or
unreadable database is refused with the fixed reason `database_unreadable`.

## Limits (library, unchanged)

256 MiB per member, 1 GiB total, 1,000 stored entries (files plus excluded files), 4,000 directory entries scanned,
1 MiB manifest, 10,000 references per database, 60 s operation budget. A live project over a limit is refused; measure
it with `--check-profile` first. A project with no `mail_checkpoints` table, no `approvals/queue.jsonl` or no
`public-packets.jsonl` is incomplete because the library requires the cursor and review_bundle roles.

## Attended backup: stop, audit, backup, restart (operator, weekly and before any engine re-pin)

`run-records.lock` is never quiescence: it covers only the run wrapper, while the arrival path, the daily mail, the console
and any `docker exec` session also write. `records quiescence-audit` checks all of them at one moment and writes the
`records-quiescence-v1` evidence `records backup` requires. It never calls `systemctl`; the operator captures unit states.
The evidence is an attestation the operator makes. The library does not authenticate it.

1. Stop every writer unit: `records-run.{service,timer}`, `records-arrival.{path,service}`,
   `deflockslo-daily-mail.{service,timer}`, `records-console.service`, `records-analysis.{service,timer}`. End any ad-hoc
   `docker exec` session.
2. Capture states (the same unit list as `quiescence_audit.WRITER_UNITS`) into a regular file:
   `systemctl --user show -p Id,ActiveState,SubState <units> > units.txt`. It is stale after 120 s.
3. Audit inside the records container, as the project owner (so every process is inspectable):
   `docker exec -i CONTAINER records quiescence-audit --project P --runtime R --units-file - --out E < units.txt`.
   Exit 0 prints `evidence_sha256=<hex>`; exit 1 prints the failing codes (`WRITER_ACTIVE`, `UNITS_INCOMPLETE`,
   `UNITS_STALE`, `OPEN_FILE`, `PROC_UNREADABLE`, `RUN_LOCK_HELD`, `ARRIVAL_PENDING`) and E records `false`. For
   `ARRIVAL_PENDING` drain it with one records run, then repeat from step 1. E must be outside P.
4. Within 120 s: `records backup --project P --into DIR --quiescence E --quiescence-sha256 <hex>`.
5. Restart the units and keep `DIR/backup-digests.jsonl` apart from the sets.
