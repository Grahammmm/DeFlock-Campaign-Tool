# Received-item classification (REC-AUTO-3)

The records `outputs` stage writes, right after `comparison.json`, one private file:
`<project>/run-outputs/<run_id>/outcomes/received-items.json` (directory 0700, file 0600,
schema `records-received-item-v1`, described by `schemas/records-received-item-v1.json`).
It is a cumulative snapshot as of that run, not a "new since last time" cursor. Nothing is
sent, approved, published or written to the ledger or kit tracking.

**Review required, never a conclusion.** Every item and finding carries
`review_required: true`, `legal_conclusion: false` and (findings) `needs_attorney_review: true`;
the us-ca law package is draft and each finding says so.

## What it records for each admitted received original

- `item_id`: SHA-256 of `["records-received-item-v1", original_sha256]` (stable across runs and re-extraction).
  One item per distinct original; duplicate deliveries of identical bytes keep all `occurrence_ids`.
- `agency_id` / `request_id`: only from unambiguous typed joins; otherwise `null` plus a diagnostic.
- `received_at`: earliest valid timezone-aware mail time, else the occurrence time, else a uniquely
  verified parent delivery time; never the current time.
- `kind`: `response | extension | denial | fee | records | other`, from fixed anchored phrases in the
  current extracted text (for example "we are extending the determination period", "your request is
  denied", "the duplication fee is", "enclosed are responsive records", or an exact opening title
  of a policy/contract/audit log). A non-`other` kind needs `kind_basis: {page, quote}` (positive page,
  exact source line). Negation, questions, quoted/forwarded history and bare words never qualify;
  two different specific kinds give `other` with `mixed_kind_candidates`; text without a real page
  gives `other` with `page_locator_unavailable`. A mail body or MIME part (locator `{mime, part}`, no page)
  is cited as page 1, and an HTML body is first rendered to one text line per block, so the quote is a
  line of the rendered body (the Caltrans portal wording "exercising its authority under Government Code
  section 7922.535, to extend the time" is an `extension` phrase). `kit_equivalent` is a read-only crosswalk to
  `kit_track` kinds (extension, denied, fee_estimate, produced; response maps to acknowledged or determination).
- `deadlines`: dates the item states for a response, extension or production ("by November 30"),
  with page and quote. A date without a year takes the year of `received_at` (`year_inferred: true`).
  These are quoted statements, not computed legal deadlines.
- `decision_source: "rules"` and an empty `model_decisions`: this stage is deterministic and calls no
  model or network. The slot is where a later wiring of the local decision model would log probabilities.
- `findings`: projection of the same run's `comparison.json` rows, see below.

## Comparison projection

The existing comparison rating is copied unchanged (`meets`, `gap`, `unknown`); no second evaluator
runs. A row attaches to an item only when the row's agency (and request, if the row names one) matches
the item and the row's `evidence` uses the admitted reference form:

```json
{"source_id": "<64 hex original sha256>"  /* or "sha256:<digest>" */, "locator": "page 3"}
```

The quote is the first non-empty line of that page. Aliases, section names and other locators
produce the report diagnostic `comparison_evidence_unresolved`; `*_practice` rows produce
`practice_policy_citation_missing` (no separate policy citation exists to bind). Without coordinator
input (`agency-fact-sheets.json`) every row is `unknown` without evidence, so items show
`comparison_status: "unresolved"` and no findings; that is absence of input, not "nothing wrong".
`agency-factsheets.json` is a different file and is not used as comparison input.

## Failure behaviour

A stale `comparison.json` (other run id), corrupt hashes, an unsafe path, or a conflicting existing
`received-items.json` fail the stage with `OUTPUTS_WRITE_FAILED`; an identical replay is a no-op.

## Offline check

```sh
python -B -m unittest tests.records.test_outcome_received_items
```
