---
name: flock-followup
description: Use when listing which open ALPR public-records requests need a provisional determination reminder, or when turning F4's QUESTIONS.md into a follow-up request draft. Reads private files; never sends or concludes lateness.
skill_version: 1.6
---

# Flock follow-up (CPRA request tracker and follow-up draft)

`REQUESTS.csv` is a private reminder list, not a request registry. `request_id` copies the
agency's own request number. The canonical ledger wins any disagreement: fix the row by hand,
never the ledger. Reminder dates come from the us-ca law package (`status: draft`), which this
skill never upgrades. Version 1.1 adds a draft-only follow-up request (`draft`); it is never sent by this tool.

## Inputs

- `<store>/agencies/<slug>/REQUESTS.csv` (v1), `<slug>` = lowercase agency name, each run of
  non-`[a-z0-9]` replaced by `-`, ends stripped. Kept in the private store, never in a repository.
- Header, exactly: `request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,source_sha256`
- `<store>/agencies/<slug>/runs/<date>/QUESTIONS.md` (from flock-verify, F4), for `draft` only. Its
  records_sought lines already ask for existing records only.
- `status` is one of open, determined, produced, closed, withdrawn. Dates are YYYY-MM-DD.
- Non-empty `receipt_evidence` (120 characters at most) needs `receipt_date` and a 64-hex
  `source_sha256`. A `receipt_date` with empty evidence is an error.

## Steps

1. Transcribe each request from the existing ledger/L2 transcription into one row. Leave
   `receipt_evidence` and `receipt_date` empty unless a cited source shows actual receipt; a send
   date is not receipt. Fill `extension_notice_date` only from an extension notice you hold.
2. Validate: `python3 tools/flock_requests.py --check <file or store dir>`. Fix every
   `row N: <field> <reason>` line (N is the spreadsheet row) until it ends with `errors=0`.
   With a store dir it also ties each sent email run to a request (`<slug>: run <date>: ...` lines; `sent_runs= matched=`).
3. List reminders: `python3 tools/flock_requests.py due <file or store dir>`. One line per open
   row, soonest first, unknown receipts last. With any error it prints nothing and exits 1.
   Relate it to a day: `python3 tools/flock_requests.py due <file or store dir> --as-of YYYY-MM-DD`.
4. See where each agency stands: `python3 tools/flock_requests.py summary <file or store dir>`. One line per
   agency, then totals; no deadline is computed. With any error it prints nothing and exits 1.
   Documents per request: `python3 tools/flock_requests.py summary <file or store dir> --inventory <INVENTORY.csv>`
   adds posted/withheld counts per request and names INVENTORY rows with an unknown request; counts only; withheld never named.
   Record a step (use record, not hand edits): `python3 tools/flock_requests.py record <store dir> --agency <slug> --request <id>
   --step receipt|extension|determination|produced|closed --date YYYY-MM-DD --evidence <text, 120 max> --sha256 <hex>`. It updates
   the row and appends to private REQUEST-STEPS.csv; a status or date regression is refused and nothing is written.
   Compare with the correspondence report: `python3 tools/flock_requests.py reconcile <store dir> --report <REPORT.json>`; the report is the records owner's export; reconcile only reads it, names sent/determination/status disagreements, unmatched rows and report-only counts (no ledger ids), exit 3 on any.
   Fees: `python3 tools/flock_requests.py fees record <store dir> --agency <slug> --request <id> --kind estimate|invoice|paid
   --amount-cents N --date YYYY-MM-DD --evidence <text> --sha256 <hex>` appends to private FEES.csv; `python3 tools/flock_requests.py fees
   check <store dir> --cap-cents N` names requests over the cap (exit 3). The organizer sets the cap; there is no default. Paying any fee is G's decision.
5. Draft a follow-up: `python3 tools/flock_requests.py draft <run folder> --requests <REQUESTS.csv or store dir>
   --from YYYY-MM-DD --to YYYY-MM-DD`. It checks every QUESTIONS.md line, holds back any item whose
   prior request is still `open` and any exact repeat, and exits 1 with `line N: <reason>` or
   `no items to draft` and no file. It never overwrites an existing FOLLOWUP-DRAFT.md. Read the
   held list at the end of the file, remove it, and leave sending to G's approval.

## Outputs

stdout only. Known receipt: `provisional reminder | id | agency | determination DATE | receipt DATE + N
calendar days[ + M extension notice] | Gov. Code 7922.535 | not a legal due date`. No evidence:
`... | receipt unknown | no date computed`, with no date on that line. `draft` prints `items=I held=H written=FOLLOWUP-DRAFT.md` and writes only
`<run folder>/FOLLOWUP-DRAFT.md` (mode 0600), headed `DRAFT - not sent - G approval required`, for
existing records only. `summary` prints `request status | agency | open=N determined=N produced=N closed=N withdrawn=N |
receipt_unknown=N | oldest_open_sent DATE or -`, then a totals line. `due`, `summary` and `--check` write no file.
`--as-of` phrases (`in N days`, `reminder date today`, `reminder date passed N days ago`) are provisional reminder arithmetic, not a legal status.

## Never

- State or compute a date without receipt evidence.
- Use the words late, overdue or violation, or conclude lateness (the quoted package note is the
  only exception); holidays are not modelled.
- Send or schedule a message, or remove the DRAFT header, or read mail, the ledger or the network from this tool.
- Ask for explanations, opinions or answers, or repeat an open request.
- Treat REQUESTS.csv as a registry in place of the canonical ledger, or commit or quote real rows.

## Golden case

`tests/fixtures/flock/requests-sample.csv` (synthetic):
`python3 -B tools/flock_requests.py --check tests/fixtures/flock/requests-sample.csv`

Draft: `tests/fixtures/flock/questions-sample.md` (synthetic) for agency folder
`sample-city-police-department`; see `tests/flock/test_requests.py`.

## Changelog

- 1.6: `fees record` / `fees check --cap-cents N`: sourced fee rows and an organizer cap check (no default cap).
- 1.5: `record` sets one dated, sourced step on a request; regressions refused.
- 1.4: `summary --inventory CSV` adds document counts per request (counts only; withheld never named).
- 1.3: `due --as-of DATE` adds days to or since each reminder and upcoming_7d/passed totals.
- 1.2: `summary` prints one request-status line per agency (stdout only).
- 1.1: `draft` writes FOLLOWUP-DRAFT.md (draft only, held items listed).
- 1.0: REQUESTS.csv v1, `--check` and `due`.
