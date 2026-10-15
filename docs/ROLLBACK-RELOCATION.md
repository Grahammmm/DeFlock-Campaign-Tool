# Publication rollback after relocation

A synthetic restored-ledger probe on main1db1e9b3 showed that publication authority
recovery used prepared journal absolute target paths before rollback validated
the supplied staging target. A writable restored clone could therefore modify
the original synthetic staging tree before refusing its isolated target.
Generic backup verify/restore remained inert; the unsafe crossover was subsequent
publication recovery, not archive extraction.

## Fail-closed boundary

Publication authority now binds its private canonical ledger to the original
absolute runtime root in the existing ledger_meta key
`publication-recovery-root-v1`. The binding is created only while the ledger
has no publication journal history and the root/SQLite writer boundary is held.
It never updates an existing binding or changes immutable journal bytes.

Every publication authority entry checks the binding under the root lock using
a read-only ledger connection before schema initialization, proposal recovery,
historical target discovery, target locks/directories or output writes. The
binding is checked again inside the writer transaction. A copied ledger retains
its original root binding and refuses at another root with
`publication_root_binding_mismatch`. Existing journal history lacking a binding
refuses with `publication_root_binding_required`; it is not silently enrolled.

This is path confinement for this publication component, not independent runtime
authorization, package rollback qualification or protection against an owner
deliberately rewriting private ledger authority. A separately reviewed relocation
adapter/target mapping is still needed to run restored publication elsewhere.
Do not edit the key or historical journals to make a restored clone pass.
Retirement, revocation and provenance guards remain in place.

## Synthetic regression scope

The new test reuses the supplied closure probe's existing RecoveryV2 and
PublicationFixture builders: interrupt rollback after current.json, snapshot
the publication ledger, verify/restore a two-database backup, and copy it into an
explicit writable clone with a separate staging tree. The clone retains pending
retirement and revoked approval state. Two rollback attempts and an approval
attempt must refuse before target locks; original/isolated output bytes and
source/clone ledger rows must stay unchanged, including journals and provenance.

A second test removes the binding only from a generated fixture to represent
legacy unbound history; refusal must neither enroll authority nor change output.
Existing same-root correction/rollback and interrupted recovery remain valid.
No live stores, mounts, credentials, model calls, backup jobs or activation.
