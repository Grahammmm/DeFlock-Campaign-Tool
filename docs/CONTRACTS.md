# Shared contracts

These interfaces are shared across the Python engine, the Cloudflare Workers, and the
runner. Change them in their own PR with tests in every consumer. Every identifier is
stable text; every timestamp is ISO-8601 UTC; every hash is lowercase hex SHA-256.

## Layout

| Path | Owner | Purpose |
| --- | --- | --- |
| `campaign_tool/` | Python engine | CLI, records pipeline, kit generation, site build, digest, outbox |
| `jurisdictions/<country>-<state>/package.json` | Law packages | Reviewed rules as data (`schemas/law-package.schema.json`) |
| `data/agencies/<country>-<state>.json` | Agency seeds | Discovery seed per state (`schemas/agency-seed.schema.json`) |
| `workers/wizard/` | Wizard Worker | Public setup flow, provisioning plan/apply |
| `workers/workspace/` | Workspace Worker | Access-protected UI + API over D1/R2, inbox handler, cron, queue |
| `workers/schema/d1.sql` | D1 schema | Single source of truth for campaign state |
| `runner/` | Runner | Container image; pulls jobs, runs engine, posts results |
| `schemas/*.schema.json` | Everyone | JSON Schema for every cross-boundary document |

## Identities

- `campaign_id`: 26-char lowercase ULID-like string, minted by the wizard.
- `agency_id`: `<state>-<slug>` from the seed (`ca-san-luis-obispo-county-sheriff`).
- `request_id`: `req_` + 16 hex; `correspondence_id`: `cor_` + 16 hex.
- `object_sha256`: the original bytes' hash; `receipt_id`: sha256 of `[source_id, object_sha256]` (matches `cli.ingest`).
- `finding_id`: `fnd_` + 16 hex; `job_id`: `job_` + 16 hex; `receipt` for external actions: `act_` + 16 hex.

## Law package (`schemas/law-package.schema.json`)

```json
{
  "schema_version": 1,
  "jurisdiction": "us-ca",
  "status": "draft | reviewed",
  "reviewed_by": [], "reviewed_at": null,
  "records_law": {
    "name": "California Public Records Act",
    "citation": "Gov. Code § 7920.000 et seq.",
    "determination_days": 10, "determination_extension_days": 14,
    "day_type": "calendar", "fee_basis": "direct cost of duplication",
    "appeal": "...", "sources": [{"title": "...", "url": "https://leginfo...", "accessed": "2026-09-30"}]
  },
  "rules": [
    {
      "rule_id": "ca-civ-1798.90.51-usage-privacy-policy",
      "citation": "Civ. Code § 1798.90.51",
      "actor": "ALPR operator", "activity": "operate ALPR",
      "duty": "maintain and post a usage and privacy policy ...",
      "exceptions": [], "remedy": "civil action, § 1798.90.54",
      "effective_from": "2016-01-01", "effective_to": null,
      "sources": [{"title": "...", "url": "...", "accessed": "..."}],
      "review": "verified | likely | needs_attorney_review"
    }
  ],
  "request_scopes": [ {"scope_id": "agreements", "title": "...", "items": ["..."], "rule_ids": ["..."]} ]
}
```

Python: `campaign_tool.law.load_package(jurisdiction) -> dict` (validates, raises `ValueError`),
`campaign_tool.law.rules_in_force(package, event_date) -> list`,
`campaign_tool.law.deadline(package, sent_date, extension=False) -> date`.
A package with `status: draft` can preview but `doctor` reports `reviewed_law_package: false`.

## Agency seed (`schemas/agency-seed.schema.json`)

```json
{
  "schema_version": 1, "jurisdiction": "us-ca", "generated": "2026-09-30",
  "counties": [
    {"county_fips": "06079", "name": "San Luis Obispo", "seat": "San Luis Obispo",
     "agencies": [
       {"agency_id": "ca-san-luis-obispo-county-sheriff", "name": "San Luis Obispo County Sheriff's Office",
        "kind": "sheriff | police | chp | district_attorney | county_board | city_council | other",
        "jurisdiction_name": "San Luis Obispo County", "place_fips": null,
        "records_url": null, "records_email": null, "portal": {"vendor": "nextrequest | govqa | justfoia | none | unknown", "url": null},
        "flock_transparency_slug": null, "muckrock_agency_id": null,
        "verified": false, "sources": []}
     ]}
  ]
}
```

Python: `campaign_tool.discovery.locate(query) -> Location` (county_fips, county_name, state, place_fips, place_name; offline seed first, Census geocoder only with `--online`),
`campaign_tool.discovery.agencies_for(location, seed) -> list[Agency]`.
`campaign_tool kit --directory D [--online]` writes `kit/agencies.json`, `kit/requests/<agency_id>.md`, `kit/summary.json`, `kit/law.json` and never sends anything.

## D1 tables (`workers/schema/d1.sql`)

campaign, agency, request, correspondence, original, receipt_occurrence, extraction,
digest, finding, review_receipt, publication, correction, subscriber_event, meeting,
job, job_claim, external_action, incident, setting, run_receipt. Every row carries `campaign_id`, `created_at`,
`updated_at`. Foreign keys on. Findings and review receipts mirror `review.py` fields exactly.

## Queue job (`schemas/job.schema.json`)

```json
{"job_id": "job_...", "campaign_id": "...", "kind": "intake | extract | digest | classify_mail | send_request | draft_followup | build_site | newsletter_draft | backup",
 "idempotency_key": "sha256 of kind+inputs", "inputs": {}, "attempt": 1, "max_attempts": 3,
 "claim_token": "random UUID for this attempt", "leased_until": "2026-10-13T12:05:00.000Z",
 "enqueued_at": "...", "privacy_tier": "redacted_cloud | strict_local"}
```

Runner API (workspace Worker, bearer token bound to one campaign):

- `GET /api/runner/jobs?lease=300` returns the next atomic lease or 204. Each lease increments
  `attempt`, mints an immutable random `claim_token`, and records an independent `job_claim` row.
  The requested duration is clamped to 30–3600 seconds. A lease is live only while
  `state = leased` and `leased_until > now`; equality is expired.
- `POST /api/runner/jobs/{job_id}/renew` requires `{"claim_token":"...","lease_seconds":300}`.
  `lease_seconds` must be an integer from 30 through 3600. Only the matching, unexpired claim
  can renew. The deadline becomes the later of its existing deadline and `now + lease_seconds`;
  renewal never increases `attempt`. The response is `{job_id, claim_token, attempt, leased_until}`.
- `POST /api/runner/jobs/{job_id}/result` requires
  `{"claim_token":"...","status":"done|failed|blocked","outputs":{},"receipt":{},"error":null}`.
  `outputs`, `receipt`, and `error` are optional; outputs must be an object and error a string or null.
  Success returns `{job_id, state, followups}`. `failed` returns the job to `queued` while attempts
  remain; exhausted failure, `done`, and `blocked` are terminal.

Missing/invalid tokens or result fields return 400. Unknown job IDs return 404. Renewals and new
results against an expired or retired claim return 409 with `{"error":"claim_conflict"}`. Result
acceptance tests campaign, job, token, leased state and deadline inside the database transaction;
reading the job before the write does not confer authority to finish it.

A result's canonical JSON includes status, outputs, receipt and error (missing optional values
normalize to `{}`, null, null). Object-key order is ignored; arrays retain order and every output
field participates. The Worker stores its SHA-256, canonical envelope, and response per claim.
An exact accepted replay returns the original 200 response even after expiration, a new attempt,
or a later terminal result. It does not mutate the current job or repeat downstream actions.
A changed envelope on an already accepted claim returns 409 with `{"error":"result_conflict"}`.
Thus a retried failure from attempt 1 still acknowledges its original `queued` transition while
attempt 2 is live or already complete. Clients need not calculate the digest themselves.

Result `outputs` may include `followups: [{"kind", "idempotency_key"?, "inputs"}]`. The Worker
considers at most 200 entries, skips unknown kinds and `send_request`, and uses the supplied
64-hex key or `sha256(kind + JSON(inputs))`. Duplicate keys return the existing job ID. Followups
on `failed`/`blocked` are ignored. A `classify_mail` result may include
`correspondence_update: {classification, classification_confidence, summary}`; only these fields
are patched on the correspondence identified by the job's inputs. Unknown classifications become
`unclassified`; confidence and summary are limited to 32 and 300 characters.

The accepted claim, parent transition, every followup, classification patch, exhausted-failure
incident, and replay response commit in one D1 `batch` transaction. Each statement is gated by
that transaction's fresh completion marker. Any failing statement rolls back the entire batch;
a retry after a lost response acknowledges the stored transition. No successful terminal parent
can commit without its intended result-derived D1 effects. Attempt evidence is exported in
`job_claim` along with `job`.

Before leasing work, a bounded sweep handles up to 100 expired final attempts. It atomically
sets `failed`, `error = lease_attempts_exhausted`, `finished_at`, and a deduplicated warning incident
keyed by job kind and idempotency key. Repeated sweeps do not increment the incident count or
change prior accepted result evidence. Expired attempts below the cap remain reclaimable with
a new token; terminal failures are excluded from the queued/leased backlog count.

`workers/workspace/migrations/0001_init.sql` is the immutable baseline. Existing databases apply
`0002_job_claims.sql` additively; no table is dropped or reinitialized. The migration preserves
all existing job columns and records current pre-upgrade evidence as a labeled `legacy_snapshot`
in `job_claim` for every attempted job. Those snapshots are not replayable accepted results.
Old attempts receive unissued legacy tokens: tokenless old runners cannot complete them, and
live leases expire normally before reclaim or final-attempt failure. Upgrade the Worker, migration
and runner together. Fresh workspace databases apply the full migration chain; fresh wizard
installations use the current `workers/schema/d1.sql` snapshot. `npm run sync` regenerates that
snapshot's shared constant without rewriting applied migrations. The local D1 migration test
checks upgrade evidence and schema/index equivalence with the fresh snapshot.

Other runner endpoints remain independently authenticated operations:
`GET /api/runner/originals/{sha256}` returns bytes; `PUT /api/runner/originals/{sha256}` stores them;
`PUT /api/runner/site/{version}/{path}` stages a public-site file (`version` matches
`[a-z0-9._-]{1,64}`; `path` has plain relative segments, an allowlisted extension or
`_headers`/`_redirects`, at most 16 MiB, and `x-object-sha256` must match its bytes). Original,
receipt, correspondence, proposal, and site mutations made while a handler runs are outside the
result transaction. The runner guards subsequent operations after claim loss; operations already
in flight are not canceled or made transactional by this contract. Runner never receives mail,
MuckRock or Brevo credentials; sends require the workspace outbox's organizer approval.

## Approval card

Every proposed external effect is a row in `external_action` with `state = proposed`. The workspace UI
shows it as a card with Approve / Edit / Reject. Only `state = approved` rows with an `approved_by` identity
from an Access JWT are executed, and execution writes `executed_at`, `provider_receipt`, `state = executed`.
No code path sends without an approved row. Kinds: `send_request`, `send_followup`, `pay_fee`, `publish_finding`,
`send_newsletter`, `post_social`, `deploy_site`.

Approve, edit and reject use a conditional database transition against the card snapshot
read by that request: state, proposal bytes, update time, approval, error and provider receipt.
A concurrent edit cannot inherit an approval for the earlier draft; a late edit or rejection
cannot overwrite an execution claim or its successful receipt. Conflicts return HTTP 409
and require reloading the card. This database consistency check does not establish that a
human semantically reviewed the content or replace Cloudflare Access authentication.

Once execution has been claimed, rejection is not cancellation of an admitted provider
effect. Never reopen an `executing` card merely because a caller timed out. A new
claim stores a unique `execution_claim: UUID` in the existing error field while
executing; it is a claim marker, not a failure. Completion uses this immutable
marker so an old executor cannot overwrite a later claim. Newsletter checkpoints
and send events are also conditional on the same claim.

`POST /approvals/:id/hold-execution` accepts the exact `execution_id` and an
interruption evidence `reference`. It atomically records the hold and audit event,
retains provider identity/key/draft, and leaves the newsletter failed with
`brevo_execution_interrupted`. It does not cancel a provider request or reapprove
anything. A late provider receipt is retained separately as `late_execution_receipt`,
without stamping a sent event or altering the held state.

Reconciliation of that held execution additionally requires
`quiescence_reference` and `quiescence_confirmed: true` (HTML form: `confirmed`).
The organizer must prove the old invocation cannot issue another call, then inspect
provider state, including queued work and late receipts. Unknown means stay held.
These are authenticated operator attestations, not machine verification of the
referenced evidence. No elapsed-time lease or client abort establishes non-delivery:
[HTTP Workers have no hard duration limit](https://developers.cloudflare.com/workers/platform/limits/),
and [Brevo sendNow schedules an existing campaign](https://developers.brevo.com/reference/send-email-campaign-now).
The hold's fencing prevents further cooperative local writes; it cannot undo an
already admitted external effect or prove remote quiescence.

Legacy/unbound executing rows and other action kinds are not silently assigned
new claims or reset. They require separate verified host/provider recovery. This
source fix does not complete live crash-recovery/operator-pilot acceptance.

## Privacy tiers

`redacted_cloud` (default): the runner redacts plate numbers, personal names, street addresses, phone numbers,
emails and officer identifiers with `campaign_tool.digest.redact` before any external model call, and logs the
redaction count. `strict_local`: no external model call; `MODEL_BASE_URL` must be a loopback or Tailscale address.

## Independent review (records pipeline)

Decision 2026-10-01: the independent review the records contract requires is satisfied by an
automated **challenge pass** inside `records run`, recorded on the stage receipt as a
`reviews[]` entry whose `reviewer_id` is never the author. The pass is
`campaign_tool.records.challenge`: a deterministic source check (every locator resolves, every
quoted statement is in the redacted sources, every legal conclusion cites a known `rule_id` and is
`needs_attorney_review`, no identifier survives redaction) plus an optional second model
(`CHALLENGE_MODEL_BASE_URL`) that must be a different model or an explicitly declared
fresh-context run (`CHALLENGE_FRESH_CONTEXT=1`). The challenger receives the redacted sources
*before* the digest and may only dispute. A dispute promotes the stage as `blocked` with the
disputes recorded; it never passes silently. The same pass covers the `compare` (legal role) and
`privacy` (factual + privacy roles) stages.

The owner's approval (`records approve`) remains the final gate before anything leaves the
pipeline, and it binds to the exact public bytes. No outside human reviewer is required for a
tier A documentary item; one may still be recorded as an additional `reviews[]` entry.

## Confidence labels

Every digest conclusion carries `confidence: verified | likely | needs_attorney_review` and at least one
`sources[]` entry with `sha256`, `locator` (page/sheet/cell) and, for law, a `rule_id`. `needs_attorney_review`
blocks `publish_finding`.
