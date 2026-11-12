# Status: what is live, offline-only, frozen and planned

Status as of 2026-11-09; update this page in the PR that changes a row.

This is the one current status page. Older milestone documents keep their
original text for provenance and carry a "Historical milestone text" banner;
where they disagree with this page, this page is current. Every row names the
file or document that is its evidence.

## Live

| Part | What it does | Evidence |
| --- | --- | --- |
| Records engine run | One records run per timer slot (default Mon..Fri 07:00 and 15:00 America/Los_Angeles; days, times and zone are render values, and no weekend slot exists): overlap lock, image pin check, the stage orchestrator, then a non-fatal notify. Its machine-valid run report is review-required, not semantic acceptance of the run. | `deploy/records-runtime/README.md`, `campaign_tool/records/run_orchestrator.py`, `schemas/records-run-report-v1.json` |
| Records stages | Sync, delta, adoption, portal, extract, digest_gates and outputs, then non-fatal notify, which sends a metadata-only Telegram message. Phase 2 OCR targets are optional, not the default live extract; OCR live wiring (T6) is not done. | `deploy/records-runtime/README.md`, [RECORDS-OCR-STAGE.md](RECORDS-OCR-STAGE.md) |

Which engine build is installed is recorded by the Flock coordinator, not in this repository.

## Shipped, used by operators (no service)

These run by hand from a checkout. None of them installs a schedule or
publishes anything. None sends mail, except the records console's optional,
separately configured send of one approved draft, which is off by default and
test-recipient-only (see the records console rows).

| Part | What it does | Evidence |
| --- | --- | --- |
| Readiness snapshot | Offline doctor projection, fail-closed privacy/identity checks; runner-authenticated setting transport and 36-hour age-aware readback; not deployed or scheduled. | `schemas/readiness-snapshot-v1.json`, `workers/workspace/src/readiness.ts`, [WORKERS.md](WORKERS.md) |
| Organizer entry screen | Access-protected workspace home: recorded gates, request-state groups, age-aware readiness and named organizer actions; prior dashboard at /status; not deployed by this change. | `workers/workspace/src/views/home.ts`, [WORKERS.md](WORKERS.md) |
| Threat model | Categories, existing safeguards, gaps and an advisory trust ranking. | [THREAT-MODEL.md](THREAT-MODEL.md) |
| Flock facts, law-policy, compare | Cited facts, law and policy text, and policy comparison for one agency. | `tools/flock_facts.py`, `tools/flock_law_policy.py`, `tools/flock_compare.py`, `skills/flock-facts/`, `skills/flock-law-policy/`, `skills/flock-compare/` |
| Flock verify | Independent check of a run's findings against their citations. | `tools/flock_verify.py`, `skills/flock-verify/` |
| Flock follow-up and email drafts | Follow-up and email drafts and a request reminder list only; they write or print files and nothing sends. Approval is a signed `EMAIL-APPROVAL.json` field (docs/APPROVALS.md). | `tools/flock_email.py`, `tools/flock_approve.py`, `tools/flock_requests.py`, `skills/flock-followup/`, `skills/flock-email/`, `skills/flock-correspondence/` |
| Flock dashboard | Private static snapshot of recorded request state, human gates and provisional reminders; rebuild before use. | `tools/flock_dashboard.py` |
| Flock reminder alerts | Provisional reminder-date and receipt-unknown alerts for one date as stdout and an optional new 0600 `flock-alerts-v1` file; the organizer sets the receipt threshold; nothing sends; not a legal due date. | `tools/flock_alerts.py`, `skills/flock-followup/` |
| Records mail poll | `records mail-poll` reads the records inbox once over implicit TLS (read-only), hands each new message to intake with a receipt and advances a cursor; credentials stay in the operator's 0600 file; nothing sends, no timer is installed; output is review-required. | `campaign_tool/records/mail_poll.py`, docs/IMAP-CONFIG.md |
| Flock request state | One explicit state per request and per email run, from recorded fields only (never from free text); prints, writes and sends nothing; not a legal status. | `tools/flock_state.py`, `skills/flock-followup/` |
| Flock deadlines | Open requests past the law package's dated determination rule (provisional reminders while the package is draft); one follow-up draft each, filed `pending` in a private queue with a `flock-deadlines-v1` receipt; never sends or approves. | `tools/flock_deadlines.py`, `skills/flock-followup/` |
| Flock agency timeline | One agency's recorded history oldest first (requests, steps, fees, email-run states, document counts) and its open requests with a provisional reminder; `index` gives one status line per agency (counts, last event, next provisional reminder, failing runs); prints only; not a legal status. | `tools/flock_agency.py`, `skills/flock-sweep/` |
| Flock historical coverage | Private per-agency report of the earliest evidenced installation, policy and vendor dates, each cited to a held document, every other period shown as unevidenced; reads only; review-required; not a legal finding. | `tools/historical_coverage.py`, `schemas/historical-coverage-v1.json` |
| Flock monitor, sweep, region | Index-page monitoring (links recorded, never fetched), watch-list sweep status and plans, a ranked read-only daily to-do list, county region scaffolds. | `tools/flock_monitor.py`, `tools/flock_sweep.py`, `tools/flock_today.py`, `tools/flock_region.py`, `skills/flock-monitor/`, `skills/flock-sweep/`, `skills/flock-region/` |
| Flock review, report, page | The staged review run (facts-import imports into a staging store and reconciles, so a human fact review keeps facts-import and the model marks valid and only the report re-renders; changed evidence still invalidates downstream), its verify sample, golden check and batch runner, its report draft and a page draft. | `tools/flock_review.py`, `tools/flock_review_verify.py`, `tools/flock_review_golden.py`, `tools/flock_review_batch.py`, `tools/flock_report.py`, `tools/flock_page.py`, `tools/flock_privacy.py` (email/phone/plate patterns shared by the report, page and publish lints), `tools/flock_privacy_scan.py` (same patterns over a private draft folder; path:line: category only), `skills/flock-review/`, `skills/flock-page/` |
| Flock publish exporter | Writes `PUBLISH.json` with every entry pending human review; publication needs G. `brief` writes a private 0600 partner/press brief draft from approved entries; never sent or built. | `tools/flock_publish.py`, `tools/flock_publish_stage.py`, `tools/flock_publish_brief.py`, `skills/flock-publish/` |
| Flock redaction workflow | Private text-only proposals, verified human signature sign-off and staging-validated immutable derivatives; originals unchanged, nothing publishes. | `tools/flock_redact.py`, [REDACTION.md](REDACTION.md) |
| Flock redaction records | Read-only proposal, trusted sign-off and derivative eligibility; exporter refuses missing originals and required-but-ineligible redactions. Eligibility never approves publication. | `tools/flock_redaction_record.py`, [REDACTION.md](REDACTION.md) |
| Flock review queue | Private challenge and receipt report for human review; never approves or publishes. | `tools/flock_review_queue.py` |
| Custody report | One private page per published finding: the source hashes and re-hash result, when and from which agency each source was received, its preservation receipt, the review receipts bound to the current content, and every missing link named. Read-only and never published. A hash establishes byte identity, not authenticity. | `tools/custody_report.py`, `schemas/custody-report-v1.json`, [RECONCILIATION.md](RECONCILIATION.md) |
| Publication gate | One check every publishing path (`finding publish`, flock stage, flock brief) calls before it emits: a named, content-matched approval or a refusal with a reason; no code writes an approval. | `campaign_tool/publication_gate.py`, `tests/test_publication_gate.py`, [PUBLICATION.md](PUBLICATION.md) |
| Flock store backup | Local create, verify and restore of the private flock store (agencies/, regions/, LAW.csv) with a SHA-256 manifest; no upload, encryption or schedule. | `tools/flock_backup.py`, [BACKUP.md](BACKUP.md) |
| Flock store check | Read-only run of every flock checker over one private store, agency slugs found in one place but not the others, and the newest backup's age. | `tools/flock_store_check.py` |
| Store inventory | Private review-required shape and category counts for an admitted store; no policy decisions. | `tools/store_inventory.py` |
| Agency chain rehearsal | Walks one named agency through every records-chain stage (verified, drafted, approved, sent, response, preserved, extracted, OCR, catalogued, detected, reviewed, published) from recorded fields and writes one private 0600 receipt naming each stage's state, the human step owed and the first break; OCR reads `not-wired`; reads only, sends and publishes nothing. | `tools/rehearse_agency.py`, [OFFLINE-LOCALITY-REHEARSAL.md](OFFLINE-LOCALITY-REHEARSAL.md) |
| Flock store init | Creates a new empty private store (header-only LAW.csv and per-agency FACTS v2/POLICY, 0700/0600) all-or-nothing; refuses a STORE that exists and is not an empty folder; writes no contacts or requests. | `tools/flock_store_init.py` |
| Records search | One private local index over the extracted text of every preserved record in a private store; hits cite document id, page, byte offset, request, agency and sha256 and never print document text; every result ends with indexed/no-text/unreadable coverage; the index is refused under a repository or public output path and `tools/check_public_tree.py` refuses its file name; extracted text stays unreviewed. | `tools/records_search.py`, [RECORDS-EXTRACTION.md](RECORDS-EXTRACTION.md#search) |
| Agency evidence pass | Fetches the index-page links the seed already records and writes one cited `agency-evidence-v1` row per agency - a verdict, its citations, and what was not checked; a verdict is a field decided from the citations alone (evidenced / likely / no-evidence / unreachable / not-checked), never an uncited verdict; writes nothing into the seed. | `tools/agency_verify.py`, `schemas/agency-evidence-v1.json` |
| Originals drift check | Read-only, review-required alerts for originals and ledger drift; no repair, schedule or delivery. | `tools/reconcile_auto.py`, [ORIGINAL-RECONCILIATION.md](ORIGINAL-RECONCILIATION.md) |
| Records host check | Read-only preflight: whether this machine can run the records container, one pass/fail/unknown row per requirement with the attended remedy; installs, pulls and starts nothing. | `tools/records_host_check.py`, [RECORDS-CONTAINER-HOST.md](RECORDS-CONTAINER-HOST.md) |
| Stale-stage check | Read-only alert when a records stage has not produced in the last N scheduled run reports while it had work, failed or was skipped; reads reports only, installs and sends nothing. | `tools/stage_stale_check.py`, `deploy/records-runtime/README.md` |
| Offline records pipeline | `records run` preserves intake and advances extract, catalog, detect, review with an independent challenge, compare and privacy in one ledger. This command does not publish or send; approval and publication are separate commands. | `campaign_tool/records/run.py` |
| Local OCR helper | Bounded local OCR for preserved PDF pages; extracted text remains unreviewed and requires visual review. This shipped helper does not establish live OCR wiring. | `campaign_tool/records/extract/ocr.py`, [RECORDS-OCR.md](RECORDS-OCR.md) |
| Run report review summary | Read-only summary of a records run report (or a reports directory): machine-valid, stage work (unknown never 0) and human review still owed per gate, kept apart; marks nothing reviewed. | `tools/run_report_review.py`, `deploy/records-runtime/README.md` |
| Campaign local schedule (not installed) | Six shipped systemd user timer pairs for gate-safe campaign slots (weekday: stale-check, deadline drafts, reconcile, run-report-review, site build; daily: backup) and a read-only check of each slot's command, gate safety, time collisions and lock; installs, enables and starts nothing; the install is an attended operator step. | `tools/local_schedule_check.py`, `deploy/local-schedule/install.md` |
| Collection triage | Read-only first pass over a returned collection's extracted text: a kind (responsive, policy, contract, search-log, boilerplate, duplicate, unsorted), a confidence and a cited reason per member, and a review queue that lists every member; every row stays unreviewed, nothing is hidden, marked reviewed or published. | `tools/collection_triage.py`, `schemas/collection-triage-v1.json`, [RECORDS-DETECTORS.md](RECORDS-DETECTORS.md) |
| Offline starter CLI | Agency discovery, request kit (with `kit-repeat` follow-on drafts), static site build, newsletter drafts, meeting import for Legistar, CivicClerk, Granicus, AgendaCenter and iCalendar (ICS) agendas (`meetings --platform` `legistar`, `civicclerk`, `granicus`, `agendacenter` or `ics`; read from a saved file unless `--online`; ALPR keyword leads to verify by hand; each platform takes its own options), backup/verify/restore. | `README.md`, [LAUNCH.md](../LAUNCH.md), `campaign_tool/meetings.py`, [MEETINGS.md](MEETINGS.md) |
| Campaign backup and recovery | `backup` writes a local, verified archive of a campaign directory (`--into DIR` names it by time and never overwrites); `verify`, `restore` and `backup-diff` read it. Optional `backup --encrypt-to KEY` encrypts it with OpenPGP (needs GnuPG and the organizer's public key) and leaves nothing unencrypted beside it. `backup-list` re-verifies a folder and reports what a keep rule would keep, deleting nothing; `backup-drill` restores into a temporary copy, checks it there and removes it. Local files only: no upload, hosted restore or retention deletion; the daily timer is shipped but not installed (Campaign local schedule row). | `campaign_tool/backup.py`, `campaign_tool/backup_crypt.py`, [encrypted archives](BACKUP.md#encrypted-archives), [keep rule](BACKUP.md#archive-inventory-and-keep-rule), [restore drill](BACKUP.md#restore-drill) |
| Records console: pages | `records panel` serves private pages bound only to literal 127.0.0.1 (stdlib; not installed, enabled or started by this repository): Home decision queue, request board and agency timeline, documents and findings, calendar, project oversight, jobs and the pipeline dashboard; `--render` writes a static snapshot without forms. Each source fails on its own and is named unavailable, never shown as passed; overdue marks are arithmetic on our records, not a legal finding; findings stay review-required. | `campaign_tool/records/admin_panel.py`, `campaign_tool/records/console_board.py`, `campaign_tool/records/console_documents.py`, `campaign_tool/records/console_calendar.py`, `campaign_tool/records/console_oversight.py`, [RECORDS-ADMIN-PANEL.md](RECORDS-ADMIN-PANEL.md), [RECORDS-CONSOLE-BOARD.md](RECORDS-CONSOLE-BOARD.md), [RECORDS-CONSOLE-DOCUMENTS.md](RECORDS-CONSOLE-DOCUMENTS.md), [RECORDS-CONSOLE-OVERSIGHT.md](RECORDS-CONSOLE-OVERSIGHT.md) |
| Records console: review and approvals | Draft review and edit with history, reject with a reason, and approve with a detached signature made elsewhere (the console holds no key); approval marks a draft approved-to-send and never sends or publishes. Packet approve/reject and public-update veto, off and kill never write a site change or turn auto-post on. Sending one approved draft needs `--send-config`, its own outbox signature and a typed confirmation, and is off by default and test-recipient-only until G sets `live` in an owner-only file; this is not evidence of any live send. Agency page publishing, redaction sign-off and finding review are not connected to the console. | `campaign_tool/records/admin_panel.py`, `campaign_tool/records/console_packets.py`, `campaign_tool/records/mail_send.py`, [RECORDS-CONSOLE-APPROVALS.md](RECORDS-CONSOLE-APPROVALS.md), [sending an approved draft](RECORDS-ADMIN-PANEL.md#sending-an-approved-draft) |
| Findings about several agencies | Optional `also_agency_ids` on a published finding (1 to 10 known agencies besides `agency_id`): one finding page and one open-data row, listed on every named agency page and feed, in the filter and in related findings. The field is reviewed content: changing it needs a correction with fresh review. Findings without it render unchanged. | `campaign_tool/content.py`, `campaign_tool/site.py`, `tests/test_finding_also_agencies.py`, [SITE-CONTENT.md](SITE-CONTENT.md) |
| Quoted source excerpts | Optional `sources[].quote` on a published finding (1 to 400 characters copied by hand from the cited record, plain text, a locator required): shown as a blockquote under its source line on the finding page and the packet with "Transcribed by the campaign; not checked against the record text." and the page when given, carried in the open-data findings file, and part of the reviewed content (changing it needs a correction with fresh review). Nothing checks it against the record. Findings without it render unchanged. | `campaign_tool/content.py`, `campaign_tool/site.py`, `tests/test_finding_source_quote.py`, [SITE-CONTENT.md](SITE-CONTENT.md) |
| Campaign register | `campaigns add\|list\|remove\|status` keeps a private register of campaign names and paths; every command takes `--campaign NAME` or `--directory D`; one private status row per campaign, counts and waiting gates only. Local only; hosted multi-campaign is an open owner decision. | `campaign_tool/registry.py`, [MULTI-CAMPAIGN.md](MULTI-CAMPAIGN.md) |

## Hosted path: deploy scheduled, nothing provisioned (G 2026-11-01)

G's 2026-11-01 decision approves planning and then spending, with the cost shown to G before anything is provisioned; it is not standing approval to provision.
The hosted path is not deployed, and no deploy receipt exists; the fresh-account launch gate in [LAUNCH.md](../LAUNCH.md) is **Not run**. Path-scoped CI keeps this code tested.

- Setup wizard: guides an organizer through account setup and a provisioning plan.
- Organizer workspace: provides the protected campaign workspace, jobs and approvals.
- Public-site Worker: serves the campaign's public site.
- Runner: processes workspace jobs in a container.

Before serving anyone, the hosted path still needs:
- A registered outbox sender in the workspace (`df-bind-sender`): the setup wizard can now write the settings, but no gateway or key is provisioned.
- Account configuration and verification for the social posting adapter (`df-social-posting`), now implemented offline.
- Provider configuration and verification for the Brevo webhook and list count (`df-subscribe`); the code is tested offline.
- Feed configuration and deployment for the Legistar poll (`df-legistar-poll`). The poll is implemented offline.
- Deployment and integration verification for the two runner handlers (`df-runner-handlers`), now implemented offline.
- A job budget / spend cap: **GAP**, no sheet item covers this.

| Part | State | Evidence |
| --- | --- | --- |
| Cloudflare Workers | Setup wizard, organizer workspace and public-site Worker; tested offline, not deployed. | `workers/`, [WORKERS.md](WORKERS.md) |
| Runner | Container that pulls jobs from the workspace Worker; tested offline with `--fake`, not attached to a deployed workspace. It answers `newsletter_draft` (draft only) and `backup` (local archive) with a receipt; nothing is sent or uploaded. | `runner/`, `runner/handlers/newsletter_draft.py`, `runner/handlers/backup.py`, [RUNNER.md](RUNNER.md) |
| Hosted send path | `campaign_tool/outbox.py` journal and the opt-in signed outbox gateway; no Worker sender is registered and no live send has been made. | `campaign_tool/outbox.py`, `runner/outbox_gateway.py`, [OUTBOX.md](OUTBOX.md), [HOSTED-OUTBOX-GATEWAY.md](HOSTED-OUTBOX-GATEWAY.md) |
| Subscription webhook and list counts | HMAC-verified double opt-in events and our own confirmed `list_count`; counts only on any page; tested offline, no provider configured, sends nothing. | `workers/workspace/src/routes/subscribe_webhook.ts`, `workers/workspace/src/subscribers.ts`, [NEWSLETTER.md](NEWSLETTER.md) |

## Planned / not started

Only items with no implementation in this repository.

| Item | Evidence that it is not built |
| --- | --- |
| Registered sender for the Worker `send_request`/`send_followup` executors | Outbox code is bound, and the wizard's Outbox sender step writes `OUTBOX_GATEWAY_URL` and `OUTBOX_SIGNING_KEY` as a both-or-neither pair, but nothing is provisioned: without the pair a workspace refuses with `sender_not_configured`: `workers/workspace/src/executors/types.ts`. A deployed workspace still needs a registered sender. |
| Live social posting | `post_social` has one Mastodon adapter behind per-post approval, but no account is configured: `workers/workspace/src/executors/post_social.ts` |
| Independently reviewed state-law packages | California ships `status: draft`: `jurisdictions/us-ca/package.json` |
| Live Legistar polling from the Worker | `pollLegistar` runs inline on the cron slots and is tested offline. It is not deployed and `LEGISTAR_FEEDS` is not configured: [MEETINGS.md](MEETINGS.md) |
| Hosted backup storage, hosted restore and retention deletion | Local campaign backups, encryption, inventory and drill are shipped (Campaign backup and recovery row). The runner `backup` handler writes a local archive and uploads nothing: `runner/handlers/backup.py`. Restore of a hosted campaign into D1 and R2 is not written and `backup-list` deletes nothing: [BACKUP.md](BACKUP.md#not-done); hosted export only: [hosted campaigns](BACKUP.md#hosted-campaigns). |
| Monitored scheduling and cost controls for the hosted path | The schedule is authored in code but undeployed; budget and spend controls remain a gap. See [hosted path](#hosted-path-deploy-scheduled-nothing-provisioned-g-2026-11-01). |
| Any production deployment of the Workers or the runner | Scheduled, not provisioned; see [hosted path](#hosted-path-deploy-scheduled-nothing-provisioned-g-2026-11-01). |

Publishing paths: `campaign_tool/publish_paths.py` registers every output path and `tests/test_publish_path_coverage.py` proves each runs the shared privacy lint; see [PUBLICATION.md](PUBLICATION.md#publishing-paths-and-the-shared-privacy-lint-bf-df-pii-everywhere). A clean lint is not a clearance.

See [LAUNCH.md](../LAUNCH.md) for the launch gates and [ROADMAP.md](ROADMAP.md)
for the longer plan.
