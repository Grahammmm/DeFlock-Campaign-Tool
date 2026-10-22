# DeFlock open-PR fixes, 2026-10-22

Status: author work (Claude); independent review still required. Nothing was
merged, force-pushed or opened as a PR. Original PR branches are untouched.
Only synthetic fixtures were used. Base: `main` at `75494fa0a3f5`.
Tests ran in a cloud sandbox (Linux, Python 3.13); they are not host validation.

## #130 Bounded resume: FIXED
- Branch: `claude/fix-pr130` (from PR head `7d83f2531fa8`)
- Head SHA: `fab6f41bb15ccaefbc03b0ff4479df14810efb53`
- Cause: `Process.join(timeout)` in `bounded_resume.run_once` waits on a pipe that a
  surviving descendant also holds, so a clean leader exit was reported as `timed_out`.
  Fix: poll the worker's own exit status (`_wait`). Added a regression test; it fails
  without the fix and passes with it.
- Note: the change is in `bounded_resume.py`, which the PR did not originally touch.
- Commands (unprivileged uid 65534 via `setpriv`, since the suite refuses root):
  - `python3 -B -m unittest tests.records.test_contained_resume tests.records.test_bounded_resume tests.records.test_worker_lifecycle`: 56 tests OK (3 repeat runs of contained_resume: 13 OK each)
  - `python3 -B tools/check_public_tree.py`: no findings
  - `node scripts/scan-secrets.mjs`: PASS
- Left: full CI not re-run on the new branch; `intake (3.11/3.12/3.13)` failures recorded in FL-4 not re-checked. Not wired into runtime; supervisor-crash and bounded-retry holds remain.

## #138 Selected exports into container intake: FIXED
- Branch: `claude/fix-pr138` (from PR head `daca716de4dc`)
- Head SHA: `d2f842d0c0b12eb72b4f94e6e2de2737e86717b2`
- Linux test failure: `ELOOP` vs `ENOTDIR` (O_DIRECTORY|O_NOFOLLOW on a symlink). Test now
  accepts either; both are refusals. Test-only change.
- Conflicts: merged `origin/main` (merge commit, no rewrite). In
  `docs/records-intake-import.json` the `engine_sha256` was recomputed from the merged
  `folder.py`; in `tools/check_secret_report.py` the manifest pin was recomputed from the
  resolved manifest. No generator script exists; the values are derived hashes, checked by
  `test_exact_manifest_and_engine_pins`. No scanner policy change. Reviewer should
  re-derive both.
- Commands:
  - `python3 -B -m unittest tests.records.test_secure_open_workspace_capability tests.records.test_selected_export_container_routing tests.test_intake_provenance_binding tests.test_provenance_scan tests.test_secret_report tests.records.test_c20_csv_strict_mapping tests.records.test_intake tests.records.test_container_host`: 163 OK (1 skipped)
  - export/unattended modules (`tests.records.test_*export*`, `test_unattended_*`): 148 OK
  - `check_public_tree.py`: no findings; `scan-secrets.mjs`: PASS
- Left: PR is still a draft; FL-4 qualification and installed-runtime holds remain.

## #161 Rootless swap hardening: BASE UPDATED ONLY, needs owner
- Branch: `claude/fix-pr161` (from PR head `0e06ec402078`)
- Head SHA: `1c928c1cc17c1c9590396bf8a0e7d119a77b23cc` (clean merge of `origin/main`, no code edits)
- CI failure `package (3.12)`: diagnosed from the job log. No test failed; the step
  `Synthetic tests` hit its 15-minute timeout while 3.11 and 3.13 passed. Main has since
  raised that timeout to 18 minutes (picked up by the merge). No code fix needed.
- Commands (as root; these swap tests need root-like ownership and fail as uid 65534):
  - `python3 -B -m unittest tests.test_flock_ocr_swap_hardening tests.test_flock_ocr_rootless_swap tests.test_flock_ocr_container`: 147 run, 1 failure, 1 skipped
  - The 1 failure (`FlockOcrSnapshotEvidenceTests.test_manifest_detects_content_owner_mode_and_sidecar_changes`) also fails on plain `origin/main` here (sandbox, likely root); not caused by this PR.
  - `check_public_tree.py`: no findings; `scan-secrets.mjs`: PASS
- Left: independent review of the run-pruning code (deletes run dirs) and storage changes;
  owner approval and a host rehearsal before any live OCR use.

## #164 Swap rehearsal mode: NOT FIXED, product decision needed
- No branch pushed. PR head `d406f671de4f` (unchanged).
- Its own tests pass (`tests.test_flock_ocr_rootless_swap`: 89 OK). All 47 CI checks on its head are green.
- It conflicts with #161 in 3 files (`container/flock-ocr/rootless_swap.py`,
  `docs/RECORDS-OCR-CONTAINER-ROOTLESS.md`, `tests/test_flock_ocr_rootless_swap.py`). The
  triage says the landing order of #161 and #164 needs owner sign-off, and resolving it
  edits the live OCR swap driver. I stopped rather than choose.
- Left: owner decides order; then rebase/merge #164 onto #161 and re-run
  `tests.test_flock_ocr_rootless_swap`; review the new rehearsal workflow
  (`flock-swap-rehearsal.yml`: rootless Docker on a runner).

## #164 Swap rehearsal mode: FIXED ON TOP OF #161 (update)
- Branch: `claude/fix-pr164`, stacked on `claude/fix-pr161`. Not merged, no force-push.
- Head SHA: `35804e2da477dd631311243899dccc268e469580` (merge of PR head `d406f671de4f` into `1c928c1cc17c`)
- Conflicts: 4 files (`preflight.py`, `rootless_swap.py`, the rootless doc, the swap tests). Resolved by hand:
  - Build context: both PRs had isolated it. Kept #161's `$RUN/b1/context`; the rehearsal uses it too.
  - Leftover recipe `dist/`: #164 refused the run, #161 ignores it. Now only **reported** in `preflight.json`.
    Intentional change to #164's R1 check; owner should confirm.
  - Free space: S1 and A2 take the **larger** of #161's per-copy budget and #164's tar-aware budget.
  - `--dry-run`, `prune`, `--rehearse` all kept.
- Tests changed to the merged rules (4 in `test_flock_ocr_rootless_swap.py`, 1 in `test_flock_ocr_swap_hardening.py`) plus a new one for the leftover-dist report. Synthetic fixtures only.
- Workflow `flock-swap-rehearsal.yml` reviewed: `contents: read` only, actions pinned by SHA, `pull_request` (not `_target`), no secrets. Unchanged.
- Commands (as root):
  - `python3 -B -m unittest tests.test_flock_ocr_rootless_swap tests.test_flock_ocr_swap_hardening`: 119 run, OK
  - `... tests.test_public_tree tests.test_secret_report tests.test_provenance_scan tests.test_flock_ocr_container`: 83 run, 1 failure, 1 skipped. Same `test_manifest_detects_content_owner_mode_and_sidecar_changes` failure as on plain main (root sandbox).
  - `check_runbook.py`: OK; `check_public_tree.py`: no findings; `scan-secrets.mjs --staged`: PASS
- Not run: the full suite, CI on the new branch, any rootless-Docker rehearsal, any host.
- Left: independent review of the merge (it edits the live swap driver); owner approval before any live use.

## Merge order recommendation
1. **#138** and **#130**: independent of each other and of the swap driver. Either first, after review.
2. **#161**, then **#164**. #164's branch already contains #161. If #161 merges first, #164 reduces to its own changes.
3. Merging #164 alone would also bring in #161. Merge #161 first so each gets its own review.
4. Re-run CI on each head before merging. Nothing here is a host rehearsal.
