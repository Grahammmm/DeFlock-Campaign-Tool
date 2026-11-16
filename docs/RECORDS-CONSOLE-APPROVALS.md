# Records console approvals

The loopback console is private and review-required. A machine-valid row is not
semantic, legal or privacy acceptance. No approval publishes by itself.

| Kind | Console / action | What Approve does | What it never does | Audit |
| --- | --- | --- | --- | --- |
| Record request (`first-request`) and correspondence follow-ups | Dashboard or next-steps → `/approvals/approve` | Verifies G's detached SSH signature over `approval_payload`, scope `records-draft-review-v1`, bound to revision; marks approved-to-send. When mail-drafts configuration is supplied, places the draft in the Drafts mailbox. | Does not send the message. | `approvals/queue.jsonl`: `approve` with `signed_revision`, project, principal, signature; draft placement journal (`mail_drafts.placements`). |
| Sending an approved draft | `/send` → POST `/send` | Re-reads identical mailbox bytes, checks switches, kill files, test-recipient restriction and caps; verifies a separate outbox signature over the exact outgoing text including To; then sends via SMTP. | Draft approval alone is not send approval. Test mode is the default: only `test_recipient` may receive mail until G alone sets `live: true` in owner-only `approvals/send.json`; no console or CLI live switch exists. | `approvals/mail-send.jsonl`: `attempt`, then `sent`/`refused` with actor, time, fingerprint, outbox key and message id; outbox SQLite sent row. |
| Tie a received message to a request (`message_link`) | `/review?id=…` → pick one candidate → `/approvals/approve`, or `records decisions-apply` | Verifies G's detached SSH signature over `approval_payload(choice=…)`, scope `records-draft-review-v1`. For this kind the signed text carries one extra line right after `request:`, `choice: <request_id> (agency <agency_id>)`, built only by `approval_queue.message_link_choice_line` from the candidate `unmatched.choose` resolves. The console shows that exact text for the selected candidate before signing (nothing signable is shown before a selection); `approval_queue payload|approve --choice REQUEST_ID` does the same on the command line. Direct approve and `decisions-apply` rebuild the text from the entry's `choice` and append the link (used from the next run) only when the signature verifies for that choice. | Does not send, publish or place a mail draft. A signature for one candidate never links another: a different `choice` is refused (`SIGNATURE_REFUSED`, HTTP 403) and `message-links.jsonl` is not touched. Old tie approvals signed over the unbound text are unverified and cannot authorize any link; existing link lines are not removed. | `approvals/queue.jsonl`: `approve` with `signed_revision`, project, principal, signature and the bound `choice`; the verified row reports `approved_choice`; `message-links.jsonl` line; `decisions-applied.jsonl` receipt. |
| Next-step drafts (social post, site update, follow-up probe) | `/next-steps` → `/approvals/approve` | Records a signed decision only. An approved `next-site-update` may separately become a `site_update` packet via `public_packets.from_queue`. | Does not post, send or write a site change. | `approvals/queue.jsonl`: `approve`. |
| Public status lines L1–L6 | `/public-updates`: Veto/Undo, OFF, kill; per-line signed approval (DF2007-10, CON-LINE-APPROVE-1) | Veto/Undo, OFF and kill only make things safer. Per-line approval takes G's detached SSH signature over the exact line payload (`records public-updates line-payload --line ID`, scope `public-update-line-v1`: project, line id, revision = SHA-256 of the line's content, and the exact text), pasted into the form or given to `approve-line --line ID --revision REV --signature-file F`. In journal mode the form only journals `public_update_line_approval` ("recorded; pending apply"); `decisions-apply` verifies it through `public_updates.approve_line`. Recording an approval never advances or posts. At the next `advance`, an approved line posts even with auto-post OFF, without the 12-hour wait; it is distinct from the signed global ON (`enable`). | Never enables auto-post, never posts at approval time, and never bypasses the text checks, per-event uniqueness, daily caps, the kill file or a veto: a kill or a veto after the approval wins, and an edited line (new revision) needs a new approval. Posting writes journal state plus the private `public-status-feed.json`, without transport. Trust unavailable defers the approval (no final refusal). | `public-updates.jsonl`: `switch`, `line_approval` (id, revision, principal, signature), `hold`, `post`, `retract`, `refused`, `alert`; console journal entry and `decisions-applied.jsonl` receipt. |
| Update packets (`document`, `summary`, `finding`, `site_update`) | `/packets` → `/packets/approve` or `/packets/reject` | Shows request, source, revision, age, expiry and the exact signature payload. Approve calls `public_packets.approve`, scope `public-packet-v1`, retaining G's revision-bound signature, 14-day expiry and lint. Reject calls `public_packets.decide`, recording the console actor and private reason. Stale forms conflict. | Never calls `write_change`, enables the packets switch or publishes. CLI write remains separate and requires switch ON plus re-verification. | `public-packets.jsonl`: `approved` with id, revision, principal, signature; `rejected` with id, revision, actor, reason. A later CLI write records `written` with file and SHA-256. |
| Agency page publishing | Read-only `/site-prs` snapshot (DF2007-09, CON-SITEPR-1); no approval action | Page units open draft site PRs held for G's approval. GitHub PR approval and the existing exact-head CI/review merge gates govern publication. The console lists the operator-captured open drafts and their age only. | No console approval, merge or refresh exists; the console never contacts GitHub. | GitHub review record and merge commit. |
| Document redactions | Console takes the pasted signature (DF2007-08, CON-REDACT-1); the CLI verifies it | `records panel --flock-store STORE` lists unsigned proposals at `/redactions` with the exact `flock_redact payload` text and journals a pasted signature (`redaction_signoff`); `tools/flock_redact.py signoff-apply STORE SLUG SHA DOCUMENT --decisions J` replays it through the one `signoff` function, scope `flock-redaction`, same trust file. The console verifies nothing and signs nothing; the separate `apply` still builds the derivative by hand. | Packet approval does not sign off redactions or apply them. A submitted signature is not a sign-off until `signoff-apply` receipts APPLIED. | `<store>/agencies/<slug>/redaction/<sha>/SIGNOFF.json`, then `ELIGIBILITY.json`. |
| Analysis findings | `records finding-review list\|approve\|reject`, or the console's `/findings/review` (journaled; applied at the next analysis tick) | Verifies a detached SSH signature over the exact finding text (`finding_payload`, scope `finding-review-v1`, bound to agency slug, finding id and a sha256 of topic, claim, fact ids and rule ids), or records a rejection with a private reason. The packet step then stops holding that exact finding as `FINDING_UNREVIEWED`. | Does not approve, sign, stage-publish or send the finding packet: packet approval (`public-packet-v1`) stays a separate decision. A changed claim or fact list needs a new review. Trust unavailable is `FINDING_REVIEW_DEFERRED`, never a final refusal. | `<analysis root>/.analysis/finding-reviews.jsonl`: append-only, hash-chained `approve` (principal, signature, finding text) and `reject` (reason). |

## Correcting a wrong message tie (`message_link` replacement)

A tie is never edited or deleted. A reply tied to the wrong request is corrected with a second, signed decision:

1. On the agency timeline (`/agency?agency=…`), the **Message ties** section lists each message tied by an approved link
   with the current request. **This tie is wrong** (POST `/message-links/relink`, CSRF-protected, fields `message_sha256`
   and `supersedes`) only queues one new `message_link` decision for that message. It changes no link and approves
   nothing. In a hosted journal the action is the queue-only `message_link_relink` entry; the page then says
   "queued for next apply", not that the tie changed. A read-only export shows no control; an export taken before this
   feature has no tie metadata and asks for a refresh (an empty set is a real, empty set). The ties shown are the ones at
   the export's age.
2. The decision names the line it replaces: its evidence carries `supersedes` (the SHA-256 of the exact current line in
   `message-links.jsonl`, taken over the stored bytes without the terminating LF) and `replaces` (the current request),
   and offers the candidates **without** the current request. With no other candidate only a rejection is possible. The
   decision identity is one per message and replaced line; asking again for the same line returns the same decision, and
   a later correction (after another line) is a fresh decision.
3. Approve exactly as for a first tie: the signed text carries the `choice:` line (`message_link_choice_line`) and the
   evidence, so the signature covers the fingerprint and the candidate list. Direct approve and `decisions-apply` pass only
   the fingerprint inside that signed evidence to `unmatched.append_link(..., supersedes=…)`.
4. `append_link` accepts a different choice for a message only from such a line, and only while the current line is still
   the replaced one; the check, read and append happen under one lock. If the current link changed after the decision was
   queued the replacement is refused with `message_link_stale` (HTTP 409 on the console, `REFUSED message_link_stale` in
   `decisions-apply`) and nothing is appended. If it changes after the approval was recorded, the approval record stays and
   still no link is appended. An exact replay of an applied replacement is a no-op. A queue-only request, an unsigned
   or wrong-choice approval and a tampered fingerprint never move a link.
5. `read_links` keeps the latest valid line per message, so the **next** `records run` outputs stage reads the new request
   into `received-items.json`; earlier run outputs are not rewritten. Nothing is sent or published, and the earlier link
   line and the message evidence are kept. To refresh the console after a decision, take a new export.

## Site draft PR snapshot (`/site-prs`, CON-SITEPR-1)

The operator captures the site repository's open draft pull requests outside this tool and imports the saved file. The
tool never runs the command and makes no network call:

    gh pr list -R OWNER/REPO --state open --draft --limit 50 --json number,title,isDraft,url,headRefName,updatedAt > capture.json
    records site-prs snapshot --from capture.json --export-root DIR --repository OWNER/REPO

- `--from` is a local regular file (at most 256 KiB), a JSON array of at most 50 rows with exactly the six fields; every
  row must be a draft and its `url` exactly `https://github.com/OWNER/REPO/pull/<number>` for the named repository.
  Control characters are stripped from titles. Anything else (extra field, other host, duplicate number or key) is refused
  and nothing is written. A full 50-row capture may have been cut off by `--limit`: check the repository separately.
- `--export-root DIR` is the existing console export root (the directory holding `current`) in snapshot mode, or the
  project directory in live mode. The file is `DIR/site-prs.json` (mode 0600, with `captured_at`), beside the sealed
  export and never inside it, so the export manifest and pin are unchanged. A re-import replaces the previous snapshot.
- `/site-prs` lists the rows with their age since the captured `updatedAt` (not proof of review inactivity), says
  "captured N hours ago" and shows a stale banner when the capture is more than 24 hours old. Home links the page when
  a snapshot exists. A missing, malformed or oversize file reads "unavailable", never zero. A snapshot is not proof that a
  pull request is still open or that the list is complete. The page has no merge, approve or refresh control.

## Missing work (not implemented here)

- **CON-LINE-APPROVE-1**: implemented (DF2007-10), see the Public status lines row. Missing: nothing.
- **CON-SITEPR-1**: implemented (DF2007-09) as a read-only snapshot, see
  "Site draft PR snapshot" below. Missing: nothing; the console must not merge them.
- **CON-REDACT-1**: implemented (DF2007-08): the console reads proposals awaiting
  sign-off and journals a pasted signature; `flock_redact.py signoff-apply` reuses
  the existing sign-off path (no new approval chain). Missing: nothing for sign-off;
  building the derivative (`apply`) stays an attended CLI step.
- **CON-FIND-1**: the signed finding-review journal and `records finding-review`
  CLI are implemented (see the Analysis findings row). The console takes the
  review too (DF2007-06): `records panel --export-root DIR --run-id auto
  --decisions-dir J --analysis-root ROOT` serves `/findings/review`, which lists
  each finding held `FINDING_UNREVIEWED` (agency, topic, claim, cited facts with
  record prefix and page, the F4 model verdict and the exact `finding-review-v1`
  text). Approve (a pasted detached signature) and Reject (a private reason) only
  append a `finding_review` entry to `J/decisions.jsonl` (`action`
  `finding_approve`/`finding_reject`, `target_id` `agency:finding_id`,
  `revision_seen` the finding's sha256, `value` the analysis-root binding plus the
  signature or reason); success reads "recorded for the next analysis tick", never
  "approved". The entry is applied by `records analysis-tick --root ROOT
  --from-journal J/decisions.jsonl`, before any agency step or packet staging,
  through `finding_review.approve|reject` with the entry id and expected hash, and
  receipted in `ROOT/.analysis/finding-decisions-applied.jsonl` (0600,
  `{entry_id, at, outcome}` only): `APPLIED`, `STALE_FINDING` (the finding changed
  after the page), `SIGNATURE_REFUSED` or `REFUSED <code>`; a receipted entry is
  `ALREADY`. A missing trust anchor or an unavailable review store is `DEFERRED`
  (nothing receipted, tried again next tick); an unreadable receipt file refuses
  the whole pass (`FINDING_APPLY_REFUSED`). An entry for another analysis root is
  left alone. `records decisions-apply` prints `ANALYSIS_TICK_REQUIRED` for these
  entries and never receipts them. The console is read-only to the analysis root;
  the later packet approval (`public-packet-v1`) stays a separate decision.

No real records or production settings are needed for console tests. Approving
packets works with the packets switch OFF; the switch controls the later CLI
site-change write, not the recording of G's signed decision.
