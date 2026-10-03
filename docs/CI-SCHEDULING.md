# CI scheduling policy

Validation runs automatically for pull-request changes and pushes to main. A branch that does not yet have a PR is not checked automatically; open a draft PR or use Actions → the workflow → Run workflow, choosing the branch. Manual dispatch is available on each workflow after this policy reaches the default branch.

Each workflow has its own concurrency group. Pull-request groups include the PR number, so a new revision cancels only obsolete validation of that same PR in that same workflow. Separate PRs and separate workflows do not cancel one another. Main and manual runs include their unique run ID and are not canceled by this policy.

This replaces branch-push plus PR duplicate validation. It does not skip tests, filter changed paths, shorten time budgets, or change deployment and approval permissions. A canceled obsolete run is not passing evidence; assess all required checks on the current PR head. A manually rerun workflow uses the existing run's concurrency identity.

Historical runs created before this policy keep their original configuration. This change does not cancel those jobs or clear an already-existing queue. Current runner capacity and the live scheduling behavior still need to be observed after merge.

Publisher documentation: [GitHub workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
