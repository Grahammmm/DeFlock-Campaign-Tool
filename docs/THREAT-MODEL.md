# Threat model

## What this project is worth attacking for

The project holds records and private working material. It publishes findings based on records. Its credibility depends on how clearly those findings can be checked and corrected.

## Adversary categories

| Category | Target and realistic consequence | Existing control or gap |
| --- | --- | --- |
| A challenger seeking to discredit a finding | Evidence and review history; a disputed finding could lose public confidence. | Content-bound checks in `campaign_tool/review.py` and re-hashing in `docs/ORIGINAL-RECONCILIATION.md`; GAP `df-custody-report` for a consolidated answer. |
| Someone seeking private contents | Private records and working material; disclosure could harm people. | The public-tree split in `campaign_tool/records/public_scan.py` limits checked paths; GAP `df-data-handling` for open retention and access decisions. |
| A party seeking a lawful demand for the store | Store contents; a subpoena could compel disclosure. | `docs/DATA-HANDLING.md` describes the current separation; GAP `df-data-handling` for retention and access decisions. |
| Someone seeking false or harmful publication | Findings and public output; misleading claims or personal data could reach readers. | `campaign_tool/approval.py`, pending exports in `tools/flock_publish.py`, shared patterns in `tools/flock_privacy.py` and the alert-only `docs/LEGAL-CLAIM-GUARD.md` help review; GAP `df-publish-gate` and `df-pii-everywhere`. |
| A thief or finder of a lost machine | A device and its contents; loss could expose records or interrupt work. | `docs/BACKUP.md` describes backup posture; GAP `df-data-handling` for device-loss decisions. |
| A party compromising the supply chain or credentials | Pipeline access or outputs; compromised work could expose records or mislead readers. | `scripts/scan-secrets.mjs` and `SECURITY.md` describe partial safeguards; GAP `df-secrets-ci` for records and OCR output scanning, a currently skipped item. |

The state law package for California remains draft, with three rules needing attorney review; deadline claims depend on an unreviewed reading until `df-law-review` is answered. OCR live wiring is not done; extracted text remains unreviewed and requires visual review (`docs/STATUS.md`). A hash establishes byte identity, not authenticity.

## What the project deliberately does not defend against

- A well-resourced adversary with host access: this development tool is not a complete host security boundary, so host protection remains an operator responsibility (`SECURITY.md`, `docs/RECORDS-CONTAINER-HOST.md`).
- Lawful compelled disclosure: the tool cannot set aside a lawful demand; the response belongs to the operator and counsel.
- An organizer who chooses to publish something they should not: review supports human judgment, but cannot replace or compel it (`docs/PUBLICATION.md`).

## What a challenge actually looks like

A challenge calls for an understandable custody answer, not just a hash: `df-custody-report` calls for the document, its receipt and review history in one place. The current reconciliation and review material can help trace the evidence (`docs/RECONCILIATION.md`, `docs/REVIEW-AUTHORITY-BOUNDARIES.md`); `docs/CORRECTIONS-PROCESS.md` describes how to handle a correction or takedown request.

## What this means for the trust items

Ranking (operator's reading, not a re-decision). This is advice, not a change to the queue or its priorities.

1. `df-pii-everywhere` — each public output is a possible disclosure point.
2. `df-publish-gate` — an explicit human gate reduces the chance of releasing unreviewed material.
3. `df-data-handling` — retention, access and device-loss decisions shape exposure before publication.
4. `df-custody-report` — a clear evidence and review history helps answer challenges.
5. `df-legal-claim-guard` — alert-only wording checks can draw attention to overconfident claims, but are not legal review.
6. `df-secrets-ci` — broader scanning could catch some exposed credentials; this item is currently skipped.
7. `df-threat-model` — this document connects the threats to the other trust work without changing its priority.
