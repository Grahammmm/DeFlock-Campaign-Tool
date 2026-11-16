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

## Import-manifest pin refresh checklist

Authors and independent code reviewers: if a PR changes any file in this table,
the matching manifest entry and the scanner approval of that whole manifest must
be refreshed in the same PR. Focused intake/gate functional tests alone do not
catch a stale pin: stale manifest or scanner pins can fail the package, shell,
WP5, intake or detect-secrets checks even when the narrow tests pass.

| Affected file | Manifest whose matching entry must refresh |
|---|---|
| `campaign_tool/records/intake/folder.py` | `docs/records-intake-import.json` |
| `campaign_tool/records/intake/repair_intake.py` | `docs/records-intake-import.json` |
| `campaign_tool/records/intake/scope_quarantine.py` | `docs/records-intake-import.json` |
| `tests/records/test_intake.py` | `docs/records-intake-import.json` |
| `campaign_tool/records/gates/batch_report.py` | `docs/records-gates-import.json` |
| `campaign_tool/records/gates/collect_reviews.py` | `docs/records-gates-import.json` |
| `campaign_tool/records/gates/reconcile_coverage.py` | `docs/records-gates-import.json` |
| `campaign_tool/records/gates/validate_findings.py` | `docs/records-gates-import.json` |
| `campaign_tool/records/gates/tests/test_gate.py` | `docs/records-gates-import.json` |

Order, all in the same PR:

1. Hash the changed file's final bytes into the matching manifest entry's `engine_sha256`.
   Keep the historical `source_sha256` and `schema_sha256` unless a real source or schema change justifies it.
2. After the manifest bytes are final, hash the entire manifest file and update only its
   corresponding `APPROVED_MANIFESTS` entry in `tools/check_secret_report.py`. A manifest-only
   byte change also invalidates the whole-file approval, and so does any later formatting or
   newline change. If both groups change, refresh both manifests and both approvals.
3. Do not copy stale hashes, strip fields, add a blanket allowlist or weaken the scanner.
4. Run `python3 -B -m unittest tests.test_provenance_scan -v` in addition to the relevant
   functional tests and the repository credential/privacy scans. That module is not a complete
   secret scan, and a correct pin is not publication clearance or independent review.

If no pinned file changed, say so in the PR body with a not-applicable rationale.
