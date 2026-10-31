> Historical milestone text (last changed 2026-10-22). Current status: [STATUS.md](STATUS.md).

# T4B slice 1 hardening and integration boundary

This is a fixture-only library slice, not permission to apply any policy or run
mail adoption. The design remains in RECORDS-LEGACY-MAIL-RESOLUTION.md. No backend,
CLI, policy-approval process, canonical store, migration SQL or schema pin is
changed here. Machine-valid evidence still requires independent review.

## Explicit trust context (F-01 / F-02)

`append_mail_export(..., output_root=...)` requires the trusted intake output root.
`effective_original(con, subject, output_root=..., mail_policy=...)` requires both
that root and the trusted loaded policy for a mail-export resolution. Neither is
inferred from `payload_json`, occurrence evidence, the database location or a
row-selected policy filename. Root-relative receipt paths, the policy identity,
entry binding and the imported legacy row are rechecked at read time.

The policy passed to a reader is the policy that created the immutable row. A
successor policy does not rewrite that identity. Future integration must retain
trusted historical policy identities/artifacts when supporting successor policy
planning, and route that trusted context through all consumers. Merely loading a
policy chosen by a row is not a substitute for trusted authorization.

Until separately reviewed integration supplies this context, existing generic
store readers fail closed on mail-resolution rows. They continue reading local
capture v1 and unresolved declarations unchanged. No real mail rows are admitted
by this slice. Backend MIME/CAS acquisition verification remains mandatory and is
not replaced by the receipt-digest check in this reader (F-11 is by design).

## Other audit closure

- F-03: check actual table/index/trigger SQL against SQLite's representation of
  the unchanged migration on every installed-schema check. A surviving marker
  does not excuse missing or replaced immutability/gate/marker triggers.
- F-06 / F-08: policy state is read-only, slot-backed and transitively immutable;
  admissions use the cached validated entries and return independent dict/list
  copies. Reinitialization is refused. This is an application invariant, not a
  sandbox against arbitrary Python runtime replacement or `object.__setattr__`.
- F-04: a savepoint covers insert and final readback. A caught failure cannot
  leave a committable rejected row, and unrelated outer work remains intact.
- F-07: SQLite insertion failures, including a stale deferred writer, become
  `original_resolution_write_failed` after savepoint rollback. The backend must
  still use its root lock and BEGIN IMMEDIATE; `in_transaction` alone does not
  prove that contract.
- F-05 / F-10: no relative or dotdot file reads; no noncanonical storage spelling.
- F-12: account/folder identities are 1–1024 Unicode code points with no Unicode
  C-category characters; UID/UIDVALIDITY are non-bool integers in 1..2**32-1.
  Ordinary Unicode names retain byte-exact identity (no normalization).
- F-13 / F-14: strict coded JSON decoding and canonical stored mail payload text.
- F-09: the existing 2 MiB and 10,000 combined entries/held limits are simultaneous
  upper bounds, not a promise that 10,000 full entries fit in 2 MiB. No cap raised.

## Tests and provenance

The Claude-authored synthetic audit repros were taken from
`11f108e614367e9fcc487179bcdd4a06765a811e`, originally
`tests/t4b_adversarial/test_{policy,resolution}_adversarial.py`. They are regular
regressions in `tests/records/test_t4b_*_adversarial.py`, with no opt-in environment
gate. Sol adaptations supply explicit context, restore triggers after deliberate
row corruption to isolate the deciding gate, assert fail-closed outcomes rather
than the formerly accepted malicious overlay, and replace timing thresholds with
a deterministic no-reparse check. `test_mail_resolution_audit_closure.py` adds
independent trust-context, binding, trigger-replacement, immutable-state,
catch-and-commit rollback and identity-boundary checks on the real legacy schema.

Original leaf/ancestor symlink cases now target valid owner-only policy bytes or
byte-identical receipt bytes. Offline fresh-process controls deliberately disable
O_NOFOLLOW (both original test groups), root comparison, policy comparison,
trigger verification, frozen state, savepoint rollback and cached admissions.
Positive controls must pass and every bypass must cause assertion failures.
These controls are self-review evidence, not an independent review verdict.
