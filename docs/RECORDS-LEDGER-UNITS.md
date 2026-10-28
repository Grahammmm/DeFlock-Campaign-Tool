# Bounded retained-unit capture

This additive WP1 slice projects retained legacy_rows from a completed capture into
canonical units. It does not run an original parser, verify extraction against
original bytes, promote stages, resolve occurrence or agency joins, or complete WP1.
No original-level human review is required to preserve this observed evidence.

## Private invocation and resumption

Run only against an isolated, backed-up private candidate, outside public Git:

    python3 -m campaign_tool.records.ledger.unit_projection \
      --database "$PRIVATE_LEDGER" --import-id "$CAPTURE_ID" \
      --batch-size 1000 --max-batches 1

Reinvoke the same command until status is complete. Default invocation commits
one batch, not the entire 1,140,284-row backlog. A completed replay returns reused
without adding units, gaps or checkpoints. Output contains aggregate counts and
opaque hashes only. Text, locators and raw legacy payloads stay in the private DB.
Public tests use synthetic fixtures only.

## Evidence and semantics

Adapter version: retained-legacy-unit-capture-v1, additionally bound to the module
SHA-256 and additive migration checksum. This is an IMPORT adapter identity,
never an inferred original parser version. Canonical parser and parser_version
are NULL; status is captured_unverified. Every projected row creates an explicit
missing_parser_provenance gap owned by unit_capture_reconciliation.

The source capture ID, units table, source rowid and exact payload SHA-256 remain
bound by a foreign key to the immutable retained payload. For ordinary rows, the
adapter checks that payload hash and the captured (sha, ordinal) identity before
projection. observed_text is retained exactly, including whitespace, line endings,
empty strings and Unicode; text_sha256 hashes its UTF-8 encoding, not original
document bytes. No normalization or text-derived original hash is used.

The exact serialized locator is retained in both canonical and capture evidence.
It must decode to a nonempty JSON object with no duplicate keys/nonfinite values.
The adapter does NOT reinterpret its fields: a MIME traversal index remains an
index; a hierarchical path remains a path; neither is an established delivery or
attachment identity. This slice establishes no locator-to-original verification.
Other parser metadata embedded in legacy data remains raw evidence, not authority.

derived_path is an opaque ledger-unit-capture:captured-unit:... reference, NOT a
filesystem artifact. Resolve by units.id against unit_projection_rows.unit_id;
the observed text lives in that private table. Consumers requiring actual derived
files must not dereference it as a filesystem path.

IDs bind adapter/version, original SHA, legacy ordinal and exact payload hash.
Identical evidence in another capture reuses the canonical unit while retaining
a separate source-row association. Changed evidence creates another unit, never
overwrites an observation. Counts are projected source rows, not original counts,
verified parser successes or unique units across captures.

## Batches, checkpoints and bounds

The additive unit_projection_v1 schema has immutable runs, row evidence and
append-only chained checkpoints. Each transaction commits canonical units, gaps,
row associations and its checkpoint together. Failure rolls back that batch;
previous committed batches remain and resume by indexed source_rowid keyset,
not OFFSET. Batch size may change on resume.

Defaults: 1,000 rows, 2 MiB maximum individual payload and 8 MiB total payload per
batch. Hard row limit is 10,000; at most 1,000 batches may be requested per call.
The API permits smaller byte limits for controlled tests/operations. Byte limits
are bound into run identity so replay cannot hide a newly enlarged resource policy.
One indexed count checks declared units when starting a run; subsequent batches
read only bounded row metadata and payloads. There is no all-unit Python list.

Oversized payloads remain solely in their existing exact legacy_rows capture:
the projector does not fetch/decode/copy their contents and records
resource_row_too_large, integrity_checked=0 and no canonical unit. The stored
payload hash for such a gap is the capture's declaration, not a newly verified
hash. Invalid or absent text/locator/kind/ordinal/original produces explicit gaps.
Out-of-scope originals retain a scope-excluded association, not a canonical unit.
Hash or source identity mismatch aborts a batch rather than blessing corruption.

Missing parser provenance is a gap alongside an observed canonical unit, not a
reason to block the whole corpus. Scope exclusions and blocked rows are separate
from projected counts. No stage_state, stage_events, acceptance or occurrences
are updated. Publication remains false and end-to-end completion remains null.

## Synthetic scope and limitations

Tests cover exact text/hash/locator retention, explicit parser gaps, bounded bytes
and row batches, interruption rollback/resume, replay, malformed inputs, immutable
evidence and unchanged stage/acceptance state. Million-row performance and private
corpus execution are not claimed by this implementation; the parent owns the
private candidate run. No mail retrieval, detector completion, service changes,
public release, source deletion or deployment is performed by this module.

## Replay and resume binding protection

The additive unit_projection_guards_v1 migration protects canonical units referenced
by unit_projection_rows, and the retained source rows backing those associations.
BEFORE UPDATE, DELETE and INSERT guards reject mutation, deletion and replacement
even with recursive_triggers disabled. Existing evidence/checkpoint immutability
is retained. Indexed binding lookups avoid scanning all unit evidence on a write.

Before every batch, including a completed replay, the adapter checks the exact
extension table/trigger definitions and the binding guard/index definitions plus
their checksum marker. These are constant-sized schema checks; prior payloads are
not rescanned on replay or between batches. Source hash/binding verification and
canonical equality still occur when initially binding each row. Canonical units
cannot change after that binding while the verified guards remain installed.
The guard marker and original unit-extension marker cannot be replaced or deleted.

Existing pre-guard unit histories fail closed with
"unguarded unit history requires separate revalidation". They are NOT grandfathered,
silently restarted, erased or certified by installing triggers after the fact.
This includes the previously reported 1,000-row private batch. Preserve that
candidate; a clean isolated pre-unit-projection backup can receive the guarded
adapter, or a separately reviewed bounded migration/revalidation must be designed.
This repair performs neither private migration nor corpus processing.

Trust boundary: this is SQLite-enforced protection for ordinary SQL data writers,
with missing/changed schema protection rejected at every entry/batch. It does not
claim detection of an attacker who disables guards, changes data, and perfectly
restores the schema between calls, or replaces the entire database. Historical
adversarial DDL/file tampering needs a separate external attestation/revalidation
mechanism. No parser/extraction acceptance or stage promotion follows from guards.

## Guard v2: incoming collisions and relational append checks

The guard checksum/version now identifies unit_projection_guards_v2. Earlier
guarded candidates also require separate reviewed revalidation; neither v1 guards
nor unguarded runs are silently adopted. No existing evidence migration is rewritten.

UPDATE predicates test both OLD and NEW identities, including replacement
destinations, for canonical units, captured unit source rows and extension markers.
All units rows in an import become source-immutable when its projection run is
created, including its unprocessed suffix. Binding lookups have dedicated indexes.

Fresh run, association and checkpoint inserts require connection-local,
statement-specific append permits. Plain SQL connections do not have these
functions and fail closed. Permits are not an external security secret: their
role is preventing ordinary independent DML from masquerading as this adapter.
Associations additionally validate run/import/source hash and, for projected rows,
exact canonical fields, source payload hash, identity, observed text/hash, locator
and NULL-parser/captured-unverified provenance. Oversized gaps retain their declared
hash-only limitation. Foreign keys must be enabled in the author connection.

Each new checkpoint validates sequence, previous digest, exact source-row prefix,
association cursor, dispositions, counts, reason counts and full summary against
the actual just-appended associations and retained source IDs. The latest checkpoint
undergoes the same correspondence check before replay/resume. This reads at most
10,001 metadata rows per table for one batch, not all prior payloads or all prior
associations. A completed replay therefore uses bounded metadata checks rather than
the previous schema-only shortcut. Earlier immutable checkpoints establish the
previous prefix by induction; the empty first capture has a separate valid case.

These checks reject a self-consistently hashed false completion and false
disposition totals, as well as fresh mismatched association inserts. Deliberate
replacement of database files, dropping/restoring guards, or hostile replacement
of application callback implementations remains outside the ordinary-SQL threat
boundary. No reviewed-parser acceptance, stage promotion or corpus-run claim follows.
