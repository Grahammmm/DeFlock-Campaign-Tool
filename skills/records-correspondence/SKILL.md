---
name: records-correspondence
description: Draft targeted Flock records follow-ups from reviewed gaps and preserve agency responses; sending requires exact item authorization and a supported outbox.
---

# Records Correspondence

Read the common contract and **Correspondence** stage. Map each gap to the relevant request, custodian, date range, native field or missing record and prior response. Draft the least burdensome useful request; describe blank/redacted export fields without asserting the native record lacks them.

Keep correspondence and recipient evidence private. Drafting does not send. Require explicit item authorization, verified recipient/transport and the supported outbox before delivery. Preserve the idempotency key and provider receipt. An ambiguous send is a reconciliation hold, never an automatic resend or a reason to switch transport.

New responses return through `records-capture`. Pass unresolved agency assertions and scope gaps to `records-applicability`; report draft, approved, sent and response states separately.

Shared versioned inputs, outputs, retry/failure rules and Hermes release gates:
[Records stage handoff](../../docs/RECORDS-STAGE-HANDOFF.md). Read it for this stage;
use the next skill only after its eligibility conditions are satisfied. These
repository skills require that shared document to travel with the skill bundle.
