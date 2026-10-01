# Roadmap

This is a staged product build, not a completed automation service.

| Milestone | Current state | Exit criterion |
|---|---|---|
| M0 Clean public foundation | Initial scaffold authored | Rights inventory and independent source/privacy review complete |
| M1 Offline vertical slice | Setup, byte intake and neutral site generator authored; validation pending | Synthetic request through digest, challenge, approved sanitized preview works end to end |
| M2 Cloudflare launch wizard | In progress (offline, DRY_RUN only) | New account launches public site and protected workspace; signup and restore verified |
| M3 Reliable intake | Runner, digest library and outbox authored and tested offline ([RUNNER.md](RUNNER.md), [OUTBOX.md](OUTBOX.md)); not attached to a live workspace | Mail/MuckRock originals, retries, checkpoints, archive failures and duplicate sends covered |
| M4 Evidence and law review | Structural gate authored; California law package drafted as data (`status: draft`, not independently reviewed) | Reviewed California package, original-level digestion, authenticated independent review and privacy gate |
| M5 Public action | Workflow authored offline: review receipts to `ready`, `publish_finding` with manifest-hashed builds, corrections and withdrawal, Brevo send behind a card, Legistar import and comment kits, verified backup ([PUBLICATION.md](PUBLICATION.md), [NEWSLETTER.md](NEWSLETTER.md), [MEETINGS.md](MEETINGS.md), [BACKUP.md](BACKUP.md)); no runner, no live send, no pilot | Source-linked content, correction propagation, verified meeting participation, branded signup |
| M6 Supported pilots | Planned | SLO plus second CA locality; second state only after its own reviewed package |

## Prioritized work

1. Validate the offline CLI, permission boundaries, crash recovery and malformed inputs.
2. Add schema contracts and a complete synthetic review journey with challenged findings.
3. Build a resumable wizard, county/state disambiguation and official agency discovery. *Partly done:* offline county/city resolution and a California agency seed (all 58 counties, unverified contacts) drive `campaign_tool kit`; see [AGENCY-DISCOVERY.md](AGENCY-DISCOVERY.md). Verified custodian contacts remain open; the wizard screens exist in `workers/wizard` (see [WORKERS.md](WORKERS.md)).
4. Build Cloudflare account/zone/resource planning, explicit apply, and deployment receipts. *Partly done:* plan/apply with per-resource receipts and a rollback list is implemented and tested with `DRY_RUN=1`; no live apply has been run.
5. Integrate hosted newsletter forms; verify CAPTCHA origins, CSP, consent and suppression. *Partly done:* non-secret Brevo settings, hosted-form URL validation, the test-signup checklist and the `send_newsletter` executor exist; no live signup has been verified.
6. Build immutable export/restore and source manifests. *Partly done:* `backup`/`verify`/`restore` with a hashed manifest, `GET /api/export.json` and `export_hosted`; encryption, scheduling and hosted restore remain.
7. Add mailbox and MuckRock adapters; preserve raw originals and attachment relationships.
8. Isolate PDF, spreadsheet, mail and archive parsers; account for every page and sheet. *Partly done:* the runner's `extract` handler reuses the sandboxed intake worker and reports OCR-needed pages; OCR itself is not installed.
9. Implement request tracking, fee limits and transactional outbox; reconcile ambiguous sends. *Partly done:* `campaign_tool/outbox.py` journals sends with idempotent keys, approval identity, daily and fee caps and reconcile; Worker executors remain interfaces.
10. Create reviewed California rule versions and local-policy overlays; maintain support matrix.
11. Authenticate reviewers and bind receipts to exact evidence and public artifacts.
12. Add publication/corrections, branded public materials and official meeting actions. *Partly done:* publication, correction and withdrawal flows with manifest-hashed rebuilds, manual meetings and comment kits; branded materials and verified participation remain.
13. Add one scheduler, job budgets, stale-data reports and deduplicated incident reporting.
14. Measure mobile experience and first-party load; never load-test external providers.
15. Run independent security/privacy review and a new-organizer pilot before a stable release.

## Not in the starter

No live legal deadline calculator. No automatic allegation publisher. No demographic
microtargeting. No email campaigns or public-comment flooding. No claim of all-state
legal support or of production readiness.

## Validation policy

Tests and independent review are release gates. The offline suite
(`python3 -B -m unittest discover -v`, synthetic fixtures, no network) runs in
GitHub Actions on every pull request and push to `main`, together with the
public-tree and secret scans. A green run shows the scaffold's structural checks
hold; it is not independent legal, privacy or capacity validation, and no
jurisdiction package or finding is treated as reviewed because its tests pass.
Do not label a task passing because code exists. Keep run version, fixture,
command, result and limitations in a release receipt.
