---
name: independent-review
description: Challenge an ALPR finding against original evidence, applicable rule versions and counterevidence before publication.
---

# Independent finding review

Work from originals and exact locators, not only the author's summary. Reproduce
material facts; seek plausible alternative explanations and contrary records.
Check event-date applicability, duty scope, exceptions and local policy provenance.
Review private data and the exact sanitized public artifact.

Use role factual, legal or privacy; decision approve, reject or changes_requested.
Record reviewer identity, UTC reviewed_at, rationale and content_sha256 produced
by campaign_tool.review.content_hash. Record artifact and evidence hashes separately.
At least two reviewers other than the author must cover the three roles.

A changed finding or artifact invalidates the corresponding approval. Do not erase
rejected versions or silently ignore an unresolved challenge. Resolve with a
superseding reviewed version and retained audit trail.

The alpha review_blockers function checks structure only: it does not authenticate
reviewers or authorize publication. Require trusted reviewer attribution, original
byte verification, human release approval and privacy review outside that function.
No review guarantees legal correctness. Escalate material unresolved legal questions
to qualified counsel rather than making an accusation stronger than its evidence.
