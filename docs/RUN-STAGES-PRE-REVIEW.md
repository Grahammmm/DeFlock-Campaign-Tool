# run_stages hotspot: composed-tree tests before review

## Every lane, every candidate

Each lane must run its relevant tests on its exact composed candidate tree
before requesting review when changing `campaign_tool/records/run_stages.py`,
its stage-contract tests, or stacked interacting stage behavior. This includes
single-lane candidates, not just the deepest stack owner or last merger.
A previously isolated-green head is not a substitute for this receipt: tests
passing separately on each PR head do not prove the composed behavior passes.

## Compose only admitted work

Use an isolated clean worktree containing the admitted base, all explicitly
admitted prerequisite lane heads, and this lane's candidate. Record the full
base SHA, each constituent full head SHA in composition order, the resulting
candidate HEAD, and the tree from `git rev-parse HEAD^{tree}`. A single-lane
candidate still records its base, own head and resulting tree; its prerequisite
list is empty. Do not infer sibling admission from an open PR.

Use the lane's already admitted composition procedure; this document grants
no shared-main checkout, no force update, no implicit rebase and no queue mutation.
Do not merge unadmitted work or change another lane's checkout to build a receipt.
If a required head is unavailable or a conflict is unresolved, stop rather than
silently omitting that constituent. Keep the final candidate worktree clean;
uncommitted changes are not identified by HEAD or its tree hash.

## Run and record offline tests

Run the lane's added/changed test modules and impacted stage-contract consumers
against that composed candidate, not against its constituent heads separately.
Choose the full relevant set from the actual lane's changes and contracts.
Record each exact offline command, working directory, interpreter/dependency
availability, return code, test count and skips. Record the candidate head/tree
before and after execution to detect drift. For orientation only:

```sh
python3 -B -m unittest tests.test_records_run_stages tests.records.test_t12_harness_strictness -q
```

That baseline example is not a substitute for the lane's full relevant set.
No entire repository suite or live container rehearsal is required here.
Known environment skips must name the case and reason and be labeled
incomplete coverage; zero failures does not mean full coverage. Unavailable
interpreter/dependencies or tests that never ran are not passing evidence.

## Stop and invalidate

For any missing admitted constituent, unresolved conflict, unrun relevant tests,
test failure, unexplained skip, head/tree drift, or cancelled or expired test run,
do not request review as passing. Record the blocker locally in the existing
handoff and keep its incomplete coverage visible. Never remove assertions,
weaken expectations or drop a constituent to manufacture green.

Any constituent or candidate change invalidates the prior receipt, including a
base update, merge resolution or dirty-tree change: require
recomposition, rerun and fresh review context before requesting review again.
Old approvals and duplicate receipts are not transferable to a different tree;
copying a prior lane's green result is not this lane's test execution.

## Review receipt template

Populate this in the existing handoff, not a new public evidence store or runtime
artifact. Synthetic placeholders only; replace them with actual software identities
and results, never campaign data or private paths. Use full SHAs, not branch names.

```text
lane: <lane identifier>
base_sha: <full admitted base SHA>
constituent_heads: <ordered lane/full-SHA pairs, including this lane; no implicit siblings>
candidate_head: <full final HEAD SHA; unchanged after execution>
candidate_tree: <full HEAD tree hash; unchanged after execution>
commands: <exact argv, cwd, interpreter and dependency availability for each command>
return_codes: <one actual return code per command; not-run stays not-run>
test_counts: <per-command total run, failures and errors>
skips: <per-command count, every skipped case and reason; or explicit zero>
limitations: <unavailable coverage and unresolved blockers; or explicit none>
```

## Scope and authority

This is an offline contributor requirement, not a new executable gate or JSON
protocol. It grants no live run, no send, no publication and
no secret/private-record access. Existing privacy, campaign/engine separation,
approval, signing, live and publication holds remain unchanged. Test success is
not factual, privacy, legal or human semantic acceptance.
Documentation tests prove policy presence only, not that a real lane ran its tests
or that contributors complied with this workflow.
