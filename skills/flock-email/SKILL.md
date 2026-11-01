---
name: flock-email
description: Use when turning a FOLLOWUP-DRAFT.md into an email draft with a cited recipient. Writes a draft only; never sends, queues or approves.
---

# Flock email (draft only)
Wraps F5b's `FOLLOWUP-DRAFT.md` into `EMAIL-DRAFT.md` for G to read. The tool cannot send.
## Inputs

- `<store>/agencies/<slug>/runs/<date>/FOLLOWUP-DRAFT.md` (flock-followup).
- `<store>/agencies/<slug>/CONTACT.csv`, header `agency,role,email,source_url,source_sha256,checked_at,added_by`:
  one `records` row with an `https` source_url, 64-hex hash and YYYY-MM-DD date.

## Cite
- A Terra legwork card adds the cited CONTACT row; nobody guesses an address.

## Check
- Before a draft: `python3 tools/flock_email.py check <store> --max-age-days 90` lists who is ready (`contact | <slug> | ready | domain=<domain>`) or `stale`; reads only.
  A domain warning (`warn: domain differs from source host <host>`, counted in `mismatch=N`) needs a human look at the source page before drafting.

## Draft
- `python3 tools/flock_email.py draft <run folder> --drafts <operator folder outside the repo> --max-age-days 90`. It prints
  `written=2 to_domain=<domain>`; errors name `row N: <field>` or `contact: no records row` and write nothing.
  When draft says stale (or `in the future`), re-check the contact page and update `checked_at`; use `--max-age-days 90` unless the organizer sets another number.

## Lint
- Run `python3 tools/flock_email.py lint <run folder> --drafts <operator folder>` after any edit and before asking G to approve: copies agree,
  `To:` is the current CONTACT.csv address, no placeholder or private pattern; exit 0 (`rules=0`) is not approval.

## Show
- The operator shows G the draft; only G's explicit reply for that exact draft permits sending, by hand.

## Status
- After sending by hand, fill `Sent by: <name> YYYY-MM-DD` (the send date); `python3 tools/flock_email.py status STORE` must exit 0
  (one `email | <slug> | <date> | <stage>` line per run; reads only, prints no address or filled value).
  `python3 tools/flock_requests.py --check STORE` then needs a REQUESTS row of that agency with `sent` equal to that date.

## Open
- Add that row with `python3 tools/flock_requests.py open --from-email <run folder> --request-id <your id>` (not by hand): it
  refuses a run not `sent recorded` or already opened, appends an `open` row and an `opened` REQUEST-STEPS line; ids only.

## Never

- Send, schedule, queue or call any outbox, SMTP or API; fill `G APPROVED`; drop the DRAFT header.
- Guess an address or use a contact without a cited source; commit or quote real drafts or contacts.
