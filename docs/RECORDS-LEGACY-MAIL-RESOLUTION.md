# Resolving legacy unresolved originals from verified mail-export evidence (T4B design)

Status: DESIGN DRAFT for independent review. Nothing here is implemented.
No engine code changes with this document. No ledger, export or TEST copy was
read or written to write it. Implementation waits for Phase 1 go-live (T17)
and a separate implementation review. Applying it to TEST or to the live ledger
also needs owner (G) approval of the resolution policy described below.

Revision 2 answers the Sol review of 349ae60. It adds one policy-pinned
binding per distinct original (repeated parts inside a receipt and originals
shared across receipts, sections 4-6 and 8.2). It also restates the TEST
acceptance sets against a measured baseline (section 11.H).

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
  several receipts. Step 0 (section 8.1) counts it, read-only.
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
| `capture_occurrence_id` | the policy's `binding_occurrence_id`: one `verified_mail_export` occurrence of this original in the binding receipt (FK to `occurrences`, section 4) |
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
`occurrence_id` must equal `capture_occurrence_id`. There is exactly one
payload per distinct original, never one per occurrence. Other occurrences of
the same bytes (a repeated attachment part, or the same original in another
receipt) are still inserted as ordinary `verified_mail_export` occurrences,
but they never produce a second resolution row. Payloads never hold raw
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
                              "binding_occurrence_id": <occurrence id>,
                              "dependent_receipts": [<sha>, ...],
                              "legacy_binding_sha256": <sha>}},
     "held": [<sha256>, ...]}

Field meanings:

- `verification_receipt_sha256` is the binding receipt: the one receipt whose
  preserve transaction writes the resolution row for this original.
- `binding_occurrence_id` is the one occurrence of the original inside the
  binding receipt that the resolution row names. Occurrence ids are
  deterministic (`hid(["attachment"|"native-msg-item", message_id, mime_path])`,
  or the message id for the EML), so the census can compute it without
  writing anything.
- `dependent_receipts` lists every other receipt in the frozen export that
  also contains this original. Each is approved to bind the original only
  after the binding receipt has resolved it (section 8.2). Sorted, unique, and
  never containing the binding receipt.
- `held` lists originals that G reviewed in the census and chose not to
  resolve. It is sorted, unique, and disjoint from `originals`.

Deterministic binding proposal. The census (section 8.1) proposes each entry.
G can accept the proposal or remove an original to `held`. G does not hand-pick
receipts. The proposal is:

1. Candidates are the receipts that contain the original and pass the legacy
   MIME precheck. The binding receipt is the candidate with the smallest
   receipt digest. Every other receipt that contains the original, including
   ones that fail the precheck, goes in `dependent_receipts`.
2. The binding occurrence is the EML occurrence when the original is the
   binding receipt's EML. Otherwise it is the lexicographically smallest
   occurrence id among that receipt's attachment and native-item occurrences
   with these bytes. Discovery order and file names play no part.
3. If no candidate passes the precheck, the census proposes the original as
   `held` with code `resolution_no_eligible_binding_receipt`.

Because every dependency edge then runs from a smaller receipt digest to a
larger one, a policy built this way cannot contain a cycle. The engine still
checks for cycles (section 8.2), because the policy is an owner artifact and
the engine does not trust how it was produced.

Rules:

- Size limits: at most 2 MiB, 10,000 `originals` entries plus `held` entries
  combined, and at most 10,000 entries in each `dependent_receipts` (the
  adopt-mail `MAX_RECEIPTS` bound). Duplicate JSON keys,
  non-finite numbers, extra keys, upper-case or malformed digests, unsorted or
  repeated list entries, an original in both `originals` and `held`, a binding
  receipt repeated in its own `dependent_receipts`, and any `role` or `scope`
  other than the fixed values are rejected (code `resolution_policy_invalid`).
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
- Each policy entry binds one original to exactly one binding receipt, one
  binding occurrence and one legacy row. That gives exactly one resolution row
  per distinct original. Only the receipts named in the entry, the binding
  receipt plus `dependent_receipts`, may touch the original in a resolving run.
  A receipt that contains the bytes but is not named in the entry is never
  authorized: that is inventory drift (section 8.2). Any other export of the
  same bytes needs a new census and a new approved policy.
- The engine checks the artifact's hash and exact schema. It does not check that
  the owner's approval is genuine. That remains the existing owner-authorization
  process. Deciding on the inventory is G's call (section 9).

## 5. Eligibility (all must hold, per distinct original)

Eligibility is checked once per distinct original O in a receipt R, not once
per occurrence. When R contains the same bytes in several parts, all those
parts share one plan entry. O can be resolved by R only if:

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
5. The policy has O in `originals` with equal `bytes`, with
   `verification_receipt_sha256` equal to R's verification receipt digest (R
   is the binding receipt), and with `binding_occurrence_id` equal to one of
   R's own occurrence ids for O. When R appears only in O's
   `dependent_receipts`, R does not resolve O. It may only bind O after O is
   resolved (section 8.2).
6. R passes the backend's existing acquisition verification: export
   bytes and hash, the legacy MIME check, intake catalog locators, and
   writing the content-addressed verification receipt. All of this is
   unchanged code in `LegacyIntakeBackend.preserve` / `_capture_verification`.
7. Kind check: when O is R's EML, `legacy_format == 'eml'`. When O is an
   attachment, the CAS proof bytes match. No MIME-type rewrite is applied
   to the legacy row.
8. The whole receipt resolves or none of it does. Every distinct original of R
   is (a) already `in_scope` at `<output>/blobs/<sha>` (the existing rule;
   this includes an original that its binding receipt resolved earlier in
   the plan, section 8.2), (b) new (the existing insert),
   or (c) eligible under 1-7 with R as its binding receipt. If any original
   fails, the whole receipt stays a coded gap and nothing is written for it.

Excluded (`out_of_scope`) originals, originals bound elsewhere and originals
already accepted can never be resolved. They keep today's codes
(`original_scope_not_in_scope`, `original_storage_elsewhere`,
`original_bytes_conflict`).

## 6. Write path (inside the existing `CanonicalMailBackend.preserve` transaction)

`CanonicalMailBackend` gets one optional constructor argument,
`resolution_policy`. adopt-mail sets it only when the config pins a policy
(section 4). With no policy, behaviour is byte-identical to today.

Inside the existing `BEGIN IMMEDIATE` in `preserve`:

1. Before the record loop, build the resolution plan, keyed by distinct
   original sha and not by record. `records` holds one row per occurrence
   (canonical_mail.py:241-257), so two byte-identical attachment parts give
   two records with the same sha and different occurrence ids. They collapse
   into one plan entry. For each distinct sha, read `effective_original`. If
   it is unresolved, run checks 1-8 once, all read-only and inside the
   transaction. That includes checking that the policy's
   `binding_occurrence_id` is one of this receipt's record ids for that sha.
   An unresolved original for which this receipt is not the binding receipt
   (for example a dependent receipt whose binding receipt has not committed
   yet) fails here. Any failure raises `canonical_original_binding_conflict`
   exactly as today, so the backend's failure surface does not change.
2. Run the existing record loop over every record, duplicates included. For
   planned originals the `canonical_original_binding_conflict` check uses the
   planned effective binding (in_scope at the CAS path) instead of the stored
   declaration, for every occurrence of that sha. It does not insert an
   `originals` row, because the row already exists. It inserts each
   `verified_mail_export` occurrence exactly as today, so a repeated part keeps
   its own occurrence row.
3. After all occurrences are inserted (the FK needs them), call
   `original_resolution.append_mail_export(con, ...)` exactly once per plan
   entry, in ascending sha order, with `occurrence_id = binding_occurrence_id`.
   It rechecks 1-5 against the open transaction, then inserts the immutable row
   through the existing `original_resolution_append_allowed` one-shot SQL
   function gate. It then calls `effective_original(con, O)`, which must now
   return the resolved binding. The backend never appends twice for one sha in
   one transaction, so the table's one-row-per-original rule cannot conflict
   with the backend's own duplicates.
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
   occurrence inside an envelope. The stage loop runs over `value.documents`
   (distinct shas), so a repeated part is preserved once.
6. A dependent receipt preserved after its binding receipt committed sees O as
   already in_scope at the CAS path. It takes the existing rule-(a) branch: no
   plan entry, no append, one new `verified_mail_export` occurrence per part.
   The preservation stage is already `done` for O, so the existing replay
   validator (`_existing_preservation_context`) checks the binding receipt's
   envelope and never enlarges its acquisition set.

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

### 8.1 Step 0: census (read-only, before any policy exists)

Add a new subcommand,
`records adopt-mail --legacy-resolution-inventory <private out path>`. The
alternative is a separate `records legacy-resolution-inventory` command; the
choice is left to the implementation review. It uses the same snapshot read as
adopt-mail (`immutable=1`, fails closed on a non-empty WAL, sidecar identities
unchanged), so it writes nothing to the ledger, intake catalog or export.

The inventory set I is every distinct original that is declared
`scope='unresolved'` and appears in at least one discovered `new` receipt,
whatever that receipt's current gap code (so the `unsupported_rfc822_part`
receipt is included). For each original in I, the census lists:

- sha, bytes, legacy_format, kind, and the legacy binding hash;
- every discovered receipt that contains it: receipt digest (computed from the
  receipt bytes; nothing is written), whether the MIME precheck passes, and
  all of that receipt's occurrence ids for the bytes (more than one means a
  repeated part);
- the deterministic proposal from section 4: binding receipt, binding
  occurrence and dependent receipts, or `held` with a code;
- eligibility checks 1-4 and 7 with a safe code;
- a warning when a proposed binding receipt also contains an original that is
  proposed as held, because then the binding cannot apply (section 8.2).

The output is a private owner-only JSON file outside Git. Stdout gets counts
only: receipts, |I|, originals shared by more than one receipt, originals
repeated inside one receipt, proposed bindings, and proposed held by code. G
reviews the inventory locally. Its sha becomes `inventory_sha256`. The policy
G approves must cover I exactly: `originals` plus `held` equals I, with no
overlap.

### 8.2 Resolution plan (one pure function, shared by dry run and apply)

The plan is computed read-only from the ledger snapshot, the discovered
receipts and the loaded policy. Its output depends only on receipt digests,
shas and occurrence ids, never on discovery order or file names. A dry run
reports the plan. Apply recomputes it under the writer lock from the same
inputs and runs exactly that plan.

1. Per-receipt checks. For every `new` receipt that passes the MIME precheck
   (which still runs first), take each distinct unresolved original O:
   - O in `held`: the receipt keeps today's accepted gap
     `original_scope_unresolved`.
   - O in neither `originals` nor `held`: blocking `resolution_not_in_policy`
     (the inventory has drifted).
   - O in `originals`, but the set of discovered receipts in any state that
     contain O is
     not exactly the binding receipt plus `dependent_receipts`: blocking
     `resolution_policy_receipt_set_mismatch` on every receipt that contains O.
     That covers a new or missing export of the same bytes.
   - The receipt is O's binding receipt: checks 1-5 and 7 (read-only). A
     failure gives a blocking specific code:
     `resolution_policy_receipt_mismatch`,
     `resolution_requires_unaccepted_original` or
     `resolution_import_binding_mismatch`.
   - The receipt is in O's `dependent_receipts`: record a dependency edge from
     the binding receipt to this receipt. The receipt never resolves O itself.
2. Binding receipts that cannot run. Suppose a binding receipt for a policy
   original is gapped for any other reason, such as a held original, the MIME
   precheck or another failing check. Then that policy original cannot resolve
   in this plan, and the receipt gets blocking `resolution_binding_receipt_blocked`.
   An approved binding that cannot apply means the inventory has drifted, so
   it must block.
3. Dependency closure. A dependent receipt whose binding receipt is not
   resolvable gets blocking `resolution_dependency_blocked`. This is repeated
   until nothing changes, because a dependent can itself be the binding
   receipt for another original. Receipts on a dependency cycle (strongly
   connected, possible only in a hand-edited policy) get blocking
   `resolution_dependency_cycle`, and their dependents are blocked in turn.
4. Result. A receipt is `resolvable` when its only gap reason was
   `original_scope_unresolved` and every distinct unresolved original passed
   step 1 as binding or dependent and survived steps 2-3. It is a binding
   receipt if it resolves at least one original, and dependent-only otherwise.
   The plan's resolvable originals E_r are the policy originals whose binding
   receipt is resolvable. Policy originals whose binding receipt is not
   resolvable are counted separately and never resolve in this run.
5. Order. Today's pending receipts (eligible new and incomplete) go first, in
   today's order. Resolvable receipts follow in topological order of the
   dependency graph, with ties broken by ascending receipt digest. With a
   census-proposed policy every edge runs from a smaller digest to a larger
   one, so this is ascending digest order. A receipt that is both binding and
   dependent is placed after all of its binding receipts.
6. Apply-time failures. When the backend refuses a binding receipt during
   apply (a fault, or a policy change caught under the lock), adopt-mail does
   not call `preserve` for any receipt that depends on it, directly or
   transitively. Those are counted as failure `resolution_dependency_failed`
   and nothing is written for them. If such a receipt were called anyway, the
   backend would refuse it too (section 6, step 1), so this rule only prevents
   noise. A later rerun recomputes the plan and picks them up.

After resolution, O is an ordinary `in_scope` original at its CAS path. While
the policy stays pinned, the step-1 receipt-set check keeps running, so a
later export of the same bytes that the entry does not name blocks as drift
instead of quietly binding. Once the owner unpins the policy, O is governed
by the existing T4 rule for adopted `in_scope` originals, the same as any
other adopted original. This design does not widen that rule.

Without a policy, today's `original_scope_unresolved` (accepted) is unchanged,
and steps 1-6 do not run.

Open point for the reviewer: should a configured-but-unmatched receipt fall
back to the accepted `original_scope_unresolved`, so it does not block?
RECOMMENDED: block. Originals G chose not to resolve are listed in `held` and
stay accepted. Anything else means the inventory has drifted.

### 8.3 Report, invariants, gate, rerun

New report counters (counts and hashes only):

- `resolvable_messages`, split into `resolvable_binding_messages` and
  `resolvable_dependent_messages`, plus `resolvable_attachments`
- `resolvable_originals` = |E_r| (dry run and apply, from the plan)
- `policy_originals_blocked` = policy originals whose binding receipt is not
  resolvable
- `held_originals` = policy `held` entries present in discovered receipts
- `resolution_baseline` = `{rows, sha256}` of `original_resolutions` before
  any write, where sha256 is the digest of the sorted
  `(original_sha256, binding_sha256)` pairs
- `resolved_originals` (apply) = number of distinct originals for which
  `append_mail_export` inserted a new row in a committed transaction
- `resolution_after` = `{rows, sha256}` after the run, under the writer lock
- `resolution_policy` = `{state: none|loaded|mismatch, sha256}`
- `after.resolvable_messages`

The invariant becomes
`receipts == already_adopted + pending_messages + coded_gap_messages + sum(identity_conflicts)`,
where `pending_messages` now also counts resolvable receipts and
`new_messages = eligible_new_messages + resolvable_messages + coded_gap_messages`.
For a successful apply with no failures there are three more:
`resolved_originals == resolvable_originals`,
`resolution_after.rows == resolution_baseline.rows + resolved_originals`, and
`adopted_messages == eligible_new_messages + resolvable_messages`.

Gate. Apply requires the existing open gate, and also a policy whose file hash
equals the pinned hash. Otherwise apply is refused with `resolution_policy_mismatch` and
nothing is written. The policy is re-read under the writer lock. If its bytes
changed, apply is refused with `resolution_policy_changed`. A dry run with a
pinned but missing policy reports `resolution_policy.state = mismatch` and
exits 3.

Rerun. Binding and dependent receipts both classify as `complete` through the
existing binding checks (`identity_original_*` uses `effective_original`). A
rerun therefore adopts 0, resolves 0 and writes nothing, and the
`resolution_after` digest does not change.

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
3. A written G decision that changes the T4 acceptance numbers. On the
   historical pre-T4 TEST baseline (section 11.H), the runbook's `GAPS=115`
   and `mail_messages=39` become `GAPS = 115 - N` and
   `mail_messages = 39 + N`, where N is `resolvable_messages`, binding and
   dependent receipts together. On any other snapshot the numbers come from
   that snapshot's own measured baseline, not from these constants.

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
   10,000 entries, more than 2 MiB, unsorted or repeated `dependent_receipts`
   or `held`, a binding receipt listed in its own `dependent_receipts`, an
   original in both `originals` and `held`, and a malformed
   `binding_occurrence_id`.
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
15. `occurrence_id` that differs from the policy's `binding_occurrence_id`,
    or a receipt digest that differs from the binding receipt: refused with
    `resolution_policy_receipt_mismatch`, and no row is written.

D. Backend integration (`test_canonical_mail_resolution.py`)

16. A receipt whose EML is a legacy unresolved original with
    `captured_legacy_local` is adopted. Expected rows: one resolution row, a
    `verified_mail_export` occurrence, and a mail row. preserve is `done`, and
    the envelope carries `resolution_sha256`.
17. In that same case, these are byte-identical before and after: the legacy
    occurrence, `legacy_rows`, `occurrence_projection_rows` and the `originals`
    row.
18. The replay validator passes (`_existing_preservation_context`).
19. The same receipt where an attachment is unresolved and missing from the
    policy: refused, all-or-nothing, with no resolution, occurrence or mail
    row.
20. No policy configured: identical to today
    (`canonical_original_binding_conflict`).
21. Duplicate attachment bytes inside one receipt. The receipt has a new EML
    and two attachment parts with the same bytes O. O is unresolved and in
    the policy, with `binding_occurrence_id` set to the smaller of the two
    occurrence ids. `preserve` commits exactly one resolution row whose
    `capture_occurrence_id` is that id. It also commits two attachment
    occurrence rows, one preserve promotion for O and one mail row. Variants:
    (a) the policy names the larger id: accepted, and the row names it,
    because the policy decides; (b) the policy names an id that is not one of
    this receipt's occurrences of O: refused, nothing written; (c) calling
    `preserve` again on the same receipt: no new row and no conflict.
22. Dependent receipt. R2 contains O, and the policy binds O to R1 with
    `dependent_receipts=[R2]`. Calling `preserve(R2)` before R1 has committed
    is refused with `canonical_original_binding_conflict`, and nothing is
    written. After `preserve(R1)` commits, `preserve(R2)` is adopted: no new
    resolution row, R2's own occurrences are inserted, the replay validator
    passes, and R1's resolution row is byte-identical.
23. Fault injection: a raise after the resolution insert and before commit
    leaves nothing. A crash after commit and before the preservation stage
    gives an `incomplete` receipt, and the next apply resumes it. A dead run
    is recovered through `recover_run`.
24. The policy file is changed between classification and apply:
    `resolution_policy_changed`, with nothing written.

E. adopt-mail plan and CLI (`test_mail_adoption_resolution.py`)

25. A dry run with a policy predicts exactly the apply outcome: every counter
    in section 8.3 is equal, including `resolvable_messages` against the
    adopted count, `resolvable_originals` against `resolved_originals`, and
    the per-code counts.
26. A dry run writes nothing. The ledger, intake catalog and every sidecar
    identity are unchanged.
27. Counter invariants (section 8.3) hold in dry run, apply and `after`.
28. An rfc822 receipt still reports `unsupported_rfc822_part`. When it is a
    dependent, its binding receipt still resolves. When a hand-edited policy
    makes it the binding receipt, it gets `resolution_binding_receipt_blocked`
    and exit 3.
29. An original that is in neither `originals` nor `held` gives
    `resolution_not_in_policy` and exit 3. A receipt whose only unresolved
    original is `held` keeps the accepted `original_scope_unresolved` and
    exit 0.
30. A v1 config is accepted and means no resolution. A v2 config with `null`
    behaves the same. A pinned but missing policy gives `mismatch` and exit 3.
31. A rerun after apply adopts 0 and resolves 0. Every binding and dependent
    receipt is `complete`, nothing is written, and the `resolution_after`
    digest is unchanged.
32. Under a concurrent root writer, apply waits or is refused under the
    existing lock, with no partial state.
33. Census: I includes unresolved originals that appear only in the rfc822
    receipt. Every containing receipt is listed for each original. The
    proposal follows the smallest receipt digest and smallest occurrence id
    rules, and is identical when the discovery order is reversed. Stdout has
    counts only. Nothing is written to the ledger, intake catalog or export.
34. Duplicate attachment bytes, end to end (the same fixture as 21): the dry
    run reports `resolvable_messages=1`, `resolvable_originals=1` and
    `resolvable_attachments=2`. Apply reports `adopted_messages=1`,
    `resolved_originals=1`, `resolution_after.rows = baseline + 1` and no
    failures. The rerun gives 0/0.
35. Shared original, both discovery orders. O is in R1 and R2 with
    digest(R1) < digest(R2), and the policy binds O to R1 with dependents
    [R2]. The dry run reports `resolvable_binding_messages=1`,
    `resolvable_dependent_messages=1` and `resolvable_originals=1`. Apply
    calls R1 before R2, adopts both, and writes one resolution row naming R1.
    The rerun gives 0/0. The fixture is built twice, with receipt file names
    chosen so that discovery order is R1,R2 once and R2,R1 once. Both runs
    give the same reports (ignoring run ids and timestamps), a byte-identical
    resolution row and identical occurrence rows.
36. Shared-original negatives:
    (a) R3 also contains O but is not named in the policy:
    `resolution_policy_receipt_set_mismatch` on R1, R2 and R3, nothing
    resolved, exit 3.
    (b) R1 also contains a held original: `resolution_binding_receipt_blocked`
    on R1, `resolution_dependency_blocked` on R2, nothing written for either.
    (c) A hand-edited cycle (R1 binds O1 and depends on O2, R2 binds O2 and
    depends on O1): `resolution_dependency_cycle` on both.
    (d) A fault injected into `preserve(R1)` during apply: R2 is never called,
    failure `resolution_dependency_failed`, nothing written for either. The
    rerun adopts both and resolves O.
37. Chain: R2 is a dependent for O1 (bound to R1) and the binding receipt for
    O2, with dependent R3. Apply order is R1, R2, R3, there are 2 resolution
    rows, and the dry run equals the apply.

F. Regression

38. All existing `tests/records/test_mail_adoption*.py`,
    `test_original_reconciliation.py`, `test_records_run_stages.py` and the
    T12 harness pass unchanged.
39. `tools/check_public_tree.py` reports findings=[], and
    `scripts/scan-secrets.mjs` passes.

G. Synthetic acceptance (`test_mail_adoption_resolution_acceptance.py`, in CI)

40. A single synthetic fixture that mirrors the TEST acceptance shape:
    - A pre-existing v1 local-capture resolution for an unrelated original L,
      so `resolution_baseline.rows = 1`.
    - Ra: an eligible new receipt with no unresolved originals.
    - Rb: the binding receipt for O1, and for O2, which appears in two parts.
    - Rc: a dependent for O1.
    - Rd: a receipt whose only unresolved original O3 is `held`.
    - Re: an rfc822 receipt that is a dependent for O1.
    - Policy: `originals={O1, O2}`, `held=[O3]`.

    Dry run: `eligible_new_messages=1`, `resolvable_messages=2` (binding 1,
    dependent 1), `resolvable_originals=2`, `held_originals=1`,
    `policy_originals_blocked=0`,
    `coded_gaps={original_scope_unresolved: 1, unsupported_rfc822_part: 1}`,
    `unaccepted_coded_gaps={}`, exit 0.
    Apply: `adopted_messages=3`, `resolved_originals=2`,
    `resolution_after.rows=3`. The L row is byte-identical in every column.
    The new rows are exactly {O1, O2}, each with the mail-export schema, the
    pinned policy hash, and the policy's binding receipt and occurrence.
    There is no row for O3. Rd and Re are unchanged coded gaps.
    Rerun: adopted 0, resolved 0, `resolution_after` digest unchanged, exit 0.

H. TEST acceptance (only after the implementation is reviewed by a different
model and G has approved the TEST policy)

This extends the existing runbook in docs/RECORDS-MAIL-ADOPTION.md. Report
numbers and digests only.

Baseline. The fixed expectations below (154, 36, 114, 1, 39) belong to one
specific historical baseline. That is TEST restored from the same pre-T4
backup that was used for the T4 acceptance at 039900b (t_082c94b0). It is
identified by its backup manifest hash, recorded privately with only the
hash reported. A post-T4 or post-T17 snapshot is not that baseline. On any
other snapshot, stop and derive the expectations from that snapshot's own
pre-flight and census instead of these constants.

Pre-flight (no policy pinned, v1 config): the dry run must report
`receipts=154`, `eligible_new_messages=36`, and
`coded_gaps={original_scope_unresolved: 114, unsupported_rfc822_part: 1}`.
If anything differs, stop. Then record, privately:

- B_mail: the `mail_messages` row count.
- R0: `resolution_baseline` `{rows, sha256}`, plus a private copy of every R0
  row.
- D0: the digest of all `originals` rows, `legacy_rows`,
  `occurrence_projection_rows` and `captured_legacy_*` occurrences.

Sets. Equality of sets is checked privately by comparing sorted sha lists.
Only counts and list digests are reported.

- I: the census inventory (section 8.1).
- P: the policy `originals`. Hd: the policy `held`. P ∪ Hd = I, and
  P ∩ Hd = ∅.
- E_r ⊆ P: the plan's resolvable originals. P \ E_r is
  `policy_originals_blocked` and must be 0, otherwise the run blocks.
- N = N_b + N_d: the resolvable binding and dependent receipts.
- S: the shas of `original_resolutions` rows that are not in R0 after apply.
  These are the originals newly resolved by committed receipt transactions.

Steps:

41. Census: report |I|, the shared and repeated counts, and proposed held by
    code. G approves. The policy is written privately with |P| + |Hd| = |I|.
42. Dry run with the policy: `receipts=154`, `eligible_new_messages=36`,
    `resolvable_messages=N` (= N_b + N_d), `resolvable_originals=|E_r|`,
    `policy_originals_blocked=0`, and
    `coded_gaps={original_scope_unresolved: 114 - N, unsupported_rfc822_part: 1}`.
    The remaining `original_scope_unresolved` receipts are the ones that
    contain held originals. `unaccepted_coded_gaps={}` and
    `resolution_baseline == R0`.
43. Apply: `adopted_messages = 36 + N`, `failures={}`,
    `resolved_originals = |E_r|`, `mail_messages = B_mail + 36 + N` (that is
    39 + N on this baseline, per the T4 runbook), and
    `resolution_after.rows = R0.rows + |E_r|`.
44. Table delta: every R0 row is byte-identical in every column. S == E_r,
    and S ∩ Hd = ∅. Every row in S has payload schema
    `original_resolution_mail_export_v1`, `policy_sha256` equal to the pinned
    hash, `capture_receipt_sha256` equal to the policy's binding receipt, and
    `capture_occurrence_id` equal to the policy's `binding_occurrence_id`.
    Nothing is asserted about rows in R0 except that they are unchanged.
45. Rerun: adopted 0, resolved 0, `resolution_after` unchanged, exit 0.
46. `quick_check` ok, FK violations 0.
47. D0 unchanged.

## 12. Implementation slicing (after T17)

Each slice is one PR, signed off, reviewed by a different model:

1. Policy class, mail payload read path and `append_mail_export`, with test
   groups A-C.
2. Backend resolution plan and transaction (D).
3. adopt-mail resolution plan (section 8.2), classification, config v2,
   counters, census (E and G), plus
   documentation updates to RECORDS-MAIL-ADOPTION.md and
   ORIGINAL-RECONCILIATION.md.
4. Backup closure addition (`resolution-policies/`).
5. TEST acceptance (H), with G's approval of the TEST policy. Live apply needs
   its own G decision.
