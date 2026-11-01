# Records coverage report (one-time, counts only)

How much of the live records is readable, as five numbers an organizer can read
without seeing any document. The procedure uses only `tools/records_extract_summary.py`,
`tools/flock_facts.py`, `tools/portal_reconcile.py` and standard shell. It adds no tool,
no timer and no stage, and it prints no hash, path, file form or other private locator.

## Who runs it

Whoever may already read the live records root, read-only: the operator or the
live-data custodian. The procedure writes nothing under the records root or the fact
store; its only scratch files live in a fresh `mktemp -d` folder that step 3 removes.
**No number in this document was measured on live data by its author.** The
procedure was proven end to end on a synthetic records root by
`tests/test_records_coverage_report.py`; the live numbers are filled in by whoever
runs it.

## Procedure (run from a clone of this repository)

Set two shell variables first: `ROOT` (the live records root, the folder that
holds `runs/` and `blobs/`) and `STORE` (the fact store, the folder that holds
`agencies/<slug>/FACTS.csv`). Every step is **counts only**.

**Step 1, numbers (1)-(4). Counts only.** Summarise the newest extract outcome file:

```sh
python3 -B tools/records_extract_summary.py "$(ls -t "$ROOT"/runs/extract-*.json 2>/dev/null | head -n 1)"
```

It prints one JSON line. Read its `outcomes` object:

- (1) = `EXTRACTED`, (2) = `PRESERVE_PENDING`, (3) = `AWAITING_OCR`.
- (4) = every other code with a count above 0, as `CODE=N`, sorted by code, except
  `DEFERRED_RUN_BUDGET` (a run-budget deferral, not a hold). No such code: `none`.
- If the line has an `error` key (exit status 1; for example `not-json`,
  `wrong-schema`, or `not-a-regular-file` when there is no extract file), lines
  (1)-(4) are `not measured: <error>`. Never write 0 for a number that was not read.
- If `unknown_outcomes` is above 0, add it to line (4) as `unknown=N`.

**Step 2, fact store check. Counts only.** The store must pass its own check first:

```sh
python3 -B tools/flock_facts.py --check "$STORE"
```

It exits 0 and prints only an empty line when the store is valid. A non-zero exit
or any printed error (row errors name a row number and field, never a value) means
line (5) is `not measured: fact store has errors`, because a malformed `source_sha256`
would silently drop out of step 3.

**Step 3, number (5). Counts only.** Join fact citations to the live `blobs/`:

```sh
export LC_ALL=C
T=$(mktemp -d)
if [ -d "$ROOT/blobs" ]; then
  find "$STORE/agencies" -mindepth 2 -maxdepth 2 -name FACTS.csv -type f -exec cat {} + \
    | grep -oE '[0-9a-f]{64}' | sort > "$T/cited"
  ls "$ROOT/blobs" | sort -u > "$T/have"
  sort -u "$T/cited" | comm -23 - "$T/have" > "$T/missing"
  echo "facts_citing_missing=$(join "$T/cited" "$T/missing" | wc -l | tr -d ' ')"
  echo "distinct_missing_sha256=$(wc -l < "$T/missing" | tr -d ' ')"
else
  echo "blobs: not measured"
fi
rm -r "$T"
```

Blobs are content-addressed files named by their sha256 directly under `blobs/`;
`ls` skips the hidden `.pending-*` files of an unfinished write. `source_sha256` is
the only 64-hex value in a `FACTS.csv` row (`fact_id` is `F-` plus 12 hex), so each
cited line is one fact. The hashes stay in the scratch folder and are never printed.
If the output is `blobs: not measured`, line (5) is `not measured: no blobs folder`.

**Context line (optional). Counts only.** `tools/portal_reconcile.py --observations
OBS.json --queue-export EXPORT.json` counts portal pointers by class (`held_policy`,
`held_redirect`, `fetched`, `queued`, `failed`, `unproven`, ...). It describes what
the portal has or has not delivered, not what is in `blobs/`, and it needs inputs
this procedure does not make. Copy its class counts into the context line only when
the custodian already has both inputs; otherwise write `not measured`.

## Report template

Fill in the lines exactly; a number that was not read is `not measured: <reason>`.

```text
Records coverage, <date>, extract run <run_id> (counts only)
(1) live originals with extracted text: <N>
(2) PRESERVE_PENDING: <N>
(3) AWAITING_OCR: <N>
(4) held, by code: <CODE=N, CODE=N | none>
(5) stored facts citing a sha256 absent from live blobs/: <F> facts (<D> distinct sha256)
context, portal pointers (not blobs/): <class=N, ... | not measured>
```

## Reading guide

- Number (5) is the one that matters: a stored fact citing a document we do not
  hold cannot be re-checked against its original. It is a **count of facts**
  (`facts_citing_missing`, one per fact row, so one missing sha256 cited by two facts
  counts 2), with the **count of distinct missing sha256** (`distinct_missing_sha256`)
  beside it. With an empty `blobs/` every cited fact is missing; a store whose
  `FACTS.csv` files hold only the header gives 0.
- `flock_facts.py coverage` reports `orphan_facts` against `INVENTORY.csv`, not
  against `blobs/`; it is not number (5).
- (1) counts originals whose text the extract stage accepted. It is not a human
  review verdict on that text or on any fact drawn from it.
- (2) is waiting for preservation, (3) for OCR; (4) shows why the rest are held.
- The report is for the organizer. Publishing it is a separate decision.
