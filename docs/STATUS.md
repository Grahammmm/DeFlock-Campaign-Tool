# Status: what is live, offline-only, frozen and planned

Status as of 2026-10-31; update this page in the PR that changes a row.

This is the one current status page. Older milestone documents keep their
original text for provenance and carry a "Historical milestone text" banner;
where they disagree with this page, this page is current. Every row names the
file or document that is its evidence.

## Live

| Part | What it does | Evidence |
| --- | --- | --- |
| Records engine run | One records run per timer slot (07:00, 13:00 and 20:30 America/Los_Angeles): overlap lock, image pin check, the stage orchestrator, then a non-fatal notify. Its machine-valid run report is review-required, not semantic acceptance of the run. | `deploy/records-runtime/README.md`, `campaign_tool/records/run_orchestrator.py`, `schemas/records-run-report-v1.json` |
| Records stages | Sync, delta, adoption, portal, extract, digest_gates and outputs, then non-fatal notify, which sends a metadata-only Telegram message. Phase 2 OCR targets are optional, not the default live extract; OCR live wiring (T6) is not done. | `deploy/records-runtime/README.md`, [RECORDS-OCR-STAGE.md](RECORDS-OCR-STAGE.md) |

Which engine build is installed is recorded by the Flock coordinator, not in this repository.

## Shipped, used by operators (no service)

These run by hand from a checkout. None of them installs a schedule, sends
mail or publishes anything.

| Part | What it does | Evidence |
| --- | --- | --- |
| Flock facts, law-policy, compare | Cited facts, law and policy text, and policy comparison for one agency. | `tools/flock_facts.py`, `tools/flock_law_policy.py`, `tools/flock_compare.py`, `skills/flock-facts/`, `skills/flock-law-policy/`, `skills/flock-compare/` |
| Flock verify | Independent check of a run's findings against their citations. | `tools/flock_verify.py`, `skills/flock-verify/` |
| Flock follow-up and email drafts | Follow-up and email drafts and a request reminder list only; they write or print files and nothing sends. | `tools/flock_email.py`, `tools/flock_requests.py`, `skills/flock-followup/`, `skills/flock-email/`, `skills/flock-correspondence/` |
| Flock monitor, sweep, region | Index-page monitoring (links recorded, never fetched), watch-list sweep status and plans, a ranked read-only daily to-do list, county region scaffolds. | `tools/flock_monitor.py`, `tools/flock_sweep.py`, `tools/flock_today.py`, `tools/flock_region.py`, `skills/flock-monitor/`, `skills/flock-sweep/`, `skills/flock-region/` |
| Flock review, report, page | The staged review run, its verify sample, golden check and batch runner, its report draft and a page draft. | `tools/flock_review.py`, `tools/flock_review_verify.py`, `tools/flock_review_golden.py`, `tools/flock_review_batch.py`, `tools/flock_report.py`, `tools/flock_page.py`, `tools/flock_privacy.py` (email/phone/plate patterns shared by the report, page and publish lints), `tools/flock_privacy_scan.py` (same patterns over a private draft folder; path:line: category only), `skills/flock-review/`, `skills/flock-page/` |
| Flock publish exporter | Writes `PUBLISH.json` with every entry pending human review; publication needs G. `brief` writes a private 0600 partner/press brief draft from approved entries; never sent or built. | `tools/flock_publish.py`, `tools/flock_publish_stage.py`, `tools/flock_publish_brief.py`, `skills/flock-publish/` |
| Flock store backup | Local create, verify and restore of the private flock store (agencies/, regions/, LAW.csv) with a SHA-256 manifest; no upload, encryption or schedule. | `tools/flock_backup.py`, [BACKUP.md](BACKUP.md) |
| Flock store check | Read-only run of every flock checker over one private store, agency slugs found in one place but not the others, and the newest backup's age. | `tools/flock_store_check.py` |
| Offline records pipeline | `records run` preserves intake and advances extract, catalog, detect, review with an independent challenge, compare and privacy in one ledger. This command does not publish or send; approval and publication are separate commands. | `campaign_tool/records/run.py` |
| Local OCR helper | Bounded local OCR for preserved PDF pages; extracted text remains unreviewed and requires visual review. This shipped helper does not establish live OCR wiring. | `campaign_tool/records/extract/ocr.py`, [RECORDS-OCR.md](RECORDS-OCR.md) |
| Offline starter CLI | Agency discovery, request kit, static site build, newsletter drafts, Legistar import, backup/verify/restore. | `README.md`, [LAUNCH.md](../LAUNCH.md) |

## Offline-only, frozen (G 2026-10-31)

No new features until a deploy is scheduled; path-scoped CI keeps them tested.

| Part | State | Evidence |
| --- | --- | --- |
| Cloudflare Workers | Setup wizard, organizer workspace and public-site Worker; tested offline, not deployed. | `workers/`, [WORKERS.md](WORKERS.md) |
| Runner | Container that pulls jobs from the workspace Worker; tested offline with `--fake`, not attached to a deployed workspace. | `runner/`, [RUNNER.md](RUNNER.md) |
| Hosted send path | `campaign_tool/outbox.py` journal and the opt-in signed outbox gateway; no Worker sender is registered and no live send has been made. | `campaign_tool/outbox.py`, `runner/outbox_gateway.py`, [OUTBOX.md](OUTBOX.md), [HOSTED-OUTBOX-GATEWAY.md](HOSTED-OUTBOX-GATEWAY.md) |

## Planned / not started

Only items with no implementation in this repository.

| Item | Evidence that it is not built |
| --- | --- |
| Binding the outbox to the Worker `send_request`/`send_followup` executors | They refuse with `sender_not_configured`: `workers/workspace/src/executors/types.ts` |
| Runner handlers for `newsletter_draft` and `backup` | No handler in `runner/handlers/`; the jobs are answered `blocked` |
| Social posting | `post_social` is `not_implemented`: `workers/workspace/src/views/screens.ts` |
| Independently reviewed state-law packages | California ships `status: draft`: `jurisdictions/us-ca/package.json` |
| Brevo webhooks and list counts | Only a schema column exists: `workers/schema/d1.sql` |
| Automatic Legistar polling from the Worker | The CLI import exists; the Worker only lists imported meetings: [MEETINGS.md](MEETINGS.md) |
| Backup encryption, scheduling and hosted restore | `campaign_tool/backup.py` writes a plain tar by hand; encryption and storage are left to the operator |
| Monitored scheduling and cost controls for the hosted path | No hosted schedule exists; see the frozen rows above |
| Any production deployment of the Workers or the runner | Frozen above until a deploy is scheduled |

See [LAUNCH.md](../LAUNCH.md) for the launch gates and [ROADMAP.md](ROADMAP.md)
for the longer plan.
