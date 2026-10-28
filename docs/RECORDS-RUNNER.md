# WP2 preparatory runner slice

This is an offline representative slice, not an activated pipeline. The staged
mail-delta importer and its 26 synthetic tests are preserved unchanged. The
runner wraps a trusted injectable exporter boundary; it does not replace or run
the existing host exporter, read credentials, connect to IMAP, send mail or alter
a timer. Current `main` has no WP1 canonical preservation/promotion API. The
legacy bridge preserves bytes in the existing intake ledger, not the canonical
WP1 ledger. It always rejects canonical promotion with an explicit integration
gap. A synthetic promoting backend exists only in tests. No whole-pipeline or
canonical acceptance claim is made by passing these tests.

## Interfaces needed from WP1 and WP0

`Exporter.folders()` returns every exposed folder with current UIDVALIDITY;
`uids(scope, after_uid)` yields strictly increasing server UIDs; `receipt(scope,
uid)` returns a complete durable receipt from the existing exporter. The private
wrapper must require selection of the declared UIDVALIDITY, provider timeouts,
PEEK/non-mutating downloads, exact original bytes and a durable export receipt.
No raw record chooses a callable, executable, permission or runtime image.

`LedgerBackend.preserve(receipt_path, account, scope, uid) -> Preserved` must
atomically verify/store exact originals and message/attachment occurrences,
including account/folder/UIDVALIDITY/UID and one-based hierarchical MIME paths.
It must reject a changed binding under an existing identity. The existing exporter
uses zero-based full `message.walk()` indices; the preserved mail-delta bridge
verifies those against exact bytes and returns hierarchical locators. They are
not interchangeable. Same bytes in other folders/epochs remain new occurrences.

`LedgerBackend.validate_and_promote(StageReceipt, run_identity)` must validate
the appropriate WP1 receipt gates and atomically/idempotently apply per-item
promotion. A crash between backend commit and control-checkpoint commit retries
this call; receipt mismatch must fail closed. No filesystem control checkpoint
is itself accepted canonical completion. Stage hooks receive a subject hash and
run identity and return a hash-bound receipt, never publish or send.

WP0 must provide an independently verified runtime-identity adapter. This slice
pins exact code-input fingerprint/package version/config-byte hash plus an image
digest, but default image verification is explicitly
`caller_declared_not_host_verified`, not proof of the running image. Every run
has `canonical_integration_verified=false` and `pipeline_complete=false`.

## Control/recovery

An owner-only control root contains one nonblocking process lock and a WAL SQLite
runner journal. All profiles using the same ledger must share this root. The
journal is a new WP2 sidecar, not a copied WP1 schema. Each run records UUID,
version/code/image verification, exact config hash and schema identity. Mail
checkpoints, observed message identity and stage-work creation commit together;
checkpoint advancement follows successful preservation only. A failed UID stops
that folder before advancing past it; other folders continue. UIDVALIDITY reset
starts a new namespace at zero; originals still deduplicate by exact bytes.
Unconfigured and configured-but-unavailable folders are explicit alerts.

Stage state is attempted in order with durable leases/attempt counts, bounded
retry/backoff and per-item result commits. On acquiring the global lock, a new
run classifies previously running owners as interrupted and requeues their work.
File locking, not a timestamp or expiring lease alone, prevents a second writer.
Exception narratives and credential/environment values are not logged. Full
private events identify failures by opaque key and fixed safe category. The
bridge retains the legacy intake lock; old timer retirement and one writer across
host/container entry points require the exact activation diff later.

## Offline command

```sh
python3 -B -m campaign_tool.records.runner --profile /private/profile.json \
 --control-root /private/control --mail-root /private/frozen-export \
 --export-manifest export-manifest.json --intake-output /private/intake \
 --image-digest sha256:<pinned-image-digest>
```

Profile (owner-only file): version=1, account_id=opaque private namespace,
folders=list of explicit folders, stages=ordered subset of extract/catalog/detect/
review/compare/privacy, runtime={version,code_sha256,image_digest,
image_verification}. Maximum messages defaults to 1000 (hard ceiling 10000);
work defaults to 100 (ceiling1000), retries3 (ceiling10), delay60seconds,
lease300seconds. Use `observed_runtime` to prepare the offline identity; it does
not authorize activation. The offline manifest contains version1, complete=true,
folders[{name,uidvalidity,messages[{uid,receipt}]}]. No flags/read-state filters.
CLI stage hooks and the canonical adapter are unavailable, so preserved input
will be visibly blocked rather than falsely reported cataloged.

## Scheduler, activation and rollback

`records schedule render --root R --engine-root E [--python P] [--mail-config M] [--environment-file F] [--ocr]`
writes fully resolved user-systemd units and two scripts into `R/ops/`:

- `records-run.timer`: `OnCalendar` 07:00, 13:00 and 20:30 `America/Los_Angeles`, `Persistent=true`
  (a run missed while the host was down is made up at boot), validated with `systemd-analyze calendar`.
- `records-run.service`: `Type=oneshot`, `UMask=0077`, `NoNewPrivileges`, `PrivateTmp`; `ExecStart` runs
  `records run` and `ExecStartPost` runs `records health --record`.
- `activate.sh [--dry-run] [--retire legacy-mail.timer ...]`: records each retired timer's enabled/active
  state in `ops/activation-state.txt`, disables it, installs the two units, `daemon-reload`, enables the timer.
  One intake owner: retire the legacy exporter timer in the same step.
- `rollback.sh [--dry-run]`: disables and removes the new units and re-enables every retired timer that
  was enabled before. Originals, ledger and checkpoints are never touched.

Both scripts are tested against a fake `systemctl` (`tests/records/test_schedule_health.py`). Rendering is
not activation: running `activate.sh` without `--dry-run` on the host is the owner's one-time step and
requires explicit approval. Nothing in the engine enables a timer by itself.

`records health --root R [--now ISO] [--record] [--json]` derives health from the approved schedule and the
ledger's `runs` rows, not from file timestamps or an hours-since threshold: per due slot `ok`, `late` (inside
the 45-minute grace), `missed`, `failed`, `stalled` (started, never finalised) or `before_first_run`; overall
`ok`, `late`, `degraded` (latest fine, an earlier slot in the lookback missed), `missed`, `failed`, `stalled`
or `never`. Exit code 0 for `ok`/`late`, 1 otherwise, 2 when the root has no ledger. `--record` writes
keyed `schedule:<status>:<slot>` alerts so `records status` shows them.

The legacy `.service.in`/`.timer.in` templates under `campaign_tool/records/runner/templates/` remain as
the unresolved historical form; the rendered units above supersede them for operation.

## Test scope and remaining gates

Tests cover actual legacy byte preservation to an injected synthetic catalog
hook, replay, folder duplicates, UIDVALIDITY reset, dropped fetches, checkpoints,
one-process ownership, stale-process/lease recovery, retry bounds, pinned runtime,
redaction and receipt rejection. Tests never contact a provider. Pending: canonical
adapter, host runtime attestation, existing-exporter wrapper, credential loading,
folder verification CLI, exact unit diff, independent review/CI, activation and
unattended receipt-to-board acceptance. This is one WP2 candidate; no WP1 files
are included or edited and no branch is committed, pushed or merged here.

### Emerging WP1 stage bridge

`WP1PromotionBridge` optionally delegates to the separate
`campaign_tool.records.ledger.stages.promote` dependency. It requires trusted
validator/validator_id, canonical run-registration resolver and an exact
accepted-state checker. There is no accepting default. Trusted hooks must
prepare content/claims using the parent's `set_content`/`claim` API once its
contract is confirmed. A backend acknowledgment must pass the accepted-state
checker before the runner journals done. Missing dependency or unconfirmed
acceptance remains an integration gap. This branch does not include WP1 files.
No actual WP1 overlay integration was executed in this first slice; the parent
must supply canonical receipt/callback/claim shape and the runtime overlay.

### Bounded canonical preservation overlay result

An explicit trusted WP1 source overlay was exercised using synthetic originals
only. `CanonicalMailBackend` enrolls originals and typed mail/attachment occurrences
atomically, checks CAS hash/length, then calls set_content -> claim -> promote
with its installed-code preservation checker, never a receipt-selected callable
or a generic always-true validator. Already-done preservation replay rechecks CAS
and evidence rather than reopening the immutable stage. Referenced exporter
receipt hashes are attributed to occurrences; durable canonical storage of raw
export receipt snapshots and authenticated mailbox namespace remain integration
requirements, not independently proven by that reference alone. Portal outcomes
are not implemented: non-mail occurrences make this adapter refuse validation.
It conservatively refuses conflicting existing storage bindings.

The overlay tests verified two synthetic originals at preservation, pending
catalog with the hook invoked, numeric zero end-to-end completion, replay without
duplicate occurrences and corrupt-CAS refusal. Tests used the WP1 candidate before
its independent lease/validator-boundary repairs; they do NOT certify the repaired
API. The connector must move to the forthcoming trusted-profile validator registry
and transactional owner/run/expiry/revision lease contract and rerun this overlay.
No actual mailbox, historical original or active canonical ledger was touched.

4 MiB is the current WP1 stage-content ceiling. Larger preserved mail originals
remain explicitly blocked at this interface, even when legacy bytes are stored;
this can stop that folder checkpoint. Resolve this tradeoff with WP1 before any
activation. Canonical mail_messages projection, export-receipt CAS proof, backend
run-finalization and the installed live-exporter wrapper are still missing. The
optional adapter is not wired to the offline CLI or an enabled service.

## WP2 stable export-proof handoff (candidate, not activated)

`core.run(control_root, profile_path, exporter, backend, hooks, *,
runtime_provider, clock=time.time, attestation_verifier=None)` is the trusted
installed integration entry point. Neither records nor receipt JSON may choose
commands, stage handlers, attestors, validators or authority factories. The CLI
currently remains offline-manifest-only. A production launcher is a separate
installed integration requirement, not a command silently supplied by these templates.

The private profile is an owner-only JSON file (version 1, at most 256 KiB) with
`account_id`, explicit `folders`, ordered `stages`, budgets, `mode` and exact
`runtime`. Modes are `fixture`, `preparation`, `release`. Runtime records engine
version, SHA-256 of the runner's enumerated code inputs, declared image digest and
its declaration status. This code inventory is not a dependency/image attestation.
Configured command details live only in profile `export`. Never put credentials
in argv: use the unchanged exporter's private configuration or an explicitly
allowed environment variable. Reports discard command output and environment values.

### Export wrapper inputs and proof

Construct `ExporterWrapper(control_root, profile["export"], account_id)`.
The exact export specification must equal the one loaded from the private profile;
its canonical hash is bound to run identity and the immutable export manifest.
Direct trusted library callers must supply `export_spec_sha256=core.hid(spec)` in
run identity. This hash is not an authorization to execute any arbitrary record.

Required export specification:

- `argv`: absolute executable and explicit argument array, never a shell string.
- `argv_sha256`: canonical array hash; `command_inputs`: absolute executable/script
  paths mapped to exact SHA-256 hashes. Pin both the interpreter and exporter script.
- `root`: owner-private export root; `status_path`, `state_path`, `receipt_dir` are
  explicit paths contained by that root; no symlinks or traversal.
- `legacy_lock_path`: existing owner-private exporter lock, used only to freeze
  completed outputs after the exporter has exited. Never overlap another exporter.
- `indexes`: exactly `messages` and `attachments`; each has `path`, `format`
  (`csv` or `jsonl`) and explicit column-role `fields`. Both need `account_id`,
  `folder`, `uidvalidity`, `uid`, `sha256`; attachments also need `part`.
- `folder_fields`: explicit status-field mapping for `folder`, `uidvalidity`,
  `messages`. These are observed exported counts, not inferred filename identities.
- `attachment_part_encoding`: exactly `exporter_receipt_part`. Native part values
  bind the exporter index to receipts; MIME hierarchy is verified separately by
  the existing importer, not guessed by matching identical hashes.
- `timeout_seconds` (1..1800), `environment_names` (explicit allowlist, at most 64).

The wrapper runs only the trusted configured argv, with `shell=False`, stdin/stdout/
stderr discarded, a restricted named environment and an isolated process group.
Timeout kills/reaps that group. Script/executable hashes are checked before and
again after execution. The existing exporter implementation is not rewritten.
The included tests execute only a no-network local fixture script.

A successful process exit alone proves nothing. SYNC-STATUS must have an empty
failure list, timezone-bearing cutoff not older than this run's start, matching
last-success state and typed counts. Exact index identities and original hashes
must reconcile with complete native receipts and status counts. Historical receipts
outside current folder epochs are counted as excluded snapshot scope, not missing
originals. Missing UIDVALIDITY or an unresolved field mapping blocks coverage;
no heuristic or legacy assumption fills it in.

Before any checkpoint advances, exact status, state, indexes and all bounded
receipt bytes are written privately, fsynced and atomically renamed under
`control_root/exports/<manifest_sha256>/`. The manifest binds run/config/export-spec
hashes, exact file hashes/lengths, observed cutoff and counts. All published bytes
are reverified once; every pre-checkpoint check detects changed snapshot metadata
and rehashes the manifest and relevant receipt. CAS originals and receipt identities
must also match the backend outcome. Proof hash and cutoff are stored in run identity
before backend processing. A stale export cannot supply fresh coverage or advance UID.

### Persistence, retries and interruptions

An owner-private process lock protects the control sidecar writer. Mail identity
is account/folder/UIDVALIDITY/UID; MIME attachment identity retains hierarchical
part locators. Read flags do not alter checkpoints. A UIDVALIDITY change creates
an explicit epoch gap and re-enumerates from zero. One failed UID stops that folder
before its checkpoint while other configured folders can progress.

Message evidence, work seeding and folder checkpoint commit in one sidecar
transaction only after successful backend preservation and proof validation.
A backend commit followed by a control crash retries idempotently. Interrupted
runs and abandoned work leases are recovered under the writer lock. There is no
cross-database distributed transaction or claim that one was implemented.
Snapshot publication failure leaves no message checkpoint. Partial `.capture-*`
folders are retained for private diagnosis and never used as successful proof.
Reruns obtain fresh export proof; an old snapshot cannot silently masquerade as a
new mailbox sweep. Retention/disk quota and orphan disposition require the installed
host operation policy; this slice never deletes originals or schedules cleanup.

### WP1 and catalog integration

`CanonicalMailBackend(mail_root, intake_output, database,
stage_runner_factory=None)` optionally uses an explicitly pinned external WP1
installation. No WP1 files are copied into WP2. It enrolls exact originals and
native occurrences; duplicate frozen receipt paths retain the first immutable
locator after exact receipt re-verification. Its domain preservation check verifies
CAS bytes/hash/length and stored acquisition receipts. Portal occurrences are not
supported by this mail adapter and are refused, not certified.

Trusted installed startup must supply the factory `(database, run_identity,
installed_preservation_validator) -> configured WP1 StageRunner`. Production must
resolve a statically installed adapter registry/profile, then `installed_runner`.
No receipt may configure the factory or select a callback. WP2 trusted startup now installs only its actual mail preservation adapter in the
WP1 installed registry, with a backend-scoped ID and exact run/config profile.
Disabling this installed adapter without another trusted factory blocks acceptance. Fixture tests use
WP1 `testing_runner`, whose acceptance is explicitly synthetic and never counts as
verified end-to-end completion. The sequence is `runner.set_preservation_evidence`, `runner.claim`
then `runner.promote(..., claim_id=claim["claim_id"])`;  the WP1 controller owns
transactional run/owner/revision/expiry authority.

Installed stage hooks are typed `(subject_sha256, run_identity) -> StageReceipt`.
The backend must acknowledge actual domain acceptance before work becomes done.
The catalog hook exists, but a real catalog adapter is WP5 and is not supplied here.
Extraction, detectors, review, comparison, privacy and website approval are also
not implemented by this runner. Missing adapters remain visible gaps. Generic
lambda-true production validators are never provided. No successful slice is
reported as a full pipeline or a releasable deployment.

### Image attestation and remaining limits

`image_attestation` names a private artifact path, exact hash and installed
`verifier_id`. The optional trusted attestor receives exact artifact bytes, observed
runtime and verifier ID, and must independently verify the running image/code.
A JSON `verified=true` assertion is insufficient. Missing verification is reported
as declared-only/unverified, and release mode refuses execution. The fixture's
synthetic verifier is not a host attestation. Credentials and image activation are
not performed by this package.

Bounded resources: 100 folders; 10,000 candidate UID/row/directory entries; 2,000
receipt files per snapshot; 16 MiB per index; 64 MiB total captured input bytes;
receipt importer size/attachment limits are additionally enforced; 1,000 default
messages (maximum 10,000), 100 default work tasks (maximum 1,000), retry limit at
most 10, lease at most 3,600 seconds. Preservation uses a <=64 KiB evidence envelope rather than raw stage content,
so originals larger than 4 MiB are stream-verified without that stage-content cap.
The importer source-file limits remain in force. The proof is bounded per run, not
cumulative disk retention. Canonical lifecycle/native mail projection are implemented below. Remaining gates are
whole-environment pinning and a real host attestor remain integration requirements.

### Scheduler preparation and retirement/rollback

Templates only, never installable without replacing/reviewing every placeholder:
`runner/templates/records.service.in` and `records.timer.in`. The intended single
job runs at 07:00, 13:00 and 20:30 America/Los_Angeles. It must absorb the legacy
exporter, not run beside a second mailbox timer. Its trusted launcher and private
profile must pin the exporter command and the installed ledger/catalog adapters.

The parent must prepare an exact host-unit diff identifying the old timer/service,
current enable/active states, exporter lock and new unit paths. Preserve copies and
hashes of the old unit files, environment configuration and checkpoints. Before
activation, obtain owner approval of that exact diff, verify no old export is
running, stop/disable only the identified old timer, then install/enable the reviewed
new single timer. These are planned operations, not executed by this work package.

Rollback preparation: stop/disable the new timer first; wait for or safely resolve
its held writer/exporter locks; preserve its receipts, snapshots, sidecar and logs;
restore the exact old unit files and prior enable state only after the new writer
has stopped. Restore the old scheduling configuration, not old mail data. Keep
new receipts and reconcile UID epoch checkpoints before any resumed export. Never
run both writers, delete evidence or reset UIDVALIDITY to hide an acquisition gap.

### Superseding installed preservation contract

WP2's pinned startup registers its narrow real verifier under a backend-scoped
`wp2-mail-cas-preserver-v3` adapter ID, configures only the preserve stage profile,
and obtains `installed_runner`. It does not register generic true validators or
downstream stages. Optional fixture factories remain explicitly synthetic.

Before acceptance, the adapter streams the actual CAS original and checks hash,
length and stable file metadata. It replays the existing exact receipt/MIME binder
idempotently to verify account/folder/epoch/UID, mail occurrence identity, parent
message identity and the attachment's exact hierarchical part locator. Same-hash
attachments at different parts never prove the wrong occurrence. Verification
receipt bytes are preserved in a private append-only-by-identity CAS directory,
created exclusively, fsynced, made read-only, and reverified on every replay.
Changed originals, proof bytes, native identities or occurrence links reject.

`preservation-evidence-v1` separately binds the original hash/length/storage path,
immutable verification receipt hash and occurrence IDs. The stage content hash is
the evidence envelope hash, not the original or acquisition receipt hash. WP1
checks structural bindings and lease authority; this installed adapter checks
real filesystem and native acquisition evidence. A tested fixture contains an
actual attachment greater than 4 MiB, with no raw original stored as stage content.
Neither synthetic data nor domain preservation acceptance certifies review,
comparison, privacy, publication readiness or full pipeline completion.

## Frozen representative mail contract: wp2-mail-preservation-v1

This handoff freezes the current bounded mail interface. It does not enable the
scheduler, widen corpus scope or implement WP4/WP5 acceptance. Parent-declared
WP1 dependency: PR 22, head `4a356f8dd7e4a2ea64fa271ab07e1e48cce6ebe3`.
That declaration is not a claim that this candidate fetched or verified the Git
commit. Test evidence separately records hashes of the actual overlaid WP1 files.

### Required trusted caller inputs

1. Owner-private control root and profile file; fresh exact runner code pins;
   declared image digest with independently checked attestation before release.
2. Private export root and explicit bounded legacy command specification, including
   its input hashes, native index/status schemas, receipt directory and existing
   exporter lock. Existing exporter logic/configuration remains outside the engine.
3. Private intake/CAS output root and separately initialized canonical WP1 ledger.
   WP1 is an explicit installed dependency; nothing is copied into this candidate.
4. Static installed startup adapters. Default WP2 startup configures only its
   actual `wp2-mail-cas-preserver-v3` adapter. Fixture authority is always tagged
   by WP1 as synthetic. Records never select installed code or model handlers.

`ExporterWrapper.prepare(run_identity, started_at, clock)` returns a reconciled
`runner-export-proof-v1` descriptor with `proof_sha256`, `cutoff`,
`coverage_verified`, `snapshot_path`, `message_count`, `attachment_count` and
`index_sha256`. Exact snapshot manifest bytes bind the private configuration and
export specification hashes. Preparation must precede enumeration and checkpoints.
No older proof is silently promoted to fresh mailbox coverage.

`Folder(name, uidvalidity)` and `Preserved(message_id, eml_sha256,
receipt_sha256, documents, attachments)` remain the public typed boundary.
`documents` is the sorted unique original SHA-256 tuple, including the EML.
`attachments` retains every `(hierarchical MIME locator, original SHA-256)` pair;
identical bytes at different parts do not merge their occurrence identities.
`message_id` is the exact account/folder/UIDVALIDITY/UID occurrence identity.
`receipt_sha256` hashes the native acquisition receipt bytes, not the EML.

`CanonicalMailBackend.preserve(receipt_path, account, folder, uid)` requires
`start_run(run_identity)` first. `core.run` performs this startup automatically.
It preserves bytes, enrolls occurrences and obtains narrow preservation acceptance
only after domain verification. A failed UID never advances its folder checkpoint.
`core.run` returns `run_id` and scoped counts/gaps, not raw mail or credentials.
Only successful export proof contributes `verified_mail_cutoff` and
`export_proof_sha256`; fixture/offline enumeration without fresh proof cannot
claim a newly swept mailbox.

### Exact downstream handoff and stage separation

Parent may consume the `Preserved.documents` subject tuple in its trusted wrapper,
or query the private runner sidecar `runner_messages.evidence` for messages first
recorded under the scoped run ID. That query exposes original hashes and attachment
locators, not a statement that later stages completed. Retry/resume consumers must
also retain the original first-run message identity and the canonical stage queue;
a zero-message replay is not proof there is no unfinished work.

The accepted canonical `preserve` state supplies its `receipt_sha256`. The immutable
receipt binds `input_hashes.original`; its `content_sha256` hashes the small
preservation envelope. That envelope separately names `original_sha256`,
`verification_receipt_sha256`, storage reference, length and occurrence IDs.
Never substitute any one of these three hashes for another.

WP4 must validate provenance/extraction itself, configure its installed extractor
adapter and obtain its own authorized stage runner. The currently frozen WP2 run
profile binds only preservation and cannot be substituted mid-run to inject an
extractor. Use a distinct properly pinned WP4 canonical run/profile and bind its
extract receipt to the current original and preserve receipt hashes. This is an
explicit multi-run composition, not a claimed single cross-stage transaction.
A future unified installed startup profile must be separately implemented/tested;
receipt data cannot add adapter IDs to the current profile.

For WP5, the same rule applies: actual catalog content/validation belongs to its
installed adapter. WP2 `validate_and_promote` deliberately refuses downstream
promotion. A hook returning `StageReceipt` alone is insufficient. Parent must not
mark runner work done by replacing that refusal with a blanket success callback.
If its representative composition uses another validated backend, that backend
must check exact canonical acceptance and authority, not merely parse the receipt.
All default downstream slots stay pending until their real adapters accept them.

### Current scope and release limitations

The fixed bounds above remain unchanged. The real large fixture is 6,000,000 bytes;
no wider corpus or throughput claim follows from it. This slice has no fresh mail,
portal retrieval, external sends, timer activation, deployment or website update.
No full-pipeline completion is claimed. Required next integrations remain the
installed production launcher/attestation, explicit legacy schema compatibility,
WP4/WP5 domain adapters, independent
challenge and CI. The parent owns the exact retirement/activation diff.

## Additive lifecycle contract: wp2-mail-preservation-v1-lifecycle

Canonical `mail_messages` is now projected in the same explicit SQLite enrollment
transaction as originals, delivery/attachment occurrences and pending stage slots.
The row's account/folder/UIDVALIDITY/UID is the exact native identity checked against
the configured account scope. Its `account_provenance_status` is always
`configured_namespace_unattested`; neither provider nor mailbox ownership attestation
is asserted. The row does not infer identity from Message-ID or read/unread flags.

Headers come from the actual verified EML CAS object, with a 64 KiB header-block
bound and stable file metadata check. Ordered repeated decoded headers are preserved
privately as `verified-eml-headers-v1`, alongside the source original SHA and parser
defect classes. Raw header bytes remain in the exact EML original. A single nonblank
Message-ID may populate the convenience field; missing/ambiguous IDs remain null.
The receipt's headers cannot override the EML. A supported timezone-bearing native
export `internaldate` is labeled exporter-declared; otherwise received_at is null,
not fabricated or treated as provider-verified. Message-ID is bounded to 4 KiB.

On exact replay, every canonical message column and occurrence binding must agree.
Changed identity, hashes, headers or native mapping reject the canonical transaction
and prevent control checkpoint advancement. Mid-enrollment failure rolls back new
original rows, pending slots, occurrences and mail projection together. Already
preserved CAS/evidence bytes are intentionally retained as unaccepted evidence,
not deleted or certified. A finalized run may replay an existing unchanged delivery
for byte/domain verification, but cannot enroll a new delivery under that run.

Optional backend lifecycle methods consumed by the trusted core runner:

- `start_run(identity)`: creates or exactly reuses a live canonical run with pinned
  version/image/config/code/mode/export-proof and coverage-cutoff binding. It never
  silently reopens a completed or interrupted run.
- `finish_run(identity, status, summary)`: maps slice_completed to completed, retains
  completed_with_gaps, or records failed. It stores ended_at and the exact scoped
  outcome. Repeating an identical finalization is idempotent; changed bindings or
  outcomes reject. These statuses do not imply full pipeline or publication readiness.
- `recover_run(prior_identity)`: under the control writer lock only, closes a prior
  running canonical record as interrupted. It never overwrites an already finalized
  result. Missing prior canonical rows remain a visible recovery event.

Core finalizes the backend before recording the final control outcome. A crash
between these independent stores is recovered on the next owned run, never hidden
as a distributed atomic commit. Canonical acceptance and preserved bytes remain
replayable. No WP1 lease is forcibly removed; its owner/run/expiry authority remains
controlling. A finalization failure is recorded explicitly as pending rather than
being called success. Failed command/proof preparation does not create a new
canonical run; failures after successful backend startup finalize that scoped run.

WP4/WP5 still require distinct authorized installed stage profiles/runs as documented
above. Current mail and header bounds remain fixed. No actual mail or corpus run,
provider attestation, host activation, external send or publication occurred.
