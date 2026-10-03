# Private approved-outbox gateway

`runner.outbox_gateway` is an opt-in handler library. It reuses the existing
Python `Outbox`; it does not install a listener, register a Worker sender,
configure a provider, or activate a schedule. `SignedOutboxSender` implements the
Worker signer. The synthetic approval/receipt caller and cross-language protocol
are tested; private TLS deployment, real provider acceptance and a hosted pilot
remain unverified.

## Trust boundary and configuration

The owner supplies one campaign ID, a private journal path, the sender address,
injected email/MuckRock transport functions, and a separate random shared signing
key (at least 32 characters). Keep the key in secret storage; its holder can attest
an outbound approval. Never reuse runner or download tokens. The caller must
provide authenticated private routing and TLS, request timeouts, connection limits
and monitoring. The HTTP handler alone does not provide those deployment controls.
No private settings or real drafts belong in the public repository.

The trusted Worker signer must read an already claimed `executing` action and its
recorded approval from authoritative campaign storage, verify its approved draft,
and resolve the request's agency, scope version and fee cap from that storage.
An arbitrary browser or runner request must never be allowed to supply the
approval identity or signing key. At approval the Worker captures authoritative
agency/scope/fee data into `outbox_binding`; the signer refuses changes to it or
the approved draft before signing. It rechecks the execution claim before fetch.
Older cards without this binding must be freshly proposed and approved.

The default registry remains disabled unless both `OUTBOX_GATEWAY_URL` (HTTPS URL
ending exactly `/send`, without credentials/query/fragment) and the separate
`OUTBOX_SIGNING_KEY` secret are supplied. This PR supplies neither. Follow the
repository production authorization requirements before configuring them. Gateway
transport/sender/journal/fee settings remain private deployment responsibilities.

## Version 1 protocol

POST exact JSON bytes to `/send` with `X-Outbox-Signature` equal to lowercase
HMAC-SHA256 hex over `b"deflock-outbox-v1\n" + body`. The signing secret is UTF-8.
The body is limited to 65,536 bytes. Duplicate JSON keys and unrecognized fields
are refused. The signed root fields are:

- `v`: integer 1, and `campaign_id`: configured campaign.
- `action_id`, `approved_by`: bounded nonblank strings.
- `approved_at`, `issued_at`: UTC ISO timestamps ending `Z`. Issuance must be
  within five minutes of the gateway clock; approval cannot be after issuance.
- `canonical_key`: existing `idempotency_key(kind, request_id, scope_version)`.
  This is SHA-256 of the existing NUL-separated identity, not the Worker card key.
- `draft`: `request_id`, `agency_id`, positive integer `scope_version`, `channel`
  (`email` or `muckrock`), `subject`, `body`, `to`, nonnegative integer
  `fee_cap_cents`, `kind` (`send_request` or `send_followup`), optional string
  `in_reply_to`. The gateway owns `from_addr`; header fields reject CR/LF/NUL.

The gateway refuses unsigned, stale or invalid frames before opening the journal.
Missing transport also refuses before opening it. It proposes the exact draft and
approves it with the attested identity, then calls the existing outbox safeguards:
agency/day and fee caps, atomic claim, and unresolved request holds. Existing draft
content under the same canonical key must match exactly. Identical successful
replay returns the stored receipt without calling the provider.

A 200 response includes `v`, `action_id`, `canonical_key`, `provider_message_id`,
`provider` and `sent_at`. The Worker binds the returned action and canonical
key to its request before using the receipt, and persists external evidence before
bookkeeping. It bounds reply bytes to 16 KiB, times fetch out at ten seconds,
refuses redirects, and holds unknown/mismatched replies as uncertain delivery.
TLS is required; replies are not separately HMAC-signed. Errors have
bounded static codes and never include provider exception text or draft contents.

## Recovery and limitations

An unknown delivery result or malformed provider receipt leaves the existing
outbox row `sending` and refuses automatic resend. A deliberate provider refusal
leaves it failed. Refreshing issuance with the old approval does not reset it.
A trusted, newly recorded approval strictly after the failure can reapprove the
identical canonical draft through `Outbox.approve_retry`; prior approver, approval
time, failure, resolver and new approval are preserved in `outbox_retry_approval`
in the same transaction as the state change. Rows with delivery evidence or an
unresolved `sending` state cannot be reapproved. Usual send caps still apply.
Use the established outbox reconciliation procedure with independently checked
provider evidence; delivered reconciliation can replay the saved receipt. A
not-delivered decision does not itself authorize another send: it must be followed
by a fresh approval. No key or scope-version change is used to bypass a hold.

The handler serializes dispatches inside one instance; the existing transactional
outbox supplies cross-connection send claims. Cross-process simultaneous proposal
creation may refuse safely rather than automatically retry. This is not a
distributed-service capacity guarantee. Do not invent new scope versions to evade
holds; distinct follow-up identity semantics still need an explicit product rule.

## Synthetic acceptance

Run `python3 -B -m unittest tests.test_outbox_gateway tests.test_outbox -v`.
Gateway tests cover signature tampering, stale approvals, strict schema and
duplicate keys, nested JSON bounds, missing provider, canonical draft conflicts,
fee/daily caps, concurrent replay, ambiguous hold, delivered reconciliation,
definite refusal, malformed receipt hold, journal failure redaction, and actual
loopback HTTP framing. Run `cd workers && npm ci && npm run check` for the real D1
approval/receipt caller, then `python3 -B tools/test_hosted_outbox_bridge.py` for
the actual TypeScript signer -> loopback Python gateway -> injected synthetic
transport. The latter uses a synthetic repository port and performs two signed
replays with one provider invocation; D1 is tested separately. Both are CI checks.
These tests do not establish real-provider delivery, production TLS, Access
deployment, distributed capacity or a complete live hosted pilot.
