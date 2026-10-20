# FL-4: frozen open-PR triage against Phase 1

Status: author recommendation; independent review required. No PR was merged,
closed, rebased or marked ready by this audit. G owns any eventual closure.

Release: `602814408e255b3e0649d4c303570f465a6f2aad` (not moving `main`).
Task: `t_c1f01547`. Admission: F3 and Part 2 FL-4 of the October 20 Fable audit;
source SHA-256 `41fdd644cefe0138ecd6fdb6cc734b56054bf3cc0d488c3e927a40f84595e842`.
Only public repository metadata/source comparisons are reproduced here; the
private operational audit itself is not copied.

Snapshot: 2026-10-20T13:13:06.928776+00:00. The F3 cohort is exactly 14 PRs, even though the live
open list contained 15 at capture. Grahammmm/DeFlock-Campaign-Tool#161 is the
additional PR and is outside this admission; it has not replaced a cohort row.
All 14 cohort PRs were still OPEN. “Draft/ready” below is GitHub's draft flag,
not readiness for merge. Full head/base IDs, paths, blob-equality partitions,
integration commit IDs and captured checks are in [evidence.json](evidence.json).

## Method and verdict meanings

For each captured PR head `H`, compute `B = git merge-base --all 6028144 H`.
There was exactly one base for each PR. The overlap column is the intersection
of net changed paths from `git diff --name-only B H` and
`git diff --name-only B 6028144`. This is a conservative release-side overlap
since that PR's common ancestor, including earlier integrated engine work; it
is **not** just the files in the last release merge, a three-way textual conflict
prediction, or a claim that every overlapping file was authored during Phase 1.
An empty intersection does not establish behavioral compatibility. Equal blobs
are compared at the captured head and pinned release, separately from overlap.

- **supersede**: the identified merged successor already represents the draft's
  payload; do not merge the old draft again. This does not close architectural,
  deployment or semantic-review holds. G can close with the successor cited.
- **rebase**: retain the distinct work and refresh/reconcile it against the
  current integration branch before normal exact-head CI and different-model
  review. This category includes clean additive/doc patches that may not need a
  literal rebase. It grants no retry, live-run or merge authorization.
- **close**: recommend G retire the open proposal from this release queue for
  the stated scope/staleness reason. This is not a finding that its unique work
  was implemented. Preserve the head/branch as historical evidence.

Summary: 8 supersede, 4 rebase/retain, 2 close recommendations.

## Per-PR table

All references are in `Grahammmm/DeFlock-Campaign-Tool`. Short hashes below are
12-character display prefixes; evidence.json preserves the exact 40-character IDs.

| PR (captured draft flag) | Head | Merge base vs 6028144 | Overlapping release-side files (exact paths) | Verdict and one-line reason |
| --- | --- | --- | --- | --- |
| Grahammmm/DeFlock-Campaign-Tool#14 (ready) | `9e27472cceeb` | `7557689cc8db` | `.github/workflows/offline.yml`; `README.md` | **close** — Out-of-scope social renderer with new paid-provider adapters and CI dependencies; preserve the branch for a separately scoped publication/privacy review, not Phase 1 integration. |
| Grahammmm/DeFlock-Campaign-Tool#39 (draft) | `efc8167fd2bf` | `936ea52118a8` | None | **close** — Historical October 1 audit packet is absent from the release, not implemented elsewhere; retain as a dated reference rather than merge stale current-state/build instructions. |
| Grahammmm/DeFlock-Campaign-Tool#80 (draft) | `e69e619b1916` | `39b9242d7f19` | `campaign_tool/records/correspondence.py`; `docs/CORRESPONDENCE.md`; `skills/flock-correspondence/SKILL.md`; `skills/flock-correspondence/references/workflow.md`; `tests/records/test_correspondence.py` | **supersede** — All five touched blobs match the release; merged successor Grahammmm/DeFlock-Campaign-Tool#93 already carries the source-only correspondence slice. |
| Grahammmm/DeFlock-Campaign-Tool#81 (draft) | `86135704d5a8` | `39b9242d7f19` | `campaign_tool/records/semantic_coverage.py`; `tests/records/test_semantic_coverage.py` | **supersede** — Both touched blobs match the release via Grahammmm/DeFlock-Campaign-Tool#94; range bookkeeping is not semantic acceptance or complete runtime coverage. |
| Grahammmm/DeFlock-Campaign-Tool#82 (draft) | `95d0ebb6ef65` | `39b9242d7f19` | `runner/handlers/build_site.py`; `tests/runner/test_build_site_contract.py`; `tests/runner/test_build_site_envelope.py`; `workers/workspace/src/manifest.ts`; `workers/workspace/test/build-contract-bridge.mjs`; `workers/workspace/test/build-contract-worker.ts`; `workers/workspace/test/build-envelope.test.ts` | **supersede** — Grahammmm/DeFlock-Campaign-Tool#95 integrated all seven blobs; only the build seam test differs now, adapted to claim-token/replay semantics by Grahammmm/DeFlock-Campaign-Tool#97. |
| Grahammmm/DeFlock-Campaign-Tool#83 (draft) | `64c8e82527d5` | `39b9242d7f19` | `campaign_tool/records/publish.py`; `campaign_tool/records/run.py`; `tests/records/test_proposal_provenance.py`; `tests/records/test_publish_authority.py` | **supersede** — Grahammmm/DeFlock-Campaign-Tool#89 incorporated publication/provenance repairs; tests and _record_proposal match, while release publication adds retirement/relocation guards. |
| Grahammmm/DeFlock-Campaign-Tool#84 (draft) | `603ab39cd98b` | `39b9242d7f19` | `campaign_tool/records/context_revision.py`; `docs/CONTEXT-REVISION-INTEGRATION.md`; `docs/integration/context-revision-run-guards.patch`; `tests/records/test_context_revision.py` | **supersede** — All four touched blobs match the release via Grahammmm/DeFlock-Campaign-Tool#96; the retained partial patch does not clear unattended/context-provider holds. |
| Grahammmm/DeFlock-Campaign-Tool#85 (draft) | `cbf04994ec67` | `39b9242d7f19` | `campaign_tool/backup.py`; `campaign_tool/records/recovery/backup.py`; `campaign_tool/records/recovery/backup_v2.py`; `docs/BACKUP.md`; `docs/RECORDS_BACKUP_V2.md`; `tests/records/test_controller_kernel_faults.py`; `tests/test_backup_integrity.py`; `tests/test_records_backup_v2.py` | **supersede** — Grahammmm/DeFlock-Campaign-Tool#92 carries all seven backup payloads unchanged; the eighth path is an obsolete kernel-test framing change, not missing backup work. |
| Grahammmm/DeFlock-Campaign-Tool#86 (draft) | `6c6f9a96925e` | `39b9242d7f19` | `campaign_tool/records/detectors/pipeline.py`; `docs/STRUCTURED-DETECTOR-CONTRACT.md`; `docs/integration/ARCH10-stage-detect.patch`; `tests/records/test_structured_detector_pipeline.py` | **supersede** — All four touched blobs match the release via Grahammmm/DeFlock-Campaign-Tool#98; source adapter inclusion does not mean seven detectors are wired or trusted inputs exist. |
| Grahammmm/DeFlock-Campaign-Tool#87 (draft) | `f05b9bf27ffb` | `39b9242d7f19` | `docs/CONTRACTS.md`; `docs/RUNNER.md`; `docs/WORKERS.md`; `runner/Dockerfile`; `runner/client.py`; `runner/cloudflare-container.md`; `runner/compose.example.yml`; `runner/loop.py`; `schemas/job.schema.json`; `tests/runner/test_build_site.py`; `tests/runner/test_client.py`; `tests/runner/test_loop.py`; `tests/runner/test_queue_reliability.py`; `workers/schema/d1.sql`; `workers/scripts/sync-data.mjs`; `workers/shared/src/generated/schema.ts`; `workers/workspace/migrations/0002_job_claims.sql`; `workers/workspace/src/db.ts`; `workers/workspace/src/runner.ts`; `workers/workspace/test/env.d.ts`; `workers/workspace/test/helpers.ts`; `workers/workspace/test/queue-migration.test.ts`; `workers/workspace/test/queue-reliability.test.ts`; `workers/workspace/test/runner.test.ts`; `workers/workspace/vitest.config.ts` | **supersede** — All 25 touched blobs match the release via Grahammmm/DeFlock-Campaign-Tool#97; source integration does not authorize a hosted migration or rollout. |
| Grahammmm/DeFlock-Campaign-Tool#110 (ready) | `51ed513a4940` | `225e7d3fa0b4` | None | **rebase** — Unique 20-line priority/restart fairness regression is absent and its file is unchanged on the release side; retain for fresh Linux CI and independent review. |
| Grahammmm/DeFlock-Campaign-Tool#130 (draft) | `7d83f2531fa8` | `ecb5c32932c1` | None | **rebase** — Contained-resume module, docs and tests are absent from the release; preserve the distinct supervision proposal, but failing captured CI and existing containment holds prohibit acceptance. |
| Grahammmm/DeFlock-Campaign-Tool#138 (draft) | `daca716de4dc` | `0ab680433b7f` | `tools/check_secret_report.py` | **rebase** — Selected-export snapshot/routing is absent from the release; retain under the single integrator, reconcile the secret-report checker overlap and preserve qualification/installed-runtime holds. |
| Grahammmm/DeFlock-Campaign-Tool#154 (ready) | `62a5e7e92a86` | `fddd7fddfe51` | None | **rebase** — Pinned build-environment guidance is still absent and its file is unchanged on the release side; retain for review/refresh, with no evidence of a textual conflict. |

## Evidence behind the supersede recommendations

These are source-integration findings, not claims of production validation.
Every successor merge below is an ancestor of pinned release 6028144.

- Grahammmm/DeFlock-Campaign-Tool#80 → Grahammmm/DeFlock-Campaign-Tool#93:
  all five changed files are byte-identical at the release. The read-only report,
  skill and tests exist; request enrollment and actual follow-up sending are not
  established by that fact.
- Grahammmm/DeFlock-Campaign-Tool#81 → Grahammmm/DeFlock-Campaign-Tool#94:
  both helper/test files are byte-identical. The successor also integrated
  current-extraction selected-coverage accounting; it explicitly does not
  authenticate semantic acceptance or dispatch all planned chunks.
- Grahammmm/DeFlock-Campaign-Tool#82 → Grahammmm/DeFlock-Campaign-Tool#95:
  six of seven changed files remain identical. The remaining
  `tests/runner/test_build_site_contract.py` changed with
  Grahammmm/DeFlock-Campaign-Tool#97: claim tokens, fixed handler error text,
  exact replay acknowledgement and conflicting replay refusal. Restoring the
  old test would discard that integration compatibility, not add missing work.
- Grahammmm/DeFlock-Campaign-Tool#83 → Grahammmm/DeFlock-Campaign-Tool#89:
  both provenance/authority test files and the complete
  `Pipeline._record_proposal` source remain identical. The release's
  `publish.py` retains the repair and adds review-retirement checks plus the
  publication-root relocation binding (Grahammmm/DeFlock-Campaign-Tool#104).
  `run.py` has other subsequent integration changes; whole-file equality is not
  claimed. The successor description explicitly identifies the captured #83
  head as one of its inputs.
- Grahammmm/DeFlock-Campaign-Tool#84 → Grahammmm/DeFlock-Campaign-Tool#96:
  all four source/test/integration-note files remain identical. The partial
  guard patch is still documentation, and manual contextual integration does
  not establish unattended reconciliation or trusted source-date providers.
- Grahammmm/DeFlock-Campaign-Tool#85 → Grahammmm/DeFlock-Campaign-Tool#92:
  seven backup payload files remain identical. The only differing path is
  `tests/records/test_controller_kernel_faults.py`: the release uses the
  Grahammmm/DeFlock-Campaign-Tool#91 dedicated progress-pipe/strict-JSON harness
  rather than the old stdout contention-notice parser. This is not an unported
  backup repair. The original PR description says seven paths, but the actual
  captured Git delta has **eight**; the table and JSON use Git evidence.
- Grahammmm/DeFlock-Campaign-Tool#86 → Grahammmm/DeFlock-Campaign-Tool#98:
  all four adapter/test/integration-note files remain identical. At 6028144,
  `campaign_tool/records/run.py` still initializes structured detector outcomes
  as skipped in `stage_detect`; neither identical blobs nor a merged adapter
  proves seven runtime detectors have trusted normalized inputs.
- Grahammmm/DeFlock-Campaign-Tool#87 → Grahammmm/DeFlock-Campaign-Tool#97:
  all 25 touched files remain identical. The coordinated hosted migration,
  runner adoption and persistent private result storage remain separate gates.

### F3's specific backup concern: #85 versus #158

The concern is conceptual overlap in backup safety, **not a file collision**.
Grahammmm/DeFlock-Campaign-Tool#158 changes only:

- `campaign_tool/records/run_stages.py`
- `schemas/records-stage-result-v1.json`
- `tests/test_records_run_stages.py`

The intersection with #85's eight changed files is empty. #158's
`backup_adoption_dbs` / `_backup_database` path performs per-run pre-adoption
ledger/intake snapshots, integrity checking, manifest creation and rotation.
#85 concerns campaign archive original-closure checks and recovery SQLite
snapshot finalization, already integrated by #92. Do not remove #158 on the
assumption that it is a duplicate of #85, and do not reapply #85 as a missing
prerequisite for #158. No private backup or production restore was inspected.

## Distinct work and owner disposition

- Grahammmm/DeFlock-Campaign-Tool#110 adds one 20-line synthetic priority/restart
  regression in an otherwise release-unchanged file. Its added test is not on
  6028144. Retain for normal Linux validation, not mocked supplemental evidence.
- Grahammmm/DeFlock-Campaign-Tool#130 adds three absent contained-resume files.
  Captured GitHub checks include failed `intake (3.11)`, `intake (3.12)` and
  `intake (3.13)` jobs (plus other failures/cancellations); this is not a green
  candidate. Preserve the original qualification history, bounded retries and
  supervisor-crash/deployment holds. No rerun or candidate code execution was
  performed for this triage.
- Grahammmm/DeFlock-Campaign-Tool#138 adds the absent export snapshot adapter
  and new routing/capability/provenance tests. Its actual delta has 11 paths,
  not the eight-path description's historical qualification scope. Only
  `tools/check_secret_report.py` intersects release-side changes; that overlap
  must not be resolved by weakening scanner policy. The added snapshot path is
  distinct from the existing export-selection CLI. Captured checks include a
  failed `package (3.11)` and cancellations; one check was still in progress.
  Do not treat the older eight-path qualification or source-only tests as
  acceptance of this exact composition, Docker mounts or installed ledgers.
- Grahammmm/DeFlock-Campaign-Tool#154 is a documentation-only pinned-build
  environment addition. Its target file has no release-side changes since the
  merge base. Retain for the existing review lane; “rebase” is not a claim that
  Git reports a conflict. No host venv creation or package installation is needed
  for this triage.
- Grahammmm/DeFlock-Campaign-Tool#14 is unique, absent social-rendering work,
  not superseded by the records release. Recommend close/defer from this release
  queue; any revival needs a separately scoped dependency, privacy and
  publication review. No paid model generation or social publication was run.
- Grahammmm/DeFlock-Campaign-Tool#39's ten audit-packet files are absent, so it
  is **not** falsely classified as implemented. Recommend close as a stale
  October 1 continuation handoff; retain its exact head as a dated reference.
  Do not promote its old “current state” into present release acceptance.

No new implementation jobs are created: the retained work already has PRs and
must remain with its existing owner/integrator. None of these recommendations
assigns or duplicates the local document queue or writes canonical records.

## Reproduction and acceptance

Collection used read-only `gh pr list`, `gh pr view`, `gh pr checks`, Git object
fetches, merge-base, path diffs and blob/source comparisons. No candidate code
or records pipeline was run. Checks for #130/#138 are a timestamped observation,
not a current CI guarantee; all other CI/mergeability checks are outside this
triage. The exact observed job links and state counts are preserved in JSON.

With full Git history and the captured PR objects available, run:

```sh
python3 -B docs/audits/fl4-open-pr-triage/verify.py
python3 -B docs/audits/fl4-open-pr-triage/verify.py --check-remote
```

If an object is absent, fetch that public head using
`git fetch --no-tags origin refs/pull/<number>/head`. If the remote head moved,
retain this frozen evidence and obtain the captured object from history rather
than replacing the ID. The remote check intentionally fails on changed heads,
state or draft flags. It does not request CI reruns or modify a PR.

The verifier checks 14/14 coverage, exact merge bases, net path intersections,
blob partitions, successor ancestry, the #83 method comparison, #85/#158 path
separation and report-table agreement. Its pass is machine-valid evidence, not
semantic acceptance of these verdicts. Different-model review by `opus-coder`
is still required. G alone decides closure; this task authorizes no merges.
