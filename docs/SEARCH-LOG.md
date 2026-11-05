# Counting a saved ALPR search log (private)

`kit-search-log` summarizes an access or search log that an agency produced in its native format (for example a
California audit log) and that you saved locally as a UTF-8 CSV. It prints counts only. It is private and read-only:
the file is not copied, ingested, fetched, saved or published, and no ledger, store or campaign file is written.

```sh
python3 -m campaign_tool kit-search-log --directory ../my-campaign --csv ~/saved/log.csv --agency ca-example-police [--json]
```

`--campaign NAME` replaces `--directory`. `--agency` must be an `agency_id` in `kit/agencies.json` (selected or not).

**A count is a lead, not evidence.** A search with a vague or missing purpose is a question to ask the agency, not a finding.
Purpose matching is a coarse categorization, never a legal conclusion or proof of misuse.

## What is counted

* Rows read, and rows skipped by reason: `blank_row` (all cells empty), `column_count` (wrong number of cells),
  `invalid_time` (blank, unsupported or impossible time). A row is counted once, under the first reason that applies.
  A blank purpose is a valid counted search. A syntax error or bad UTF-8 refuses the whole file.
* Searches per month (`YYYY-MM`), per searching organization and state (exact spellings, nothing merged), and per purpose category.
* Dates are as stated in the log, with no timezone conversion. Accepted: `YYYY-MM-DD`; `YYYY-MM-DD[T or space]HH:MM[:SS[.fraction]][Z or ±HH:MM]`;
  `MM/DD/YYYY HH:MM:SS` with optional AM/PM. Anything else is skipped as `invalid_time`.

## Columns

The header is matched ignoring case, spaces and underscores. Time: `timestamp, time, date, search time, search date, searched at`.
Purpose: `purpose, reason, search purpose, stated purpose, justification`. Organization (optional): `organization, organisation,
searching organization, agency, searching agency`. State (optional, used with the organization): `state, searching state, agency state`.
A missing time or purpose column is refused by flag name. If several columns match, or your headers differ, name the exact
header with `--time-col`, `--reason-col`, `--org-col`, `--state-col`. These flags must name the organization and state columns only:
never a user, case or plate column. User, case and plate columns are never read.

## Purpose categories

In order: `blank_or_na` (empty, `n/a`, `na`, `none`, `null`, `not applicable`), `immigration_related` (whole words or phrases:
immigration, immigrant, deportation, deport, ice, cbp, border patrol, homeland security; `ice` does not match `office`),
`vague` (the whole purpose is `investigation` or `inv`), `other`. The text is classified and dropped: it is never printed or written.

`--categories FILE` (UTF-8 JSON, at most 64 KiB) replaces all three lists. It needs exactly the keys `blank_or_na`, `vague`,
`immigration_related`, each an array of at most 100 distinct strings of at most 80 characters. An empty purpose is always `blank_or_na`.

## --json (schema_version 1)

```json
{
  "schema_version": 1,
  "agency_id": "ID", "agency_name": "name from kit/agencies.json",
  "input": {"sha256": "64 hex of the exact file bytes", "columns": {"time": "header", "reason": "header", "org": null, "state": null},
            "categories_sha256": null},
  "period": {"first_date": null, "last_date": null},
  "rows": {"warning": "A count is a lead, not evidence", "read": 0, "counted": 0,
           "skipped": {"blank_row": 0, "column_count": 0, "invalid_time": 0}},
  "months": {"warning": "A count is a lead, not evidence", "rows": []},
  "organizations": {"warning": "A count is a lead, not evidence", "unattributed": 0, "rows": []},
  "purposes": {"warning": "A count is a lead, not evidence", "counts":
               {"blank_or_na": 0, "immigration_related": 0, "vague": 0, "other": 0}}
}
```

Month rows are `{month, count}`; organization rows `{name, state, count}`. read = counted + skipped; month counts and purpose counts each add up to counted;
organization rows plus `unattributed` add up to counted. Without an organization column `unattributed` is all counted rows.
Output holds no file path, time of run, purpose text, user, case or plate value.

## Limits

At most 100 MiB, a regular file (no symlink, FIFO or directory), streamed. A file that changes while it is read is refused
(best-effort detection, not a snapshot guarantee). An organization or state cell with an email, phone or plate pattern, or a control
character, refuses the run. Errors never repeat cell contents or the path.
