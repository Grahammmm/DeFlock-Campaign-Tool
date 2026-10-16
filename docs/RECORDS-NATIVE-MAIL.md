# Extended native mail contract (synthetic candidate, not activated)

This candidate extends merged PR57 without rewriting its scalar-only or RFC822
receipt families. Only scalar attached EML/MSG and received outer MSG select
`mail-wire-receipt-v2`. The immutable outer and captured byte payloads are
recomputed before importing a receipt. Existing PR50 related-root and body
ambiguity checks remain the authority in every EML message context.

## Originals versus derived embedded MSG items

A received outer MSG is one original. A byte-valued attached EML/MSG is another
received byte original with an immediate parent occurrence. An object-valued
embedded MSG is not a standalone received file. Its canonical occurrence uses
schema-supported kind `attachment`, identified by `native-msg-item`, and its
evidence explicitly says `derived_embedded_msg_item` and
`received_standalone_original: false`.

Each item binds the outer MSG SHA-256, exact OLE storage locator and a versioned,
exhaustive ordered stream manifest. Every listed stream length/hash and exact
bytes are independently checked against reparsed received OLE storage. Manifests
and stream artifacts live outside the originals CAS and never add unique
originals, preservation byte counts or standalone message hashes.

## Inventory and durable checkpoint boundary

The exporter seals original/capture bytes and derived proofs before writing a
complete receipt. Import securely stages every physical file, reparses the exact
outer bytes, verifies complete membership, metadata, immediate parents and
shared budgets, and checks every native manifest/stream before ledger mutation.
Existing transaction, lock, quarantine and rollback rules remain unchanged.
Canonical preservation requires replay verification and durable receipt
capture/promotion before the IMAP checkpoint advances. Checkpoint failure is
reported as failure, not successful preservation, and replay keeps original
counts unchanged.

Scalar EML transitions, RFC822 message roots, native object messages and
byte-valued MSG attachments share count, nesting, MIME/OLE directory and byte
ceilings. Repeated occurrences are charged separately, not deduplicated for
admission. Limits can only be lowered from 64 MiB source, 512 MiB cumulative,
1,000 parts/directories, 100 captures/items and depth 32. OLE stream read bytes
are conservatively charged, including repeated manifest/attachment views.

## Typed extraction provenance

Catalog locators use existing `part` and `mime` fields and remain bounded at
500 characters with no control bytes. No catalog validator is relaxed. The full
source-part and exact OLE storage/stream locator remain in unit
`data.wire_provenance`. Projection uniquely maps item/attachment units to an
already inventoried source part. Missing or ambiguous mappings fail closed.
Text and MSG properties are explicitly derived, not original byte evidence.
EML extraction no longer uses `EmailMessage.as_bytes()` to capture children.

The existing optional native parser runtime is used; nothing is installed on
the host. CI pins extract-msg/olefile for native-positive and tamper assertions.
Parser and inventory sources enter the runtime fingerprint. Existing runtime
profiles must be regenerated for the accepted code, which is not host-image
attestation or operational approval.

All fixtures are synthetic. Passing tests and CI are not independent legal
review, production-capacity acceptance or permission for real intake. Native
parser failures hold the message; no hidden attachment completeness is claimed.

## Canonical extraction acceptance

MSG writer metadata binds the exact serialized units artifact hash and installed
intake, extract-msg and olefile versions. Canonical acceptance hashes installed
native package source files and native inventory/receipt sources, then independently
reparses the immutable received original and compares every unit and physical child
hash. Only the MSG parser family is added to the existing reparse allowlist;
complete status, runtime identity, unit hashes and original preservation remain
mandatory. Existing canonical acceptance bounds (including the 8 MiB source bound)
remain in force and may hold otherwise inventory-valid larger messages.

Both EML and MSG callers intersect their configured expanded-byte allowance with
the 512 MiB wire ceiling. Lower caller allowances are retained, not raised.
Synthetic pipeline acceptance is not production activation, capacity acceptance,
host attestation or a claim that derived native items are standalone originals.
