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
job, external_action, incident, setting. Every row carries `campaign_id`, `created_at`,
`updated_at`. Foreign keys on. Findings and review receipts mirror `review.py` fields exactly.

## Queue job (`schemas/job.schema.json`)

```json
{"job_id": "job_...", "campaign_id": "...", "kind": "intake | extract | digest | classify_mail | send_request | draft_followup | build_site | newsletter_draft | backup",
 "idempotency_key": "sha256 of kind+inputs", "inputs": {}, "attempt": 1, "max_attempts": 3,
 "enqueued_at": "...", "privacy_tier": "redacted_cloud | strict_local"}
```

Runner API (workspace Worker, bearer token bound to one campaign):
`GET /api/runner/jobs?lease=300` → next job or 204; `POST /api/runner/jobs/{job_id}/result` with
`{"status": "done|failed|blocked", "outputs": {}, "receipt": {...}}`; `GET /api/runner/originals/{sha256}` → bytes;
`PUT /api/runner/originals/{sha256}` → store; `PUT /api/runner/site/{version}/{path}` → stage a public-site file
(`version` matches `[a-z0-9._-]{1,64}`; `path` is relative with plain segments, an allowlisted extension or
`_headers`/`_redirects`, at most 16 MiB; the `x-object-sha256` header must equal the body's hash). Runner never
receives credentials for mail, MuckRock or Brevo; sends are performed by the workspace outbox after an organizer approval.

Result `outputs` may include `followups: [{"kind", "idempotency_key"?, "inputs"}]`; the Worker enqueues each with
the given 64-hex key or `sha256(kind + JSON(inputs))`, skips unknown kinds and `send_request`, and ignores followups
on `failed`/`blocked` results. A `classify_mail` result may include `correspondence_update: {classification,
classification_confidence, summary}`; no other correspondence field is writable from a result.

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
effect. Never reopen an `executing` card merely because a caller timed out. The hosted
crash-recovery/operator-pilot gate remains open until the original executor can be proven
quiescent and its provider outcome reconciled; this transition fix does not add such recovery.

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
