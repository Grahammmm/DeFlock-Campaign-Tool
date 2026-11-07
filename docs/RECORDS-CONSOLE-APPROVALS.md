# Records console approvals

The loopback console is private and review-required. A machine-valid row is not
semantic, legal or privacy acceptance. No approval publishes by itself.

| Kind | Console / action | What Approve does | What it never does | Audit |
| --- | --- | --- | --- | --- |
| Record request (`first-request`) and correspondence follow-ups | Dashboard or next-steps → `/approvals/approve` | Verifies G's detached SSH signature over `approval_payload`, scope `records-draft-review-v1`, bound to revision; marks approved-to-send. When mail-drafts configuration is supplied, places the draft in the Drafts mailbox. | Does not send the message. | `approvals/queue.jsonl`: `approve` with `signed_revision`, project, principal, signature; draft placement journal (`mail_drafts.placements`). |
| Sending an approved draft | `/send` → POST `/send` | Re-reads identical mailbox bytes, checks switches, kill files, test-recipient restriction and caps; verifies a separate outbox signature over the exact outgoing text including To; then sends via SMTP. | Draft approval alone is not send approval. Test mode is the default: only `test_recipient` may receive mail until G alone sets `live: true` in owner-only `approvals/send.json`; no console or CLI live switch exists. | `approvals/mail-send.jsonl`: `attempt`, then `sent`/`refused` with actor, time, fingerprint, outbox key and message id; outbox SQLite sent row. |
| Next-step drafts (social post, site update, follow-up probe) | `/next-steps` → `/approvals/approve` | Records a signed decision only. An approved `next-site-update` may separately become a `site_update` packet via `public_packets.from_queue`. | Does not post, send or write a site change. | `approvals/queue.jsonl`: `approve`. |
| Public status lines L1–L6 | `/public-updates`: Veto/Undo, OFF, kill | No per-line Approve exists. CLI `records public-updates enable` verifies G's switch signature; advancement uses the 12-hour hold, caps and checks. Posting writes journal state plus the private `public-status-feed.json`, without transport. | Console cannot enable auto-post or approve an individual line. | `public-updates.jsonl`: `switch`, `hold`, `post`, `retract`, `refused`, `alert`. |
| Update packets (`document`, `summary`, `finding`, `site_update`) | `/packets` → `/packets/approve` or `/packets/reject` | Shows request, source, revision, age, expiry and the exact signature payload. Approve calls `public_packets.approve`, scope `public-packet-v1`, retaining G's revision-bound signature, 14-day expiry and lint. Reject calls `public_packets.decide`, recording the console actor and private reason. Stale forms conflict. | Never calls `write_change`, enables the packets switch or publishes. CLI write remains separate and requires switch ON plus re-verification. | `public-packets.jsonl`: `approved` with id, revision, principal, signature; `rejected` with id, revision, actor, reason. A later CLI write records `written` with file and SHA-256. |
| Agency page publishing | Not connected to console | Page units open draft site PRs held for G's approval. GitHub PR approval and the existing exact-head CI/review merge gates govern publication. | No console approval or merge exists. | GitHub review record and merge commit. |
| Document redactions | Not connected to console | CLI `tools/flock_redact.py signoff STORE SLUG SHA DOCUMENT --signature ...`, scope `flock-redaction`; separate apply builds a redacted derivative. | Packet approval does not sign off redactions or apply them. | `<store>/agencies/<slug>/redaction/<sha>/SIGNOFF.json`, then `ELIGIBILITY.json`. |
| Analysis findings | Not connected to console | No finding sign-off flow exists. Gap proposals are review-required. A finding packet requires `finding_state == "approved for public"`, but the finding-review transition is not implemented. | A valid packet is not evidence of human finding acceptance. | No finding-review audit journal exists yet. |

## Missing work (not implemented here)

- **CON-LINE-APPROVE-1**: signed, text-bound per-line status approval while OFF;
  requires its own design review, without bypassing kills, caps or text checks.
- **CON-SITEPR-1**: operator-staged, read-only snapshot of open site draft PRs
  (`site-prs.json`); the console must not merge them.
- **CON-REDACT-1**: read proposals awaiting sign-off and reuse the existing
  redaction sign-off path, not a new approval chain.
- **CON-FIND-1**: signed finding-review journal for the human transition to
  approved-for-public or rejected. Until that exists, findings stay held.

No real records or production settings are needed for console tests. Approving
packets works with the packets switch OFF; the switch controls the later CLI
site-change write, not the recording of G's signed decision.
