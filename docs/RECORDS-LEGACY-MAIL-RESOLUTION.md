# Resolving legacy unresolved originals from verified mail-export evidence (T4B design)

Status: DESIGN DRAFT for independent review. Nothing here is implemented.
No engine code changes with this document. No ledger, export or TEST copy was
read or written to write it. Implementation waits for Phase 1 go-live (T17)
and a separate implementation review. Applying it to TEST or to the live ledger
also needs owner (G) approval of the resolution policy described below.

Code references are to Grahammmm/DeFlock-Campaign-Tool#148 at exact head
`039900bb55e023e67c1121663802280a15b1b1ad`, which is stacked on #142
(`records adopt-mail`). Main does not yet contain `mail_adoption.py`.

## 1. Problem

`records adopt-mail` (docs/RECORDS-MAIL-ADOPTION.md) adopts frozen
mail-history export receipts through `CanonicalMailBackend.preserve`. That
backend refuses a receipt when any of its originals (the EML, an attachment or
a native item) is already declared in the ledger with
`scope != 'in_scope'`. The refusal is `canonical_original_binding_conflict`
(runner/canonical_mail.py, the `original['scope']!='in_scope'` check in
`preserve`). adopt-mail predicts that refusal without writing anything and
reports it as the coded gap `original_scope_unresolved`.

Those declarations come from the legacy import. Each one is an `originals` row
with `role = scope = 'unresolved'`, `storage_path IS NULL`, provenance
`{"table": "docs", "import_id": ..., "source_rowid": ...}` that binds it to a
`legacy_rows` entry, and one or more projected `captured_legacy_*` occurrences.
For the EMLs in question these are `captured_legacy_local` filesystem
observations (ledger/occurrence_projection.py `_base_plan`, scheme
`legacy_filesystem_observation_v1`). A byte hash proves which bytes these are.
It does not prove role, scope or how they were acquired. That is why the
importer left them unresolved, and why adopt-mail must not reinterpret them on
its own.

G decided on T4 on 2026-10-19: option (c) now, meaning adopt everything that
can be adopted and report the rest as accepted coded gaps. Option (b), which
resolves them, comes later under a separately reviewed contract. This document
is that contract.

T4 acceptance on TEST at 039900b (t_082c94b0 handoff): 154 receipts, 36
adopted, 115 coded gaps. The gaps split into `original_scope_unresolved: 114`
and `unsupported_rfc822_part: 1`.

So the card title's "115 legacy originals" is really two things:

- 114 receipts are blocked by unresolved legacy declarations. The number of
  distinct unresolved originals behind them is not yet known. A receipt can
  bind several originals (EML plus attachments), and one original can appear in
  several receipts. Step 0 (section 8) counts it, read-only.
- 1 receipt is blocked by MIME parsing (a `message/rfc822` part). Resolving
  scope does not fix that receipt, because the legacy MIME check runs before
  any ledger binding. It is out of scope here and stays a coded gap (section 10).

## 2. Goals and non-goals

Goals

1. Let an unresolved legacy original get an initial `role='original'`,
   `scope='in_scope'` binding to its canonical intake CAS path. The evidence is
   a verified mail-export acquisition plus an owner-approved, hash-pinned
   resolution policy.
2. Adopt the blocked receipts in the same transaction, through the existing
   `CanonicalMailBackend.preserve` path and the existing preservation stage.
   No new writer of `mail_messages` or `occurrences`.
3. Keep existing guarantees: idempotent reruns, dry run by default with no
   writes, counts and hashes only in reports, fail-closed binding checks,
   crash recovery through the existing run lifecycle.

Non-goals

- No rewrite of any `originals`, `legacy_rows`, `occurrence_projection_rows` or
  `captured_legacy_*` occurrence row. The legacy declaration stays immutable and
  keeps its unresolved provenance (docs/ORIGINAL-RECONCILIATION.md).
- No supersession, rebinding or scope override of an original that is already
  resolved, bound, excluded or in progress.
- No stage promotion other than the existing `preserve` stage the backend
  already runs. No extraction, OCR, review or semantic acceptance. A resolved
  original is still review-required.
- No inference of scope from folder names, sender, subject or model output.
  Scope comes only from the owner-approved policy.
- No change to `ACCEPTED_GAP_CODES`, and no change to adopt-mail behaviour when
  no resolution policy is configured.
- No mailbox access, exporter run, outbound mail or publication.

## 3. Decision: reuse the `original_resolution_v1` table with a second evidence kind

Today `ledger/original_resolution.py` implements exactly one evidence kind:
verified local capture (`append_initial`, `effective_original`). Two ways to add
mail-export evidence were considered.

Option A: a new additive extension, e.g. `original_mail_resolution_v1`, with its
own table and marker.

- Pro: column names fit the meaning.
- Con: it needs a cross-table trigger so an original is never resolved twice
  (local and mail).
- Con: an engine version that predates it (rollback) silently ignores the new
  table. It would read the original as unresolved again, while stage history
  shows preserve `done`. That is a quiet inconsistency, not a loud stop.

Option B (RECOMMENDED): keep the installed `original_resolution_v1` table, its
DDL, its triggers and its schema hash exactly as they are. Add a second payload
schema, `original_resolution_mail_export_v1`, chosen by `payload["schema"]`.

- The table's `original_sha256 PRIMARY KEY` and its "cannot be replaced" trigger
  already guarantee at most one resolution per original, across both kinds.
- No new DDL, no new marker, and the backup schema pin does not change.
- An older reader meets a mail payload and fails loudly with
  `original_resolution_payload_mismatch`, because its `FIELDS` and
  `schema == migration.NAME` checks reject the payload. That stop is what we
  want on rollback.
- Con: the column names `capture_receipt_sha256` and `capture_occurrence_id`
  are reused with a mail meaning. The mapping below is fixed and documented.

Column mapping for a mail-export resolution:

| column | value |
|---|---|
| `original_sha256` | the resolved original |
| `binding_sha256` | `sha256(canonical(payload))` |
| `declaration_sha256` | `digest(originals row)`, the same function as v1 |
| `policy_sha256` | sha256 of the approved mail-resolution policy (section 4) |
| `capture_receipt_sha256` | the export verification receipt digest (`<output>/verification-receipts/<digest>`) |
| `capture_occurrence_id` | the `verified_mail_export` occurrence id of this original in that receipt (FK to `occurrences`) |
| `payload_json` | canonical payload below |

The payload has exactly these keys (`MAIL_FIELDS`):

    {"schema": "original_resolution_mail_export_v1",
     "original_sha256": <sha>, "declaration_sha256": <sha>,
     "policy_sha256": <sha>, "verification_receipt_sha256": <sha>,
     "occurrence_id": <id>, "occurrence_kind": "mail" | "attachment",
     "mail_identity_sha256": sha256(canonical({account, folder, uidvalidity, uid})),
     "legacy_binding_sha256": <legacy_rows.payload_sha256>,
     "byte_length": <int>, "role": "original", "scope": "in_scope",
     "storage_path": "<intake output>/blobs/<sha>"}

`verification_receipt_sha256` must equal `capture_receipt_sha256`, and
`occurrence_id` must equal `capture_occurrence_id`. Payloads never hold raw
account, folder or header text. The mail identity appears only as a hash and
can be checked against the occurrence's `source_ref`.

## 4. Trust root: the approved mail-resolution policy

The owner, not the engine, decides role and scope, the same way the local
capture policy works (docs/ORIGINAL-RECONCILIATION.md). The private artifact
`approved-mail-resolution-policy-v1` has exactly these keys:

    {"schema": "approved-mail-resolution-policy-v1",
     "inventory_sha256": <sha of the step-0 inventory G reviewed>,
     "approval_sha256": <sha of G's written approval record>,
     "originals": {<sha256>: {"bytes": <int>, "role": "original",
                              "scope": "in_scope",
                              "verification_receipt_sha256": <sha>,
                              "legacy_binding_sha256": <sha>}}}

Rules:

- Size limits: at most 2 MiB and 10,000 originals. Duplicate JSON keys,
  non-finite numbers, extra keys, upper-case or malformed digests, and any
  `role` or `scope` other than the fixed values are rejected (code
  `resolution_policy_invalid`).
- The policy is content-addressed. It lives at
  `<root>/resolution-policies/<sha256>`, owner-only, a regular non-symlink file,
  inside the private records root and never in Git. It is loaded through
  `ApprovedMailResolutionPolicy(raw, expected_sha256=...)`.
- Where the expected hash comes from. Two choices:
  - Option 1 (RECOMMENDED): a new optional key
    `"legacy_resolution_policy_sha256": <sha>|null` in the owner-only adoption
    config. That bumps the config schema to `mail-history-adoption-v2`; v1
    configs stay valid and mean "no resolution". The config is already the
    owner's trust root for the adoption gate. It is re-read and re-checked under
    the records-root writer lock, like the gate.
  - Option 2: an installed-startup constant, like the local capture policy.
    This is stricter, but every policy change then needs an engine install.
  No CLI flag, receipt, evidence row or callback can choose the policy.
- Each policy entry binds one original to one specific verification receipt and
  one specific legacy row. A different export of the same bytes does not
  qualify. That export needs its own approved entry.
- The engine checks the artifact's hash and exact schema. It does not check that
  the owner's approval is genuine. That remains the existing owner-authorization
  process. Deciding on the inventory is G's call (section 9).

## 5. Eligibility (all must hold, per original)

An original O bound by receipt R can be resolved only if:

1. `originals` row: `role = scope = 'unresolved'`, `storage_path IS NULL`, and
   `bytes` equals the CAS proof length.
2. Legacy binding: provenance `table='docs'` with a `legacy_rows` entry whose
   payload hash, identity `{"sha": O}`, `bytes` and `format` match. This is the
   existing v1 check (`original_resolution_import_binding_mismatch`). Its
   `payload_sha256` must equal the policy's `legacy_binding_sha256`.
3. Untouched: all seven `stage_state` rows are `pending` with no receipt, there
   are no `stage_content` rows, and no proposal receipt has O as its subject.
   These are the v1 checks.
4. No existing row in `original_resolutions` for O. Exactly the same payload
   counts as an idempotent replay. Anything else is `original_resolution_conflict`.
5. The policy has O with equal `bytes` and with
   `verification_receipt_sha256` equal to R's verification receipt digest.
6. R passes the backend's existing acquisition verification: export
   bytes and hash, the legacy MIME check, intake catalog locators, and
   writing the content-addressed verification receipt. All of this is
   unchanged code in `LegacyIntakeBackend.preserve` / `_capture_verification`.
7. Kind check: when O is R's EML, `legacy_format == 'eml'`. When O is an
   attachment, the CAS proof bytes match. No MIME-type rewrite is applied
   to the legacy row.
8. The whole receipt resolves or none of it does. Every original of R is either
   (a) already `in_scope` at `<output>/blobs/<sha>` (the existing rule), (b) new
   (the existing insert), or (c) eligible under 1-7. If any original fails, the
   whole receipt stays a coded gap and nothing is written for it.

Excluded (`out_of_scope`) originals, originals bound elsewhere and originals
already accepted can never be resolved. They keep today's codes
(`original_scope_not_in_scope`, `original_storage_elsewhere`,
`original_bytes_conflict`).

## 6. Write path (inside the existing `CanonicalMailBackend.preserve` transaction)

`CanonicalMailBackend` gets one optional constructor argument,
`resolution_policy`. adopt-mail sets it only when the config pins a policy
(section 4). With no policy, behaviour is byte-identical to today.

Inside the existing `BEGIN IMMEDIATE` in `preserve`:

1. Before the record loop, build a resolution plan for each record. Read
   `effective_original`. If it is unresolved, run checks 1-8, all read-only and
   inside the transaction. Any failure raises
   `canonical_original_binding_conflict` exactly as today, so the backend's
   failure surface does not change.
2. Run the existing record loop. For planned originals the
   `canonical_original_binding_conflict` check uses the planned effective
   binding (in_scope at the CAS path) instead of the stored declaration. It
   does not insert an `originals` row, because the row already exists. It
   inserts the `verified_mail_export` occurrence exactly as today.
3. After the occurrences are inserted (the FK needs them), call
   `original_resolution.append_mail_export(con, ...)` for each planned original.
   It rechecks 1-5 against the open transaction, then inserts the immutable row
   through the existing `original_resolution_append_allowed` one-shot SQL
   function gate. It then calls `effective_original(con, O)`, which must now
   return the resolved binding.
4. Insert `mail_messages` as today, then commit. All of it is one atomic
   transaction. A failure or crash at any point before commit leaves no
   resolution, occurrence or mail row behind.
5. The preservation stage then runs unchanged.
   `effective_original(...)['resolution_sha256']` already goes into
   `preservation-evidence-v1` (the existing `if effective.get('resolution_sha256')`
   branch), and `_validator` already requires the envelope and the effective
   original to agree. The envelope's `occurrence_ids` list only
   `verified_*` occurrences, so the `captured_legacy_local` declarations stay
   out of the acquisition set. The validator already rejects a declared
   occurrence inside an envelope.

Filesystem side effects happen before the SQLite transaction, as today: intake
CAS blob, intake catalog row and verification receipt. They are all
hash-addressed and reused exactly on retry. An interrupted run can leave
harmless orphan bytes but never a binding.

`append_mail_export` must only be called by the backend inside its
root-locked write transaction. It is not exposed through the CLI.

## 7. Read path (`effective_original`)

Choose by `payload["schema"]`:

- `original_resolution_v1`: unchanged.
- `original_resolution_mail_export_v1`: check the exact `MAIL_FIELDS` set,
  `binding_sha256 == digest(payload)`, the declaration digest, `byte_length`,
  the declaration still unresolved with no storage, and the columns equal the
  payload (`policy_sha256`, receipt and occurrence). The occurrence must exist
  with `original_sha256 = O`, `acquisition_method = 'verified_mail_export'` and
  the same `kind`, with `sha256(canonical(source_ref identity fields))` equal to
  `mail_identity_sha256` and evidence `receipt_sha256` equal to the payload
  receipt. The verification receipt at `<output>/verification-receipts/<digest>`
  (path derived from the digest, never taken from evidence, no symlinks, at
  most 1 MiB) must still hash to its digest. `storage_path` must be absolute
  and end in `blobs/<O>`, and its `blobs` parent must be the same output root
  as the verification receipt. As in v1, the CAS bytes are verified by the
  preservation validator, not on every read.
  The function then returns the declaration overlaid with `role='original'`,
  `scope='in_scope'`, `storage_path`,
  `preservation_status='mail_export_validation_pending'` and
  `resolution_sha256`.
- Any other schema: `original_resolution_payload_mismatch`.

Codes are new where needed and safe (no paths):
`original_resolution_mail_occurrence_unbound`,
`original_resolution_mail_receipt_changed`,
`original_resolution_storage_binding`, plus the existing v1 codes.

## 8. adopt-mail integration (classification, report, gate)

Step 0, census (read-only, before any policy exists): a new subcommand,
`records adopt-mail --legacy-resolution-inventory <private out path>`. The
alternative is a separate `records legacy-resolution-inventory` command; the
choice is left to the implementation review. It uses the same snapshot read as
adopt-mail (`immutable=1`, fails closed on a non-empty WAL, sidecar identities
unchanged), so it writes nothing to the ledger, intake catalog or export. For
every receipt that is currently `original_scope_unresolved` it lists each
unresolved original with: sha, bytes, legacy_format, kind, receipt verification
digest (computed from the receipt bytes; nothing is written), legacy binding
hash, and eligibility checks 1-4 and 7 with a safe code. The output is a
private owner-only JSON file outside Git. Stdout gets counts only: receipts,
distinct originals, eligible, held by code. G reviews the inventory locally;
its sha becomes `inventory_sha256`.

Classification. A `new` receipt whose only gap reason is
`original_scope_unresolved` becomes `resolvable` when a policy is configured
and every unresolved original passes 1-5 and 7 read-only, after the MIME
precheck (which still runs first). Otherwise it stays a coded gap. When a
policy is configured, a more specific code is added, all of them blocking:
`resolution_not_in_policy`, `resolution_policy_receipt_mismatch`,
`resolution_requires_unaccepted_original`,
`resolution_import_binding_mismatch`. That way the owner sees exactly why an
approved original did not resolve. Without a policy, today's
`original_scope_unresolved` (accepted) is unchanged.

Open point for the reviewer: should a configured-but-unmatched receipt fall
back to the accepted `original_scope_unresolved`, so it does not block?
RECOMMENDED: block. An approved policy that does not apply means the inventory
has drifted.

New report counters (counts only):

- `resolvable_messages` and `resolvable_attachments`
- `resolved_originals` (apply)
- `resolution_policy` = `{state: none|loaded|mismatch, sha256}`
- `after.resolvable_messages`

The invariant becomes
`receipts == already_adopted + pending_messages + coded_gap_messages + sum(identity_conflicts)`,
where `pending_messages` now also counts resolvable receipts and
`new_messages = eligible_new_messages + resolvable_messages + coded_gap_messages`.

Gate. Apply requires the existing open gate, and also a policy whose file hash
equals the pinned hash. Otherwise apply is refused with `resolution_policy_mismatch` and
nothing is written. The policy is re-read under the writer lock. If its bytes
changed, apply is refused with `resolution_policy_changed`. A dry run with a
pinned but missing policy reports `resolution_policy.state = mismatch` and
exits 3.

Rerun. A resolved receipt classifies as `complete` through the existing binding
checks (`identity_original_*` uses `effective_original`). A rerun therefore
adopts 0 and writes nothing.

## 9. Owner decisions and safety

These need G before any apply (NEEDS G at that time, not now):

1. Approve the step-0 inventory, as a written approval record whose hash
   becomes `approval_sha256`. This is a scope decision about real records, so
   the engine cannot make it.
2. Approve the TEST policy for TEST acceptance, and later and separately the
   live policy for live apply. Live apply is an irreversible ledger write: the
   resolutions are immutable by trigger. It needs a verified pre-apply backup of
   the full closure (ledger, intake catalog, CAS, verification receipts,
   `resolution-policies/`).
3. A written G decision that changes the T4 acceptance numbers. The runbook's
   `GAPS=115` and `mail_messages=39` become `GAPS = 115 - N` and
   `mail_messages = 39 + N`, where N is the number of resolvable receipts.

Rollback. Resolution rows cannot be updated or deleted (existing triggers). The
only rollback is restoring the pre-apply backup closure. A later correction
needs its own reviewed supersession contract. Never hand-edit SQLite to clear
a resolution.

Data handling. The policy, inventory, approval record, receipts and any
per-original listing stay in private storage. Reports, PRs, cards and chat
carry counts, codes and the policy and inventory hashes only. Tests use
synthetic fixtures only.

## 10. Out of scope / follow-ups

- The single `unsupported_rfc822_part` receipt needs a separately reviewed MIME
  contract for `message/rfc822` parts in legacy receipts. It is unaffected by
  this design and stays an accepted coded gap.
- Resolving legacy originals that no frozen export receipt covers (for example
  portal or local-only legacy rows) is out of scope. The local-capture
  resolution path already exists for those.
- Backup and release composition (docs/RECORDS_BACKUP_V2.md): add
  `resolution-policies/` to the required closure. The `original_resolution_v1`
  pin does not change (Option B).

## 11. Test plan

All tests use synthetic fixtures built in temp dirs, like the existing
`tests/records/test_mail_adoption*.py` and `test_original_reconciliation.py`.
Run with `-W error::ResourceWarning`.

A. Policy (`test_mail_resolution_policy.py`)

1. Valid policy loads, and its hash is checked.
2. Hash mismatch fails closed.
3. Rejected inputs: extra or missing keys, duplicate JSON keys, NaN,
   upper-case digests, role/scope other than the fixed values, more than
   10,000 entries, more than 2 MiB.
4. A symlink, a non-regular file, or a group- or world-readable file is refused.

B. Reader (`test_original_resolution_mail.py`)

5. Happy path: effective_original returns the overlay and `resolution_sha256`.
   The stored `originals` row is unchanged byte for byte.
6. Fails closed when any of these is tampered with: payload key set,
   binding hash, declaration digest (the original row edited), byte_length,
   column/payload disagreement, occurrence missing, occurrence with the wrong
   method, kind or sha, mail identity hash, evidence receipt digest,
   verification receipt missing, symlinked or changed, or storage path
   outside `blobs/<sha>` or under another output root.
7. An unknown payload schema raises `original_resolution_payload_mismatch`.
8. A simulated pre-T4B reader (v1 `FIELDS` only) refuses a mail payload loudly
   (the rollback property).
9. v1 local-capture resolutions still read exactly as before (regression).

C. Append (`test_original_resolution_mail.py`)

10. Each eligibility check 1-7 has a negative case that leaves no row and gives
    its safe code.
11. Calling the gate function outside the backend is refused by the trigger.
12. UPDATE and DELETE are refused, and a second resolution with different
    evidence gives `original_resolution_conflict`.
13. An exact replay is idempotent and returns `(identity, True)`.
14. Local-capture then mail-export, and mail-export then local-capture, for
    the same original: the second one is refused.

D. Backend integration (`test_canonical_mail_resolution.py`)

15. A receipt whose EML is a legacy unresolved original with
    `captured_legacy_local` is adopted. Expected rows: one resolution row, a
    `verified_mail_export` occurrence, and a mail row. preserve is `done`, and
    the envelope carries `resolution_sha256`.
16. In that same case, these are byte-identical before and after: the legacy
    occurrence, `legacy_rows`, `occurrence_projection_rows` and the `originals`
    row.
17. The replay validator passes (`_existing_preservation_context`).
18. The same receipt where an attachment is unresolved and missing from the
    policy: refused, all-or-nothing, with no resolution, occurrence or mail
    row.
19. No policy configured: identical to today
    (`canonical_original_binding_conflict`).
20. Fault injection: a raise after the resolution insert and before commit
    leaves nothing. A crash after commit and before the preservation stage
    gives an `incomplete` receipt, and the next apply resumes it. A dead run
    is recovered through `recover_run`.
21. The policy file is changed between classification and apply:
    `resolution_policy_changed`, with nothing written.

E. adopt-mail (`test_mail_adoption_resolution.py`)

22. Dry run with a policy predicts exactly the apply outcome:
    `resolvable_messages` equals the adopted count, and the per-code counts
    match.
23. A dry run writes nothing. The ledger, intake catalog and every sidecar
    identity are unchanged.
24. Counter invariants hold in dry-run, apply and `after`.
25. An rfc822 receipt whose EML is in the policy still reports
    `unsupported_rfc822_part`.
26. A configured-but-unmatched original gives the blocking specific code, and
    the exit code is 3.
27. A v1 config is accepted and means no resolution. A v2 config with `null`
    behaves the same. A pinned but missing policy gives `mismatch` and exit 3.
28. A rerun after apply adopts 0, every resolved receipt is `complete`, and
    nothing is written.
29. Under a concurrent root writer, apply waits or is refused under the existing
    lock, with no partial state.
30. Census: the output lists only unresolved originals of
    `original_scope_unresolved` receipts. Stdout has counts only. Nothing is
    written to the ledger, intake catalog or export.

F. Regression

31. All existing `tests/records/test_mail_adoption*.py`,
    `test_original_reconciliation.py`, `test_records_run_stages.py` and the
    T12 harness pass unchanged.
32. `tools/check_public_tree.py` reports findings=[], and
    `scripts/scan-secrets.mjs` passes.

G. TEST acceptance (only after the implementation is reviewed by a different
model and G has approved the TEST policy)

Run it on TEST restored from the pre-apply backup, extending the existing
runbook in docs/RECORDS-MAIL-ADOPTION.md. Report numbers only:

33. Census: report distinct unresolved originals, eligible originals and held
    originals by code. G approves the inventory, and the policy is written
    privately.
34. Dry run: `receipts=154`, `eligible_new_messages=36`, `resolvable_messages=N`,
    `coded_gaps` has only `unsupported_rfc822_part: 1` plus any held codes G
    has accepted in writing.
35. Apply: `adopted_messages = 36 + N`, `resolved_originals = M` (the census
    count), `mail_messages = 39 + N`.
36. Rerun: `adopted_messages = 0`.
37. `quick_check` ok, FK violations 0.
38. Unchanged before and after: digest of all `originals` rows, `legacy_rows`
    and `captured_legacy_*` occurrences.
39. `original_resolutions` count equals M, and every row has the mail-export
    schema and the approved policy hash.

## 12. Implementation slicing (after T17)

Each slice is one PR, signed off, reviewed by a different model:

1. Policy class, mail payload read path and `append_mail_export`, with test
   groups A-C.
2. Backend resolution plan and transaction (D).
3. adopt-mail classification, config v2, counters, census (E), plus
   documentation updates to RECORDS-MAIL-ADOPTION.md and
   ORIGINAL-RECONCILIATION.md.
4. Backup closure addition (`resolution-policies/`).
5. TEST acceptance (G), with G's approval of the TEST policy. Live apply needs
   its own G decision.
