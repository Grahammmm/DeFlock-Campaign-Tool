# Hosted mail receipt recovery

Approved request and follow-up cards persist a bounded, validated sender receipt before request and correspondence bookkeeping. A malformed receipt, unknown transport result, or receipt-storage failure holds the same action and idempotency key for authenticated delivery reconciliation. A valid receipt also prevents bookkeeping failure from reopening the card automatically.

Adapters may throw ExecutorFailure only for a conclusive refusal or with the mail_send_ambiguous code for an uncertain delivery. Ordinary exceptions are uncertain. NotConfiguredSender remains a conclusive refusal; no provider is enabled by this change.

Initial request sends also claim the request scope atomically across approval keys. A prior executing or executed card, an uncertain failed card, a failed card with delivery evidence, or a request with a sent timestamp blocks a new initial send before the provider is called. A conclusive refusal does not block a separately approved replacement. This guard applies within a campaign to send_request cards with a request subject ID; it does not deduplicate unbound cards or distinct follow-ups.

Reconciliation records mail-provider evidence, not a Brevo campaign event. Confirmed non-delivery returns the same card to proposed and requires new approval. Delivered mail requires its stored provider message ID or an explicitly supplied provider-confirmed ID when no receipt survived. A conflicting ID is refused.

For request-bound mail, delivered reconciliation atomically closes the action, stores the delivery audit event, repairs initial-send fields and records outbound correspondence. Initial request state only advances from draft/approved; later response states and newer activity are retained. Follow-up repair keeps the initial send identity. Existing matching correspondence is deduplicated; conflicting provider identities or failed writes roll the whole repair back, keeping the action held. Stale action evidence cannot change request records. This operation never calls a sender.

Do not create a new key to evade a held action. The future hosted outbox still needs stable content identity for unbound cards and follow-up sends.

This is synthetic receipt/retry validation, not a configured hosted sender or live pilot. No mail, services or schedules were activated.
