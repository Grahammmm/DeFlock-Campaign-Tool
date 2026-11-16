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

For each extraction, rebuild the pilot from a pinned engine version and compare its output against the accepted asset hashes. List every intentional difference. Do not rewrite findings, dates, policy claims or consent wording as an incidental refactor.

Staging deployment to the pilot's existing staging Worker is authorized. Production deployment, DNS, Access and Brevo list changes require explicit owner approval in chat. No account values or owner identities belong in a public fixture.

## run_stages hotspot pre-review

Each lane changing `campaign_tool/records/run_stages.py`, its stage-contract tests,
or stacked interacting stage behavior must run its relevant tests on the exact
composed candidate tree before requesting review, including single-lane candidates.
An isolated-green head is not a composed-tree receipt. Follow the
[composed-tree test workflow and review receipt](docs/RUN-STAGES-PRE-REVIEW.md);
record every admitted constituent and rerun after any head/tree change.
