# Develop the records pipeline in GitHub

The reusable engine, synthetic tests, architecture and issue-level acceptance
plans belong in this public repository. Contributors can review the same pinned
commit and propose changes through pull requests. Adding a collaborator or
granting an external service access is a separate owner action.

Campaign originals, private analyses, mailbox settings, reviewer identities,
deployment receipts and operational logs stay outside Git. Public CI uses only
synthetic inputs. A source merge is not installed adoption, a successful model
call is not independent review, and a passing fixture is not a real-corpus count.

## Delivery slices and dependencies

| Slice | Existing starting point | Deliverable and acceptance | Depends on |
| --- | --- | --- | --- |
| Installed intake | #4; records run / mail delta | Versioned installed invocation manifest and a separately authorized one-message receipt. Test UIDVALIDITY changes, omitted folders, replay and failure checkpoints synthetically. Never expose mailbox settings in an issue. | Reviewed runtime and operator-bound adapter |
| Durable originals | EML export and ledger | Fault-inject write, fsync, publication and identical retry; preserve originals and delivery occurrences. An interrupted scan cannot improve coverage. Conflicting bytes must fail closed. | Intake identity contract |
| Extraction and OCR | Extraction routes and OCR modules | Pinned parser/runtime manifest, page/sheet/row locators and explicit unsupported/low-confidence gaps. Test native and image-only synthetic fixtures; real-input coverage needs a separate private receipt. | Preserved originals |
| Local classification | Catalog / digest | Versioned local transport and redaction contract; test proxy/redirect refusal, malformed output and evidence-linked labels. No raw-record cloud routing is enabled by this plan. | Extraction and transport qualification |
| Evidence-linked review | #5; review bundles and gates | Authenticated independent reviewer receipt bound to exact evidence/public bytes; test malformed responses, revocation and provenance-only changes. A model or structural check stays non-independent. | Catalog and event-date policy package |
| Publication | #6; staging publisher | Build approved bytes, privacy-scan and stage a versioned artifact. Test correction, revocation, crash/replay and rollback. Live publication remains a separate explicit approval. | Independent review and privacy clearance |
| Backup | Recovery backup modules | No-prune create/verify interface, immutable manifests and isolated synthetic restore. Cover referenced originals, write stores and SQLite sidecars. Production destination identity and retention deletion each need their own qualification. | Storage manifest and operation closure |

Implement and review one compatible slice at a time. Link issues/PRs to exact
commit, command, fixture hashes and results. Reuse equivalent accepted evidence;
do not restart exhausted acceptance cycles or repeat unchanged suites for handoff.

## Bounded resume boundary

`python3 -m campaign_tool.records.bounded_resume` is inert plan mode.
`--execute` without an operator-bound adapter fails closed. There is no worker
CLI switch, configuration discovery, credential-file parser, adapter import path
flag, live mail implementation, notification sender or retention operation here.

`python3 -m campaign_tool.records.contained_resume` is also inert plan mode;
`--execute` refuses with `authorized_adapter_required`. Its Python API,
`run_contained(AdapterIdentity(path, sha256, attribute), execute=True,
seconds=120, run_parent=...)`, requires an absolute adapter path and a private,
owned 0700 run parent without symlinks. Linux, a non-root UID and working pidfds
are required. The adapter is captured once through a no-follow regular-file
descriptor (current UID, one link, at most 256 KiB) and hash-checked before launch.
An owned 0600 binding carries those bytes; replacing the adapter path cannot
change what executes. Captured bytes bind only this module, not its imports.

An operator can bind a separately reviewed callable through `run_once`. That
callable must already have authorized narrow dependencies and honor the immutable
request: one item maximum, notifications false, pruning false. This contract is
not a sandbox for arbitrary code and does not itself enforce adapter semantics.
Adapters and their dependencies must spawn **no child processes**, even ordinary
children in the same process group. Standalone `run_once` reports only
`leader_completed`, with `completion_claimed: false` and `quiescence: unproved`.
Its leader wait polls waitpid-backed state, not an inherited sentinel pipe.
The contained path composes the existing pidfd/subreaper `worker_lifecycle`
supervisor rather than adding a second supervisor. Descendant cleanup is a
safety net, never permission for an adapter or its dependencies to spawn.
Adapter admission must include a static dependency review and synthetic evidence
of this no-child restriction. Adapters requiring subprocesses are inadmissible to
this boundary and need a separately qualified runner. Adapters must not
load an unapproved broad configuration file through a new route.

Execution requires explicit opt-in and a supported Linux runtime. Standalone
execution enforces at most 120 seconds; the contained wall budget is 6–120 seconds
plus two-second TERM and KILL graces and a five-second parent margin.
Timeout/failure returns stable status and never claims completion. Both OS output
file descriptors and Python output streams are redirected to the null sink.

The internal `__supervised_worker__ <absolute binding path>` entry first verifies
its parent PID and live identity against the supervisor's validated private
state.json. A direct invocation without that binding exits 3 before loading
adapter bytes. The captured module is re-hashed, compiled and executed only
inside the existing fork fence, after setsid, alarm and output redirection.
Only a persisted receipt matching the supervisor stdout, state identity, boot
and run ID can establish `completed`: worker exit zero AND `quiescent: true`
are required. Fixed result keys are `status`, `completion_claimed`, `quiescent`
and `escalated`; absent or inconsistent evidence never claims completion.
Parent timeout returns `not_quiescent`, not cleanup proof. Supervisor SIGKILL
or crash containment remains a P1 deployment hold; this is not independently
stoppable exec-only deployment. Mechanical completion is not semantic acceptance.

Adapter output,
exceptions and payloads are not returned. An interrupted operation can still have
partial immutable writes; its adapter must supply idempotent recovery receipts.

The boundary does not update a schedule, remove a failure marker, migrate a
datastore, resume a production service or grant publication authority. Reverting
the new source/ceasing to use the boundary leaves existing invocations unchanged;
preserve any accepted originals and receipts rather than deleting them to roll back.

## Qualification order

1. Seal the exact source/test/doc commit and obtain independent static review.
2. After approval, run one qualified Linux synthetic invocation:
   `python3 -B -m unittest tests.records.test_bounded_resume -v`.
3. Preserve failures and stop on the first unexpected failure. No live adapter,
   private input, credential read or production write is part of that gate.
4. Run normal required PR checks once on the final candidate. A green PR does
   not authorize deployment or private-input execution.
5. Admit a specific adapter only after its credential boundary, write effects,
   recovery behavior and operator authority are reviewed separately.

The source boundary is only an initial delivery slice. Durable legacy adoption,
real OCR/model routing, independent review, publication and backup qualification
remain tracked work, not completed features because this document exists.
