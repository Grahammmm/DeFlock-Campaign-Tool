# Synthetic release acceptance packet and isolated restore plan

This is a planning-only companion to [issue 7](https://github.com/Grahammmm/DeFlock-Campaign-Tool/issues/7).
[release-acceptance-plan.example.json](release-acceptance-plan.example.json) pins
main 28e9d198 and draft PR96/97/98 independently. Those drafts are not one combined
release; no wheel, active selector, trusted manifest or rollback identity is invented.
All live evidence digests are null and activation remains held.

## Example placeholders and observed provenance

The JSON is a planning template: its descriptive provenance placeholders are
not executable release evidence. Exact observed pins are preserved here. Replace
placeholders only after independently verifying the intended source; this
sample never grants activation or authenticates the unavailable review kit.

| Reference | Observed commit |
| --- | --- |
| Main used for the synthetic rehearsals | `28e9d19854ee56039a52289e6a8ded437e0ce5cc` |
| PR96 draft | `b4c2fa2fe04c2b8ace540d5345b8877656dbb3ab` |
| PR97 draft | `f32ada2b8638d4be0d52f853b282513dac10335b` |
| PR98 draft, stacked on PR96 | `7bd1833b62651c142cf2b63857e1e7638092db65` |
| Documented standalone kit candidate; source unavailable | `ef87da12170e358a9168ce7f2cc83674661a47d3` |

## Reuse and verified scope

Reuse the issue's existing offline review-kit checklist: pinned source, required
CI completeness, independent review, artifact/target binding, evidence digests,
held states and explicit rollback identity. Candidate ef87da1 was not found in
the available object stores/checkouts, so its collector and historical 79-test
receipt are not claimed executed here. No replacement collector or release
authority is introduced. Use the existing [release manifest](RECORDS-RELEASE.md),
[v2 recovery](RECORDS_BACKUP_V2.md), publication guards and their tests.

At main 28e9d198, thirty existing tests passed with zero skips: 23 release/restore
cases and seven publication rollback/history cases. These are synthetic temporary
roots, not a live backup or runtime rollback qualification. Commands are recorded
below. The JSON example records the observed CI counts separately; CI does not
authenticate independent review, prove deployed applicability or grant activation.

## Ordered live rehearsal plan — NOT EXECUTED

1. **Select a reviewed candidate.** Pin the actual combined successor commit,
   required-check set/run identities, independent review receipts, lock hashes,
   wheel digest, installed manifest and target identity. Compare installed package
   inventory outside the checkout. Keep dirty/untagged state truthful. No tag or
   active runtime is changed by this plan.
2. **Close the operational profile before capture.** Enumerate every store/CAS/
   policy dependency used by the bounded operation. Include canonical/intake
   ledgers, original blobs, capture/resolution receipts and policies, embedded
   stage artifacts/metadata, actual resume cursors, review evidence, proposal
   revisions and publication journals. If PR96/97 are adopted, map context
   artifact pointers, claim/migration evidence and retained result envelopes.
   Bind exact schema hashes and a truthful role-to-file/embedded-payload mapping.
   A zero-row role is not a license to create a placeholder file or constant
   SELECT reference. Generic v2 requires two distinct databases and its required
   roles; unsupported operation-specific closure needs a reviewed adapter.
   Unknown stores or bounds/permission/symlink failures block; do not raise caps
   or sweep the legacy tree merely to make a profile pass.
3. **Coordinate and prove all-writer quiescence.** Inventory each writer's owner,
   executable/entrypoint, scheduler/service/timer/cron source, process identity,
   open write descriptors and mapped store scope. Cover manual pipelines,
   unattended jobs, intake/capture, publication/outbox, SQLite/WAL writers,
   hosted/host/container processes and host bind/namespace mappings. Obtain a
   stop acknowledgment for every writer and independently observe the complete
   interval. Zero accessible write FDs or the root lock alone is insufficient.
   Any denied FD directory, inaccessible scheduler or unmapped namespace keeps
   the gate held; do not acquire new permissions as an automatic workaround.
   The caller evidence must cover exactly the declared roots, bind its independent
   receipt digest and have a UTC observation no older than 120 seconds. Maintain
   that boundary through every snapshot; expiry or resumed writers abort capture.
4. **Verify genuine read-only source access.** Confirm the source filesystem/view
   is read-only in the capture UID's namespace and reconcile host bind mappings.
   chmod 0400 alone is not a read-only filesystem. Do not alter originals,
   permissions or mounts under this planning authorization.
5. **Capture and retain evidence only after authorization and gates.** Use new
   owner-private destinations outside active/source/profile roots. Snapshot every
   mutating dependency within the same proven interval. Retain a separately
   trusted manifest digest, source/role inventory and historical receipts.
   Preserve originals/occurrences, test-only markers, retirement pending state,
   revoked approvals, journals and provenance. Never reinterpret old receipts
   as genuine preservation, independent review or current publication authority.
6. **Verify and restore in isolation.** Verify the exact manifest pin, inventory,
   schema, foreign keys, integrity and database-bound references; restore into a
   new isolated private root, never the source or active root. Recheck every
   copied hash/role and absence of unexpected stores. Do not execute restored
   services/models or connect restored schedulers. Inject tamper, missing-file,
   interruption and stale-digest faults only in synthetic clones. No success
   receipt may survive an interrupted or incomplete restore.
7. **Qualify rollback independently.** Pin the actual prior interpreter/package/
   selector and its schema/configuration compatibility. Rehearse recovery and
   switchback on an isolated clone while preserving retirement, revocation,
   publication serialization and provenance guards. A runtime predating those
   guards is not a rollback candidate merely because it starts. Retain old and
   new receipts separately; do not restore previously revoked approval.
8. **Hold activation until every gate passes.** Require final combined-head CI,
   independent review, artifact/install binding, fresh writer proof, profile
   closure, isolated restore, qualified rollback and explicit owner activation
   authority. PR96's source-date/unattended limits, PR98's provider dependencies
   and PR97's coordinated Worker/migration/runner plus persistent-result storage
   requirements must be resolved if adopted. Publication approval is separate.
   No live jobs, migration, backup, storage change or deployment ran here.

## PR98 contracts still missing; smallest next slice

- **Normalizer:** versioned mapping over every authoritative primary/support unit,
  explicit field states and unmapped/unknown rows, with exact source/hash/locator
  bindings and no type/date/agency guesses from text or worksheet labels.
- **Dates/applicability:** verified source-bound conduct/disclosure/retention
  periods, event kinds, historical policy revisions/adoption/scope evidence and
  authenticated acceptance. Mail timestamps, CLI dates and draft rule flags
  cannot supply these decisions.
- **Support provider:** explicit primary/support identities, complete current
  accepted extraction inventories, verified derived bytes and fresh canonical
  joins in one consistent snapshot. A 2,000-unit selection is not completeness.
- **Revision/wiring:** normalization/support/join/rule/config digests must bind
  the PR96 contextual identity before cached detect/downstream reuse. Seven
  outcomes need protected receipts; missing inputs stay blocked. Unattended
  selection/resume/budget integration remains a separate hold.

The smallest future implementation is a **read-only current structured-source
resolver** using explicit primary/support identities and the existing canonical
ledger. It returns the complete verified inventory plus fresh canonical joins
and explicit unresolved normalizer/date/policy requirements. Start with synthetic
missing/historical/capped/changed support cases. It neither normalizes semantic
fields nor sets version_verified/applicability, promotes stages or adds a registry.
This slice has NOT been started.

## Executed synthetic commands

All run with TMPDIR=/var/tmp and PYTHONDONTWRITEBYTECODE=1, Python 3.12.3, at main
28e9d198. Existing tests were reused without changes.

- ReleaseManifestTests plus LedgerSchemaPinTests, the backup/publication
  compatibility test, and ten selected RecoveryV2 cases: 23 passed in 0.770s.
  Selected cases: complete_two_database_roundtrip_and_explicit_synthetic_replay;
  missing_quiescence_rejected; root_lock_alone_rejected; unknown_store_fails_closed;
  database_reference_omission_rejected;
  quiescent_wal_verifies_and_restores_without_connection_finalization;
  restore_inside_original_profile_root_rejected; exact_trusted_manifest_digest_required;
  corrupt_archive_blob_rejected; interrupted_restore_never_returns_success.
- PublishTests.test_correction_and_rollback; five AuthorityRegressionTests
  (modified/missing history, manifest tamper, symlink history, interrupted rollback);
  ProposalProvenanceTests.test_revision_history_tracks_provenance_separately_from_public_text:
  seven passed in 1.447s.

These test names resolve in tests/records/test_release_manifest.py,
tests/records/test_backup_publication_compatibility.py, tests/test_records_backup_v2.py,
tests/records/test_publish.py, tests/records/test_publish_authority.py and
tests/records/test_proposal_provenance.py. Nothing above is evidence of all-writer
stop, operational profile completeness, deployed rollback or activation.
