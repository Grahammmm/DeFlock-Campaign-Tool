---
name: flock-followup
description: Use when listing which open ALPR public-records requests need a provisional determination reminder. Reads a private REQUESTS.csv and prints reminders only; never sends or concludes lateness.
skill_version: 1.0
---

# Flock follow-up (part 1: CPRA request tracker)

`REQUESTS.csv` is a private reminder list, not a request registry. `request_id` copies the
agency's own request number. The canonical ledger wins any disagreement: fix the row by hand,
never the ledger. Reminder dates come from the us-ca law package (`status: draft`), which this
skill never upgrades. Drafting a follow-up is a later version and is not part of v1.0.

## Inputs

- `<store>/agencies/<slug>/REQUESTS.csv` (v1), `<slug>` = lowercase agency name, each run of
  non-`[a-z0-9]` replaced by `-`, ends stripped. Kept in the private store, never in a repository.
- Header, exactly: `request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,source_sha256`
- `status` is one of open, determined, produced, closed, withdrawn. Dates are YYYY-MM-DD.
- Non-empty `receipt_evidence` (120 characters at most) needs `receipt_date` and a 64-hex
  `source_sha256`. A `receipt_date` with empty evidence is an error.

## Steps

1. Transcribe each request from the existing ledger/L2 transcription into one row. Leave
   `receipt_evidence` and `receipt_date` empty unless a cited source shows actual receipt; a send
   date is not receipt. Fill `extension_notice_date` only from an extension notice you hold.
2. Validate: `python3 tools/flock_requests.py --check <file or store dir>`. Fix every
   `row N: <field> <reason>` line (N is the spreadsheet row) until it ends with `errors=0`.
3. List reminders: `python3 tools/flock_requests.py due <file or store dir>`. One line per open
   row, soonest first, unknown receipts last. With any error it prints nothing and exits 1.

## Outputs

stdout only. Known receipt: `provisional reminder | id | agency | determination DATE | receipt DATE + N
calendar days[ + M extension notice] | Gov. Code 7922.535 | not a legal due date`. No evidence:
`... | receipt unknown | no date computed`, with no date on that line. The tool writes no file.

## Never

- State or compute a date without receipt evidence.
- Use the words late, overdue or violation, or conclude lateness (the quoted package note is the
  only exception); holidays are not modelled.
- Send, schedule or draft a message, or read mail, the ledger or the network from this tool.
- Treat REQUESTS.csv as a registry in place of the canonical ledger, or commit or quote real rows.

## Golden case

`tests/fixtures/flock/requests-sample.csv` (synthetic):
`python3 -B tools/flock_requests.py --check tests/fixtures/flock/requests-sample.csv`

## Changelog

- 1.0: REQUESTS.csv v1, `--check` and `due`.
