# CI wall-time change: baseline and verification

## Baseline inspected

Read-only GitHub CLI inspection used `gh run view RUN --repo
Grahammmm/DeFlock-Campaign-Tool --json jobs,headSha,conclusion,event,createdAt,updatedAt,url`.
Times below are seconds from the returned timestamps, not estimates. Queue time
is run creation to job start; elapsed time includes queueing. These historical
runs are not same-head controlled experiments.

| Successful run | Event / source head | Run elapsed | Job execution | Queue delay |
|---|---|---:|---|---|
| [40444749728](https://github.com/Grahammmm/DeFlock-Campaign-Tool/actions/runs/40444749728) | main push / ca60b7a | 3710 | offline 3.11: 701; 3.12: 1065; 3.13: 1015 | 1854; 2644; 1691 respectively |
| [40444749670](https://github.com/Grahammmm/DeFlock-Campaign-Tool/actions/runs/40444749670) | main push / ca60b7a | 4142 | package 3.11: 911; 3.12: 1059; 3.13: 1077 | 3048; 2910; 3065 respectively |
| [40521079195](https://github.com/Grahammmm/DeFlock-Campaign-Tool/actions/runs/40521079195) | PR / 6c4c5fb | 1296 | retirement-boundary: 634; slice 3.11: 340; 3.12: 601; 3.13: 191 | 507; 343; 694; 314 respectively |

Full discovery dominated offline jobs (678–1047 seconds) and release jobs
(873–1035 seconds including public-pattern scanning). Pip installs were 4–11
seconds. Slice OCR apt installation varied from 9 to 446 seconds, while slice
tests took 145–172 seconds. Queue waits were a major contributor, so a reduced
test runtime alone is not a measured end-to-end speedup.

## Changes and coverage contract

1. PR concurrency is isolated by workflow, event and PR ref. Main/batch/manual
   runs additionally include the run ID. This avoids cancelling unrelated PRs
   or replacing pending non-PR runs (a risk even with cancel-in-progress false).
   Reusable workflows inherit their caller's cancellation; they do not create
   a competing concurrency group.
2. Normal PR Python matrices execute 3.13. Main/batch pushes, batch/main-head
   PRs and manual runs retain the 3.11/3.12/3.13 matrices. Former single-version
   jobs use 3.13 on PRs and preserve their prior interpreter on pushes.
3. `records.yml` now owns the shared full-discovery population previously
   repeated by `records.yml`, `records-intake.yml` and `offline.yml`. The latter
   two are reusable workflows, called unconditionally for their distinct
   credential scan, Node tests, example build and public-tree checks. The
   stricter ResourceWarning-as-error mode is retained. Explicit SD-12 S3a
   acceptance remains in shard zero for each selected interpreter.
4. Release discovery remains separate: its build/parser dependency environment
   is not assumed equivalent to the smaller offline dependency environment.
   Its public scan, source-bound wheel build, fresh-venv installed CLI checks
   and zero-skip installed composition are unchanged, and run in parallel
   with the release shards.
5. Both full discovery populations use four parallel shards. The runner loads
   standard unittest discovery (including load_tests), preserves the multiset
   of test IDs, keeps modules/classes together, and balances by module test
   count. New tests are automatically included. Import failures and custom
   suite implementations that cannot safely be flattened are hard failures.
   No existing test file, assertion, strict integration list or source pin is
   removed. Existing opt-in discovery skips remain possible; their separate
   strict zero-skip integration workflows are retained, not replaced by a
   non-strict full-suite success.
6. All former check names remain as fail-closed result gates. On normal PRs,
   the legacy 3.11/3.12 labels are compatibility aliases for the approved 3.13
   policy, not a claim those interpreters ran. Every selected matrix member
   must pass. Failure, cancellation, unknown output or an unexpected skip
   fails the gate. `offline`, `intake`, `shell` and `package` gates require both
   their full-suite result and their distinct scan/build results.
7. No workflow-level path filter can strand a required check. Only prose
   directly in `docs/` or `docs/ci/` may skip the two expensive discovery jobs
   on normal PRs. Runbooks, README/build metadata, provenance JSON, fixtures,
   requirements, workflows and unknown paths always run tests. Missing diff,
   empty diff, malformed metadata or failed git inspection also runs tests.
   The complete NUL-delimited git diff has no API file-count limit and disables
   rename detection, so a code-to-document rename cannot conceal deletion.
   Scans, package builds and all result gates still execute for prose-only PRs.
8. Pip download caches use setup-python's OS/architecture/interpreter keys plus
   all relevant requirements files, pyproject.toml and setup.py. WP5 uses its
   engine checkout's inputs. Install steps remain unconditional: caches are
   accelerators, not environments or permission to skip dependency resolution.
   The existing npm lockfile cache and unconditional npm ci remain intact.

## Not applicable / deliberately not changed

- These workflows have no deployment job. Wheel builds, Docker builds and
  synthetic publication tests do not deploy. Nothing was added that deploys
  on a PR, and no production deployment is used for measurement.
- There is no Playwright/browser installation or browser-test dependency in
  these workflows. A browser cache would save nothing; no browser is added.
- Apt state is not cached or copied between hosts. The existing disposable
  hosted OCR installation and actual OCR checks remain intact.
- The actual 10000/10001-row retirement-boundary case is not split or reduced:
  its single-transaction boundary semantics must remain real. Long full-suite
  jobs, not this atomic acceptance case, are sharded.
- No runner migration, repository variable, branch protection, permission,
  credential, dependency source pin or application feature changes.

## Validation and measurement status

Before the candidate commit, 16 new CI-helper regression tests passed; complete
collection found 5199 cases, partitioned 1300/1300/1300/1299 without dropping IDs.
A source comparison against the admitted base verified all original check
contexts, all non-discovery acceptance command lines and all pinned revisions
remain present. YAML parses successfully. Public-tree scanning found no
findings. Exact logs and later execution/hosted measurements are recorded in
the PR handoff; collection is not execution or independent acceptance.

Reproduce the new helper checks with:

    python -B -W error::ResourceWarning -m unittest tests.test_ci_workflows -v
    python -B -W error::ResourceWarning tools/ci_unittest.py --shard 0 --shards 4 --collect-only

Execute all four shard indexes 0, 1, 2, 3 in independent checkouts/environments,
using the same dependency set and warning flags as the respective workflow.
The unchanged integration and installed-wheel commands remain required too.
Do not substitute this helper-only test run for the full CI contract.

At initial commit no post-change hosted timing is available. Cold-cache and
warm-cache hosted timings, actual PR supersession cancellation and a live
prose-only PR event have not been measured. Unit/structural checks cover their
rules but are not live GitHub behavior evidence. No numeric speedup is claimed.
