# Approvals: only a human signature approves an outbound message

DF-2. Nothing in this repository treats a typed name or a sentence as approval. An approval is
an SSH signature by an owner key, over the exact text that would go out, verified against a
root-owned trust file. An agent running as the campaign user cannot edit that file or swap the
verifier, so it cannot approve its own proposal. This change does not bind, enable or configure
any sender.

| Surface | Approval state (a field) | What makes it valid |
| --- | --- | --- |
| `campaign_tool/outbox.py` journal | `state = 'approved'` plus `approval_evidence` | An owner signature over `payload KEY`, re-verified before every send. |
| Hosted gateway (`runner/outbox_gateway.py`) | the same columns | The Worker's HMAC-signed frame for this exact draft, re-verified before every send. It is honoured only inside the gateway. |
| Flock email (`tools/flock_email.py`) | `EMAIL-APPROVAL.json` `"state": "approved"` | `tools/flock_approve.py` writes it only after the signature verifies. The stage reads `approval recorded` / `sent recorded` only when the signature is re-verified (`flock_approve.py status`, `flock_requests.py open` and `--check`); `flock_email.py` alone reports `approval unverified` (see below). |

## Redaction sign-off (`flock-redaction`)

`tools/flock_redact.py payload STORE SLUG SHA DOCUMENT` prints the exact canonical
proposal to review and sign with the same `deflock-approval` namespace and trust
file. `signoff STORE SLUG SHA DOCUMENT --signature FILE` records only the principal
returned by the shared verifier. See [REDACTION.md](REDACTION.md) for the private
ceremony. No typed name, setting or automation approves; apply re-verifies the
signature. Redaction eligibility is not approval to publish or send anything.

## One-time setup (G, needs sudo; nothing works until this is done)

1. Make an approval key that agents cannot use. Preferred is a hardware key, which needs a touch
   for every signature: `ssh-keygen -t ed25519-sk -f ~/.ssh/deflock_approver`. Without a
   hardware key, use `ssh-keygen -t ed25519 -f ~/.ssh/deflock_approver` with a passphrase that
   no agent knows.
2. Write one trust line, `g@deflock namespaces="deflock-approval" <contents of ~/.ssh/deflock_approver.pub>`,
   into `approvers.allowed_signers`, then install it root-owned:
   `sudo install -d -o root -g root -m 0755 /etc/deflock` and
   `sudo install -o root -g root -m 0644 approvers.allowed_signers /etc/deflock/approvers.allowed_signers`.
   Approval is refused if this file, or any directory above it, is not root-owned or is group- or
   world-writable. `/usr/bin/ssh-keygen` must also be root-owned.

## Approving an outbox row (G)

```
python3 -m campaign_tool.outbox --journal J payload KEY > approve.txt   # the full text: To, Subject, body, journal record
less approve.txt                                                          # read it; this is exactly what you sign
ssh-keygen -Y sign -f ~/.ssh/deflock_approver -n deflock-approval approve.txt   # writes approve.txt.sig
python3 -m campaign_tool.outbox --journal J approve KEY --signature approve.txt.sig
```

The recorded `approved_by` is the principal from the trust file, not a name anyone types.

After a definite failure (state `failed`, no receipt), a retry needs a new signature over the
next round. `payload KEY` then prints that round (`round: 1`, and so on), so the same three steps
apply, with `approve-retry` in place of `approve`:

```
python3 -m campaign_tool.outbox --journal J payload KEY > retry.txt         # shows round: N+1
ssh-keygen -Y sign -f ~/.ssh/deflock_approver -n deflock-approval retry.txt
python3 -m campaign_tool.outbox --journal J approve-retry KEY --signature retry.txt.sig
```

The first approval's signature covers round 0, so it cannot be replayed for a retry.

## Approving several outbox letters (G)

`python3 -m campaign_tool.outbox --journal J payload-batch --out DIR --agency ID`
exports proposed rows (omit `--agency` for all; repeat it for several agencies).
DIR must be absent or empty, not a symlink. Read INDEX.md and **every** .txt;
the index masks agency names in comparisons, but each payload is exact and unmasked.
From DIR, run `ssh-keygen -Y sign -n deflock-approval -f <your key> *.txt`.
Then run `python3 -m campaign_tool.outbox --journal J approve-batch --dir DIR`.
Each letter keeps its own signature, verified by the existing approval path.
Changed letters, missing or invalid signatures are skipped and named; re-export
changed letters into a fresh directory and read them again before signing.
Nothing sends from either batch command. Sending remains a separate per-row action.

## Approving a Flock email (G)

```
python3 tools/flock_approve.py payload RUNDIR > approve.txt
less approve.txt
ssh-keygen -Y sign -f ~/.ssh/deflock_approver -n deflock-approval approve.txt
python3 tools/flock_approve.py approve RUNDIR --signature approve.txt.sig   # writes EMAIL-APPROVAL.json
python3 tools/flock_approve.py verify RUNDIR                               # operator, right before sending by hand
python3 tools/flock_approve.py status STORE                                # every run's stage, signatures checked
```

The signed text is the draft byte for byte, minus only its last line, which must be the one
`Sent by:` record line. Any other edit to the draft after approval, including a `Sent by:` line
anywhere else, voids the approval:
`status` then reports `malformed`. A field whose signature does not verify (hand-written, fake,
an untrusted key, or no trust file installed) is never `approval recorded`. It shows as
`approval unverified` / `sent unverified`. `flock_approve.py status` exits 1 on it,
`flock_requests.py open` refuses to open a request from it, and `flock_requests.py --check`
reports it as an error.

## How a drafted deadline follow-up arrives in the queue

`python3 tools/flock_deadlines.py run --store STORE --date YYYY-MM-DD` drafts one follow-up per
(request, rule, reminder date) once an open request's provisional reminder date has passed.
The draft is built by `flock_requests.py`'s own draft step and written to
`STORE/deadlines/drafts/<slug>/<request>/<rule>/<reminder date>/FOLLOWUP-DRAFT.md`, and one row
with state `pending` is added to `STORE/deadlines/QUEUE.csv`. The tool writes no other state:
it has no send path and cannot write an approval. A note, evidence text or a line reading
"APPROVED" never moves a row out of `pending`. A queued draft that nobody approves stays drafted,
and every run's status line says `pending human approval`.

To send one, copy it into a normal email run (`STORE/agencies/<slug>/runs/<date>/`) and use the
Flock email approval steps above; the signed `EMAIL-APPROVAL.json` is still the only approval.
If you edit a queued draft, the tool keeps your edit and puts its fresh text in the
`<reminder date>.suggested/` sibling. While the law package is `status: draft`, every date is
labelled `provisional reminder; not a legal due date`.

## Known limits (follow-up cards)

- `flock_email.py` keeps its no-send import guard (`tests/flock/test_email.py` forbids
  `subprocess` and `campaign_tool`), so it cannot check a signature itself. Its own `status` and
  `lint` therefore never report `approval recorded`, only `approval unverified`, and they point to
  `flock_approve.py status`. Consumers inject the verifier from `tools/flock_approve.py`; the
  guard was left as it is.
- `Sent by:` in a Flock draft is still a prose record line. It is not a gate, but item 4 of
  DF-2 asks for state as a field.
- Anyone who can write the journal can delete `outbox_retry_approval` rows, which lowers the
  retry round so that an earlier valid signature for the same exact text verifies again. That
  allows a resend of text G already approved once. It never allows unapproved text.
- The gateway's HMAC secret is a deployment credential. Whoever holds it can attest approvals,
  so it must stay out of agent reach.
- Three approval surfaces exist: the Worker `external_action`, the outbox journal and Flock
  files. Which one is canonical is still undecided.

## Records reply drafts: review queue (REC-AUTO-4)

The records outputs stage turns each received item (REC-AUTO-3) into at most one cited draft:
an extension reply, a request for reconsideration after a denial, a fee reply, or a follow-up
listing cited `gap` findings. Drafts quote the agency's words with their page numbers and carry
the target agency, the request, why, the deadline the agency itself stated (quoted, never
calculated) and `State: pending-approval`. Nothing is sent.

- Store: `<project>/approvals/queue.jsonl` (directory 0700, file 0600), append-only. Duplicate
  runs never overwrite an edit or reopen a rejection; changed evidence for the same item is refused.
- `python -m campaign_tool.records approvals list|payload|approve|reject|edit --project DIR`.
  `approve` takes a detached SSH signature file over `payload` (same trust file as the outbox);
  a typed name is refused. An edit returns the draft to pending and voids earlier approvals.
- Approval only marks the draft reviewed and approved-to-send. It cannot send, and an outbox or
  Flock signature cannot approve a draft. The outbox and `EMAIL-APPROVAL.json` stay the only
  send authority.
- A truncated or malformed journal tail is refused (`queue_invalid`) and left untouched for the
  operator; the outputs stage then reports `OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE`.

## No-response follow-ups (REC-AUTO-7)

After each outputs stage, the records pipeline reads that run's `CURRENT-REQUEST-REGISTER.json` and,
for agencies you switch on, queues a pending draft when a request's clock has passed with no
response. Nothing is sent; approval is not sending.

Settings: `<project>/followup-settings.json` (regular file, owner-only, at most 64 KiB; a symlink,
group/world-readable, oversized, duplicate-key or unknown-value file is refused as a whole):

```json
{"schema": "records-followup-settings-v1",
 "default": {"autonomy": "off"},
 "agencies": {"<agency_key>": {"autonomy": "draft", "notice_after_days": 10}}}
```

- `autonomy` is `off` (default, also when the file is absent) or `draft`. There is no sending value;
  `send`, `auto` or `on` make the file invalid, every agency stays `off` and the stage reports
  `OUTPUTS_FOLLOWUP_SETTINGS_INVALID`. `notice_after_days` (1 to 60, default 10) is the wait after an
  approved reminder before a formal notice is drafted.
- `reminder-no-response`: the register's determination reminder date passed with no determination.
- `reminder-promised-date`: the agency's own promised date (tracker column `promised_at`) passed and no
  production is recorded on or after it.
- `formal-notice-no-response`: drafted only after the reminder for the same clock was approved and
  `notice_after_days` have passed. A pending, rejected or edited reminder never escalates.
  It asks for a written response and states no violation, penalty or lawsuit.
- Each draft is keyed by agency, request and clock date, so a rerun adds nothing, an edit or rejection is
  never overwritten, and a new date (a recorded extension or a new promise) is a new draft. Rows with a
  register issue, a closed status or an `off` agency yield nothing.
- Dates and wording come from the register and its limits text. The period is still the register's fixed
  10 + 14 day rule (moving it into the law package is a follow-up); it is a provisional reminder, not a
  legal determination.
- `python -m campaign_tool.records followups plan|rows --project DIR --run-id RID` prints the planned drafts
  or the Deadlines and Overdue rows, read-only.

## Drafts mailbox (REC-SEND-1)

Approving a records reply draft in the console can also put the same text in the campaign's outbound
mailbox Drafts folder, so it can be read in the mail client. **This only places a draft. Nothing is sent**;
there is no SMTP call in `campaign_tool/records/mail_drafts.py`. Sending is a separate, later step.

- Config (per campaign, owner-only, same shape as `records mail-poll`'s) must name the Drafts folder with
  `"folder"` (for example `"Drafts"`; `INBOX` and a missing folder are refused). The From address is the
  config `user`. The password stays in `password_file`; it is read only by `mail_poll` at connect time.
- `records mail-drafts place --project DIR --config FILE [--dry-run]` places every approved, not yet placed
  row and prints counts only. `records mail-drafts list --project DIR` prints the journal without text.
  `records panel ... --mail-drafts-config FILE` places the one draft right after a successful Approve; a
  placement failure is shown on the page and never undoes the approval.
- The draft is threaded (`In-Reply-To`, `References`, `To` from `Reply-To`/`From`) when the ledger links the
  received item to a stored agency message; otherwise it is placed unthreaded, `To` empty, and the page says
  why. A thread is never guessed from subject text.
- Running it twice makes one draft: each draft has a Message-ID derived from the queue id and revision, the
  mailbox is searched for it before appending, and `approvals/mail-drafts.jsonl` (0600, append-only) records
  the message id, folder and a fingerprint (sha256 of the exact message bytes).
- If the text is edited and approved again, the new revision is placed as a new draft and the older
  placement is marked `superseded` in the journal and on the page. The code never deletes, flags or moves
  mailbox messages, so remove the superseded draft in the mail client.

## Records console: Approve and send

A records draft goes queue-approved (REC-AUTO-4 signature), is placed in the Drafts mailbox (REC-SEND-1), and is sent
only by a separate click on the console with a **second**, outbox-specific signature (`ssh-keygen -Y sign -n
deflock-approval` over the payload the send page shows). The two signatures are not interchangeable. See the
"Records console send path" section of `docs/OUTBOX.md` for the switches (shipped off, kill file present), the daily
cap, the fingerprint check and the log. Start the console with `--send-config FILE` to show the button at all.
