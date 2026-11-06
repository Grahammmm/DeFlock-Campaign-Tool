# Records next steps (REC-AUTO-10)

`records next-steps --project P --run-id R --campaign-root C [--base-url URL] [--dry-run]` turns one run's
`outcomes/gap-proposals.json` (REC-AUTO-9) into pending drafts in the approval queue. The admin panel shows them at
`/next-steps` with Approve and Deny. **Nothing is sent, posted, published or edited by this code.** Approving records a
decision on the draft (who and when, from the queue journal) and nothing else.

## The three recommendations

| Queue kind | One per | What it is | Approved means |
| --- | --- | --- | --- |
| `next-social-post` | agency | Fixed-template post: agency name, a count, field labels, up to two source record ids. No quote, person, plate, email or phone. At most 280 weighted characters (labels, then ids, are dropped to fit). | approved-to-publish |
| `next-site-update` | agency in `content/agencies.json` | A unified diff of `content/agencies.json` that changes only that agency's `profile.status` to `Records received YYYY-MM-DD; under review`. The proposed document is checked with `content.load_agencies`. | approved-to-publish |
| `next-followup-probe` | (agency, request) | A numbered follow-up draft: one ask per proposal, each citing the rule label and quoting the source line and page exactly. | approved-to-send |

All wording is a fixed template; no model is called. Each row shows its source finding(s), why it is recommended and a
status: proposed (pending), approved, or denied (rejected).

## Eligibility and idempotency

Only proposals with confidence `high` or `medium` count; `low` ones are reported as `skipped_low`. Rows are keyed by a hash of
the kind, agency, request and sorted proposal ids (the site update also by the received date), so rerunning queues nothing
new and never overwrites a human edit or reopens a denied row. A request-less proposal is queued under the request label
`agency-wide` because the queue needs a non-empty request id.

## Checks that refuse a draft (diagnostics, never the text)

`agency_not_in_content`, `social_privacy_refused` (category names only), `social_legal_claim_flagged`, `social_too_long`,
`site_update_invalid`, `site_file_missing`, `received_date_missing`, `probe_legal_claim_flagged`. Quoted record text is exempt
from the legal-claim check by design; the unquoted wording must pass it. Inner double quotes in a quoted line are shown as
single quotes. Missing, stale or invalid inputs end with exit 0 and a `skipped` code; a queue error exits 3 with its code.

## Downstream (not here)

Posting goes through the existing social drafts and publication gate; a site edit is a human edit of `content/agencies.json`;
sending goes through the existing outbox. The generator writes only queue rows, never `content/`, `public/` or `kit/`, and is
not run by the panel (GET never generates). See also [the admin panel](RECORDS-ADMIN-PANEL.md).
