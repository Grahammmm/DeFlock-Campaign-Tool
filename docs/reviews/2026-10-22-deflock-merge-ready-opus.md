# Independent review: PRs #110, #154, #162 (2026-10-22)

Reviewer: Claude (independent second-model review). Scope: check the earlier
triage (`docs/triage/2026-10-22-open-prs.md` on `claude/affectionate-albattani-fdfcky`, PR #177),
which called these three PRs "merge-ready".

Nothing was merged, pushed to a PR branch, or force-pushed. No secrets or real
data were used. All tests used the repo's synthetic fixtures.

## Summary

| PR | Head SHA | Verdict | One line |
| --- | --- | --- | --- |
| #110 | `51ed513a4940aaebf143f8d1be05accb6dc66bd2` | **APPROVE** | One useful test. Proven to catch a real bug. |
| #154 | `62a5e7e92a8648cf98219186fdff2f368b960de1` | **CHANGES** (small) | Claims are correct, but it hard-codes one machine's path into the public engine docs. |
| #162 | `7710baec8fe259d3129ca752a0659cf86467918a` | **APPROVE** (as a dated record) | Verifier passes on today's main. Verdicts are frozen to an older release. |

Main at review time: `75494fa0a3f525e378aac7b9cefbf4c3f7ec58b7`.

## Common checks (all three)

Each PR head was fetched with `git fetch origin pull/<N>/head`, then
trial-merged onto current main in a throwaway worktree.

| Check | #110 | #154 | #162 |
| --- | --- | --- | --- |
| Commits behind main | 254 | 117 | 46 |
| Trial merge onto main | clean | clean | clean |
| `node scripts/scan-secrets.mjs` (merged tree) | PASS | PASS | PASS |
| `python3 -B tools/check_public_tree.py` (merged tree) | 0 findings | 0 findings | 0 findings |
| GitHub checks on PR head | 46/46 success | 46/46 success | 46/46 success |
| Emails, private repo names, real records in diff | none | none | none |

Note: GitHub CI ran against each PR's old base, not current main. The local
trial merges cover that gap for the checks listed here.

Local tools: Python 3.13.16, Node 22.22.0.

---

## PR #110: test(records): priority cannot starve queue work after restart

**Verdict: APPROVE**

What it does: adds one 20-line test to `tests/records/test_queue_runtime.py`.
20 high-priority items are blocked; one low-priority healthy item is ready.
After a restart, the test checks that the healthy item is picked next, not
another high-priority blocked item.

Commands run (on the main + #110 trial merge):

- `TMPDIR=/var/tmp python3 -B -m unittest tests.records.test_queue_runtime tests.records.test_queue tests.records.test_queue_scope tests.records.test_queue_integration`
  (the same suite as `.github/workflows/records-queue.yml`) → **Ran 47, OK**, no skips.
- New test alone → **OK**.
- **Does the test catch a bug?** I broke the code on purpose:
  - In `QueueRuntime.advance`, I made priority score sort *before* the fairness
    cursor. Result: **only the new test failed** (1 of 20). The other 19
    existing tests did not notice. So the test adds real protection.
  - In `_candidates`, I made the same change, and separately dropped the cursor
    entirely. Result: all tests still passed. That sort is overridden later
    by `advance`, so it does not matter in practice.
  - All code changes were reverted afterwards.

MUST-FIX: none.

NICE-TO-HAVE:

- Update the branch from main (or re-run CI) before merging, so CI runs
  against current code. The local trial merge already passes.
- Separate follow-up, not this PR: the sort in `_candidates`
  (`campaign_tool/records/queue_runtime.py`, near "fairness_denominator_mismatch")
  has no effect, because `advance` sorts again. Either remove it or add a
  comment that `advance` decides the final order.

## PR #154: docs(RECORDS-RELEASE): pinned flock-build-venv

**Verdict: CHANGES (small)**

What it does: changes `docs/RECORDS-RELEASE.md` so the wheel build uses a
pinned build venv, and adds a "Pinned build environment" section.

Claims checked against the code:

| Claim in the doc | True? |
| --- | --- |
| `setuptools==84.0.0`, `wheel==0.48.0` match `pyproject.toml` `[build-system]` | Yes |
| ...and match `requirements-build.txt` | Yes |
| `container/flock-ocr/build_wheel.py` exists and takes `--build-python` | Yes (required arg) |
| `build_wheel.py` checks the pinned tools first and never installs or downloads | Yes (`check_build_python`, then pip with `--no-index`) |
| Pins match `container/flock-ocr/pins.json` `wheel_build.requirements` | Yes |
| "on Python 3.12" | Not enforced or pinned anywhere in the repo |
| `docs/RECORDS-RELEASE.md` unchanged on main since the PR's base | Yes, so no conflict |

Commands run: the secret scan and public-tree check above; `grep` over the
repo (no other file mentions `flock-build-venv` or `agent-work`).

MUST-FIX:

- **Remove the hard-coded machine path.** `~/agent-work/flock-build-venv` is
  one operator's local folder. This repo is the reusable public engine
  (see AGENTS.md: keep it provider-independent and free of account or host
  specifics). The PR also changes the *generic* build command so every reader
  must use that exact path. Use a placeholder instead, the way the same code
  block already uses `$WHEEL_OUTPUT` and `$CANDIDATE_RUNTIME`. For example:
  `BUILD_PYTHON="$BUILD_VENV/bin/python"`, with one line saying to create
  `$BUILD_VENV` from `requirements-build.txt`. The SLO pilot's own path can
  go in the private campaign repo.

NICE-TO-HAVE:

- Drop "on Python 3.12", or say it is only the tested version. Nothing in the
  repo requires it.
- Say that `build_wheel.py` builds the commit pinned in `pins.json` (not the
  current checkout) and writes to `container/flock-ocr/dist/`. Without this,
  readers may think it builds the tree they are standing in.
- The install step pulls from PyPI with no hashes. Fine for docs, but you could
  add a note to check versions after install, e.g. by running
  `build_wheel.py --build-python ...`, which refuses wrong versions.

## PR #162: docs: FL-4 triage of 14 open PRs against Phase 1

**Verdict: APPROVE (as a dated historical record)**

What it does: adds `docs/audits/fl4-open-pr-triage/` (README, `evidence.json`,
`commits.txt`, `verify.py`). It is a frozen triage of 14 PRs against release
`602814408e25`. It does not change any code.

Commands run:

- Fetched all 14 cohort PR heads (`git fetch --no-tags origin refs/pull/<n>/head`)
  and confirmed the clone has full history.
- `python3 -B docs/audits/fl4-open-pr-triage/verify.py` on the main + #162
  trial merge → **PASS 14/14**, verdicts `close 2 / rebase 4 / supersede 8`.
- I did not run `--check-remote` (it needs `gh`). Instead I compared the live open-PR
  list from the GitHub API with the README. All 14 heads and draft flags
  still match, and all 14 PRs are still open.
- `python3 -B -m unittest tests.test_public_tree` → OK.
- URL/email scan of the diff: only links to this repo's own Actions runs.

Is it still current?

- The evidence is pinned to `602814408e25`. Main is now 46 commits ahead.
  The verifier checks the frozen evidence, not today's main. That is by
  design, and the README says so.
- For #110 and #154 its "rebase/retain" verdict still holds (same heads, files
  still unchanged on main).
- PR #177 builds on it and does not replace it. So it is not SUPERSEDED.

MUST-FIX: none.

NICE-TO-HAVE:

- `verify.py` uses bare `assert` for every check. Run with `python -O`, all
  checks are skipped and it still prints PASS. Use explicit
  `if ...: raise SystemExit(...)` checks, or refuse to run when
  `sys.flags.optimize` is set.
- Add one line at the top saying these verdicts are as of `602814408e25` and
  pointing to the newer triage (#177) once that merges.
- The README uses internal shorthand ("G", "opus-coder", task id
  `t_c1f01547`). This is harmless, but public readers won't know what it means.

## Where this differs from the earlier triage

- #110: agree (merge-ready). I added a test that breaks the code on purpose,
  which shows the new test catches a real bug.
- #154: **disagree**. The earlier triage noted "names a host path" as low risk.
  I treat it as a must-fix, because it puts one machine's layout into the
  generic public build command.
- #162: agree (merge as history). Re-verified on today's main and against the
  live PR heads.
