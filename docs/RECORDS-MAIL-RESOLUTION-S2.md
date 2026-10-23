# S2 canonical mail-resolution transaction (fixture-only)

This backend slice implements design section 6 in
RECORDS-LEGACY-MAIL-RESOLUTION.md. It is not an adopt-mail policy/config CLI,
owner approval, live apply, extraction, or independent semantic review.

## Atomicity and authorization

CanonicalMailBackend accepts optional `resolution_policy`. Under its existing
BEGIN IMMEDIATE, a read-only plan groups records by original SHA. It checks
legacy provenance, untouched stage/cache/proposal state and the policy's own
binding occurrence. The existing record loop retains every genuine occurrence.
After those occurrence FKs exist, appends run once per planned SHA in sorted
order, then the mail row is inserted and the transaction commits. Original
and legacy declarations are never rewritten. The installed Option B DDL,
triggers and marker hash remain unchanged.

An unresolved dependent refuses before canonical writes. Once the binding
commits, a dependent adds only its own occurrences. With a configured policy,
resolved originals still require the configured entry's binding receipt,
occurrence and legacy hash to match, and the current receipt to be listed.
A successor never replaces the resolution or its original policy hash.
Filesystem CAS/catalog/verification-receipt writes still precede the canonical
transaction and can leave harmless hash-addressed orphans after refusal.

## Explicit reader trust (scoped operator admission)

S1's mail reader is unchanged: it still requires the ORIGINAL trusted policy
and explicit output root. Generic store readers remain fail-closed. A ledger
row's policy hash is never used to find or select a trusted policy.

`MailResolutionReadContext` is caller-built, frozen preservation-only transport:

- output_root is the independently configured intake root;
- policy is an independently loaded/pinned policy, sufficient for same-policy
  reads and new bindings;
- historical_policies maps each subject to `(independent_original_policy_hash,
  original_ApprovedMailResolutionPolicy_object)`.

S3a owns the production config trust root that supplies historical pins. In
this slice they are synthetic fixture inputs. Supplying a successor without
original historical trust refuses `resolution_policy_mismatch`. Unpinning the
current write policy does not unpin historical reader trust: callers must still
provide the original historical context to read an existing mail binding.
An unpinned context cannot bootstrap trust from the row.

The backend passes this object explicitly through wp1_bridge to the installed
preserver. The runner profile fingerprint includes its trust fingerprint.
Only preserve set-content/claim/promote reads use it; extract and other
substantive stages do NOT gain mail-reader authority. The preservation envelope,
stage algorithm and replay validation rules are unchanged; their original reads
now receive the context. Existing context-free callers keep their API/defaults.
An injected stage_runner_factory used with resolution must accept the explicit
`preservation_context` keyword; legacy no-policy factory calls are unchanged.

This is trusted in-process startup configuration, not a sandbox or an approval
oracle: a caller that can author code and mint policy objects remains within
the documented owner/config trust boundary.

## S3b remaining obligation: D24

ApprovedMailResolutionPolicy now retains immutable optional source_path and
source_sha256 metadata supplied by the caller. load() supplies its securely read
content-addressed path and the pinned digest; the bytes constructor can omit
both for fixtures or already securely read artifacts. Metadata must be paired
and the source hash must equal the verified policy hash.

This slice DOES NOT re-read that file between classification and apply. S3b
must re-read and compare the source under the records-root writer lock, refuse
`resolution_policy_changed` with zero writes when changed, and implement/test
design D24. S2 does not claim D24 acceptance.

## Verification boundary

The new test module covers D16-23, duplicate binding choices and replay,
all-state unlisted-receipt refusal, immutable v1/baseline rows, explicit
historical trust negatives, scoped context transport, precommit exception
rollback, and an actual forked-process exit after commit followed by recover_run
and stage resumption. No live or private records are inputs.

Original reconciliation suite, full discovery, Group F, T12: exact-head CI only,
not run locally (guard rules). The admission owner's SD-12 disposition assigns
these unchanged suites to CI because their child commands conflict with
unattended command rules. Local acceptance comprises policy/mail, D16-23,
audit/closure, authorization controls and scans; the in-process policy/mail
tests are not represented as the full A-C selection.
Passing structural tests is review-required, never semantic acceptance.
