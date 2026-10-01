# Cloudflare Workers: wizard, workspace, public site

`workers/` holds the three Workers described in [CONTRACTS.md](CONTRACTS.md) and
[ARCHITECTURE.md](ARCHITECTURE.md). They are TypeScript, share one npm workspace, and are
tested offline with `@cloudflare/vitest-pool-workers` (workerd, D1, R2 and KV emulated in
process; no account, no network). Nothing in this directory has been deployed to a
production campaign; the pilot's private repository still runs its own code.

| Package | Purpose | Status |
| --- | --- | --- |
| `workers/shared` | Ports of `campaign_tool` pieces the Workers need: `discovery.locate`/`agencies_for`, `kit.render_combined_request`, `review.review_blockers` (+ `content_hash`), identity minting, the site shell. | Implemented; parity fixtures |
| `workers/wizard` | Public setup flow (seven screens), encrypted session, provisioning plan/apply. | Implemented; `DRY_RUN=1` records calls only |
| `workers/workspace` | Access-protected organizer UI + API, runner API, approvals, cron, inbound mail. | Implemented core; sends are interfaces |
| `workers/public-site` | Serves `sites/<version>/` from the public bucket with the site's `_headers` CSP. | Implemented |

## Commands

```sh
cd workers
npm ci
npm run sync        # copy data/agencies, jurisdictions, schema and CSS into the packages
npm run check       # sync:check + typecheck + tests (builds the Worker bundles first)
```

`npm run sync:check` fails when a generated copy is stale. `scripts/build-workspace-bundle.mjs`
bundles the workspace and public-site Workers with esbuild into
`wizard/src/generated/bundles.ts` (gitignored) so the wizard can upload them.

Local development: copy each package's `dev.vars.example` to `.dev.vars` (gitignored, and
the credential scan forbids tracking it) and run `npx wrangler dev` in the package.
`wrangler.jsonc` files carry no account values; every binding id is `local`.

## Wizard (`workers/wizard`)

Screens: 1 Location (offline resolution against the California seed, same ordering as
`campaign_tool kit`), 2 Campaign (name, tagline, domain, privacy tier, schedule, organizer
email), 3 Agencies (sheriff, police, county board and city council checked by default;
district attorney and CHP not), 4 Requests (one combined draft per selected law-enforcement
agency; channel `email | muckrock | portal_manual`; fee cap), 5 Accounts (Cloudflare token
with the exact minimum scope list, account and zone ids, Zero Trust team; Email Routing or
IMAP/SMTP mailbox; optional Brevo; model provider, which for `strict_local` must be a loopback
or Tailscale URL; neutral cost table), 6 Deploy (plan, then apply), 7 Handoff.

Session state is an AES-GCM blob in KV (`SESSION_KEY`, 64 hex chars) that expires one hour
after creation; the cookie holds only a random id. Account tokens exist only in that blob
until apply succeeds, then the session is rewritten without them; the generated runner token
is shown once on the handoff screen. `launch-receipt.json` lists resource ids and receipts
and never contains a secret.

The plan is an ordered list of Cloudflare API calls with placeholders resolved from earlier
outputs: token and zone verification; D1 database; R2 buckets `<slug>-originals` and
`<slug>-public`; KV namespace; Queue; the two Worker scripts (multipart upload of the built
bundles with bindings); D1 migrations and a seed of the campaign, agencies and drafts; DNS
(`AAAA 100::`, proxied) and Worker routes for `<domain>` and `workspace.<domain>`; an Access
application and an allow policy for the organizer email; Email Routing enable + rule for
`requests@<domain>`; Worker secrets; cron triggers. Apply writes one receipt per resource,
stops at the first failure, and returns the rollback list (newest first). The rollback calls
are listed, not executed, unless `rollbackOnFailure` is set by the caller; the wizard UI does
not execute them.

`DRY_RUN=1` makes the client record every call without sending it and return synthetic ids,
so the whole flow can be exercised offline. Production deployment still requires the owner
approvals in AGENTS.md.

## Workspace (`workers/workspace`)

Authentication: every request except `/api/runner/*`, `/dl/*` and `/healthz` must carry a
Cloudflare Access JWT (`Cf-Access-Jwt-Assertion` header or `CF_Authorization` cookie). The
Worker verifies RS256 with WebCrypto against `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`
(cached one hour, one refresh on an unknown `kid`), checks `iss`, `aud`, `exp` and `nbf`, and
takes the identity from the token's `email` claim. The `Cf-Access-Authenticated-User-Email`
header alone is never trusted. Runner endpoints use `Authorization: Bearer <RUNNER_TOKEN>`
with a constant-time comparison.

Screens: Dashboard, Requests (+ detail with the correspondence timeline and a "Draft
follow-up" button that enqueues a `draft_followup` job), Inbox, Records (signed ten-minute
download links served by the Worker; the bucket has no public URL), Findings (review gate
computed by the `review_blockers` port; review receipts take the reviewer from the Access
identity), Approvals (Approve / Edit / Reject / Execute; `approved_by` is the Access email),
Publish (`deploy_site` cards, publications, manifest rebuild), Subscribers (counts, newsletter
drafts with Approve & send, send receipts), Meetings (manual add, comment-kit generator),
Settings (non-secret Brevo settings and the test-signup checklist, backup job button, secret
presence only; runner-token rotation shows the new value once and stores its fingerprint).
`GET /api/export.json` streams the D1 export ([BACKUP.md](BACKUP.md)).

Runner API, exactly per CONTRACTS.md: `GET /api/runner/jobs?lease=300` (single
`UPDATE ... RETURNING` lease, 204 when nothing is runnable; expired leases requeue until
`max_attempts`), `POST /api/runner/jobs/:id/result`, `GET|PUT /api/runner/originals/:sha256`
(PUT verifies the hash before storing), `POST /api/runner/proposals` (idempotent on
`idempotency_key`), `POST /api/runner/correspondence` (deduped on `provider_message_id`),
`POST /api/runner/receipts` (`receipt_id = sha256([source_id, sha256])`),
`PUT /api/runner/site/:version/*path` (stages one public-site file at `sites/<version>/<path>`
in the public bucket after path, extension, size and `x-object-sha256` checks; nothing is
served until a `deploy_site` card flips `site_version`). A `done` result may carry
`outputs.followups`, which the Worker enqueues idempotently (known kinds only, never
`send_request`); a `classify_mail` result's `outputs.correspondence_update` patches only the
classification fields of its correspondence row.

`scheduled()` writes a `run_receipt` and enqueues `intake`, `digest` and, for requests whose
determination date has passed without a production or denial, `draft_followup`, with
idempotency keys per schedule slot. `email()` stores the raw MIME at `mail/<sha256>` in the
originals bucket, inserts one `correspondence` row per Message-ID and enqueues
`classify_mail`. `POST /api/deploy-site` executes an approved `deploy_site` card, which flips
the `site_version` setting (D1) and the KV key the public-site Worker reads.

Executors (`src/executors/`): every card kind resolves to a deterministic outcome.
`publish_finding` re-checks the review gate and content hash, writes the publication,
enqueues `build_site` with the full content manifest and proposes `deploy_site`
([PUBLICATION.md](PUBLICATION.md)); `deploy_site` flips the served version;
`send_newsletter` creates and sends one Brevo campaign ([NEWSLETTER.md](NEWSLETTER.md));
`send_request` and `send_followup` hand the draft to the `MailSender` port, whose default
`NotConfiguredSender` fails the card with `sender_not_configured`; `post_social` fails with
`not_implemented` and `pay_fee` with `manual_only` (fees are paid by a person and recorded on
the card). Execution is idempotent: an executed card returns its receipt, a failed one records
`<code>: <message>` and an incident. No code path sends without an approved row.

## Tests

`npm test` runs, per package: shared (location resolution, agency ordering, default
selection, request draft parity, review parity fixture, ids); workspace (Access JWT
valid/invalid/expired/wrong audience with a generated RSA key, no trust in the email header,
runner token, lease atomicity and requeue, job results, proposal idempotency, originals hash
mismatch, receipts, correspondence dedupe, approvals requiring identity, every executor with
fakes (publish gate refusals, Brevo create+send, not-configured sender, manual-only fee,
idempotent re-execution), the review/propose/correct/withdraw HTTP flow, newsletter draft
proposals, meetings and comment kits, `/api/export.json` and `/api/backup`, inbound mail
dedupe, cron job creation including the monthly newsletter draft); wizard (session encrypt/decrypt/tamper/
expiry, location resolve, default selection, plan lists every resource, DRY_RUN receipts and
rollback, stop on failure with rollback, multipart upload and secret wiring, full HTTP flow
with no token persisted after handoff); public site.

The review parity fixture `tests/fixtures/review-cases.json` is checked by both
`tests/test_review_fixture.py` (Python reference) and `workers/shared/test/review.test.ts`.

## Not done here

Binding the engine-side outbox ([OUTBOX.md](OUTBOX.md)) to the Worker's `MailSender` port, runner
handlers for `newsletter_draft` and `backup` (the runner in `runner/` handles classify, extract,
digest, follow-up drafts and site builds; see [RUNNER.md](RUNNER.md)), social posting, Brevo
webhooks, queue consumers beyond the poll loop, Access group management beyond the organizer
policy, and any production deployment. See [ROADMAP.md](ROADMAP.md).
