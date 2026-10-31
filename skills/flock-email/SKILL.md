---
name: flock-email
description: Use when turning a FOLLOWUP-DRAFT.md into an email draft with a cited recipient. Writes a draft only; never sends, queues or approves.
skill_version: 1.1
---

# Flock email (draft only)
Wraps F5b's `FOLLOWUP-DRAFT.md` into `EMAIL-DRAFT.md` for G to read. The tool cannot send.
## Inputs

- `<store>/agencies/<slug>/runs/<date>/FOLLOWUP-DRAFT.md` (flock-followup).
- `<store>/agencies/<slug>/CONTACT.csv`, header `agency,role,email,source_url,source_sha256,checked_at,added_by`:
  one `records` row with an `https` source_url, 64-hex hash and YYYY-MM-DD date.

## Steps

1. A Terra legwork card adds the cited CONTACT row; nobody guesses an address.
2. Before a draft: `python3 tools/flock_email.py check <store>` lists who is ready (`contact | <slug> | ready | domain=<domain>`); reads only.
3. `python3 tools/flock_email.py draft <run folder> --drafts <operator folder outside the repo>`. It prints
   `written=2 to_domain=<domain>`; errors name `row N: <field>` or `contact: no records row` and write nothing.
4. The operator shows G the draft; only G's explicit reply for that exact draft permits sending, by hand.

## Never

- Send, schedule, queue or call any outbox, SMTP or API; fill `G APPROVED`; drop the DRAFT header.
- Guess an address or use a contact without a cited source; commit or quote real drafts or contacts.
