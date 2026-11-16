# Contributor instructions

Treat this repository as software, not a storage location for campaign records.
Never copy campaign originals, email, subscribers, tokens, signed download links,
private analyses, or production settings into source control.

Use synthetic fixtures for development. Do not send email, submit requests,
provision paid services, change DNS, or publish findings without scoped approval.
Do not bypass access denials. Keep analysis and public output separate.
Never describe scaffolding or extraction as completed independent review.

Read README.md and docs/ROADMAP.md to distinguish implemented and planned work.
Keep adapters provider-independent. Prefer exact source locators, hashes,
idempotent receipt identities, explicit coverage gaps, and bounded retries.

## Engine and campaign separation

This public repository is the reusable engine. Campaign configuration and reviewed public content belong in each organizer's separate campaign repository. The SLO pilot uses the private Grahammmm/deflockslo-site repository. Records originals, private analysis, subscribers, analytics data, API credentials and Cloudflare Access settings belong in private storage and provider accounts, never either repository.

The current CLI remains an offline starter. SLO production code has not yet been extracted. Before importing any such code, the owner must approve the private campaign's docs/PUBLIC-SPLIT.md rights/privacy inventory. Import generic pieces through small PRs using synthetic example data only. Preserve the existing CLI and review safeguards.

Use one codex/<task> branch and PR per task; never push to main. Before each push, run a credential scan and review all staged paths; report the command and checked scope in the PR. Never transfer private repo history.

## Records-test pre-push interpreter

When the pre-push selector includes records tests, check the repository's own provisioned `.venv/bin/python` first, before inspecting any alternate runtime.

- Managed canonical checkout: `/home/user/repos/DeFlock-Campaign-Tool/.venv/bin/python`. In another clone, prefer that clone's own already-provisioned `.venv/bin/python`; the host path is an example, not a universal prerequisite.
- Pins live in `requirements-records-test.txt`, `requirements-records-parsers.txt` and `requirements-runner.txt` (currently pypdf 6.10.0). Probe the interpreter with an external script file that prints `sys.executable`, the pypdf/openpyxl versions and `campaign_tool.__file__`; never use interpreter one-liners or command-line PYTHONPATH overrides.
- A detached worktree may reuse the canonical checkout's interpreter for its installed parser packages, but run with cwd at the candidate checkout and confirm `campaign_tool.__file__` is under that cwd. On an import-source mismatch, report it; do not silently test the donor checkout.
- Apply the interpreter prefix to the repository's existing workflow/selector checks, not a reduced suite. `<repo-python> -B -m unittest tests.records.test_fa100 -q` is only a smoke check, not full pre-push acceptance. Parser-independent starter tests need no extra dependency.
- Only after that probe, inspect alternate runtimes, or report ENVIRONMENT with the actual missing path, import or version. If the venv is absent or broken, record that fact. Do not create a venv, download or upgrade packages, treat parser-dependent skips as green, or run the runtime doctor/run against private records.
- Credential scans and staged-path review above still apply.

For each extraction, rebuild the pilot from a pinned engine version and compare its output against the accepted asset hashes. List every intentional difference. Do not rewrite findings, dates, policy claims or consent wording as an incidental refactor.

Staging deployment to the pilot's existing staging Worker is authorized. Production deployment, DNS, Access and Brevo list changes require explicit owner approval in chat. No account values or owner identities belong in a public fixture.

## run_stages hotspot pre-review

Each lane changing `campaign_tool/records/run_stages.py`, its stage-contract tests,
or stacked interacting stage behavior must run its relevant tests on the exact
composed candidate tree before requesting review, including single-lane candidates.
An isolated-green head is not a composed-tree receipt. Follow the
[composed-tree test workflow and review receipt](docs/RUN-STAGES-PRE-REVIEW.md);
record every admitted constituent and rerun after any head/tree change.
