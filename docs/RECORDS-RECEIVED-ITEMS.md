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
  is not given an invented page: it stays `other` with `page_locator_unavailable` until a mail-body
  locator contract exists. Only originals whose admission resolves (`effective_original`) are read;
  an unresolved binding omits the original unread. `kit_equivalent` is a read-only crosswalk to
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

The quote is the first non-empty line of that page, and only when exactly one current unit carries
that page (several units on one page are ambiguous and give `comparison_evidence_unresolved`).
`policy_ref` is set only for written-policy fields whose citations all resolve to one source whose
opening title names a policy; otherwise it is null with the diagnostic `policy_source_unestablished`
(access logs and meeting minutes are evidence, never the policy). Aliases, section names and other locators
produce the report diagnostic `comparison_evidence_unresolved`; `*_practice` rows produce
`practice_policy_citation_missing` (no separate policy citation exists to bind). Without coordinator
input (`agency-fact-sheets.json`) every row is `unknown` without evidence, so items show
`comparison_status: "unresolved"` and no findings; that is absence of input, not "nothing wrong".
`agency-factsheets.json` is a different file and is not used as comparison input.

## Untied messages and message links (DFGL-04)

An item whose `request_id` is null is listed in `outcomes/unmatched-messages.json`
(`records-unmatched-v1`, 0600): message sha256, received date, sender domain only, attachment count and up to
3 candidate requests from the ledger register (the agency's requests, open first, or requests of an agency whose
portal host matches the sender domain). No subject, body or address is written. Each message also queues one
`message_link` decision ("Tie this message to a request") in the approval queue. Approving it with one of the
listed candidates (the `choice` form field; any other value is a 400 and changes nothing) appends one line to
`<project>/message-links.jsonl` (0600, append-only: sha256, request id, agency id, actor, time); rejecting records
"not a response". `received_items` reads that file before its join: a linked sha256 takes the linked ids from the
next run on; a link whose request no longer exists is ignored and counted as `message_link_request_missing`.
If the writer fails the stage warns `OUTPUTS_UNMATCHED_UNAVAILABLE` and every earlier output stays. Nothing is
sent and no model matches messages.

## Failure behaviour

A stale `comparison.json` (other run id), corrupt hashes, an unsafe path, or a conflicting existing
`received-items.json` fail the stage with `OUTPUTS_WRITE_FAILED`; an identical replay is a no-op.

## Offline check

```sh
python -B -m unittest tests.records.test_outcome_received_items
```
