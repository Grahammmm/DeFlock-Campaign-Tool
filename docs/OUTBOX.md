# Transactional outbox

`campaign_tool/outbox.py` is the only code in the engine that may send a records request or
follow-up. It keeps a local SQLite journal with one row per intended send and refuses to run
a transport unless every safeguard passes. The runner never imports it; the runner proposes,
an organizer approves, and the outbox executes.

Status: implemented and tested with an SMTP stub on loopback and a scripted MuckRock opener.
It is the organizer's-own-machine path. The workspace Worker's `send_request` and
`send_followup` executors are still typed interfaces; when they are written they must mirror
this journal row for row (same key, same states, same caps) rather than invent a second
sending path.

## Journal

`outbox.sqlite` lives in a 0700 directory at mode 0600. Columns: `idempotency_key`
(primary key), `kind`, `request_id`, `agency_id`, `scope_version`, `channel`, `draft_json`
(the exact `RequestDraft` that will be sent), `fee_cap_cents`, `state`, `approved_by`,
`approved_at`, `sending_at`, `sent_at`, `provider_receipt`, `error`, `resolved_by`,
timestamps.

Key: `sha256(kind \0 request_id \0 scope_version)`. The same request and scope version is one
row forever, however many times a job retries or an organizer clicks. A new scope version is
a new row.

States: `proposed → approved → sending → sent | failed | blocked`.

## Safeguards (`Outbox._check`, enforced on every `send`)

The journal reads, safeguards and approved-to-sending claim run in one SQLite
`BEGIN IMMEDIATE` transaction. This serializes competing keys as well as retries
of one key: neither an agency/day budget nor an unresolved-request hold can be
passed concurrently from stale reads. Refusals roll the transaction back. The
transaction commits before calling SMTP or MuckRock, so network operations do
not hold the database write lock. A database lock timeout occurs before transport
and must not be treated as evidence that a provider delivered anything.

| Check | Result |
| --- | --- |
| Row already `sent` | no-op, the transport is not called |
| Row `sending` (previous attempt unresolved) or another `sending` row for the same request | `Blocked(ambiguous_send_unresolved)` until `reconcile` |
| Row `blocked` | `Blocked(blocked)` with the stored reason |
| Not `approved` or no `approved_by` | `Blocked(not_approved)` |
| `fee_cap_cents` above the campaign cap | `Blocked(fee_cap_exceeded)` |
| An email or filing already `sent`/`sending` to the same agency this UTC day | `Blocked(daily_agency_cap)`; follow-ups count too |

A `Blocked` raised by the transport itself (MuckRock 402) moves the row to `blocked`. An
`OutboxError` (SMTP refusal, MuckRock 401/403/4xx) moves it to `failed`. Anything ambiguous
(socket error after `DATA`, MuckRock 5xx or a reply without an id, any unexpected exception)
leaves the row in `sending` with the error recorded and raises `AmbiguousFailure`; nothing for
that request sends again until an organizer resolves it with `reconcile <key> delivered|
not_delivered --by <identity>`, which needs the provider's own evidence (mailbox Sent folder,
MuckRock request page).

## Transports

- `send_email(draft, SmtpSettings, smtp_factory=None)`: `smtplib` with STARTTLS unless
  disabled, optional login, a `Message-ID` minted locally and returned as the provider
  receipt, `In-Reply-To`/`References` when the draft carries `in_reply_to`.
- `file_muckrock(draft, token, base_url, opener=None)`: `POST /foia/` on MuckRock API v2 with
  `{"agencies": [id], "title", "requested_docs"}`; the receipt is the created request id and
  URL. 402 is `no_credits`; the organizer buys credits or switches the row to email.

Both take credentials as arguments. The CLI reads them from `OUTBOX_SMTP_PASSWORD` and
`MUCKROCK_TOKEN` (names overridable) at the moment of sending and never stores them.

## CLI

A normal SMTP result with a nonempty refusal map means at least one recipient
was accepted and others were refused ([Python SMTP documentation](https://docs.python.org/3/library/smtplib.html#smtplib.SMTP.sendmail)).
The journal holds that attempt in `sending` for reconciliation; neither retrying
the same key nor proposing another scope for the request may resend it. Inspect
delivery by recipient before resolving the hold. An exception stating that all
recipients were refused remains a definite `failed` attempt with no DATA sent.

```
python3 -m campaign_tool.outbox --journal private/outbox.sqlite propose draft.json
python3 -m campaign_tool.outbox --journal private/outbox.sqlite approve <key> --by organizer@example.org
python3 -m campaign_tool.outbox --journal private/outbox.sqlite --fee-cap-cents 5000 send <key> --smtp-host smtp.example.org --smtp-user requests@example.org
python3 -m campaign_tool.outbox --journal private/outbox.sqlite reconcile <key> delivered --by organizer@example.org
python3 -m campaign_tool.outbox --journal private/outbox.sqlite list --state sending
```

`send` exits 0 on `sent`, 2 on `blocked` (JSON with the reason), 3 on an ambiguous failure
(JSON with `next: reconcile before any resend`). The draft file holds `RequestDraft` fields:
`request_id`, `agency_id`, `scope_version`, `channel` (`email|muckrock`), `subject`, `body`,
`to`, `from_addr`, `fee_cap_cents`, `kind` (`send_request|send_followup`), `in_reply_to`.

## Relationship to the workspace

The workspace's `external_action` row is the approval; the outbox row is the send. The
runner's `draft_followup` handler proposes a `send_followup` card with the subject, body,
recipient and `in_reply_to`; once an executor exists it will turn the approved card into an
outbox `propose` + `approve` (with `approved_by` from the Access identity) + `send`, and
write the provider receipt back to the card and a `correspondence` row (`direction =
outbound`). Live SMTP, MuckRock and Brevo access, like production deployment, needs explicit
owner approval per AGENTS.md.

## Tests

`tests/test_outbox.py`: journal permissions and key stability, SMTP stub receives exactly one
message and a second `send` is a no-op, per-agency daily cap (shared by follow-ups), fee cap,
MuckRock 402 → `blocked` and a successful filing, MuckRock 5xx and transport crashes stay
`sending` until reconciled, 401 → `failed`, `In-Reply-To` threading, approval and reconcile
require an identity, CLI round trip.

## Explicit approval after a definite failure

The trusted hosted signer can call `Outbox.approve_retry(key, approved_by, approved_at)`
for an unchanged draft after a definite failure. Approval must be strictly later
than the failure; merely replaying or refreshing the old envelope is refused.
The row must be failed without a sending/sent timestamp or provider receipt.
Approval history and failure/resolver evidence are appended to the private
`outbox_retry_approval` table atomically with the state change. This method does
not send, lift agency/fee caps or reconcile uncertainty. An independently verified
not-delivered reconciliation needs a subsequent fresh approval. Do not alter the
canonical key or request scope to escape an unresolved effect. The CLI's ordinary
`approve` remains proposed-only; this API requires an authenticated trusted caller.
