# Records gap and possible-misuse proposals

Private, per-run questions for a human reviewer. Written by the outputs stage to
`<project>/run-outputs/<run_id>/outcomes/gap-proposals.json` (mode 0600, directory 0700,
`publishable:false`), directly after `received-items.json`. Schema:
`schemas/records-gap-proposals-v1.json`. **Nothing here is published, drafted or sent, and a
proposal is never a legal conclusion.** The law package is a draft.

## What is proposed

| type | when | compared with |
| --- | --- | --- |
| `policy_gap` | a received-item finding is rated `gap` (copied, never re-rated) | the finding's own `rule_ref` and `policy_ref` |
| `misuse_lead` | a `records` item has sharing or immigration wording (reuses `digest/detectors.py`) | Civ. Code 1798.90.55(b) / Gov. Code 7284.6 and the same agency's `sharing_limits` policy finding |
| `promised_not_received` | a stated date has passed and no later records or denial is in the inventory | Gov. Code 7922.535 |
| `deadline_passed_no_answer` | a ledger request's recorded determination date passed with no determination or answer item | Gov. Code 7922.535 |
| `exemption_without_citation` | a denial's text names no Public Records Act section or exemption | Gov. Code 7922.540 |
| `fee_review` | a fee item names a basis other than duplication cost | Gov. Code 7922.530 |
| `missing_field` | an answer item has no request or received time | Gov. Code 7922.535 |

Every proposal carries `source_id`, `page` and an exact `quote` (a ledger-date proposal quotes
the recorded field and has `evidence_kind: ledger_field`), `rule_ref`, `policy_section`
(null with flag `policy_section_unavailable`), `confidence`, a plain-words `reason` and a
`question`. Question templates are fixed, name no agency and pass the legal-claim guard.
A lead is a reason to ask, not evidence of misuse. A gap is a documentary comparison.

## Confidence

Fixed rules decide it, never a model: `policy_gap` high with a policy section, else medium;
`misuse_lead` medium when the page also has an audit-log line, else low; `promised_not_received`
medium (low when the year was inferred); other types medium or low as listed in the code.
Unreviewed OCR lowers one level (never below low) and adds `text_unreviewed`.

## Model score and the review list

Each proposal gets a yes/no score. When a local H2O endpoint is configured
(`DEFLOCK_H2O_URL`, loopback only) it answers with probabilities and top log-probabilities;
otherwise, or on any fault, a fixed deterministic score is used. Both are written to
`model_log`. A model answer never raises confidence; a "no" only moves the item to the review
list. `review_list` holds every proposal that is low confidence, lacks a policy section, rests
on unreviewed text or is not supported by the model. Items in it are never stated as fact.

Items with no agency are counted in `unlinked_items` and never assigned. A request with no id
is grouped agency-wide, last.

## Handoff

The admin panel (REC-AUTO-5) and the next-steps page (REC-AUTO-10) read this file by
`proposal_id`. No module outside `campaign_tool/records/` may import it. A different replay for
the same run is refused with `gap_proposals_run_already_exists`.
