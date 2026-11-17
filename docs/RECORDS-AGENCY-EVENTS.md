# Agency events from mail receipts

`records agency-events --receipts DIR --agency-aliases FILE --out DIR` reads a mail-history store
(read-only, never fetches, never sends) and writes `events.tsv`, `manual-events.jsonl` and
`unclassified.tsv` into a new or empty `--out` (mode 0600, not inside `--receipts`). Refusals print
`agency-events: <code>` and exit 1: `aliases_invalid`, `sync_status_unhealthy`, `out_not_empty`,
`out_inside_receipts`.

## Aliases JSON (<= 64 KiB, no duplicate keys)

`{"agency": "...", "agency_id": "slug", "event_prefix": "ABC", "aliases": ["3-80 chars", ...],
"requests": {"26-348": "request_id"}}`. A message matches when its From, To, Cc or Subject, lower-cased
with non-alphanumerics removed, contains an alias in the same form. The request ref is the first
`NN-NNN` in the Subject that is a key of `requests`.

## Input contract

`SYNC-STATUS.json` with an empty `failures` list; `receipts/*.json` (regular files, <= 1 MiB; symlinks and
unparseable files are counted in `bad_receipts`) with `folder`, `uid`, `internaldate`
(`DD-Mon-YYYY HH:MM:SS +ZZZZ`) and `headers` lists (`From To Cc Subject Message-ID Date`).
Optional `MESSAGE-INDEX.csv` supplies a missing `internaldate`.

## Output

- Times convert from UTC to America/Los_Angeles (`PDT`/`PST`); the date is the Pacific date.
- `receipt basis: recorded` = `internaldate`; `proxy` = only the `Date` header parsed. Neither: `no_time`.
- Event types: `request_sent` (earliest Sent per ref), `follow_up_sent`, `clock_start`, `clock_due`.
  Text is a fixed template; subject and body are never copied.
- Clock rows (Gov. Code 7922.535, 10 calendar days, INFERRED): start is the send date, or the next day at
  17:00 local or later; weekends and holidays are not rolled and no extension is applied.
- `manual-events.jsonl` has one `sent` / `our follow-up sent` line per sent row (read by the timeline).
- `unclassified.tsv` lists every other message (`no_agency_match`, `no_request_ref`, `request_unmapped`,
  `no_time`, `inbound_needs_review`); inbound mail is classified by the operator. Bad receipts are
  listed as `bad_receipt`, with `receipts/` plus the JSON-quoted filename in the folder column and
  blank uid/date (the filename is escaped to preserve TSV structure). Nothing is dropped.

Machine output for review; not a legal finding.

## Validate

`records agency-events --validate EVENTS_TSV [--json]` checks an existing events.tsv, reads one file (regular,
not a symlink, <= 2 MiB, UTF-8) and writes nothing. It prints `events.tsv line N: code (column)` (at most 200
lines; never cell text) and exits 1 on any problem. Codes: `file_invalid`, `header_invalid` (stops the check),
`row_columns`, `event_id_missing`, `event_id_duplicate`, `event_type_unknown` (schema types plus
`request_received`, `reconsideration_sent`), `date_invalid`, `time_invalid` (`HH:MM PDT|PST` or empty),
`basis_invalid` (`DATED` or `INFERRED`), `request_ref_missing`, `angle_bracket`, `date_is_utc_date` (row date equals
the UTC date of a `...Z` stamp in source or clock_note whose Pacific date is earlier), `overdue_wording`
(overdue/late/past due needs "on our records" and "not a legal finding" in clock_note).
