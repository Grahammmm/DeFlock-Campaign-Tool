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
- With `--report`: that run report's `run_id` and `needs_g`, and the same run's private outcome list
  `processing-v1/runs/extract-<run>.json` (hashes and codes, written by the extract stage).

It never opens an original, an evidence payload, unit text, a mail receipt or a message. It prints no
path, file name, subject or address: each item is a 12-hex prefix of the original's sha256 plus fixed codes.

## Which documents are listed

The extract stage's own set: an in-scope original whose `extract` stage is `pending`
(`native_extract.pending_subjects`). For each one, in this order:

1. Unpreserved: `PRESERVE_PENDING` (the extract stage never parses it).
2. An open `extract:` alert that holds it under `native_extract`'s rule (not retryable, or retryable
   with `count >= MAX_ATTEMPTS`; an OCR-runtime-only alert is not treated as holding): that alert's code.
3. Otherwise, when `--report` names a run whose outcome list codes it (not `EXTRACTED`): that code
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

## Report comparison (`--report`)

For every extract code in `run_stages.EXTRACT_NEEDS` the item count must equal the report's extract
`needs_g` count (outcome `PRESERVE_PENDING` is needs code `EXTRACT_PRESERVE_PENDING`). Each difference
prints `COUNT_MISMATCH <code> <ledger> <report>`: a finding to record (for example on the HOLD-RELEASE
card), not an error; the exit stays 0. With `--json` the lines go to stderr and into `report.mismatches`.

`ADOPTION_CODED_GAPS` are mail receipts the adopter has not enrolled, so the ledger has no row for
them and this tool cannot list them without opening mail. Their report count is printed as
`NOT_IN_LEDGER ADOPTION_CODED_GAPS <n>`; the per-message view is `adopt-mail`'s dry run
(docs/RECORDS-MAIL-ADOPTION.md). An unreadable or malformed report prints `REPORT_UNREADABLE`.

## Live use (read-only, between slots)

```
docker exec -u coder <flock container> env PYTHONPATH=<engine> /opt/campaign-tool/venv/bin/python -B \
  -m campaign_tool.records holds --project <records project> \
  --report <run-report.json path visible in the container> --json
```

Record counts and `COUNT_MISMATCH` lines only. Releasing retryable holds is a separate verb (HOLD-RELEASE-1).
