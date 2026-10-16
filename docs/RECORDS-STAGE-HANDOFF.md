# Records stage handoff contract v1

This is a source-level workflow contract for the seven repository skills. It is
not a new executable schema, installed adapter, scheduler or Hermes dispatch.
Reference source: `9c7dca918d24d7e8cbefdb39dece66c6f63570e5`. Verify the exact
composed release before using a runtime. An API described as a repair seam below
is unavailable in that reference source until its separately reviewed change is
integrated and installed. Retain this document with the seven skill directories;
copying a skill alone breaks its relative reference.

Flow: capture → preservation → extraction → content review → correspondence →
applicability → publication. Correspondence can remain an explicit no-action
result when no evidence gap needs follow-up. New responses loop through capture;
changed evidence or rule context invalidates affected downstream work. This flow
describes responsibilities, not the order of the existing seven ledger stages.

## Common boundary

The private local evidence store and immutable ledger receipts are authoritative.
A task queue, dashboard, skill narrative or parent status is a projection. Preserve
original bytes, every occurrence, prior failed/rejected versions and supersession
links. Do not turn unresolved historical declarations into genuine deliveries.

Keep original identifiers/hashes, filenames, raw text, OCR, mail, locators,
correspondence, private paths, credentials and principal registries local. A cloud
handoff may contain reviewed generic code, contract versions, aggregate counts,
fixed failure codes and sanitized operational receipts. It must not embed the
private artifact references or source hashes merely because they look opaque.
Create a separate allowlisted public status projection; do not upload a local
handoff object. Exception narratives may expose evidence and are not public codes.

One assigned writer owns each root and operation. Honor the existing root lock and
private-directory guards. A copied canonical ledger does not isolate operations
whose callbacks write a separate intake store. Copy or block every mutating
dependency in diagnostic work; a blocked probe is not a successful replay verdict.
Never acquire a second schedule, credentials, grants, GPU worker or production
writer through a skill invocation.

## Versioned local input and result

Before dispatch, the adapter must bind these concepts to an implemented, validated
local serialization. They are required contract semantics, not arbitrary fields
to add to strict existing receipt schemas:

| Binding | Required meaning |
| --- | --- |
| Contract and stage | This contract v1, skill stage and exact supported adapter/schema versions |
| Code identity | Composed engine commit, component/dependency hashes and installed manifest; distinguish source candidate from installed runtime |
| Inputs | Immutable private artifact references, byte hashes, prior acceptance receipts, coverage denominator and scope, context/rule revisions when relevant |
| Operation | Stable local operation identity bound to exact inputs, adapter version, effective policy and requested action; changed bindings produce a new operation |
| Ownership and bounds | Assigned writer/run, root, deadline, attempt/resource budgets and cancellation state; no credential material |
| Result | Candidate artifacts, current acceptance receipt or explicit hold, completed/remaining counts, preserved gaps, replay or supersession evidence |
| Failure | Native fixed code, operational category, retry eligibility, attempts consumed and responsible owner; no private exception text in public projection |
| Parent handoff | Local receipt identifying the dispatched task and verified parent status adapter/version, or an explicit unverified mapping hold |

A produced artifact, enrolled candidate, functional acceptance, substantive review,
item approval and external completion are different outcomes. The installed
validator owns eligibility and acceptance; skills do not mint stage receipts or
select validators from receipt-controlled fields. A missing acceptance remains a
hold even when all source bytes or parser units exist.

Retries reuse the same bound operation and verify completed receipts first. Record
budgets before execution, consume failed attempts, stop on cancellation/deadline or
exhaustion and resume only through the existing controller. Deterministic binding,
unsupported-policy and authority failures require corrected inputs or reviewed
code/policy, not repeated execution. Transient I/O may retry within the verified
adapter's bounds. Ambiguous external completion requires reconciliation before
another attempt. Do not invent a retry adapter where none exists.

Operational categories for the parent projection are **integrity**, **unsupported**,
**authority**, **resource**, **transient**, **ambiguous external action** and
**review required**. These are reporting categories, not replacements for existing
error codes or an assertion that the runtime exposes these names.

## Capture

**Input v1:** approved acquisition scope plus an exact local-drop snapshot or mail
identity/checkpoint using the acquisition adapter's native versioned schema.
**Output v1:** immutable CAS bytes and genuine acquisition/occurrence receipts;
retrieval failures and unsupported material stay explicit. Hashes establish byte
identity, not authenticity or factual truth.

At the reference source, `intake.document_drop.snapshot(...)` and `capture(...)`
support bounded private flat PDF/XLSX drops. Snapshot limits are 200 entries,
64 MiB each and 256 MiB aggregate; read-time acquisition requires a read-only
source. Mail acquisition uses its own receipt and folder/UIDVALIDITY/UID binding.
Do not bypass those guards or rename formats to enter the local-drop route.
Existing checkpoints and native occurrence identities provide replay detection.
Changed snapshots require new validation. Access refusal, source drift and missing
bytes prevent preservation eligibility.

## Preservation

**Input v1:** capture receipts, original bytes, imported declaration state and
exact approved preservation policy. **Output v1:** verified preservation evidence
and a functional preserver acceptance, or an unresolved-binding/capture hold.
Legacy occurrence declarations remain declarations and retained receipts remain
historical evidence.

`CanonicalDocumentsBackend.preserve_document(...)` rejects conflicting existing
bindings in the reference source; it does not resolve imported originals.
`CanonicalMailBackend._validator` checks every current occurrence and can call
legacy preservation/mail import. Replaying old accepted receipts is therefore not
inherently read-only, and added unresolved declarations can prevent replay.

Repair seam under separate development (candidate APIs, not installed):

- `intake.local_capture.ApprovedCapturePolicy(raw: bytes, *, expected_sha256: str)`
  consumes a trusted-startup private artifact. Exact schema
  `approved-local-capture-policy-v1` binds `schema`, `inventory_sha256`,
  `approval_sha256` and `originals`; each original binds `bytes`, `role: original`,
  `scope: in_scope` and `evidence_sha256`. Its hash/schema checks do **not**
  authenticate owner approval. Independently establish that approval; a matching
  byte hash or receipt-controlled policy is insufficient.
- `CanonicalOriginalReconciler(mail_root, intake_output, database, *, policy)`
  uses inherited `start_run(identity)` / `finish_run(...)`.
  `enroll_local_capture(source_path, *, original_sha256)` holds the root lock,
  captures a genuine read-only private file, and appends a local occurrence plus
  initial immutable `original_resolutions` for eligible imported unresolved
  originals. Returned identifiers stay private; enrollment has zero stage
  promotions and no semantic acceptance.
- Explicit `accept(original_sha256)` invokes the fixed installed functional
  preserver. It does not fall back to mail replay or grant review authority.
  Original declarations, occurrences and receipts remain immutable. Existing
  accepted/cached originals cannot be resolved again; excluded scope cannot be
  converted. `store.effective_original` must serve every canonical reader, and
  preservation/extraction envelopes must bind the resolution hash.

The resolution binds original declaration, approved policy, capture occurrence and
receipt, storage, length, role and scope. Private `local-capture-v1` evidence binds
source path/stat/policy/subject; none belongs in cloud status. Replay checks exact
CAS, receipt and policy. Preserve `user_version=1`; verify the additive schema and
reader compatibility separately. Do not weaken legacy mail verification or
reinterpret prior receipts.

Candidate limits: streamed capture at most 512 MiB/file; policy at most 2 MiB and
10,000 originals. Owner-only, read-only, no-symlink/no-hardlink source and stable
pre/post fingerprints are required. This is a **per-call** API. Inherited pipeline
batch budget/cancellation has not yet been integrated: do not claim autonomous
bounded Hermes batch operation. The existing 10,000-original metadata cap is not
expanded. Source API tests do not authorize installation, policy creation or live
registry mutation; final PR/CI, exact installed composition and copy acceptance
remain release gates.

## Extraction

**Input v1:** accepted preservation, canonical bytes, qualified parser/OCR runtime
and exact bundle receipt hash. **Output v1:** units/artifacts/page or sheet coverage,
parser/runtime identity, explicit visual/unsupported gaps and functional extraction
receipt when the installed validator accepts.

Existing route version is `format-routes-1`. The supported acceptance sequence is
`ExtractionLedgerAdapter.enroll(original_path=..., receipt_path=...,
receipt_sha256=...)` → `ExtractionStageAdapter.accept(import_id)` under
`install_extraction_validator(...)` and trusted run/profile configuration.
Enrollment alone leaves candidates pending. Exact replay must preserve receipt
identity; changed bytes, artifacts, parser components or context require a new
qualified binding, not a patch to old receipts.

Enrollment and acceptance have different bounds. At the reference source,
enrollment limits units to 10,000 and aggregate evidence to 128 MiB; acceptance
limits source bytes to 8 MiB, evidence to 64 MiB and PDF pages to 256. Route limits
can be larger. Check all installed bounds before dispatch; a workbook's large row
count is not an acceptance waiver or semantic review. OCR evidence must bind page,
tool/version, derivative and text bytes; unreviewed OCR/visual queues retain holds.
Additional OCR/workbook compatibility modules are integration seams until their
reviewed source and exact installed callback pass copy tests.

Pillow image metadata has a missing units digest binding in the reference source.
Repair may enable candidate extraction/enrollment, but the installed complete
parser policy still refuses Pillow with `unsupported_complete_parser`. Test that
refusal after repairing the digest. Visual review and a separately approved image
acceptance policy remain required; do not weaken the validator or repair private
receipts in place.

## Content review

**Input v1:** original bytes, accepted extraction with scope/gaps, exact locators,
related evidence and proposed factual claims. **Output v1:** source-grounded digest,
counterevidence, reproduced material counts, authenticated factual/privacy reviews
of exact artifacts and explicit unresolved challenges.

`review_bundle.Authority`, `create_bundle`, `record_review`, `assess_bundle` and
`record_owner_decision` provide a trusted-startup WP8 library. Authenticated
principal roles, policy hashes and signed envelopes prevent some attribution and
binding errors; they do not prove that originals were inspected or that a first
pass was blind. `test_only=True` cannot supply production handoff readiness.
Do not create keys/principals or change that flag to clear a hold.

Ordinary ledger review declarations and model challenge are not this authority.
The installed genuine Authority and authenticated stage bridge remain unverified;
WP8's existing recovery publication bridge is not an ordinary review-stage bridge.
Bind subject/content/public artifacts, coverage, exact review set and policy through
a separately tested integration. Work may draft a digest while that seam is held,
but cannot report independently accepted review. Bound local inference and visual
work to the assigned budget; preserve image/weak OCR holds and partial scope.

## Correspondence

**Input v1:** reviewed evidence gap, request history, custodian/recipient evidence
and exact draft. **Output v1:** unsent versioned draft or approved outbox operation
with provider receipt; ambiguous sends and unresolved responses remain visible.

`campaign_tool.outbox.Outbox.propose`, `approve`, `send` and `reconcile` provide
journal/idempotency and agency/fee bounds. The identity key binds kind, request,
scope version and intent. Verify that the deployed transport is configured;
Workers executors can remain blocked. An approval declaration in a draft is not
external sending authorization. Require item approval and verified recipient.
Use existing caps; do not add recipients, providers or credentials to clear a hold.
New messages enter capture, not a fabricated response receipt. An explicit no-action
result preserves the reason and does not block unrelated qualified findings.

## Applicability

**Input v1:** reviewed proposition, event context, jurisdiction/role, primary rule
version, policy adoption/effective-date evidence and exceptions. **Output v1:**
evidence-to-duty comparison with exact revision bindings, bounded classification,
counterevidence and independent legal review or unresolved applicability hold.

The baseline comparison stage's concrete event-date and strict receipt schemas
are narrower than the intended contextual revision contract. Historical/unknown
dates and multiple duty events cannot be reduced to today's date or a single
unqualified policy. Context revision integration must bind outer content envelopes,
identify every changed-stage invalidation and revoke downstream approval; do not
add fields to strict old receipts. Preserve no-conflict and exculpatory outcomes.
Research gaps may create correspondence drafts. Legal review remains distinct from
functional comparison validation. Bound retrieval/research to approved sources and
budget; inaccessible rules or uncertain adoption remain explicit holds.

## Publication

**Input v1:** exact sanitized public bytes, manifest, current privacy/independent
review bindings and item-specific owner approval. **Output v1:** staged journaled
release and correction history, or separately verified live deployment receipt.

`publish.approve`, `publish.publish(..., staging=...)`, `publish.rollback` and
`publish.reopen` share the existing root lock; target locks and immutable journals
bind publication dependencies. Run the upgraded pipeline before publisher use to
establish cached-review retirement. Pending/malformed retirement holds refuse
approval, publication and rollback. Recovery aborts pending authority work rather
than replaying stale approval. Same text with changed dependencies needs renewed
approval. Engine staging is not website deployment; preserve the exact supported
deployment receipt and item authorization before reporting live completion.

## Hermes and parent PPP release gate

Parent PPP status schema/adapter is **unverified**. Do not invent its fields, map
all seven skills to seven existing ledger stages, or treat a Codex test as actual
Hermes dispatch. Before first dispatch, the integration owner must verify:

1. Exact composed engine/components, parser dependencies and installed manifest;
   frozen source/runtime differences must be reconciled, not overwritten.
2. Registry extension compatibility on isolated copies, with all mutating intake
   dependencies isolated; preserve originals, declarations and receipt history.
3. Active entrypoint, sole writer, bounded execution/cancellation and safe rollback
   that retains current publication guards. Runtime presence is not activation.
4. Existing authenticated authority/transport configuration where needed. New
   credentials, grants or policies require exact owner approval.
5. Parent adapter's actual schema/version, dispatched task identity and receipt
   mapping for eligible, candidate, accepted, held, failed and replayed outcomes.
   Those words describe required meanings here, not verified PPP field values.

Start with one qualified bounded stage, not an unattended full-corpus chain. A
ready capture stage needs genuine access and private acquisition storage; a ready
preservation stage needs reviewed reconciliation if its originals are imported.
Record actual Hermes dispatch, local execution and parent acknowledgement receipts.
Only after those match may the parent report a handoff. Until then report source
skills prepared and dispatch held. Do not duplicate the document owner's writer.

Current orchestration refuses aggregate counts above 10,000 originals. Internal
retirement tests above that size do not extend full-command support. Scope/budgets
for a 404-original inventory still require its exact runtime and stage eligibility;
404 identities do not imply 404 accepted captures, reviews or publications.
