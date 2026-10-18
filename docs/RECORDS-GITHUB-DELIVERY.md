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

An operator can bind a separately reviewed callable through `run_once`. That
callable must already have authorized narrow dependencies and honor the immutable
request: one item maximum, notifications false, pruning false. This contract is
not a sandbox for arbitrary code and does not itself enforce adapter semantics.
Adapters must not daemonize or escape the supervised process group. They must not
load an unapproved broad configuration file through a new route.

Execution requires explicit opt-in and a supported Linux runtime. The child has
an absolute deadline and the parent independently enforces at most 120 seconds.
Timeout/failure returns stable status and never claims completion. Adapter output,
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
