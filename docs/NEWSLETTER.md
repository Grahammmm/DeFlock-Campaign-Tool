# Newsletter

Monthly and post-publication updates to subscribers, sent through Brevo only after an organizer
approves a `send_newsletter` card. The workspace never imports, lists, exports or edits
contacts; consent, suppression and unsubscribe stay inside the provider. Code:
`campaign_tool/newsletter/draft.py`, `workers/workspace/src/brevo.ts`,
`executors/send_newsletter.ts`, `manifest.ts` (`newsletterManifest`), `cron.ts`.

## Settings (non-secret)

Settings > Newsletter stores one `brevo` setting row: `list_id` (positive integer), hosted
signup `form_url` (`https://*.sibforms.com/...` only), `sender_name` and `sender_email`
(must be a verified Brevo sender). The Brevo API key lives only in the Worker environment; the
Settings page shows presence only. Saving these settings reads and writes no contacts.
The form URL sets `signup.mode = brevo_hosted` on the public site; the CSP and CAPTCHA
origin checks are in [CLOUDFLARE.md](CLOUDFLARE.md) step 8.

The test-signup checklist on the Settings page (`SIGNUP_TEST_CHECKLIST`) is CLOUDFLARE.md
step 9: one approved test address you control, double opt-in, welcome, list status,
unsubscribe, never reactivating a suppressed contact, and deleting the test contact
afterwards.

## Drafts

A `newsletter_draft` job is enqueued:

- by cron on the first run of each month (idempotency key `newsletter_draft:monthly:<YYYY-MM>`,
  so later ticks in the same month dedupe), and
- after every executed `publish_finding` (key `newsletter_draft:pub:<publication_id>`).

The job inputs carry a manifest assembled from D1 by `newsletterManifest(repo)`:

```json
{"schema_version": 1,
 "campaign": {"name": "...", "base_url": "https://..." | null, "county_name": "..."},
 "since": "<ISO of the last campaign_sent event>" | null,
 "findings": [{"title", "summary", "classification", "confidence", "path", "published_at", "corrected"}],
 "meetings": [{"body", "starts_at", "agenda_item", "agenda_url", "relevance"}]}
```

Only published or corrected findings since the last send and upcoming meetings appear. No
subscriber, correspondence, original or credential data is in the manifest.

The runner turns it into a draft with `campaign_tool.newsletter.build_draft(manifest)`, which
returns `{"subject", "html", "text", "counts"}`. The body is rendered from a Markdown subset
through `markdown_lite`, so raw HTML never reaches the draft; the only post-render addition is
Brevo's `{{ unsubscribe }}` placeholder, inserted exactly once outside any manifest text. The
builder refuses manifests with angle brackets, email addresses or phone-like numbers, script
content, non-https links or over-long fields (`DraftError`).

Offline: `python3 -m campaign_tool.newsletter --directory D [--manifest FILE]` writes
`kit/newsletter-draft.html`, `.txt` and `.json`. Without `--manifest` it renders the empty
template from `campaign.json`. Nothing is sent.

## Approve & send

Subscribers lists the draft jobs. Approve & send on a `done` draft creates a `send_newsletter`
card (`POST /subscribers/drafts/:job_id/propose`) whose proposal carries the subject, HTML,
text and list id; the route refuses drafts without the unsubscribe placeholder. The card
follows the normal Approve / Edit / Reject / Execute flow; `approved_by` is the Access identity.

`send_newsletter` executes by creating one Brevo email campaign for the configured list
(`POST /v3/emailCampaigns` with the idempotency key as tag) and sending it
(`POST /v3/emailCampaigns/{id}/sendNow`). It refuses with stable codes when
`BREVO_API_KEY`, the sender or the list id is missing (`brevo_not_configured`), when the
HTML lacks the placeholder or contains a script (`missing_unsubscribe`, `invalid_proposal`),
and surfaces provider errors as `brevo_error`. On success it writes a `subscriber_event`
(`campaign_sent`, Brevo campaign id, list id) that becomes the `since` boundary of the next
draft, and the card receipt holds the Brevo campaign id.

Tests use `FakeBrevo` (`workers/workspace/test/executors.test.ts`); no network call is made in
the suite.

## Ambiguous delivery and recovery

A returned provider campaign ID is stored before the send call. Creation timeouts and
inconclusive server errors use `brevo_create_ambiguous`; send failures after creation use
`brevo_send_ambiguous`. Re-proposing that same key leaves the failed card and original
proposal untouched. A retained provider receipt also holds the card if local bookkeeping
fails after a successful provider send. Known configuration/input rejections remain retryable.
The provider's tag is a lookup aid, not a promise of provider-side idempotency.

In **Approvals**, inspect the provider account for the action ID/tag and campaign receipt.
Record a provider-check reference and one conclusive result:

- **Delivered**: include the actual provider delivery time as a past UTC ISO timestamp.
  The card becomes executed and future executions return its existing receipt without
  calling the provider. The confirmed time becomes the newsletter's `since` boundary.
- **Not delivered**: the unchanged original draft and stable key return to proposed;
  approval identity is cleared and a fresh approval is required before sending.
- **Unknown**: leave the card held. Do not create a new key to get around the hold.

The authenticated `POST /approvals/:id/reconcile` accepts `outcome`, `reference`, and
`delivered_at` (required for delivered). It does not call Brevo. The receipt preserves the
previous provider identity/error and checking identity. An attempted check is journaled;
a conditional D1 transaction then records the state transition and its event together.
This uses [Cloudflare’s documented transactional batch semantics](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch). Stale concurrent checks cannot overwrite another result. A storage failure rolls back the
transition; the card stays held. This is an organizer attestation, not automated verification
of provider state. No new permissions, contact reads, subscription changes or real sends
are introduced by this route.

## Interrupted execution

If a new newsletter execution is interrupted while `executing`, use its exact
claim ID and an interruption reference to **Hold interrupted execution** in
Approvals. This records a durable held card; it does not cancel or resend. A
late creation result cannot continue to send after the hold, and a late completion
cannot overwrite it. Already admitted provider work may still finish. Inspect
the card's **Inspect this action's recovery and late provider receipts** link
(`GET /approvals/:id/execution-evidence`, Access-protected) or
`late_execution_receipt` entries in the Subscribers event log along with the
retained campaign ID and original action/key/tag when checking provider state.
The action-specific view returns at most 50 records and flags `truncated` when
more exist; use the authenticated full export for an older evidence history.

Before recording either delivered or not-delivered, prove that the old invocation
has stopped and record its termination evidence reference and explicit confirmation.
Then check the provider, including any queued work. A timeout, old timestamp,
fetch abort or the hold itself is insufficient. If either outcome is uncertain,
leave the card held. A confirmed absent delivery returns the same draft/key to
proposed with approval cleared; a confirmed delivery records the actual provider
time and cannot be resent. No caller is trusted to have terminated merely because
its database claim has been fenced. These are operator attestations, not automatic
verification of the referenced platform/provider receipts.

The implementation uses claim-bound database writes and atomic hold/event storage;
it adds no schema migration or provider cancellation call. Legacy/unbound executions
and non-newsletter recovery remain separate acceptance work. The default
records-request sender is still unconfigured. Synthetic tests do not establish a
live hosted pilot, and this change does not activate production.

## Not done

Brevo webhook ingestion for bounces and list counts (the Subscribers screen shows
`list_count` events if something writes them), A/B or segment sends, and any transactional
mail. Those stay behind the same approval-card rule when added.

## Consent footer

The draft's closing lines are the campaign's own reviewed wording, set in Settings > Brevo
(`consent_footer`: why the reader receives this, who sends it and from where; it must keep
the sentence "Unsubscribe at any time." so the provider link lands there). Without it the
engine appends a clearly marked template footer and the `send_newsletter` executor refuses
the draft (`template_consent_footer`): the engine's wording is never sent as a campaign's
consent statement by accident.
