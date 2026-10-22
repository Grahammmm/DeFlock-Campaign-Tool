# Imported original reconciliation

This is an opt-in source API. It does not install a runtime, run a batch, create
reviewer authority or promote extraction. A matching byte hash establishes byte
identity; it does not establish role, scope, delivery or substantive review.

The installed application must supply `ApprovedCapturePolicy(raw,
expected_sha256=...)` at trusted startup from an **externally owner-approved**
private artifact. The class verifies the artifact hash and exact schema, not the
authenticity of owner approval. No record, callback submission or CLI selects
the policy. The expected policy hash and its inventory/approval/evidence
references must be verified through the existing owner authorization process.

The `approved-local-capture-policy-v1` artifact has exactly `schema`,
`inventory_sha256`, `approval_sha256`, and `originals`. Each original hash maps
to `{bytes, role: "original", scope: "in_scope", evidence_sha256}`. References
and raw identities stay in local private storage. No policy artifact, captured
source path, original hash, correspondence or raw source belongs in cloud prompts
or GitHub. Parent reports carry source versions, counts and fixed hold reasons.

## Enrollment and acceptance

Construct `CanonicalOriginalReconciler(mail_root, intake_output, database,
policy=...)` using the existing pinned startup identity and lifecycle:

1. `start_run(identity)` binds the existing canonical run.
2. `enroll_local_capture(source_path, original_sha256=...)` captures a selected
   owner-only, single-link, nonsymlink file from a read-only filesystem. It
   streams at most the existing 512 MiB source bound, rechecks exact source stat
   identity, and seals byte-identical CAS plus immutable policy/capture receipts.
3. Enrollment appends a local occurrence. An imported unresolved original may
   receive one evidence-bound `original_resolutions` row; the imported original,
   legacy occurrences, gaps, stage events and old receipts are unchanged.
   Enrollment reports `stage_promotions: 0`, `semantic_acceptance: false`.
4. `accept(original_sha256)` is a separate explicit callback. It validates the
   approved policy, exact capture/CAS and declaration bindings with the fixed
   installed functional preserver, then uses the existing protected controller.
   Only preservation can complete. No mail replay, model, parser or review
   fallback is available through this callback.
5. `finish_run(identity, "slice_completed" | "completed_with_gaps" | "failed",
   summary)` uses the existing bounded lifecycle. Batch orchestration must supply
   its existing time/cancellation/original budgets; this per-original API does
   not create an autonomous scheduler or claim an integrated batch budget.

Calls acquire the same canonical root lock as pipeline and publication authority
operations. Exact enrollment and acceptance replay keep their identities and
immutable history. The source/output must be separate and outside Git. Current
local capture has no inferred received time or mail namespace.

## Initial resolution contract

The additive `original_resolution_v1` extension leaves bootstrap schema checksum
and `user_version = 1` unchanged. Each immutable binding hashes the original
declaration, approved policy, capture occurrence/receipt, byte length, role,
scope and fixed canonical CAS path. All canonical consumers read
`store.effective_original`; selected-column exports retain their existing field
boundary. Preservation evidence and extraction manifest/content envelopes bind
the resolution hash, which propagates through existing receipt dependencies.

Only captured imported originals with unresolved role/scope, no storage binding,
seven untouched pending stages, no stage content cache and no proposal receipt
are eligible. An existing bound pending original may enroll genuine local
capture without a resolution. Scope exclusions and other established bindings
cannot be overridden. No replacement/rebinding or silent receipt interpretation
is implemented. A future correction requires an explicit reviewed supersession
contract, approval revocation and controlled dependent invalidation.

A second payload schema, `original_resolution_mail_export_v1`, resolves a legacy
unresolved original from verified mail-export evidence under an owner-approved
mail-resolution policy (docs/RECORDS-LEGACY-MAIL-RESOLUTION.md). It reuses the
same table, triggers and schema hash. The canonical mail backend writes it inside
its own transaction, and `records adopt-mail` plans it; the local-capture path
above is unchanged. An older reader fails loudly with
`original_resolution_payload_mismatch` on a mail payload.

Captured legacy declarations are checked against their immutable released
projection/input bindings. They retain their unresolved provenance and cannot
satisfy the acquisition set of accepted preservation. Historical preservation
replay uses its original stored evidence and exact acquisition set, not a larger
set constructed after projection. The legacy **mail** verifier still invokes
its mutating intake-import path; a copied canonical ledger alone is insufficient
to isolate that replay. Copy the complete intake/CAS/write closure or report a
guarded diagnostic stop, never a completed replay verdict.

## Holds, resources and failure recovery

Policy input is bounded to 2 MiB/10,000 originals; this does not expand the
existing 10,000 metadata limit. Each file is streamed; missing/changed capture,
policy, CAS, declaration or effective binding fails closed. Unsupported/excluded
scope, unknown inventory, writable or linked source, conflicting resolution,
cached/accepted original and concurrent root writer remain held.

Filesystem artifacts commit before the SQLite binding transaction. Interrupted
enrollment may leave harmless hash-addressed orphan bytes but no accepted stage;
retry reuses exact bytes. Known validation failures are checked before claiming
preservation and are rechecked by the installed controller before acceptance.
Faults after a claim follow existing lease/run recovery; never fabricate a pass
receipt or clear a hold by editing SQLite.

Backups and release composition must include the exact new schema pin,
`local-capture-receipts`, `resolution-policies`, original blobs and all captured
legacy input dependencies. Existing backup receipts retain their original pins.
Restore/copy acceptance must verify the entire closure before activation.

## Qualification

Synthetic tests simulate the read-only mount state and exercise actual fixed
capture/preservation validation, unchanged history, negative cases and exact
replay. Linux tests additionally run real bounded native-text extraction through
the installed functional callback. These prove software contracts, not genuine
campaign capture, substantive review or Hermes dispatch.

The frozen operational extraction modules differ from the public candidate and
depend on OCR/workbook modules absent from the baseline. Reconcile those sources
and exact parser policy first; literal-redacted diagnostics are not executable
patches. Activation also needs verified installed manifest, active entrypoint,
safe compatible rollback retaining retirement/publication guards, current
backup closure and exact owner approval. No activation follows from these APIs.
