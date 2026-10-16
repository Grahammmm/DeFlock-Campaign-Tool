---
name: records-publication
description: Prepare and stage exact approved sanitized Flock findings with current review/privacy bindings and correction history; live publication requires scoped approval and supported deployment.
---

# Records Publication

Read the common contract and **Publication** stage. Verify the exact public bytes, manifest, privacy and independent-review dependencies, current owner decision and cleared retirement hold. Run the upgraded pipeline on the root before publisher use. A passed scanner is not substantive privacy clearance.

Use `publish.approve(...)` and `publish.publish(..., staging=...)` only within the approved scope; engine staging is not a live deploy. Preserve the existing root/target locks and journals. Changed dependencies revoke approval even when text is unchanged. Corrections use `publish.reopen(...)`; rollback must verify retained history and current authority, never restore an obsolete runtime that removes guards.

Require the supported deployment and item approval for the live target. Report proposed, staged and deployed receipts separately; notify the parent only through the verified status adapter.

Shared versioned inputs, outputs, retry/failure rules and Hermes release gates:
[Records stage handoff](../../docs/RECORDS-STAGE-HANDOFF.md). Read it for this stage;
use the next skill only after its eligibility conditions are satisfied. These
repository skills require that shared document to travel with the skill bundle.
