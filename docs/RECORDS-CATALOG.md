# Private records catalog import (M1)

The catalog is a private, metadata-only board. It preserves every unique document
identity in an existing intake database, including identities absent from an
older exported queue. A stored object is not necessarily an agency original.
Declared roles, extraction stages, original-level digestion, independent review,
legal/privacy review, and publication readiness remain separate.

## Import contract

Run `python -m campaign_tool.records.catalog` with explicitly supplied private
`--database`, `--queue`, `--snapshot`, `--digests-root`, and `--output` paths.
Optional `--support-root` preserves later receipt and ledger files without
adjudicating their claims. Optional `--blob-root` checks original bytes against
identity hashes and recorded sizes; without it, preservation is not checked.
The tool does not infer host paths, access email, retrieve portal files, invoke
models, send messages, publish, or change schedules. No credentials are needed.

The source database is read using SQLite's backup API into an owner-only output
snapshot. JSONL queue/snapshot inputs and supporting files are copied into a
SHA-256 content-addressed object directory. Inputs are never edited. Symlink
inputs are rejected. Output must be owner-only and outside captured input roots.
An exclusive writer lock prevents overlapping catalog imports.

Each snapshot includes:

- `input-manifest.json`: exact captured bytes, source provenance, and hashes.
- `catalog.json`: one card per database SHA-256 identity, with inherited claims
  explicitly marked as not revalidated.
- `inventory.jsonl`: complete identity input to the existing coverage reconciler.
- `COVERAGE-RECONCILIATION.json` and `DOCUMENT-REVIEW-QUEUE.jsonl`: the existing
  `reconcile_coverage.py` result for this same complete identity scope.
- `board.html`: self-contained private board with searchable cards and no external
  scripts, fonts, trackers, links to originals, or publication controls.
- `artifact-hashes.json`: generated artifact integrity inventory.

`CURRENT.json` points to the latest snapshot; previous snapshots are retained.
Never put the output tree, database backups, source documents, private agency
hints, review text, or real host configuration in the public repository.

## Meaning of the cards

Dates observed by intake are not document dates. An unavailable document date
remains unknown. A format/declared-role score is provisional triage metadata,
not substantive importance or a legal finding. Proposed low-value items remain
open. Snapshot claims are preserved verbatim as stage labels, not promoted into
new independent approvals. Later support files are preserved but their joins
still require reconciliation. No card is publication-ready through this import.

Counts explicitly separate the database inventory, active intake identities,
prior queue coverage, snapshot identities, declared roles, extraction stages,
digest coverage, and preservation outcomes. Missing identities and stale joins
are gaps, not evidence of completed review. This M1 importer is not OCR,
substantive digestion, an ALPR detector, legal review, or a publisher.

## Tests

`python -m unittest tests.records.test_catalog tests.records.test_catalog_board -v`
uses only synthetic records. The normal repository CI runs these along with the
existing intake and gate tests. Host package installation is not part of this
step. A private corpus import must be reported separately from synthetic tests.

Agency attribution is classified separately as `hint_present`, `unassigned`, or
`scope_excluded`. Empty/whitespace hints and the literal `UNASSIGNED` placeholder
(case-insensitive, ignoring surrounding whitespace) do not establish attribution.
Original hint strings are retained for provenance; a named hint is not independently
verified agency attribution. Excluded identities are not agency-assignment work.
