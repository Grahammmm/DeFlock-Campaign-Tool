# Canonical records ledger candidate

The ledger preserves immutable legacy inputs and creates canonical original identities, occurrences, derived units, receipt evidence and seven separately accountable stages. Imported `complete` or `full` declarations never become newly verified review, legal, privacy or publication approval.

## Candidate commands

```sh
python -m campaign_tool.records.ledger init --database "$PRIVATE_CANDIDATE/ledger.sqlite"
python -m campaign_tool.records.ledger import-legacy \
  --snapshot "$PRIVATE_CAPTURED_SNAPSHOT" \
  --database "$PRIVATE_CANDIDATE/ledger.sqlite" --batch-size 1000
python -m campaign_tool.records.ledger counts --database "$PRIVATE_CANDIDATE/ledger.sqlite"
```

Use a new owner-only private directory outside all repositories. The captured snapshot requires `intake.sqlite` and its `input-manifest.json`, including the database hash. Live source databases with active journals are rejected. The commands do not access mail, portals, models or the network.

`import-legacy` initializes an absent database. `--max-batches 1` permits bounded resumable work. Captured rows, seeded originals, initial stage history and the checkpoint commit atomically. Source, importer and configuration identities are bound to the import ID. Repeating identical completed work adds no rows. Never edit checkpoints to disguise a failed or changed input.

Initialization commits a temporary database before publishing its final filename by a non-overwriting hard link. It cleans only its own temporary files. Existing ledgers are never overwritten. Writable connections enable WAL and recursive triggers. Database guards reject replacing immutable evidence or stage identities, including ordinary external SQL connections that disable recursive triggers.

## Implemented components

- Typed occurrence projection with exact identity and replay checks: `RECORDS-LEDGER-OCCURRENCES.md`.
- Bounded legacy-unit projection with explicit parser-provenance gaps: `RECORDS-LEDGER-UNITS.md`.
- Hash-bound receipt artifact import and source-line binding: `RECORDS-LEDGER-RECEIPTS.md`.
- Explicit mail, portal and digest inventory reconciliation: `RECORDS-LEDGER-RECONCILIATION.md`.
- Stage content revisions, prerequisite checks, worker leases, immutable receipts and invalidation: `RECORDS-LEDGER-STAGES.md`.

`counts` reports numeric status counts for preserve, extract, catalog, detect, review, compare and privacy, and distinguishes verified completion from unaccepted legacy declarations. A captured unit is not a reviewed unit; an artifact hash match is not proof of independent review. Stage acceptance must be performed by the configured trusted acceptance implementation, not by a caller-supplied assertion. Synthetic checks establish only the behavior of the tested implementation, not real-record acceptance.

## Remaining deployment gates

The private snapshot remains a candidate until its scope reconciliation, accepted adapters and independent checks are recorded. Unified runner, board, domain-specific acceptance, runtime activation, current CI and the owner merge are separate gates. This work package does not authorize a timer, host installation, portal access, publication, external send or source deletion. See the private deployment record for current test receipts and unresolved runtime checks; no private corpus counts, paths or hashes belong in this public document.
