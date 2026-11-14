# Records backup operations (backup_v2 profile and adapter)

`records backup` and `records backup-verify` wrap the `backup_v2` library with a reviewed profile of the records project
layout. Output is codes and counts only. The adapter writes nothing off-box, deletes nothing and never treats
`run-records.lock` as quiescence. The attended writer procedure that produces the evidence is added by DFGL-12B.

## Commands

- `records backup --check-profile --project P [--explain]` prints coverage counts as one JSON line (files per role,
  excluded per class, totals against the limits, `out_of_scope_legacy`, `referenced_outside_set`, `missing_roles`)
  followed by two plain lines, `out_of_scope_legacy: files N, bytes B` and `referenced_outside_set: N`. It exits 1 on
  `PROFILE_INCOMPLETE`, `LIMIT_ENTRIES`, `LIMIT_BYTES` or `REFERENCED_OUTSIDE_SET`. It writes nothing. `--explain` adds
  one line per unclassified family, `unclassified: <pattern> files N, bytes B`, and one per referenced path outside the
  set, `referenced_outside: <role> <pattern> refs N`. A pattern is the path relative to P with every hash-like, run,
  temporary or numeric component collapsed (`<sha>`, `<run>`, `<tmp>`, `<hh>`, `<n>`); a path off P shows only
  `<off-project>/` and its last three collapsed components. Names and counts only, never contents. This is how the next
  layout drift is seen.
- `records backup --project P --into DIR --quiescence FILE [--quiescence-sha256 HEX]` refuses with `QUIESCENCE_REFUSED`
  unless FILE is a fresh (at most 120 s) `records-quiescence-v1` document from `records quiescence-audit` with
  `basis=independent_all_writer_audit`, `all_writers_stopped=true` and `roots` equal to `[P]`. `--quiescence-sha256` is the
  digest the audit printed; a mismatch (tampered file) is refused. DIR must be outside P. A set is written to
  `DIR/set-<utc>` and `{set, manifest_sha256, evidence_sha256, at}` is appended to `DIR/backup-digests.jsonl` (0600).
- `records backup-verify --set DIR/set-... --digests FILE` verifies the set against the digest recorded for it. Keep a copy
  of `backup-digests.jsonl` apart from the sets; a list stored beside a tampered set proves nothing.

## Scope: the pipeline stores only

`records backup` covers the records pipeline stores only. The top-level entries of P it walks are `processing-v1`,
`portal-queue-v1`, `run-outputs`, `approvals`, `site-changes`, `public-packets.json`, `public-packets.jsonl`,
`public-packets.lock`, `public-packets.KILL`, `run-report.json`, `run.lock`, `.t12-sandbox-v1` and
`.adoption-config-live-v1.json`, plus any credential-named entry (walked so it is classified `credential`). Every other
top-level entry of P is the legacy tree (analysis-pipeline-*, operations, research-*, received*, productions, ...). It
is out of scope: never entered or opened by the backup, counted by `--check-profile` (`out_of_scope_legacy`, lstat
only) and recorded in each set's manifest as an `out_of_scope_legacy` exclusion. **The legacy tree is not in any
records backup set. It is covered by the operator's archive (tar) under E-030.**

A file a ledger row references is not legacy for this purpose. Every path the closure queries name (originals
`storage_path`, adapter receipts, intake preservations, portal versions, original resolutions) must be an included
store; any that is not (off P, in the legacy tree, or missing) is counted as `referenced_outside_set` and both
`--check-profile` and `records backup` refuse with `REFERENCED_OUTSIDE_SET`. Originals with no `storage_path` (legacy
declarations with no stored bytes) are no reference.

## Profile coverage (path relative to the project root)

| Path | Class |
| --- | --- |
| `processing-v1/ledger.sqlite` | canonical_ledger (+ cursor when `mail_checkpoints` exists) |
| `processing-v1/intake/intake.sqlite` | intake_ledger |
| `portal-queue-v1/portal.sqlite` | queue_store |
| `processing-v1/intake/blobs/<sha>`, `portal-queue-v1/objects/<sha>` | original_blob (name = content sha) |
| `processing-v1/intake/verification-receipts/<sha>`, `.../local-capture-receipts/<sha>`, `.../document-receipts/<sha>`, `processing-v1/mail/<sha>/receipt.json` | receipt |
| `processing-v1/intake/resolution-policies/<sha>` | resolution_policy |
| `approvals/queue.jsonl`, `public-packets.jsonl` | review_bundle |
| `public-packets.json`, `site-changes/**`, `processing-v1/proposals/**` | publication_store |
| `processing-v1/awaiting-ocr.jsonl`, `processing-v1/outbox/**` | queue_store |
| `processing-v1/mail/**`, `ocr/**` (page receipts, `current.json`, `indexes/`), `artifacts/evidence/*`, `context-v1/**`, `captured-inputs/<sha>.sqlite` | derived_resume |
| `processing-v1/.activation-intent-v1.json`, `.bound-trial-v1.json`, `portal-queue-v1/.canonical-binding-v1.json` | derived_resume (no writer in this package; kept by the include-unless-provably-not-needed rule) |
| `processing-v1/.adoption-disabled-v1.json`, `.adoption-config-live-v1.json` | configuration (mail adoption controls) |
| `processing-v1/backups/<run>/{ledger,intake}.sqlite[-wal,-shm,-journal]`, `manifest.json` | excluded: prior_backup (older per-run copies of the two included databases, written by `run_stages.backup_adoption_dbs`) |
| `processing-v1/extract/extract-<tmp>/**` | excluded: rebuildable (a fresh work dir per extract call; nothing opens it afterwards: enrollment already copied an accepted extraction's receipt and derivatives into `artifacts/evidence`, and a blocked one is redone by the next extract stage from the original blob and the OCR page receipts) |
| `run-outputs/**`, `run-report.json`, `processing-v1/runs/**` | excluded: rebuildable |
| `*.lock`, `.page.lock`, `public-packets.KILL`, sandbox marker | excluded: process_state |
| `*.sqlite-wal`, `-shm`, `-journal` of a declared database | excluded: sqlite_sidecar |
| `.runtime`, `.env*`, keys, tokens | excluded: credential |

Any other file in scope, any symlink or special file leaves the profile incomplete. Directories are declared (empty ones
included) through `Profile.directories`; the library still refuses an undeclared directory. Out-of-scope entries are
declared through `Profile.outside`; nothing declared may sit under one.

Closure queries: ledger `originals.storage_path` and `extraction_adapter_imports.receipt_sha256` (evidence file), the
intake `preservations`, portal `portal_versions`, and `original_resolutions` through the library's fixed reader. The
two journals have no database row, so the profile binds each with a constant closure row holding its digest at profile
build time. Supported schemas are pinned from the reviewed migration modules (ledger, intake and portal), never taken
from the source database: a missing, changed or extra table, trigger or index is refused as `schema_drift`, and optional
migration groups are accepted only exactly or absent. The schema hash handed to the library is computed over the
reviewed reference rows built from those modules, not over the source database's `sqlite_master`. A corrupt or
unreadable database is refused with the fixed reason `database_unreadable`.

## Limits (library constants; no flag lifts one)

Sized in BACKUP-FIT-1 so the live project (2026-11-13: 3,258 in-scope files, 731 directories, 429 MB included,
largest member 46 MB) fits with at least 5x headroom on every axis:

| Bound | Value | Refusal |
| --- | --- | --- |
| stored entries (files plus excluded files plus out-of-scope entries) | 20,000 (was 1,000) | `LIMIT_ENTRIES` |
| directory entries scanned | 80,000 (was 4,000) | `LIMIT_ENTRIES` (`scope_entry_bound` in the library) |
| total included bytes | 8 GiB (was 1 GiB) | `LIMIT_BYTES` (`total_size_bound`) |
| one member | 256 MiB (unchanged; the live maximum is under half of it) | `LIMIT_BYTES` (`file_size_bound`) |
| manifest | 16 MiB (was 1 MiB) | `manifest_size_bound` |
| operation budget | 600 s (was 60 s) | `operation_timeout` |
| references per database | 10,000 (unchanged) | `reference_count_bound` |

A live project over a limit is refused; measure it with `--check-profile` first. A project with no `mail_checkpoints`
table, no `approvals/queue.jsonl` or no `public-packets.jsonl` is incomplete (`reason: required_role_missing`, listed
in `missing_roles`) because the library requires the cursor and review_bundle roles.

## Attended backup: stop, audit, backup, restart (operator, weekly and before any engine re-pin)

`run-records.lock` is never quiescence: it covers only the run wrapper, while the arrival path, the daily mail, the console
and any `docker exec` session also write. `records quiescence-audit` checks all of them at one moment and writes the
`records-quiescence-v1` evidence `records backup` requires. It never calls `systemctl`; the operator captures unit states.
The evidence is an attestation the operator makes. The library does not authenticate it.

1. Stop every writer unit: `records-run.{service,timer}`, `records-arrival.{path,service}`,
   `deflockslo-daily-mail.{service,timer}`, `records-console.service`, `records-analysis.{service,timer}`. End any ad-hoc
   `docker exec` session.
2. Capture states (the same unit list as `quiescence_audit.WRITER_UNITS`) with a capture-time first line:
   `{ echo "Captured=$(date +%s)"; echo; systemctl --user show -p Id,ActiveState,SubState <units>; } > units.txt`.
   A capture without `Captured=` or older than 120 s is `UNITS_STALE`. A pipe has no file age, so the time travels in the data.
3. Audit inside the records container, as the project owner (so every process is inspectable):
   `docker exec -i CONTAINER records quiescence-audit --project P --runtime R --units-file - --out E < units.txt`.
   Exit 0 prints `evidence_sha256=<hex>`; exit 1 prints the failing codes (`WRITER_ACTIVE`, `UNITS_INCOMPLETE`,
   `UNITS_STALE`, `OPEN_FILE`, `PROC_UNREADABLE`, `RUN_LOCK_HELD`, `ARRIVAL_PENDING`) and E records `false`. For
   `ARRIVAL_PENDING` drain it with one records run, then repeat from step 1. E must be outside P.
4. Within 120 s: `records backup --project P --into DIR --quiescence E --quiescence-sha256 <hex>`.
5. Restart the units and keep `DIR/backup-digests.jsonl` apart from the sets.
