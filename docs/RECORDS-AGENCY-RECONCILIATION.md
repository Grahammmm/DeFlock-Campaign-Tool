# Provenance-bound agency candidates

`python -m campaign_tool.records.agency_reconciliation --snapshot PRIVATE_SNAPSHOT --output PRIVATE_OUTPUT`

This independent M1 step consumes a version-1 private catalog snapshot. It does
not need the catalog implementation installed, so it can be reviewed separately.
It verifies catalog/manifest bindings and captured digest-object bytes before
using the minimal metadata fields: document hash, agency label, object hash and
physical JSONL line. No narrative record text is retained in its outputs or sent
to a model. No network access, extraction, OCR or mailbox intake occurs.

Targets are unassigned agency originals and container children. Each result item
in `cards` retains separate `exact_hash_candidates`, `parent_hints`, and
`parent_digest_candidates`. Parent digest candidates come only from digest records
matched to an immediate, present, non-excluded parent; they do not traverse its
parents or inherit its reconciliation results. An item may have all three sources.
The canonical conflict union includes all three sources. Summary fields
`exact_hash_candidate_items`, `parent_hint_items`, and
`parent_digest_candidate_items` count targets with at least one candidate of the
respective source, not individual candidates. Existing `either_source_items`
counts targets with any of the three sources. Counts must not be added without
accounting for overlap. Duplicate parent references are deduplicated. Missing or
excluded parents remain unresolved. Digests that do not join the catalog are
reported separately; exclusions are not reopened.

Whitespace/case normalization does not establish verified identity. Unknown and
`UNASSIGNED` labels do not establish attribution. Optional `--aliases` accepts an
explicit private JSON mapping of agency labels to canonical labels. Do not create
aliases automatically to eliminate conflicts. Keep actual campaign aliases and
policies outside this public repository. Alias chains must be explicitly flattened.

Outputs are immutable owner-only run directories containing `candidates.json` and
`receipt.json`. Identity binds input hashes, the implementation and exact result.
Identical reruns reuse and validate the result; modified input creates a new run.
The source catalog, original documents and historical review claims are unchanged.
Use a new private output root for a corrected run; preserve historical outputs,
including any with insecure permissions, rather than repairing them in place.

States mean:

- `no_evidence`: no exact-hash candidate, parent hint or parent digest candidate was available.
- `candidate_only`: one canonical label, still not verified attribution.
- `conflicting_candidates`: more than one canonical label; could reflect naming,
  shared records, mixed containers or a real provenance problem. Not a legal finding.

Every candidate includes exact source locators. Parent hints identify the parent
hash; the hash-bound catalog supplies its metadata provenance. Parent digest
candidates retain `agency`, `canonical_agency`, `parent_sha256`, and
`source: {object_sha256, line}` identifying the captured digest object and exact
physical JSONL line. All results have
`verified: false` and `publication_ready: false`. Independent examination and an
approved application step remain necessary before changing a catalog assignment.
This tool never changes schedules or input permissions, sends messages or publishes.

Supported placeholder labels are blank text, `UNASSIGNED`, and `Unknown`, after
case/whitespace normalization. Other labels are not silently discarded. Canonical
comparison labels are casefolded; original labels are preserved separately.
Unmatched entries retain normalized source locators; excluded entries retain only
identity and locator, not agency labels. Reuse rechecks ownership and owner-only
permissions on the output root, run directory, result, receipt and writer lock.
New result and receipt file descriptors are explicitly set to mode `0600` before
writing any bytes, independently of umask or inherited creation permissions.
New writer locks are created exclusively and their descriptors are also set to
`0600`. Existing locks are opened without truncation or chmod and validated by
both descriptor and path before use; insecure locks are rejected unchanged.
Run directories remain owner-only (`0700`).
