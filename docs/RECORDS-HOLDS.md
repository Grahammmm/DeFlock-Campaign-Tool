# records holds (HOLD-INSPECT-1)

`records holds` lists every held extract document of one records project, one line per document,
with metadata only. It is read-only and clears nothing. A listing is review-required, not acceptance.

```
python3 -B -m campaign_tool.records holds --project PROJECT [--report RUN-REPORT.json] [--code CODE] [--json]
```

## What it reads

- `PROJECT/processing-v1/ledger.sqlite`, only through the engine's `run_stages.snapshot_ro`
  (`mode=ro&immutable=1`, `query_only`). A non-empty WAL or a rollback journal means a writer is
  active: the tool prints `LEDGER_UNREADABLE busy` and exits 2. A missing or unreadable ledger is
  `LEDGER_UNREADABLE missing|error`, exit 2. Run it between slots.
- A run's private outcome list `processing-v1/runs/extract-<run>.json` (hashes and codes, written by the
  extract stage). Without `--report` it is the newest one (run IDs sort by time; file names only are
  listed). With `--report` it is the list of that report's `run_id`, and the report's `needs_g` is read.

It never opens an original, an evidence payload, unit text, a mail receipt or a message. It prints no
path, file name, subject or address: each item is a 12-hex prefix of the original's sha256 plus fixed codes.

## Which documents are listed

The extract stage's own set: an in-scope original whose `extract` stage is `pending`
(`native_extract.pending_subjects`). For each one, in this order:

1. Unpreserved: `PRESERVE_PENDING` (the extract stage never parses it).
2. An open `extract:` alert that holds it under `native_extract`'s rule (not retryable, or retryable
   with `count >= MAX_ATTEMPTS`; an OCR-runtime-only alert is not treated as holding): that alert's code.
3. Otherwise, when the run outcome list codes it (not `EXTRACTED`): that code
   (for example an image `AWAITING_OCR`, `DEFERRED_RUN_BUDGET`, a retry the run deferred).
4. Otherwise an open retryable alert below `MAX_ATTEMPTS`: its code (it is retried on the next run).
5. Otherwise it is not held (parsed on the next run) and not listed.

## Item fields (`records-holds-v1`)

| field | meaning |
|---|---|
| `sha256_prefix` | first 12 hex of the original's sha256 |
| `code` | native_extract outcome code (`EXTRACT_ERROR`, `EXTRACT_NOT_ACCEPTED`, `PRESERVE_PENDING`, `AWAITING_OCR`, ...) |
| `attempts` | the alert's count: runs that recorded this failure (`MAX_ATTEMPTS` = 3 holds a retryable code); 0 without an alert |
| `first_run`, `last_run` | UTC times the alert was first and last recorded by a run; for `PRESERVE_PENDING` `first_run` is when the ledger first saw the original and `last_run` is null |
| `detail_code` | `EXTRACT_NOT_ACCEPTED`: the held refusal code (`held_refusal_detail`); other alerts: their issue codes; `PRESERVE_PENDING`: `preserve_<stage status>` |
| `preserved` | the original's `preserve` stage is done |
| `disposition` | `retryable` (EXTRACT_ERROR, EXTRACT_TIMEOUT, AWAITING_OCR, DEFERRED_RUN_BUDGET, PARSER_MISSING), `custodian` (PRESERVE_PENDING: the Flock coordinator preserves originals), `review_required` (every other code) |

`counts` always covers every listed document; `--code` filters `items` only. The text table prints the
same items with `age_days` (from `first_run`).

## Count reconciliation

For every extract code in `run_stages.EXTRACT_NEEDS` the item count must equal the baseline count
(outcome `PRESERVE_PENDING` is needs code `EXTRACT_PRESERVE_PENDING`). The baseline is the newest run
outcome list's per-code tally, which is exactly what that run's extract `needs_g` counts
(`run_stages.extract_result`); with `--report` it is the report's extract `needs_g`. Each difference
prints `COUNT_MISMATCH <code> <ledger> <report>`: a finding to record (for example on the HOLD-RELEASE
card), not an error; the exit stays 0. With `--json` the lines go to stderr and into `report.mismatches`
(`report.source` is `run_list` or `report`, `report.run_id` the run compared). With no outcome list the
tool prints `RUN_LIST_MISSING` and `NOT_RECONCILED` instead of a silent listing.

`ADOPTION_CODED_GAPS` are mail receipts the adopter has not enrolled, so the ledger has no row for
them and this tool cannot list them without opening mail. With `--report` their count is printed as
`NOT_IN_LEDGER ADOPTION_CODED_GAPS <n>`; the per-message view is `adopt-mail`'s dry run
(docs/RECORDS-MAIL-ADOPTION.md). An unreadable or malformed report prints `REPORT_UNREADABLE` and the
newest outcome list is used.

## Live use (read-only, between slots)

```
docker exec -u coder <flock container> env PYTHONPATH=<engine> /opt/campaign-tool/venv/bin/python -B \
  -m campaign_tool.records holds --project <records project> --json
```

Add `--report <run-report.json visible in the container>` for the adoption count. Record counts and
`COUNT_MISMATCH` lines only.

## Releasing retryable holds (HOLD-RELEASE-1)

```
python3 -B -m campaign_tool.records holds release --project PROJECT --code EXTRACT_ERROR|EXTRACT_TIMEOUT \
  [--max N] [--json]                                                     # dry run: lists, changes nothing
python3 -B -m campaign_tool.records holds release --project PROJECT --code EXTRACT_ERROR \
  --apply --max N --receipt DIR --report RUN-REPORT.json --runtime-lock RUNTIME_DIR/run-records.lock
```

`--runtime-lock` is required with `--apply`: it is the records wrapper's overlap lock, which the wrapper
holds across its preflight and every stage. The extract stage's own `processing-v1/run.lock` is held only
while extraction runs, so it alone cannot keep an apply out of an active run.

`native_extract` retries an `EXTRACT_ERROR` or `EXTRACT_TIMEOUT` document on later runs until its open
`extract:` alert has counted `MAX_ATTEMPTS` (3) runs; after that the document is held and counted every
run, never parsed again. `release` sets that alert's `count` back to 0. The alert stays open under the
same key, so the next run's extract stage parses the document again under its normal `--max-documents`
budget; a new failure counts again and the document holds itself after `MAX_ATTEMPTS` more runs (no
endless retry). Nothing is deleted, moved or re-extracted, and no other column or table changes.
(`tools/resolve_alerts.py` is the separate attended tool that resolves alerts under a custodian
contract; there a repeat failure reopens the alert with its retained count.)

Which documents: the listing's own set (in-scope, extract `pending`, preserved) whose every holding
alert classifies as `--code`. Release assumes OCR is off, as `native_extract` does by default, so an
OCR-runtime-only alert (`AWAITING_OCR`) counts as holding. A document that another code holds as well is
not released (a reset would not free it) and is counted as `held_by_other_code`. Order is the extract stage's own order;
`--max N` (1-25) takes the first N, so a second call releases the next ones.

Refusals (stderr, exit status; nothing is changed):

| code | exit | when |
|---|---|---|
| `CODE_CUSTODIAN` | 2 | `PRESERVE_PENDING`: the Flock coordinator preserves originals |
| `CODE_NOT_RETRYABLE` | 2 | any other code (`EXTRACT_NOT_ACCEPTED`, `CORRUPT_DOCUMENT`, `EXTRACT_INCOMPLETE`, ...): not retryable by design |
| `MAX_REQUIRED`, `RECEIPT_REQUIRED`, `REPORT_REQUIRED`, `RUNTIME_LOCK_REQUIRED` | 2 | `--apply` without them |
| `MAX_OUT_OF_RANGE` | 2 | `--max` outside 1-25 |
| `REPORT_UNREADABLE`, `REPORT_STALE`, `REPORT_NOT_LATEST` | 2 | the report is unreadable, its `run_id` is more than 24 h old (a release with no run after it retries nothing), or it is not the newest run with an extract outcome list |
| `RECEIPT_UNSAFE` | 2 | the receipt directory is inside the project, not a directory, not owned by the caller or not 0700-only |
| `RUNTIME_LOCK_UNREADABLE` | 2 | `--runtime-lock` names no openable regular file (it is never created; a symlink is refused) |
| `LEDGER_UNREADABLE missing\|error` | 2 | as for the listing |
| `LEDGER_BUSY` | 3 | `run-records.lock` or the extract stage's `processing-v1/run.lock` is held (or cannot be locked), or the ledger has a pending WAL or journal |
| `LEDGER_CHANGED` | 3 | an alert's count changed between the read and the write: the transaction is rolled back |
| `RELEASE_FAILED` | 1 | the write failed: rolled back |

The lock check sees locks only, not systemd: run it between slots. Both locks are taken first and held
from the report check through the receipt's publication, so no run starts during the apply and the
report checked is still the newest one when the counts are reset.

Receipt: one new file `DIR/holds-release-<UTC>-<rand>.jsonl` (0600) per apply that released anything,
one line per document: `{"schema": "records-holds-release-v1", "sha256_prefix", "code",
"attempts_before", "released_at", "run_report_sha256"}` (the sha256 of the report bytes that were
checked). It is written and fsynced before the single ledger transaction and published by rename
after its commit; if that rename fails the command prints `RECEIPT_NOT_PUBLISHED .<name>.tmp` (exit 1)
and the staged file is the receipt. No path, file name, subject or address is written or printed.

Rollback: a released document that fails again holds itself after `MAX_ATTEMPTS` runs; the receipt
lists every reset with its count before. No data changes, so there is nothing else to undo.

Live use [ATTENDED, operator, between slots]: dry run first and record the counts; then apply with
`--max 10`, a receipt directory owned by the container user, the newest run report and the wrapper's
`run-records.lock` (a path visible where the command runs). The next slot's
report shows `EXTRACT_ERROR` falling by the number that now extract; the rest come back as
`EXTRACT_ERROR` with internal codes for a diagnosis card. If a clear would need deleting or
re-extracting an original into another path, stop: that is a G data decision, not this command.
