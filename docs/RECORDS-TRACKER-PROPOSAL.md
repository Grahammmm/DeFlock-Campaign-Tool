# Proposed request tracker rows (pilot data)

`data/slo-pilot/MASTER-REQUEST-TRACKER.proposed.csv` holds one row for each of the 17 pilot requests. It is a
file for a person to check. Nothing applies it: copying it over a live project's `MASTER-REQUEST-TRACKER.csv`
is a separate step that needs the project owner's go-ahead. Every row is review-required; none of it is a
legal finding.

## Columns

The first columns reuse the names the register reader already knows (`request_id`, `agency`, `scope`,
`submitted_at`, `agency received date`, `extension`, `status`), so `records` can read the file as it is.

| Column | Meaning |
|---|---|
| `submitted_at` | The date we sent or submitted the request (Pacific date). |
| `agency received date`, `receipt_basis` | `recorded_receipt`: an agency message or portal notice states receipt. `submission_proxy`: no such message, so the sent date stands in. |
| `extension` | Each extension notice with its date and message id. `none recorded` means none was found, not that none exists. |
| `promised_dates` | Dates the agency itself named, with the message id. Dates marked "derived" are our arithmetic on an agency estimate. |
| `status` | `waiting_on_agency`, `ball_in_our_court`, `closed_by_agency` or `unknown`. |
| `asked_items` | Numbered items with a proposed tier: `T1` audit records and search/use logs, `T2` policy, retention, access and sharing, `T3` contracts, `T?` outside the three tiers. Numbers come from the sent email where the store holds it. Requests sent before 2026-08-25 are not in the store, so they carry one unnumbered entry. |
| `source_message_ids` | `Folder:uid` ids in the mail store (INBOX UIDVALIDITY 1785435472, Sent 1785435473). `A..B` is a range. `none-in-mail-store` is allowed only with `non_mail_sources`. |
| `walk_overdue_2026-11-05` | The 2026-11-05 survey's reading: past the date on our records, not a legal finding. |
| `evidence_gaps` | What the row could not confirm. |

## Check it

    python3 -m campaign_tool.records.tracker_proposal data/slo-pilot/MASTER-REQUEST-TRACKER.proposed.csv \
        --mail-index <mail store message index> --expect-rows 17

The command only reads. It reports malformed dates, bases, statuses, item numbering and source ids that the
index does not hold. A clean result says the file is well formed, not that its facts are right.
