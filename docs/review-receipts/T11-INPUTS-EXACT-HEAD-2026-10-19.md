# T11 composed-release inputs: exact-head Sol approvals (2026-10-19)

Retained copy of the exact-head review verdicts for PRs #142, #145 and #148 (review card t_81423d24).
The original lived only as an untracked worktree file `EXACT-HEAD-REVIEW.txt`; GitHub holds no review objects for these PRs.

- Source file: `EXACT-HEAD-REVIEW.txt` (untracked worktree file, left in place)
- Source sha256: `d1e052f27c9c319cc60206a1fefd5c14369ee716102ff2b03896ba2d8fb144c2` (6805 bytes)
- Retained on: 2026-11-16
- Retention only: this adds no new approval, re-review, merge or publication claim beyond the source text.

| PR | Approved head | Merged head | Match? |
|---|---|---|---|
| #142 | `29c0065c09b0b354f262710ea2df9b866ff1579a` | `29c0065c09b0b354f262710ea2df9b866ff1579a` | yes |
| #145 | `7544af311798a78e2046aa302201c4d3c2745117` | `7544af311798a78e2046aa302201c4d3c2745117` | yes |
| #148 | `039900bb55e023e67c1121663802280a15b1b1ad` | `0a2c2547deff8395932fd6234b703d7dc7772c59` | **NO: mismatch** |

#148 merged at `0a2c2547`, not at the Sol-approved `039900bb`. The delta is one signed-off merge commit (parents `039900b` + `31f4090`).
#148 merged head 0a2c2547: exact-head approval receipt NOT FOUND (merge-only delta from approved 039900bb).
A later review card body describes that merge-commit review, but no verdict line was located. The `039900bb` approval must not be read as approval of `0a2c2547`.

## Source text (verbatim)

```text
Independent exact-head review — t_81423d24
Reviewer: sol-reviewer / gpt-6.1-sol. Authors: non-Sol (Opus/DeepSeek implementation lineage identified in PR descriptions; task labels codex/opus). No implementation edits, merge, live processing, email, external notification or publication performed.

Grahammmm/DeFlock-Campaign-Tool#142
APPROVED at 29c0065c09b0b354f262710ea2df9b866ff1579a
Scope: mail-history adoption and synthetic acceptance-script changes.
Reviewed default dry-run, explicit owner-only enable config, disable-marker hash binding and recheck under root lock, canonical receipt/occurrence identity and CAS validation, durable incomplete recovery, no-op replay, and G option-c coded gaps. Adoption has no mailbox-connect/send path. Accepted gaps are limited to original_scope_unresolved and unsupported_rfc822_part; unaccepted gaps/conflicts remain blocking report outcomes. Blocking is per identity: eligible receipts can still be adopted while other identities are reported as conflicts; this is not an all-or-nothing batch guarantee.
Fresh detached exact-head checkout:
  python3 -B -m unittest -v tests.records.test_mail_adoption tests.records.test_mail_adoption_bindings tests.records.test_mail_adoption_coded_gaps tests.records.test_mail_adoption_recovery tests.records.test_mail_adoption_regression tests.records.test_verify_adoption_test_script
  Ran 93 tests in 85.907s; OK (skipped=2: optional native MSG parser unavailable).
  python3 -B -m unittest tests.records.test_mail_delta tests.records.test_mail_wire_intake tests.records.test_mail_related
  Ran 94 tests in 39.362s; OK (skipped=3).
  python3 -B tools/check_public_tree.py: 669 tracked files; findings=[] (pattern coverage only).
  node scripts/scan-secrets.mjs: PASS; forbiddenPathCount=0; credentialValuesPrinted=false.
  git diff --check: clean; exact-head checkout status clean.
The first concurrent focused attempt exceeded its 180-second bound; it was not counted as a pass. The complete retry above passed. Counts are fresh results, not the historical 172-test count in the PR narrative.

Grahammmm/DeFlock-Campaign-Tool#145
APPROVED at 7544af311798a78e2046aa302201c4d3c2745117
Scope: runtime templates, stage orchestration, run-report v1 schema/validator, timer renderer.
Reviewed runtime-wide nonblocking flock ownership through work and notify, exit-73 skip receipt without queuing, bounded Docker readiness, whole-process-group cleanup, stop/continue stage policy, schema-validated eight-stage reports and atomic report publication, and notify failure remaining nonfatal to work exit. Renderer fixes 07:00/13:00/20:30 America/Los_Angeles and refuses invalid budgets, unknown/missing/unsafe values and Git-contained output. No installation or timer enablement performed.
Fresh detached exact-head checkout:
  python3 -B -m unittest tests.test_records_runtime tests.test_records_run_report tests.test_schema_files tests.test_public_tree tests.test_provenance_scan tests.test_secret_report tests.test_campaign_install_resources
  Ran 92 tests in 80.030s; OK.
  python3 -B tools/check_public_tree.py: 675 tracked files; findings=[].
  node scripts/scan-secrets.mjs: PASS; forbiddenPathCount=0; credentialValuesPrinted=false.
  sh -n deploy/records-runtime/run-records.sh.in: PASS.
  dash -n deploy/records-runtime/run-records.sh.in: PASS.
Known documented semantics retained: notify failure rolls overall_status to fail although work exit can be zero. Schemas are sourced from the checkout; approval is not a wheel-resource or deployed-runtime qualification.

Grahammmm/DeFlock-Campaign-Tool#148
APPROVED at 039900bb55e023e67c1121663802280a15b1b1ad
Scope: admitted T11i/T11j stage-adapter/TEST-plan/legacy-allowlist delta. NOT approval of TEST/live acceptance or completeness of extraction.
Inspected git diff 7544af311..039900bb5 and separated inherited changes from the stage-adapter, generator/template, harness, renderer-placeholder and legacy-allowlist additions. The raw range includes inherited mail adoption, portal and other mainline commits, so it is not literally a unique-files-only diff.
TEST template fixes mode=test and uses explicit project/config/interpreter placeholders. Harness constructs a sandbox copy from records-project-TEST; real data/container driver was NOT executed by this review. Portal stage performs allowlist-gated intake followed only by fetch dry run. Legacy allowlist mapping preserves root ownership, expiry, named hosts and constrained paths; it does not edit or add hosts to the deployed allowlist. Coded adoption gaps remain visible with fixed codes; conflicts/unaccepted gaps fail adoption. Count-only reads refuse pending WAL/journal without creating sidecars, child execution has container-side limits and group cleanup, and results contain counts/fixed codes rather than record text. No live stage plan is hardcoded.
Fresh detached exact-head checkout:
  python3 -B -m unittest tests.test_records_run_stages tests.records.test_portal_legacy_approval tests.records.test_flock_e2e_harness tests.test_records_runtime tests.test_records_run_report
  Ran 114 tests in 82.668s; OK.
  python3 -B -m unittest tests.test_records_portal tests.test_records_portal_https tests.test_records_portal_repairs tests.test_records_portal_runner_contract tests.test_records_portal_wp1 tests.records.test_portal_intake tests.records.test_portal_legacy_approval
  Ran 130 tests in 35.986s; OK.
  python3 -B tools/check_public_tree.py: 721 tracked files; findings=[].
  node scripts/scan-secrets.mjs: PASS; forbiddenPathCount=0; credentialValuesPrinted=false.
  git diff --check: clean; exact-head checkout status clean.
Existing findings are not re-litigated: extract at this head only inventories backlog; the host harness can accept repeated equal nonzero exits and lacks strict advancement/fault assertions. T11k t_386e5f18 and T12s t_eb04d4a1 own those release-gated improvements. The stale harness inherited from #144 is superseded through the separately approved #144 head f48e3c4ac3fda599def11451dcf9f51158375ecf / t_f00469ee. A composed release still needs those dependent approvals and composed-head review. This scoped approval must not be cited as evidence that three successful real-stage runs or live readiness were verified.

Release handoff
All three remote PR heads were re-read and matched the pinned SHAs; #148 remains draft. These are one consolidated verdict per PR recorded on the board, not GitHub-published reviews. The child t_f02da880 owns merge order, undrafting and required CI verification on each exact head immediately before merge. This task performs no merge and does not attest current CI. Machine-valid output is review-required, not semantic acceptance. Public-tree/secret scanners provide bounded pattern coverage, not a proof of absence of every secret.
```
