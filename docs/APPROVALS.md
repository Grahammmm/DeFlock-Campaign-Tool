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
