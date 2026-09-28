# Reusable engine and private campaigns

Status: design documented; engine extraction has not started. The offline CLI is still the implementation described in README.md.

## Intended ownership

| Location | Contains | Excludes |
| --- | --- | --- |
| Public engine | Templates, schema, generic map and cards, signup adapters, analytics code, Worker, tests, docs, synthetic example | Real campaign records, private analysis, contact lists, credentials, Access settings |
| Private campaign | County/agency/branding configuration, approved public findings/evidence notes/stories/meetings, pinned engine version | Original productions, private analysis, subscribers, analytics rows, credentials, Access settings |
| Organizer storage/accounts | Private originals, review workspace, D1 data, R2 objects, Brevo contacts, secrets and Access policy | Automatic publication of private material |

## Order and approval gates

1. Capture the deployed pilot in a clean private branch. Prove public asset parity, preserve protected routes, add CI and staging, record rollback version. Owner accepts the baseline.
2. Audit every source file, runtime assumption and third-party asset in the private docs/PUBLIC-SPLIT.md. The owner approves that document before extraction.
3. Extract shell/styles, map, agency/evidence cards, story stream, signup, analytics, then Worker/build in separate PRs. For each, add JSON Schema fields and synthetic tests, pin an engine release in the private campaign and compare outputs.
4. Add a template repository, account-owned Cloudflare deployment, protected first-run setup and LAUNCH.md. Test with a fresh account and report actual timing and costs.
5. Add meeting decisions, mailto contact actions, comment kits, city-specific signup, agency pages/social metadata, shorter homepage, deferred interactive maps, source attribution and language structure. Translations remain marked for human review.

## Validation debt

The existing commit message mentions 16 offline checks, but no test files are tracked in the current main branch. Recover or reproduce those tests on a separate approved implementation branch; do not count the commit message as test evidence. The public CLI must continue to support init, ingest, status, doctor and build with its privacy safeguards.

## Publication rules

An asset being publicly viewable does not establish redistribution rights. Map attribution and database rights, fonts, portraits and logos need an explicit rights inventory. A legal allegation needs its existing source and review history; do not fabricate sample facts by copying real agencies into the synthetic example. The public engine is intended for any jurisdiction, but state-specific legal support must not be claimed until reviewed.

The pilot's private audit is intentionally not copied here. Public PRs may explain generic implementation decisions without carrying private records, campaign identifiers, owner identities or account configuration.
