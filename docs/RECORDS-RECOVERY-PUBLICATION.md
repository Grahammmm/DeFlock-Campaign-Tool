# WP9 bounded recovery and publication outbox candidate

This slice provides selected-file backup/restore and an explicitly synthetic
publication rehearsal. It does not deploy, activate a runtime, send mail, run a
schedule, process the backlog, or establish production publication readiness.
No WP1/WP8 implementation is copied. These contracts are downstream interfaces.

## Recovery

`recovery.backup.backup(destination, entries)` takes explicit private entries with
`category`, `name`, and `source`. Categories are originals, ledger, rules, receipts;
one or more of each and exactly one SQLite ledger are required in this bounded
slice. Names are single safe filename components. All directories/files are created
exclusively; existing outputs and originals are never replaced. Output directories
are owner-only, finished files read-only. Every source path component is opened
without following symlinks, and regular-file metadata is checked during streaming.
Hashes are SHA-256; files stream in 1 MiB chunks. Limits: 1,000 selected files,
256 MiB per file, 1 GiB total, 1 MiB manifest. Exceeding a bound fails, never silently
omits an entry. This is not yet a whole-corpus backup service.

The ledger is copied using SQLite's online backup API, including committed WAL
state, with a 30-second bound and quick_check. Its snapshot bytes, not an unstable
hash of the live source file, are hashed. The original live database is not changed
by the backup API. Snapshot paths are private. The ledger/sidecar can retain SQLite
read-lock/WAL bookkeeping, but no application service is started or stopped.

The canonical JSON manifest is written last. Its exact SHA-256 is returned and must
be retained separately through a trusted owner/storage channel. A manifest alongside
a checksum invented by an attacker is not a trust anchor. `restore(source, into,
manifest_sha256=trusted_hash)` verifies that anchor, manifest identities, each streamed
file hash/size and isolated destination. Missing, modified, symlinked or oversized
files fail closed. No success receipt exists for an interrupted restore. Partial
outputs are retained for diagnosis; retry must use a fresh directory. Restore never
executes restored content, calls a site adapter, sends, deploys or activates jobs.
No original deletion or overwrite is involved.

Remaining recovery work: canonical WP1 referential reconciliation across a coherent
snapshot of all selected files, model/rule version inventory, encrypted/off-host
storage, retention and automated restore scheduling. Individual immutable files plus
a consistent SQLite snapshot are proven here; concurrent multi-file campaign-wide
transactional consistency is not. The manifest explicitly says selected scope.

## Approval and outbox authority

`PublicationOutbox.stage(runtime, proposal_id=..., content=bytes,
review_bundle=bytes, approval=dict, supersedes=None)` binds exact public-content and
review-bundle hashes, owner identity, installed profile and optional supersession.
Only the public content bytes can be staged as `site/<proposal_id>.md`. Review bundles,
private notes and approval data remain in private SQLite, not that site directory.
Content is capped at 1 MiB, review bundles at 256 KiB, approvals at 64 KiB.
An existing proposal cannot change; a correction is a new approved ID referencing
its predecessor. Both versions and append-only event history remain preserved.

Trusted installed startup calls `installed_runtime(owner=..., profile=...,
approval_verifier_id=..., site_adapter_id=...)`. It selects registered installed IDs,
not receipt-provided callbacks. The candidate ships no production verifier or site
adapter. Missing components leave a durable blocked job. There is no production
fallback accepting everything. An actual installed verifier must authenticate the
owner and independently validate WP8's exact reviewed/privacy-cleared bundle,
source/public-field restrictions and current approval status. Raw content inspection
or substantive review is not implemented by this outbox shell.

`testing_runtime(SyntheticApprovalAuthority(...))` creates only a test runtime.
Its local fixture signer authenticates exact bindings using an ephemeral key. It is
not a real owner's approval. Fake prepare/deploy/rollback receipts always include
`test_only=true`, `production=false` and synthetic references. A fake cannot be used
as a production adapter. No API or network is called. The fake simulates current
version, supersession and rollback to the prior version; it refuses an old rollback
that would replace a newer deployment. Its remote-site simulation is in-memory and
is not a persistent production service.

## Execution and recovery contract

`outbox.execute(runtime, proposal_id, action)` supports prepare, deploy, rollback in
that order. Claims and review are not promoted here. A nonblocking private writer
lock serializes operations. Approval, content and bundle bindings are rechecked.
An exact action idempotency key and pending action are committed before the adapter;
completion and receipt hash are recorded only after a matching result. Interrupted
acknowledgement retries the same action key. Production adapters must implement
remote lookup/idempotency, and refuse ambiguous unknown effects, before activation.
The fake exercises that contract without making production exactly-once claims.

Successful replay produces no duplicate fake effect. Prepare writes only the exact
approved public artifact; deploy refuses altered artifacts. Rollback records its
prior version and preserves all artifacts/history. Root CLI, real-site PR adapter,
release-specific deploy/rollback checks, WP1 publications projection, owner identity
backend and WP8 accepted-review registry remain integration work.

Daily ledger-generated reports, deduplicated operational alerts, map publication,
unattended mailbox demonstration and timer replacement are also outside this bounded
WP9 slice. No activation is authorized by green synthetic tests.

Tests: `python -B -m unittest tests.records.test_recovery -v`.

## WP8 bridge and replay hardening (supersedes the opaque-review shell above)

Trusted startup calls configure_wp8_review_gate(verifier_id, root=private_root,
authority=installed_wp8_authority) and selects that ID through
installed_runtime(..., review_verifier_id=...). The root and Authority cannot come
from a proposal, receipt or callback supplied by a visitor. No production owner
verifier, site adapter, key or runtime is included. Missing review gates block even
the synthetic runtime. Unit tests explicitly install a test-only stub; separate
composition tests use the actual WP8 implementation and synthetic signed authority.

gate.reference(bundle_id, owner_receipt_id=..., owner=...) creates a small exact
reference binding bundle, finding, public bytes, current review set, owner receipt
and installed authority policy hashes. Stage and every execute/replay freshly
assess WP8 evidence bytes, coverage, reviews, challenges and authenticated owner
decision. Both WP8's owner receipt and the separate outbox publication approval
must match. New challenges, changed evidence or extra reviews block old staged
work. Production additionally requires WP8 production review/handoff readiness;
synthetic authority can only rehearse with the fake adapter.

This bounded bridge pins WP8 private _root, _locked, _assess, _load,
_entries, decode and Authority contracts pending a public atomic handoff API.
It holds WP8's root lock across assessment and adapter/ack, preventing competing
WP8 review/owner appends. Evidence outside that root must remain immutable under
installed host policy; this does not establish a whole-corpus filesystem snapshot.
WP8 remains a separate read-only dependency; none of its code is copied here.

Actual WP8 public JSON bytes are stored unchanged in site/<proposal_id>.json.
Only legacy synthetic stub unit fixtures use .md. There is no JSON-to-Markdown
conversion, template rendering or claim that later rendered bytes were reviewed.
A future real site must consume this exact reviewed artifact or return its exact
rendered bytes through review and owner approval before deployment.

Prepare writes and fsyncs an exclusive private scratch file, exposes its complete
bytes with a no-overwrite atomic hard link, and fsyncs the site directory. Ordinary
failure removes only that invocation's scratch file; abrupt process death can
leave private scratch bytes but does not poison retry. Retry never overwrites an
existing committed artifact. Completed replay rechecks committed artifact bytes
and receipt action/key/content/review/test-mode bindings before returning success,
without calling the adapter again. Missing or altered artifacts fail closed.

The append-only history INSERT guard rejects INSERT OR REPLACE and REPLACE even
when recursive triggers are disabled. Backup/restore code and bounds are unchanged:
256 MiB per file excludes a multi-gigabyte real ledger; this is a selected-slice
recovery rehearsal, not a production corpus backup.

Tests: ordinary recovery unit suite plus tests.records.test_publication_review_bridge
with an explicitly supplied WP8 package/gates/test overlay. Set
REQUIRE_WP8_INTEGRATION=1 in the integration job: an absent dependency is then an
error, not skipped evidence. Production remote idempotency, restart-persistent
adapter receipts, installed identity/key lifecycle and independent acceptance
remain required. No live effects are authorized here.

## Explicit withdrawal authority and non-destructive holds

A review failure on prepare, deploy or replay sets blocked_reason but retains the
last successful prepared/deployed/rolled_back phase. Existing successful receipts
and append-only events remain intact. The returned operation result may be blocked
without rewriting the job's last successful phase. Legacy blocked projections are
reconstructed from successful events rather than forgetting a deployed version.

Rollback is a distinct withdrawal operation, NOT permission to make a disputed
claim anew. It does not require the disputed WP8 review set to remain valid.
It requires a separately configured trusted owner withdrawal verifier and an
explicit authorization argument on every execute(..., action='rollback',
rollback_authorization=...). Neither the original publication approval nor a
current review pass grants withdrawal permission. Absent verifier or authorization
blocks by default; no automated or real rollback is enabled.

rollback_target(runtime, proposal_id) resolves the historical successful deployment,
its receipt hash, exact deployed version and content/review hashes, target previous
version, and the previous approved proposal/content/review/receipt hashes. A target
of None explicitly means withdrawal to no prior deployment. Successful historical
receipt hashes must match append-only completion history. Unknown external prior
versions are blocked rather than assumed approved. The owner authorization binds
this entire target plus installed owner, profile and test mode. At execution it
is recomputed; altered target/version/content or an unrelated owner fails closed.
The site adapter must still refuse withdrawal if its live active version is newer.
A prepared artifact missing after deployment does not prevent withdrawal, since
historical exact approved job bytes and deployment receipts remain the authority.

The installed rollback registry is empty; trusted startup selects its ID with
installed_runtime(..., rollback_verifier_id=...). SyntheticRollbackAuthority is an
explicit test-only signer, separately passed to testing_runtime. It cannot grant
production permission or approve a deployment. Successful withdrawal receipts bind
the rollback target and authorization hashes and preserve historical evidence.
Adapters receive only public bytes and necessary identity/hash/target bindings,
never private review or owner-authorization payloads.

The actual-WP8 synthetic regression deploys two reviewed versions, adds a challenge
to the second, proves deploy replay and unauthorized rollback remain blocked while
deployed phase survives, and explicitly authorizes withdrawal to the exact prior
approved bytes. A separate test refuses stale withdrawal over a newer deployment.
All production identity, live-version comparison and durable remote-idempotency
contracts still need an installed independently reviewed adapter. No such adapter
or real credentials are included; no external operation occurs in these tests.
