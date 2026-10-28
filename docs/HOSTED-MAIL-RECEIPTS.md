# Hosted mail receipt recovery

Approved request and follow-up cards persist a bounded, validated sender receipt before request and correspondence bookkeeping. A malformed receipt, unknown transport result, or receipt-storage failure holds the same action and idempotency key for authenticated delivery reconciliation. A valid receipt also prevents bookkeeping failure from reopening the card automatically.

Adapters may throw ExecutorFailure only for a conclusive refusal or with the mail_send_ambiguous code for an uncertain delivery. Ordinary exceptions are uncertain. NotConfiguredSender remains a conclusive refusal; no provider is enabled by this change.

Initial request sends also claim the request scope atomically across approval keys. A prior executing or executed card, an uncertain failed card, a failed card with delivery evidence, or a request with a sent timestamp blocks a new initial send before the provider is called. A conclusive refusal does not block a separately approved replacement.

All mail cards additionally compare their outbound content in the same atomic claim: campaign, action kind, request subject ID (including null), channel, recipient, subject and body. Proposal edit metadata and approval keys do not create a new send identity. Recipients follow the sender port's conversion: a non-string or omitted recipient is null. Text fields otherwise match exactly; this is not a fuzzy similarity test. Distinct follow-up text, recipient or channel can be separately approved. Identical content stays held while an equivalent card is executing, delivered, or uncertain; recorded non-delivery plus fresh approval releases the original card.

Reconciliation records mail-provider evidence, not a Brevo campaign event. Confirmed non-delivery returns the same card to proposed and requires new approval. Delivered mail requires its stored provider message ID or an explicitly supplied provider-confirmed ID when no receipt survived. A conflicting ID is refused.

For request-bound mail, delivered reconciliation atomically closes the action, stores the delivery audit event, repairs initial-send fields and records outbound correspondence. Initial request state only advances from draft/approved; later response states and newer activity are retained. Follow-up repair keeps the initial send identity. Existing matching correspondence is deduplicated; conflicting provider identities or failed writes roll the whole repair back, keeping the action held. Stale action evidence cannot change request records. This operation never calls a sender.

Do not create a new key to evade a held action. A production adapter must still bind these approval/claim identities to provider-side idempotency and preserve conclusive refusal versus uncertain delivery semantics.

This is synthetic receipt/retry validation, not a configured hosted sender or live pilot. No mail, services or schedules were activated.
