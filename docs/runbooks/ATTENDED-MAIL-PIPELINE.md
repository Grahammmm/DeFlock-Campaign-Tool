# Attended mail intake: runbook and readiness matrix

No agent runs any step.
Credentials are typed only by G or the custodian.
A guard refusal means STOP; keep the exact text.

This is an attended operator reference, not permission to execute it. Every step
except private reporting is currently HELD. READY means only that this row's
machine-checkable evidence cells are met: it is not semantic, legal, publication
or live-run acceptance. A card marked DONE or APPROVED supplies no evidence cell.

## How to read this reference

The companion [matrix](mail-readiness-matrix.json) uses schema
`mail-readiness-matrix-v1`, with ordered row keys under `rows`. Each row owns its
tools, gate evidence, backup receipt, rollback rehearsal and authority. No row's
verdict fills another row. Tool paths bind to the reviewed repository head;
private attended matrix copies may additionally pin a lowercase SHA-256 digest.
UNSUPPLIED means the named owner must supply the prerequisite, not an invitation
to infer it. Tool presence alone is never approval of inputs or a live action.

For the offline checker, the interface is `tools/mail_readiness_matrix.py` with a
matrix argument and optional `--runbook` argument. It emits counts and fixed codes
only. Exit zero means structurally valid, even when every action is HELD. The
committed matrix reports 14 rows, one READY (S10), and 13 HELD.

Only top-level sh fences below contain command templates. One command per line;
no chaining, substitutions, redirects or credential editing. Work in the reviewed,
pinned engine checkout. Angle-bracket tokens denote operator-supplied absolute
private paths: replace the entire token with the actual path, safely quoted.
Do not paste angle brackets into a shell. `EXPORT_MANIFEST_SHA256` is a literal
replacement marker for the digest printed by the first S3e command, not a shell
variable or a private path. Replace it manually; do not invent a digest.

The displayed commands document existing interfaces only. Do not execute even
an offline command until the row's attended gates are supplied. The builder and
reviewer exercise only synthetic tests and the offline readiness checker.

Global STOP conditions: any guard refusal, missing cell, changed tool identity,
source mismatch, missing exact-action approval, ambiguous output, credential
prompt for an agent or unresolved prior STOP. Keep exact refusal text privately;
never retry a guard refusal. No new download, IMAP access, send, timer swap,
service/container restart, installation or network change is authorized here.
Canonical stores, extraction, OCR and source acquisition stay with the Flock
coordinator; the bounded Qwen queue keeps its admitted exports and raw receipts.
No duplicate jobs, ledger writes, deletions or off-box export are authorized.

## S2 — Read-only server census probe
Matrix row: S2

Purpose: capture server UID census without fetching bodies or headers.
HELD: operator must supply M-01 reviewed probe.
What the operator types: no command is available.
STOP until C-1 through C-8 discovery, window and time bound are posted and G says
PROBE at the console for this capture. The future bound is the smaller of 900
seconds and seconds to the next slot minus 1200. A failed or partial probe is not
coverage. Status hashes before and after must match; custodian confirms lock
release and no sync process. Credentials: G or custodian only, with G present.
Backup: none for a read-only probe. Rollback: no store writes to undo; retain
partial evidence and never infer server nonmutation from local hashes.

## S3 — Consistent snapshot copy
Matrix row: S3

Purpose: create a consistent frozen copy without touching the live root.
HELD: custodian must supply M-06 archive, member and manifest pin.
What the operator types: no repo-executable stager is supplied; packet membership
is not a custodian pin or execution authority.
STOP until capacity preflight gives source bytes and destination free bytes,
with free space at least twice source size plus one GiB. Use a window starting
at least 40 minutes after the previous slot, bounded to 1200 seconds, ending at
least 20 minutes before the next. Stable before/after status hashes and the
stager's copy hashes are required. Changed source or failed copy means quarantine
and an operator-owned later-window decision, never an agent retry.
Credentials: G or custodian only; none should be needed for a local copy.
Backup: live root untouched. Rollback: quarantine the copy, never delete it.

## S3e — Selected preserved-export intake (W12)
Matrix row: S3e

Purpose: bind exactly the selected preserved receipts to one export manifest.
HELD pending the custodian's sanitized source contract, exact backup receipt,
rollback rehearsal and separate G exact-action approval for this live intake.
The operator would obtain the digest first, then substitute that printed digest
for `EXPORT_MANIFEST_SHA256` in the second command. Neither is run by an agent.

```sh
python3 -B -m campaign_tool.records.container_host export-manifest --exports </private/selected-exports> --export-receipt </private/export-receipt.json>
python3 -B -m campaign_tool.records run --unattended --root </private/admitted-records-root> --export-root </private/selected-exports> --export-receipt </private/export-receipt.json> --export-manifest-sha256 EXPORT_MANIFEST_SHA256
```

The empty collection-host scope is deliberate: no body-link retrieval is granted.
Every body link stays a visible gap, not a silently satisfied receipt. STOP on
unselected receipts, changed manifest, source identity mismatch or absent scope.
Credentials: G or custodian only; no mailbox config is read for export mode.
Backup: exact destination backup plus immutable selected source receipts.
Rollback: custodian's rehearsed attended destination recovery; sources unchanged.

## S4 — Store verifier and comparator
Matrix row: S4

Purpose: compare the bound census and verified frozen store, as of one capture.
HELD: operator must supply M-02/M-03 reviewed interfaces.
What the operator types: no command is available.
STOP without S3 manifest, status hashes A/B and bound census inputs. No broad
coverage or server-nonmutation claim is justified by matching local hashes.
The optional `rollback_rehearsal` cell here carries custodian-owned M-31 server
nonmutation evidence (no restore is needed for this read-only comparison).
If every required cell and tool is met while M-31 is UNSUPPLIED, the exact label
is `as of one capture; nonmutation UNSUPPLIED (M-31)`; there is no stronger verdict.
Credentials: G or custodian only; none needed offline. Backup and rollback: none
for read-only frozen inputs. Retain coded failures and route source facts to the
custodian; do not use a comparison failure to authorize fallback F1.

## S4b — Historical coverage L-B
Matrix row: S4b

Purpose: assess historical receipt coverage and separately attested analytical
coverage. Tool status is mixed: the verdict checker exists, packet inventory
is NON_EXECUTABLE here. HELD: custodian must supply M-26 pinned packet and
provenance, plus M-29 frozen analytical baseline runtime attestations.

```sh
python3 -B tools/historical_coverage_check.py --diff </private/historical-diff.json> --attestation </private/historical-attestation.json> --output </private/new-coverage.json>
```

STOP on absent per-root producer statements, unbound diffs or missing runtime
attestations. Do not relabel accepted historical runs. Preserve historical-only
receipts and count unkeyed, changed, held-provenance and unprocessed groups.
Credentials: G or custodian only; none needed offline. Backup: none, inputs are
read-only. Rollback: retain the new report privately; historical roots unchanged.

## S5 — Decode inventory and portal reconciliation
Matrix row: S5

Purpose: inventory decoded receipts and compare portal pointers with an offline
queue export. M-09 exists, M-07 does not. HELD: operator must supply M-07 reviewed
decoder inventory and interface-compatible bound observations plus S4 evidence.

```sh
python3 -B tools/portal_reconcile.py --observations </private/portal-observations.json> --queue-export </private/queue-export.json> --output </private/new-portal-reconcile.json>
```

STOP if observations or the custodian export do not match the reviewed M-09
schema. The current CLI is not the proposed expanded decoder/reconciler contract.
No portal fetch, queue update or allowlist access is performed by this command.
Count held, failed and unproven pointers without dropping them.
Credentials: G or custodian only; none needed offline. Backup: none, read-only
inputs. Rollback: new report only; queue and source roots unchanged.

## S6 — Adoption dry run on a frozen -TEST copy
Matrix row: S6

Purpose: qualification on copies only, using the pinned engine's adoption verifier.
HELD until the custodian supplies engine pin, owner-only config and frozen
mail-history plus processing-v1 copy ending in -TEST, with its receipt.

```sh
python3 -B scripts/verify_adoption_test.py --test-root </private/frozen-TEST> --config </private/owner-only-adoption.json> --receipt </private/new-test-receipt.json> --dry-run-only
```

STOP if the engine checkout is not pinned, TEST receipt is absent or verification
fails. Reconcile expected coded gaps with the frozen baseline (116 or explained
drift); the tool's default expectations are not an attestation of this corpus.
Post only the coded-gap histogram and counts, not receipt contents. T14 is an
extract-only tool, not this adoption dry run; its real-corpus STOP is not replayed.
Credentials: G or custodian only; agents never read the owner-only config.
Backup: TEST copy only, output receipt outside it. Rollback: no apply, live root
untouched; retain the TEST evidence privately.

## S7 — Engine pin and portal enable
Matrix row: S7

Purpose: check a run report as one part of an attended, E-bound portal enable.
The report checker exists, but the live apply driver, timer-gate CLI and
DATA-restore CLI are UNSUPPLIED. HELD: custodian must supply reviewed live driver
and attended recovery invocation; this offline report command does not enable it.

```sh
python3 -B tools/run_report_check.py </private/run-report.json> --baseline </private/reviewed-baseline.json>
```

The baseline is a reviewed, operator-supplied private JSON input, not a file
bundled with this repository. Replace its entire angle-bracket token with the
safely quoted absolute path; STOP if that baseline has not been supplied.

STOP without reviewed T2/T5/T9 evidence, pre-stop GO, exact engine pin, backups,
restore rehearsal and G-log 468 exact scope with G present. No live invocation is
invented here. T13 is an offline synthetic API, not a live scheduler driver.
Credentials: G or custodian only. Backup: matching host/container/SSD data hashes
and LIVE.pre-E.json. Rollback: custodian's attended plan and DATA restore, then
baseline comparison. No automatic DATA restore exists. No timer or service
commands are supplied or authorized by this runbook.

## S7r — Contained one-item resume (W13)
Matrix row: S7r

Purpose: inspect plan mode only. No adapter is admitted. Real adapter execution
needs its own admission, scope, backup and rehearsed rollback; this row stays HELD.

```sh
python3 -B -m campaign_tool.records.contained_resume
```

STOP if any real adapter or side effect is requested. A plan-only response does
not close source gaps or release a live hold. Credentials: G or custodian only;
none needed for the empty plan. Backup and rollback: separately supplied by the
custodian/operator for any future admitted adapter, never inherited from S7.

## S8 — Live legacy-resolution session
Matrix row: S8

Purpose: compare live outcomes to a reviewed rehearsal vector V.
HELD: operator must supply M-11/M-12 reviewed rehearsal invocation.
What the operator types: no command is available. T14 cannot produce this vector.
STOP without policy approval, apply-holder barrier, rehearsal V, data/plan backup,
rehearsed restore and G-log 468 exact scope with G present. G accepts V or orders
rollback; a mismatch is not permission to adjust the target. No replay of T14's
real-corpus STOP. Credentials: G or custodian only. Backup: session-bound data
and plan. Rollback: attended rehearsed restore when live outcomes differ from V.

## S9a — Comprehension TEST qualification F-047
Matrix row: S9a

Purpose: qualify comprehension on TEST copies only after F-046 wiring.
HELD: operator admission must supply reviewed F-047 runbook after F-046.
What the operator types: no command is available.
STOP without reviewed F-047 acceptance, operator admission and TEST receipts.
NOT_ESTABLISHED is evidence, never positive qualification. No TEST result supplies
S9b authority. Credentials: G or custodian only. Backup: TEST copies only.
Rollback: no live flags changed; retain TEST qualification evidence privately.

## S9b — Comprehension LIVE activation F-048
Matrix row: S9b

Purpose: separately attended LIVE activation, one flag per approved session.
HELD: operator admission must supply reviewed F-048 runbook.
What the operator types: no command is available.
STOP until a separate explicit G-attended exact-action approval names this
activation; neither G-log 468 nor 527 nor positive S9a authorizes it. Its own
matrix authority must be MET and name `separate G exact-action approval:` followed
by the private approval reference. This checks shape, not the truth of approval.
F-003 checks, session evidence and human acceptance remain necessary.
Credentials: G or custodian only. Backup: data and plan. Rollback: rehearsed
restore, flags back to false and the next run compared to baseline.

## S10 — Private plain-English report to G
Matrix row: S10

Purpose: explain what moved, what is held and why, and what is next. READY for
reporting shape only. What the operator types: no tool or command is needed.
Reports carry counts only and nothing is published. No raw text, addresses,
paths, subjects, credentials or private source identifiers go into a report.
STOP if a report would disclose private data or claim unsupported acceptance.
Credentials: G or custodian only; none needed. Backup and rollback: not applicable,
no data mutation. Publication still needs redaction and independent review gates.

## F1 — Isolated-root fallback pull
Matrix row: F1

Purpose: a separately bounded fallback, never automatic after a failed comparison.
HELD: operator must supply M-05b reviewed parameterisation.
What the operator types: no command is available.
STOP until the custodian supplies gap evidence, isolated-root scope, size/time
budgets, backup and rollback rehearsal, and G gives its own separate exact-action
fallback approval after M-05b. S2 PROBE and S4 outcomes supply no F1 authority.
No new pull or acquisition is authorized here. Credentials: G or custodian only.
Backup: canonical root untouched, exact isolated-destination receipt required.
Rollback: attended quarantine of isolated output, never delete or mutate canonical
stores. Missing source facts remain with the custodian.
