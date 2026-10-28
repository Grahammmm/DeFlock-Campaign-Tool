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

The separate mail-export census/config slice is documented in
[RECORDS-MAIL-ADOPTION.md](RECORDS-MAIL-ADOPTION.md#staged-legacy-census-and-config-v2-s3a).
Its owner-only v2 config independently pins each historical subject's creating
mail policy; a resolution row cannot choose its own trust root. Census proposals
are not scope decisions or semantic acceptance. The local-capture policy,
Option B resolution table/schema pin, and generic fail-closed readers remain
unchanged. S3b adds the shared pure all-state planner and locked config/policy
reload to `records adopt-mail`: blocked originals or any inventory drift refuse
the whole apply before recovery, run creation, CAS/catalog or ledger writes.
Complete receipts and existing mail resolutions still participate in drift
checks. Held-only unresolved originals remain accepted gaps; rfc822 remains
unsupported. Binding/dependent chains run in deterministic topological order,
failed predecessors suppress all transitive dependents, and partial/postcommit
recovery reuses committed immutable bindings without another append.

For successor mail policies the original creating policy must be independently
subject-pinned in the owner-only config. Binding receipt, occurrence and legacy
binding remain fixed; only approved dependent coverage may grow. The stored row
keeps its creating policy hash. Reports expose counts, safe codes and baseline/
after pair digests, not original listings. Acceptance compares every baseline
K0 original row and every R0 resolution row unchanged, PLUS exact allowed new
original/resolution sets; whole-originals-table equality would reject legitimate
new EML/attachment insertions. See the T4B runbook for the separate inventory,
TEST-policy, revised-number and live-policy human gates. Synthetic green tests
never supply those decisions or semantic/legal acceptance.

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
