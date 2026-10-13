---
name: flock-correspondence
description: Review ALPR public-records correspondence against an existing canonical records ledger, trace agency requests and deliveries, identify outstanding items, and prepare evidence-bound follow-up and reminder proposals. Use for correspondence tracking, not document ingestion, legal deadline calculation, sending, or scheduling.
---

# Flock correspondence

Produce a private, reproducible account of each existing agency request: its recorded
status, linked deliveries and attachments, uncertain thread links, review gaps,
outstanding-item assertions, and proposed next action. Use the records owner's
canonical ledger; this skill does not create another request registry.

Read [workflow](references/workflow.md) for the command, review packet contract,
and interpretation rules. In an engine checkout, read its `AGENTS.md` and
`docs/CORRESPONDENCE.md` for current source interfaces and integration limits.
If the command or supported ledger is unavailable, report that concrete blocker;
do not initialize, migrate, or substitute a different database to make it run.

## Working boundaries

- Operate on the existing `campaign_tool.records.ledger.store` canonical schema
  with `ledger(..., readonly=True)`. Preserve request IDs, byte hashes, delivery
  occurrences, parent relationships, and the existing records owner's work.
- Keep reports and optional review packets in owner-only private storage outside
  repositories. Reports can expose request identifiers, dates, and provenance;
  they are not public artifacts. Use synthetic examples for source changes and
  tests. Do not export raw mail to a cloud service or copy a private corpus into
  source control.
- Separate recorded facts, agency assertions, reviewer proposals, and unresolved
  gaps. A typed source link is a recorded provenance declaration; this report
  does not reverify the source or prove an assertion's truth.
  Receipt of bytes, a portal notice, a classification, or a closed status does not
  establish complete production or substantive review.
- Use original hashes for byte identity and occurrence IDs for delivery context.
  Preserve repeated deliveries. Header-based thread candidates do not authorize
  request joins; a common subject, sender, or agency is insufficient.
- Make follow-up dates explicit and timezone-aware. A date-only or ambiguous date
  needs review of its source and intended timezone. Do not calculate a statutory
  deadline, infer a violation, or substitute a follow-up date for a legal deadline.
- Treat all review packets as transient proposals bound to the report's snapshot.
  They cannot update authoritative status, certify fulfillment, or satisfy factual,
  legal, privacy, or independent-review gates.
- This workflow creates no scheduler, mailbox connection, outbox item, real
  reminder, or external message. Prepare concrete drafts and reminder proposals;
  execute any separately authorized action through its existing approved workflow.

## Complete the review

Run the read-only report with an explicit as-of timestamp. Inspect evidence under
the existing records workflow and record only assertions with exact occurrence
and source locators. Re-run with a review packet when item dispositions or
follow-up proposals are useful. If the snapshot has changed, reconcile the new
evidence and reissue the packet; never change its hash merely to bypass a failure.

Return the recorded request status, unique originals versus delivery occurrences,
outstanding or unknown items, proposed follow-up dates and reasons, precise
coverage limits, and the next action owned by the appropriate person or workflow.
Report missing canonical joins, unsupported schema, inaccessible inputs, unresolved
dates, and unreviewed content as gaps. Hand document ingestion and substantive
analysis back to their existing owner.
