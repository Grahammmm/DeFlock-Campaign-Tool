# Candidate catalog context links (M1)

The campaign_tool.records.catalog_links module accepts an explicit private
catalog snapshot and private agency/request registry. It writes candidate-only
context links; it never edits the catalog, verifies attribution, digests records,
invokes a model, or publishes anything.

    python3 -m campaign_tool.records.catalog_links \
      --snapshot /private/snapshot \
      --registry /private/registry.json \
      --output /private/catalog-links

The UTF-8 JSON registry has exactly these keys: schema_version (integer 1),
snapshot_id, catalog_sha256, agencies, requests, and links. Agencies have unique
typed agency IDs. Requests have a typed native request ID and an agency ID; the
agency + request ID pair is the identity, so the same native ID may appear in
different agencies. Links contain source_sha256, agency_id, and request_id.
Each link must point to a catalog card SHA, a known agency, and a request in
that agency. Identical file bytes may legitimately appear under several agencies
and request contexts; no agency is inferred from the hash alone.

The engine checks snapshot ID against its input manifest, catalog and manifest
bytes against artifact-hashes.json, registry snapshot/catalog bindings, exact
schemas, duplicate JSON keys/IDs/links, and source/foreign keys. A card with
excluded_unrelated_personal role cannot be linked; inconsistent excluded
role/status pairs are blocked. Unknown, malformed, excluded, or stale bindings
fail closed with fixed machine-readable codes. These links are contextual
candidates, not reviewed or verified agency attributions or catalog mutations.

A successful run creates an owner-only, immutable-by-contract directory named
by a SHA-256 run ID. candidates.json and receipt.json bind snapshot, manifest,
artifact inventory, registry, implementation, and result bytes. Accepted output
bytes are capped to the same bound used for reuse validation. New files and the
staging directory are synced before rename, then the output directory is synced.
New output-path directories use owner-only mode; the directory ancestor chain is
synced bottom-up on every attempt, including retries after partial setup. Reuse
validates exact published bytes, then syncs the destination and output directory
before returning success. A failed sync blocks success without changing a receipt.
Changed inputs create a new receipt; unchanged runs retain the existing identity.
Existing mismatched output, including a symlinked published receipt, is rejected
as existing_output_mismatch. The writer uses a local lock; output directories
and files must be owner-only, and symlink paths are rejected. Keep output on a
private local filesystem. Machine output includes only run ID, counts, reuse
flag, or a fixed blocked code, never private paths or source text.

This module does not infer links, reconcile an agency registry, update board
state, or promote candidates to verified findings. Operators must supply and
independently review the private registry before any later catalog merge.
