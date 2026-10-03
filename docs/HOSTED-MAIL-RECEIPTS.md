# Hosted mail receipt recovery

Approved request and follow-up cards persist a bounded, validated sender receipt before request and correspondence bookkeeping. A malformed receipt, unknown transport result, or receipt-storage failure holds the same action and idempotency key for authenticated delivery reconciliation. A valid receipt also prevents bookkeeping failure from reopening the card automatically.

Adapters may throw ExecutorFailure only for a conclusive refusal or with the mail_send_ambiguous code for an uncertain delivery. Ordinary exceptions are uncertain. NotConfiguredSender remains a conclusive refusal; no provider is enabled by this change.

Reconciliation records mail-provider evidence, not a Brevo campaign event. Confirmed non-delivery returns the same card to proposed and requires new approval. A delivered reconciliation records the action outcome; request/correspondence bookkeeping repair remains an operational task, not a new send. Do not create a new key to evade a held action. The future hosted outbox must enforce cross-key request-level deduplication as well.

This is synthetic receipt/retry validation, not a configured hosted sender or live pilot. No mail, services or schedules were activated.
