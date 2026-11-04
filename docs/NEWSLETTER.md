# Newsletter

Monthly and post-publication updates to subscribers, sent through Brevo only after an organizer
approves a `send_newsletter` card. The workspace never imports, lists, exports or edits
provider contacts; it keeps only the double opt-in record described under Subscriptions below.
Code:
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
 "findings": [{"title", "summary", "classification", "confidence", "path", "published_at", "corrected",
               "corrections"?: [{"date": "YYYY-MM-DD", "note"}]}],
 "meetings": [{"body", "starts_at", "agenda_item", "agenda_url", "relevance"}]}
```

`corrections` is optional: the Worker manifest omits it, `--from-content` sets it. When present
it is a list of at most 50 objects with a `YYYY-MM-DD` `date` and a string `note`; anything else
is refused naming `findings[i].corrections[j]`.

Only published or corrected findings since the last send and upcoming meetings appear. No
subscriber, correspondence, original or credential data is in the manifest.
`build_draft` applies that window itself (findings with `published_at >= since`, meetings with
`starts_at >= now`, soonest first) and reports what it left out in `excluded`.

The runner turns it into a draft with `campaign_tool.newsletter.build_draft(manifest)`, which
returns `{"subject", "html", "text", "counts"}`. The body is rendered from a Markdown subset
through `markdown_lite`, so raw HTML never reaches the draft; the only post-render addition is
Brevo's `{{ unsubscribe }}` placeholder, inserted exactly once outside any manifest text. The
builder refuses manifests with angle brackets, email addresses or phone-like numbers, script
content, non-https links or over-long fields (`DraftError`).

Offline: `python3 -m campaign_tool.newsletter --directory D [--manifest FILE | --from-content
[--since YYYY-MM-DD]]` writes `kit/newsletter-draft.html`, `.txt` and `.json`. `--from-content`
builds the manifest from `content/`: published findings (`/findings/<slug>.html`, corrected
when they carry corrections) and meetings not `cancelled`; `--since` (UTC midnight) needs it.
The window above still applies, with one addition: a finding published before `--since` whose
latest correction is dated on or after that day is listed under "Corrections" as
`TITLE: corrected DATE — NOTE (link)` (note capped at 200 characters), is not counted as a new
finding or in the subject, and is counted in the JSON's `corrections_listed` (present only when
non-zero). A finding published and corrected after `--since` stays a new finding marked
"(corrected)". Without either flag it renders the empty template from `campaign.json`. The JSON
names its `source` (`content`, `manifest` or `template`). Nothing is sent.
`--site PUBLIC` (an existing folder, not a symlink, else `Stopped:` exit 1 and no draft) then checks
every campaign-site link of the written draft against that built `public/`: HTML links under `base_url`
(finding pages, `#ids`, the home link) or, without `base_url`, the plain-text `/findings/...` paths,
using the `build --check` resolver. The JSON gains `site_links` (`public`, `checked`, `external_skipped`,
`missing` as `{link, reason}` with `no_page` or `no_fragment`); each missing link is also printed to
stderr as `newsletter: link LINK not in PUBLIC (REASON)` and the exit is 3. Other hosts are only counted.

`--from-content` also lists, under **Decisions** (after New findings and Corrections, before Upcoming meetings),
each meeting outcome an organizer recorded with `meetings-outcome` (see MEETINGS.md) on or after
the `--since` day (all of them without `--since`), oldest first: body, date, result label, vote
when given and an "Official record" link to the https source. Only recorded outcomes appear; a
meeting without one is never described as decided. At most 8 lines, then "and N more on the
site."; `counts.decisions` is present only when the section is. Worker manifests carry no
`decisions` key and render exactly as before.

`--from-content` also lists, under **Records requests** (after New findings and Corrections, before
Decisions), each public `asked` item from `content/agencies.json` whose `asked_on` or `updated_on` is on
or after the `--since` day (all of them without `--since`) and not after the day the draft is built;
an item dated after the build day is left out. Each line is `AGENCY: TOPIC — SENTENCE`, where SENTENCE
is the agency page's own state sentence (`site.asked_sentence`, so the newsletter and the site never
disagree), followed by an "Agency page" link only when `campaign.base_url` is set (the agency's
`/agencies/<slug>.html` page, else its `/agencies.html#<slug>` anchor; never a relative link). The
organizer `note` and everything under `kit/` are never read. Newest activity first; at most 8 lines,
then "and N more on the site."; the heading is omitted when no item is kept and `counts.requests` is
present only when the section is. Worker manifests carry no `requests` key and render exactly as
before; a `requests` that is not a list is refused (`DraftError`).

`--from-content` also lists, under **New in the records library** (after Records requests, before
Decisions), each `content/sources.json` entry whose `received_at` day is on or after the `--since` day (all
of them without `--since`) and not after the day the draft is built. Each line is `**TITLE** (AGENCY), received
YYYY-MM-DD, record PREFIX`, with a "Public copy" link when the entry has a `public_url` and a "Sources page"
link (`/sources.html#<fragment>`) only when `campaign.base_url` is set. AGENCY is the agency name, else the
agency id the sources page shows. Only the 12-character record number is shown, never the full hash, `note`,
`pages` or the public-copy check. Newest first; at most 12 lines, then "and N more on the sources page.";
the heading is omitted when no entry is kept and `counts.records` is present only when the section is.
Worker manifests carry no `records` key and render exactly as before; a `records` that is not a list, or a
prefix that is not 12 lowercase hex characters, is refused (`DraftError`).

Under each upcoming meeting `--from-content` adds one indented line. With participation facts an
organizer verified (see MEETINGS.md) it reads `How to take part: In person: yes|no; Remote: HOW;
Written comments: HOW, by DEADLINE; Speaker limit: N minutes (checked DAY) [Official details](URL)`,
each part only when the source states it, the deadline in the meeting's own UTC offset. A deadline
already passed when the draft is built is left out (`excluded.comment_deadlines_passed`); a `how`
holding an email address or phone number is never copied ("see official details",
`excluded.contact_text_withheld`). Without verified facts: "Check the posted agenda for how to comment."

### Offline sends

The tool never sends. After sending `kit/newsletter-draft.html` (or `.txt`) by hand, record it
with `python3 -m campaign_tool.newsletter --directory D --record-sent YYYY-MM-DD` (a flag form of
`newsletter sent --date D`; it cannot be combined with `--manifest`, `--from-content` or `--since`).
It appends `{"sent_on", "draft_sha256", "subject", "recorded_at"}` to the private
`kit/newsletter-sent.json` (mode 0600, at most 1000 issues), where `draft_sha256` is the sha256 of
`kit/newsletter-draft.json`, which holds the draft's subject, HTML and text. It refuses a date that
is not a real day, after today or before 2000-01-01; a missing, linked or over-4 MiB draft; a
draft already recorded (naming its date); and a date earlier than the last record, so records stay
in date order. A linked, non-regular or malformed `kit/newsletter-sent.json` is refused
(`kit/newsletter-sent.json unreadable: ...`) and never reset or rewritten. Afterwards
`--from-content` without `--since` starts the day after the last recorded send (UTC midnight); an
explicit `--since` wins. The draft JSON output names where the window came from in `since_from`:
`"record"`, `"flag"` or `null`. This is the offline counterpart of the hosted `campaign_sent` event.

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

## Subscriptions, double opt-in and list counts

`POST /api/webhooks/subscribe` (`workers/workspace/src/routes/subscribe_webhook.ts`) records
provider subscription events. It needs no Access identity but trusts nothing unverified: each
delivery carries `x-subscribe-timestamp` (Unix seconds) and `x-subscribe-signature`, the hex
HMAC-SHA256 of `<timestamp>.<raw body>` under the `SUBSCRIBE_WEBHOOK_SECRET` Worker secret
(Brevo's webhook, or a relay in front of it, must sign this way). A delivery outside a
300-second window, with a bad or missing signature, or re-using a provider event id is refused
with its reason enum only and raises one deduplicated `subscribe_webhook:<reason>` incident.
Without the secret every delivery is refused `not_configured`. Bodies, headers and addresses
are never logged or echoed.

Body: `{"id": ..., "event": ..., "email": ..., "scope"?: ...}`. Double opt-in is enforced:
`subscribed` records a pending address that never counts; only `confirmed` (of a pending
address) counts, with its confirmed-at time and scope; `unsubscribed`, `bounced`/`hard_bounce`
and `complained`/`spam` remove it at once, with no confirmation step. A later `subscribed`
puts a removed address back to pending only; it counts again after a fresh `confirmed`.

After each accepted event a `list_count` event is written with our own count of confirmed
addresses and its as-of time; the Subscribers screen shows the newest one. A count in the
provider payload is ignored.

The address is stored once, in the `subscriber` table (`workers/workspace/src/subscribers.ts`),
which is not exported, rendered or reported. Events, counts and incidents carry a hash of the
address only. Nothing in this path sends mail: `send_newsletter` stays behind its approval card
and still fails `brevo_not_configured` without `BREVO_API_KEY`.

## Not done

A/B or segment sends, any transactional mail, subscriber export or deletion requests, and a
subscriber-facing preference page. Those stay behind the same approval-card rule when added.

## Consent footer

The draft's closing lines are the campaign's own reviewed wording, set in Settings > Brevo
(`consent_footer`: why the reader receives this, who sends it and from where; it must keep
the sentence "Unsubscribe at any time." so the provider link lands there). Without it the
engine appends a clearly marked template footer and the `send_newsletter` executor refuses
the draft (`template_consent_footer`): the engine's wording is never sent as a campaign's
consent statement by accident.
